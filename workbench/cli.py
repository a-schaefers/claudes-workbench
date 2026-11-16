"""wb: hand a coding task to a local model, review it, accept or discard it.

Every command prints a short, plain report: it is read by Claude, whose tokens are the scarce
resource, and by a person at a terminal.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, gitops, models, results, store
from .runner import PKG_ROOT

PROTOCOL = """

---
How to work (workbench protocol):
- You are in a git worktree on branch {branch}. Do not commit, push, or switch branches.
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


def pick_model(size: str, wanted: str | None):
    if wanted:
        key, m = models.resolve(wanted)
        return key, m, models.check(key, m)
    reg = models.registry()
    order = [k for k, m in reg.items() if m.get("tier") == size] + \
            [k for k, m in reg.items() if m.get("tier") != size]
    if size == "trivial":  # a trivial task runs on whatever is loaded rather than evict it
        held = models.on_gpu()
        order = [k for k in held if k in reg] + [k for k in order if k not in held]
    first = None
    for key in order:
        state = models.check(key, reg[key])
        if state["ready"]:
            return key, reg[key], state
        first = first or (key, reg[key], state)
    return first


# --- commands -----------------------------------------------------------------------------------

def cmd_start(a):
    if not store.NAME_RE.match(a.name):
        die("a task name is lowercase letters, digits and hyphens, at most 48")
    task = store.Task(a.name)
    if task.exists:
        die(f"task {a.name!r} exists ({task.status.get('phase')}); discard it or pick a new name")
    try:
        repo = gitops.toplevel(Path(a.repo).resolve())
    except gitops.GitError:
        die(f"{a.repo} is not inside a git repository")
    spec_text = Path(a.spec).read_text() if a.spec != "-" else sys.stdin.read()
    if not spec_text.strip():
        die("the spec is empty")
    key, m, state = pick_model(a.size, a.model)
    if not state["ready"] and not a.force:
        die(f"{key} can't run now: {state['why']}. (--force queues it anyway)", 3)
    dirty = gitops.git(repo, "status", "--porcelain").strip()

    task.dir.mkdir(parents=True)
    try:
        base = gitops.create_worktree(repo, a.name, task.worktree, a.base)
    except gitops.GitError as e:
        task.dir.rmdir()
        die(str(e))
    budget = a.ask_budget
    store.write_json(task.dir / "task.json", {
        "name": a.name, "repo": str(repo), "base": base, "base_ref": a.base,
        "branch": gitops.branch_name(a.name), "model": key, "model_label": m["label"],
        "opencode_model": m["opencode"], "size": a.size, "test_cmd": a.test,
        "auto_fix": a.auto_fix, "ask_budget": budget, "ask_timeout": a.ask_timeout,
        "round_timeout": a.round_timeout, "silence_timeout": a.silence_timeout,
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
    print(f"started {a.name} on {m['label']} · branch {gitops.branch_name(a.name)} · "
          f"base {base[:8]}" + (f" · tests `{a.test}`, auto-fix {a.auto_fix}" if a.test else ""))
    if dirty:
        print("note: the repo has uncommitted changes; the agent's worktree starts from "
              f"{a.base}, without them")
    if not state["ready"]:
        print(f"note: queued although {state['why']}")
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
    task.set_status(round=n, reviews=reviews, phase="queued",
                    auto_fixes_total=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                    auto_fixes=0)
    spawn_runner(task)
    print(f"feedback round {reviews} sent to {a.name} (round {n})"
          + ("  — that is past the usual 3 review rounds" if reviews > 3 else ""))


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
    print(f"answered {a.name}/{qid}")


def _ack_file(task):
    return task.dir / "acked"


def cmd_wait(a):
    """Block until a task (or any task) raises a notification nobody has acknowledged yet."""
    deadline = time.time() + a.timeout if a.timeout else None
    path = store.home() / "notify.jsonl"
    while True:
        rows, _ = store.read_jsonl(path)
        for row in reversed(rows):
            if a.name and row["task"] != a.name:
                continue
            task = store.Task(row["task"])
            acked = float((_ack_file(task).read_text() if _ack_file(task).exists() else "0") or 0)
            if row["ts"] > acked:
                _ack_file(task).write_text(str(row["ts"]))
                print(f"[{row['kind']}] {row['task']}\n{row['text']}")
                return
        if a.name and need(a.name).status.get("phase") in store.CLOSED:
            die(f"{a.name} is closed", 2)
        if deadline and time.time() > deadline:
            die("timed out waiting", 2)
        time.sleep(2)


def cmd_status(a):
    tasks = [need(a.name)] if a.name else [t for t in store.all_tasks()
                                           if a.all or t.status.get("phase") not in store.CLOSED]
    if not tasks:
        print("no open tasks" + ("" if a.all else " (wb status --all shows closed ones)"))
        return
    print(f"{'task':22} {'phase':9} {'rnd':>3} {'model':8} {'age':>5} {'idle':>5}  last")
    for t in tasks:
        st, sp = t.status, t.spec
        phase = st.get("phase", "?")
        if phase in store.ACTIVE and not runner_alive(t) and phase != "asking":
            phase += "?"  # the runner died without saying so
        print(f"{t.name:22} {phase:9} {st.get('round', 1):>3} {sp.get('model', ''):8} "
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
    while a.follow:
        time.sleep(1)
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
    task = need(a.name)
    print(gitops.diff(task.worktree, task.spec["base"], stat=a.stat, paths=a.paths)
          or "(no changes)")


def _stop_runner(task):
    pid = task.status.get("pid")
    if pid and runner_alive(task):
        try:
            os.kill(pid, signal.SIGTERM)  # the runner stops its agent's process group itself
        except ProcessLookupError:
            return
        for _ in range(30):
            if not runner_alive(task):
                break
            time.sleep(0.5)


def cmd_stop(a):
    task = need(a.name)
    _stop_runner(task)
    task.set_status(phase="stuck", last="stopped by the reviewer", pid=None)
    print(f"stopped {a.name}; wb feedback continues it, wb discard drops it")


def cmd_accept(a):
    task = need(a.name)
    spec, st = task.spec, task.status
    if st.get("phase") != "review" and not a.force:
        die(f"{a.name} is {st.get('phase')}, not waiting for review (--force accepts anyway)")
    repo = Path(spec["repo"])
    rounds, autofix = st.get("round", 1), st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0)
    msg = (f"{a.message}\n\nDelegated to {spec['model_label']} via OpenCode: {rounds} round(s), "
           f"{autofix} automatic test-fix, {st.get('reviews', 0)} review round(s); reviewed by "
           f"Claude.\nWorkbench-Task: {a.name}\nOpenCode-Session: {st.get('session')}\n\n"
           "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
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
    print(f"discarded {a.name}" + ("" if a.keep_branch else f"; branch {spec['branch']} deleted"))


def cmd_models(a):
    held = models.on_gpu()
    for key, m in models.registry().items():
        st = models.check(key, m)
        print(f"{key:8} {'READY' if st['ready'] else 'no':5} {m['tier']:11} {m['label']}"
              + ("  [on GPU]" if key in held else "") + ("" if st["ready"] else f"  — {st['why']}"))


def cmd_stats(a):
    print(results.table())


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
    for key, m in models.registry().items():
        st = models.check(key, m)
        print(f"{'ok ' if st['ready'] else '-- '} model {key}: {st['why']}")
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
    s.add_argument("--model", help="registry key (strata, qwen27b, mimo) or OpenCode model id")
    s.add_argument("--test", help="acceptance command, run in the worktree after every round")
    s.add_argument("--auto-fix", type=int, default=2,
                   help="rounds that send failing tests back without waking Claude (default 2)")
    s.add_argument("--ask-budget", type=int, default=2)
    s.add_argument("--ask-timeout", type=int, default=1200)
    s.add_argument("--round-timeout", type=int, default=2400)
    s.add_argument("--silence-timeout", type=int, default=900)
    s.add_argument("--base", default="HEAD")
    s.add_argument("--force", action="store_true", help="queue even if the model can't run now")
    s.set_defaults(f=cmd_start)

    s = sub.add_parser("feedback", help="send review feedback for another round")
    s.add_argument("name"); s.add_argument("--message", required=True, help="file, or - for stdin")
    s.set_defaults(f=cmd_feedback)
    s = sub.add_parser("answer", help="answer the agent's ask_reviewer question")
    s.add_argument("name"); s.add_argument("--id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text"); g.add_argument("--file")
    s.set_defaults(f=cmd_answer)
    s = sub.add_parser("wait", help="block until a task needs Claude; prints its report")
    s.add_argument("name", nargs="?"); s.add_argument("--timeout", type=int, default=0)
    s.set_defaults(f=cmd_wait)
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
    s.add_argument("paths", nargs="*")
    s.set_defaults(f=cmd_diff)
    s = sub.add_parser("stop", help="stop a running task (it can be continued with feedback)")
    s.add_argument("name"); s.set_defaults(f=cmd_stop)
    s = sub.add_parser("accept", help="commit the work on its branch and close the task")
    s.add_argument("name"); s.add_argument("--message", "-m", required=True)
    s.add_argument("--merge", action="store_true"); s.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_accept)
    s = sub.add_parser("discard", help="drop the work and close the task")
    s.add_argument("name"); s.add_argument("--reason")
    s.add_argument("--keep-branch", action="store_true")
    s.set_defaults(f=cmd_discard)
    sub.add_parser("models", help="which local models can run now").set_defaults(f=cmd_models)
    sub.add_parser("stats", help="results by model").set_defaults(f=cmd_stats)
    sub.add_parser("doctor", help="check the setup").set_defaults(f=cmd_doctor)

    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
