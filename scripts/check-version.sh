#!/usr/bin/env bash
# The version users' copies move by (2026-11-17). A marketplace install keeps its cached copy of
# mod/ until mod/.claude-plugin/plugin.json's version changes, so a change to mod/ that merges
# without a bump never reaches them, and `wb` (which moves with every pull) ends up beside an old
# pane. check.yml runs this on every pull request; scripts/ship.sh before it pushes.
#
#   scripts/check-version.sh [base]   base: what the branch merges into (default origin/main)
#
# Checks: plugin.json's version is workbench/__init__.py's __version__; and when the branch
# changes mod/ (its tests aside: users never run them), that version is higher than base's.
set -euo pipefail

base=${1:-origin/main}
plugin=mod/.claude-plugin/plugin.json
version() { python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'; }

v=$(version < "$plugin")
py=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' workbench/__init__.py)
if [ "$v" != "$py" ]; then
  echo "check-version.sh: $plugin says $v, workbench/__init__.py says $py: make them the same" >&2
  exit 1
fi

since=$(git merge-base "$base" HEAD)
changed=$(git diff --name-only "$since" HEAD -- mod/ ':(exclude,glob)mod/**/*.test.*')
[ -n "$changed" ] || { echo "version $v (mod/ unchanged)"; exit 0; }
was=$(git show "$since:$plugin" | version)
if [ "$v" = "$was" ] || [ "$(printf '%s\n%s\n' "$was" "$v" | sort -V | tail -1)" != "$v" ]; then
  echo "check-version.sh: this changes mod/ but leaves the version at $v (base: $was)." >&2
  echo "  Users' copies move only when it rises: bump it in $plugin and workbench/__init__.py." >&2
  echo "  Changed: $(echo "$changed" | tr '\n' ' ')" >&2
  exit 1
fi
echo "version $was -> $v"
