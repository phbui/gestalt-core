#!/usr/bin/env python3
"""
Gestalt skill eval runner.

Two tiers:
  - deterministic (60%): `validate` and `run` below — pure Python, no deps
    beyond pyyaml, no network. This is the CI hard gate.
  - LLM-as-judge (30%): `export` converts a config to a promptfoo YAML file
    (evals/promptfoo/<skill>.yaml) that pairs each config's assertions with
    an llm-rubric assertion, run against pre-captured skill outputs via
    evals/provider.py. See README.md for the full pipeline and the capture
    workflow.

Naming (X13, 2026-10-06): the trigger score is a keyword-overlap lint on the skill description. It predicts
nothing about how a model routes a prompt, so it is called a trigger lint here and in baseline.json
(`trigger_lint_*`). The output assertions run only on captured runs: `captures` reads personal/eval-captures/ when it is unlocked (evals/README.md).

Also in the deterministic tier: `triggers` scores each config's
trigger_cases (should_trigger prompts) for routing accuracy against the
skill's own frontmatter description, and `baseline` save/checks those scores
(plus deterministic-assertion results where a captured transcript exists) so
CI can gate on regression. See README.md §Trigger-routing tier and
§Baseline discipline.
"""
import json
import sys
import re
import yaml
from datetime import datetime, timezone
from pathlib import Path

CONFIGS_DIR = Path(__file__).parent / "configs"
PROMPTFOO_DIR = Path(__file__).parent / "promptfoo"
CAPTURED_DIR = Path(__file__).parent / "captured"
# X13 (2026-10-06): real runs captured by tools/capture-skill-run.py. The directory is git-crypt encrypted, so CI
# (no key) sees ciphertext or nothing and the `captures` mode skips. Baseline never reads it, so CI stays reproducible.
PRIVATE_CAPTURES_DIR = Path(__file__).parent.parent / "personal" / "eval-captures"
BASELINE_PATH = Path(__file__).parent / "baseline.json"
SKILLS_DIR = Path(__file__).parent.parent / "claude-tree" / "skills"  # F10: real repo tree, not the .claude compat symlink

# custom config assertion type -> promptfoo assertion type
# (promptfoo's negation convention is a "not-" prefix on the positive type)
ASSERTION_TYPE_MAP = {
    "contains_section": "contains",
    "regex": "regex",
    "not_contains": "not-contains",
}

# Words too common to count as a routing signal for the trigger-accuracy
# heuristic below. Deliberately short/blunt — this is a tier-1 free check,
# not an NLP pipeline.
STOPWORDS = {
    "this", "that", "with", "from", "into", "your", "have", "were", "will",
    "what", "when", "where", "which", "while", "does", "about", "these",
    "their", "such", "than", "then", "them", "they", "some", "each", "over",
    "more", "most", "very", "just", "like", "make", "made", "used", "using",
    "should", "would", "could", "shall", "must", "need", "want", "help",
    "talk", "show", "tell", "give", "look", "find", "know", "think",
    "session", "skill", "please", "before", "after", "during", "across",
    "through", "being", "doing", "going", "right", "really", "actually",
}


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_skill_description(skill: str) -> str | None:
    """Read the `description` field from <skill>/SKILL.md's YAML frontmatter.

    Returns None if the skill has no SKILL.md or no frontmatter — the
    trigger scorer treats that as "cannot route here" rather than guessing.
    """
    skill_md = SKILLS_DIR / skill / "SKILL.md"
    if not skill_md.exists():
        return None
    text = skill_md.read_text()
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    frontmatter = yaml.safe_load(m.group(1)) or {}
    return frontmatter.get("description") or ""


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {w for w in words if len(w) > 3 and w not in STOPWORDS}


def _stem_match(a: str, b: str) -> bool:
    """Cheap stemming substitute: exact match, or shared 5-char prefix.

    Catches investigate/investigation, compact/compaction, etc. without a
    stemming library. Deliberately blunt — see STOPWORDS comment.
    """
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5:
        return False
    return a[:5] == b[:5]


def predict_trigger(prompt: str, description: str) -> tuple[bool, int, int]:
    """Deterministic tier-1 routing predictor: does `prompt` route to the
    skill described by `description`?

    Keyword-overlap heuristic, no API calls: any shared keyword (exact or
    5-char-prefix stem match) between the prompt and the skill's frontmatter
    description predicts a trigger. Measured at ~80% aggregate accuracy
    against the 33 existing configs' trigger_cases (105 cases) — good enough
    to catch gross routing drift, not a substitute for the 30% LLM-judge or
    10% human tiers on ambiguous cases.

    Returns (predicted, overlap_count, prompt_keyword_count).
    """
    prompt_kw = _keywords(prompt)
    desc_kw = _keywords(description)
    if not prompt_kw or not desc_kw:
        return False, 0, len(prompt_kw)
    overlap = sum(1 for pw in prompt_kw if any(_stem_match(pw, dw) for dw in desc_kw))
    return overlap >= 1, overlap, len(prompt_kw)


def compute_trigger_results(configs: list[Path]) -> dict:
    """Score trigger_cases for each config against its skill's own
    description. Shared by the `triggers` command and `baseline save/check`.
    """
    results = {}
    for c in configs:
        cfg = load_config(c)
        skill = cfg.get("skill", c.stem)
        cases = cfg.get("trigger_cases", [])
        description = load_skill_description(skill)

        case_results = []
        correct = 0
        for case in cases:
            prompt = case["prompt"]
            expected = case.get("should_trigger", True)
            if description is None:
                predicted, overlap = False, 0
            else:
                predicted, overlap, _ = predict_trigger(prompt, description)
            ok = predicted == expected
            correct += int(ok)
            case_results.append({
                "prompt": prompt,
                "expected": expected,
                "predicted": predicted,
                "ok": ok,
                "overlap": overlap,
                "routes_to": case.get("routes_to"),
            })

        total = len(cases)
        results[skill] = {
            "total": total,
            "correct": correct,
            "accuracy": (correct / total) if total else None,
            "has_description": description is not None,
            "cases": case_results,
        }
    return results


def run_deterministic_assertions(config: dict, output_text: str) -> list[dict]:
    """Run schema/format assertions against a skill output string (60% tier)."""
    results = []
    for assertion in config.get("output_assertions", []):
        atype = assertion["type"]
        value = assertion["value"]
        desc = assertion.get("description", value)

        if atype == "contains_section":
            passed = value in output_text
        elif atype == "regex":
            passed = bool(re.search(value, output_text))
        elif atype == "not_contains":
            passed = value not in output_text
        else:
            results.append({"type": atype, "desc": desc, "status": "SKIP", "reason": f"unknown type {atype!r}"})
            continue

        results.append({"type": atype, "desc": desc, "status": "PASS" if passed else "FAIL"})
    return results


def list_configs() -> None:
    configs = sorted(CONFIGS_DIR.glob("*.yaml"))
    if not configs:
        print("No eval configs found in evals/configs/")
        return
    print(f"Found {len(configs)} eval config(s):")
    for c in configs:
        cfg = load_config(c)
        trigger_cases = cfg.get("trigger_cases", [])
        assertions = cfg.get("output_assertions", [])
        print(f"  {c.name}: {len(trigger_cases)} trigger cases, {len(assertions)} output assertions")


def validate_config(path: Path) -> None:
    """Dry-run: confirm config is valid YAML with required fields."""
    cfg = load_config(path)
    skill = cfg.get("skill", "<unknown>")
    trigger_cases = cfg.get("trigger_cases", [])
    assertions = cfg.get("output_assertions", [])
    print(f"\nConfig: {path.name}")
    print(f"  Skill: {skill}")
    print(f"  Trigger cases: {len(trigger_cases)}")
    for case in trigger_cases:
        mark = "✓" if case.get("should_trigger") else "✗"
        print(f"    [{mark}] {case['prompt'][:80]}")
    print(f"  Output assertions: {len(assertions)}")
    for a in assertions:
        print(f"    - [{a['type']}] {a.get('description', a['value'])[:60]}")
    print("  Status: config valid ✓")


def run_skill(skill: str, output_file: str) -> None:
    """Run deterministic assertions from configs/<skill>.yaml against a captured output file.

    Prints a PASS/FAIL table and exits 1 if any assertion fails (the CI hard gate).
    """
    cfg_path = CONFIGS_DIR / f"{skill}.yaml"
    if not cfg_path.exists():
        print(f"ERROR: {cfg_path} not found")
        sys.exit(1)

    out_path = Path(output_file)
    if not out_path.exists():
        print(f"ERROR: output file {out_path} not found")
        sys.exit(1)

    cfg = load_config(cfg_path)
    output_text = out_path.read_text()
    results = run_deterministic_assertions(cfg, output_text)

    print(f"\nRunning deterministic assertions for /{skill} against {out_path}")
    if not results:
        print("  (no output_assertions defined in config)")
        return

    fail_count = 0
    skip_count = 0
    marker = {"PASS": "✓", "FAIL": "✗", "SKIP": "?"}
    for r in results:
        if r["status"] == "FAIL":
            fail_count += 1
        elif r["status"] == "SKIP":
            skip_count += 1
        print(f"  [{marker[r['status']]}] {r['status']:<4} [{r['type']:<15}] {r['desc'][:70]}")

    total = len(results)
    passed = total - fail_count - skip_count
    summary = f"\n{passed}/{total} passed"
    if skip_count:
        summary += f", {skip_count} skipped"
    if fail_count:
        summary += f", {fail_count} FAILED"
    print(summary)

    if fail_count:
        sys.exit(1)


def cmd_triggers(skill: str | None, use_llm: bool) -> None:
    """Score trigger_cases routing accuracy for one config (or all).

    Deterministic keyword/description matching against the skill's own
    frontmatter description — no API calls, no captured transcript needed.
    Prints per-case and per-skill PASS/FAIL plus an aggregate accuracy.
    Informational only (does not exit 1) — `baseline check` is the gate.
    """
    if use_llm:
        print(
            "NOTE: --llm requested, but evals/provider.py has no live LLM-invocation path — "
            "it replays pre-captured evals/captured/<skill>.{txt,md} transcripts only "
            "(see provider.py call_api docstring), it does not classify arbitrary prompts. "
            "Falling back to the deterministic keyword-overlap tier.\n"
        )

    if skill:
        configs = [CONFIGS_DIR / f"{skill}.yaml"]
        for c in configs:
            if not c.exists():
                print(f"ERROR: {c} not found")
                sys.exit(1)
    else:
        configs = sorted(CONFIGS_DIR.glob("*.yaml"))

    results = compute_trigger_results(configs)

    print("Trigger lint (keyword overlap between a prompt and the skill description; NOT a model routing test, tier 1)")
    overall_correct = 0
    overall_total = 0
    miss_skills = []
    for skill_name, r in results.items():
        if r["total"] == 0:
            print(f"\n  {skill_name}: no trigger_cases defined")
            continue
        note = "" if r["has_description"] else "  (no SKILL.md found -- all predicted no-trigger)"
        print(f"\n  {skill_name}: {r['correct']}/{r['total']} correct{note}")
        for case in r["cases"]:
            mark = "✓" if case["ok"] else "✗"
            exp = "trigger" if case["expected"] else "no-trigger"
            pred = "trigger" if case["predicted"] else "no-trigger"
            routes = f" -> /{case['routes_to']}" if case["routes_to"] else ""
            print(f"    [{mark}] expected={exp:<10} predicted={pred:<10} (overlap={case['overlap']}) {case['prompt'][:55]}{routes}")
        overall_correct += r["correct"]
        overall_total += r["total"]
        if r["correct"] < r["total"]:
            miss_skills.append(skill_name)

    print()
    if overall_total:
        pct = 100 * overall_correct / overall_total
        print(f"Aggregate: {overall_correct}/{overall_total} trigger cases correct ({pct:.1f}%)")
    else:
        print("No trigger cases found.")
    if miss_skills:
        print(f"Skills with routing misses: {', '.join(miss_skills)}")


def _find_captured(skill: str) -> Path | None:
    for ext in (".txt", ".md"):
        candidate = CAPTURED_DIR / f"{skill}{ext}"
        if candidate.exists():
            return candidate
    return None


def _skill_deterministic_entry(skill: str) -> dict:
    """Deterministic-tier baseline entry for one skill.

    If a captured transcript exists (evals/captured/<skill>.{txt,md}), runs
    the real output_assertions and records pass/fail counts. Otherwise (the
    default state today -- evals/captured/ has only .gitkeep) records
    "not_captured" plus the declared assertion count, so baseline still
    tracks assertion-coverage regressions (someone deleting checks) even
    before any transcript exists. Once a transcript is captured, re-running
    `baseline save` upgrades that skill's entry to real pass/fail counts.
    """
    cfg = load_config(CONFIGS_DIR / f"{skill}.yaml")
    assertions_total = len(cfg.get("output_assertions", []))
    captured = _find_captured(skill)
    if not captured:
        return {"status": "not_captured", "assertions_total": assertions_total}
    det_results = run_deterministic_assertions(cfg, captured.read_text())
    passed = sum(1 for x in det_results if x["status"] == "PASS")
    failed = sum(1 for x in det_results if x["status"] == "FAIL")
    return {
        "status": "captured",
        "assertions_total": assertions_total,
        "passed": passed,
        "failed": failed,
    }


def private_captures(skill: str) -> list[Path]:
    """Readable plaintext captures for one skill under personal/eval-captures/<skill>/, oldest first.

    Returns [] when the directory is missing (fresh clone) or the files are still git-crypt ciphertext (locked clone).
    """
    d = PRIVATE_CAPTURES_DIR / skill
    out = []
    for f in sorted(d.glob("*.md")) + sorted(d.glob("*.txt")) if d.is_dir() else []:
        try:
            head = f.read_bytes()[:10]
            if head.startswith(b"\x00GITCRYPT"):
                continue
            f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        out.append(f)
    return out


def cmd_captures(skill: str | None, strict: bool = False) -> None:
    """Run each skill's output_assertions on every readable private capture and print pass/fail per skill.

    A failing assertion on a real run is a finding about the assertion or the skill, so it is listed, not hidden.
    Exits 0 unless --strict is given and something failed. A missing or locked directory is a skip, never an error.
    """
    configs = [CONFIGS_DIR / f"{skill}.yaml"] if skill else sorted(CONFIGS_DIR.glob("*.yaml"))
    for c in configs:
        if not c.exists():
            print(f"ERROR: {c} not found")
            sys.exit(1)
    any_capture = False
    any_fail = False
    tot_pass = tot_fail = 0
    for c in configs:
        cfg = load_config(c)
        name = cfg.get("skill", c.stem)
        files = private_captures(name)
        if not files:
            continue
        any_capture = True
        sk_pass = sk_fail = 0
        detail = []
        for f in files:
            res = run_deterministic_assertions(cfg, f.read_text())
            p = sum(1 for r in res if r["status"] == "PASS")
            fl = [r for r in res if r["status"] == "FAIL"]
            sk_pass += p
            sk_fail += len(fl)
            detail.append((f, p, fl))
        print(f"\n  {name}: {sk_pass} passed, {sk_fail} failed over {len(files)} capture(s)")
        for f, p, fl in detail:
            print(f"    {f.name}: {p} passed, {len(fl)} failed")
            for r in fl:
                print(f"      [FAIL] [{r['type']}] {r['desc'][:90]}")
        tot_pass += sk_pass
        tot_fail += sk_fail
        any_fail = any_fail or sk_fail > 0
    if not any_capture:
        print("No readable captures (personal/eval-captures/ is missing or still encrypted). Output assertions not run.")
        return
    print(f"\nTotal on captured runs: {tot_pass} passed, {tot_fail} failed")
    if strict and any_fail:
        sys.exit(1)


def baseline_save() -> None:
    """Write evals/baseline.json: per-skill trigger accuracy + deterministic-tier results."""
    configs = sorted(CONFIGS_DIR.glob("*.yaml"))
    trigger_results = compute_trigger_results(configs)

    skills_out = {}
    for skill_name, r in trigger_results.items():
        skills_out[skill_name] = {
            "trigger_lint_total": r["total"],
            "trigger_lint_correct": r["correct"],
            "trigger_lint_accuracy": r["accuracy"],
            "deterministic": _skill_deterministic_entry(skill_name),
        }

    baseline = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills": skills_out,
    }
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    print(f"Wrote baseline for {len(skills_out)} skill(s) to {BASELINE_PATH}")
    captured_count = sum(1 for s in skills_out.values() if s["deterministic"]["status"] == "captured")
    print(f"  {captured_count}/{len(skills_out)} skill(s) had a captured transcript for the deterministic tier")


def baseline_check() -> None:
    """Exit non-zero if any skill's trigger accuracy or deterministic-tier
    failures regress below/above the stored evals/baseline.json values.
    New skills with no baseline entry pass with a note (nothing to regress
    against yet -- run `baseline save` to add them).
    """
    if not BASELINE_PATH.exists():
        print(f"ERROR: {BASELINE_PATH} not found -- run `python3 run_evals.py baseline save` first")
        sys.exit(1)

    stored = json.loads(BASELINE_PATH.read_text())
    stored_skills = stored.get("skills", {})

    configs = sorted(CONFIGS_DIR.glob("*.yaml"))
    trigger_results = compute_trigger_results(configs)

    print("Trigger-lint regression check (against evals/baseline.json)\n")
    regressions = []
    for skill_name, r in trigger_results.items():
        base = stored_skills.get(skill_name)
        if base is None:
            print(f"  [new]  {skill_name}: no baseline entry yet -- pass")
            continue

        base_acc = base.get("trigger_lint_accuracy", base.get("trigger_accuracy"))  # old key read for a baseline saved before 2026-10-06
        cur_acc = r["accuracy"]
        if base_acc is not None and cur_acc is not None and cur_acc < base_acc:
            msg = f"  [FAIL] {skill_name}: trigger-lint accuracy {cur_acc:.2f} < baseline {base_acc:.2f}"
            print(msg)
            regressions.append(msg)
            continue

        base_det = base.get("deterministic", {})
        det_note = ""
        if base_det.get("status") == "captured":
            cur_det = _skill_deterministic_entry(skill_name)
            if cur_det["status"] == "captured" and cur_det["failed"] > base_det.get("failed", 0):
                msg = (
                    f"  [FAIL] {skill_name}: deterministic-tier failures {cur_det['failed']} "
                    f"> baseline {base_det.get('failed', 0)}"
                )
                print(msg)
                regressions.append(msg)
                continue
            det_note = "  (deterministic tier re-checked)"
        else:
            # Nothing was captured, so no output_assertion ran. Saying "no regression"
            # here claims a check that did not happen; name it instead.
            det_note = "  (deterministic tier NOT ENFORCED — no captured baseline)"

        print(f"  [ok]   {skill_name}: no regression{det_note}")

    print()
    if regressions:
        print(f"{len(regressions)} skill(s) regressed against evals/baseline.json:")
        for r_ in regressions:
            print(r_)
        sys.exit(1)
    print(f"No regressions across {len(trigger_results)} skill(s).")


def _to_promptfoo_assertion(assertion: dict) -> tuple[str, str, str | None]:
    atype = str(assertion["type"])
    pf_type = ASSERTION_TYPE_MAP.get(atype, atype)
    return pf_type, str(assertion["value"]), assertion.get("description")


def export_promptfoo(skill: str) -> Path:
    """Export configs/<skill>.yaml to a promptfoo config at promptfoo/<skill>.yaml.

    One test entry per skill: vars.skill_case_id names the captured-output file
    for evals/provider.py to load, and the assert list transliterates every
    output_assertion plus one appended llm-rubric assertion synthesized from
    the assertion descriptions (the skill's "expected output shape").

    The llm-rubric assertion intentionally omits a `provider` field — the
    grader model is controlled globally via `defaultTest.options.provider` in
    a wrapping promptfoo config, or per-invocation via the promptfoo CLI, not
    hardcoded per skill.
    """
    src = CONFIGS_DIR / f"{skill}.yaml"
    if not src.exists():
        print(f"ERROR: {src} not found")
        sys.exit(1)

    cfg = load_config(src)
    assertions = cfg.get("output_assertions", [])

    PROMPTFOO_DIR.mkdir(exist_ok=True)
    out_path = PROMPTFOO_DIR / f"{skill}.yaml"

    lines = [
        f"# Exported from evals/configs/{skill}.yaml by `python3 run_evals.py export {skill}`.",
        "# Do not hand-edit -- re-run the export instead. See evals/README.md for the LLM-judge tier.",
        "providers:",
        '  - id: "file://../provider.py"  # pre-captured-output provider, see evals/provider.py',
        "prompts:",
        '  - "{{prompt}}"  # passthrough -- provider.py ignores the rendered prompt text and reads',
        "                   # evals/captured/<skill_case_id>.txt instead",
        "tests:",
        "  - vars:",
        f"      skill_case_id: {json.dumps(skill)}",
        "    assert:",
    ]

    descriptions = []
    for assertion in assertions:
        pf_type, value, desc = _to_promptfoo_assertion(assertion)
        if desc:
            lines.append(f"      # {desc}")
            descriptions.append(desc)
        else:
            descriptions.append(value)
        lines.append(f"      - type: {pf_type}")
        lines.append(f"        value: {json.dumps(value)}")

    rubric_text = (
        f"Output follows the /{skill} skill's mandated structure: " + "; ".join(descriptions)
        if descriptions
        else f"Output follows the /{skill} skill's mandated structure."
    )
    lines.append("      # LLM-judge tier (30%) -- grader provider intentionally unset here; set it via")
    lines.append("      # defaultTest.options.provider in a wrapping config, or --grader on the CLI.")
    lines.append("      - type: llm-rubric")
    lines.append(f"        value: {json.dumps(rubric_text)}")
    lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"Exported {out_path}")
    return out_path


def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] == "list":
        list_configs()
        return

    if args[0] == "validate":
        target = args[1] if len(args) > 1 else None
        if target:
            configs = [CONFIGS_DIR / target if target.endswith(".yaml") else CONFIGS_DIR / f"{target}.yaml"]
        else:
            configs = sorted(CONFIGS_DIR.glob("*.yaml"))
        for c in configs:
            if not c.exists():
                print(f"ERROR: {c} not found")
                sys.exit(1)
            validate_config(c)
        print("\nAll configs valid.")
        return

    if args[0] == "run":
        if len(args) < 3:
            print("Usage: python run_evals.py run <skill> <output_file>")
            sys.exit(1)
        run_skill(args[1], args[2])
        return

    if args[0] == "export":
        target = args[1] if len(args) > 1 else None
        if target:
            export_promptfoo(target)
        else:
            for c in sorted(CONFIGS_DIR.glob("*.yaml")):
                export_promptfoo(c.stem)
        return

    if args[0] == "triggers":
        rest = args[1:]
        use_llm = "--llm" in rest
        rest = [a for a in rest if a != "--llm"]
        target = rest[0] if rest else None
        cmd_triggers(target, use_llm)
        return

    if args[0] == "captures":
        rest = [a for a in args[1:] if a != "--strict"]
        cmd_captures(rest[0] if rest else None, "--strict" in args)
        return

    if args[0] == "baseline":
        sub = args[1] if len(args) > 1 else None
        if sub == "save":
            baseline_save()
        elif sub == "check":
            baseline_check()
        else:
            print("Usage: python run_evals.py baseline save|check")
            sys.exit(1)
        return

    print("Usage:")
    print("  python run_evals.py list                    — list available configs")
    print("  python run_evals.py validate [skill]         — validate config(s) (60% tier, CI hard gate)")
    print("  python run_evals.py run <skill> <output.txt> — run deterministic assertions against a captured output")
    print("  python run_evals.py export [skill]           — export config(s) to promptfoo/<skill>.yaml (30% LLM-judge tier)")
    print("  python run_evals.py triggers [skill] [--llm]  — score trigger_cases routing accuracy (tier 1, deterministic by default)")
    print("  python run_evals.py captures [skill] [--strict] — output assertions on private captured runs (personal/eval-captures/), skipped when locked")
    print("  python run_evals.py baseline save             — write evals/baseline.json from current trigger + deterministic results")
    print("  python run_evals.py baseline check             — exit 1 if any skill regressed below its stored baseline")
    print("  (see README.md for the full 60/30/10 pipeline and the capture workflow)")


if __name__ == "__main__":
    main()
