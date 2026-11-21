"""The suite's CI hooks, both off unless their variable is set (#950: the nightly's macOS job ran
past its 60 minutes and was cancelled on every run, and unittest prints failures only at the end,
so about 40 failures and an 18-minute hang were never reported).

WB_TEST_HANG_SECS=N: a test still running after N seconds gets every thread's stack on stderr
and fails with TimeoutError naming it, its tearDown runs, and the suite goes on, instead of a hang
eating the job's budget unnamed.

WB_TEST_SHARD=K/N: discovery (`python3 -m unittest`, no names) runs only the K-th of N shards,
whole test classes dealt by their number of tests (each to the lightest shard so far), so a slow
platform's suite splits across a matrix, as does naming the package (`python3 -m unittest tests`).
Naming test modules, classes or tests on the command line runs exactly those."""
import faulthandler
import os
import signal
import sys
import threading
import unittest

HANG_SECS = int(os.environ.get("WB_TEST_HANG_SECS") or 0)

if HANG_SECS and hasattr(signal, "SIGALRM"):
    _run = unittest.TestCase.run

    def _run_watched(self, result=None):
        if threading.current_thread() is not threading.main_thread():  # signals are the main thread's
            return _run(self, result)

        def hung(signum, frame):
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            raise TimeoutError(f"{self.id()} hung past WB_TEST_HANG_SECS ({HANG_SECS} s)")
        before = signal.signal(signal.SIGALRM, hung)
        signal.alarm(HANG_SECS)
        try:
            return _run(self, result)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, before)

    unittest.TestCase.run = _run_watched


def _cases(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _cases(t)
        else:
            yield t


_loading = False  # load_tests is under way: a nested call returns its modules unsharded


def load_tests(loader, standard_tests, pattern):
    """Discovery of the package: every test module, or with WB_TEST_SHARD only one shard's classes."""
    global _loading
    here = os.path.dirname(os.path.abspath(__file__))
    nested, _loading = _loading, True
    try:
        # named (`python3 -m unittest tests`), discover() loads this package again and calls
        # this once more: both sharded, a shard ran about a third of its tests, 784 of 1196
        # never ran, unsaid (the review of #950). Only the outermost call deals the shards
        found = loader.discover(here, pattern=pattern or "test*.py", top_level_dir=os.path.dirname(here))
    finally:
        _loading = nested
    shard = os.environ.get("WB_TEST_SHARD")
    if not shard or nested:
        standard_tests.addTests(found)
        return standard_tests
    k, n = (int(x) for x in shard.split("/"))
    if not 1 <= k <= n:
        raise ValueError(f"WB_TEST_SHARD={shard}: K/N with 1 <= K <= N")
    cases = list(_cases(found))
    key = lambda t: f"{type(t).__module__}.{type(t).__qualname__}"  # noqa: E731
    sizes: dict[str, int] = {}
    for t in cases:
        sizes[key(t)] = sizes.get(key(t), 0) + 1
    shards: list[set] = [set() for _ in range(n)]
    load = [0] * n
    for c in sorted(sizes, key=lambda c: (-sizes[c], c)):  # the largest class to the lightest shard
        i = load.index(min(load))
        shards[i].add(c)
        load[i] += sizes[c]
    standard_tests.addTests(t for t in cases if key(t) in shards[k - 1])
    return standard_tests
