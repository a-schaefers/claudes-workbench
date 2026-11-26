#!/usr/bin/env bash
# Installs the pinned TypeScript compiler that builds the web app (workbench/chat_static/src, #1323)
# and type-checks it and the browser tests: TypeScript 7 is native, one binary per platform in
# npm's @typescript/typescript-<platform> package, its lib .d.ts files beside it. The tarball comes
# straight from the registry and is checked against the hash pinned here, so neither npm nor Node
# is needed to build. The mod's type check runs against Claude Code's own tsconfig and compiler, so
# the web app can't borrow it.
#
#   scripts/install-tsc.sh          this platform's pinned tarball, unpacked into
#                                   $TSC_DIR (default ${XDG_CACHE_HOME:-~/.cache}/workbench/tsc/<version>)
#
# It prints the path of the tsc binary. A copy already there is checked again against
# its recorded hash and reused. A bump: the version and every hash below, together.
set -euo pipefail

version=7.0.2
case "$(uname -sm)" in
  "Linux x86_64") plat=linux-x64; sum=7ecad6f67377e831856367ab062ef394f21506a611405bf8ac0ff039348637d3 ;;
  "Linux aarch64") plat=linux-arm64; sum=c83d931ac9dd7549cde6e71246aa9d6a9812843023df3e277fe3b5dcf41dd0ea ;;
  "Darwin arm64") plat=darwin-arm64; sum=902e2fe1cf0799198ef902c6b8c310a450fef629a6baba41d45641ef75c04ebd ;;
  "Darwin x86_64") plat=darwin-x64; sum=eba158cb54050f723d5ff781438f33de5640054440bb4f2bd170cfe9bc2eb551 ;;
  *) echo "install-tsc.sh: no pinned TypeScript for $(uname -sm)" >&2; exit 1 ;;
esac
dir=${TSC_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/workbench/tsc/$version}

sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

if [ -x "$dir/lib/tsc" ] && [ "$(cat "$dir/.sha256" 2>/dev/null)" = "$sum" ] \
   && [ "$(cat "$dir/.tsc.sha256" 2>/dev/null)" = "$(sha "$dir/lib/tsc")" ]; then
  echo "$dir/lib/tsc"
  exit 0
fi

mkdir -p "$(dirname "$dir")"
tmp=$(mktemp -d "$(dirname "$dir")/.install.XXXXXX")  # beside it: one filesystem for the mv, not /tmp
trap 'rm -rf "$tmp"' EXIT
url=https://registry.npmjs.org/@typescript/typescript-$plat/-/typescript-$plat-$version.tgz
curl -fsSL --retry 3 -o "$tmp/tsc.tgz" "$url"
got=$(sha "$tmp/tsc.tgz")
[ "$got" = "$sum" ] || { echo "install-tsc.sh: $url is sha256 $got, not the pinned $sum" >&2; exit 1; }
mkdir -p "$tmp/x"
# only the package's regular files: npm tarballs hold one top directory, `package/`
tar xzf "$tmp/tsc.tgz" -C "$tmp/x" --no-same-owner package/
[ -x "$tmp/x/package/lib/tsc" ] || { echo "install-tsc.sh: the tarball holds no lib/tsc" >&2; exit 1; }
rm -rf "$dir.new"
mv "$tmp/x/package" "$dir.new"
echo "$sum" > "$dir.new/.sha256"
sha "$dir.new/lib/tsc" > "$dir.new/.tsc.sha256"
rm -rf "$dir"
mv "$dir.new" "$dir"
"$dir/lib/tsc" --version >&2
echo "$dir/lib/tsc"
