"""W1.6: README truthfulness check (part of tools/gate.sh).

1. every key in config.json / config.local.json.example appears in the
   README's configuration table
2. known-stale marketing numbers are gone from the README

Exit 1 (gate fails) on any violation.
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
readme = (BASE / "README.md").read_text(encoding="utf-8")

keys = set()
for name in ("config.json", "config.local.json.example"):
    keys |= set(json.loads((BASE / name).read_text(encoding="utf-8")))

missing = sorted(k for k in keys if f"`{k}`" not in readme)
stale = [s for s in ("2,800-row", "37 jobs") if s in readme]

if missing or stale:
    print("check_readme: FAIL", file=sys.stderr)
    for k in missing:
        print(f"  config key not documented in README: {k}", file=sys.stderr)
    for s in stale:
        print(f"  stale claim still in README: {s!r}", file=sys.stderr)
    sys.exit(1)

print(f"check_readme: OK ({len(keys)} config keys documented)")
