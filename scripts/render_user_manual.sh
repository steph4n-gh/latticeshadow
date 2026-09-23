#!/bin/sh
# Render the committed Markdown manual as an offline, print-ready HTML edition.
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v pandoc >/dev/null 2>&1; then
  echo "pandoc is required to regenerate docs/USER_MANUAL.html" >&2
  exit 1
fi
pandoc "$repo_root/docs/USER_MANUAL.md" \
  --from=gfm --to=html5 --standalone --toc --toc-depth=2 \
  --template="$repo_root/docs/user-manual-template.html" \
  --output="$repo_root/docs/USER_MANUAL.html"
