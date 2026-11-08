"""Read and edit Phi's OWN Handshake (the university) student profile through a persistent agent Chromium profile.

Sibling of tools/linkedin-agent-browser.py. The profile ~/.local/share/agent-chromium/handshake-nyu
holds Phi's Handshake login, signed in once by him through the university SSO and Duo in a headed window
(`login` mode). Nothing else is logged in there. Never copy the profile to another host and never
copy cookies in from another browser (the LinkedIn incidents of 2026-09-22, 2026-10-26, 2026-11-05).

Run from any directory, with the tool sandbox disabled:

    uv run --with playwright python3 tools/handshake-agent-browser.py login            # headed, Phi signs in once
    uv run --with playwright python3 tools/handshake-agent-browser.py check            # exit 0 logged in, 2 not
    uv run --with playwright python3 tools/handshake-agent-browser.py dump [<url> ...]  # text + html + screenshot per page
    uv run --with playwright python3 tools/handshake-agent-browser.py edit PLAN.json [--dry-run] [--out FILE]

`dump` with no URL reads the profile page. It writes <out-stem>-<n>.txt, .html and .png beside --out.
One page load every PAUSE seconds. Reading what the logged-in user could already see is the defensible
use (knowledge/<kb-entry>.md ^legal). This script opens Phi's own pages only. It never searches
other students, never bulk-collects and is never scheduled. Handshake's Terms (joinhandshake.com/tos,
2025-07-22) forbid scripted account creation and bulk collection of other users' data. They do not
address a student editing his own profile in a real browser. That gray area was accepted by Phi on
2026-11-07 for this narrow use.

EDIT MODE mirrors the LinkedIn tool: a JSON array of actions applied in order, kinds in SPECS,
--dry-run probes forms and types nothing, every save is read back, the first checkpoint stops the run.
Exit codes: 2 not logged in, 3 challenge or SSO wall, 4 failure or readback mismatch, 5 load budget.
"""
import argparse, json, os, random, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".local/share/agent-chromium/handshake-nyu"
PAUSE = 7
SAVE_GAP = 12
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
LOGIN_URL = "https://nyu.joinhandshake.com/login"
HOME_URL = "https://nyu.joinhandshake.com/"
PROFILE_URL = "https://nyu.joinhandshake.com/profiles/buwyp8"
# URL fragments that mean the session is gone or a challenge was served.
LOGIN_MARKERS = ("/login", "login.microsoftonline.com", "shibboleth", "duosecurity.com", "/auth/saml")
CHALLENGE_MARKERS = ("challenges.cloudflare.com", "/cdn-cgi/challenge")


def clear_stale_lock():
    lock = PROFILE / "SingletonLock"
    if not lock.is_symlink():
        return
    pid = os.readlink(lock).rsplit("-", 1)[-1]
    try:
        os.kill(int(pid), 0)
        return  # a live Chromium owns the profile
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (PROFILE / name).unlink()
        except FileNotFoundError:
            pass


def launch(p, headed=False):
    exes = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if not exes:
        sys.exit("no cached Chromium under ~/.cache/ms-playwright; run `uv run --with playwright playwright install chromium`")
    PROFILE.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(PROFILE), executable_path=str(exes[-1]), headless=not headed,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"], viewport={"width": 1280, "height": 900},
        user_agent=UA, locale="en-US", timezone_id="America/New_York",
    )


def session_state(url, title):
    if any(m in url for m in CHALLENGE_MARKERS) or "Just a moment" in title:
        return "challenge"
    if any(m in url for m in LOGIN_MARKERS):
        return "logged_out"
    return "ok"


def logged_in(page):
    r = page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    return {"status": r.status if r else None, "url": page.url, "title": page.title(),
            "state": session_state(page.url, page.title())}


# ---------------------------------------------------------------- edit mode
P = "https://nyu.joinhandshake.com/profiles/buwyp8"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
W, O, E = "#profile-workexperience-", "#profile-organization-", "#profile-education-"
def _hook(h):
    return [("css", f'[data-hook="{h}"]', 0)]
def _card(label):
    return [("aria", label, 0), ("aria_prefix", label, 0)]
DESC = ("ta", "textarea[name=description]")
# Field spec: (type, css[, css2]). Types: ta, input, select, monthyear (two selects, value "Sep 2025" or null),
# typeahead (search input, picks the exact role=option), datepick (button aria prefix, value "Feb 2024"),
# checkbox (bool). Every form is a modal except Projects, which edits inline inside the "Project editor" group.
# `card` is the aria-label of the edit button that reopens the item for readback, formatted from the action.
SPECS = {
    "summary": {"open": _hook("profile-section-SUMMARY-add"), "save": "Save", "fields": {"text": ("ta", "textarea[name=bio]")}},
    "main_info": {"open": _hook("profile-main-info-edit"), "save": "Save", "fields": {
        "headline": ("input", "input[name=headline]"), "first_name": ("input", "input[name=preferredName]")}},
    "work": {"open": _card("Edit {name} work experience"), "save": "Save and close", "needs_name": True, "fields": {
        "title": ("typeahead", W + "job-position-name-input"), "job_type": ("select", W + "job-type"),
        "start": ("monthyear", W + "start-month", W + "start-year"), "end": ("monthyear", W + "end-month", W + "end-year"),
        "location": ("typeahead", "#location"), "description": DESC}},
    "work_new": {"open": _hook("profile-section-WORKEXPERIENCE-add"), "save": "Save", "card": "Edit {employer} work experience",
        "present_text": "{title}", "readback_text": ["{title}", "{description}"],
        "fields": {"employer": ("typeahead", W + "employer-name-input"), "title": ("typeahead", W + "job-position-name-input"),
        "job_type": ("select", W + "job-type"), "start": ("monthyear", W + "start-month", W + "start-year"),
        "end": ("monthyear", W + "end-month", W + "end-year"), "location": ("typeahead", "#location"), "description": DESC}},
    "education": {"open": _card("Edit {name} education"), "save": "Save and close", "needs_name": True, "fields": {
        "area_of_study": ("typeahead", E + "majors"), "start": ("monthyear", E + "start-month", E + "start-year"),
        "end": ("monthyear", E + "end-month", E + "end-year"), "gpa": ("input", "input[name=cumulativeGpa]"),
        "hide_gpa": ("checkbox", "input[name=hideCumulativeGpa]"), "description": DESC}},
    "education_remove": {"open": _card("Edit {name} education"), "remove": "Remove", "needs_name": True, "fields": {}},
    "project": {"open": _card("Edit {name} project"), "inline": '[role=group][aria-label="Project editor"]', "save_click": 'h2:has-text("Projects")',
        "needs_name": True, "fields": {"title": ("input", "input[name=name]"), "role": ("input", "input[name=positionName]"),
        "start": ("datepick", "Start date"), "end": ("datepick", "End date"), "description": ("ta", "textarea[name=description]"),
        "url": ("input", "input[name=externalUrl]")}},
    "project_new": {"open": _hook("profile-section-PROJECT-add"), "inline": '[role=group][aria-label="Project editor"]', "save_click": 'h2:has-text("Projects")',
        "card": "Edit {name} project", "fields": {"name": ("input", "input[name=name]"), "role": ("input", "input[name=positionName]"),
        "start": ("datepick", "Start date"), "end": ("datepick", "End date"), "description": ("ta", "textarea[name=description]"),
        "url": ("input", "input[name=externalUrl]")}},
    "organization_new": {"open": _hook("profile-section-ORGANIZATION-add"), "save": "Save", "card": "Edit {organization} organization",
        "present_text": "{organization}", "readback_text": ["{organization}", "{position}"],
        "fields": {"organization": ("typeahead", O + "name-input"), "position": ("input", "input[name=position]"),
        "start": ("monthyear", O + "start-month", O + "start-year"), "end": ("monthyear", O + "end-month", O + "end-year"),
        "location": ("typeahead", "#location"), "description": DESC}},
    "course_new": {"open": _hook("profile-section-COURSE-add"), "save": "Save", "fields": {
        "name": ("ta", "#course-name-0-input"), "number": ("ta", "#course-code-0")}},
    # One save for a whole course list: the form grows a row per "Add course" click (data-hook add-course).
    "courses_bulk": {"open": _hook("profile-section-COURSE-add"), "save": "Save", "bulk": "courses", "fields": {}},
    "link_new": {"open": [("css", 'button:has-text("Add link")', 0)], "save": "Save", "card": "x", "present_text": "{title}", "readback_text": ["{title}"], "fields": {
        "title": ("input", "input[name=name]"), "url": ("input", "input[name=externalUrl]"), "description": DESC}},
    "language_new": {"open": _hook("profile-section-LANGUAGE_PROFICIENCIES-add"), "save": "Save", "fields": {}},
    "skills": {"open": _hook("profile-section-SKILL-add"), "save": "Save", "fields": {}},
    "looking_for": {"open": _hook("profile-section-LOOKING_FOR-add"), "save": "Save", "fields": {}},
    "links": {"open": [("css", 'button:has-text("Add link")', 0)], "fields": {}},
    "showcase_page": {"open": _card("Create a page for {name}"), "fields": {}},
    # A Showcase page per project is the only place Handshake shows project media: the page editor opens from the
    # project's "Create page" button with title, description and link prefilled, and takes a cover image
    # ("Best at 3:2, at least 1200px wide. Max file size 20 MB."). Readback: the Create button is gone afterwards.
    # After a page exists the profile's inline project editor is gone, and the project's title, description
    # and link are edited on the page itself through its "Edit project" button (2026-11-07).
    "showcase_edit": {"url": "https://nyu.joinhandshake.com/ai-showcase/projects/{page_id}", "needs_page": True,
        "open": [("css", 'button:has-text("Edit project")', 0)], "save": "Save", "fields": {
        "title": ("ta", "textarea:not([placeholder*='Introduce'])"), "description": ("ta", "textarea[placeholder*='Introduce']"),
        "url": ("input", "input[placeholder='https://']")}},
    "showcase_cover": {"open": _card("Create a page for {name}"), "needs_name": True, "cover": True, "card": "x", "fields": {}},
}
for _k in SPECS:
    SPECS[_k].setdefault("url", P)
LIMITS = {("summary", "text"): 1000, ("main_info", "headline"): 75, ("project_new", "name"): 255, ("project", "title"): 255,
          ("project_new", "description"): 1000, ("project", "description"): 1000}


class Stop(Exception):
    def __init__(self, code, why):
        super().__init__(why); self.code = code; self.why = why


def norm(t):
    return re.sub(r"\s+", " ", (t or "").replace(" ", " ")).strip()


def monthyear(v):
    """'Sep 2025' -> (8, 2025)."""
    m, y = v.split()
    return [x[:3].lower() for x in MONTHS].index(m[:3].lower()), int(y)


def validate_plan(plan):
    for i, act in enumerate(plan):
        k = act.get("kind")
        if k not in SPECS:
            sys.exit(f"action {i}: unknown kind {k!r} (kinds: {sorted(SPECS)})")
        if SPECS[k].get("needs_page") and not act.get("page_id"):
            sys.exit(f"action {i}: {k} needs a page_id")
        if SPECS[k].get("needs_name") and not act.get("name"):
            sys.exit(f"action {i}: {k} needs a name (the card's aria-label subject)")
        for f, v in act.items():
            if f in ("kind", "name", "page_id") or f not in SPECS[k]["fields"]:
                if f not in ("kind", "name", "steps", "page_id") and f != SPECS[k].get("bulk") and not (f == "file" and SPECS[k].get("cover")):
                    sys.exit(f"action {i}: {k} has no field {f!r}")
                continue
            typ = SPECS[k]["fields"][f][0]
            if typ in ("monthyear", "datepick") and v is not None:
                monthyear(v)
            lim = LIMITS.get((k, f))
            if lim and isinstance(v, str) and len(v) > lim:
                sys.exit(f"action {i}: {k}.{f} is {len(v)} chars, limit {lim}")


class Runner:
    def __init__(self, page, out, out_path, max_loads=None):
        self.page, self.out, self.out_path, self.max_loads = page, out, out_path, max_loads
        self.loads = 0; self.last_load = 0.0; self.last_save = 0.0

    def finish(self, code):
        Path(self.out_path).write_text(json.dumps(self.out, indent=1))
        json.dump(self.out, sys.stdout, indent=1); print()
        sys.exit(code)

    def nav(self, url):
        if self.max_loads is not None and self.loads >= self.max_loads:
            raise Stop(5, "load budget exhausted")
        wait = PAUSE - (time.time() - self.last_load)
        if self.loads and wait > 0:
            time.sleep(wait)
        r = self.page.goto(url, wait_until="domcontentloaded")
        self.last_load = time.time(); self.loads += 1
        self.page.wait_for_timeout(4000)
        u, t = self.page.url, self.page.title()
        st = session_state(u, t)
        if st == "challenge":
            raise Stop(3, f"challenge at {u} title={t!r}")
        if st == "logged_out":
            raise Stop(2, f"not logged in: {u}")
        return {"requested": url, "status": r.status if r else None, "url": u, "title": t,
                "missing": r is not None and r.status == 404}

    def dialog(self):
        d = self.page.locator("dialog[open], [role=dialog]")
        for i in range(d.count()):
            if d.nth(i).is_visible():
                return d.nth(i)
        return None

    def scope(self, kind=None):
        """The edit form container: the inline group for projects, else the open dialog, else the body."""
        if kind and SPECS[kind].get("inline"):
            g = self.page.locator(SPECS[kind]["inline"])
            if g.count():
                return g.first
        return self.dialog() or self.page.locator("body")

    def resolve(self, d, cands, select=False):
        for m, text, n in cands:
            loc = {"aria": lambda: d.locator(f'[aria-label="{text}"]'),
                   "aria_prefix": lambda: d.locator(f'[aria-label^="{text}"]'),
                   "ph": lambda: d.locator(f'[placeholder*="{text}" i]'),
                   "label": lambda: d.get_by_label(text),
                   "css": lambda: d.locator(text)}[m]()
            if loc.count() > n:
                return loc.nth(n), f"{m}:{text}[{n}]"
        return None, None

    @staticmethod
    def read(loc):
        return loc.evaluate("""e => {
          if (e.tagName === 'SELECT') return e.value;
          if (e.type === 'checkbox') return String(e.checked);
          if (e.value !== undefined) return e.value;
          return e.innerText || ''; }""")

    def current(self, kind, d, f):
        """Current value of a field in plan terms, or None if the control is absent."""
        spec = SPECS[kind]["fields"][f]; typ = spec[0]
        if typ == "monthyear":
            m, y = d.locator(spec[1]), d.locator(spec[2])
            if not m.count() or not y.count():
                return None
            mv, yv = self.read(m.first), self.read(y.first)
            return None if "-" in (mv, yv) or mv == "" else f"{MONTHS[int(mv)][:3]} {yv}"
        if typ == "datepick":
            b = d.locator(f'button[aria-label^="{spec[1]}"]')
            if not b.count():
                return None
            lab = b.first.get_attribute("aria-label") or ""
            v = lab.split(",", 1)[1].strip() if "," in lab else ""
            if not v or v.lower().startswith("currently"):
                return v or None
            try:
                mo, yr = monthyear(v); return f"{MONTHS[mo][:3]} {yr}"
            except Exception:
                return v
        if typ == "typeahead":
            # the visible chip or the hidden mirror input next to the search box
            hidden = {W + "employer-name-input": "input[name=employerName]", W + "job-position-name-input": "input[name=jobPositionName]",
                      O + "name-input": "input[name=organizationName]", E + "majors": "input[name=majorIds]"}.get(spec[1])
            if hidden and d.locator(hidden).count():
                return self.read(d.locator(hidden).first)
            loc = d.locator(spec[1])
            return self.read(loc.first) if loc.count() else None
        loc = d.locator(spec[1])
        if not loc.count():
            return None
        v = self.read(loc.first)
        return v if typ != "checkbox" else (v == "true")

    def probe_form(self, kind, d):
        res = {}
        for f, spec in SPECS[kind]["fields"].items():
            cur = self.current(kind, d, f)
            res[f] = {"type": spec[0], "resolved": cur is not None or d.locator(spec[1] if spec[0] != "datepick" else f'button[aria-label^="{spec[1]}"]').count() > 0,
                      "current": cur}
        return res

    def same(self, kind, f, cur, want):
        typ = SPECS[kind]["fields"][f][0]
        if typ == "checkbox":
            return bool(cur) == bool(want)
        if typ in ("monthyear", "datepick"):
            if want is None:
                return cur is None or str(cur).lower().startswith("currently")
            mo, yr = monthyear(want); return norm(cur) == f"{MONTHS[mo][:3]} {yr}"
        if typ == "typeahead" and f == "area_of_study":
            return cur not in (None, "", "[]")  # ids, not names: treat any chosen major as set
        return norm(str(cur)) == norm(str(want))

    def pick_option(self, text):
        opts = self.page.locator("[role=option]:visible")
        n = opts.count()
        texts = [norm(opts.nth(i).inner_text()) for i in range(n)]
        for i, t in enumerate(texts):
            if t.lower() == norm(text).lower():
                opts.nth(i).click(); return f"option exact {t!r}"
        for i, t in enumerate(texts):
            if norm(text).lower() in t.lower():
                opts.nth(i).click(); return f"option contains {t!r}"
        raise Stop(4, f"typeahead: no option for {text!r}; saw {texts[:8]}")

    def set_select(self, loc, value):
        try:
            loc.select_option(value=str(value), timeout=3000)
        except Exception:
            loc.evaluate("(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); }", str(value))
        if self.read(loc) != str(value):
            raise Stop(4, f"select did not take {value!r}, reads {self.read(loc)!r}")

    def set_field(self, kind, d, f, value):
        spec = SPECS[kind]["fields"][f]; typ = spec[0]
        if typ == "datepick":
            b = d.locator(f'button[aria-label^="{spec[1]}"]').first
            b.click(); self.page.wait_for_timeout(1200)
            mo, yr = monthyear(value)
            lab = self.page.locator("span[aria-live=polite]").last
            for _ in range(40):
                shown = int(norm(lab.inner_text()))
                if shown == yr:
                    break
                self.page.locator(f'button[aria-label="{"Next" if shown < yr else "Previous"} year"]').last.click()
                self.page.wait_for_timeout(300)
            else:
                raise Stop(4, f"datepick: could not reach year {yr}")
            self.page.locator(f'[role=option][aria-label="Choose {MONTHS[mo]} {yr}"]').first.click()
            self.page.wait_for_timeout(800)
            return "datepick"
        if typ == "monthyear":
            m, y = d.locator(spec[1]).first, d.locator(spec[2]).first
            if value is None:
                return "monthyear:skip-null"
            mo, yr = monthyear(value)
            self.set_select(m, mo); self.set_select(y, yr)
            return "monthyear"
        loc = d.locator(spec[1]).first
        if not loc.count():
            raise Stop(4, f"{kind}.{f}: control {spec[1]!r} absent")
        if typ == "select":
            self.set_select(loc, value); return "select"
        if typ == "checkbox":
            if (self.read(loc) == "true") != bool(value):
                loc.evaluate("e => e.click()")
            return "checkbox"
        if typ == "typeahead":
            loc.evaluate("e => { e.scrollIntoView({block: 'center'}); e.focus(); }")
            self.page.keyboard.press("Control+A"); self.page.keyboard.press("Backspace")
            self.page.keyboard.type(value, delay=random.randint(15, 40))
            self.page.wait_for_timeout(1800)
            how = self.pick_option(value); self.page.wait_for_timeout(600)
            return f"typeahead:{how}"
        # Focus through the DOM instead of a click: an open typeahead list can sit over the next field
        # and a click then waits forever for the hit target (the ASCE organization save, 2026-11-07).
        loc.evaluate("e => { e.scrollIntoView({block: 'center'}); e.focus(); }")
        loc.fill(value)
        if norm(loc.input_value()) != norm(value):
            loc.fill(""); loc.fill(value)
        if norm(loc.input_value()) != norm(value):
            raise Stop(4, f"{kind}.{f}: fill did not stick")
        return typ

    def open_form(self, kind, act):
        spec = SPECS[kind]
        rec = self.nav(spec["url"].format(**act))
        if rec["missing"]:
            raise Stop(4, f"{kind} form missing at {rec['url']}")
        opener = [(m, (t.format(**act)[:60] if m == "aria_prefix" else t.format(**act)), n) for m, t, n in spec["open"]]
        loc, how = self.resolve(self.page.locator("body"), opener)
        if loc is None:
            raise Stop(4, f"{kind}: opener {opener[0][1]!r} not found")
        loc.evaluate("e => e.scrollIntoView({block: 'center'})"); loc.click(); self.page.wait_for_timeout(2500)
        rec["opener"] = how
        return rec

    def body_text(self):
        """textContent of the profile after scrolling it to the end (lists may lazy-load)."""
        last = -1
        for _ in range(6):
            txt = self.page.evaluate("document.body.textContent")
            if len(txt) == last:
                break
            last = len(txt)
            self.page.mouse.wheel(0, 4000); self.page.wait_for_timeout(900)
        return norm(txt)

    def card_present(self, kind, act):
        """For *_new kinds: is the item already on the profile? (rerun safety). Work roles are matched by
        title, since one employer may hold two roles and the edit card is labelled by employer only."""
        spec = SPECS[kind]
        if spec.get("cover"):
            return self.page.locator(f'[aria-label="Create a page for {act["name"]}"]').count() == 0
        if "present_text" in spec:
            return norm(spec["present_text"].format(**act)) in self.body_text()
        card = spec.get("card")
        if not card:
            return False
        return self.page.locator(f'[aria-label="{card.format(**act)}"]').count() > 0

    def close_form(self):
        self.page.keyboard.press("Escape"); self.page.wait_for_timeout(500)
        d = self.dialog()
        if d is not None:
            c = d.locator('button:has-text("Cancel")')
            if c.count():
                c.first.click(); self.page.wait_for_timeout(500)

    def dry(self, i, act):
        kind = act["kind"]
        rec = {"index": i, "kind": kind, "nav": self.open_form(kind, act)}
        if SPECS[kind].get("card"):
            rec["already_present"] = self.card_present(kind, act)
        d = self.scope(kind)
        rec["fields"] = self.probe_form(kind, d)
        rec["save_buttons"] = "click-away" if SPECS[kind].get("save_click") else d.locator(f'button:has-text("{SPECS[kind].get("save", "Save")}")').count()
        rec["dialog"] = self.dialog() is not None
        stem = str(Path(self.out_path).with_suffix(""))
        Path(f"{stem}-form-{i}.txt").write_text(d.inner_text())
        Path(f"{stem}-form-{i}.html").write_text(d.evaluate("e => e.outerHTML"))
        self.page.screenshot(path=f"{stem}-form-{i}.png", full_page=False)
        rec["form_text"] = norm(d.inner_text())[:1500]
        rec["intended"] = {f: act[f] for f in SPECS[kind]["fields"] if f in act}
        if SPECS[kind].get("cover"):
            rec["file_inputs"] = self.page.locator("input[type=file]").count()
            rec["editor_text"] = norm(self.page.locator("body").inner_text())[:600]
            rec["intended"] = {"file": act.get("file")}
        if SPECS[kind].get("bulk") == "courses":
            rec["add_button"] = d.locator('[data-hook="add-course"]').count()
            rec["row0"] = {"name": d.locator("#course-name-0-input").count(), "code": d.locator("#course-code-0").count()}
            rec["intended"] = {"courses": len(act["courses"])}
        rec["would_change"] = [f for f in rec["intended"] if not self.same(kind, f, rec["fields"][f]["current"], act[f])]
        self.close_form()
        self.out["actions"].append(rec)

    def apply(self, i, act):
        kind = act["kind"]; spec = SPECS[kind]
        gap = SAVE_GAP - (time.time() - self.last_save)
        if self.last_save and gap > 0:
            time.sleep(gap)
        rec = {"index": i, "kind": kind, "set": {}}
        if spec.get("card"):
            self.nav(spec["url"])
            if self.card_present(kind, act):
                rec["skipped"] = "already present"; self.out["actions"].append(rec); return
            self.loads -= 0  # the open_form nav below is a second load; keep the count honest
        rec["nav"] = self.open_form(kind, act)
        d = self.scope(kind)
        if spec.get("remove"):
            btn = d.locator(f'button:has-text("{spec["remove"]}")')
            if not btn.count():
                raise Stop(4, f"{kind}: no {spec['remove']} button")
            btn.last.click(); self.page.wait_for_timeout(1500)
            conf = self.dialog()
            rec["confirm_text"] = norm(conf.inner_text())[:300] if conf is not None else None
            if conf is not None:
                cb = conf.locator('button:has-text("Remove"), button:has-text("Delete"), button:has-text("Yes"), button:has-text("Confirm")')
                if cb.count():
                    cb.last.click(); self.page.wait_for_timeout(2500); rec["confirmed"] = True
            self.last_save = time.time()
            self.nav(spec["url"])
            still = self.page.locator(f'[aria-label="{spec["open"][0][1].format(**act)}"]').count()
            rec["removed"] = still == 0
            self.out["actions"].append(rec)
            if still:
                raise Stop(4, f"{kind}: card still present after remove")
            return
        if spec.get("cover"):
            fi = self.page.locator("input[type=file]")
            rec["file_inputs"] = fi.count()
            if not fi.count():
                raise Stop(4, f"{kind}: no file input on the page editor")
            fi.first.set_input_files(act["file"]); self.page.wait_for_timeout(6000)
            rec["cover_imgs"] = self.page.evaluate("() => [...document.images].filter(i => i.naturalWidth >= 600).map(i => [i.naturalWidth, i.naturalHeight])")
            rec["editor_text_after_upload"] = norm(self.page.locator("body").inner_text())[:400]
            save = self.page.locator('button:has-text("Save")')
            if not save.count():
                raise Stop(4, f"{kind}: no Save on the page editor")
            save.last.click(); self.last_save = time.time(); self.page.wait_for_timeout(5000)
            rec["saved"] = True; rec["url_after_save"] = self.page.url
            rec["page_text_after_save"] = norm(self.page.locator("body").inner_text())[:300]
            self.nav(spec["url"])
            rec["create_button_gone"] = self.page.locator(f'[aria-label="Create a page for {act["name"]}"]').count() == 0
            rec["view_buttons"] = self.page.evaluate("""(n) => [...document.querySelectorAll('button,a')].map(e => e.getAttribute('aria-label') || '').filter(t => t.includes(n) && /page/i.test(t)).slice(0, 4)""", act["name"])
            self.out["actions"].append(rec)
            if not rec["create_button_gone"]:
                raise Stop(4, f"{kind}: Create page button still present after save")
            return
        if spec.get("bulk") == "courses":
            body = self.body_text().lower()
            def present(c):
                # name alone false-matches words that appear elsewhere on the profile ("Robotics", "Geology")
                return norm(c["name"]).lower() in body and (not c.get("number") or norm(c["number"]).lower() in body)
            todo = [c for c in act["courses"] if not present(c)]
            rec["skipped_present"] = len(act["courses"]) - len(todo)
            if not todo:
                self.close_form(); rec["saved"] = False; self.out["actions"].append(rec); return
            # "Add course" PREPENDS an empty row at index 0 and shifts the typed rows down (probe-6, 2026-11-07),
            # so every course is typed into row 0, and the list is walked in reverse to keep the intended order.
            # The name is a combobox that commits on blur, hence the Tab after each field.
            for i, c in enumerate(reversed(todo)):
                if i:
                    d.locator('[data-hook="add-course"]').first.click(); self.page.wait_for_timeout(600)
                n = d.locator("#course-name-0-input").first; n.click(); self.page.keyboard.type(c["name"], delay=random.randint(8, 25))
                self.page.wait_for_timeout(300); self.page.keyboard.press("Tab")
                if c.get("number"):
                    k = d.locator("#course-code-0").first; k.click(); self.page.keyboard.type(c["number"], delay=random.randint(8, 25))
                    self.page.keyboard.press("Tab")
                self.page.wait_for_timeout(200)
                rec["set"][f"row{i}"] = c["name"]
            rec["row0_check"] = {"name": self.read(d.locator("#course-name-0-input").first), "hidden": self.read(d.locator("input[name=courseName]").first)}
            save = d.locator('button:has-text("Save")')
            save.last.click(); self.last_save = time.time(); self.page.wait_for_timeout(4000)
            rec["saved"] = True; rec["dialog_still_open"] = self.dialog() is not None
            if rec["dialog_still_open"]:
                rec["dialog_text_after_save"] = norm(self.dialog().inner_text())[:500]
            missing = todo
            for _ in range(3):  # the list renders a few seconds after the save, and present() reads the lowercased body
                self.nav(spec["url"]); body = self.body_text().lower()
                missing = [c for c in todo if not present(c)]
                if not missing:
                    break
                self.page.wait_for_timeout(5000)
            missing = [c["name"] for c in missing]
            rec["readback_text_missing"] = missing
            self.out["actions"].append(rec)
            if missing:
                raise Stop(4, f"courses: profile text lacks {missing}")
            return
        before = self.probe_form(kind, d)
        rec["before"] = {f: (v["current"][:200] if isinstance(v["current"], str) else v["current"]) for f, v in before.items()}
        for f in spec["fields"]:
            if f not in act:
                continue
            if self.same(kind, f, before[f]["current"], act[f]):
                rec["set"][f] = "unchanged"; continue
            rec["set"][f] = self.set_field(kind, d, f, act[f])
        if not rec["set"] or all(v == "unchanged" for v in rec["set"].values()):
            self.close_form(); rec["saved"] = False
            self.out["actions"].append(rec); return
        if spec.get("save_click"):
            # Inline editors (Projects) have no Save button. They commit when focus leaves the editor.
            self.page.keyboard.press("Tab"); self.page.wait_for_timeout(500)
            self.page.locator(spec["save_click"]).first.click(); self.last_save = time.time()
            self.page.wait_for_timeout(3500)
            rec["save"] = "click-away"
        else:
            save = d.locator(f'button:has-text("{spec.get("save", "Save")}")')
            if save.count() == 0:
                raise Stop(4, f"{kind}: no Save button")
            save.last.click(); self.last_save = time.time()
            self.page.wait_for_timeout(3500)
        rec["saved"] = True
        rec["dialog_still_open"] = self.dialog() is not None
        if rec["dialog_still_open"]:
            rec["dialog_text_after_save"] = norm(self.dialog().inner_text())[:500]
        # Readback: reopen the item and compare every field that was set.
        ract = dict(act)
        if spec.get("readback_text"):
            self.nav(spec["url"])
            body = self.body_text()
            missing = [t for t in spec["readback_text"] if act.get(re.findall(r"{(\w+)}", t)[0]) and norm(t.format(**act))[:120] not in body]
            rec["readback_text_missing"] = missing
            self.out["actions"].append(rec)
            if missing:
                raise Stop(4, f"{kind}: profile text lacks {missing}")
            return
        if spec.get("card"):
            ract["name"] = act.get("name") or act.get("employer") or act.get("organization")
            if kind == "project" and act.get("title"):
                ract["name"] = act["title"]
            rkind = {"work_new": "work", "project_new": "project", "organization_new": "organization_new"}.get(kind, kind)
            self.nav(spec["url"])
            if not self.card_present(kind, act):
                self.out["actions"].append(rec)
                raise Stop(4, f"{kind}: item not on the profile after save")
            rec["card_present"] = True
            if rkind == "organization_new":
                self.out["actions"].append(rec); return  # no edit kind yet; presence is the readback
            self.open_form(rkind, ract)
            after = self.probe_form(rkind, self.scope(rkind))
            bad = [f for f in rec["set"] if rec["set"][f] != "unchanged" and f in SPECS[rkind]["fields"]
                   and not self.same(rkind, f, after[f]["current"], act[f])]
        else:
            self.open_form(kind, act)
            after = self.probe_form(kind, self.scope(kind))
            bad = [f for f in rec["set"] if rec["set"][f] != "unchanged" and not self.same(kind, f, after[f]["current"], act[f])]
        rec["after"] = {f: (v["current"][:200] if isinstance(v["current"], str) else v["current"]) for f, v in after.items()}
        self.close_form()
        self.out["actions"].append(rec)
        if bad:
            raise Stop(4, f"{kind}: readback mismatch on {bad}")


def run_probe(a):
    """Open forms and exercise controls without saving. Steps: fill (css, text), click (css), dump."""
    plan = json.loads(Path(a.args[0]).read_text())
    clear_stale_lock()
    out = {"mode": "probe", "plan": a.args[0], "actions": []}
    code = 0
    with sync_playwright() as p:
        ctx = launch(p)
        r = Runner(ctx.new_page(), out, a.out, a.max_loads)
        stem = str(Path(a.out).with_suffix(""))
        try:
            for i, act in enumerate(plan):
                kind = act["kind"]
                rec = {"index": i, "kind": kind, "nav": r.open_form(kind, act), "steps": []}
                for j, st in enumerate(act.get("steps", [])):
                    srec = dict(st)
                    try:
                        if "fill" in st:
                            loc = r.page.locator(st["fill"]).first
                            loc.click(); loc.fill(st["text"]); r.page.wait_for_timeout(st.get("wait", 1800))
                        elif "click" in st:
                            r.page.locator(st["click"]).first.click(); r.page.wait_for_timeout(st.get("wait", 1500))
                        elif "key" in st:
                            r.page.keyboard.press(st["key"]); r.page.wait_for_timeout(800)
                        d = r.scope()
                        srec["options"] = r.page.evaluate("""() => [...document.querySelectorAll('[role=option], [role=listbox] li, [role=menuitem]')]
                            .filter(e => e.offsetParent !== null).map(e => (e.innerText || '').trim().slice(0, 80)).slice(0, 25)""")
                        srec["new_controls"] = r.page.evaluate("""() => [...document.querySelectorAll('input,textarea,select,button,[role=grid],[role=dialog]')]
                            .filter(e => e.offsetParent !== null).map(c => ({tag: c.tagName, type: c.type || null, name: c.name || null, id: c.id || null,
                            ph: c.placeholder || null, aria: c.getAttribute('aria-label'), role: c.getAttribute('role'),
                            text: ['BUTTON'].includes(c.tagName) ? (c.innerText || '').slice(0, 40) : null, value: (c.value || '').slice(0, 60)}))""")
                        srec["dialog_text"] = norm(d.inner_text())[:1200]
                        r.page.screenshot(path=f"{stem}-{i}-{j}.png", full_page=False)
                        Path(f"{stem}-{i}-{j}.html").write_text(r.page.content())
                    except Exception as ex:
                        srec["error"] = repr(ex)[:300]
                    rec["steps"].append(srec)
                r.page.keyboard.press("Escape"); r.page.wait_for_timeout(500); r.page.keyboard.press("Escape")
                out["actions"].append(rec)
        except Stop as s:
            out["stopped"] = {"code": s.code, "why": s.why, "url": r.page.url}; code = s.code
        except Exception as ex:
            out["stopped"] = {"code": 4, "why": repr(ex)[:400]}; code = 4
        out["loads"] = r.loads
        ctx.close()
    r.finish(code)


def run_edit(a):
    if not a.args:
        sys.exit("edit needs PLAN.json")
    plan = json.loads(Path(a.args[0]).read_text())
    validate_plan(plan)
    clear_stale_lock()
    out = {"mode": "edit", "dry_run": a.dry_run, "plan": a.args[0], "actions": []}
    code = 0
    with sync_playwright() as p:
        ctx = launch(p)
        r = Runner(ctx.new_page(), out, a.out, a.max_loads if a.dry_run else None)
        try:
            for i, act in enumerate(plan):
                try:
                    (r.dry if a.dry_run else r.apply)(i, act)
                except Stop as s:
                    if s.code == 5 and a.dry_run:
                        for j, rest in enumerate(plan[i:], i):
                            out["actions"].append({"index": j, "kind": rest["kind"], "probed": False, "note": "not probed: load budget"})
                        break
                    raise
        except Stop as s:
            out["stopped"] = {"code": s.code, "why": s.why, "url": r.page.url}; code = s.code
        except Exception as ex:
            out["stopped"] = {"code": 4, "why": repr(ex)[:400]}; code = 4
        if code:
            try:
                shot = str(Path(a.out).with_suffix("")) + "-fail.png"
                r.page.screenshot(path=shot, full_page=False)
                out["stopped"]["screenshot"] = shot
                out["stopped"]["visible_text"] = r.page.evaluate("document.body.innerText").strip()[:1500]
            except Exception as ex:
                out["stopped"]["screenshot_error"] = repr(ex)[:200]
        out["loads"] = r.loads
        ctx.close()
    r.finish(code)


# ---------------------------------------------------------------- read modes
def run_login(a):
    """Headed window. Phi completes the university SSO and Duo himself. The script only waits for the app to load."""
    clear_stale_lock()
    with sync_playwright() as p:
        ctx = launch(p, headed=True)
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        deadline = time.time() + a.wait
        state = "logged_out"
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            state = session_state(page.url, page.title())
            if state == "ok" and "joinhandshake.com" in page.url:
                break
        out = {"mode": "login", "state": state, "url": page.url, "title": page.title()}
        if state == "ok":
            page.wait_for_timeout(3000)  # let the session cookies settle before the profile is closed
        ctx.close()
    Path(a.out).write_text(json.dumps(out, indent=1))
    json.dump(out, sys.stdout, indent=1); print()
    sys.exit(0 if state == "ok" else 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["login", "check", "dump", "edit", "probe"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--out", default="handshake-agent-browser-out.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-loads", type=int, default=8)
    ap.add_argument("--max-chars", type=int, default=20000)
    ap.add_argument("--wait", type=int, default=600, help="login: seconds to wait for Phi to finish SSO")
    a = ap.parse_args()

    if a.mode == "login":
        run_login(a)
    if a.mode == "edit":
        run_edit(a)
    if a.mode == "probe":
        run_probe(a)
    clear_stale_lock()
    out = {}
    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.new_page()
        out["login"] = logged_in(page)
        ok = out["login"]["state"] == "ok" and out["login"]["status"] == 200
        out["logged_in"] = ok
        if not ok:
            print("NOT LOGGED IN: run `login` from a desktop terminal and sign in once", file=sys.stderr)
        if ok and a.mode == "dump":
            stem = str(Path(a.out).with_suffix(""))
            for n, u in enumerate(a.args or [PROFILE_URL]):
                r = page.goto(u, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                text = page.evaluate("document.body.innerText").strip()
                Path(f"{stem}-{n}.txt").write_text(text)
                Path(f"{stem}-{n}.html").write_text(page.content())
                page.screenshot(path=f"{stem}-{n}.png", full_page=True)
                out[u] = {"status": r.status if r else None, "url": page.url, "title": page.title(),
                          "state": session_state(page.url, page.title()), "chars": len(text),
                          "text": text[: a.max_chars], "files": [f"{stem}-{n}.{e}" for e in ("txt", "html", "png")]}
                if n < len(a.args or [0]) - 1:
                    time.sleep(PAUSE)
        ctx.close()
    Path(a.out).write_text(json.dumps(out, indent=1))
    brief = {k: ({kk: vv for kk, vv in v.items() if kk != "text"} if isinstance(v, dict) else v) for k, v in out.items()}
    json.dump(brief, sys.stdout, indent=1)
    print()
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
