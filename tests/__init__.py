"""The suite's CI hooks, both off unless their variable is set (#950: the nightly's macOS job ran
past its 60 minutes and was cancelled on every run, and unittest prints failures only at the end,
so about 40 failures and an 18-minute hang were never reported).

WB_TEST_HANG_SECS=N: a test still running after N seconds gets every thread's stack on stderr
and fails with TimeoutError naming it, its tearDown runs, and the suite goes on, instead of a hang
eating the job's budget unnamed.

WB_TEST_SHARD=K/N: discovery (`python3 -m unittest`, no names) runs only the K-th of N shards,
every N-th test of the discovered order, so a suite splits across a matrix (check.yml's six, the
macOS nightly's three), as does naming the package (`python3 -m unittest tests`). Tests, not
classes: dealt whole by size, one class of slow end-to-end tests (Lifecycle, 147) weighed on one
shard; dealt one by one, each shard gets a slice of every class (no test has class or module
fixtures). Naming test modules, classes or tests on the command line runs exactly those."""
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
    """Discovery of the package: every test module, or with WB_TEST_SHARD only one shard's tests."""
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
    # discovery's order is sorted (modules, classes, methods), so every shard deals the same list
    standard_tests.addTests(t for i, t in enumerate(_cases(found)) if i % n == k - 1)
    return standard_tests
