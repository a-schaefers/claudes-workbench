#!/usr/bin/env bash
# Runs one shard of the suite as several `python3 -m unittest` processes at once: check.yml's
# `suite` jobs (2026-11-25). A shard's one process sat half idle on its runner's four CPUs (a
# local shard 1/6: 404 s wall, 221 s of CPU; most tests wait on a fake OpenCode, a runner or a
# server), and the slowest of the six shards was every pull request's wait: its unittest step 6.5
# to 10 minutes, the check 9.5 to 10.5, nearly all of ship.sh's wait. Four processes on four
# CPUs ran that shard in 142 s, every test passing; eight in 111 s, the tail then one process's
# slow tests.
#
#   scripts/suite.sh K/N P    shard K of N (tests/__init__.py, WB_TEST_SHARD) in P processes
#   scripts/suite.sh 1/1 4    the whole suite in four
#
# Process j runs shard K+N*(j-1) of N*P: every test of discovery's i-th place with i mod N = K-1
# runs once, in exactly one process, as in one `WB_TEST_SHARD=K/N python3 -m unittest`. Each
# writes its own log, printed whole as it ends (interleaved, unittest's tracebacks would mix),
# under a header naming its WB_TEST_SHARD, so a failure reproduces with that shard alone. A
# cancelled job prints every log so far: a cancel that reported nothing is #950's lesson.
# Exit 1 if any process failed (each one's status, never through a pipe), 64 on bad arguments.
set -uo pipefail

shard=${1:-} procs=${2:-}
if ! [[ $procs =~ ^[1-9][0-9]*$ ]] || ! [[ $shard =~ ^([0-9]+)/([0-9]+)$ ]]; then
  echo "usage: scripts/suite.sh K/N P" >&2; exit 64
fi
k=${BASH_REMATCH[1]} n=${BASH_REMATCH[2]}  # the last match's: the shard's
if [ "$k" -lt 1 ] || [ "$k" -gt "$n" ]; then
  echo "scripts/suite.sh: K/N with 1 <= K <= N, not $shard" >&2; exit 64
fi

logs=$(mktemp -d)
trap 'rm -rf "$logs"' EXIT
pids=() names=()
for j in $(seq 1 "$procs"); do
  sub=$((k + n * (j - 1)))/$((n * procs))
  names+=("$sub")
  WB_TEST_SHARD=$sub python3 -m unittest > "$logs/$j" 2>&1 &
  pids+=($!)
done
echo "shard $shard: $procs processes, WB_TEST_SHARD ${names[*]}"

shown=()
show() {  # $1: index, $2: status
  [ -z "${shown[$1]:-}" ] || return 0
  shown[$1]=1
  echo "=== WB_TEST_SHARD=${names[$1]}: $2"
  cat "$logs/$(($1 + 1))"
}
cancelled() {
  for i in "${!pids[@]}"; do kill "${pids[$i]}" 2>/dev/null; done
  for i in "${!pids[@]}"; do show "$i" "cancelled"; done
  exit 1
}
trap cancelled INT TERM

rc=0
running=("${!pids[@]}")
while [ "${#running[@]}" -gt 0 ]; do
  # whichever ends first is printed first: a failure shows before the slowest process is done
  rest=()
  for i in "${running[@]}"; do rest+=("${pids[$i]}"); done
  ended=
  wait -n -p ended "${rest[@]}"; status=$?
  [ -n "$ended" ] || { echo "scripts/suite.sh: wait failed ($status)" >&2; cancelled; }
  still=()
  for i in "${running[@]}"; do
    if [ "${pids[$i]}" = "$ended" ]; then
      [ "$status" -eq 0 ] || rc=1
      show "$i" "exit $status"
    else
      still+=("$i")
    fi
  done
  running=("${still[@]}")
done
exit "$rc"
