"""wb bench: real accepted tasks replayed against any model, graded by the tests they were accepted
with, so models (or a change to workbench itself) are compared on the same work.

A case (`wb bench add`) is one step of an accepted task: its base as a git bundle of that commit's
history alone (the agent can't read the answer out of git), its spec and acceptance command, the
grants it had, and its hidden checks: the reference's test files, copied over the replay's work
before the acceptance command runs again, plus an optional --check command. A run (`wb bench
run`) is a detached driver that replays each case on each model, one at a time, as a trial task
that wakes no one and records no results row, and appends one row per replay to
bench/runs.jsonl. Real work comes first: a task that needs the GPU stops the bench's
(cli._preempt_bench), and the driver waits until no real task runs before each replay.

The synthetic suite this replaces (removed 2026-11-16) graded toy tasks every model passed in one
round; these are the tasks this machine's models were actually given.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from . import ask_mcp, context, gitops, models, router, runner, store
from .runner import PKG_ROOT
from .watch import Watcher

PREEMPTED = "preempted: a real task needed the GPU"
REFUSED = ("No reviewer is available in this run, so nothing outside the everyday rules can be "
           "granted. Do the task without it, and say so in your summary.")
# The protocol's fold (cli.PROTOCOL, `transcript`'s marker) and its acceptance line; a step's
# opening (cli.cmd_next). Prompt openings are an API: these read the prompts wb wrote.
FOLD = "\n---\nHow to work"
TEST_LINE = re.compile(r"^- The acceptance command is `(.+)`\. Run it", re.M)
STEP_HEAD = re.compile(r"\AStep (\d+) of this task is done and committed \([^)]*\)\. The next step:\n\n")
TESTISH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+$|_test\.[^/.]+$|\.(test|spec)\.[^/]+$")
BASE_CHECK_SECS = 120  # the hidden checks on the base, at add time; past this, not checked


def home() -> Path:
    return store.home() / "bench"


def case_dir(name: str) -> Path:
    return home() / "cases" / name


def runs_file() -> Path:
    return home() / "runs.jsonl"


def cases() -> dict[str, dict]:
    d = home() / "cases"
    found = {}
    for p in sorted(d.iterdir()) if d.is_dir() else ():
        if (c := store.read_json(p / "case.json")) and c.get("name") == p.name:
            found[p.name] = c
    return found


# --- making a case ------------------------------------------------------------------------------

def _step_prompt(task: store.Task, step: int) -> str | None:
    """The prompt that handed the agent step `step`: round 1's, or the one `wb next` wrote."""
    rounds = sorted((int(p.name), p) for p in (task.dir / "rounds").iterdir()
                    if p.name.isdigit()) if (task.dir / "rounds").is_dir() else []
    for n, p in rounds:
        try:
            text = (p / "prompt.md").read_text(encoding="utf-8")
        except OSError:
            continue
        head = STEP_HEAD.match(text)
        if (step == 1 and n == 1 and not head) or (head and int(head.group(1)) == step - 1):
            return text
    return None


def _spec_of(prompt: str) -> str:
    text = STEP_HEAD.sub("", prompt, count=1)
    return text.split(FOLD, 1)[0].rstrip() + "\n"


def _reference(task: store.Task, step: int, steps: int) -> str | None:
    """The commit that landed step `step`: `wb next` marks its steps "(step N)", `wb accept` the last."""
    spec = task.spec
    want = f"Workbench-Task: {task.name}" + (f" (step {step})" if step < steps else "")
    log = gitops.git(spec["repo"], "log", "--format=%H%x00%B%x01", f"{spec['base']}..{spec['branch']}",
                     check=False)
    for entry in log.split("\x01"):
        sha, _, body = entry.strip().partition("\x00")
        if sha and want in body.splitlines():
            return sha
    return None


def _safe(path: str) -> bool:
    p = Path(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts


def bundle(repo: Path, commit: str, dest: Path) -> None:
    """A bundle of `commit`'s history alone, as branch main: made in a scratch clone, so the
    user's repo gets no ref, and the commits after it (the answer) aren't in it."""
    with tempfile.TemporaryDirectory(prefix="wb-bench-") as tmp:
        clone = Path(tmp) / "c"
        gitops.git(tmp, "clone", "-q", "--no-checkout", "--shared", str(repo), str(clone))
        gitops.git(clone, "update-ref", "refs/heads/main", commit)
        gitops.git(clone, "bundle", "create", str(dest.resolve()), "main")


def checkout(case: dict, dest: Path) -> None:
    """The case's base, checked out at `dest`, with an identity of its own (nothing commits)."""
    gitops.git(dest.parent, "clone", "-q", "-b", "main", str(case_dir(case["name"]) / "base.bundle"),
               str(dest))
    gitops.git(dest, "config", "user.name", "workbench bench")
    gitops.git(dest, "config", "user.email", "bench@workbench.invalid")


def overlay(case: dict, worktree: Path) -> None:
    """The hidden checks over the replay's work. The driver runs outside the lane and the agent
    could have planted links in its worktree, so nothing is written through one: a link on the
    way (a file or a directory) is replaced."""
    for rel in case["hidden"]:
        at = worktree
        for part in Path(rel).parts:
            at = at / part
            if at.is_symlink() or (at.exists() and at != worktree / rel and not at.is_dir()):
                at.unlink()
            elif at == worktree / rel and at.is_dir():
                shutil.rmtree(at)
        (worktree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(case_dir(case["name"]) / "hidden" / rel, worktree / rel)


class _At(store.Task):
    """A task whose worktree is somewhere else: the original's lane and environment around a
    scratch checkout of its reference (add's check)."""
    def __init__(self, name: str, worktree: Path):
        super().__init__(name)
        self._worktree = worktree

    @property
    def worktree(self) -> Path:
        return self._worktree


def add(name: str, step: int | None = None, hidden: list[str] | None = None,
        check: str | None = None, case_name: str | None = None) -> list[str]:
    """Make a case of one step of an accepted task; the report's lines. RuntimeError refuses."""
    task = store.Task(name)
    if not task.exists:
        raise RuntimeError(f"no task named {name!r}")
    spec, st = task.spec, task.status
    if st.get("phase") != "accepted":
        raise RuntimeError(f"{name} is {st.get('phase')}: a case is made from an accepted task")
    if spec.get("trial") or spec.get("bench"):
        raise RuntimeError(f"{name} is a trial run: a check of workbench, not work to replay")
    repo = Path(spec.get("repo") or "")
    if not repo.is_dir():
        raise RuntimeError(f"{repo} is gone: the case is cut from its repo")
    steps = st.get("steps", 1)
    step = step or steps
    if not 1 <= step <= steps:
        raise RuntimeError(f"{name} had {steps} step(s)")
    case_name = case_name or (name if steps == 1 else f"{name}-s{step}")
    if not store.NAME_RE.match(case_name):
        raise RuntimeError("a case name is lowercase letters, digits and hyphens, at most 48")
    if case_dir(case_name).exists():
        raise RuntimeError(f"case {case_name} exists (wb bench list)")
    ref = _reference(task, step, steps)
    if not ref:
        raise RuntimeError(f"no commit of step {step} on {spec.get('branch')} in {repo}: the branch "
                           "was deleted or rewritten")
    base = gitops.git(repo, "rev-parse", f"{ref}^").strip()
    prompt = _step_prompt(task, step)
    if prompt is None:
        raise RuntimeError(f"step {step}'s prompt is gone from {task.dir / 'rounds'}")
    test = TEST_LINE.search(prompt)
    if not test:
        raise RuntimeError(f"step {step} had no acceptance command: nothing to grade a replay by")
    changed = gitops.git(repo, "diff", "--name-only", "--no-renames", "--diff-filter=AM", base, ref
                         ).splitlines()
    picked = hidden if hidden is not None else [p for p in changed if TESTISH.search(p)]
    for p in picked:
        if not _safe(p):
            raise RuntimeError(f"{p}: a hidden check is a path inside the repo")
        if gitops.git(repo, "cat-file", "-t", f"{ref}:{p}", check=False).strip() != "blob":
            raise RuntimeError(f"{p} isn't a file at step {step}'s commit")
    if not picked and not check:
        raise RuntimeError(f"step {step} changed no test files ({len(changed)} files): name the "
                           "hidden checks with --hidden <path>…, or --check <command>")
    d = case_dir(case_name)
    tmp = d.with_name(f".{case_name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for p in picked:
            (tmp / "hidden" / p).parent.mkdir(parents=True, exist_ok=True)
            (tmp / "hidden" / p).write_bytes(gitops.git_bytes(repo, "show", f"{ref}:{p}"))
        bundle(repo, base, tmp / "base.bundle")
        (tmp / "spec.md").write_text(_spec_of(prompt), encoding="utf-8")
        (tmp / "reference.patch").write_bytes(gitops.git_bytes(repo, "diff", base, ref))
        row = next((r for r in reversed(store.results(trials=True)) if r.get("task") == name), {})
        measured = row.get("steps") or []
        step_row = measured[step - 1] if len(measured) >= step else {}
        grants = context.grants(task)
        case = {"name": case_name, "task": name, "step": step, "repo": str(repo), "base": base,
                "reference": ref, "size": spec.get("size"), "stack": spec.get("stack"),
                "test_cmd": test.group(1), "test_timeout": spec.get("test_timeout") or 900,
                "check_cmd": check, "hidden": picked, "allow": grants.get("allow") or [],
                "paths": grants.get("paths") or [], "skills": st.get("skills_ever") or [],
                "original": {"model": spec.get("model"), "rounds": row.get("rounds"),
                             "reviews": step_row.get("reviews", row.get("reviews")),
                             "minutes": row.get("minutes"), "steps": steps},
                "created_at": store.now()}
        # the hidden checks must fail on the base, or a replay that does nothing passes. Run as
        # the reference check and a replay's grade are, in the agent's lane: in a plain shell
        # with the caller's environment the same test saw other files, other variables and no
        # cleanup (#81, the 2026-11-18 audit)
        home().mkdir(parents=True, exist_ok=True)  # not in /tmp, where the git shim allows writes
        with tempfile.TemporaryDirectory(prefix=".base-", dir=home()) as scratch:
            work = Path(scratch) / "repo"
            gitops.git(scratch, "clone", "-q", "-b", "main", str(tmp / "base.bundle"), str(work))
            for p in picked:
                (work / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp / "hidden" / p, work / p)
            at = _At(name, work)
            codes = [runner.run_in_lane(at, c, BASE_CHECK_SECS)[0]
                     for c in (case["test_cmd"], check) if c]
        if all(c == 0 for c in codes):
            raise RuntimeError("the hidden checks already pass on the base: a replay that changed "
                               "nothing would pass. Name others (--hidden, --check)")
        case["base_check"] = "not checked (timed out)" if "timeout" in codes else "fails"
        # and pass on the step's own commit, run as a replay's grade is (the lane, read-only
        # git): workbench's suite commits, so clean-fixtures failed every replay (2026-11-17)
        home().mkdir(parents=True, exist_ok=True)  # not in /tmp, where the git shim allows writes
        with tempfile.TemporaryDirectory(prefix=".check-", dir=home()) as scratch:
            work = Path(scratch) / "repo"
            gitops.git(scratch, "clone", "-q", "--shared", "--no-checkout", str(repo), str(work))
            gitops.git(work, "checkout", "-q", "--detach", ref)
            at = _At(name, work)
            for c in (case["test_cmd"], check):
                code = c and runner.run_in_lane(at, c, case["test_timeout"])[0]
                if c and code != 0:
                    raise RuntimeError(
                        f"`{c}` fails on step {step}'s own commit (exit {code}) when run as a "
                        "replay is graded, in the agent's lane: no replay could pass it")
        store.write_json(tmp / "case.json", case)
        os.replace(tmp, d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return [f"case {case_name}: step {step} of {name}, base {base[:8]}, {spec.get('size')}",
            f"acceptance: `{case['test_cmd']}`" + (f", then `{check}`" if check else ""),
            "hidden checks: " + (", ".join(picked) if picked else "none (the --check command)"),
            f"on the base: {case['base_check']}",
            "a case is worth keeping when the spec pinned what the hidden tests call; "
            f"wb bench run --case {case_name} --model <id>"]


# --- running ------------------------------------------------------------------------------------

def wb_commit() -> str:
    return gitops.git(PKG_ROOT, "rev-parse", "--short", "HEAD", check=False).strip()


def driver() -> dict:
    """The driver running now ({pid, run}), or {}."""
    d = store.read_json(home() / "driver.json", {}) or {}
    pid = d.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {}
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return {}
    return d if models.runs(pid, "-m", "workbench.bench") else {}  # a recycled pid runs something else


def stop_driver() -> int | None:
    d = driver()
    if not d:
        return None
    try:
        os.kill(d["pid"], signal.SIGTERM)  # it stops its replay's runner and closes the task
    except ProcessLookupError:
        return None
    from .watch import wait_exit
    wait_exit(d["pid"], 30)
    return d["pid"]


def plan(run: str) -> dict:
    return store.read_json(home() / "runs" / run / "plan.json", {}) or {}


def start(model_ids: list[str], case_names: list[str], skills: list[str], label: str,
          repeat: int) -> tuple[str, list[str]]:
    """Write a run's plan and spawn its driver; the run id and the report's lines."""
    if d := driver():
        raise RuntimeError(f"bench run {d.get('run')} is running (wb bench stop)")
    have = cases()
    if not have:
        raise RuntimeError("no cases yet: wb bench add <accepted task>")
    unknown = [c for c in case_names if c not in have]
    if unknown:
        raise RuntimeError(f"no case {', '.join(unknown)} (wb bench list)")
    chosen = {n: have[n] for n in case_names} if case_names else have
    found = models.discover()
    if model_ids:
        ms = [models.resolve(x) for x in model_ids]
    else:
        ms = [m for m in found.values() if models.status(m)["available"]]
    if not ms:
        raise RuntimeError("no model that can run (wb models)")
    pairs = [(c, m.id) for c in chosen for m in ms
             if model_ids or router.allowed(m, chosen[c].get("size") or "significant")]
    run = time.strftime("%m%d-%H%M%S")
    d = home() / "runs" / run
    d.mkdir(parents=True)
    store.write_json(d / "plan.json", {"run": run, "label": label or "", "pairs": pairs,
                                       "skills": skills, "repeat": repeat, "created_at": store.now()})
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT), "WB_HOME": str(store.home())}
    env.pop("WB_TASK", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)  # its tasks are no session's
    env.pop("CLAUDE_PID", None)
    with open(d / "driver.log", "a") as log:
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "workbench.bench", run], cwd=d,
                                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
    store.write_json(home() / "driver.json", {"pid": proc.pid, "run": run})
    return run, [f"bench run {run}: {len(pairs)} replay(s) × {repeat}"
                 + (f", label {label}" if label else "") + ", one at a time; real tasks go first",
                 f"follow: wb bench report --run {run}   stop: wb bench stop"]


def _done(row: dict) -> bool:
    return row.get("outcome") in ("passed", "failed")


def _idle() -> bool:
    return not any(t.status.get("phase") in store.ACTIVE and not t.spec.get("bench")
                   for t in store.all_tasks())


CURRENT: dict = {}


def _on_term(signum, frame):
    raise SystemExit(1)


def drive(run: str) -> None:
    p = plan(run)
    label, reps = p.get("label", ""), int(p.get("repeat") or 1)
    for rep in range(1, reps + 1):
        for case_name, model in p.get("pairs") or []:
            key = (case_name, model, label, rep)
            done = {(r.get("case"), r.get("model"), r.get("label", ""), r.get("rep", 1))
                    for r in store.read_jsonl(runs_file())[0] if _done(r)}
            if key in done:
                continue
            case = cases().get(case_name)
            if not case:
                continue
            while True:
                with Watcher(store.tasks_dir()) as watcher:
                    while not _idle():  # a real task runs, or waits for the GPU: after it
                        watcher.wait(60.0)
                row = replay(case, model, rep, run, label, p.get("skills") or [])
                store.append_jsonl(runs_file(), row)
                if row["outcome"] != "preempted":
                    break


def _start_task(case: dict, model: str, name: str, repo: Path, skills: list[str], meta: dict) -> None:
    from . import cli  # cli imports this module
    argv = ["start", name, "--spec", str(case_dir(case["name"]) / "spec.md"), "--repo", str(repo),
            "--size", case.get("size") or "significant", "--model", model, "--test", case["test_cmd"],
            "--test-timeout", str(case.get("test_timeout") or 900), "--ask-budget", "0",
            "--checkin", "0", "--queue", "--trial", "--no-claude-md"]
    for s in skills:
        argv += ["--skill", s]
    for pat in case.get("allow") or []:
        argv += ["--allow", pat]
    for path in case.get("paths") or []:
        argv += ["--allow-path", path]
    a = cli.parser().parse_args(argv)
    a.bench = meta
    a.f(a)


def replay(case: dict, model: str, rep: int, run: str, label: str, skills: list[str]) -> dict:
    """One case on one model: start, wait, grade, close. Its runs.jsonl row."""
    from . import cli
    stamp = f"{int(time.time() * 1000) % 10 ** 9:09d}"
    name = f"bench-{case['name'][:30].rstrip('-')}-{stamp}"
    d = home() / "runs" / run / name
    d.mkdir(parents=True)
    row = {"ts": store.now(), "run": run, "label": label, "rep": rep, "case": case["name"],
           "model": model, "task": name, "wb_commit": wb_commit(), "skills": skills}
    began = time.time()
    log = open(d / "wb.log", "a")
    try:
        checkout(case, d / "repo")
        with redirect_stdout(log):
            try:
                _start_task(case, model, name, d / "repo", skills,
                            {"case": case["name"], "run": run, "label": label})
            except SystemExit:
                pass
        task = store.Task(name)
        if not task.exists:
            return {**row, "outcome": "error", "why": "wb start refused"}
        CURRENT["task"] = task
        st, asks_refused = _wait(task)
        phase = st.get("phase")
        row.update(phase=phase, rounds=st.get("round", 1),
                   auto_fixes=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                   tok_in=st.get("tok_in", 0), tok_out=st.get("tok_out", 0),
                   minutes=round((time.time() - began) / 60, 1), asks_refused=asks_refused,
                   visible_ok=(st.get("tests") or {}).get("ok"),
                   handovers=sum(e.get("kind") == "fresh" for e in
                                 store.read_jsonl(task.dir / "events.jsonl")[0]))
        if st.get("last") == PREEMPTED:
            return {**row, "outcome": "preempted"}
        if phase != "review":
            return {**row, "outcome": "failed", "why": (st.get("last") or phase)[:200]}
        (d / "diff.patch").write_text(gitops.diff(task.worktree, task.spec["base"]))
        overlay(case, task.worktree)
        out, ok = [], True
        for cmd in (case["test_cmd"], case.get("check_cmd")):
            if cmd:
                code, text = runner.run_in_lane(task, cmd, int(case.get("test_timeout") or 900))
                out.append(f"$ {cmd}\nexit: {code}\n\n{text}")
                ok = ok and code == 0
        (d / "grade.txt").write_text("\n\n".join(out))
        return {**row, "outcome": "passed" if ok else "failed",
                **({} if ok else {"why": "the hidden checks failed (grade.txt)"})}
    finally:
        log.close()
        task = CURRENT.pop("task", None)
        if task is not None and task.status.get("phase") not in store.CLOSED:
            with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
                try:
                    cli.cmd_discard(SimpleNamespace(name=task.name, reason="bench", spec_gap=None,
                                                    keep_branch=False))
                except SystemExit:
                    pass
        shutil.rmtree(d / "repo", ignore_errors=True)


def _wait(task: store.Task) -> tuple[dict, int]:
    """Until the replay needs Claude: refuse what it asks for (no one is there to grant it)."""
    from . import cli
    refused = 0
    with Watcher(task.dir) as watcher:
        while True:
            st = task.status
            phase = st.get("phase")
            if phase in cli.LIVE:
                cli.reap(task)
            if phase == "asking" and (qid := st.get("asking")) and ask_mcp.is_open(task, qid):
                ask_mcp.write_answer(task, qid, REFUSED)
                refused += 1
            elif phase in ("review", "stuck", "failed") and not cli.runner_alive(task):
                return st, refused
            watcher.wait(30.0)


# --- reading the results ------------------------------------------------------------------------

def rows(run: str | None = None, label: str | None = None) -> list[dict]:
    return [r for r in store.read_jsonl(runs_file())[0]
            if (run is None or r.get("run") == run) and (label is None or r.get("label", "") == label)
            and _done(r)]


def report(run: str | None = None, label: str | None = None) -> str:
    rs = rows(run, label)
    every = store.read_jsonl(runs_file())[0]
    other = [r for r in every if (run is None or r.get("run") == run) and not _done(r)]
    head = []
    if d := driver():
        p = plan(d["run"])
        n = len(p.get("pairs") or []) * int(p.get("repeat") or 1)
        head.append(f"running: {d['run']}, {len([r for r in every if r.get('run') == d['run'] and _done(r)])}"
                    f" of {n} replayed")
    # why a replay never started, and where its log is: a run whose every replay failed to start
    # said only "no replays yet" (the 2026-11-18 audit, #89)
    errors = [r for r in other if r.get("outcome") == "error"]
    unstarted = [f"{r.get('case')} on {r.get('model')}: didn't start: {r.get('why') or '?'} "
                 f"({home() / 'runs' / str(r.get('run')) / str(r.get('task')) / 'wb.log'})"
                 for r in errors[-10:]]
    if not rs:
        return "\n".join(head + ([f"no replay finished; {len(errors)} didn't start:"] + unstarted
                                 if errors else
                                 ["no replays yet" + (" (wb bench add, then wb bench run)"
                                                      if not cases() else "")]))
    by: dict = {}
    for r in rs:
        by.setdefault((r.get("label") or "-", r["model"]), []).append(r)
    out = head + [f"{'label':12} {'model':34} {'cases':>5} {'passed':>6} {'rounds':>6} "
                  f"{'autofix':>7} {'min':>6} {'local tok':>10}"]
    for (lab, model), g in sorted(by.items()):
        n = len(g)
        avg = lambda k: sum(r.get(k) or 0 for r in g) / n  # noqa: E731
        out.append(f"{lab[:12]:12} {model[:34]:34} {n:>5} {sum(r['outcome'] == 'passed' for r in g) / n:>6.0%} "
                   f"{avg('rounds'):>6.1f} {avg('auto_fixes'):>7.1f} {avg('minutes'):>6.1f} "
                   f"{int(avg('tok_in') + avg('tok_out')):>10}")
    have = cases()
    out.append("")
    for c in sorted({r["case"] for r in rs}):
        o = (have.get(c) or {}).get("original") or {}
        marks = ", ".join(f"{r['model'].split('/')[-1]} {'PASS' if r['outcome'] == 'passed' else 'fail'}"
                          f" {r.get('rounds', '?')}r {r.get('minutes', '?')}m"
                          for r in rs if r["case"] == c)
        out.append(f"{c}: {marks}  (accepted from {str(o.get('model') or '?').split('/')[-1]}, "
                   f"{o.get('reviews', '?')} review round(s))")
    if len(other) > len(errors):
        out.append(f"({len(other) - len(errors)} replay(s) preempted, left out)")
    out += unstarted
    return "\n".join(out)


def model_line(model_id: str) -> str | None:
    """For wb models and wb scout: how a model did on the cases (shown only; the router doesn't read it)."""
    g = [r for r in rows() if r.get("model") == model_id]
    if not g:
        return None
    latest = {}
    for r in g:
        latest[(r["case"], r.get("label") or "")] = r
    passed = sum(r["outcome"] == "passed" for r in latest.values())
    return f"{passed} of {len(latest)} case(s) passed"


def list_lines() -> list[str]:
    have = cases()
    if not have:
        return ["no cases yet: wb bench add <accepted task>"]
    out = []
    for n, c in have.items():
        done = [r for r in rows() if r.get("case") == n]
        out.append(f"{n:30} {c.get('size') or '?':11} {len(c.get('hidden') or [])} hidden"
                   + (" + check" if c.get("check_cmd") else "") + f", from {c['task']}"
                   + (f" step {c['step']}" if (c.get("original") or {}).get("steps", 1) > 1 else "")
                   + f", {len(done)} replay(s)")
    return out


def main() -> None:
    run = sys.argv[1]
    signal.signal(signal.SIGTERM, _on_term)
    store.absolute_env()
    try:
        drive(run)
    finally:
        mine = store.read_json(home() / "driver.json", {}) or {}
        if mine.get("pid") == os.getpid():
            (home() / "driver.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
