#!/bin/sh
# Render the committed Markdown manual as an offline, print-ready HTML edition.
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v pandoc >/dev/null 2>&1; then
  echo "pandoc is required to regenerate docs/USER_MANUAL.html" >&2
  exit 1
fi
manual_sha=$(python3 - "$repo_root/docs/USER_MANUAL.md" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
pandoc "$repo_root/docs/USER_MANUAL.md" \
  --from=gfm --to=html5 --standalone --toc --toc-depth=2 \
  --variable="source-sha256:$manual_sha" \
  --template="$repo_root/docs/user-manual-template.html" \
  --output="$repo_root/docs/USER_MANUAL.html"
