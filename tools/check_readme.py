"""W1.6 + H1: README truthfulness check (part of tools/gate.sh).

1. every key in config.json / config.local.json.example appears in the
   README's configuration table
2. known-stale marketing claims are gone from the README
3. every screenshot the README embeds exists on disk
4. the test counts the README states match the counts the gate just ran
   (gate.sh passes them: --tests N --js M)

Exit 1 (gate fails) on any violation.
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
readme = (BASE / "README.md").read_text(encoding="utf-8")

ap = argparse.ArgumentParser()
ap.add_argument("--tests", type=int, help="Python tests the gate just ran")
ap.add_argument("--js", type=int, help="jsdom tests the gate just ran")
ap.add_argument("--no-routes", action="store_true", help="skip the endpoint check")
args = ap.parse_args()

keys = set()
for name in ("config.json", "config.local.json.example"):
    keys |= set(json.loads((BASE / name).read_text(encoding="utf-8")))

missing = sorted(k for k in keys if f"`{k}`" not in readme)
stale = [s for s in ("2,800-row", "37 jobs", "clinical-data-automation") if s in readme]

# H1: the screenshots were the only claim in the docs nothing checked.
DOCS = {n: (BASE / n).read_text(encoding="utf-8") for n in ("README.md", "PROBLEMS.md")}
IMG_RE = r"!\[[^\]]*\]\(([^)]+)\)"
images = sum(len(re.findall(IMG_RE, t)) for t in DOCS.values())
broken = [f"{n}: {p}" for n, t in DOCS.items() for p in re.findall(IMG_RE, t)
          if not (BASE / p).resolve().exists()]

# H1: a test count in prose goes stale on the next commit. The gate knows the
# real numbers, so it makes the README say them.
wrong_counts = []
for name, text in DOCS.items():
    if args.tests is not None and f"{args.tests} Python tests" not in text:
        wrong_counts.append(f"{name} must state '{args.tests} Python tests'")
    if args.js is not None and f"{args.js} jsdom" not in text:
        wrong_counts.append(f"{name} must state '{args.js} jsdom' tests")

# P3: the profile the seed actually creates must be the one the README names.
seed_src = (BASE / "resumes" / "schema.py").read_text(encoding="utf-8")
seeded = set(re.findall(r'"default":\s*"([^"]+)"', seed_src))
missing_profiles = sorted(n for n in seeded if f"`{n}`" not in readme)

# H1: the endpoint table must be the routes the app actually serves. Docs drift
# is how this repo got "buggy": the code moved, the docs kept describing it.
sys.path.insert(0, str(BASE))
missing_routes = []
if not args.no_routes:
    from app.server import app
    arch = (BASE / "docs" / "architecture.md").read_text(encoding="utf-8")
    for r in app.routes:
        for method in sorted(set(getattr(r, "methods", set())) - {"HEAD", "OPTIONS"}):
            if r.path in {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}:
                continue
            if f"`{method} {r.path}`" not in arch:
                missing_routes.append(f"{method} {r.path}")

if missing or stale or broken or wrong_counts or missing_profiles or missing_routes:
    print("check_readme: FAIL", file=sys.stderr)
    for k in missing:
        print(f"  config key not documented in README: {k}", file=sys.stderr)
    for s in stale:
        print(f"  stale claim still in README: {s!r}", file=sys.stderr)
    for p in broken:
        print(f"  README embeds a missing image: {p}", file=sys.stderr)
    for c in wrong_counts:
        print(f"  {c}", file=sys.stderr)
    for n in missing_profiles:
        print(f"  seeded resume profile '{n}' is not documented in README", file=sys.stderr)
    for r in missing_routes:
        print(f"  route not in docs/architecture.md endpoint table: {r}", file=sys.stderr)
    sys.exit(1)

print(f"check_readme: OK ({len(keys)} config keys, {images} screenshots, "
      f"{'counts match' if args.tests is not None else 'counts not supplied'}, "
      f"{'endpoints match' if not args.no_routes else 'endpoints skipped'})")
