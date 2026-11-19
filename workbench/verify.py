"""wb verify: one regression test run on the base and on the candidate, each in a scratch
checkout under the task's own lane and environment, so review can tell a proven fix from a test
that already passed before the change (#96). Neither run touches the user's checkout or the
worktree under review: the candidate is read through a snapshot of it (gitops.snapshot), and the
base gets the candidate's test files copied in, which is what makes a test separable.

A base failure proves the fix only when the test failed on an assertion: a missing module, a
command not found or a timeout says nothing about the bug, so the comparison is inconclusive
(#97: an unrelated setup error can't count as proof).
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from . import gitops, runner, store

LIMIT = 300  # seconds for each run; past it, that run is inconclusive

# what a test file's path looks like, in the languages workbench sees: tests/, test_x.py,
# x_test.go, x.test.ts, x.spec.js
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[A-Za-z]+$"
                       r"|\.(test|spec)\.[A-Za-z]+$")
# an assertion failing, as the common runners print one: what went wrong first (pytest's `E
# assert`, unittest's AssertionError, jest's expect), then a failed test's header
ASSERTION = re.compile(r"AssertionError|^E\s+assert|\bassert(ion)?\b|Expected|expected|Error: expect\("
                       r"|panic: ", re.I)
FAILED_TEST = re.compile(r"^(FAIL[:\s]|--- FAIL|\s*✕)")


class _At(store.Task):
    """The task's lane and environment around a scratch checkout."""
    def __init__(self, name: str, worktree: Path):
        super().__init__(name)
        self._worktree = worktree

    @property
    def worktree(self) -> Path:
        return self._worktree


def test_files(task: store.Task, tree: str) -> list[str]:
    """The candidate's changed files that look like tests: what the base run borrows."""
    names = gitops.git(task.worktree, "diff", "--name-only", "--no-renames",
                       task.spec["base"], tree).split("\n")
    return [n for n in names if n and TEST_PATH.search(n)]


def assertion(text: str) -> str:
    """The line that says what a failed assertion found, else a failed test's header; ''."""
    lines = [x.strip() for x in text.splitlines()]
    for pattern in (ASSERTION, FAILED_TEST):
        for line in lines:
            if pattern.search(line):
                return line[:300]
    return ""


def _run(task: store.Task, checkout, cmd: str, limit: int, keep=None) -> tuple[int | str, str]:
    """``cmd`` in a scratch clone that ``checkout`` sets up, under the task's lane; ``keep(work)``
    takes what the run left before the clone goes."""
    store.home().mkdir(parents=True, exist_ok=True)  # not in /tmp, where the git shim allows writes
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=store.home()) as scratch:
        work = Path(scratch) / "repo"
        gitops.git(scratch, "clone", "-q", "--shared", "--no-checkout", task.spec["repo"], str(work))
        checkout(work)
        out = runner.run_in_lane(_At(task.name, work), cmd, limit)
        if keep:
            keep(work)
        return out


def _checkouts(task: store.Task, tree: str, files: list[str]):
    """The base, with the candidate's ``files`` copied in, and the candidate as the worktree is."""
    head = gitops.git(task.worktree, "rev-parse", "HEAD").strip()

    def base(work: Path) -> None:
        gitops.git(work, "checkout", "-q", "--detach", task.spec["base"])
        if files:
            gitops.git(work, "checkout", "-q", tree, "--", *files)  # the candidate's, alone

    def candidate(work: Path) -> None:
        gitops.git(work, "checkout", "-q", "--detach", head)
        gitops.git(work, "read-tree", "-u", "--reset", tree)  # the worktree as it is, new files too
    return base, candidate


def verdict(base: int | str, base_out: str, cand: int | str) -> str:
    """proven, base-passes, candidate-fails or inconclusive."""
    if "timeout" in (base, cand) or "output limit" in (base, cand):
        return "inconclusive"
    if cand != 0:
        return "candidate-fails"
    if base == 0:
        return "base-passes"
    return "proven" if assertion(base_out) else "inconclusive"


def run(task: store.Task, cmd: str, files: list[str] | None = None, limit: int = LIMIT) -> dict:
    """Run ``cmd`` on the base (with the candidate's test files) and on the candidate; the
    result, also kept in tasks/<task>/verify/<n>/ with both outputs."""
    tree = gitops.snapshot(task.worktree)
    files = test_files(task, tree) if files is None else files
    if not files:
        raise RuntimeError("no test file among the candidate's changes: name the regression "
                           "test's files with --files (or review it as usual)")
    base, candidate = _checkouts(task, tree, files)
    b_code, b_out = _run(task, base, cmd, limit)
    c_code, c_out = _run(task, candidate, cmd, limit)
    d = task.dir / "verify"
    n = 1 + max((int(p.name) for p in d.glob("*") if p.name.isdigit()), default=0)
    out = d / str(n)
    out.mkdir(parents=True)
    (out / "base.txt").write_text(b_out, encoding="utf-8")
    (out / "candidate.txt").write_text(c_out, encoding="utf-8")
    result = {"cmd": cmd, "files": files, "tree": tree, "ts": store.now(),
              "base": {"exit": b_code, "assertion": assertion(b_out)},
              "candidate": {"exit": c_code, "assertion": assertion(c_out)},
              "verdict": verdict(b_code, b_out, c_code), "dir": str(out)}
    store.write_json(out / "result.json", result)
    return result


SAY = {"proven": "proven: it fails on the base and passes on the candidate",
       "base-passes": "not proven: it already passes on the base, so it doesn't show the bug",
       "candidate-fails": "the candidate fails it",
       "inconclusive": "inconclusive: the base run didn't fail on an assertion (a setup error, a "
                       "missing module) or a run timed out"}


def lines(r: dict) -> list[str]:
    """The report: the verdict, each run's exit and assertion, where the outputs are."""
    out = [f"regression check `{r['cmd']}` ({', '.join(r['files'])}): {SAY[r['verdict']]}"]
    for side in ("base", "candidate"):
        x = r[side]
        out.append(f"  {side}: exit {x['exit']}" + (f": {x['assertion']}" if x["assertion"] else ""))
    out.append(f"  outputs: {r['dir']}/{{base,candidate}}.txt")
    return out


SHOT = ".wb-shot.png"  # where a capture command writes, in its checkout ($WB_SHOT_OUT)


def shot(task: store.Task, cmd: str, files: list[str] | None = None, viewport: str = "1280x800",
         limit: int = LIMIT) -> dict:
    """Run one capture command on the base and on the candidate (#98), each in a scratch
    checkout under the task's lane: the same command, fixture and viewport, so the two images
    show the same interaction. The command is the project's own (a headless browser script, a
    test that saves a screenshot): it writes a PNG to $WB_SHOT_OUT and reads the size from
    $WB_SHOT_VIEWPORT. Kept in tasks/<task>/shots/<n>/ with what each image is of."""
    if not re.fullmatch(r"\d{2,5}x\d{2,5}", viewport):
        raise RuntimeError(f"--viewport {viewport}: give it as WIDTHxHEIGHT, e.g. 390x844")
    tree = gitops.snapshot(task.worktree)
    base, candidate = _checkouts(task, tree, files or [])
    d = task.dir / "shots"
    n = 1 + max((int(p.name) for p in d.glob("*") if p.name.isdigit()), default=0)
    out = d / str(n)
    out.mkdir(parents=True)
    st = task.status
    meta = {"task": task.name, "step": st.get("steps", 1), "round": st.get("round", 1),
            "cmd": cmd, "viewport": viewport, "files": files or [], "ts": store.now(),
            "base": {"rev": task.spec["base"]}, "candidate": {"rev": tree}}
    for side, checkout in (("base", base), ("candidate", candidate)):
        dest = out / f"{side}.png"

        def keep(work: Path, dest=dest) -> None:
            p = work / SHOT
            if p.is_file() and not p.is_symlink() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
                shutil.copyfile(p, dest)
        env = f"WB_SHOT_OUT=./{SHOT} WB_SHOT_VIEWPORT={viewport} "
        code, text = _run(task, checkout, env + f"sh -c {shlex.quote(cmd)}", limit, keep)
        (out / f"{side}.txt").write_text(text, encoding="utf-8")
        meta[side].update(exit=code, image=str(dest) if dest.exists() else None)
        if not dest.exists():  # said, never hidden: the ordinary code review still stands
            meta[side]["error"] = (f"exit {code}" if code != 0 else "no PNG at $WB_SHOT_OUT") + \
                f": {out / (side + '.txt')}"
    store.write_json(out / "shots.json", meta)
    return {**meta, "dir": str(out)}


def shot_lines(m: dict) -> list[str]:
    out = [f"screenshots of {m['task']}, step {m['step']} round {m['round']} at {m['viewport']} "
           f"(`{m['cmd']}`):"]
    for side in ("base", "candidate"):
        x = m[side]
        out.append(f"  {side} {x['rev'][:10]}: " + (x["image"] if x.get("image") else
                                                    f"no image: {x['error']}"))
    if not (m["base"].get("image") and m["candidate"].get("image")):
        out.append("  a capture failed: the code review stands on its own")
    return out


def note(task: store.Task) -> str:
    """For wb accept and the report: the newest check when it isn't proof of the change being
    accepted (not proven, or run on an earlier state of the worktree); '' when it is, or none ran."""
    r = last(task)
    if not r:
        return ""
    if r["stale"]:
        return (f"note: the last regression check (`{r['cmd']}`, {r['verdict']}) ran before the "
                "worktree last changed: it proves nothing about this state")
    if r["verdict"] != "proven":
        return f"note: the last regression check (`{r['cmd']}`) is {SAY[r['verdict']]}"
    return ""


def last(task: store.Task) -> dict | None:
    """The task's newest regression check, with ``stale`` when the worktree has changed since."""
    runs = sorted((p for p in (task.dir / "verify").glob("*") if p.name.isdigit()),
                  key=lambda p: int(p.name))
    if not runs:
        return None
    try:
        r = json.loads((runs[-1] / "result.json").read_text())
    except (OSError, ValueError):
        return None
    return {**r, "stale": task.worktree.is_dir() and gitops.snapshot(task.worktree) != r.get("tree")}
