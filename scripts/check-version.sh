#!/usr/bin/env bash
# One version for the whole product (2026-11-17): the plugin and the code it runs are one thing,
# so any change to what users run moves it. A marketplace install keeps its cached copy of mod/
# until mod/.claude-plugin/plugin.json's version changes, and `wb` moves with every pull, so a
# change merged without a bump left an old pane beside new code, both claiming the same number.
# check.yml runs this on every pull request; scripts/ship.sh before it pushes.
#
#   scripts/check-version.sh [base]   base: what the branch merges into (default origin/main)
#
# Checks: plugin.json's version is workbench/__init__.py's __version__; and when the branch
# changes what ships, that version is higher than base's. What doesn't ship (and so needs no
# bump): the tests (the mod's too), CI and the scripts here, the docs and their images.
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
changed=$(git diff --name-only "$since" HEAD -- . ':(exclude)tests' ':(exclude,glob)mod/**/*.test.*' \
  ':(exclude).github' ':(exclude)scripts' ':(exclude)docs' ':(exclude)README.md' \
  ':(exclude)ARCHITECTURE.md' ':(exclude)CLAUDE.md' ':(exclude)LICENSE' ':(exclude).gitignore')
[ -n "$changed" ] || { echo "version $v (nothing that ships changed)"; exit 0; }
was=$(git show "$since:$plugin" | version)
if [ "$v" = "$was" ] || [ "$(printf '%s\n%s\n' "$was" "$v" | sort -V | tail -1)" != "$v" ]; then
  echo "check-version.sh: this changes what ships but leaves the version at $v (base: $was)." >&2
  echo "  Bump it in $plugin and workbench/__init__.py (a patch is enough)." >&2
  echo "  Changed: $(echo "$changed" | tr '\n' ' ')" >&2
  exit 1
fi
echo "version $was -> $v"
