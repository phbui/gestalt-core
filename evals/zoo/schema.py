"""Validate a result JSON against result.v1.json.

    python -m evals.zoo.schema FILE...

Exit 0 when every file is valid, 1 when any is not. Each error prints with the path inside the file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).with_name("result.v1.json")


def validate(path: str | Path) -> list[str]:
    """Error messages for the file at `path`, empty when it is valid. Beyond the schema, per_query and query_ids must be the same length."""
    result = json.loads(Path(path).read_text())
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(result)]
    if not errors:
        n = len(result["query_ids"])
        errors = [f"systems/{s}/per_query: {len(v['per_query'])} scores for {n} query ids"
                  for s, v in result["systems"].items() if len(v["per_query"]) != n]
    return errors


def main(argv: list[str] | None = None) -> int:
    files = sys.argv[1:] if argv is None else argv
    if not files:
        print(__doc__, file=sys.stderr)
        return 1
    bad = 0
    for f in files:
        try:
            errors = validate(f)
        except (OSError, ValueError) as e:
            errors = [f"cannot read: {e}"]
        print(f"{f}: " + ("ok" if not errors else f"{len(errors)} error(s)"))
        for e in errors:
            print(f"  {e}")
        bad += bool(errors)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
