#!/bin/sh
# One-command check gate (W1.1/W2.4). W1.3 extends this with smoke_e2e.sh and
# check_fixtures.sh and installs the pre-push hook that calls it.
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

echo "gate: PASS"
