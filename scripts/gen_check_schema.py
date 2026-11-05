#!/usr/bin/env python3
"""Write site/public/schema/check.json: the check file's JSON Schema (mobile_agent/verify/check_schema.py).

mobster.dev serves it at /schema/check.json, the URL a saved check names on its first line
(`# yaml-language-server: $schema=...`), so an editor completes and checks .mobster/checks/*.yaml as you type.

    python scripts/gen_check_schema.py           write the file
    python scripts/gen_check_schema.py --check   exit 1 if the committed file differs from the code

mobile_agent/tests/test_check_schema.py fails when the committed file is stale.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "site" / "public" / "schema" / "check.json"


def build() -> str:
    from mobile_agent.verify.check_schema import schema
    return json.dumps(schema(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed file differs from the code")
    args = parser.parse_args()
    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/gen_check_schema.py", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"Wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
