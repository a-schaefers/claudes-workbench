#!/usr/bin/env bash
# nightly.yml's `wsl` job, run as root inside WSL2's Debian on a Windows runner, from the
# checkout on the Windows drive (the 2026-11-19 audit: nothing ran WSL at all). It installs what
# the suite needs, then runs it as a user who isn't root (root reads what the lane and the tests'
# unreadable directories refuse), from a clone in WSL's own file system, as people run it.
# OPENCODE_VERSION, OPENCODE_SHA256, WB_TEST_SHARD and WB_TEST_HANG_SECS come from the job
# (WSLENV): the job's shard, as four processes (scripts/suite.sh: one process ran past the job's
# 60 minutes, 2026-11-25).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 curl ca-certificates sudo >/dev/null
id ci >/dev/null 2>&1 || useradd -m ci
src=$PWD
sudo -u ci -H --preserve-env=OPENCODE_VERSION,OPENCODE_SHA256,WB_TEST_SHARD,WB_TEST_HANG_SECS bash -euo pipefail -s "$src" <<'EOF'
git config --global --add safe.directory '*'   # the checkout belongs to Windows' user
git config --global user.name "workbench CI"
git config --global user.email "ci@invalid"
rm -rf ~/workbench
git clone -q --no-hardlinks "$1" ~/workbench
cd ~/workbench
echo "sh is $(readlink -f /bin/sh); $(python3 --version); kernel $(uname -r)"
OPENCODE_DIR=~/.local/opencode scripts/install-opencode.sh "$OPENCODE_VERSION" "$OPENCODE_SHA256" >/dev/null
export PATH=~/.local/opencode:$PATH
python3 -c 'from workbench import lane, hardware; print("system", hardware.system(), "· Landlock ABI", lane.abi() or "none here: the Lane tests skip")'
exec scripts/suite.sh "$WB_TEST_SHARD" 4
EOF
