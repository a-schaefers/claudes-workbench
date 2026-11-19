"""wb try's hidden check: python3 check.py <worktree>. Claude runs it at review; the agent never
sees it. Exit 0 when stats.median does what the spec says."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
sys.dont_write_bytecode = True  # leaves no __pycache__ in the worktree under review
try:
    from stats import median
except ImportError as e:
    sys.exit(f"hidden check: FAIL (no stats.median: {e})")

failed = []
for xs, want in (([3, 1, 2], 2), ([4, 1, 3, 2], 2.5), ([5], 5), ([-1, -3], -2), ([2.5, 0.5, 1.5], 1.5)):
    try:
        got = median(xs)
        if got != want:
            failed.append(f"median({xs}) is {got!r}, not {want!r}")
    except Exception as e:  # noqa: BLE001 the check reports whatever went wrong
        failed.append(f"median({xs}) raised {e!r}")
xs = [3, 1, 2]
try:
    median(xs)
except Exception:  # noqa: BLE001 reported above
    pass
if xs != [3, 1, 2]:
    failed.append("median sorted its argument in place")
try:
    median([])
    failed.append("median([]) returned instead of raising ValueError")
except ValueError:
    pass
except Exception as e:  # noqa: BLE001
    failed.append(f"median([]) raised {e!r}, not ValueError")
print("hidden check: " + ("PASS" if not failed else "FAIL\n  " + "\n  ".join(failed)))
sys.exit(1 if failed else 0)
