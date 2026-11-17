#!/usr/bin/env bash
# Installs a release of OpenCode's CLI for CI, so the real-OpenCode tests run there instead of
# skipping (2026-11-17). Linux x64 only: the GitHub runner.
#
#   scripts/install-opencode.sh 2.3.0 <sha256>   that release, checked against the pinned hash
#   scripts/install-opencode.sh newest           the highest released version, checked against
#                                                its own SHA256SUMS (needs GH_TOKEN)
#
# It installs into $OPENCODE_DIR (default ~/.local/opencode) and prints that directory, for
# $GITHUB_PATH. 2.x isn't on npm (opencode-ai there is the 1.x line), so it comes from the
# release assets. "newest" sorts the tags by version, not by date: the 1.x line still ships
# patch releases, and GitHub's "Latest" marks the newest of those.
set -euo pipefail

want=${1:?usage: install-opencode.sh <version> <sha256> | newest}
sum=${2:-}
repo=anomalyco/opencode
asset=opencode-linux-x64.tar.gz
dir=${OPENCODE_DIR:-$HOME/.local/opencode}

if [ "$want" = newest ]; then
  tag=$(gh api "repos/$repo/releases?per_page=50" \
          --jq '.[] | select(.draft or .prerelease | not) | .tag_name' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
else
  tag=v$want
  [ -n "$sum" ] || { echo "install-opencode.sh: a pinned version needs its sha256" >&2; exit 64; }
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
url=https://github.com/$repo/releases/download/$tag
curl -fsSL --retry 3 -o "$tmp/$asset" "$url/$asset"
if [ -z "$sum" ]; then
  curl -fsSL --retry 3 -o "$tmp/SHA256SUMS" "$url/SHA256SUMS"
  sum=$(awk -v a="$asset" '$2 == a {print $1}' "$tmp/SHA256SUMS")
  [ -n "$sum" ] || { echo "install-opencode.sh: $tag lists no $asset" >&2; exit 1; }
fi
echo "$sum  $tmp/$asset" | sha256sum -c --quiet -
mkdir -p "$dir"
tar xzf "$tmp/$asset" -C "$dir" opencode
"$dir/opencode" --version >&2
echo "$dir"
