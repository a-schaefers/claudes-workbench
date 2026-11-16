"""wb: hand a coding task to a local model, review it, accept or discard it.

Every command prints a short, plain report: it is read by Claude, whose tokens are the scarce
resource, and by a person at a terminal.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path

from . import __version__, bench, chat, gitops, hardware, models, results, router, store, transcript
from .watch import Watcher, wait_exit
from .runner import PKG_ROOT

PROTOCOL = """

---
How to work (workbench protocol):
- You are in a git worktree on branch {branch}. Git is read-only for you; leave the changes
  uncommitted. Claude commits, pushes and deploys.
{test_line}- If the spec is genuinely ambiguous, you may call the `ask_reviewer` tool ({budget} \
question(s) for this task). Otherwise make reasonable choices and say what you assumed.
- Stay inside the scope of the spec. No unrelated refactors, no new dependencies unless asked.
- Finish with a short summary: the files you changed, the test result, and any assumptions.
"""


def die(msg: str, code: int = 1):
    print(f"wb: {msg}", file=sys.stderr)
    sys.exit(code)


def need(name: str) -> store.Task:
    t = store.Task(name)
    if not t.exists:
        die(f"no task named {name!r} (wb status lists them)")
    return t


def spawn_runner(task: store.Task) -> int:
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT), "WB_HOME": str(store.home())}
    with open(task.dir / "runner.log", "a") as log:
        proc = subprocess.Popen([sys.executable, "-m", "workbench.runner", task.name],
                                cwd=PKG_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=log, start_new_session=True)  # outlives this command
    task.set_status(pid=proc.pid)
    return proc.pid


def runner_alive(task: store.Task) -> bool:
    pid = task.status.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def ago(ts) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h"


def pick_model(size: str, wanted: str | None) -> tuple[models.Model, dict, str]:
    """The task's model: the one asked for, else the router's choice on the evidence."""
    if wanted:
        try:
            m = models.resolve(wanted)
        except KeyError as e:
            die(str(e).strip('"'))
        return m, models.status(m), "asked for"
    top, why = router.choose(size)
    if top is None:
        die(why, 3)
    return top.model, top.state, why


# --- commands -----------------------------------------------------------------------------------

def _has_file(repo: Path, ref: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
                          capture_output=True).returncode == 0


def _need_claude_md(repo: Path, base: str, a) -> None:
    if a.bench or a.no_claude_md:
        return
    if not _has_file(repo, base, "CLAUDE.md"):
        die(f"{repo} has no CLAUDE.md at {base}: the agent would start blind. Write it (what the "
            "project is, how to build and test it, its conventions), commit it, and run "
            f"wb init {repo} to link AGENTS.md; --no-claude-md skips this check")


def cmd_start(a):
    if not store.NAME_RE.match(a.name):
        die("a task name is lowercase letters, digits and hyphens, at most 48")
    task = store.Task(a.name)
    if task.exists:
        die(f"task {a.name!r} exists ({task.status.get('phase')}); discard it or pick a new name")
    try:
        repo = gitops.toplevel(Path(a.repo).resolve())
    except gitops.GitError:
        die(f"{a.repo} is not inside a git repository: wb init {a.repo} sets the project up")
    spec_text = Path(a.spec).read_text() if a.spec != "-" else sys.stdin.read()
    if not spec_text.strip():
        die("the spec is empty")
    m, state, why = pick_model(a.size, a.model)
    if not state["available"] and not a.force:
        die(f"{m.id} can't run: {state['why']}. (--force queues it anyway)", 3)
    dirty = gitops.git(repo, "status", "--porcelain").strip()
    _need_claude_md(repo, a.base, a)

    task.dir.mkdir(parents=True)
    try:
        base = gitops.create_worktree(repo, a.name, task.worktree, a.base)
    except gitops.GitError as e:
        task.dir.rmdir()
        die(str(e))
    budget = a.ask_budget
    store.write_json(task.dir / "task.json", {
        "name": a.name, "repo": str(repo), "base": base, "base_ref": a.base,
        "branch": gitops.branch_name(a.name), "model": m.id, "model_label": m.label,
        "opencode_model": m.id, "route": why, "bench": a.bench, "size": a.size, "test_cmd": a.test,
        "auto_fix": a.auto_fix, "ask_budget": budget, "ask_timeout": a.ask_timeout,
        "checkin": (a.checkin if a.checkin is not None
                    else 10 if a.size == "significant" else 0) if not a.bench else 0,
        "round_timeout": a.round_timeout, "silence_timeout": a.silence_timeout,
        # a benchmark measures the model unaided: no nudges
        "think_timeout": 0 if a.bench else (a.think_timeout if a.think_timeout is not None
                                            else bench.think_budget(m.id)),
        "created_at": store.now(), "wb_version": __version__})
    test_line = (f"- The acceptance command is `{a.test}`. Run it before you finish and make it "
                 "pass. Never weaken, skip or delete tests to get there.\n") if a.test else ""
    prompt = spec_text.rstrip() + PROTOCOL.format(branch=gitops.branch_name(a.name),
                                                  test_line=test_line, budget=budget)
    task.round_dir(1).mkdir(parents=True)
    task.round_dir(1).joinpath("prompt.md").write_text(prompt)
    task.set_status(phase="queued", round=1, reviews=0, auto_fixes=0, tools=0,
                    tok_in=0, tok_out=0, session=None, last="starting")
    spawn_runner(task)
    if a.bench:
        return
    print(f"started {a.name} on {m.label} · branch {gitops.branch_name(a.name)} · "
          f"base {base[:8]}" + (f" · tests `{a.test}`, auto-fix {a.auto_fix}" if a.test else ""))
    if dirty:
        print("note: the repo has uncommitted changes; the agent's worktree starts from "
              f"{a.base}, without them")
    print(f"model: {why}" if why != "asked for" else f"model: {m.id} (asked for)")
    if not state["available"]:
        print(f"note: queued although {state['why']}")
    elif not state["resident"]:
        print("note: the runner loads it onto the GPU first, unloading what is there")
    print(f"next: wb wait {a.name}   (the workbench mod wakes Claude by itself when loaded)")


def cmd_feedback(a):
    task = need(a.name)
    st = task.status
    if st.get("phase") not in ("review", "stuck", "failed"):
        die(f"{a.name} is {st.get('phase')}; feedback goes to a task waiting for review")
    if runner_alive(task):
        die(f"{a.name}'s runner is still running")
    text = Path(a.message).read_text() if a.message != "-" else sys.stdin.read()
    n = st.get("round", 1) + 1
    task.round_dir(n).mkdir(parents=True, exist_ok=True)
    task.round_dir(n).joinpath("prompt.md").write_text(
        "Review feedback from the senior reviewer. Make exactly these changes, then re-run the "
        "acceptance command and finish with a short summary:\n\n" + text.strip())
    reviews = st.get("reviews", 0) + 1
    task.set_status(context="fresh" if a.fresh else "keep" if a.keep_context else None)
    task.set_status(round=n, reviews=reviews, phase="queued",
                    auto_fixes_total=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                    auto_fixes=0)
    spawn_runner(task)
    _ack_all(task)
    print(f"feedback round {reviews} sent to {a.name} (round {n})"
          + ("  — that is past the usual 3 review rounds" if reviews > 3 else ""))


def cmd_next(a):
    """Commit the approved step on the task's branch and give the same agent the next one: same
    worktree, same session (so it keeps what it has read), unless it is full or --fresh."""
    task = need(a.name)
    spec, st = task.spec, task.status
    if st.get("phase") != "review":
        die(f"{a.name} is {st.get('phase')}; wb next follows an approved step under review")
    if runner_alive(task):
        die(f"{a.name}'s runner is still running")
    step = st.get("steps", 1)
    try:
        commit = gitops.commit_all(task.worktree, f"{a.message}\n\nWorkbench-Task: {a.name} "
                                   f"(step {step})\nOpenCode-Session: {st.get('session')}")
    except gitops.GitError as e:
        die(str(e))
    if a.test is not None:
        store.write_json(task.dir / "task.json", {**spec, "test_cmd": a.test})
    test = a.test if a.test is not None else spec.get("test_cmd")
    test_line = (f"- The acceptance command is `{test}`. Run it before you finish and make it "
                 "pass. Never weaken, skip or delete tests to get there.\n") if test else ""
    budget = spec.get("ask_budget", 0)
    n = st.get("round", 1) + 1
    task.round_dir(n).mkdir(parents=True, exist_ok=True)
    task.round_dir(n).joinpath("prompt.md").write_text(
        f"Step {step} of this task is done and committed ({commit}). The next step:\n\n"
        + Path(a.spec).read_text().rstrip()
        + PROTOCOL.format(branch=spec["branch"], test_line=test_line, budget=budget))
    task.event({"ts": store.now(), "round": n, "kind": "next_step",
                "text": f"step {step} committed as {commit}; next step sent"})
    task.set_status(round=n, steps=step + 1, step_round=n, phase="queued", reviews=0,
                    context="fresh" if a.fresh else None,
                    auto_fixes_total=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                    auto_fixes=0, last=f"step {step} committed; next step queued")
    spawn_runner(task)
    _ack_all(task)
    print(f"step {step} of {a.name} committed as {commit}; step {step + 1} sent "
          + ("to a fresh session" if a.fresh else "to the same session"))


def cmd_answer(a):
    task = need(a.name)
    qid = a.id or task.status.get("asking")
    if not qid:
        die(f"{a.name} has no open question")
    q = task.dir / "ask" / f"{qid}.json"
    if not q.exists():
        die(f"no question {qid} in {a.name}")
    text = a.text if a.text is not None else Path(a.file).read_text()
    (task.dir / "ask" / f"{qid}.answer").write_text(text.strip() + "\n")
    _ack_all(task)
    print(f"answered {a.name}/{qid}")


def _ack_file(task):
    return task.dir / "acked"


def _acked(task):
    f = _ack_file(task)
    return float((f.read_text() if f.exists() else "0") or 0)


def _ack_all(task):
    """Claude acted on the task: whatever it was notified about so far is handled, except news
    still true now. A note or a diff run seconds after the task failed must not swallow the
    failure notice Claude hasn't seen: the mark stops just short of it."""
    phase = task.status.get("phase")
    news = "question" if phase == "asking" else phase
    acked = _acked(task)
    rows, _ = store.read_jsonl(store.home() / "notify.jsonl")
    pending = [r["ts"] for r in rows if r["task"] == task.name and r["kind"] == news and r["ts"] > acked]
    mark = min(pending) - 0.001 if pending else store.now()
    _ack_file(task).write_text(str(max(acked, mark)))


def cmd_wait(a):
    """Block until a task (or any task) raises a notification nobody has acknowledged yet."""
    deadline = time.time() + a.timeout if a.timeout else None
    path = store.home() / "notify.jsonl"
    path.touch()
    watcher = Watcher(store.home(), *([need(a.name).dir] if a.name else []))
    while True:
        rows, _ = store.read_jsonl(path)
        for row in reversed(rows):
            if a.name and row["task"] != a.name:
                continue
            task = store.Task(row["task"])
            if row["ts"] > _acked(task):
                _ack_file(task).write_text(str(row["ts"]))
                print(f"[{row['kind']}] {row['task']}\n{row['text']}")
                return
        if a.name:
            task = need(a.name)
            phase = task.status.get("phase")
            if phase in store.CLOSED:
                die(f"{a.name} is closed", 2)
            # Already waiting on Claude, its notice taken by the mod or another waiter: say so
            # now rather than block until a notice that will never come.
            mine = [r for r in rows if r["task"] == a.name]
            if (phase in store.NEEDS_CLAUDE and mine and (phase == "asking" or not runner_alive(task))
                    and mine[-1]["kind"] == ("question" if phase == "asking" else phase)):
                print(f"[{mine[-1]['kind']}] {a.name} (already delivered once)\n{mine[-1]['text']}")
                return
        if deadline and time.time() > deadline:
            die("timed out waiting", 2)
        # woken by the change itself; the minute bound re-checks the runner is alive
        watcher.wait(min(60.0, deadline - time.time()) if deadline else 60.0)


def cmd_watch(a):
    """The live conversation between Claude and the agent: for a person, in a terminal."""
    if a.name:
        task = need(a.name)
    else:
        tasks = sorted(store.all_tasks(), key=lambda t: t.status.get("updated_at") or 0)
        live = [t for t in tasks if t.status.get("phase") not in store.CLOSED]
        if not tasks:
            die("no tasks yet")
        task = (live or tasks)[-1]
    try:
        transcript.follow(task, verbose=a.verbose, once=a.once or a.json, as_json=a.json,
                          color=False if a.no_color else None)
    except KeyboardInterrupt:
        print()
    except BrokenPipeError:  # piped into head/less and the reader quit
        sys.stdout = open(os.devnull, "w")


def cmd_status(a):
    tasks = [need(a.name)] if a.name else [t for t in store.all_tasks()
                                           if a.all or t.status.get("phase") not in store.CLOSED]
    if not tasks:
        print("no open tasks" + ("" if a.all else " (wb status --all shows closed ones)"))
        return
    print(f"{'task':22} {'phase':9} {'rnd':>3} {'model':16} {'age':>5} {'idle':>5}  last")
    for t in tasks:
        st, sp = t.status, t.spec
        phase = st.get("phase", "?")
        if phase in store.ACTIVE and not runner_alive(t) and phase != "asking":
            phase += "?"  # the runner died without saying so
        short = sp.get("model", "").split("/")[-1][:16]
        print(f"{t.name:22} {phase:9} {st.get('round', 1):>3} {short:16} "
              f"{ago(sp.get('created_at')):>5} {ago(st.get('updated_at')):>5}  "
              f"{(st.get('last') or '')[:70]}")


def cmd_log(a):
    task = need(a.name)
    off = 0
    rows, off = store.read_jsonl(task.dir / "events.jsonl", off)

    def show(r):
        t = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
        k = r["kind"]
        if k == "tool":
            mark = "" if r["ok"] else "  ✗ " + r.get("error", "")[:100]
            print(f"{t} r{r['round']} {r['tool']:8} {r['brief']}{mark}")
        elif k in ("text", "ask", "answer", "stopped", "autofix", "round", "tests", "error"):
            body = r.get("text", "")
            if k == "text" and not a.verbose:
                body = body.strip().splitlines()[0][:110] if body.strip() else ""
            print(f"{t} r{r['round']} {k:8} {body}")

    for r in rows[-a.n:] if a.n else rows:
        show(r)
    watcher = Watcher(task.dir) if a.follow else None
    while a.follow:
        watcher.wait(30)
        more, off = store.read_jsonl(task.dir / "events.jsonl", off)
        for r in more:
            show(r)
        if task.status.get("phase") in store.NEEDS_CLAUDE | store.CLOSED and not more:
            break


def cmd_report(a):
    task = need(a.name)
    n = a.round or task.status.get("round", 1)
    p = task.round_dir(n) / "summary.md"
    print(p.read_text() if p.exists() else f"round {n} has no report yet "
          f"({task.status.get('phase')}: {task.status.get('last')})")


def cmd_diff(a):
    """The work under review: the whole task, or after `wb next` the current step (--all: all)."""
    task = need(a.name)
    base = "HEAD" if task.status.get("steps", 1) > 1 and not a.all else task.spec["base"]
    print(gitops.diff(task.worktree, base, stat=a.stat, paths=a.paths) or "(no changes)")


def _stop_runner(task):
    pid = task.status.get("pid")
    if pid and runner_alive(task):
        try:
            os.kill(pid, signal.SIGTERM)  # the runner stops its agent's process group itself
        except ProcessLookupError:
            return
        wait_exit(pid, 15)


LIVE = ("queued", "running", "testing", "asking")


def _open_tasks():
    return [t for t in store.all_tasks()
            if t.status.get("phase") not in store.CLOSED
            and (runner_alive(t) or t.status.get("phase") in LIVE)]


def bench_pids() -> set[int]:
    """Running `wb bench` processes on this state directory: from bench.pid, and from the process
    table, which also finds a run that predates the pid file or lost it."""
    pids = set()
    pidfile = store.home() / "bench.pid"
    try:
        pids.add(int(pidfile.read_text()))
    except (OSError, ValueError):
        pass
    mine = str(store.home().resolve())
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            env = dict(x.split(b"=", 1) for x in (proc / "environ").read_bytes().split(b"\0")
                       if b"=" in x)
        except OSError:  # gone, or not ours
            continue
        words = [a.decode(errors="replace") for a in argv]
        at = next((i for i, w in enumerate(words) if Path(w).name == "wb"), None)
        if at is None or words[at + 1:at + 2] != ["bench"] or "--report" in words:
            continue
        home = env.get(b"WB_HOME", b"").decode() or str(Path(env.get(b"HOME", b"").decode())
                                                        / ".local" / "state" / "workbench")
        if str(Path(home).resolve()) == mine:
            pids.add(int(proc.name))
    return {p for p in pids if _alive(p)}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_everything() -> list[str]:
    """"bail out claude": the benchmark, then every agent. Nothing is finished or thrown away:
    real tasks keep their worktrees (stopped, for wb feedback or wb discard later); bench tasks
    are scratch and are discarded."""
    stopped = []
    benches = bench_pids()
    for pid in benches:  # first, so it starts no new task
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(f"wb bench (pid {pid})")
        except ProcessLookupError:
            pass
    for pid in benches:  # gone before the scan below, or a task it just started would be missed
        wait_exit(pid, 5)
    (store.home() / "bench.pid").unlink(missing_ok=True)
    for t in _open_tasks():
        _stop_runner(t)
        t.event({"ts": store.now(), "round": t.status.get("round"), "kind": "stopped",
                 "text": "bail out: stopped by the user"})
        t.set_status(phase="stuck", last="bail out: stopped", pid=None)
        _ack_all(t)
        if t.spec.get("bench"):
            with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
                cmd_discard(argparse.Namespace(name=t.name, reason="bail out", keep_branch=False))
            stopped.append(f"{t.name} (bench, discarded)")
        else:
            stopped.append(f"{t.name} (worktree kept: {t.worktree})")
    return stopped


def cmd_stop(a):
    if a.all:
        stopped = stop_everything()
        print("stopped: " + ("; ".join(stopped) if stopped else "nothing was running"))
        return
    if not a.name:
        die("name a task, or --all")
    task = need(a.name)
    _stop_runner(task)
    task.set_status(phase="stuck", last="stopped by the reviewer", pid=None)
    print(f"stopped {a.name}; wb feedback continues it, wb discard drops it")


def cmd_steer(a):
    """A note to a running agent: delivered when its current run ends, or at once with --now."""
    task = need(a.name)
    phase = task.status.get("phase")
    if phase not in ("queued", "running", "testing", "asking") or not runner_alive(task):
        die(f"{a.name} is {phase}, not running: use wb feedback (or wb answer for a question)")
    text = (Path(a.message).read_text() if a.message else a.text).strip()
    if not text:
        die("empty note")
    store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text, "now": a.now})
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "steer",
                "now": a.now, "text": text})
    if a.now:
        print(f"interrupting {a.name}: it stops within a few seconds and continues with your note")
    else:
        print(f"note queued for {a.name}: delivered when its current run ends, before the tests")


def cmd_note(a):
    """Claude's own words in the task's transcript: a captain's log entry the user sees."""
    task = need(a.name)
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "captain",
                "text": a.text.strip()})
    _ack_all(task)
    print(f"noted on {a.name}")


def cmd_takeover(a):
    """Stop the agent and hand the worktree to Claude, to finish by hand or hand back."""
    if a.all:  # "just get it done claude"
        tasks = [t for t in _open_tasks() if not t.spec.get("bench")]
        if not tasks:
            print("no open delegated tasks to take over")
        for t in tasks:
            cmd_takeover(argparse.Namespace(name=t.name, all=False))
        return
    if not a.name:
        die("name a task, or --all")
    task = need(a.name)
    if task.status.get("phase") in store.CLOSED:
        die(f"{a.name} is {task.status.get('phase')}")
    _stop_runner(task)
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "takeover",
                "text": "Claude took over"})
    task.set_status(phase="review", last="Claude took over", pid=None)
    _ack_all(task)
    print(f"took over {a.name}: the agent is stopped. Edit in {task.worktree}, then "
          f"wb accept {a.name} -m \"…\", or hand it back with wb feedback {a.name} --message <file>")


def cmd_accept(a):
    task = need(a.name)
    spec, st = task.spec, task.status
    if st.get("phase") != "review" and not a.force:
        die(f"{a.name} is {st.get('phase')}, not waiting for review (--force accepts anyway)")
    repo = Path(spec["repo"])
    rounds, autofix = st.get("round", 1), st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0)
    msg = (f"{a.message}\n\nDelegated to {spec['model_label']} via OpenCode: {rounds} round(s), "
           f"{autofix} automatic test-fix, {st.get('reviews', 0)} review round(s)."
           f"\n\nWorkbench-Task: {a.name}\nOpenCode-Session: {st.get('session')}")
    try:
        commit = gitops.commit_all(task.worktree, msg)
        gitops.remove_worktree(repo, task.worktree)
    except gitops.GitError as e:
        die(str(e))
    merged = None
    if a.merge:
        try:
            merged = gitops.merge(repo, a.name)
        except gitops.GitError as e:
            print(f"wb: not merged: {e}", file=sys.stderr)
    task.set_status(phase="accepted", last=f"accepted as {commit}" +
                    (f", merged {merged}" if merged else ""))
    results.record(task, "accepted", commit=commit, merged=bool(merged))
    _ack_all(task)
    note = claude_md_status(repo, spec["branch"])
    print(f"{note}: keep it current (update and commit it if this change alters what it says)"
          if note != "no CLAUDE.md" else "the repo has no CLAUDE.md: write one (wb init)")
    print(f"accepted {a.name}: commit {commit} on {spec['branch']}" +
          (f", merged into {gitops.git(repo, 'branch', '--show-current').strip()} as {merged}"
           if merged else f"\nmerge with: git -C {repo} merge --no-ff {spec['branch']}"))


def cmd_discard(a):
    task = need(a.name)
    spec = task.spec
    _stop_runner(task)
    repo = Path(spec["repo"])
    gitops.remove_worktree(repo, task.worktree, force=True)
    if not a.keep_branch:
        gitops.delete_branch(repo, a.name)
    task.set_status(phase="discarded", last=a.reason or "discarded", pid=None)
    results.record(task, "discarded", reason=a.reason)
    _ack_all(task)
    print(f"discarded {a.name}" + ("" if a.keep_branch else f"; branch {spec['branch']} deleted"))


def cmd_models(a):
    """What OpenCode offers, whether each can run, and who the router would pick per size."""
    found = models.discover()
    if not found:
        die(f"no local models in {models.opencode_config_path()}")
    for m in found.values():
        st = models.status(m)
        print(f"{m.id:34} {('on GPU' if st['resident'] else 'ready' if st['available'] else 'no'):6}"
              f"  {m.label}" + ("" if st["available"] else f"  ({st['why']})"))
    for size in router.SIZES:
        print(f"\n{size}:")
        for i, sc in enumerate(router.rank(size)):
            mark = "→" if i == 0 and sc.state["available"] else " "
            print(f" {mark} {sc.model.id:34} {sc.line()}")
        for m in found.values():
            if not router.allowed(m, size):
                print(f"   {m.id:34} not for {size} tasks (wb model sizes)")
    if not store.read_jsonl(store.home() / "bench.jsonl")[0]:
        print("\nno measurements yet: `wb bench` runs the suite on every model")
    if a.set_default:
        top, why = router.choose("significant")
        if top is None or not top.tries:
            die("no measured model to make the default yet: run `wb bench` first")
        path = models.opencode_config_path()
        cfg = json.loads(path.read_text())
        old = cfg.get("model")
        cfg["model"] = top.model.id
        store.write_json(path, cfg)
        print(f"\nOpenCode's default model: {old} → {top.model.id} ({why})")


def cmd_bench(a):
    """Measure models: probes, then the graded task suite. Long: run it in the background."""
    targets = [models.resolve(x) for x in a.models] if a.models else \
        [m for m in models.discover().values() if models.status(m)["available"]]
    tasks = bench.suite()
    if a.tasks:
        unknown = set(a.tasks) - set(tasks)
        if unknown:
            die(f"unknown bench task(s): {', '.join(sorted(unknown))}; known: {', '.join(tasks)}")
        tasks = {k: v for k, v in tasks.items() if k in a.tasks}
    if a.report:
        print(bench.table())
        return
    say = lambda msg: print(f"  {msg}", flush=True)  # noqa: E731
    pidfile = store.home() / "bench.pid"  # so `wb stop --all` can stop the run itself
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))
    try:
        _bench(a, targets, tasks, say)
    finally:
        if pidfile.exists() and pidfile.read_text() == str(os.getpid()):
            pidfile.unlink()


def _bench(a, targets, tasks, say):
    for m in targets:
        print(f"== {m.id}", flush=True)
        if not a.no_probe:
            p = bench.probe(m, say=say)
            print(f"probe: load {p['load_s']}s · prefill {p['prefill_tps']} tok/s · decode "
                  f"{p['decode_tps']} tok/s · tool calls {p['tools_ok']}/{p['tools_n']}", flush=True)
        for _ in range(a.repeat):
            for name, task in tasks.items():
                r = bench.run_task(m, name, task, say=say)
                print(f"{name:16} {'PASS' if r['passed'] else 'fail'}  {r['minutes']:5.1f} min  "
                      f"rounds {r['rounds']}  visible {'ok' if r['visible_ok'] else 'FAIL'}  "
                      f"{'' if r['passed'] else r['hidden']}", flush=True)
    print()
    print(bench.table())


def cmd_stats(a):
    print(results.table())


SCOUT_EVERY_DAYS = 7


def scout_state() -> dict:
    return store.read_json(store.home() / "scout.json", {}) or {}


def cmd_scout(a):
    """The weekly model check's facts: this machine, what is installed, how each model does.
    Claude reads it, looks up what is newest and best for this hardware, and recommends."""
    path = store.home() / "scout.json"
    st = scout_state()
    if a.done:
        store.write_json(path, {**st, "done_at": store.now(), "notes": a.notes or ""})
        print(f"model check recorded; the next one is due in {SCOUT_EVERY_DAYS} days")
        return
    watch = store.home() / "watch.json"
    items = store.read_json(watch, []) or []
    if a.watch:
        store.write_json(watch, items + [{"ts": store.now(), "text": a.watch}])
        print("added to the watch list")
        return
    if a.unwatch:
        if not 1 <= a.unwatch <= len(items):
            die(f"the watch list has {len(items)} item(s)")
        store.write_json(watch, items[:a.unwatch - 1] + items[a.unwatch:])
        print("removed from the watch list")
        return
    if a.due:
        due = store.now() - st.get("done_at", 0) > SCOUT_EVERY_DAYS * 86400
        print("due" if due else "not due")
        sys.exit(0 if due else 1)
    when = st.get("done_at")
    print("last model check: " + (f"{time.strftime('%Y-%m-%d', time.localtime(when))}, "
                                  f"{(store.now() - when) / 86400:.0f} days ago"
                                  + (f": {st['notes']}" if st.get("notes") else "") if when else "never"))
    if items:
        print("\nwatch list: what would reopen a past decision; check each against what is new")
        for i, r in enumerate(items, 1):
            print(f"  {i}. ({time.strftime('%Y-%m-%d', time.localtime(r['ts']))}) {r['text']}")
    print("\nhardware")
    print("\n".join(hardware.report()))
    provs = models.providers()
    registered = models.discover()
    print("\nmodels OpenCode can use (opencode.json)")
    real = defaultdict(list)
    for r in store.read_jsonl(store.home() / "results.jsonl")[0]:
        real[r.get("model")].append(r)
    for pid, prov in provs.items():
        installed = {x["name"]: x for x in models.ollama_installed(prov["root"])} \
            if prov["engine"] == "ollama" else {}
        for name in prov["models"]:
            m = registered.get(f"{pid}/{name}")
            disk = installed.get(name) or installed.get(f"{name}:latest") or {}
            facts = " · ".join(x for x in (
                f"{hardware.size(disk.get('size'))} on disk" if disk.get("size") else "",
                disk.get("params") or "", disk.get("quant") or "",
                f"{(m.config.get('limit') or {}).get('context', 0) // 1024}K context" if m else "") if x)
            rs = real.get(f"{pid}/{name}", [])
            used = (f"{len(rs)} real task(s), {sum(r['outcome'] == 'accepted' for r in rs)} accepted, "
                    f"last {time.strftime('%Y-%m-%d', time.localtime(max(r['ts'] for r in rs)))}"
                    if rs else "no real tasks yet")
            print(f"  {pid}/{name}: {facts or prov['engine']} · {used}")
        extra = [x for x in installed if x not in prov["models"] and x.removesuffix(":latest")
                 not in prov["models"]]
        if extra:
            print(f"  installed in {pid} but not registered: " + ", ".join(
                f"{x} ({hardware.size(installed[x]['size'])})" for x in extra))
    cfg = json.loads(models.opencode_config_path().read_text())
    print(f"  OpenCode's default: {cfg.get('model') or '(none)'}")
    print("\nmeasured (wb bench)")
    print(bench.table())
    print("\nnext: compare with what is newest and best for this hardware, recommend to the user, "
          "and when done: wb scout --done --notes \"…\"")


def cmd_model(a):
    """Add a model to OpenCode's config (pulling it first with --pull), or retire one."""
    if a.action == "sizes":
        m = models.resolve(a.id)
        bad = set(a.sizes or ()) - set(router.SIZES)
        if bad:
            die(f"sizes are {', '.join(router.SIZES)}")
        models.set_override(m.id, "sizes", sorted(set(a.sizes)) if a.sizes else None)
        print(f"{m.id}: routed " + (" and ".join(sorted(set(a.sizes))) + " tasks only"
                                    if a.sizes else "tasks of every size, as measured"))
        return
    provider, _, name = a.id.partition("/")
    provs = models.providers()
    if provider not in provs or not name:
        die(f"name it provider/model, with one of OpenCode's local providers: {', '.join(provs)}")
    prov = provs[provider]
    if a.action == "add":
        if a.pull:
            if prov["engine"] != "ollama":
                die(f"{provider} is not an Ollama provider; install the model the server's own way")
            models.ollama_pull(prov["root"], name, say=lambda m: print(m, flush=True))
        facts = models.ollama_show(prov["root"], name) if prov["engine"] == "ollama" else {}
        caps = facts.get("capabilities") or []
        # the context the server actually serves: what this provider's other models are set to,
        # else what the model was trained for
        siblings = [(e.get("limit") or {}).get("context") for e in prov["models"].values()]
        ctx = a.context or next((c for c in siblings if c), None) or facts.get("context_length")
        entry = {"name": a.label or name,
                 "tool_call": "tools" in caps if caps else True,
                 "reasoning": "thinking" in caps if caps else True}
        if ctx:
            out = min(4096, ctx // 8)
            entry["limit"] = {"context": ctx, "input": ctx - out, "output": out}
        if prov["engine"] == "ollama":
            entry["options"] = {"max_tokens": models.ollama_cap(entry)}
        models.write_entry(provider, name, entry)
        print(f"registered {a.id}: {'tools' if entry['tool_call'] else 'NO tool calls'}, "
              f"{'reasoning' if entry['reasoning'] else 'no reasoning'}, "
              f"{(ctx or 0) // 1024}K context\nnext: wb bench {a.id}  (the router ignores it "
              "until it is measured)")
        return
    # retire
    cfg = json.loads(models.opencode_config_path().read_text())
    if cfg.get("model") == a.id:
        die(f"{a.id} is OpenCode's default model; wb models --set-default picks another first")
    if name not in prov["models"]:
        die(f"{a.id} is not registered")
    models.write_entry(provider, name, None)
    done = f"retired {a.id}: removed from OpenCode's config (its measurements stay in the history)"
    if a.delete:
        if prov["engine"] != "ollama":
            done += f"; {provider} is not Ollama, so delete its weights the server's own way"
        else:
            models.ollama_delete(prov["root"], name)
            done += "; its weights were deleted from Ollama"
    print(done)


def claude_md_status(repo: Path, ref: str = "HEAD") -> str:
    """One line on the repo's CLAUDE.md at ref: missing, or how far behind the code it may be."""
    if not _has_file(repo, ref, "CLAUDE.md"):
        return "no CLAUDE.md"
    last = gitops.git(repo, "log", "-1", "--format=%h", ref, "--", "CLAUDE.md").strip()
    since = int(gitops.git(repo, "rev-list", "--count", f"{last}..{ref}", "--", ".",
                           ":!CLAUDE.md", ":!AGENTS.md").strip() or 0)  # commits that changed code
    return (f"CLAUDE.md is current with {ref}" if not since else
            f"CLAUDE.md last changed {since} commit(s) before {ref} ({last})")


def cmd_init(a):
    """Set a project up as Claude keeps every project: a git repo on master with an initial
    commit, AGENTS.md a symlink to CLAUDE.md. Safe to run again; no remote is ever added."""
    d = Path(a.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    inside = gitops.git(d, "rev-parse", "--show-toplevel", check=False).strip()
    if inside and Path(inside) != d:
        die(f"{d} is inside the git repo {inside}; init the project in its own directory")
    if not gitops.git(d, "config", "user.email", check=False).strip():
        die("git has no user.email: the user's identity signs the commits (git config --global)")
    did = []
    if not inside:
        gitops.git(d, "init", "-q", "-b", "master")
        did.append("git repo on master")
    claude, agents = d / "CLAUDE.md", d / "AGENTS.md"
    if agents.is_file() and not agents.is_symlink() and not claude.exists():
        agents.rename(claude)  # the project's instructions live in CLAUDE.md, AGENTS.md points at it
        did.append("AGENTS.md moved to CLAUDE.md")
    if claude.is_file() and agents.is_file() and not agents.is_symlink():
        if agents.read_text() == claude.read_text():
            agents.unlink()
        else:
            print("AGENTS.md and CLAUDE.md differ: merge them into CLAUDE.md, delete AGENTS.md, "
                  "and run wb init again")
    if claude.is_file() and not agents.exists() and not agents.is_symlink():
        agents.symlink_to("CLAUDE.md")
        did.append("AGENTS.md → CLAUDE.md")
    has_commits = bool(gitops.git(d, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())
    if not has_commits:
        gitops.git(d, "add", "-A")
        gitops.git(d, "commit", "-q", "--allow-empty", "-m", "Initial commit")
        did.append("initial commit")
    else:
        touched = [p for p in ("CLAUDE.md", "AGENTS.md")
                   if gitops.git(d, "status", "--porcelain", "--", p).strip()]
        if touched and any("AGENTS.md" in x or "moved" in x for x in did):
            gitops.git(d, "add", "-A", "--", *touched)
            gitops.git(d, "commit", "-q", "-m", "chore: AGENTS.md links to CLAUDE.md", "--", *touched)
            did.append("committed")
    status = claude_md_status(d)
    print(f"{d}: " + (", ".join(did) if did else "already set up") + f"; {status}"
          + ("\nnext: write CLAUDE.md (what the project is, how to build and test it, its "
             "conventions), commit it, then wb init again to link AGENTS.md"
             if status == "no CLAUDE.md" else ""))


# What `wb events` reports: the state, not the work (worktrees, bench repos) or the noise.
EVENT_SKIP = ("worktree", "repos", "rounds")
QUIET = (".lock", ".tmp", "runner.log")


def cmd_events(a):
    """One JSON line the moment workbench's state changes: what the mod listens to instead of
    polling. The first line is {"hello": pid}; it ends when the process that started it does."""
    home = store.home()
    (home / "tasks").mkdir(parents=True, exist_ok=True)
    chat.chat_dir()
    parent = os.getppid()
    print(json.dumps({"hello": os.getpid()}), flush=True)
    with Watcher(home, recursive=True, skip=EVENT_SKIP) as w:
        while os.getppid() == parent:
            changed = {str(p.relative_to(home)) for p in w.wait(10)
                       if not p.name.endswith(QUIET) and not p.name.startswith(".")}
            if changed:
                try:
                    print(json.dumps({"changed": sorted(changed)}), flush=True)
                except BrokenPipeError:
                    return


def cmd_chat(a):
    """The web chat: the user's side door to Claude while away from the terminal."""
    if a.action == "serve":
        chat.serve(a.port, a.bind or None, say=lambda m: print(m, flush=True))
    elif a.action == "url":
        print(chat.url(a.port))  # the token is the key: keep the link private
    elif a.action == "reply":
        text = (a.text if a.text is not None else
                Path(a.message).read_text() if a.message and a.message != "-" else sys.stdin.read()).strip()
        if not text:
            die("the reply is empty")
        chat.send(text, "claude")
        print("sent to the web chat")
    elif a.action == "ack":
        chat.ack(a.id, a.by or "claude", a.how)
    elif a.action == "pending":
        for r in chat.pending():
            print(json.dumps(r))
    else:  # log
        for r in chat.thread()[-a.n:]:
            who = "you" if r["from"] == "user" else "Claude"
            print(f"{time.strftime('%H:%M', time.localtime(r['ts']))} {who}: {r['text']}")
        lst = chat.listener()
        print(f"listening: {lst['cwd']}" if lst else "no session is listening")


def cmd_doctor(a):
    ok = True

    def line(good, what):
        nonlocal ok
        ok &= good
        print(f"{'ok ' if good else 'BAD'} {what}")

    oc = subprocess.run(["opencode", "--version"], capture_output=True, text=True)
    line(oc.returncode == 0, f"opencode {oc.stdout.strip() or oc.stderr.strip()}")
    cfg = Path.home() / ".config/opencode/opencode.json"
    line(cfg.exists(), f"OpenCode config {cfg}")
    g = subprocess.run(["git", "--version"], capture_output=True, text=True)
    line(g.returncode == 0, g.stdout.strip() or "git not found")
    found = models.discover()
    line(bool(found), f"{len(found)} local model(s) in {models.opencode_config_path()}")
    for m in found.values():
        st = models.status(m)
        print(f"{'ok ' if st['available'] else '-- '} model {m.id}: {st['why']}")
    for pid, prov in models.providers().items():
        if prov["engine"] != "ollama":
            continue
        for name, e in prov["models"].items():
            line("max_tokens" in (e.get("options") or {}),
                 f"{pid}/{name} caps its output on Ollama (options.max_tokens; Ollama ignores the "
                 "max_completion_tokens OpenCode sends)")
    line(os.access(store.home().parent, os.W_OK), f"state dir {store.home()}")
    print(f"--  the Claude Code mod lives in {PKG_ROOT / 'mod'} (see README for installing it)")
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wb", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"wb {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="hand a task to a local agent")
    s.add_argument("name")
    s.add_argument("--spec", required=True, help="spec file, or - for stdin")
    s.add_argument("--repo", default=".")
    s.add_argument("--size", choices=["trivial", "significant"], default="significant")
    s.add_argument("--model", help="an OpenCode model id or a fragment of one; default: the "
                   "router's choice for --size, from benchmarks and past results")
    s.add_argument("--test", help="acceptance command, run in the worktree after every round")
    s.add_argument("--checkin", type=float, metavar="MIN",
                   help="wake Claude every MIN minutes while it runs, with a digest of progress "
                   "(default 10 for significant tasks, off for trivial; 0 turns it off)")
    s.add_argument("--auto-fix", type=int, default=2,
                   help="rounds that send failing tests back without waking Claude (default 2)")
    s.add_argument("--ask-budget", type=int, default=2)
    s.add_argument("--ask-timeout", type=int, default=1200)
    s.add_argument("--round-timeout", type=int, default=2400)
    s.add_argument("--silence-timeout", type=int, default=900)
    s.add_argument("--think-timeout", type=int, help="seconds of generating with no tool call or "
                   "word before the runner nudges the agent to act (default: from the model's "
                   "measured speed; 0 turns it off)")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--force", action="store_true", help="queue even if the model can't run now")
    s.add_argument("--bench", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--no-claude-md", action="store_true",
                   help="start even though the repo has no CLAUDE.md")
    s.set_defaults(f=cmd_start)

    s = sub.add_parser("feedback", help="send review feedback for another round")
    s.add_argument("name"); s.add_argument("--message", required=True, help="file, or - for stdin")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--fresh", action="store_true",
                   help="start the agent on a fresh session, handed the task and the work so far")
    g.add_argument("--keep-context", action="store_true",
                   help="continue the session even if it is getting full")
    s.set_defaults(f=cmd_feedback)
    s = sub.add_parser("next", help="commit the approved step and send the same agent the next one")
    s.add_argument("name"); s.add_argument("--spec", required=True)
    s.add_argument("--message", "-m", required=True, help="commit subject for the approved step")
    s.add_argument("--test", help="the next step's acceptance command (default: unchanged)")
    s.add_argument("--fresh", action="store_true", help="a fresh session instead of the same one")
    s.set_defaults(f=cmd_next)
    s = sub.add_parser("answer", help="answer the agent's ask_reviewer question")
    s.add_argument("name"); s.add_argument("--id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text"); g.add_argument("--file")
    s.set_defaults(f=cmd_answer)
    s = sub.add_parser("wait", help="block until a task needs Claude; prints its report")
    s.add_argument("name", nargs="?"); s.add_argument("--timeout", type=int, default=0)
    s.set_defaults(f=cmd_wait)
    s = sub.add_parser("watch", help="follow a task live: the conversation between Claude and "
                       "the agent, every tool call and test")
    s.add_argument("name", nargs="?", help="default: the most recently active task")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="everything in full: reasoning, outputs, the protocol")
    s.add_argument("--once", action="store_true", help="print what happened so far and exit")
    s.add_argument("--no-color", action="store_true")
    s.add_argument("--json", action="store_true", help=argparse.SUPPRESS)  # the mod's pane
    s.set_defaults(f=cmd_watch)

    s = sub.add_parser("status", help="open tasks at a glance")
    s.add_argument("name", nargs="?"); s.add_argument("--all", action="store_true")
    s.set_defaults(f=cmd_status)
    s = sub.add_parser("log", help="the agent's events")
    s.add_argument("name"); s.add_argument("-n", type=int, default=0)
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(f=cmd_log)
    s = sub.add_parser("report", help="a round's compact report")
    s.add_argument("name"); s.add_argument("--round", type=int)
    s.set_defaults(f=cmd_report)
    s = sub.add_parser("diff", help="the agent's changes against the task's base")
    s.add_argument("name"); s.add_argument("--stat", action="store_true")
    s.add_argument("--all", action="store_true", help="every step, not just the current one")
    s.add_argument("paths", nargs="*")
    s.set_defaults(f=cmd_diff)
    s = sub.add_parser("steer", help="a note to a running agent: at the end of its current run, "
                       "or --now to interrupt it")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text"); g.add_argument("--message", help="a file")
    s.add_argument("--now", action="store_true",
                   help="interrupt the agent mid-step; the note goes first, marked high priority")
    s.set_defaults(f=cmd_steer)
    s = sub.add_parser("note", help="Claude's captain's-log line in the task's transcript")
    s.add_argument("name"); s.add_argument("--text", required=True); s.set_defaults(f=cmd_note)
    s = sub.add_parser("takeover", help="stop the agent and finish the task by hand")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true", help="every open delegated task")
    s.set_defaults(f=cmd_takeover)
    s = sub.add_parser("stop", help="stop a running task (it can be continued with feedback)")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true",
                   help="everything: the benchmark and every agent (\"bail out claude\")")
    s.set_defaults(f=cmd_stop)
    s = sub.add_parser("accept", help="commit the work on its branch and close the task")
    s.add_argument("name"); s.add_argument("--message", "-m", required=True)
    s.add_argument("--merge", action="store_true"); s.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_accept)
    s = sub.add_parser("discard", help="drop the work and close the task")
    s.add_argument("name"); s.add_argument("--reason")
    s.add_argument("--keep-branch", action="store_true")
    s.set_defaults(f=cmd_discard)
    s = sub.add_parser("models", help="the local models, and which the router picks per task size")
    s.add_argument("--set-default", action="store_true",
                   help="make the router's pick for significant tasks OpenCode's default model")
    s.set_defaults(f=cmd_models)
    s = sub.add_parser("model", help="add a model to OpenCode (--pull downloads it), retire one, "
                                     "or limit the task sizes it is routed")
    s.add_argument("action", choices=["add", "retire", "sizes"])
    s.add_argument("id", help="provider/model")
    s.add_argument("sizes", nargs="*", help="sizes: the only task sizes it gets (none: all)")
    s.add_argument("--pull", action="store_true", help="add: download it into Ollama first")
    s.add_argument("--label", help="add: the name OpenCode shows")
    s.add_argument("--context", type=int, help="add: context tokens (default: what the provider's "
                                                "other models use, else the model's own)")
    s.add_argument("--delete", action="store_true", help="retire: also delete its weights")
    s.set_defaults(f=cmd_model)
    s = sub.add_parser("scout", help="the weekly model check: hardware, installed models, results")
    s.add_argument("--done", action="store_true", help="record that the check was done")
    s.add_argument("--notes", help="with --done: what was recommended or changed")
    s.add_argument("--due", action="store_true", help="exit 0 if a check is due, else 1")
    s.add_argument("--watch", metavar="TEXT", help="add what would reopen a decision (a model "
                                                   "set aside, and what it needs to return)")
    s.add_argument("--unwatch", type=int, metavar="N", help="drop watch-list item N, once settled")
    s.set_defaults(f=cmd_scout)
    s = sub.add_parser("events", help="print a line whenever workbench's state changes (the mod's ears)")
    s.set_defaults(f=cmd_events)
    s = sub.add_parser("chat", help="the web chat: serve it, its link, reply to the user there")
    s.add_argument("action", choices=["serve", "url", "reply", "log", "ack", "pending"], nargs="?",
                   default="log")
    s.add_argument("id", nargs="?", help="ack: the message id")
    s.add_argument("--port", type=int, default=chat.PORT)
    s.add_argument("--bind", action="append", help="serve: address (default: localhost and Tailscale)")
    s.add_argument("--text", help="reply: the text"); s.add_argument("--message", help="reply: file, or -")
    s.add_argument("--by", help=argparse.SUPPRESS); s.add_argument("-n", type=int, default=10)
    s.add_argument("--how", choices=["now", "queued"], default="now", help=argparse.SUPPRESS)
    s.set_defaults(f=cmd_chat)
    s = sub.add_parser("init", help="set a project up: git on master with an initial commit, "
                                    "AGENTS.md → CLAUDE.md")
    s.add_argument("dir"); s.set_defaults(f=cmd_init)
    s = sub.add_parser("bench", help="measure models: speed probes and graded coding tasks")
    s.add_argument("models", nargs="*", help="default: every model that can run")
    s.add_argument("--tasks", nargs="+", help="only these bench tasks")
    s.add_argument("--repeat", type=int, default=1, help="run the task suite N times")
    s.add_argument("--no-probe", action="store_true", help="skip the speed and tool-call probe")
    s.add_argument("--report", action="store_true", help="print the measurements, run nothing")
    s.set_defaults(f=cmd_bench)
    sub.add_parser("stats", help="results by model").set_defaults(f=cmd_stats)
    sub.add_parser("doctor", help="check the setup").set_defaults(f=cmd_doctor)

    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
