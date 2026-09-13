#!/bin/sh
# One-command check gate: ruff + full pytest + personal-path check +
# README truthfulness (W1.6). Refused by the pre-push hook (.githooks).
set -e
cd "$(dirname "$0")/.."

ruff check .
python -m pytest tests/ -q

# W2.4: no personal machine paths in tracked source (git grep = tracked only,
# so config.local.json stays personal; the .json.example placeholder is exempt
# by the '*.py' pathspec).
if git grep -nF -e 'Dropbox' -e 'C:\Users\' -- '*.py'; then
  echo "gate: FAIL - personal machine path in tracked .py source" >&2
  exit 1
fi

# W5.1: no route module over 250 lines (split when one grows past it).
for f in app/routers/*.py; do
  n=$(wc -l < "$f")
  if [ "$n" -gt 250 ]; then
    echo "gate: FAIL - $f is $n lines (max 250)" >&2
    exit 1
  fi
done

# W1.6: README truthfulness (config keys documented; no stale counts)
# W3.1: live drift check (exit 0 with warn when offline; exit 1 on markup drift)
# W6.1: every frontend module must parse as an ES module
if command -v node >/dev/null; then
  for f in static/app.js static/js/*.js; do
    cp "$f" tools/_gate.mjs 2>/dev/null || true
    # cp to tools/ is fine (local disk); /tmp is not
    if ! node --check tools/_gate.mjs; then echo "JS syntax check failed: $f"; exit 1; fi
  done
  rm -f tools/_gate.mjs
else
  echo "node not found — skipping JS syntax check"
fi

python tools/check_fixtures.py
python tools/check_readme.py

echo "gate: PASS"
