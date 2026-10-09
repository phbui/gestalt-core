"""Supersession and freshness for gestalt notes.

A note may say `supersedes: [slug, ...]`, `superseded_by: slug` and `valid_until: YYYY-MM-DD` in its frontmatter.
The index builder stores them in sections_meta. The ranker demotes superseded and expired sections and never drops one.
Everything is off until GESTALT_FRESHNESS=on.

Knobs:
    GESTALT_FRESHNESS=off|on              default off
    GESTALT_FRESHNESS_FACTOR=0.5          score multiplier for a superseded section, 0 to 1
    GESTALT_FRESHNESS_EXPIRED_FACTOR=0.25 score multiplier for a section past valid_until, 0 to 1
"""

from __future__ import annotations

import datetime as _dt
import os
import re

COLUMNS = (
    ("superseded_by", "TEXT"),
    ("valid_until", "TEXT"),
    ("note_modified", "TEXT"),
    ("note_modified_source", "TEXT"),
    ("supersedes", "TEXT"),
)
DEFAULT_FACTOR = 0.5
DEFAULT_EXPIRED_FACTOR = 0.25
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# --- knobs ------------------------------------------------------------------------------------------------

def enabled() -> bool:
    raw = os.environ.get("GESTALT_FRESHNESS", "").strip().lower()
    if raw in ("", "off"):
        return False
    if raw == "on":
        return True
    raise ValueError(f"GESTALT_FRESHNESS must be off or on, got {raw!r}")


def _factor_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number from 0 to 1, got {raw!r}") from None
    if not 0.0 <= val <= 1.0:
        raise ValueError(f"{name} must be a number from 0 to 1, got {raw!r}")
    return val


def factor() -> float:
    return _factor_from_env("GESTALT_FRESHNESS_FACTOR", DEFAULT_FACTOR)


def expired_factor() -> float:
    return _factor_from_env("GESTALT_FRESHNESS_EXPIRED_FACTOR", DEFAULT_EXPIRED_FACTOR)


# --- frontmatter ------------------------------------------------------------------------------------------

def _unquote(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val.strip()


def _slug(val: str) -> str:
    """A slug from `x`, `x.md`, `knowledge/x.md` or `[[x]]`."""
    val = _unquote(val).strip("[]").split("|")[0].strip()
    val = val.rsplit("/", 1)[-1]
    return val.removesuffix(".md")


def _date(val: str) -> str | None:
    """The leading YYYY-MM-DD of val when it is a real date, else None."""
    m = _DATE_RE.match(_unquote(val))
    if not m:
        return None
    try:
        _dt.date.fromisoformat(m.group(0))
    except ValueError:
        return None
    return m.group(0)


def parse_front(text: str) -> dict:
    """Read the freshness keys from the frontmatter of a note.

    Returns supersedes (list of slugs), superseded_by (slug or None), valid_until (date text or None) and modified (date text or None).
    `modified` falls back to `updated`, which most notes carry. A bad date is ignored. Frontmatter is flat key: value lines.
    A block list under `supersedes:` also works.
    """
    out: dict = {"supersedes": [], "superseded_by": None, "valid_until": None, "modified": None}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    lines = text[3:end if end != -1 else 0].splitlines()
    vals: dict[str, str] = {}
    blocks: dict[str, list[str]] = {}
    key = None
    for line in lines:
        if line.startswith((" ", "\t", "- ")):
            if key and line.strip().startswith("- "):
                blocks.setdefault(key, []).append(line.strip()[2:])
            continue
        k, sep, v = line.partition(":")
        key = k.strip() if sep else None
        if key:
            vals[key] = v.strip()
    raw = vals.get("supersedes", "")
    if raw:
        items = raw.strip("[]").split(",")
    else:
        items = blocks.get("supersedes", [])
    out["supersedes"] = [s for s in (_slug(i) for i in items) if s]
    out["superseded_by"] = (_slug(vals["superseded_by"]) or None) if "superseded_by" in vals else None
    out["valid_until"] = _date(vals["valid_until"]) if "valid_until" in vals else None
    for k in ("modified", "updated"):
        if k in vals and _date(vals[k]):
            out["modified"] = _date(vals[k])
            break
    return out


# --- index schema -----------------------------------------------------------------------------------------

def ensure_columns(db) -> list[str]:
    """Add the freshness columns to sections_meta when missing. Returns the names it added."""
    have = {r[1] for r in db.execute("PRAGMA table_info(sections_meta)")}
    added = []
    for name, kind in COLUMNS:
        if name not in have:
            db.execute(f"ALTER TABLE sections_meta ADD COLUMN {name} {kind}")
            added.append(name)
    return added


def load_meta(db, ids) -> dict:
    """{id: {freshness columns}} for the given section ids. An index without the columns answers with nothing."""
    have = {r[1] for r in db.execute("PRAGMA table_info(sections_meta)")}
    cols = [n for n, _ in COLUMNS if n in have]
    ids = list(ids)
    if not cols or not ids:
        return {}
    out: dict = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.execute(f"SELECT id, {', '.join(cols)} FROM sections_meta WHERE id IN ({marks})", chunk):
            out[r[0]] = dict(zip(cols, r[1:]))
    return out


# --- dates and labels -------------------------------------------------------------------------------------

def _as_date(value) -> _dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text).date()
    except ValueError:
        pass
    ok = _date(text)
    return _dt.date.fromisoformat(ok) if ok else None


def age_days(note_modified, now) -> int | None:
    mod, today = _as_date(note_modified), _as_date(now)
    if mod is None or today is None:
        return None
    return max((today - mod).days, 0)


def age_label(note_modified, now) -> str:
    """A short age: "today", "3 days", "2 months", "1 year". Empty when the date is unknown."""
    days = age_days(note_modified, now)
    if days is None:
        return ""
    if days < 1:
        return "today"
    if days < 30:
        n, unit = days, "day"
    elif days < 365:
        n, unit = days // 30, "month"
    else:
        n, unit = days // 365, "year"
    return f"{n} {unit}{'' if n == 1 else 's'}"


# --- ranking ----------------------------------------------------------------------------------------------

def _freshness(meta: dict | None, now) -> dict:
    meta = meta or {}
    until = _as_date(meta.get("valid_until"))
    today = _as_date(now)
    expired = bool(until and today and today > until)
    superseded_by = meta.get("superseded_by") or None
    supersedes = meta.get("supersedes") or []
    if isinstance(supersedes, str):
        supersedes = [s for s in supersedes.split(",") if s]
    return {
        "superseded_by": superseded_by,
        "supersedes": supersedes,
        "valid_until": meta.get("valid_until") or None,
        "age_days": age_days(meta.get("note_modified"), now),
        "age": age_label(meta.get("note_modified"), now),
        "expired": expired,
        "demoted": bool(superseded_by) or expired,
        "multiplier": 1.0,
    }


def _scale(score: float, mult: float) -> float:
    """Lower a score by mult for either sign, so a negative rerank logit also drops."""
    if score >= 0:
        return score * mult
    return score / mult if mult else float("-inf")


def demote(rows, meta_lookup, now, factor=DEFAULT_FACTOR, expired_factor=DEFAULT_EXPIRED_FACTOR, *, active=None):
    """Return the rows with superseded and expired sections demoted.

    rows are dicts with `id` and `score`. meta_lookup is a dict {id: meta} or a function id -> meta, holding the sections_meta freshness columns.
    A superseded row's score is multiplied by factor. A row past valid_until is multiplied by expired_factor. A row that is both takes the smaller multiplier.
    Each returned row is a copy with a `freshness` dict: superseded_by, valid_until, age_days, demoted and more.
    The sort is stable on the new score, so equal scores keep their input order. No row is dropped.
    With the knob off (GESTALT_FRESHNESS, or active=False) the rows come back unchanged.
    """
    if not (enabled() if active is None else active):
        return list(rows)
    get = meta_lookup if callable(meta_lookup) else meta_lookup.get
    out = []
    for pos, row in enumerate(rows):
        fr = _freshness(get(row["id"]), now)
        mult = 1.0
        if fr["superseded_by"]:
            mult = min(mult, factor)
        if fr["expired"]:
            mult = min(mult, expired_factor)
        fr["multiplier"] = mult
        new = dict(row)
        new["score"] = _scale(row["score"], mult)
        new["freshness"] = fr
        out.append((new["score"], pos, new))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in out]


def apply_to_result(db, result, now=None) -> None:
    """Demote a gestalt_rank SearchResult in place. Does nothing when the knob is off.

    It reorders result.rows, scales result.scores and result.fused_scores for demoted ids, and sets result.freshness = {id: freshness dict}.
    It works on the rows already cut to the limit, so a demoted row drops down but nothing outside the cut comes in.
    """
    if not enabled() or not result.rows:
        return
    now = now or _dt.date.today()
    fct, efct = factor(), expired_factor()
    ids = [r["id"] for r in result.rows]
    # Rank order is the fallback score for a row with none, so an unscored result still keeps its order.
    rows = [{"id": i, "score": result.scores.get(i, float(len(ids) - p))} for p, i in enumerate(ids)]
    out = demote(rows, load_meta(db, ids), now, fct, efct, active=True)
    by_id = {r["id"]: r for r in result.rows}
    result.rows = [by_id[o["id"]] for o in out]
    result.freshness = {o["id"]: o["freshness"] for o in out}
    for o in out:
        mult = o["freshness"]["multiplier"]
        if mult != 1.0:
            for d in (result.scores, result.fused_scores):
                if o["id"] in d:
                    d[o["id"]] = _scale(d[o["id"]], mult)


def snippet_prefix(row, now=None) -> str:
    """Text the hook and the MCP server put before a snippet. Empty when the knob is off.

    row is a dict carrying `freshness` (from demote or apply_to_result) or the sections_meta freshness columns.
    Example: "[updated 3 days ago, supersedes x] ".
    """
    if not enabled():
        return ""
    fr = row.get("freshness") if hasattr(row, "get") else None
    if fr is None:
        fr = _freshness(dict(row), now or _dt.date.today())
    parts = []
    if fr.get("age"):
        parts.append("updated today" if fr["age"] == "today" else f"updated {fr['age']} ago")
    if fr.get("supersedes"):
        parts.append("supersedes " + ", ".join(fr["supersedes"]))
    if fr.get("superseded_by"):
        parts.append(f"superseded by {fr['superseded_by']}")
    if fr.get("expired"):
        parts.append(f"expired {fr['valid_until']}")
    return f"[{', '.join(parts)}] " if parts else ""
