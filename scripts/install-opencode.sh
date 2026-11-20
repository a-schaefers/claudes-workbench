#!/usr/bin/env bash
# Installs a release of OpenCode's CLI for CI, so the real-OpenCode tests run there instead of
# skipping (2026-11-17). Linux x64 (the Ubuntu runner, WSL) and macOS on Apple silicon (the macos
# runner, since the 2026-11-19 audit: it ran without OpenCode, so those tests skipped there).
#
#   scripts/install-opencode.sh 2.3.0 <sha256>   that release, checked against the pinned hash
#                                                (this platform's asset's: check.yml pins both)
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
case "$(uname -sm)" in
  "Linux x86_64") asset=opencode-linux-x64.tar.gz ;;
  "Darwin arm64") asset=opencode-darwin-arm64.zip ;;
  *) echo "install-opencode.sh: no OpenCode asset for $(uname -sm) here" >&2; exit 1 ;;
esac
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
if command -v sha256sum >/dev/null; then
  echo "$sum  $tmp/$asset" | sha256sum -c --quiet -
else  # macOS
  echo "$sum  $tmp/$asset" | shasum -a 256 -c --quiet -
fi
mkdir -p "$dir"
case $asset in
  *.zip) unzip -oq "$tmp/$asset" opencode -d "$dir" ;;
  *) tar xzf "$tmp/$asset" -C "$dir" opencode ;;
esac
"$dir/opencode" --version >&2
echo "$dir"
