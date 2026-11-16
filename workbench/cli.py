"""wb: hand a coding task to a local model, review it, accept or discard it.

Every command prints a short, plain report: it is read by Claude, whose tokens are the scarce
resource, and by a person at a terminal.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import (__version__, chat, context, gitops, hardware, images, lessons, models, results,
               router, scan, skills, store, transcript)
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
  If you hit something the next agent would trip on too (a library's quirk, a pitfall, a check
  worth running), add a line `Lesson: <the rule, and why>` to it.
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
    if a.no_claude_md:
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
    handed = _skill_names(a.skill)
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
    limits = router.budgets(m)  # from the model's measured speed on this machine, else defaults
    store.write_json(task.dir / "task.json", {
        "name": a.name, "repo": str(repo), "base": base, "base_ref": a.base,
        "branch": gitops.branch_name(a.name), "model": m.id, "model_label": m.label,
        "opencode_model": m.id, "route": why, "size": a.size, "test_cmd": a.test,
        "auto_fix": a.auto_fix, "ask_budget": budget, "ask_timeout": a.ask_timeout,
        "checkin": a.checkin if a.checkin is not None else 10 if a.size == "significant" else 0,
        "round_timeout": a.round_timeout or limits["round"],
        "silence_timeout": a.silence_timeout or limits["silence"],
        "think_timeout": a.think_timeout if a.think_timeout is not None else limits["think"],
        "created_at": store.now(), "wb_version": __version__})
    test_line = (f"- The acceptance command is `{a.test}`. Run it before you finish and make it "
                 "pass. Never weaken, skip or delete tests to get there.\n") if a.test else ""
    prompt = spec_text.rstrip() + PROTOCOL.format(branch=gitops.branch_name(a.name),
                                                  test_line=test_line, budget=budget)
    task.round_dir(1).mkdir(parents=True)
    task.round_dir(1).joinpath("prompt.md").write_text(prompt)
    task.set_status(phase="queued", round=1, reviews=0, auto_fixes=0, tools=0,
                    tok_in=0, tok_out=0, session=None, last="starting",
                    skills=handed, skills_ever=handed, skills_rev=skills.revs(handed))
    spawn_runner(task)
    print(f"started {a.name} on {m.label} · branch {gitops.branch_name(a.name)} · "
          f"base {base[:8]}" + (f" · tests `{a.test}`, auto-fix {a.auto_fix}" if a.test else ""))
    if dirty:
        print("note: the repo has uncommitted changes; the agent's worktree starts from "
              f"{a.base}, without them")
    print(f"model: {why}" if why != "asked for" else f"model: {m.id} (asked for)")
    print(f"slice: {router.slice(m).line()}")
    if handed:
        print("skills handed: " + ", ".join(handed))
    elif fits := skills.suggest(spec_text):
        print(f"library skills that may fit: {', '.join(fits)} (wb skill hand {a.name} <name>)")
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
    text = a.text if a.text is not None else \
        sys.stdin.read() if a.message == "-" else Path(a.message).read_text()
    n = st.get("round", 1) + 1
    task.round_dir(n).mkdir(parents=True, exist_ok=True)
    task.round_dir(n).joinpath("prompt.md").write_text(
        f"{lessons.FEEDBACK_HEAD} Make exactly these changes, then re-run the "
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
    _say(lessons.nudge(task))


def cmd_next(a):
    """Commit the approved step on the task's branch and give the same agent the next one: same
    worktree, same session (so it keeps what it has read), unless it is full or --fresh."""
    task = need(a.name)
    spec, st = task.spec, task.status
    if st.get("phase") != "review":
        die(f"{a.name} is {st.get('phase')}; wb next follows an approved step under review")
    handed = _skill_names(a.skill) if a.skill else None
    if runner_alive(task):
        die(f"{a.name}'s runner is still running")
    step = st.get("steps", 1)
    measured = results.step(task, landed=True)
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
    if handed is not None:
        task.set_status(skills=handed, skills_ever=_union(st.get("skills_ever"), handed),
                        skills_rev={**(st.get("skills_rev") or {}), **skills.revs(handed)})
    task.set_status(round=n, steps=step + 1, step_round=n, phase="queued", reviews=0,
                    step_sizes=(st.get("step_sizes") or []) + [measured],
                    context="fresh" if a.fresh else None,
                    auto_fixes_total=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                    auto_fixes=0, last=f"step {step} committed; next step queued")
    spawn_runner(task)
    _ack_all(task)
    print(f"step {step} of {a.name} committed as {commit}; step {step + 1} sent "
          + ("to a fresh session" if a.fresh else "to the same session"))
    _say(lessons.nudge(task))


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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_everything() -> list[str]:
    """"bail out claude": every agent. Nothing is finished or thrown away: tasks keep their
    worktrees (stopped, for wb feedback or wb discard later)."""
    stopped = []
    for t in _open_tasks():
        _stop_runner(t)
        t.event({"ts": store.now(), "round": t.status.get("round"), "kind": "stopped",
                 "text": "bail out: stopped by the user"})
        t.set_status(phase="stuck", last="bail out: stopped", pid=None)
        _ack_all(t)
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
        tasks = _open_tasks()
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
    measured = results.step(task, landed=True)
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
    results.record(task, "accepted", measured, commit=commit, merged=bool(merged))
    _ack_all(task)
    note = claude_md_status(repo, spec["branch"])
    print(f"{note}: keep it current (update and commit it if this change alters what it says)"
          if note != "no CLAUDE.md" else "the repo has no CLAUDE.md: write one (wb init)")
    print(f"accepted {a.name}: commit {commit} on {spec['branch']}" +
          (f", merged into {gitops.git(repo, 'branch', '--show-current').strip()} as {merged}"
           if merged else f"\nmerge with: git -C {repo} merge --no-ff {spec['branch']}"))
    _say(lessons.nudge(task, "accepted"))


def cmd_discard(a):
    task = need(a.name)
    spec = task.spec
    _stop_runner(task)
    repo = Path(spec["repo"])
    measured = results.step(task, landed=False)
    gitops.remove_worktree(repo, task.worktree, force=True)
    if not a.keep_branch:
        gitops.delete_branch(repo, a.name)
    task.set_status(phase="discarded", last=a.reason or "discarded", pid=None)
    results.record(task, "discarded", measured, reason=a.reason)
    _ack_all(task)
    print(f"discarded {a.name}" + ("" if a.keep_branch else f"; branch {spec['branch']} deleted"))
    _say(lessons.nudge(task, "discarded"))


def cmd_models(a):
    """What OpenCode offers, whether each can run, and who the router would pick per size."""
    found = models.discover()
    if not found:
        die(f"no local models in {models.opencode_config_path()}")
    for m in found.values():
        st = models.status(m)
        print(f"{m.id:34} {('on GPU' if st['resident'] else 'ready' if st['available'] else 'no'):6}"
              f"  {m.label}" + ("" if st["available"] else f"  ({st['why']})"))
        print(f"{'':36}slice: {router.slice(m).line()}")
        if sp := router.speed_line(m.id):
            print(f"{'':36}speed: {sp} on this machine")
    for size in router.SIZES:
        print(f"\n{size}:")
        for i, sc in enumerate(router.rank(size)):
            mark = "→" if i == 0 and sc.state["available"] else " "
            print(f" {mark} {sc.model.id:34} {sc.line()}")
        for m in found.values():
            if not router.allowed(m, size):
                print(f"   {m.id:34} not for {size} tasks (wb model sizes)")
    if a.set_default:
        top, why = router.choose("significant")
        if top is None:
            die(why)
        cfg = models.read_config()
        old = cfg.get("model")
        cfg["model"] = top.model.id
        try:
            models.write_config(cfg)
        except RuntimeError as e:
            die(str(e))
        print(f"\nOpenCode's default model: {old} → {top.model.id} ({why})")


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
    have = skills.available()
    use = skills.usage(skills.revs(list(have)))
    flagged = [(n, skills.advice(use.get(n), store.now())) for n in have]
    flagged = [(n, w) for n, w in flagged if w and w != "never offered yet"]
    print(f"\nskill library: {len(have)} skill(s)" + (", all pulling their weight" if have and not flagged else ""))
    for n, w in flagged:
        print(f"  {n}: {w}")
    pending = lessons.survey()
    if pending:
        print(f"lessons waiting: {len(pending)}, {sum(bool(x['again']) for x in pending)} seen in "
              "more than one task (wb lessons)")
    print("\nhardware")
    print("\n".join(hardware.report()))
    print("\nimage engines (wb image)")
    print("\n".join(image_lines()) or f"  none: describe one in {images.config_path()}")
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
            served = models.ollama_served_context(prov["root"], name) if prov["engine"] == "ollama" else None
            facts = " · ".join(x for x in (
                f"{hardware.size(disk.get('size'))} on disk" if disk.get("size") else "",
                disk.get("params") or "", disk.get("quant") or "",
                f"{(m.config.get('limit') or {}).get('context', 0) // 1024}K context" if m else "",
                f"Ollama serves {served // 1024}K" if served else "") if x)
            rs = real.get(f"{pid}/{name}", [])
            used = (f"{len(rs)} real task(s), {sum(r['outcome'] == 'accepted' for r in rs)} accepted, "
                    f"last {time.strftime('%Y-%m-%d', time.localtime(max(r['ts'] for r in rs)))}"
                    if rs else "no real tasks yet")
            print(f"  {pid}/{name}: {facts or prov['engine']} · {used}"
                  + (f"\n    slice: {router.slice(m).line()}" if m else ""))
        extra = [x for x in installed if x not in prov["models"] and x.removesuffix(":latest")
                 not in prov["models"]]
        if extra:
            print(f"  installed in {pid} but not registered: " + ", ".join(
                f"{x} ({hardware.size(installed[x]['size'])})" for x in extra))
    cfg = models.read_config()
    print(f"  OpenCode's default: {cfg.get('model') or '(none)'}")
    print("\nreal results (wb stats)")
    print(results.table())
    print("\nnext: compare with what is newest and best for this hardware, recommend to the user, "
          "and when done: wb scout --done --notes \"…\"")


def _skill_names(names) -> list[str]:
    """Library skills named on the command line (repeatable, or comma-separated), checked."""
    out = [x for n in names or [] for x in n.split(",") if x]
    try:
        skills.resolve(out)
    except KeyError as e:
        die(f"no skill {e.args[0]} in the library (wb skill list)")
    return list(dict.fromkeys(out))


def _union(a, b) -> list[str]:
    return list(dict.fromkeys([*(a or []), *(b or [])]))


def _say(line: str) -> None:
    if line:
        print(line)


def _closed_by(ids, name: str) -> str:
    """Close the lessons a skill change took in; the line saying so."""
    done = lessons.close([x for i in ids or [] for x in i.split(",") if x], f"skill:{name}")
    return f"\nlessons closed: {', '.join(done)}" if done else ""


def cmd_skill(a):
    """Claude's skill library for the agents: write, hand out, take back, retire."""
    if a.action == "list":
        have, now = skills.available(), store.now()
        use = skills.usage(skills.revs(list(have)))
        if not have:
            print(f"the skill library is empty ({skills.library()}); "
                  "wb skill add <name> --from <file> -m \"why\" starts it")
            return
        for name, info in have.items():
            note = skills.advice(use.get(name), now)
            print(f"{name}: {info['description']}\n  {skills.record_line(use.get(name))}"
                  + (f" · {note}" if note else ""))
        return
    if a.action == "show":
        have = skills.available()
        if a.name not in have:
            die(f"no skill {a.name} in the library")
        print((have[a.name]["path"] / "SKILL.md").read_text().rstrip())
        print("\nhistory:\n" + "\n".join("  " + h for h in skills.history(a.name)))
        return
    if a.action == "add":
        personal = set(context.denied_skills())
        if a.name in personal:
            die(f"{a.name} is the name of one of the user's own skills; pick another")
        try:
            verb, warns = skills.add(Path(a.source), a.name, a.message, a.origin or "")
        except ValueError as e:
            die(f"not added: {e}")
        print(f"{verb} {a.name}" + "".join(f"\nwarning: {w}" for w in warns)
              + _closed_by(a.lesson, a.name))
        return
    if a.action == "patch":
        old = Path(a.old_file).read_text() if a.old_file else a.old
        new = Path(a.new_file).read_text() if a.new_file else a.new
        if not old or new is None:
            die("say what to replace: --old and --new (or --old-file and --new-file)")
        try:
            warns = skills.patch(a.name, old, new, a.message)
        except KeyError:
            die(f"no skill {a.name} in the library")
        except ValueError as e:
            die(f"not patched: {e}")
        print(f"patched {a.name} ({skills.rev(a.name)}); tasks from now on are judged on this version"
              + "".join(f"\nwarning: {w}" for w in warns) + _closed_by(a.lesson, a.name))
        return
    if a.action == "scan":
        target = skills.available().get(a.name, {}).get("path") or Path(a.name)
        if not target.exists():
            die(f"no skill or path {a.name}")
        found = scan.scan(target)
        print(f"{scan.verdict(found)}: {target}" + "".join(f"\n  {x}" for x in scan.lines(found)))
        return
    if a.action == "import":
        cmd_skill_import(a)
        return
    if a.action == "retire":
        try:
            skills.retire(a.name, a.message)
        except KeyError:
            die(f"no skill {a.name} in the library")
        print(f"retired {a.name} (wb skill restore {a.name} brings it back)")
        return
    if a.action == "restore":
        try:
            skills.restore(a.name)
        except KeyError:
            die(f"no retired skill {a.name}")
        print(f"restored {a.name}")
        return
    # hand / take: change what one task's agent holds, from its next run on
    task = need(a.task)
    names = _skill_names(a.names) if a.action == "hand" else \
        [x for n in a.names for x in n.split(",") if x]
    st = task.status
    held = st.get("skills") or []
    if a.action == "hand":
        held = _union(held, names)
        task.set_status(skills=held, skills_ever=_union(st.get("skills_ever"), names),
                        skills_rev={**(st.get("skills_rev") or {}), **skills.revs(names)})
    else:
        held = [x for x in held if x not in names]
        task.set_status(skills=held)
    when = "from its next run" + (" (`wb steer --now` starts that run now)" if runner_alive(task)
                                  else "")
    print(f"{task.name} holds: {', '.join(held) or 'no skills'}; the agent is told {when}")


def cmd_skill_import(a):
    """Fetch a skill from outside (a git URL, owner/repo on GitHub, or a path) for Claude to read.
    Nothing enters the library here: a dangerous one is refused and removed; anything else
    waits in the state directory until Claude has read it, made it ours and added it."""
    try:
        root, meta, found = skills.fetch(a.src, a.path)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        die(str(e))
    if not found:
        die(f"no SKILL.md {'at ' + a.path if a.path else 'anywhere'} in {meta['source']}")
    if len(found) > 1:
        print(f"{len(found)} skills in {meta['source']}; pick one with --path:")
        for d in found[:40]:
            print(f"  {d.relative_to(root)}: {skills.parse(d / 'SKILL.md')['description'][:120]}")
        return
    d = found[0]
    findings = scan.scan(d)
    verdict = scan.verdict(findings)
    origin = f"{meta['source']}" + (f"@{meta['rev'][:12]}" if meta["rev"] else "") + \
        (f" {d.relative_to(root)}" if d != root else "")
    print(f"{d.relative_to(root) if d != root else d.name} from {origin}\n"
          f"license: {skills.license_of(d, root)}\nsha256: {scan.content_hash(d)}\nscan: {verdict}"
          + "".join(f"\n  {x}" for x in scan.lines(findings)))
    if verdict == "dangerous":
        if root.is_relative_to(store.home() / "imports"):
            shutil.rmtree(root, ignore_errors=True)
        die("refused: don't take anything from this skill")
    problems, warns = skills.check(d / "SKILL.md", a.name or d.name)
    for w in problems + warns:
        print(f"  lint: {w}")
    print(f"\nread it all: {d}\nthen make it ours (our format, our voice, only what fits the "
          "agents' work), write it to the scratchpad, and:\n"
          f"  wb skill add {a.name or d.name} --from <dir> -m \"why\" --source \"{origin}, "
          f"{skills.license_of(d, root)}\"")


def cmd_lessons(a):
    """What tasks taught, waiting for Claude: write or patch a skill, or close it with why."""
    if a.action == "close":
        done = lessons.close([x for i in a.ids for x in i.split(",") if x], "dropped", a.message)
        print(f"closed: {', '.join(done)}" if done else "none of those is open")
        return
    pending = [x for x in lessons.survey() if not a.task or x["task"] == a.task]
    if not pending:
        print("no open lessons")
        return
    shown = pending if a.all else pending[:12]
    again = sum(bool(x["again"]) for x in pending)
    print(f"open lessons: {len(pending)}" + (f", {again} seen in more than one task" if again else ""))
    for x in shown:
        print("  " + lessons.line(x))
    if len(shown) < len(pending):
        print(f"  … and {len(pending) - len(shown)} more (--all)")
    print("next: a lesson in a skill: wb skill patch|add … --lesson <id>; not one (a one-task "
          "detail, the environment, a dead end): wb lessons close <id> -m why")


def image_lines() -> list[str]:
    out = []
    for name, e in images.engines().items():
        why = images.ready(e)
        out.append(f"  {name}: {e.get('label') or e.get('command', e.get('url', ''))[:60]}"
                   f" · {'ready' if not why else why}"
                   + (f"\n    {e['notes']}" if e.get("notes") else ""))
    return out


def cmd_image(a):
    """Local image generation: Claude writes the prompt, looks at the candidates, keeps one."""
    if a.list or not a.prompt:
        print("\n".join(image_lines()) or f"no image engine: describe one in {images.config_path()}")
        return
    try:
        w, h = (int(x) for x in a.size.lower().split("x"))
    except ValueError:
        die(f"--size {a.size!r}: WIDTHxHEIGHT, e.g. 1024x1024")
    out = Path(a.out) if a.out else store.home() / "images" / time.strftime("%Y%m%d-%H%M%S")
    try:
        made = images.generate(a.prompt, out, n=a.n, size=(w, h), seed=a.seed, name=a.engine,
                               refs=a.ref or [], say=lambda m: print(m, flush=True))
    except (KeyError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        die(str(e).strip("'\""))
    print(f"{len(made)} image(s) in {out}: look at each before choosing; the .json beside each "
          "holds its prompt and seed")


def cmd_model(a):
    """Add a model to OpenCode's config (pulling it first with --pull), or retire one."""
    if a.action == "sizes":
        m = models.resolve(a.id)
        bad = set(a.sizes or ()) - set(router.SIZES)
        if bad:
            die(f"sizes are {', '.join(router.SIZES)}")
        models.set_override(m.id, "sizes", sorted(set(a.sizes)) if a.sizes else None)
        print(f"{m.id}: routed " + (" and ".join(sorted(set(a.sizes))) + " tasks only"
                                    if a.sizes else "tasks of every size"))
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
        # the context the server actually serves: what Ollama says it gives this model, else what
        # this provider's other models are set to, else what the model was trained for
        served = models.ollama_served_context(prov["root"], name) if prov["engine"] == "ollama" else None
        siblings = [(e.get("limit") or {}).get("context") for e in prov["models"].values()]
        ctx = a.context or served or next((c for c in siblings if c), None) or facts.get("context_length")
        entry = {"name": a.label or name,
                 "tool_call": "tools" in caps if caps else True,
                 "reasoning": "thinking" in caps if caps else True}
        if ctx:
            out = ctx // 8  # 4K on a 32K window; a bigger window writes bigger files in one call
            entry["limit"] = {"context": ctx, "input": ctx - out, "output": out}
        if prov["engine"] == "ollama":
            entry["options"] = {"max_tokens": models.ollama_cap(entry)}
        try:
            models.write_entry(provider, name, entry)
        except RuntimeError as e:
            die(str(e))
        print(f"registered {a.id}: {'tools' if entry['tool_call'] else 'NO tool calls'}, "
              f"{'reasoning' if entry['reasoning'] else 'no reasoning'}, "
              f"{(ctx or 0) // 1024}K context\nnext: wb model sizes {a.id} … if it should only "
              "take some task sizes; the router guesses from its size until real tasks show more")
        return
    # retire
    cfg = models.read_config()
    if cfg.get("model") == a.id:
        die(f"{a.id} is OpenCode's default model; wb models --set-default picks another first")
    if name not in prov["models"]:
        die(f"{a.id} is not registered")
    try:
        models.write_entry(provider, name, None)
    except RuntimeError as e:
        die(str(e))
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
    """Set a project up as Claude keeps every project: a git repo (on git's init.defaultBranch)
    with an initial commit, AGENTS.md a symlink to CLAUDE.md. Safe to run again; no remote is ever added."""
    d = Path(a.dir).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    inside = gitops.git(d, "rev-parse", "--show-toplevel", check=False).strip()
    if inside and Path(inside) != d:
        die(f"{d} is inside the git repo {inside}; init the project in its own directory")
    if not gitops.git(d, "config", "user.email", check=False).strip():
        die("git has no user.email: the user's identity signs the commits (git config --global)")
    did = []
    if not inside:
        gitops.git(d, "init", "-q")  # on the user's init.defaultBranch
        did.append("git repo on " + (gitops.git(d, "symbolic-ref", "--short", "HEAD",
                                                check=False).strip() or "its default branch"))
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


# What `wb events` reports: the state, not the work (worktrees) or the noise.
EVENT_SKIP = ("worktree", "rounds")
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
    cfg = models.opencode_config_path()
    try:
        models.read_config()
        line(cfg.exists(), f"OpenCode config {cfg}")
    except ValueError as e:
        line(False, f"OpenCode config {cfg} doesn't parse: {e}")
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
            said, served = (e.get("limit") or {}).get("context"), models.ollama_served_context(prov["root"], name)
            if said and served and said > served:
                line(False, f"{pid}/{name} says a {said // 1024}K context but Ollama serves "
                     f"{served // 1024}K: it would cut prompts off silently (raise OLLAMA_CONTEXT_LENGTH, "
                     f"or wb model add {pid}/{name} --context {served})")
            elif said and served and served > said:
                print(f"--  {pid}/{name}: Ollama serves {served // 1024}K, the entry says {said // 1024}K; "
                      f"wb model add {pid}/{name} --context {served} lets its tasks grow")
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
                   "router's choice for --size: its guess from model size, corrected by past results")
    s.add_argument("--test", help="acceptance command, run in the worktree after every round")
    s.add_argument("--checkin", type=float, metavar="MIN",
                   help="wake Claude every MIN minutes while it runs, with a digest of progress "
                   "(default 10 for significant tasks, off for trivial; 0 turns it off)")
    s.add_argument("--auto-fix", type=int, default=2,
                   help="rounds that send failing tests back without waking Claude (default 2)")
    s.add_argument("--ask-budget", type=int, default=2)
    s.add_argument("--ask-timeout", type=int, default=1200)
    s.add_argument("--round-timeout", type=int, help="seconds a round may run (default 2400, "
                   "longer for a model measured slow on this machine)")
    s.add_argument("--silence-timeout", type=int, help="seconds with no output before the round "
                   "is stopped (default 900, longer for a model measured slow)")
    s.add_argument("--think-timeout", type=int, help="seconds of generating with no tool call or "
                   "word before the runner nudges the agent to act (default 420, or what the "
                   "model's measured speed gives; 0 turns it off)")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--force", action="store_true", help="queue even if the model can't run now")
    s.add_argument("--no-claude-md", action="store_true",
                   help="start even though the repo has no CLAUDE.md")
    s.add_argument("--skill", action="append", metavar="NAME",
                   help="hand the agent this library skill (repeatable; wb skill list)")
    s.set_defaults(f=cmd_start)

    s = sub.add_parser("feedback", help="send review feedback for another round")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text"); g.add_argument("--message", help="a file, or - for stdin")
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
    s.add_argument("--skill", action="append", metavar="NAME",
                   help="the skills for the next step (repeatable; replaces the set; default: unchanged)")
    s.set_defaults(f=cmd_next)

    s = sub.add_parser("skill", help="Claude's skill library for the agents: list, show, add, "
                       "retire, restore; hand a task a skill or take it back")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list", help="every skill, how often it was offered, loaded and helped")
    x = ss.add_parser("show"); x.add_argument("name")
    x = ss.add_parser("add", help="add or update a skill: a SKILL.md, or a directory holding one")
    x.add_argument("name"); x.add_argument("--from", dest="source", required=True)
    x.add_argument("--message", "-m", required=True, help="why: what review taught")
    x.add_argument("--source", dest="origin", help="where an imported skill came from (url@commit, license)")
    x.add_argument("--lesson", action="append", metavar="ID", help="the lessons this takes in (wb lessons)")
    x = ss.add_parser("patch", help="fix one passage of a skill in place")
    x.add_argument("name")
    x.add_argument("--old", help="the passage to replace (it must occur exactly once)")
    x.add_argument("--new", help="what replaces it")
    x.add_argument("--old-file"); x.add_argument("--new-file")
    x.add_argument("--message", "-m", required=True, help="why: what review taught")
    x.add_argument("--lesson", action="append", metavar="ID", help="the lessons this takes in (wb lessons)")
    x = ss.add_parser("import", help="fetch a skill from outside for Claude to read: scanned, never added by itself")
    x.add_argument("src", help="a git URL, owner/repo (GitHub), or a local path")
    x.add_argument("--path", help="the skill's directory inside it")
    x.add_argument("--as", dest="name", help="the name it would take here")
    x = ss.add_parser("scan", help="the security scan, for a library skill or any path")
    x.add_argument("name")
    x = ss.add_parser("retire"); x.add_argument("name")
    x.add_argument("--message", "-m", required=True, help="why it is no longer needed")
    x = ss.add_parser("restore"); x.add_argument("name")
    for verb, what in (("hand", "give a task's agent skills"), ("take", "take skills back")):
        x = ss.add_parser(verb, help=what + ", from its next run")
        x.add_argument("task"); x.add_argument("names", nargs="+")
    s.set_defaults(f=cmd_skill)
    s = sub.add_parser("lessons", help="what tasks taught, waiting to become skills")
    s.add_argument("action", nargs="?", choices=["close"])
    s.add_argument("ids", nargs="*")
    s.add_argument("--task", help="only this task's")
    s.add_argument("--all", action="store_true")
    s.add_argument("--message", "-m", default="", help="why it isn't a skill")
    s.set_defaults(f=cmd_lessons)
    s = sub.add_parser("image", help="generate images with a local image model, for Claude to "
                       "look at and choose from")
    s.add_argument("prompt", nargs="?")
    s.add_argument("--n", type=int, default=4, help="candidates (seeds SEED, SEED+1, ...)")
    s.add_argument("--size", default="1024x1024")
    s.add_argument("--seed", type=int)
    s.add_argument("--out", help="directory (default: a new one under $WB_HOME/images)")
    s.add_argument("--engine", help="an engine from images.json (default: the first)")
    s.add_argument("--ref", action="append", help="a reference image (repeatable), for engines "
                   "that take them")
    s.add_argument("--list", action="store_true", help="the configured engines")
    s.set_defaults(f=cmd_image)
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
                   help="every agent (\"bail out claude\")")
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
    s.add_argument("--context", type=int, help="add: context tokens (default: what Ollama serves "
                   "it, else what the provider's other models use, else the model's own)")
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
    s = sub.add_parser("init", help="set a project up: git with an initial commit, "
                                    "AGENTS.md → CLAUDE.md")
    s.add_argument("dir"); s.set_defaults(f=cmd_init)
    sub.add_parser("stats", help="results by model").set_defaults(f=cmd_stats)
    sub.add_parser("doctor", help="check the setup").set_defaults(f=cmd_doctor)

    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
