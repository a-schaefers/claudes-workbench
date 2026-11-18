#!/usr/bin/env bash
# Tags and releases each new version once it is on main (2026-11-17). release.yml runs it on every
# push to main, daily (a merge armed with GitHub Actions' token starts no push workflow) and by
# hand; it does nothing when the version already has its tag, so any run may repeat.
#
#   scripts/release.sh [--dry-run]   needs GH_TOKEN with contents: write, and full history
#
# The tag is v<version> with a trailing ".0" dropped (0.90.0 was v0.90, 0.90.1 is v0.90.1), on
# the first commit of main's first-parent history that carried the version: the merge that
# bumped it, not whatever came after. The notes are GitHub's, generated from the pull requests
# since the previous release; the owner can rewrite them afterwards (`gh release edit`).
set -euo pipefail

dry=
[ "${1:-}" = --dry-run ] && dry=1
plugin=mod/.claude-plugin/plugin.json
v=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])' < "$plugin")
tag=v${v%.0}

if git ls-remote --exit-code --tags origin "refs/tags/$tag" > /dev/null; then
  echo "$tag is released"
  exit 0
fi
at=$(git log --first-parent --diff-merges=first-parent --no-patch --format=%H \
       -S"\"version\": \"$v\"" -- "$plugin" | tail -1)
[ -n "$at" ] || { echo "release.sh: no commit on this history sets version $v" >&2; exit 1; }
if [ -n "$dry" ]; then
  echo "would release $tag at $(git log -1 --format='%h %s' "$at")"
  exit 0
fi
gh release create "$tag" --target "$at" --title "workbench $v" --generate-notes
echo "released $tag at ${at:0:7}"
