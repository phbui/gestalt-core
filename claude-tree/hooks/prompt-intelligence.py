#!/usr/bin/env python3
"""
Tier 2 per-prompt relevance injection for gestalt memory system.
Queries Graphiti, gestalt_search, and Letta domain blocks concurrently.
Uses RRF to merge results, applies token budget, and outputs additionalContext JSON.
"""
from __future__ import annotations  # the laptop2 runs Python 3.9: `float | None` in a signature fails at import without this

import sys
import json
import argparse
import asyncio
import re
import time
import hashlib
from difflib import SequenceMatcher
from pathlib import Path

REPEAT_NUDGE = (
    "You've asked for something like this before in this session — consider "
    "codifying it as a skill/rule (see codify-repeated-requests.md) before proceeding."
)

# Below this, difflib similarity on short/generic prompts ("run the tests", "ok
# do it") produces noisy matches unrelated to genuine repeats. Tune only with a
# stdlib-only test proving 0.6 too noisy on real transcripts.
REPEAT_SIMILARITY_THRESHOLD = 0.6

PARALLELISM_HINTS = (
    "BEFORE RESPONDING: "
    "(1) Decompose this prompt into subtasks. If 2+ are independent, spawn parallel agents — "
    "up to 6 per wave. Use Explore agents for read-only work, general-purpose for writes/research. "
    "Even 2 parallel agents = 50% faster. "
    "(2) If the task matches a gestalt skill, suggest it: "
    "/investigate (deep research), /discuss (structured critique), /build (parallel workstreams), "
    "/fix (batch fixes), /audit (quality gate). "
    "Suggest before auto-invoking expensive skills."
)


def static_output() -> str:
    return json.dumps({"additionalContext": PARALLELISM_HINTS})


def _iter_transcript_user_texts(transcript_path: str):
    """Yield user-turn text (oldest to newest) from a Claude Code transcript.

    Transcript lines nest the actual message under entry["message"]["content"]
    (confirmed against live transcripts under .claude/memory/transcripts/ — every
    sampled "type":"user" entry carries "content" only inside "message", never at
    the top level), not entry["content"] directly. isMeta entries (local-command
    caveats, etc.) are skipped.
    """
    if not transcript_path or not Path(transcript_path).exists():
        return
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception:
        return

    for line in lines:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        message = entry.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        text = str(content).strip()
        if text:
            yield text[:500]


def extract_prompt(stdin_data: dict) -> str:
    """Extract user prompt text from hook payload. Try direct field, fall back to transcript."""
    # Try direct field (Claude Code may provide this)
    if "prompt" in stdin_data:
        return str(stdin_data["prompt"])[:500]

    # Fall back to last user message in transcript
    transcript_path = stdin_data.get("transcript_path", "")
    last_text = ""
    for text in _iter_transcript_user_texts(transcript_path):
        last_text = text
    return last_text


def extract_recent_user_prompts(stdin_data: dict, limit: int = 20) -> list[str]:
    """Return up to the last `limit` user-turn texts already in the transcript.

    Degrades to [] silently if the transcript is missing, unreadable, or empty —
    callers must treat that as "no history available", never as an error.
    """
    transcript_path = stdin_data.get("transcript_path", "")
    try:
        texts = list(_iter_transcript_user_texts(transcript_path))
    except Exception:
        return []
    return texts[-limit:]


def _normalize_for_similarity(text: str) -> str:
    """Lowercase and strip punctuation for a cheap, stdlib-only similarity check."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def repeat_request_nudge(current_prompt: str, history: list[str]) -> str:
    """Return REPEAT_NUDGE if `current_prompt` fuzzy-matches an OLDER prompt in
    `history`, else "". Uses difflib.SequenceMatcher (stdlib, no network/LLM call).

    The most recent history entry (the immediately preceding prompt) is excluded
    from comparison on purpose: matching against it would nag on ordinary
    back-to-back retries/clarifications ("no I meant X") rather than catch a
    genuine second ask separated by at least one other turn.
    """
    current_norm = _normalize_for_similarity(current_prompt)
    if len(current_norm) < 8 or not history:
        return ""

    older = history[:-1]
    for past in older:
        past_norm = _normalize_for_similarity(past)
        if not past_norm or past_norm == current_norm:
            # Exact-duplicate-of-something-older still counts as a repeat, but an
            # exact duplicate of the LITERAL previous prompt was already excluded
            # by dropping history[-1] above.
            return REPEAT_NUDGE
        ratio = SequenceMatcher(None, current_norm, past_norm).ratio()
        if ratio >= REPEAT_SIMILARITY_THRESHOLD:
            return REPEAT_NUDGE
    return ""


async def retrieve_graphiti(
    prompt: str, url: str, timeout: float, limit: int
) -> list[dict]:
    """Query Graphiti search_memory_facts via Streamable HTTP MCP."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Health check
            try:
                health = await client.get(f"{url}/health")
                if health.status_code != 200:
                    return []
            except Exception:
                return []

            # MCP initialize
            init_resp = await client.post(
                f"{url}/mcp",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "gestalt-prompt-hook", "version": "1.0"},
                    },
                },
            )
            session_id = init_resp.headers.get("Mcp-Session-Id", "")
            if not session_id:
                return []

            # Notify initialized
            await client.post(
                f"{url}/mcp",
                headers={
                    "Mcp-Session-Id": session_id,
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            # Search
            resp = await client.post(
                f"{url}/mcp",
                headers={
                    "Mcp-Session-Id": session_id,
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "search_memory_facts",
                        "arguments": {
                            "query": prompt,
                            "max_facts": limit,
                            "group_id": "gestalt",
                        },
                    },
                },
            )

            data = resp.json()
            if data.get("result", {}).get("isError"):
                return []

            content = data.get("result", {}).get("content", [])
            facts = []
            for i, item in enumerate(content):
                text = item.get("text", "").strip()
                if text:
                    facts.append({
                        "source": "graphiti",
                        "rank": i,
                        "text": text[:300],
                        "type": "fact",
                        "key": f"graphiti:{text[:80]}",
                    })
            return facts
    except Exception:
        return []


def retrieve_gestalt_sync(prompt: str, gestalt_dir: str, limit: int) -> list[dict]:
    """Query the lexical (FTS5/BM25) index. Synchronous, and must stay that way.

    Uses gestalt_search_fts, not gestalt_search: the hybrid path loads an embedding
    model and cannot finish inside this hook's timeout ([[gestalt#^fts-path]]).

    Do not move this into a thread pool. Cancelling a task that wraps
    run_in_executor does not stop the thread, and asyncio.run() joins the executor
    on the way out, so a "timed out" retrieval still pays full cost.
    """
    tools_dir = str(Path(gestalt_dir) / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        from gestalt_mcp_server import gestalt_search_fts  # type: ignore

        results = gestalt_search_fts(prompt, limit=limit)
    except Exception:
        return []

    # Dedupe by slug: three sections of one entry are one reference to the reader.
    out: list[dict] = []
    seen: set[str] = set()
    for r in results:
        slug = r.get("slug", "")
        if not slug or slug in seen:
            continue
        # L7 (2026-10-06): a restricted entry is never injected, not even as a reference. gestalt_search_fts
        # already swaps it for a `withheld` notice row. This skip keeps the hook safe on its own too.
        if r.get("withheld") or r.get("sensitivity") == "restricted":
            continue
        seen.add(slug)
        out.append({
            "source": "gestalt",
            "rank": len(out),
            "slug": slug,
            "heading": r.get("heading", ""),
            "block_id": r.get("block_id", ""),
            # References, not content: ~20 tokens for three hits. That cheapness is
            # why no relevance gate is applied ([[gestalt#^bm25-thresholds]]).
            "text": "",
            "type": "entry",
            "key": f"gestalt:{slug}#{r.get('block_id', '')}",
        })
    return out


async def retrieve_letta_blocks(prompt: str, state_dir: str) -> list[dict]:
    """Keyword-match prompt against cached Tier 2 Letta blocks."""
    cache_path = Path(state_dir) / "tier2-cache.json"
    if not cache_path.exists():
        return []

    try:
        with open(cache_path, "r") as f:
            cache = json.load(f)
    except Exception:
        return []

    prompt_words = set(re.findall(r"[a-z]{3,}", prompt.lower()))
    if not prompt_words:
        return []

    scored = []
    for block in cache.get("blocks", []):
        block_keywords = set(block.get("keywords", []))
        if not block_keywords:
            continue
        overlap = prompt_words & block_keywords
        if not overlap:
            continue
        score = len(overlap) / max(len(block_keywords), 1)
        label = block.get("label", "")
        value = block.get("value", "")
        scored.append({
            "source": "letta",
            "score": score,
            "label": label,
            "text": value[:300],
            "type": "block",
            "key": f"letta:{label}",
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    for i, item in enumerate(scored):
        item["rank"] = i
    return scored[:3]


# Which sources answered inside their timeout on this run. Filled by main() and retrieve_all(), read by
# log_injection() (X7, 2026-10-06). The values are only booleans, so nothing private reaches the log.
_SOURCE_STATUS: dict[str, bool] = {}

INJECTION_LOG = "injections.jsonl"
INJECTION_LOG_MAX_BYTES = 1_000_000


def _item_id(item: dict) -> str:
    """Slug or id for the log. A gestalt key is already a slug. A letta key is a block label. A graphiti
    key is a text prefix of a private fact, so it is hashed and never written out."""
    key = item.get("key", "")
    src = item.get("source", "")
    if src == "graphiti":
        return item.get("uuid") or hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]
    return key.split(":", 1)[1] if ":" in key else key


def build_injection_record(prompt: str, results: dict[str, list[dict]], injected: list[dict],
                           status: dict[str, bool], now: float | None = None) -> dict:
    """One record per prompt. Holds a hash of the prompt, never the prompt text."""
    ranks = {}
    for src, lst in results.items():
        for r, item in enumerate(lst):
            ranks.setdefault(item.get("key", ""), r + 1)
    return {
        "ts": int(now if now is not None else time.time()),
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16],
        "answered": {k: bool(v) for k, v in status.items()},
        "returned": {k: len(v) for k, v in results.items()},
        "items": [
            {"source": it.get("source", ""), "id": _item_id(it), "rank": ranks.get(it.get("key", "")),
             "merged_rank": i + 1}
            for i, it in enumerate(injected)
        ],
    }


def log_injection(state_dir: str, record: dict) -> None:
    """Append one JSON line to injections.jsonl. Fails open: any error is swallowed, nothing is printed.
    The file is capped by size: past the cap the older half is dropped. No network, no subprocess."""
    try:
        path = Path(state_dir) / INJECTION_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > INJECTION_LOG_MAX_BYTES:
            data = path.read_bytes()
            keep = data[len(data) // 2:]
            keep = keep[keep.find(b"\n") + 1:]  # start on a whole line
            path.write_bytes(keep)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:  # fail open: the injection log is never on the critical path
        print(f"prompt-intelligence: injection log write failed: {e}", file=sys.stderr)


async def retrieve_all(prompt: str, args) -> dict[str, list[dict]]:
    """Run all three retrievals concurrently with individual timeouts."""
    outer_timeout = max(args.graphiti_timeout, 0.5) + 0.2

    # gestalt is not in this gather: it is a synchronous few-millisecond query, and
    # a thread pool here is what made the timeout unenforceable.
    tasks = {
        "graphiti": asyncio.create_task(
            retrieve_graphiti(prompt, args.graphiti_url, args.graphiti_timeout, args.search_limit)
        ),
        "letta": asyncio.create_task(
            retrieve_letta_blocks(prompt, args.state_dir)
        ),
    }

    done, _ = await asyncio.wait(list(tasks.values()), timeout=outer_timeout)

    results: dict[str, list[dict]] = {}
    for name, task in tasks.items():
        if task in done and not task.cancelled():
            _SOURCE_STATUS[name] = True
            try:
                results[name] = task.result() or []
            except Exception:
                _SOURCE_STATUS[name] = False
                results[name] = []
        else:
            _SOURCE_STATUS[name] = False
            task.cancel()
            results[name] = []

    return results


def rrf_merge(results: dict[str, list[dict]], k: int = 60) -> list[dict]:
    """Merge ranked lists using Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for result_list in results.values():
        for rank, item in enumerate(result_list):
            key = item.get("key", f"{item['source']}:{item.get('text', '')[:50]}")
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in items:
                items[key] = item

    sorted_keys = sorted(scores, key=lambda k: scores[k], reverse=True)
    merged = []
    for key in sorted_keys:
        item = items[key].copy()
        item["rrf_score"] = scores[key]
        merged.append(item)
    return merged


def apply_threshold(results: list[dict], threshold: float) -> list[dict]:
    return [r for r in results if r.get("rrf_score", 0.0) >= threshold]


def apply_token_budget(results: list[dict], max_tokens: int) -> list[dict]:
    budgeted = []
    total_chars = 0
    char_budget = max_tokens * 4  # ~4 chars per token

    for item in results:
        item_chars = len(item.get("text", "")) + 60
        if total_chars + item_chars > char_budget:
            break
        budgeted.append(item)
        total_chars += item_chars
    return budgeted


def deduplicate(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for item in results:
        fingerprint = re.sub(r"\s+", " ", item.get("text", "")[:100].lower()).strip()
        # Reference-only items (gestalt entries) carry no text by design, so fall
        # back to their key. Without this an empty fingerprint is falsy and the
        # item is dropped rather than deduplicated.
        if not fingerprint:
            fingerprint = item.get("key", "")
        if fingerprint and fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(item)
    return unique


def format_context(results: list[dict]) -> str:
    if not results:
        return ""

    facts = [r for r in results if r["type"] == "fact"]
    entries = [r for r in results if r["type"] == "entry"]
    blocks = [r for r in results if r["type"] == "block"]

    sections = []

    if facts:
        fact_lines = "\n".join(f"- {r['text']}" for r in facts)
        sections.append(f"**Facts:** {fact_lines}")

    if entries:
        entry_refs = []
        for r in entries:
            slug = r.get("slug", "")
            block_id = r.get("block_id", "")
            heading = r.get("heading", "")
            ref = f"[[{slug}{'#' + block_id if block_id else ''}]]"
            if heading:
                ref += f" — {heading}"
            entry_refs.append(ref)
        sections.append("**Gestalt:** " + ", ".join(entry_refs))

    if blocks:
        block_lines = "\n".join(f"- `{r['label']}`: {r['text']}" for r in blocks)
        sections.append(f"**Memory:** {block_lines}")

    if not sections:
        return ""

    return "## Relevant Context\n\n" + "\n\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description="Gestalt prompt relevance injection")
    parser.add_argument("--gestalt-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--letta-url", default="http://localhost:8283/v1")
    parser.add_argument("--graphiti-url", default="http://localhost:8200")
    parser.add_argument("--tier2-max-tokens", type=int, default=700)
    parser.add_argument("--search-limit", type=int, default=3)
    parser.add_argument("--graphiti-timeout", type=float, default=0.4)
    parser.add_argument("--relevance-threshold", type=float, default=0.01)
    args = parser.parse_args()

    # Read stdin
    try:
        raw = sys.stdin.read().strip()
        stdin_data = json.loads(raw) if raw else {}
    except Exception:
        stdin_data = {}

    prompt_text = extract_prompt(stdin_data)

    # Cheap, local, no network/LLM call — must never crash or block the rest of
    # the hook, so any failure here degrades to "no nudge".
    try:
        nudge = repeat_request_nudge(
            prompt_text, extract_recent_user_prompts(stdin_data, limit=20)
        )
    except Exception:
        nudge = ""

    if not prompt_text:
        if nudge:
            print(json.dumps({"additionalContext": PARALLELISM_HINTS + "\n\n" + nudge}))
        else:
            print(static_output())
        return

    # Check if httpx is available (required for Graphiti)
    try:
        __import__("httpx")
        _has_httpx = True
    except ImportError:
        _has_httpx = False

    if not _has_httpx:
        # Graphiti requires httpx — fall back to gestalt + letta only
        async def retrieve_no_httpx(p, a):
            task = asyncio.create_task(retrieve_letta_blocks(p, a.state_dir))
            done, _ = await asyncio.wait([task], timeout=0.6)
            _SOURCE_STATUS["graphiti"] = False  # httpx missing: never asked
            _SOURCE_STATUS["letta"] = task in done
            return {"graphiti": [], "letta": task.result() if task in done else []}
        results = asyncio.run(retrieve_no_httpx(prompt_text, args))
    else:
        results = asyncio.run(retrieve_all(prompt_text, args))

    # Synchronous, outside the event loop, so its cost is bounded by its own speed.
    results["gestalt"] = retrieve_gestalt_sync(
        prompt_text, args.gestalt_dir, args.search_limit
    )
    _SOURCE_STATUS["gestalt"] = True

    merged = rrf_merge(results)
    filtered = apply_threshold(merged, args.relevance_threshold)
    budgeted = apply_token_budget(filtered, args.tier2_max_tokens)
    deduped = deduplicate(budgeted)

    context = format_context(deduped)

    if context:
        additional = context + "\n\n---\n\n" + PARALLELISM_HINTS
    else:
        additional = PARALLELISM_HINTS

    if nudge:
        additional += "\n\n" + nudge

    print(json.dumps({"additionalContext": additional}))
    sys.stdout.flush()  # hook output is delivered before the log write, which is never on the critical path
    try:
        log_injection(args.state_dir, build_injection_record(prompt_text, results, deduped, _SOURCE_STATUS))
    except Exception as e:  # fail open: a logging error never changes the hook's exit status
        print(f"prompt-intelligence: injection record failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
