"""wb: hand a coding task to a local model, review it, accept or discard it.

Every command prints a short, plain report: it is read by Claude, whose tokens are the scarce
resource, and by a person at a terminal.
"""
import argparse
import fnmatch
import functools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from . import (__version__, ask_mcp, bench, chat, context, gitops, hardware, images, lane, lessons, models,
               results, router, scan, skills, spend, store, transcript, tuning)
from .watch import Watcher, wait_exit
from . import runner
from .runner import PKG_ROOT

PROTOCOL = """

---
How to work (workbench protocol):
- You are in a git worktree on branch {branch}. Git is read-only for you; leave the changes
  uncommitted. Claude commits, pushes and deploys.
{test_line}- If the spec is genuinely ambiguous, you may call the `ask_reviewer` tool ({budget} \
question(s) for this task). Otherwise make reasonable choices and say what you assumed.
- Stay inside the scope of the spec. No unrelated refactors, no new dependencies unless asked.
- You can write only in this worktree, the temp directory and build caches: "Permission denied"
  anywhere else is that limit, so don't probe it. Need a directory outside? Call `ask_reviewer`
  with `path` set to it (this doesn't use the question budget).
- CLAUDE.md and AGENTS.md are Claude's, and you can't edit them. When the task asks for a change
  to one, write the whole new file next to it as `<name>.proposed` (`CLAUDE.md.proposed`); Claude
  reads the difference and takes it.
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
    env.pop("WB_TASK", None)  # the agent's mark (runner.sweep kills what carries it), not the runner's
    task.set_status(stopping=None)  # a stop wb made earlier says nothing about this runner
    with open(task.dir / "runner.log", "a") as log:
        # -X utf8: the runner reads and writes its files as UTF-8 under any locale (the
        # 2026-11-16 audit); a flag, not PYTHONUTF8, so the agent and its tests keep their own
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "workbench.runner", task.name],
                                cwd=PKG_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=log, start_new_session=True)  # outlives this command
    task.set_status(pid=proc.pid)
    return proc.pid


PROC = Path("/proc")


def runner_alive(task: store.Task) -> bool:
    pid = task.status.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):  # gone, or a recycled pid that isn't ours
        return False
    try:  # a recycled pid of ours runs something else
        return f"workbench.runner\0{task.name}\0".encode() in (PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        if PROC.joinpath("self").exists():
            return False
    # No /proc (macOS): ask ps. Trusting any live pid let wb discard, after a reboot, SIGKILL an
    # unrelated process group that had been given the dead runner's pid (the 2026-11-16 audit).
    # -ww: BSD ps cuts the column at 79 characters, and a framework Python's path alone is longer.
    try:
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # no ps either: trust the pid
    toks = out.split()  # whole words: task "a" isn't the runner of task "ab"
    return any(x == "workbench.runner" and y == task.name for x, y in zip(toks, toks[1:]))


# A live task whose runner is gone without a word (SIGKILL, out of memory, a reboot) would sit
# in its phase forever: `wb wait` blocked, feedback and resume refused it (the 2026-11-16
# audit). After REAP_GRACE seconds with no status change (feedback, resume and next set phase
# queued just before spawn_runner writes the new pid, their slow work done first) it is failed,
# with one notice.
REAP_GRACE = 15
RUNNER_DIED = "the runner died (see runner.log)"


def reap(task: store.Task) -> bool:
    """Fail a live task whose runner is dead; True when this call did it."""
    st = task.set_status_if(  # one reaper wins: two waiters, one notice
        lambda st: st.get("phase") in LIVE and not runner_alive(task)
        and time.time() - (st.get("updated_at") or 0) >= REAP_GRACE,
        phase="failed", last=RUNNER_DIED, pid=None, agent_pid=None, asking=None)
    if st is None:
        return False
    runner.sweep(task)  # the agent it left, if the kernel didn't take it with the runner
    ask_mcp.close_all(task)  # and its questions, which nothing will relay (the 0.90 review)
    task.event({"ts": store.now(), "round": st.get("round"), "kind": "error",
                "text": "the runner died without a report"})
    task.notify("failed", f"task {task.name}: the runner died without a report (killed, out of "
                f"memory or a reboot); see {task.dir / 'runner.log'}. wb resume {task.name} "
                "continues it in a fresh session", round=st.get("round"))
    return True


def _need_worktree(task: store.Task) -> None:
    """A worktree removed by hand turned the next round into a runner crash (the 2026-11-16
    audit). Its committed steps are on the branch; discard --keep-branch keeps them."""
    if not task.worktree.is_dir():
        die(f"{task.name}'s worktree is gone ({task.worktree}): its committed work is on branch "
            f"{task.spec.get('branch')}; wb discard {task.name} --keep-branch drops the task and "
            "keeps the branch")


def _stale_notes(task: store.Task) -> list[dict]:
    """The notes the last runner never delivered (sent as its round ended, or before a stuck
    stop), marked taken: else the next runner delivered them first, and a stale --now note cut a
    feedback round short (the 2026-11-16 audit). Claude's own come back, for the caller to carry
    or report; the runner's nudges are dropped."""
    rows = runner.pending_steer(task)
    if rows:
        task.set_status(steer_taken=task.status.get("steer_taken", 0) + len(rows))
    return [r for r in rows if r.get("by") != "workbench"]


def _say_dropped(notes: list[dict]) -> None:
    for r in notes:
        print("not delivered, dropped (put it in the feedback if it still matters): "
              + r["text"].strip().replace("\n", " ")[:120])


def one_at_a_time(cmd):
    """Run a command that changes a task under the task's command lock, so two at once (two
    sessions woken by one review, the chat and the terminal) run one after the other, the second
    seeing what the first did: an accept racing a discard once deleted the accepted commit and
    wrote two results rows (the 2026-11-16 audit). The lock (runner.lock_file) is where no lane
    reads it: an agent holding it would wedge every command. The runner never takes it."""
    @functools.wraps(cmd)
    def run(a):
        name = getattr(a, "name", None)
        if getattr(a, "all", False) or not name or not store.NAME_RE.match(name) \
                or not store.Task(name).exists:
            return cmd(a)
        with store.locked(runner.lock_file(name, "cmd")):
            return cmd(a)
    return run


def ago(ts) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h"


def pick_model(size: str, wanted: str | None, stack=()) -> tuple[models.Model, dict, str]:
    """The task's model: the one asked for, else the router's choice on the evidence (the
    record on this kind of work first: router.score)."""
    if wanted:
        try:
            m = models.resolve(wanted)
        except KeyError as e:
            die(str(e).strip('"'))
        return m, models.status(m), "asked for"
    top, why = router.choose(size, stack)
    if top is None:
        die(why, 3)
    return top.model, top.state, why


def _gpu_holders(me: str | None = None) -> dict:
    """Who holds or waits for the GPU, but ``me``: gpu.json's live entries, plus each task whose
    runner is alive but hasn't reached the lease yet (one just started), as an entry of its own."""
    held = {p: h for p, h in models.holders().items() if h.get("task") != me}
    named = {h.get("task") for h in held.values()}
    found = models.discover()
    for t in store.all_tasks():
        m = found.get(t.spec.get("model") or "")
        if t.name not in named and t.name != me and m and not m.remote \
                and t.status.get("phase") in store.ACTIVE and runner_alive(t):
            held[f"task {t.name}"] = {"model": m.id, "task": t.name}
    return held


def _gpu_wait(m: models.Model, me: str | None = None) -> tuple[dict, bool]:
    """The GPU's other holders, and whether ``m`` would have to wait for them (queued) rather
    than run beside them. A remote model takes no lease: it never waits."""
    if m.remote:
        return {}, False
    held = _gpu_holders(me)
    return held, bool(held) and not models.admits(m, held)


def _preempt_bench(m: models.Model, me: str | None = None) -> str:
    """Real work first: the bench's replays that keep `m` off the GPU are stopped (the driver
    records them preempted and replays them once no real task runs). An overnight run must
    never make the morning's task queue."""
    if m.remote:
        return ""
    held, waits = _gpu_wait(m, me)
    names = sorted({h["task"] for h in held.values() if h.get("task")
                    and store.Task(h["task"]).spec.get("bench")}) if waits else []
    for name in names:
        t = store.Task(name)
        with store.locked(runner.lock_file(name, "cmd")):
            if t.status.get("phase") in store.CLOSED:
                continue
            _stop_runner(t)
            t.set_status(phase="stuck", last=bench.PREEMPTED, pid=None)
    return f"note: stopped the bench's replay ({', '.join(names)}) to make room" if names else ""


def _gpu_note(task: store.Task) -> str:
    """For wb feedback/next/resume, which queue the task anyway: who it will wait for."""
    m = models.discover().get(task.spec.get("model") or "")
    if m and not task.spec.get("bench"):
        _say(_preempt_bench(m, task.name))
    held, waits = _gpu_wait(m, task.name) if m else ({}, False)
    return (f"note: the GPU is busy with {', '.join(sorted({h['task'] for h in held.values()}))}; "
            f"{task.name} waits as queued until it's free") if waits else ""


# --- commands -----------------------------------------------------------------------------------

def _has_file(repo: Path, ref: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
                          capture_output=True).returncode == 0


def _ignored(repo: Path, path: str) -> bool:
    """Whether git ignores ``path`` in ``repo`` (a tracked file never is)."""
    return subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", path],
                          capture_output=True).returncode == 0


def _local_claude_md(repo: Path) -> bool:
    """A CLAUDE.md kept out of git on purpose: the agents get the checkout's copy
    (context.instruction_files), so it is never to be committed (the 2026-11-16 audit)."""
    return (repo / "CLAUDE.md").is_file() and _ignored(repo, "CLAUDE.md")


# Where a project's committed instructions may live, as Claude Code reads them (context.ROOT_FILES
# loads both): a repo with only .claude/CLAUDE.md was refused as having none (the 2026-11-16
# audit). CLAUDE.local.md is the user's own, rarely committed, so it doesn't count.
PROJECT_MEMORY = ("CLAUDE.md", ".claude/CLAUDE.md")


def _claude_md_at(repo: Path, ref: str) -> str | None:
    """The first of PROJECT_MEMORY committed at ``ref``, or None."""
    return next((p for p in PROJECT_MEMORY if _has_file(repo, ref, p)), None)


def _need_claude_md(repo: Path, base: str, a) -> None:
    if a.no_claude_md:
        return
    if not _claude_md_at(repo, base) and _local_claude_md(repo):
        print("note: CLAUDE.md is gitignored: the agent gets your checkout's copy")
    elif not _claude_md_at(repo, base):
        die(f"{repo} has no CLAUDE.md (or .claude/CLAUDE.md) at {base}: the agent would start "
            "blind. Write it (what the project is, how to build and test it, its conventions), "
            f"commit it, and run wb init {shlex.quote(str(repo))} to link AGENTS.md; "
            "--no-claude-md skips this check")


def _identity(repo: Path) -> bool:
    """Whether git can sign a commit in ``repo`` as someone: what `git commit` itself resolves
    (repo config, includeIf, GIT_AUTHOR_*/EMAIL), strictly. Without one a task ran for its whole
    length and only wb accept found out (the 2026-11-16 audit)."""
    return all(gitops.git(repo, "var", v, check=False).strip()
               for v in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"))


def _lane_fits(repo: Path, test: str | None, *, off: bool = False) -> None:
    """Refuse a task the agent's lane can't hold (the 2026-11-16 audit): a repo that is the home
    directory (the lane would read all of it) or lies where credentials live (it couldn't read
    the repo at all), and an acceptance command that is a snap (no-new-privileges, which the
    lane sets, stops snap-confine, so every test round would fail). `off`: `--allow-path '*'`
    turned the lane off for this task (the 0.90 review: it was refused all the same)."""
    if off or not context.lane_on():
        return
    real = os.path.realpath(repo)
    if real in ("/", os.path.realpath(Path.home())):
        die(f"{repo} is your home directory: the agent's lane would read all of it. Delegate "
            "from the project's own repository")
    if any(real == h or real.startswith(h + os.sep) for h in context.secret_paths()):
        die(f"{repo} lies where credentials live (context.SECRETS), which the agent can't read: "
            "delegate from a repository outside it")
    try:
        first = shlex.split(test)[:1] if test else []
    except ValueError:
        first = []
    if first and sys.platform.startswith("linux") and context.snaps(first):
        die(f"{first[0]} is a snap, which can't run in the agent's lane (no-new-privileges stops "
            "snap-confine): install it another way, or start with --allow-path '*' (the lane "
            'off for this task), or commands.json "paths": ["*"] for every task')


def cmd_start(a):
    if not store.NAME_RE.match(a.name):
        die("a task name is lowercase letters, digits and hyphens, at most 48")
    task = store.Task(a.name)
    if task.exists:  # discarding never frees a name, so the refusal doesn't suggest it (the audit)
        die(f"task {a.name!r} exists ({task.status.get('phase')}): task names are never reused, "
            "pick a new one")
    try:
        repo = gitops.toplevel(Path(a.repo).resolve())
    except gitops.GitError as e:  # git's own reason when it isn't "not a repo": dubious ownership
        why = str(e).split(": ", 1)[-1]  # would otherwise send Claude to git init a repo (the audit)
        if not Path(a.repo).is_dir():
            die(f"{a.repo} is not a directory")
        if "not a git repository" not in why:
            die(f"{a.repo}: {why}")
        die(f"{a.repo} is not inside a git repository: wb init {shlex.quote(a.repo)} sets the "
            "project up")
    tasks = store.tasks_dir().resolve()
    if repo.resolve().is_relative_to(tasks):  # gone at that task's accept, wedging this one (the audit)
        owner = store.Task(repo.resolve().relative_to(tasks).parts[0])
        die(f"{repo} is task {owner.name}'s worktree, not a project: start from its repo "
            f"(--repo {owner.spec.get('repo', '<the project>')})")
    _lane_fits(repo, a.test, off="*" in (a.allow_path or []))
    spec_text = Path(a.spec).read_text(encoding="utf-8") if a.spec != "-" else sys.stdin.read()
    if not spec_text.strip():
        die("the spec is empty")
    handed = _skill_names(a.skill)
    # the repo's own directories (context.repo_dirs reads only the spec's repo: task.json is to come)
    guard = context.repo_dirs(SimpleNamespace(spec={"repo": str(repo)}))
    lane_dirs = [_lane_dir(p, guard) for p in a.allow_path or []]
    if not shutil.which(exe := runner.opencode_bin()[0]):  # else the runner emptied the GPU, then
        # the round died as "opencode exited 1" (the 2026-11-16 audit)
        die(f"{exe} isn't on PATH: install OpenCode (https://opencode.ai), or put it on the PATH "
            "Claude Code runs wb with; wb doctor checks the setup")
    stack = gitops.stack(repo)
    m, state, why = pick_model(a.size, a.model, stack)
    if not state["available"] and not a.force:
        fix = models.remedy(m, state)
        die(f"{m.id} can't run: {state['why']}" + (f": {fix}" if fix else "")
            + (" (--force queues it anyway, to wait for it)" if models.may_come_up(state) else ""), 3)
    dirty = gitops.git(repo, "status", "--porcelain").strip()
    if not gitops.base_commit(repo, a.base):  # first: a typo read as a missing CLAUDE.md (the audit)
        die(f"no commit {a.base} in {repo}: a typo, or a ref not fetched yet (git fetch)")
    if not _identity(repo):
        die("git has no user.name/user.email here: the user's identity signs the commits "
            "(git config --global user.name/user.email)")
    _need_claude_md(repo, a.base, a)
    # Two projects, one GPU: a task that can't run beside the GPU's holders used to queue in
    # silence behind them, maybe for hours (the 2026-11-16 audit). Say so before any worktree.
    if not getattr(a, "bench", None):
        _say(_preempt_bench(m, a.name))
    held, waits = _gpu_wait(m, a.name)
    if waits and not a.queue:
        die(models.busy(held, f"{m.id} can't run beside {'it' if len(held) == 1 else 'them'}. "
                        "--queue starts it anyway, to wait behind; or do this step yourself"), 4)

    try:
        task.dir.mkdir(parents=True)
    except FileExistsError:  # a start under way, or one that died before task.json
        die(f"{task.dir} exists without a task.json: another wb start {a.name} is under way or "
            "one failed; pick a new name")
    try:
        base = gitops.create_worktree(repo, a.name, task.worktree, a.base)
    except gitops.GitError as e:
        shutil.rmtree(task.dir, ignore_errors=True)  # create_worktree removed its own leftovers
        die(str(e))
    subs = gitops.submodules(repo, base)
    budget = a.ask_budget
    limits = router.budgets(m)  # from the model's measured speed on this machine, else defaults
    store.write_json(task.dir / "task.json", {
        "name": a.name, "repo": str(repo), "cwd": os.getcwd(), "base": base, "base_ref": a.base,
        # the Claude Code session whose Bash ran this (_scope, the mod's `mine`); none by hand.
        # And its process: once that is gone, the directory rule may hand the task to a new one
        **({"session": sid} if (sid := os.environ.get("CLAUDE_CODE_SESSION_ID")) else {}),
        **({"claude_pid": int(pid)} if (pid := os.environ.get("CLAUDE_PID", "")).isdigit() else {}),
        "base_branch": gitops.start_branch(repo, a.base),  # where wb accept --merge lands
        "branch": gitops.branch_name(a.name), "model": m.id, "model_label": m.label,
        "opencode_model": m.id, "route": why, "size": a.size, "stack": stack,
        **({"trial": True} if a.trial else {}), "test_cmd": a.test, "test_timeout": a.test_timeout,
        # a replay of the bench driver's (bench.replay): no notices, no results row, not listed
        **({"bench": b} if (b := getattr(a, "bench", None)) else {}),
        **({"no_base_check": True} if a.no_base_check else {}),
        "auto_fix": a.auto_fix, "ask_budget": budget, "ask_timeout": a.ask_timeout,
        # periodic check-ins are opt-in: each wakes Claude, re-reading its whole context, and they
        # were 500K of one task's 1.2M (2026-11-17); the pane shows progress, and the warnings
        # (a nudge, a full window, handovers, a cut base check) still wake
        "checkin": a.checkin or 0, "self_review": a.self_review,
        "round_timeout": a.round_timeout or limits["round"],
        "silence_timeout": a.silence_timeout or limits["silence"],
        "think_timeout": a.think_timeout if a.think_timeout is not None else limits["think"],
        "created_at": store.now(), "wb_version": __version__})
    test_line = (f"- The acceptance command is `{a.test}`. Run it before you finish and make it "
                 "pass. Never weaken, skip or delete tests to get there.\n") if a.test else ""
    prompt = spec_text.rstrip() + PROTOCOL.format(branch=gitops.branch_name(a.name),
                                                  test_line=test_line, budget=budget)
    task.round_dir(1).mkdir(parents=True)
    task.round_dir(1).joinpath("prompt.md").write_text(prompt, encoding="utf-8")
    task.set_status(phase="queued", round=1, reviews=0, auto_fixes=0, tools=0,
                    tok_in=0, tok_out=0, session=None, last="starting",
                    skills=handed, skills_ever=handed, skills_rev=skills.revs(handed))
    context.grant(task, "allow", [p.strip() for p in a.allow or [] if p.strip()])
    context.grant(task, "paths", lane_dirs)
    spawn_runner(task)
    print(f"started {a.name} on {m.label} · branch {gitops.branch_name(a.name)} · "
          f"base {base[:8]}" + (f" · tests `{a.test}`, auto-fix {a.auto_fix}" if a.test else ""))
    if a.test and not a.no_base_check:
        print("base check: the runner runs the acceptance command once on the base first; what "
              "it saw comes with the first report (a check-in at once if it already passes)")
    if dirty:
        print("note: the repo has uncommitted changes; the agent's worktree starts from "
              f"{a.base}, without them")
    if subs:
        print(f"note: submodules ({', '.join(subs[:5])}) are not checked out in the agent's "
              "worktree: a build or test that needs them fails there")
    print(f"model: {why}" if why != "asked for" else f"model: {m.id} (asked for)")
    print(f"slice: {router.slice(m).line()}")
    if gaps := results.spec_gap_line():
        print(gaps)
    if handed:
        print("skills handed: " + ", ".join(handed))
    elif fits := skills.suggest(spec_text):
        use = skills.usage()
        print(f"library skills that may fit: {', '.join(_elsewhere(n, use.get(n), repo, stack) for n in fits)}"
              f" (wb skill hand {a.name} <name>)")
    holding = ", ".join(sorted({h["task"] for h in held.values()}))
    if not state["available"]:
        print(f"note: queued although {state['why']}")
    elif waits:
        print(f"note: the GPU is busy with {holding}; this task waits as queued until it's free")
    elif held:
        print(f"note: it runs beside {holding} on the GPU (their models fit together)")
    elif not state["resident"]:
        print("note: the runner loads it onto the GPU first, unloading what is there")
    print(f"next: wb wait {a.name}   (the workbench mod wakes Claude by itself when loaded)")


def _elsewhere(name: str, u: dict | None, repo: Path, stack: list[str]) -> str:
    """A suggested skill, and where it has worked so far when that was only other projects: one
    project's knowledge in the library shouldn't spread unnoticed."""
    seen = {r for r in (u or {}).get("repos") or () if r}
    if not seen or str(repo) in seen:
        return name
    kinds = {k for r in seen for k in router.row_stack({"repo": r})}
    other = f", {'/'.join(sorted(kinds))} work" if kinds and not kinds & set(stack) else ""
    return f"{name} (loaded so far only in {', '.join(sorted(Path(r).name for r in seen))}{other})"


def _spec_gaps(task: store.Task, gaps: list[str] | None) -> None:
    """What the spec missed, by Claude's own account (--spec-gap): a review round the spec cost,
    not the agent. Kept with the task, counted in results, and gathered as lessons."""
    if not gaps:
        return
    rnd = task.status.get("round", 1)
    task.set_status(spec_gaps=(task.status.get("spec_gaps") or [])
                    + [{"round": rnd, "text": g.strip()} for g in gaps])
    for g in gaps:
        task.event({"ts": store.now(), "round": rnd, "kind": "spec_gap", "text": g.strip()})


@one_at_a_time
def cmd_feedback(a):
    task = need(a.name)
    reap(task)
    st = task.status
    if st.get("phase") not in ("review", "stuck", "failed"):
        die(f"{a.name} is {st.get('phase')}; feedback goes to a task waiting for review")
    if runner_alive(task):
        die(f"{a.name}'s runner is still running")
    _need_worktree(task)
    text = a.text if a.text is not None else \
        sys.stdin.read() if a.message == "-" else Path(a.message).read_text(encoding="utf-8")
    if not text.strip():  # before anything is written: an empty one cost a round (the audit)
        die("the feedback is empty")
    _spec_gaps(task, a.spec_gap)
    dropped = _stale_notes(task)
    n = st.get("round", 1) + 1
    task.round_dir(n).mkdir(parents=True, exist_ok=True)
    rerun = " re-run the acceptance command and" if task.spec.get("test_cmd") else ""
    task.round_dir(n).joinpath("prompt.md").write_text(
        f"{lessons.FEEDBACK_HEAD} Make exactly these changes, then{rerun} finish with a short "
        "summary:\n\n" + text.strip(), encoding="utf-8")
    reviews = st.get("reviews", 0) + 1
    handled = store.now()  # everything before the new round; what its runner raises stays news
    task.set_status(context="fresh" if a.fresh else "keep" if a.keep_context else None)
    gpu = _gpu_note(task)  # before the task looks live (REAP_GRACE): it may probe model servers
    task.set_status(round=n, reviews=reviews, phase="queued",
                    auto_fixes_total=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                    auto_fixes=0)
    spawn_runner(task)
    _ack_all(task, upto=handled)
    print(f"feedback round {reviews} sent to {a.name} (round {n})"
          + ("  — that is past the usual 3 review rounds" if reviews > 3 else ""))
    _say(gpu)
    _say_dropped(dropped)
    _say(lessons.nudge(task))


def _settle_proposals(task: store.Task, take: bool) -> None:
    """The agent's proposed instruction files (runner.proposals) are never committed as they are:
    --take-claude-md puts each over its file, else it is dropped."""
    for real, prop in runner.proposals(task.worktree):
        rel = real.relative_to(task.worktree)
        if take:
            prop.replace(real)
            print(f"took the agent's proposed {rel}")
        else:
            prop.unlink()
            print(f"dropped the agent's proposed {rel} (--take-claude-md takes it)")


@one_at_a_time
def cmd_next(a):
    """Commit the approved step on the task's branch and give the same agent the next one: same
    worktree, same session (so it keeps what it has read), unless it is full or --fresh."""
    task = need(a.name)
    reap(task)
    spec, st = task.spec, task.status
    if st.get("phase") != "review":
        die(f"{a.name} is {st.get('phase')}; wb next follows an approved step under review")
    handed = _skill_names(a.skill) if a.skill else None
    if runner_alive(task):
        die(f"{a.name}'s runner is still running")
    _need_worktree(task)
    step = st.get("steps", 1)
    try:  # read before anything is committed: a bad path must leave the step as it was
        nxt = sys.stdin.read() if a.spec == "-" else Path(a.spec).read_text(encoding="utf-8")
    except OSError as e:
        die(f"can't read the next step's spec: {e}")
    if not nxt.strip():
        die("the next step's spec is empty")
    measured = results.step(task, landed=True)
    _settle_proposals(task, a.take_claude_md)
    try:
        commit = gitops.commit_all(task.worktree, f"{a.message}\n\nWorkbench-Task: {a.name} "
                                   f"(step {step})\nOpenCode-Session: {st.get('session')}")
    except gitops.GitError as e:
        die(str(e))
    _spec_gaps(task, a.spec_gap)  # after the commit: a retry after a failed one counts them once
    dropped = _stale_notes(task)
    if a.allow:
        context.grant(task, "allow", [p.strip() for p in a.allow if p.strip()])
    if a.test is not None:  # the one change to task.json after start
        store.write_json(task.dir / "task.json", {**spec, "test_cmd": a.test})
    test = a.test if a.test is not None else spec.get("test_cmd")
    test_line = (f"- The acceptance command is `{test}`. Run it before you finish and make it "
                 "pass. Never weaken, skip or delete tests to get there.\n") if test else ""
    used = ask_mcp.used(task) if "asks" in st else sum(  # the budget is the task's: what's left
        1 for p in (task.dir / "ask").glob("*.json") if not ask_mcp._permission(store.read_json(p, {})))
    budget = max(0, spec.get("ask_budget", 0) - used)
    n = st.get("round", 1) + 1
    task.round_dir(n).mkdir(parents=True, exist_ok=True)
    task.round_dir(n).joinpath("prompt.md").write_text(
        f"Step {step} of this task is done and committed ({commit}). The next step:\n\n"
        + nxt.rstrip()
        + PROTOCOL.format(branch=spec["branch"], test_line=test_line, budget=budget),
        encoding="utf-8")
    task.event({"ts": store.now(), "round": n, "kind": "next_step",
                "text": f"step {step} committed as {commit}; next step sent"})
    if handed is not None:
        task.set_status(skills=handed, skills_ever=_union(st.get("skills_ever"), handed),
                        skills_rev={**(st.get("skills_rev") or {}), **skills.revs(handed)})
    handled = store.now()
    task.set_status(round=n, steps=step + 1, step_round=n, reviews=0,
                    step_sizes=(st.get("step_sizes") or []) + [measured],
                    context="fresh" if a.fresh else None,
                    auto_fixes_total=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                    auto_fixes=0)
    # before the task looks live: fresh_reason exports the session (up to 30 s), and a task
    # queued with no runner that long was reaped as dead under wb next (the 0.90 review)
    why = runner.fresh_reason(task)
    gpu = _gpu_note(task)
    task.set_status(phase="queued", last=f"step {step} committed; next step queued")
    spawn_runner(task)
    _ack_all(task, upto=handled)
    print(f"step {step} of {a.name} committed as {commit}; step {step + 1} sent "
          + (f"to a fresh session ({why})" if why else "to the same session"))
    _say(gpu)
    _say_dropped(dropped)
    _say(lessons.nudge(task))


def cmd_answer(a):
    task = need(a.name)
    qid = a.id or task.status.get("asking")
    if not qid:
        die(f"{a.name} has no open question")
    if not ask_mcp.QID.match(qid) or not (task.dir / "ask" / f"{qid}.json").exists():
        die(f"no question {qid} in {a.name}")
    if not ask_mcp.is_open(task, qid):  # an answer to it would say "answered" to nobody (the 0.90 review)
        die(f"{qid} is closed: answered, or the agent stopped waiting for it")
    text = a.text if a.text is not None else Path(a.file).read_text(encoding="utf-8")
    if not text.strip():  # the question stays open: an empty answer unblocked the agent (the audit)
        die("the answer is empty")
    handled = store.now()  # the question; a newer notice, raised after the answer, stays news
    ask_mcp.write_answer(task, qid, text)  # whole, and never through a link in the lane
    _ack_all(task, upto=handled)
    print(f"answered {a.name}/{qid}")


def _ack_file(task):
    return task.dir / "acked"


def _write_ack(task, ts: float) -> None:
    """The mark replaced whole: the mod and wb wait read it on every poll, and a read between a
    truncate and the write (or a write that failed after it) saw 0, every old notice news again
    (the 2026-11-16 audit)."""
    f = _ack_file(task)
    tmp = f.with_name(f".acked.{os.getpid()}.tmp")
    tmp.write_text(str(ts))
    os.replace(tmp, f)


def _acked(task):
    try:
        return float(_ack_file(task).read_text() or 0)
    except (OSError, ValueError):  # none yet, or garbled: nothing acknowledged
        return 0.0


def _ack_all(task, upto: float | None = None):
    """Claude acted on the task: whatever it was notified about so far is handled, except news
    still true now. A note or a diff run seconds after the task failed must not swallow the
    failure notice Claude hasn't seen: the mark stops just short of it. A command that handles
    the news itself (answer, feedback, next, resume) passes `upto`, the moment it acted: all
    before it is handled, even news still true then (an answered question once came back from
    wb wait, the 2026-11-16 audit), and what its runner raises after it isn't; but the mark still
    stops short of another question still open (answer and allow write their answer first, so
    theirs isn't): answering the first of two acknowledged the second's notice before anything
    delivered it, and the agent waited out the ask timeout (the 0.90 review)."""
    acked = _acked(task)
    phase = task.status.get("phase")
    if upto is None:
        news = "question" if phase == "asking" else "checkin" if phase in LIVE else phase
        pending = [r["ts"] for r in store.notices()
                   if r["task"] == task.name and r["kind"] == news and r["ts"] > acked]
        upto = min(pending) - 0.001 if pending else store.now()
    elif phase == "asking":  # not in review: a question from an older round is closed by then
        still = [r["ts"] for r in store.notices()
                 if r["task"] == task.name and r["kind"] == "question" and r["ts"] > acked
                 and ask_mcp.is_open(task, str(r.get("id") or ""))]
        upto = min([upto, *(ts - 0.001 for ts in still)])
    _write_ack(task, max(acked, upto))


def _oldest_open(task: store.Task, rows: list[dict]) -> str | None:
    """The id of the task's oldest question still open, by its notice: the one a wait delivers
    next. status.asking lags an answer until the runner's next tick (the 0.90 review)."""
    asked = [r for r in rows if r["task"] == task.name and r["kind"] == "question" and r.get("id")
             and ask_mcp.is_open(task, str(r["id"]))]
    return min(asked, key=lambda r: r["ts"])["id"] if asked else None


def _stale(row, st: dict, oldest: str | None = None) -> bool:
    """A notice no longer true, as the mod judges it: a review, stuck or failed notice for a task
    since sent back, resumed or closed, a question no longer asked, a check-in for a task no
    longer running. Of the questions, only the oldest still open (`oldest`, _oldest_open) is
    news: oldest first, as the runner's status.asking orders them, but judged by the answer
    files, so the second of two is delivered once the first is answered, not the first again
    (the 0.90 review). An old notice without an id stands for whatever is asked."""
    phase = st.get("phase")
    if row["kind"] in ("review", "stuck", "failed"):
        return phase != row["kind"]
    if row["kind"] == "question":
        return phase != "asking" or ("id" in row and row["id"] != oldest)
    if row["kind"] == "checkin":
        return phase not in ("queued", "running", "testing")
    return False


def cmd_wait(a):
    """Block until a task (or any task) raises a notification nobody has acknowledged yet."""
    deadline = time.time() + a.timeout if a.timeout else None
    path = store.home() / "notify.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    watcher = Watcher(store.home(), *([need(a.name).dir] if a.name else []))
    while True:
        for t in [need(a.name)] if a.name else store.all_tasks():
            if t.status.get("phase") in LIVE:
                reap(t)  # a dead runner's task becomes failed, with the notice this wait delivers
        rows = store.notices()
        for row in reversed(rows):
            if a.name and row["task"] != a.name:
                continue
            task = store.Task(row["task"])
            if not task.exists or _stale(row, task.status, _oldest_open(task, rows)
                                         if row["kind"] == "question" else None):
                continue  # its directory removed by hand, or no longer true
            if row["ts"] > _acked(task):
                _write_ack(task, row["ts"])
                print(f"[{row['kind']}] {row['task']}\n{row.get('text', '')}")
                return
        if a.name:
            task = need(a.name)
            st = task.status
            phase = st.get("phase")
            if phase in store.CLOSED:
                die(f"{a.name} is closed", 2)
            # Already waiting on Claude, its notice taken by the mod or another waiter: say so
            # now rather than block until a notice that will never come. A task stopped or taken
            # over by hand has none at all (the 2026-11-16 audit: a wait on it blocked forever).
            if phase in store.NEEDS_CLAUDE and (phase == "asking" or not runner_alive(task)):
                want = "question" if phase == "asking" else phase
                mine = [r for r in rows if r["task"] == a.name and r["kind"] == want
                        and not _stale(r, st, _oldest_open(task, rows))
                        and r.get("round", st.get("round")) == st.get("round")]
                if mine:
                    print(f"[{want}] {a.name} (already delivered once)\n{mine[-1].get('text', '')}")
                    return
                if phase == "failed" and st.get("last") == RUNNER_DIED \
                        and time.time() - (st.get("updated_at") or 0) < 10 \
                        and not (deadline and time.time() > deadline):
                    # another wb is reaping it (reap writes the phase, then sweeps, then the
                    # notice): said "no notice", and the notice came again on the next wait (the
                    # 0.90 review). Bounded: a reaper killed meanwhile ends in "no notice".
                    watcher.wait(1.0)
                    continue
                if phase != "asking":  # a question always has its notice, which carries its id
                    print(f"[{phase}] {a.name} (no notice: {st.get('last') or phase})")
                    return
        elif not any(t.status.get("phase") not in store.CLOSED for t in store.all_tasks()):
            die("no open tasks to wait for (wb status --all)", 2)
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
                          color=False if a.no_color else None, last=a.last if a.json else 0)
    except KeyboardInterrupt:
        print()
    except BrokenPipeError:  # piped into head/less and the reader quit
        sys.stdout = open(os.devnull, "w")


def _gone(pid) -> bool:
    """Whether the Claude Code process a task recorded (task.json `claude_pid`) has exited. One we
    can't signal isn't it either: it ran as this user. Unknown (no pid) is not gone."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        return False
    return False


def _scope(tasks, where, sessions=None):
    """The tasks of one Claude Code session, as the mod names it: `sessions` (`--session`, every
    id its process has had: /clear starts another) and `where` (`--repo`, its directory). A task
    `wb start` recorded a session for (task.json `session`) is that session's, wherever it ran;
    once the Claude Code process it ran in is gone (`claude_pid`, `_gone`), the directory rule
    claims it too, so a new process in the project adopts the old one's tasks (the 0.90 review:
    after a crash or an update, plain `claude` in the same repo was woken for none of them, and
    its quit left them running for nobody). One without a session (started by hand, or before
    0.90), or with no `sessions` given (by hand), goes by directory: its `repo` is `where`,
    inside it or holds it, or its `cwd` (where wb start ran) is `where` or inside it. A session
    in the home directory or / claims none by directory. A task with neither key is everyone's.
    The others belong to another session, which its mod wakes and its quit commands stop (the
    2026-11-16 audit: two projects at once, one session's /workbench-quit stopped the other's
    agents; the 0.90 review: a task started from ~ belonged to every session under it, and one
    started after a `cd` elsewhere to none). Returns (mine, others)."""
    if not where and not sessions:
        return list(tasks), []
    here = Path(where).resolve() if where else None
    broad = here in (Path.home().resolve(), Path("/"))
    mine, others = [], []
    for t in tasks:
        sid, cwd, repo = (t.spec.get(k) for k in ("session", "cwd", "repo"))
        # Path.is_relative_to compares whole components: /x/ab isn't inside /x/a
        near = here is not None and not broad and bool(
            cwd and Path(cwd).is_relative_to(here)
            or repo and (Path(repo).is_relative_to(here) or here.is_relative_to(repo)))
        if sessions and sid:
            ok = sid in sessions or near and _gone(t.spec.get("claude_pid"))
        elif here is None or (not cwd and not repo):
            ok = True
        else:
            ok = near
        (mine if ok else others).append(t)
    return mine, others


def _others_line(others, what):
    if others:  # by name: all_tasks' order follows the file system's (flaky in CI on 0.90's merge)
        return (f"{what} (other sessions'): "
                + ", ".join(f"{t.name} ({t.spec.get('repo')})"
                            for t in sorted(others, key=lambda t: t.name)))
    return ""


def cmd_status(a):
    tasks = [need(a.name)] if a.name else [t for t in store.all_tasks()  # replays: wb bench report
                                           if a.all or (t.status.get("phase") not in store.CLOSED
                                                        and not t.spec.get("bench"))]
    tasks, others = (tasks, []) if a.name else _scope(tasks, a.repo, a.session)
    if not tasks:
        print("no open tasks" + ("" if a.all else " (wb status --all shows closed ones)"))
        _say(_others_line(others, "open elsewhere"))
        return
    print(f"{'task':22} {'phase':9} {'rnd':>3} {'model':16} {'age':>5} {'idle':>5}  last")
    for t in tasks:
        if t.status.get("phase") in LIVE:
            reap(t)
        st, sp = t.status, t.spec
        phase = st.get("phase", "?")
        if phase in store.ACTIVE and not runner_alive(t) and phase != "asking":
            phase += "?"  # the runner died seconds ago (reap waits REAP_GRACE)
        short = sp.get("model", "").split("/")[-1][:16]
        print(f"{t.name:22} {phase:9} {st.get('round', 1):>3} {short:16} "
              f"{ago(sp.get('created_at')):>5} {ago(st.get('updated_at')):>5}  "
              f"{(st.get('last') or '')[:70]}")
    if a.name and tasks[0].worktree.is_dir():
        print(f"worktree: {tasks[0].worktree}")
    _say(_others_line(others, "open elsewhere"))


def cmd_log(a):
    task = need(a.name)
    off = 0
    rows, off = store.read_jsonl(task.dir / "events.jsonl", off)
    # the agent's words and output, on a terminal: no raw escape sequences (transcript.plain)
    shown = transcript.plain if sys.stdout.isatty() else str

    def show(r):
        t = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
        k = r["kind"]
        if k == "tool":
            mark = "" if r["ok"] else "  ✗ " + r.get("error", "")[:100]
            print(shown(f"{t} r{r['round']} {r['tool']:8} {r['brief']}{mark}"))
        elif k in ("text", "ask", "answer", "stopped", "autofix", "round", "tests", "error"):
            body = r.get("text", "")
            if k == "text" and not a.verbose:
                body = body.strip().splitlines()[0][:110] if body.strip() else ""
            print(shown(f"{t} r{r['round']} {k:8} {body}"))

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
    text = p.read_text() if p.exists() else (f"round {n} has no report yet "
                                             f"({task.status.get('phase')}: {task.status.get('last')})")
    print(transcript.plain(text) if sys.stdout.isatty() else text)  # it quotes the agent's words


def cmd_diff(a):
    """The work under review: the whole task, or after `wb next` the current step (--all: all)."""
    task = need(a.name)
    if not task.worktree.is_dir():
        die(f"{a.name} is {task.status.get('phase')} and its worktree is gone: "
            f"git -C {shlex.quote(task.spec['repo'])} diff {task.spec['base'][:12]} "
            f"{task.spec['branch']}")
    base = "HEAD" if task.status.get("steps", 1) > 1 and not a.all else task.spec["base"]
    try:
        print(gitops.diff(task.worktree, base, stat=a.stat, paths=a.paths) or "(no changes)")
    except gitops.GitError as e:
        die(f"{e}\nthe branch's commits: git -C {shlex.quote(task.spec['repo'])} diff "
            f"{task.spec['base'][:12]} {task.spec['branch']}")


def _stop_runner(task):
    pid = task.status.get("pid")
    if pid and runner_alive(task):
        task.set_status(stopping=True)  # wb's stop, not a shutdown's (runner.SIGNAL_STOP)
        try:
            os.kill(pid, signal.SIGTERM)  # the runner stops its agent's process group itself
        except ProcessLookupError:
            pass
        else:
            if not wait_exit(pid, 15):  # it stops its agent first; past that, end it all
                for group in (pid, task.status.get("agent_pid")):  # the agent has a session of its own
                    try:
                        if group:
                            os.killpg(group, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
    runner.sweep(task)  # whatever the agent left running, the runner dead or alive


LIVE = ("queued", "running", "testing", "asking")


def _open_tasks():
    return [t for t in store.all_tasks()
            if t.status.get("phase") not in store.CLOSED
            and (runner_alive(t) or t.status.get("phase") in LIVE)]




def stop_everything(tasks=None) -> list[str]:
    """`wb stop --all`: every agent (or those of `tasks`). Nothing is finished or thrown away:
    tasks keep their worktrees (stopped, for wb feedback or wb discard later)."""
    stopped = []
    for t in _open_tasks() if tasks is None else tasks:
        # each task under its command lock, as one_at_a_time (which --all skips): an accept or
        # discard that closed it after the listing was set back to stuck (the 0.90 review)
        with store.locked(runner.lock_file(t.name, "cmd")):
            if t.status.get("phase") in store.CLOSED:
                continue
            _stop_runner(t)
            t.event({"ts": store.now(), "round": t.status.get("round"), "kind": "stopped",
                     "by": "user", "text": "bail out: stopped by the user"})
            t.set_status(phase="stuck", last="bail out: stopped", pid=None)
            _ack_all(t)
            stopped.append(f"{t.name} (worktree kept: {t.worktree})")
    return stopped


@one_at_a_time
def cmd_stop(a):
    if a.all:
        # everything, without --repo/--session: the bench's driver too, first, so it starts nothing
        driver = None if a.repo or a.session else bench.stop_driver()
        mine, others = _scope(_open_tasks(), a.repo, a.session)
        stopped = ([f"wb bench (pid {driver})"] if driver else []) + stop_everything(mine)
        print("stopped: " + ("; ".join(stopped) if stopped else "nothing was running"))
        _say(_others_line(others, "left running"))
        return
    if a.repo or a.session:
        die("--repo and --session go with --all")
    if not a.name:
        die("name a task, or --all")
    task = need(a.name)
    if task.status.get("phase") not in LIVE and not runner_alive(task):
        die(f"{a.name} is {task.status.get('phase')}, not running")
    _stop_runner(task)
    task.set_status(phase="stuck", last="stopped by the reviewer", pid=None)
    _ack_all(task)  # else the mod's safety net wakes Claude about the stop it just made
    print(f"stopped {a.name}; wb resume continues it in a fresh session, wb feedback with "
          "corrections, wb discard drops it")


@one_at_a_time
def cmd_resume(a):
    """Continue a task Claude stopped (wb stop) where the work stands: a fresh session, handed
    the task, the work so far and what the step's rounds asked. Not a review round."""
    task = need(a.name)
    reap(task)
    st = task.status
    if st.get("phase") not in ("stuck", "failed"):
        die(f"{a.name} is {st.get('phase')}; wb resume continues a stopped task")
    if runner_alive(task):
        die(f"{a.name}'s runner is still running")
    _need_worktree(task)
    n = st.get("round", 1)
    asks = runner.round_asks(task, n)
    notes = _stale_notes(task)  # resume means carry on: a note never delivered still applies
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    task.round_dir(n + 1).joinpath("prompt.md").write_text(runner.RESUME.format(
        asked=runner.ASKED.format("\n\n".join(asks)).rstrip() if asks else "")
        + (f"\n\n{runner.steer_prompt(notes, False)}" if notes else ""))
    handled = store.now()
    gpu = _gpu_note(task)  # before the task looks live (REAP_GRACE): it may probe model servers
    task.set_status(round=n + 1, context="fresh", phase="queued", last="resumed by the reviewer")
    task.event({"ts": store.now(), "round": n + 1, "kind": "resumed",
                "text": "Claude resumed the task in a fresh session"})
    spawn_runner(task)
    _ack_all(task, upto=handled)  # the stuck or failed notice is handled: wb wait must not hand it out again
    print(f"resumed {a.name} in a fresh session (round {n + 1})")
    _say(gpu)
    _say(lessons.nudge(task))


def _lane_dir(raw: str, guard: set[str] = frozenset()) -> str:
    """A directory Claude adds to an agent's lane, made if it isn't there yet (the kernel's rule
    is on a real directory). Not through a link: the agent can plant one in its lane (/tmp/out ->
    another project), and the grant went to the link's target (the 2026-11-16 audit), so Claude
    names the real directory itself; nor a place the lane hides (context.lane_refusal); nor a
    place in the task's repo or its git directory (`guard`, context.repo_dirs), which
    context.writable leaves out of every lane: the grant was said "allowed", the agent restarted,
    and its retry was refused again (the 0.90 review). One that holds the repo is split around it."""
    if raw == "*":
        return raw  # the lane off for this task
    given = os.path.abspath(os.path.expanduser(raw))
    real = os.path.realpath(given)
    if real != given:
        die(f"{raw} is a link to {real}: allow {real} itself if that is what the task needs")
    if why := context.lane_refusal(real):
        die(why)
    if any(real == g or context._under(real, g) for g in guard):
        die(f"{raw}: inside the task's own repo or its git directory, which no lane may write (wb's "
            "git runs there, and a hook planted in it would run outside the lane); take what the "
            "task needs there yourself")
    path = Path(real)
    if not path.exists():
        path.mkdir(parents=True)
    return str(path)


def _permission_asked(task: store.Task) -> str | None:
    """The oldest permission request still open. status.asking is the oldest question of any
    kind, and `wb allow` answered a plain question asked before the request with its leave,
    the request left open (the 0.90 review)."""
    def made(qid):
        try:
            return (task.dir / "ask" / f"{qid}.json").stat().st_mtime
        except OSError:  # the agent can delete its ask files
            return float("inf")
    perms = [q for q, k in (task.status.get("asks") or {}).items()
             if k == "p" and ask_mcp.is_open(task, q)]
    return min(perms, key=lambda q: (made(q), q), default=None)


def cmd_allow(a):
    """Let a task's agent run commands outside the everyday list (context.ALLOWED_COMMANDS), or
    use a directory outside its lane (--path, context.writable): for this task, or --always for
    every task. A pending request is answered, and a running agent is restarted on its session at
    once so the new rule applies (OpenCode reads it at start, the lane is drawn at start)."""
    task = need(a.name)
    pats = [p.strip() for p in a.pattern if p.strip()]
    dirs = [_lane_dir(p, context.repo_dirs(task)) for p in a.path or [] if p.strip()]
    if not pats and not dirs:
        die("nothing to allow")
    conf_path = context.commands_path()
    conf = context.commands_conf()  # refused unless it parses: rewriting it would lose the file
    for key, new in (("allow", pats), ("paths", dirs)):
        if not new:
            continue
        context.grant(task, key, new)
        if a.always:
            conf[key] = (conf.get(key) or []) + [p for p in new if p not in (conf.get(key) or [])]
    if a.always:
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        store.write_json(conf_path, conf)
    said = ", ".join([f"`{p}`" for p in pats] + [f"the directory {d}" for d in dirs])
    text = (f"Claude allowed {said}" + (" from now on" if a.always else " for this task")
            + ". Run it again now and carry on." + (f" {a.text.strip()}" if a.text else ""))
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "allow",
                "text": f"allowed {said}" + (" always" if a.always else "")})
    qid = _permission_asked(task) or task.status.get("asking")
    handled = None
    if qid and ask_mcp.is_open(task, qid):
        handled = store.now()  # the request is answered: handled, as wb answer does
        ask_mcp.write_answer(task, qid, text)  # whole, and never through a link in the lane
    if (phase := task.status.get("phase")) in LIVE and runner_alive(task):
        store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text, "now": True})
        print(f"allowed {said} on {a.name}" + (" (always)" if a.always else "") + ": "
              + _when_delivered(task, phase, "the agent restarts on its session with it"))
    else:
        print(f"allowed {said} on {a.name}" + (" (always)" if a.always else "")
              + ": it applies from the next round")
    _ack_all(task, upto=handled)


def cmd_steer(a):
    """A note to a running agent: delivered when its current run ends, or at once with --now."""
    task = need(a.name)
    reap(task)
    phase = task.status.get("phase")
    if phase not in ("queued", "running", "testing", "asking") or not runner_alive(task):
        die(f"{a.name} is {phase}, not running: use wb feedback (or wb answer for a question)")
    text = (Path(a.message).read_text() if a.message else a.text).strip()
    if not text:
        die("empty note")
    store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text, "now": a.now})
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "steer",
                "now": a.now, "text": text})
    if phase in ("testing", "queued"):
        print(f"note queued for {a.name}: " + _when_delivered(task, phase, ""))
    elif a.now:
        print(f"interrupting {a.name}: it stops within a few seconds and continues with your note")
    else:
        print(f"note queued for {a.name}: delivered when its current run ends, before the tests")


def _when_delivered(task: store.Task, phase: str, running: str) -> str:
    """When a note reaches the agent: --now promised "within a few seconds" while the tests ran
    (up to the test timeout) or the task waited for the GPU (the 2026-11-16 audit)."""
    if phase == "testing":
        return (f"its tests are running (up to {int(task.spec.get('test_timeout') or 900)}s); "
                "the agent gets it when they end")
    if phase == "queued":
        return ("its round hasn't started (waiting for the GPU or its model); the agent gets it "
                "when the round starts")
    return running


def cmd_note(a):
    """Claude's own words in the task's transcript: a captain's log entry the user sees."""
    task = need(a.name)
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "captain",
                "text": a.text.strip()})
    _ack_all(task)
    print(f"noted on {a.name}")


@one_at_a_time
def cmd_takeover(a):
    """Stop the agent and hand the worktree to Claude, to finish by hand or hand back."""
    if a.all:  # "just get it done claude"
        tasks, others = _scope([t for t in store.all_tasks()
                                if t.status.get("phase") not in store.CLOSED], a.repo, a.session)
        if not tasks:
            print("no open delegated tasks to take over")
        for t in tasks:
            cmd_takeover(argparse.Namespace(name=t.name, all=False, repo=None, session=None))
        _say(_others_line(others, "left alone"))
        return
    if getattr(a, "repo", None) or getattr(a, "session", None):
        die("--repo and --session go with --all")
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
    print(f"took over {a.name}: the agent is stopped. Edit in {task.worktree} and leave it "
          f"uncommitted, then wb accept {a.name} -m \"…\" (it commits), or hand it back with "
          f"wb feedback {a.name} --message <file>")


@one_at_a_time
def cmd_accept(a):
    task = need(a.name)
    reap(task)
    spec, st = task.spec, task.status
    if st.get("phase") in store.CLOSED:
        die(f"{a.name} is already {st.get('phase')}")
    if st.get("phase") != "review" and not a.force:
        die(f"{a.name} is {st.get('phase')}, not waiting for review (--force accepts anyway)")
    # A doomed accept once stopped the runner first, then died on "nothing to commit", leaving
    # the task running with no runner (the 2026-11-16 audit): check before stopping anything,
    # and a stopped task whose commit fails is stuck, for wb resume, feedback or discard.
    was_live = st.get("phase") in LIVE
    if (was_live and st.get("steps", 1) == 1 and task.worktree.is_dir()
            and not gitops.changed(task.worktree)):
        die(f"{a.name} has no changes to accept yet: wait for it, or wb stop {a.name} first")
    _stop_runner(task)  # under --force it may still be running, and would set its phase back
    _settle_proposals(task, a.take_claude_md)
    repo = Path(spec["repo"])
    rounds, autofix = st.get("round", 1), st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0)
    msg = (f"{a.message}\n\nDelegated to {spec['model_label']} via OpenCode: {rounds} round(s), "
           f"{autofix} automatic test-fix, {st.get('reviews', 0)} review round(s)."
           f"\n\nWorkbench-Task: {a.name}\nOpenCode-Session: {st.get('session')}")
    measured = results.step(task, landed=True)
    try:
        commit = _landed(task)
        if not commit:
            head = gitops.git(task.worktree, "rev-parse", "HEAD").strip()
            if gitops.changed(task.worktree) or head == spec["base"]:
                commit = gitops.commit_all(task.worktree, msg)
            else:  # nothing new: the branch's commits past the base are the work (the steps
                # wb next committed, or Claude's own after a takeover: the audit)
                commit = gitops.git(task.worktree, "rev-parse", "--short", "HEAD").strip()
    except gitops.GitError as e:  # nothing removed: the uncommitted work is still in the worktree
        if was_live:
            task.set_status(phase="stuck", last=f"accept failed: {e}"[:200], pid=None)
            die(f"{e}\n{a.name} is stopped: fix the cause and wb accept again, or wb resume, "
                "wb feedback or wb discard it")
        die(str(e))
    if warn := gitops.remove_worktree(repo, task.worktree):  # committed: the rest is cleanup
        print(f"wb: {warn}", file=sys.stderr)
    context.drop_runtime_dir(task)
    _spec_gaps(task, a.spec_gap)
    merged = None
    if a.merge:
        try:
            merged = gitops.merge(repo, a.name, spec.get("base_branch") or spec.get("base_ref"),
                                  spec["base"])
        except gitops.GitError as e:
            print(f"wb: not merged: {e}", file=sys.stderr)
    task.set_status(phase="accepted", last=f"accepted as {commit}" +
                    (f", merged {merged}" if merged else ""))
    row = results.record(task, "accepted", measured, commit=commit, merged=bool(merged))
    _ack_all(task)
    try:  # a reminder: it once turned a finished accept into exit 1 (the 2026-11-16 audit)
        note = claude_md_status(repo, spec["branch"])  # the task's own commit counts
    except (gitops.GitError, OSError, ValueError):
        note = None
    if note:
        print("the repo has no CLAUDE.md: write one (wb init)" if note == "no CLAUDE.md" else
              f"{note}: keep it current (update it if this change alters what it says)"
              if note == LOCAL_CLAUDE_MD else
              f"{note}: keep it current (update and commit it if this change alters what it says)")
    print(f"accepted {a.name}: commit {commit} on {spec['branch']}" +
          (f", merged into {gitops.current_branch(repo)} as {merged}" if merged else
           f"\nmerge with: git -C {shlex.quote(str(repo))} merge --no-ff {spec['branch']}"))
    _say(spend.line(row.get("claude")))
    _say(lessons.nudge(task, "accepted"))


def _landed(task: store.Task) -> str | None:
    """The commit an earlier wb accept made before its cleanup failed: the worktree is gone or
    broken and the branch's tip carries accept's own trailer (a step's says "(step N)"). A retry
    then finishes the job; it once died on "nothing to commit" for good (the 2026-11-16 audit)."""
    try:
        gitops.git(task.worktree, "rev-parse", "HEAD")
        return None  # a working worktree: commit from it
    except gitops.GitError:
        pass
    repo, branch = task.spec["repo"], task.spec["branch"]
    if f"Workbench-Task: {task.name}" in gitops.git(repo, "log", "-1", "--format=%B", branch,
                                                    check=False).splitlines():
        return gitops.git(repo, "rev-parse", "--short", branch).strip()
    return None


@one_at_a_time
def cmd_discard(a):
    task = need(a.name)
    spec = task.spec
    if task.status.get("phase") in store.CLOSED:
        die(f"{a.name} is already {task.status.get('phase')}")
    _stop_runner(task)
    _spec_gaps(task, a.spec_gap)
    repo = Path(spec["repo"])
    measured = results.step(task, landed=False)
    if warn := gitops.remove_worktree(repo, task.worktree):  # never stops a discard (the audit)
        print(f"wb: {warn}", file=sys.stderr)
    context.drop_runtime_dir(task)
    kept = a.keep_branch or not repo.is_dir()
    if not kept:
        gitops.delete_branch(repo, a.name)
    task.set_status(phase="discarded", last=a.reason or "discarded", pid=None)
    row = results.record(task, "discarded", measured, reason=a.reason)
    _ack_all(task)
    print(f"discarded {a.name}" + ("" if a.keep_branch else f"; branch {spec['branch']} deleted"
                                   if not kept else f"; {repo} is gone, so its branch with it"))
    _say(spend.line(row.get("claude")))
    _say(lessons.nudge(task, "discarded"))


def cmd_models(a):
    """What OpenCode offers, whether each can run, and who the router would pick per size."""
    found = models.discover()
    if not found:
        die(f"no local models: {models.why_none()}")
    for m in found.values():
        st = models.status(m)
        print(f"{m.id:34} {('on GPU' if st['resident'] else 'ready' if st['available'] else 'no'):6}"
              f"  {m.label}" + ("" if st["available"] else f"  ({st['why']})"))
        print(f"{'':36}slice: {router.slice(m).line()}")
        if sp := router.speed_line(m.id):
            print(f"{'':36}speed: {sp} on this machine")
        if b := bench.model_line(m.id):
            print(f"{'':36}bench: {b} (wb bench report)")
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
            models.write_config(cfg, f'set "model": "{top.model.id}"')
        except RuntimeError as e:
            die(str(e))
        print(f"\nOpenCode's default model: {old} → {top.model.id} ({why})")


def cmd_stats(a):
    print(results.table())


def cmd_tune(a):
    print(tuning.table())


def cmd_trial(a):
    """Closed tasks that were checks of workbench itself, not work: their results stop counting.
    An open task is refused: it has no results row yet, so it was marked as nothing while its
    lessons were dropped, and its accept later counted (the 2026-11-16 audit)."""
    live = [n for n in a.names if store.Task(n).exists
            and store.Task(n).status.get("phase") not in store.CLOSED]
    if live:
        die(f"{', '.join(live)} still open: wb trial marks closed tasks (wb start --trial marks "
            "one up front)")
    hit = store.mark_trials(set(a.names))
    open_ = [i for i, x in lessons._all()[0].items() if x["task"] in a.names]
    shut = lessons.close(open_, "dropped", "a trial run: a check of workbench, not work")
    print(f"trial runs now, left out of the record: {', '.join(hit)}" if hit
          else "no unmarked results for those tasks")
    if shut:
        print(f"lessons dropped with them: {len(shut)}")


def cmd_bench(a):
    """Replays of accepted tasks, graded by their hidden checks (bench.py)."""
    if a.bench_cmd == "add":
        print("\n".join(bench.add(a.task, a.step, a.hidden, a.check, a.name)))
    elif a.bench_cmd == "list":
        print("\n".join(bench.list_lines()))
    elif a.bench_cmd == "run":
        if a.repeat < 1:
            die("--repeat is at least 1")
        _, lines = bench.start(a.model or [], a.case or [], _skill_names(a.skill), a.label or "",
                               a.repeat)
        print("\n".join(lines))
    elif a.bench_cmd == "stop":
        pid = bench.stop_driver()
        print(f"stopped the bench run (pid {pid})" if pid else "no bench run is running")
    else:
        print(bench.report(a.run, a.label))


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
    print("  " + gpu_line())
    print("\nimage engines (wb image)")
    print("\n".join(image_lines()) or f"  none: describe one in {images.config_path()}")
    provs = models.providers()
    registered = models.discover()
    print("\nmodel servers here (work with what the user runs)")
    print("\n".join(server_lines(provs)))
    print("\nmodels OpenCode can use (opencode.json)")
    real = defaultdict(list)
    for r in store.results():
        if tuning.judged(r):  # a discard Claude put down to its own spec says nothing of the model
            real[r.get("model")].append(r)
    for pid, prov in provs.items():
        # an Ollama that doesn't answer is asked once, not for every model (the 2026-11-16 audit)
        up = prov["engine"] == "ollama" and (models._fake() or models.ollama_answers(prov["root"]))
        if prov["engine"] == "ollama" and not up:
            print(f"  {pid}: Ollama isn't answering at {prov['root']}, so what it has and serves is unknown")
        installed = {x["name"]: x for x in models.ollama_installed(prov["root"])} if up else {}
        other = prov["engine"] != "ollama" and not models._fake() and \
            models._ask(f"{prov['url'].rstrip('/')}/models", prov["key"], timeout=1) is not None
        for name in prov["models"]:
            m = registered.get(f"{pid}/{name}")
            disk = installed.get(name) or installed.get(f"{name}:latest") or {}
            said = models.server_facts(prov["root"], name, prov["key"]) if other else {}
            if said.get("weights"):
                disk = {"size": said["weights"]}
            served = models.ollama_served_context(prov["root"], name) if up else said.get("context")
            ctx = ((m.config.get("limit") or {}).get("context") or 0) if m else 0
            facts = " · ".join(x for x in (
                f"{hardware.size(disk.get('size'))} on disk" if disk.get("size") else "",
                disk.get("params") or "", disk.get("quant") or "",
                f"{ctx // 1024}K context" if ctx else "no limit.context" if m else "",
                f"{'Ollama' if up else 'the server'} serves {served // 1024}K" if served else "") if x)
            rs = real.get(f"{pid}/{name}", [])
            used = (f"{len(rs)} real task(s), {sum(r['outcome'] == 'accepted' for r in rs)} accepted, "
                    f"last {time.strftime('%Y-%m-%d', time.localtime(max(r['ts'] for r in rs)))}"
                    if rs else "no real tasks yet")
            replayed = bench.model_line(f"{pid}/{name}")
            print(f"  {pid}/{name}: {facts or prov['engine']} · {used}"
                  + (f" · bench: {replayed}" if replayed else "")
                  + (f"\n    slice: {router.slice(m).line()}" if m else ""))
        extra = [x for x in installed if x not in prov["models"] and x.removesuffix(":latest")
                 not in prov["models"]]
        local = [x for x in extra if not installed[x]["cloud"]]
        if local:
            print(f"  installed in {pid} but not registered: " + ", ".join(
                f"{x} ({hardware.size(installed[x]['size'])})" for x in local))
        if cloud := [x for x in extra if installed[x]["cloud"]]:  # stubs: ollama.com runs them
            print(f"  Ollama cloud models in {pid} (off this machine, not candidates): " + ", ".join(cloud))
    if not provs:
        print("  none: " + models.why_none())
        if models.ollama_answers(root := models.ollama_root_here()):
            print(f"  Ollama answers at {root} but OpenCode has no provider for it (wb model add "
                  "ollama/<model> adds one); it has: " + (", ".join(
                      x["name"] for x in models.ollama_installed(root)) or "nothing pulled"))
    cfg = models.read_config()
    print(f"  OpenCode's default: {cfg.get('model') or '(none)'}")
    print("\nreal results (wb stats)")
    print(results.table())
    print("\nnext: compare with what is newest and best for this hardware, recommend to the user, "
          "and when done: wb scout --done --notes \"…\"")


def server_lines(provs: dict) -> list[str]:
    """wb scout's view of the model servers on this machine, whichever they are: the commands on
    PATH, and each default port that answers, with whether OpenCode has a provider for it."""
    on_path, answering = models.servers_here()
    roots = {p["root"].rstrip("/") for p in provs.values()}
    lines = [f"  on PATH: {', '.join(on_path)}"] if on_path else []
    for x in answering:
        listed = ", ".join(x["models"][:8]) + (", ..." if len(x["models"]) > 8 else "") or "no models"
        known = (x["root"] in roots or x["root"].replace("127.0.0.1", "localhost") in roots)
        lines.append(f"  {x['root']} answers (usually {x['usually']}): {listed}" + (
            "" if known else f"; OpenCode has no provider for it (wb model add <provider>/<model> "
                             f"--url {x['root']}/v1 --context N)"))
    return lines or ["  none found on PATH or answering on the usual ports"]


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


def _say(line: str | None) -> None:
    if line:
        print(line)


def _one_project(ids, name: str) -> list[str]:
    """A warning when every lesson behind a new skill came from one repo: the library is for
    every project, and a rule only that one needs belongs in it."""
    from_ = lessons.repos([x for i in ids or [] for x in i.split(",") if x])
    if len(from_) != 1 or None in from_:
        return []
    repo = next(iter(from_))
    return [f"every lesson behind it came from {Path(repo).name}: if only that project needs it, "
            f"move it to {repo}/.claude/skills/{name}/ (committed there) and wb skill retire {name}"]


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
        print(f"{verb} {a.name}" + "".join(f"\nwarning: {w}" for w in warns + _one_project(a.lesson, a.name))
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
        except ValueError as e:
            die(str(e))
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


NOTES_MAX, NOTE_CHARS = 20, 300  # the mod puts them in every workbench-mode prompt: keep them few


def _notes() -> list[str]:
    try:
        text = (store.home() / "notes.md").read_text()
    except FileNotFoundError:
        return []
    notes = []
    for line in text.splitlines():  # a hand-wrapped line belongs to the note above it
        if line.startswith("- ") or not notes:
            notes.append(line.removeprefix("- ").strip())
        elif line.strip():
            notes[-1] += " " + line.strip()
    return [n for n in notes if n]


def cmd_notes(a):
    """What Claude has learned about how the user wants delegation run, in any project: the mod
    hands them to every workbench-mode session, so they follow the user from repo to repo."""
    path = store.home() / "notes.md"
    with store.locked(store.home() / "notes.lock"):
        notes = _notes()
        if a.action == "add":
            text = " ".join(" ".join(a.args).split())
            if not text:
                die('wb notes add "<what the user wants, in any project>"')
            if len(text) > NOTE_CHARS:
                die(f"{len(text)} chars (max {NOTE_CHARS}): one preference, in a sentence")
            if len(notes) >= NOTES_MAX:
                die(f"{NOTES_MAX} notes already: drop or merge one first (wb notes drop N)")
            notes.append(text)
        elif a.action == "drop":
            try:
                gone = sorted({int(n) for n in a.args}, reverse=True)
                for n in gone:
                    if not 1 <= n <= len(notes):
                        raise ValueError
            except ValueError:
                die(f"wb notes drop N: a number from 1 to {len(notes)}" if notes else "no notes yet")
            for n in gone:
                del notes[n - 1]
        if a.action:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".notes.md.{os.getpid()}.tmp")
            tmp.write_text("".join(f"- {n}\n" for n in notes))
            os.replace(tmp, path)
    if not notes:
        print("no notes yet: wb notes add \"<what the user wants, in any project>\"")
    for i, n in enumerate(notes, 1):
        print(f"{i}. {n}")


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
                   + (" · a server: it holds its GPU memory while it runs" if e.get("url")
                      and not e.get("command") else "")
                   + (f"\n    {e['notes']}" if e.get("notes") else ""))
    return out


def cmd_image(a):
    """Local image generation: Claude writes the prompt, looks at the candidates, keeps one."""
    # `wb image list`, like `wb skill list`, used to draw four pictures of "list" (the 2026-11-16 audit)
    if a.list or not a.prompt or a.prompt.strip().lower() in ("list", "ls"):
        print("\n".join(image_lines()) or f"no image engine: describe one in {images.config_path()}")
        return
    try:
        w, h = (int(x) for x in a.size.lower().split("x"))
    except ValueError:
        die(f"--size {a.size!r}: WIDTHxHEIGHT, e.g. 1024x1024")
    out = Path(a.out) if a.out else store.home() / "images" / time.strftime("%Y%m%d-%H%M%S")
    try:
        made = images.generate(a.prompt, out, n=a.n, size=(w, h), seed=a.seed, name=a.engine,
                               refs=a.ref or [], say=lambda m: print(m, flush=True), wait=a.wait)
    except (KeyError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        die(str(e).strip("'\""))
    print(f"{len(made)} image(s) in {out}: look at each before choosing; the .json beside each "
          "holds its prompt and seed")


def cmd_model(a):
    """Add a model to OpenCode's config (pulling it first with --pull), or retire one."""
    if a.action == "sizes":
        try:
            m = models.resolve(a.id)
        except KeyError as e:
            die(e.args[0] if e.args else f"no model {a.id}")
        bad = set(a.sizes or ()) - set(router.SIZES)
        if bad:
            die(f"sizes are {', '.join(router.SIZES)}")
        models.set_override(m.id, "sizes", sorted(set(a.sizes)) if a.sizes else None)
        print(f"{m.id}: routed " + (" and ".join(sorted(set(a.sizes))) + " tasks only"
                                    if a.sizes else "tasks of every size"))
        return
    provider, _, name = a.id.partition("/")
    if a.action != "add" and (a.url or a.start or a.stop):
        die("--url, --start and --stop go with wb model add")
    if a.action == "add" and name and a.url:
        try:  # any OpenAI-compatible server, not only Ollama (the server is the user's choice)
            if models.add_server_provider(provider, a.url):
                print(f"added provider {provider} to {models.opencode_config_path()}: {a.url}")
        except RuntimeError as e:
            die(str(e))
    if a.action == "add" and name:  # how wb starts and stops a server that runs one model each
        for key, cmd in (("start", a.start), ("stop", a.stop)):
            if cmd:
                models.set_override(a.id, key, cmd)
    provs = models.providers()
    if a.action == "add" and name and provider == "ollama" and provider not in provs:
        try:  # the first run: Ollama answers, OpenCode has no provider for it
            root = models.add_ollama_provider(provider)
        except RuntimeError as e:
            die(str(e))
        if root:
            print(f"added provider {provider} to {models.opencode_config_path()}: the Ollama at {root}")
            provs = models.providers()
    if provider not in provs or not name:
        skipped = dict(models.skipped_providers())
        die(f"provider {provider}: {skipped[provider]}" if provider in skipped else
            f"name it provider/model, with one of OpenCode's local providers: {', '.join(provs)}"
            if provs else f"no local provider: {models.why_none()}")
    prov = provs[provider]
    if a.action == "add":
        if prov["problem"]:
            die(f"provider {provider}: {prov['problem']}")
        own = models._overrides().get(a.id) or {}
        if not prov["engine_known"] and not prov["start"] and not own.get("start"):
            # a guess would write an Ollama entry without its output cap, or the reverse
            # (the 2026-11-16 audit)
            die(f"{prov['root']} is not answering, so wb can't tell whether {provider} is Ollama: "
                f"start it, or pass --start (the command that starts it), or set \"engine\" for "
                f"{provider} in {models.overrides_path()}, and retry")
        if prov["engine"] == "ollama" and not models._fake() and not models.ollama_answers(prov["root"]):
            # a pull then died with a urllib traceback, and a describe blamed a missing pull
            # (the 2026-11-16 audit)
            die(f"Ollama isn't answering at {prov['root']}: start it (ollama serve, or its "
                "service or app), then retry")
        if prov["engine"] == "ollama" and models.ollama_cloud(prov["root"], name) \
                and not models.cloud_allowed(a.id):
            die(f"{a.id} is an Ollama cloud model: it runs on ollama.com, off this machine. To hand "
                f"it work anyway, give it \"local\": true in {models.overrides_path()} first")
        if a.pull:
            if prov["engine"] != "ollama":
                die(f"{provider} is not an Ollama provider; install the model the server's own way")
            try:
                models.ollama_pull(prov["root"], name, say=lambda m: print(m, flush=True))
            except (OSError, RuntimeError, ValueError) as e:  # URLError and HTTPError are OSErrors
                die(f"pulling {name} failed: {getattr(e, 'reason', e)}")
        try:
            facts = models.ollama_show(prov["root"], name) if prov["engine"] == "ollama" else {}
        except Exception as e:
            die(f"{provider} can't describe {name} ({e}): is it pulled? (--pull downloads it)")
        caps = facts.get("capabilities") or []
        # the context the server actually serves: what it says it gives this model, else what
        # this provider's other models are set to. Never Ollama's trained length: it serves its
        # own small default then, and the prompt was cut off silently (the 2026-11-16 audit)
        other = {} if prov["engine"] == "ollama" or models._fake() else \
            models.server_facts(prov["root"], name, prov["key"])
        served = models.ollama_served_context(prov["root"], name) if prov["engine"] == "ollama" \
            else other.get("context")
        siblings = [(e.get("limit") or {}).get("context") for e in prov["models"].values()]
        ctx = a.context or served or next((c for c in siblings if c), None)
        if prov["engine"] == "ollama":
            if not ctx:
                die(f"Ollama would serve {name} its own default context (often 4K-8K, too small for "
                    "OpenCode's ~10K-token prompt): set OLLAMA_CONTEXT_LENGTH (e.g. 32768) in the "
                    f"Ollama service and restart it, then wb model add {a.id} again; or pass "
                    "--context N if you know what it serves")
            ctx = min(ctx, facts.get("context_length") or ctx)  # never past what it was trained on
        elif not ctx and not ((prov["models"].get(name) or {}).get("limit") or {}).get("context"):
            # an entry with no window: no handovers, no compaction, and the server cut the
            # session off mid-task (the 2026-11-16 audit)
            die(f"{provider} doesn't say what window it serves {name} (LM Studio: not loaded yet): "
                "pass --context N, the context the server was started with (llama.cpp -c, LM "
                "Studio's context length, vLLM --max-model-len)")
        if ctx and ctx < models.MIN_WINDOW:
            die(f"{a.id}: {models.small_window_why(ctx)}")
        if a.context and other.get("context") and a.context > other["context"]:
            print(f"--  {provider} serves {name} a {other['context'] // 1024}K window, below --context "
                  f"{a.context}: it would cut prompts off until the server is started with more")
        tools = "tools" in caps if caps else other.get("tools") is not False  # LM Studio says
        entry = {"name": a.label or name,
                 "tool_call": tools,
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
        ctx = ctx or prov["models"][name]["limit"]["context"]  # kept from the entry as it was
        print(f"registered {a.id}: "
              f"{'tools' if entry['tool_call'] else 'NO tool calls, so wb will not route tasks to it'}, "
              f"{'reasoning' if entry['reasoning'] else 'no reasoning'}, "
              f"{ctx // 1024}K context\nnext: wb model sizes {a.id} … if it should only "
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
    for key in list(models._overrides().get(a.id) or {}):  # its sizes, labels, start/stop
        models.set_override(a.id, key, None)
    done = f"retired {a.id}: removed from OpenCode's config (its measurements stay in the history)"
    if a.delete:
        if prov["engine"] != "ollama":
            done += f"; {provider} is not Ollama, so delete its weights the server's own way"
        else:  # the entry is gone already: say what's left rather than crash (the 2026-11-16 audit)
            try:
                models.ollama_delete(prov["root"], name)
                done += "; its weights were deleted from Ollama"
            except OSError as e:  # URLError and HTTPError are OSErrors
                done += ("; Ollama had no weights for it" if getattr(e, "code", None) == 404 else
                         f"; its weights were NOT deleted ({getattr(e, 'reason', e)}): ollama rm {name}")
    print(done)


LOCAL_CLAUDE_MD = "CLAUDE.md is gitignored: agents get the checkout's copy"


def claude_md_status(repo: Path, ref: str = "HEAD") -> str:
    """One line on the repo's CLAUDE.md at ref: missing, local, or how far behind the code it
    may be."""
    md = _claude_md_at(repo, ref)  # CLAUDE.md before .claude/CLAUDE.md: AGENTS.md links to it
    if not md:
        return LOCAL_CLAUDE_MD if _local_claude_md(repo) else "no CLAUDE.md"
    last = gitops.git(repo, "log", "-1", "--format=%h", ref, "--", md).strip()
    since = int(gitops.git(repo, "rev-list", "--count", f"{last}..{ref}", "--", ".",
                           f":!{md}", ":!AGENTS.md").strip() or 0)  # commits that changed code
    return (f"{md} is current with {ref}" if not since else
            f"{md} last changed {since} commit(s) before {ref} ({last})")


# What wb init won't put in a project's first commit (the 2026-11-16 audit: an .env with an API
# key, node_modules and .venv all went in). Matched on a file's name, and on a path's directories.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519",
                "id_ecdsa", "id_dsa", "credentials.json", ".npmrc", ".pypirc", ".netrc")
NOT_SECRET = (".env.example", ".env.sample", ".env.template")
VENDORED = ("node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache")


def _unfit_for_commit(d: Path) -> tuple[list[str], list[str]]:
    """(likely secrets, vendored or generated directories) among the files `git add -A` would
    take; `target/` counts only beside a Cargo.toml."""
    secrets, vendored = [], set()
    for n in gitops.git(d, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not n:
            continue
        parts = n.rstrip("/").split("/")
        if parts[-1] not in NOT_SECRET and any(fnmatch.fnmatch(parts[-1], p) for p in SECRET_FILES):
            secrets.append(n)
        for i, c in enumerate(parts[:-1] if not n.endswith("/") else parts):
            if c in VENDORED or c == "target" and (d.joinpath(*parts[:i]) / "Cargo.toml").is_file():
                vendored.add("/".join(parts[:i + 1]) + "/")
                break
    return secrets, sorted(vendored)


def cmd_init(a):
    """Set a project up as Claude keeps every project: a git repo (on git's init.defaultBranch)
    with an initial commit, AGENTS.md a symlink to CLAUDE.md. Safe to run again; no remote is ever added."""
    d = Path(a.dir).expanduser().resolve()
    if Path.home().resolve().is_relative_to(d):  # the home directory, or above it (/ included)
        die(f"wb init takes a project's own directory, not {d}: make one (e.g. ~/projects/<name>)")
    d.mkdir(parents=True, exist_ok=True)
    try:
        inside = str(gitops.toplevel(d))
    except gitops.GitError as e:  # a repo git refuses (dubious ownership) is no place to git init
        if "not a git repository" not in str(e):
            die(f"{d}: {str(e).split(': ', 1)[-1]}")
        inside = ""
    if inside and Path(inside) != d:
        die(f"{d} is inside the git repo {inside}; init the project in its own directory")
    if not _identity(d):
        die("git has no user.name/user.email: the user's identity signs the commits "
            "(git config --global user.name/user.email)")
    did = []
    if not inside:
        gitops.git(d, "init", "-q")  # on the user's init.defaultBranch
        did.append("git repo on " + (gitops.git(d, "symbolic-ref", "--short", "HEAD",
                                                check=False).strip() or "its default branch"))
    has_commits = bool(gitops.git(d, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())
    if not has_commits:  # before anything is changed: a run after the fix starts clean
        secrets, vendored = _unfit_for_commit(d)
        if secrets or vendored:
            die(f"not committing {d} as it is: "
                + (f"these look like secrets, which never go in git: {', '.join(secrets[:10])}"
                   + (f" (+{len(secrets) - 10})" if len(secrets) > 10 else "") + "; " if secrets else "")
                + (f"these are installed or generated: {', '.join(vendored[:10])}; " if vendored else "")
                + f"add them to {d / '.gitignore'} (or remove them) and run wb init again")
    claude, agents = d / "CLAUDE.md", d / "AGENTS.md"
    if claude.is_symlink() and agents.is_file() and not agents.is_symlink() \
            and claude.resolve() == agents.resolve():
        # CLAUDE.md -> AGENTS.md, the reverse convention: swap them. Comparing the two names'
        # text once deleted AGENTS.md, the only copy (the audit)
        claude.unlink()
        agents.rename(claude)
        agents.symlink_to("CLAUDE.md")
        did.append("AGENTS.md moved to CLAUDE.md, which it now links to")
    if agents.is_file() and not agents.is_symlink() and not claude.exists():
        agents.rename(claude)  # the project's instructions live in CLAUDE.md, AGENTS.md points at it
        did.append("AGENTS.md moved to CLAUDE.md")
    if claude.is_file() and agents.is_file() and not agents.is_symlink():
        if not os.path.samefile(claude, agents) and agents.read_bytes() == claude.read_bytes():
            agents.unlink()
        else:
            print("AGENTS.md and CLAUDE.md differ: merge them into CLAUDE.md, delete AGENTS.md, "
                  "and run wb init again")
    local = _local_claude_md(d)  # kept out of git: a link to it would dangle in every clone
    if claude.is_file() and not local and not agents.exists() and not agents.is_symlink():
        agents.symlink_to("CLAUDE.md")
        did.append("AGENTS.md → CLAUDE.md")
    if not has_commits:
        gitops.git(d, "add", "-A")  # the agents work from commits: the project as it is now
        n = len(gitops.git(d, "diff", "--cached", "--name-only", "-z").split("\0")) - 1
        gitops.git(d, "commit", "-q", "--allow-empty", "-m", "Initial commit")
        did.append(f"initial commit of the {n} file(s) here (git show --stat to check)" if n
                   else "initial commit")
    else:
        touched = [p for p in ("CLAUDE.md", "AGENTS.md")
                   if gitops.git(d, "status", "--porcelain", "--", p).strip()]
        if touched and any("AGENTS.md" in x or "moved" in x for x in did):
            gitops.git(d, "add", "-A", "--", *touched)
            gitops.git(d, "commit", "-q", "-m", "chore: AGENTS.md links to CLAUDE.md", "--", *touched)
            did.append("committed")
    status = claude_md_status(d)
    print(f"{d}: " + (", ".join(did) if did else "already set up") + f"; {status}"
          + (" (AGENTS.md not linked)" if local else "")
          + ("\nnote: CLAUDE.md is a link to a missing file" if claude.is_symlink() and not claude.exists() else "")
          + ("\nnext: write CLAUDE.md (what the project is, how to build and test it, its "
             "conventions), commit it, then wb init again to link AGENTS.md"
             if status == "no CLAUDE.md" else ""))


# What `wb events` reports: the state, not the work (worktrees) or the noise. rules/ (the agents'
# config layers and shims) is nothing the mod reads; each closed task is skipped too (cmd_events):
# a thousand finished tasks were 4,000 inotify watches per Claude session, or a rescan of them
# four times a second without inotify (the 2026-11-16 audit).
EVENT_SKIP = ("tasks/*/worktree", "tasks/*/rounds", "rules", "imports", "images")  # clones, outputs
QUIET = (".lock", ".tmp", "runner.log")


def cmd_events(a):
    """One JSON line the moment workbench's state changes: what the mod listens to instead of
    polling. The first line is {"hello": pid}; it ends when the process that started it does."""
    home = store.home()
    (home / "tasks").mkdir(parents=True, exist_ok=True)
    chat.chat_dir()
    parent = os.getppid()
    print(json.dumps({"hello": os.getpid()}), flush=True)
    # accepted and discarded are final and names aren't reused; tasks/ itself stays watched, so a
    # new task is picked up, and one that closes while this runs simply stays watched
    closed = tuple(f"tasks/{t.name}" for t in store.all_tasks() if t.status.get("phase") in store.CLOSED)
    reaped = time.time()
    with Watcher(home, recursive=True, skip=EVENT_SKIP + closed) as w:
        while os.getppid() == parent:
            changed = {str(p.relative_to(home)) for p in w.wait(10)
                       if not p.name.endswith(QUIET) and not p.name.startswith(".")}
            if time.time() - reaped > 60:  # a runner that died silently: its failed notice
                reaped = time.time()       # wakes the mod like any other (reap)
                for t in store.all_tasks():
                    if t.status.get("phase") in LIVE:
                        reap(t)
            if changed:
                try:
                    print(json.dumps({"changed": sorted(changed)}), flush=True)
                except BrokenPipeError:
                    return


def cmd_chat(a):
    """The web chat: the user's side door to Claude while away from the terminal."""
    if a.action == "serve":
        chat.serve(a.port, a.bind or None, a.host or None, say=lambda m: print(m, flush=True))
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


def cmd_setup(a):
    """Link `wb` onto PATH and /workbench into Claude Code's skills, from wherever this copy lives:
    the marketplace's clone (~/.claude/plugins/marketplaces/workbench) for users, a git clone for
    development (2026-11-16: the plugin install copies only mod/, so these two come from here)."""
    links = [(Path.home() / ".local/bin/wb", PKG_ROOT / "bin/wb"),
             (context.claude_home() / "skills/workbench", PKG_ROOT / "skill/workbench")]
    bad = False
    for at, to in links:
        if at.is_symlink() and at.resolve() == to.resolve():
            print(f"ok  {at} → {to}")
            continue
        if at.is_symlink() and a.force:
            at.unlink()
        elif at.is_symlink() or at.exists():
            bad = True
            print(f"BAD {at} is already {'a link to ' + os.readlink(at) if at.is_symlink() else 'there'}: "
                  f"left as it is{' (--force replaces a link)' if at.is_symlink() else ''}")
            continue
        at.parent.mkdir(parents=True, exist_ok=True)
        at.symlink_to(to)
        print(f"ok  {at} → {to} (made)")
    old = context.claude_home() / "skills/delegate"  # /delegate became /workbench (2026-11-17)
    if old.is_symlink() and not old.exists() and os.readlink(old).rstrip("/").endswith("skill/delegate"):
        old.unlink()
        print(f"ok  {old} removed: /delegate is now /workbench")
    bindir = str(Path.home() / ".local/bin")
    if bindir not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"--  {bindir} isn't on PATH: add it; Claude and the mod run wb by name (WB_BIN moves "
              "only the mod's calls)")
    print("next: wb doctor")
    sys.exit(1 if bad else 0)


UPDATE_EVERY = 7 * 24 * 3600  # the mod's weekly check (wb update --check --if-due)


def _fetch_origin() -> None:
    """The update check's fetch: never prompts and never hangs. The user's own ssh command
    (GIT_SSH_COMMAND, GIT_SSH, core.sshCommand) is kept, as replacing it picked the wrong key for
    good; with none, ssh runs in BatchMode. No terminal, so nothing can ask, and a time limit
    inside the mod's (the 2026-11-16 audit)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "SSH_ASKPASS_REQUIRE": "never"}
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH")
            or gitops.git(PKG_ROOT, "config", "core.sshCommand", check=False).strip()):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    try:
        r = subprocess.run(["git", "-C", str(PKG_ROOT), "fetch", "-q", "origin"], env=env,
                           capture_output=True, stdin=subprocess.DEVNULL, start_new_session=True,
                           timeout=FETCH_SECS)
    except subprocess.TimeoutExpired:
        raise gitops.GitError(f"origin didn't answer in {FETCH_SECS}s") from None
    if r.returncode:
        raise gitops.GitError(r.stderr.decode("utf-8", "replace").strip() or "git fetch failed")


FETCH_SECS = 60  # under the mod's 120 s for a wb command


def cmd_update(a):
    """Bring this copy up to date with its origin: the marketplace's clone through Claude Code
    (which also refreshes the mod's cached copy), a developer's clone with a fast-forward pull.
    --check only says whether there is news; --if-due asks the network at most weekly."""
    marketplace = context.claude_home() / "plugins" / "marketplaces"
    kind = "marketplace" if PKG_ROOT.resolve().is_relative_to(marketplace.resolve()) else "clone"
    path = store.home() / "update.json"
    last = store.read_json(path, {})
    if a.check:
        quiet = os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")
        if a.if_due and (quiet or time.time() - last.get("checked_at", 0) < UPDATE_EVERY):
            try:  # no network: the last fetch's upstream against HEAD as it is now, which a hand
                # pull or Claude Code's own update moved (a stale count toasted for a week: the audit)
                behind = int(gitops.git(PKG_ROOT, "rev-list", "--count", "HEAD..@{u}"))
            except (gitops.GitError, ValueError):
                behind = last.get("behind", 0)
        else:
            store.write_json(path, {**last, "checked_at": time.time()})  # a killed check waits too
            try:
                _fetch_origin()
                behind = int(gitops.git(PKG_ROOT, "rev-list", "--count", "HEAD..@{u}"))
            except gitops.GitError as e:
                die(f"couldn't check for an update: {e}")
            store.write_json(path, {"checked_at": time.time(), "behind": behind, "kind": kind})
        print(f"update available: {behind} new commit(s); /workbench-update (or wb update) installs it"
              if behind else "up to date")
        return
    live = [t.name for t in store.all_tasks() if runner_alive(t)]
    if live:  # a pull would change the git shim, the lane and the ask server under them
        die(f"tasks are running ({', '.join(live)}): update when they finish, or wb stop --all first")
    steps = [["claude", "plugin", "marketplace", "update", "workbench"],
             ["claude", "plugin", "update", "workbench@workbench"]]
    if kind == "clone":  # a clone is its own marketplace: pull, then let Claude Code re-read it
        if gitops.git(PKG_ROOT, "status", "--porcelain", "--untracked-files=no").strip():
            die(f"{PKG_ROOT} has uncommitted changes: commit or stash them, then wb update")
        steps.insert(0, ["git", "-C", str(PKG_ROOT), "pull", "-q", "--ff-only"])
    if not shutil.which("claude"):  # before the pull: a clone once pulled, then died (the audit)
        die("claude isn't on PATH: wb update refreshes the plugin through it (run /workbench-update "
            "in Claude Code, or put claude on PATH)")
    before = gitops.git(PKG_ROOT, "rev-parse", "HEAD").strip()
    pulled = ""
    for step in steps:
        try:
            r = subprocess.run(step, capture_output=True, text=True, timeout=300)
            err = (r.stderr or r.stdout).strip()[-300:] if r.returncode else None
        except (OSError, subprocess.TimeoutExpired) as e:
            err = str(e)
        if err is not None:
            die(f"{' '.join(step[:4])} failed: {err}" + pulled)
        if step[0] == "git":  # landed: the count is 0 whatever the claude steps do next
            store.write_json(path, {"checked_at": time.time(), "behind": 0, "kind": kind})
            pulled = ("; the pull is done: finish with claude plugin marketplace update workbench "
                      "&& claude plugin update workbench@workbench, then /reload-plugins")
    after = gitops.git(PKG_ROOT, "rev-parse", "HEAD").strip()
    store.write_json(path, {"checked_at": time.time(), "behind": 0, "kind": kind})
    if before == after:
        print("already up to date")
    else:
        n = gitops.git(PKG_ROOT, "rev-list", "--count", f"{before}..{after}").strip()
        print(f"updated {kind} {before[:7]}..{after[:7]} ({n} commit(s)); /reload-plugins or a "
              "restart picks up the pane")


def gpu_line() -> str:
    """What the GPU budget counts (wb doctor, wb scout): sharing and the router's fit rest on it."""
    budget, devs = models.budget_devices()
    if budget:
        names = "; ".join(g["name"] for g in devs) or "the GPU"
        return (f"GPU budget {budget / 1e9:.0f} GB, from {names}: tasks share the GPU when their "
                "Ollama models fit in it together")
    gs = [] if models._fake() else hardware.gpus()
    if gs:
        return (f"GPU memory unknown here ({', '.join(sorted({g['driver'] for g in gs}))}): tasks take "
                "turns on the GPU, and the router can't hold back a model too big for it")
    return ("no GPU memory found: local models run on the CPU, slowly; "
            "wb model sizes <id> trivial keeps a big one off small tasks")


def cmd_doctor(a):
    ok = True

    def line(good, what):
        nonlocal ok
        ok &= good
        print(f"{'ok ' if good else 'BAD'} {what}")

    def elsewhere(what, at, mine):
        """Another copy's (a clone and a marketplace install both linked once): it runs, not this
        one, which doctor was checking (the 2026-11-16 audit)."""
        if at and Path(at).exists() and Path(at).resolve() != mine.resolve():
            print(f"--  {what} comes from {Path(at).resolve().parent.parent}, not this copy ({PKG_ROOT}): "
                  "wb setup --force from the copy you want" if Path(at).is_symlink() or what == "wb" else
                  f"--  {at} is a directory of its own, not a link to this copy's {mine}")

    def version(argv):
        try:
            r = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=30)
            return r.returncode == 0, r.stdout.strip() or r.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"not found ({e})"

    good, said = version(runner.opencode_bin())
    major = re.search(r"(\d+)\.\d+", said) if good else None
    if good and not major:  # an odd build string: say it, don't fail on it
        print(f"--  opencode {said}: its version doesn't parse; workbench needs OpenCode 2.x")
    else:  # the config layer and `run --standalone --auto` are 2.x only (the 2026-11-16 audit)
        old = bool(major) and int(major[1]) < 2
        line(good and not old, f"opencode {said}"
             + (": workbench needs OpenCode 2.x (checked against 2.3); upgrade it" if old else ""))
    files = models.opencode_config_files()
    try:
        models.read_config()
        line(bool(files), "OpenCode config " + (" + ".join(map(str, files)) or
                                                f"{models.opencode_config_path()} (none)")
             + (" (OpenCode layers them; wb won't rewrite either)" if len(files) > 1 else ""))
    except RuntimeError as e:  # "<file> doesn't parse (…)"
        line(False, str(e))
        sys.exit(1)  # nothing below can read the models
    if os.environ.get("OPENCODE_CONFIG"):
        print(f"--  OPENCODE_CONFIG is set ({os.environ['OPENCODE_CONFIG']}): wb reads only "
              f"{models.opencode_config_path().parent}, and agents get their own layer in its "
              "place, so providers there are invisible to both")
    good, said = version(["git"])
    line(good, said)
    # Claude's Bash runs `wb` by name whatever WB_BIN says: WB_BIN only moves the mod's own calls
    # (the 2026-11-16 audit: doctor said ok beside "the mod can't run it")
    on_path, wb_bin = shutil.which("wb"), os.environ.get("WB_BIN")
    if on_path:
        line(True, f"wb on PATH: {on_path}" + (f" (the mod runs WB_BIN={wb_bin})" if wb_bin else ""))
    else:
        line(False, "wb isn't on PATH: Claude runs it by name in Bash, and the mod too; link bin/wb "
             "into a PATH directory (wb setup)" + (f"; WB_BIN={wb_bin} covers only the mod's calls"
                                                    if wb_bin else ""))
    elsewhere("wb", wb_bin or on_path, PKG_ROOT / "bin/wb")
    found = models.discover()
    line(bool(found), f"{len(found)} local model(s)")
    for pid, why in models.skipped_providers():  # once dropped without a word (the 2026-11-16 audit)
        print(f"--  provider {pid}: {why}")
    provs = models.providers()
    if not provs and models.ollama_answers(root := models.ollama_root_here()):
        print(f"--  Ollama answers at {root} but OpenCode has no provider for it "
              "(wb model add ollama/<model> adds one)")
    # each Ollama is asked once whether it answers: a sleeping LAN box cost 35 s a model, and
    # doctor ran past Claude's 2-minute Bash limit (the 2026-11-16 audit)
    down = set() if models._fake() else {
        p["root"] for p in provs.values() if p["engine"] == "ollama" and not models.ollama_answers(p["root"])}
    for pid, prov in provs.items():
        if prov["root"] in down:
            print(f"--  provider {pid}: Ollama isn't answering at {prov['root']}; its context checks are skipped")
    states = {mid: models.status(m, down) for mid, m in found.items()}
    for mid, st in states.items():
        print(f"{'ok ' if st['available'] else '-- '} model {mid}: {st['why']}")
    if found and not any(st["available"] for st in states.values()):  # doctor said all was well
        mid = next(iter(found))                                         # (the 2026-11-16 audit)
        fix = models.remedy(found[mid], states[mid])
        line(False, "no model can run now" + (f" ({mid}: {fix})" if fix else ""))
    for pid, prov in provs.items():
        if prov["problem"]:
            line(False, f"provider {pid}: {prov['problem']}")
        ollama = prov["engine"] == "ollama"
        for name, e in prov["models"].items():
            said = (e.get("limit") or {}).get("context")
            if not said and f"{pid}/{name}" in found:  # it passed, and the runner never handed
                # over (the 2026-11-16 audit)
                line(False, f"{pid}/{name} has no limit.context, so its tasks never hand over and "
                     "nothing compacts before the server cuts them off: wb model add "
                     f"{pid}/{name}" + ("" if ollama else " --context N (the window it serves)"))
            if not ollama:
                # only a server running this model is asked: a down one cost seconds a model
                served = models.server_facts(prov["root"], name, prov["key"])["context"] \
                    if (states.get(f"{pid}/{name}") or {}).get("resident") else None
                if said and served and said > served:
                    line(False, f"{pid}/{name} says a {said // 1024}K context but the server serves "
                         f"{served // 1024}K: it would cut prompts off (start it with a bigger "
                         f"window, or wb model add {pid}/{name} --context {served})")
                elif said and served and served > said:
                    print(f"--  {pid}/{name}: the server serves {served // 1024}K, the entry says "
                          f"{said // 1024}K; wb model add {pid}/{name} --context {served} lets its tasks grow")
                continue
            capped = "max_tokens" in (e.get("options") or {})
            line(capped, f"{pid}/{name} caps its output on Ollama (options.max_tokens; Ollama ignores "
                 "the max_completion_tokens OpenCode sends)" + ("" if capped else f": wb model add {pid}/{name}"))
            if stall := router.thinking_stall(f"{pid}/{name}", e):
                print(f"--  {stall}")
            if prov["root"] in down:
                continue
            served = models.ollama_served_context(prov["root"], name)
            if said and not served:  # a stock Ollama serves 4K-8K (the 2026-11-16 audit)
                print(f"--  {pid}/{name}: Ollama's served context is unknown (not loaded, no num_ctx, "
                      f"no OLLAMA_CONTEXT_LENGTH); its default may be below the entry's "
                      f"{said // 1024}K and cut prompts off: set OLLAMA_CONTEXT_LENGTH")
            if said and served and said > served:
                line(False, f"{pid}/{name} says a {said // 1024}K context but Ollama serves "
                     f"{served // 1024}K: it would cut prompts off silently (raise OLLAMA_CONTEXT_LENGTH"
                     + (f", or wb model add {pid}/{name} --context {served})" if served >= models.MIN_WINDOW
                        else f": tasks need {models.MIN_WINDOW // 1024}K+)"))
            elif said and said < models.MIN_WINDOW:
                line(False, f"{pid}/{name}: {models.small_window_why(said)}")
            elif said and served and served > said:
                print(f"--  {pid}/{name}: Ollama serves {served // 1024}K, the entry says {said // 1024}K; "
                      f"wb model add {pid}/{name} --context {served} lets its tasks grow")
    try:  # every wb command makes it: a fresh home has no ~/.local/state yet (the 2026-11-16 audit)
        store.home().mkdir(parents=True, exist_ok=True)
        why = "" if os.access(store.home(), os.W_OK | os.X_OK) else "not writable"
    except OSError as e:
        why = f"can't make it: {e.strerror or e}"
    line(not why, f"state dir {store.home()}" + (f": {why}" if why else ""))
    print("--  " + gpu_line())  # informs only: a CPU-only machine isn't broken
    v = lane.abi()
    if v:  # draw a real lane and try a write outside it: the check is the refusal itself
        import tempfile
        with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory(dir=Path.home()) as out:
            probe = [sys.executable, "-c", f"open({str(Path(out) / 'x')!r}, 'w')"]
            r = subprocess.run(lane.wrap([inside], probe), capture_output=True, text=True, timeout=30)
            # refused by the lane, not by the kernel refusing the lane (exit 126, "workbench
            # lane: …") and not some other failure; a home named "delaney" holds "lane" in the
            # PermissionError's path (the 2026-11-16 audit)
            held = (r.returncode not in (0, 126) and "PermissionError" in r.stderr
                    and not (Path(out) / "x").exists())
        line(held, f"agents write only in their lane (Landlock ABI {v})" if held else
             f"the lane didn't hold a write outside it: {r.stderr.strip()[-200:] or 'the write went through'}")
        for gap in lane.gaps(v):  # what this kernel's Landlock can't confine (the 2026-11-16 audit)
            print(f"--  Landlock ABI {v}: {gap}")
        if gap := context.interop_gap():  # WSL: a Windows program escapes it (the 0.90 review)
            print(f"--  {gap}")
    else:
        print("--  agents' writes outside their lane and reads of credentials aren't refused here "
              "(no Landlock): only OpenCode's file tools keep to it; " +
              ("sudo still is (no-new-privileges)" if sys.platform.startswith("linux")
               else "nor is sudo"))
    try:
        context.commands_conf()
        line(True, f"commands.json {context.commands_path()}"
             + ("" if context.commands_path().exists() else " (none: no standing allowances)"))
    except RuntimeError as e:
        line(False, str(e))
        sys.exit(1)  # every task reads it: nothing runs until it is fixed
    snapped = context.snaps(context.TOOLCHAINS) if context.lane_on() else []
    if snapped and sys.platform.startswith("linux"):
        print(f"--  {', '.join(snapped)}: snaps, which can't run in the agent's lane "
              "(no-new-privileges stops snap-confine): install them another way, or turn the lane "
              'off with commands.json "paths": ["*"]')
    skill = context.claude_home() / "skills/workbench/SKILL.md"
    line(skill.exists(), f"/workbench skill at {skill.parent}" if skill.exists() else
         f"/workbench isn't in {skill.parent.parent}: wb setup links it")
    elsewhere("/workbench", skill.parent, PKG_ROOT / "skill/workbench")
    try:  # information only: a mod loaded another way (--plugin-dir, a dev mod) isn't listed here
        listed = any(k.startswith("workbench@") for k in json.loads(
            (context.claude_home() / "plugins/installed_plugins.json").read_text()).get("plugins", {}))
    except (OSError, ValueError, AttributeError):
        listed = False
    print("ok  the Claude Code mod is installed (workbench@workbench)" if listed else
          "--  the Claude Code mod (the pane, the wake-ups) isn't among Claude Code's installed "
          "plugins: claude plugin install workbench@workbench, unless it loads another way")
    sys.exit(0 if ok else 1)


def main(argv=None):
    store.absolute_env()  # before anything reads a path from the environment or a child inherits it
    a = parser().parse_args(argv)
    try:
        a.f(a)
    except RuntimeError as e:  # wb's own failures (git, a config that doesn't parse): a message
        die(str(e))
    except OSError as e:  # a --spec, --message or --file that isn't there: a line, not a traceback
        die(f"{e.filename}: {e.strerror}" if e.filename else str(e))  # (the 2026-11-16 audit)


def parser() -> argparse.ArgumentParser:
    """wb's commands and flags (main; the bench driver starts its replays through it)."""
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
                   "(default off: each wake costs Claude its whole context)")
    s.add_argument("--self-review", action="store_true",
                   help="once the tests pass, one more automatic round in which the agent "
                   "reviews its own change against Claude's checks, before Claude is woken")
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
    s.add_argument("--test-timeout", type=int, default=900, metavar="SECS",
                   help="seconds the acceptance command may run (default 900)")
    s.add_argument("--no-base-check", action="store_true",
                   help="don't run the acceptance command once on the base before the agent starts")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--force", action="store_true", help="queue the --model asked for even if it "
                   "can't run now")
    s.add_argument("--queue", action="store_true", help="start even when the GPU's holders leave "
                   "no room for its model: it waits behind them (else exit 4)")
    s.add_argument("--trial", action="store_true", help="a check of workbench itself (a scratch "
                   "repo): its result and lessons don't count as evidence")
    s.add_argument("--no-claude-md", action="store_true",
                   help="start even though the repo has no CLAUDE.md (or .claude/CLAUDE.md)")
    s.add_argument("--allow", action="append", metavar="PATTERN",
                   help="let the agent run a command outside the everyday list, e.g. \"curl *\"")
    s.add_argument("--allow-path", action="append", metavar="DIR",
                   help="let the agent read and write this directory outside its lane")
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
    s.add_argument("--spec-gap", action="append", metavar="WHAT",
                   help="this round is the spec's fault: what it missed (repeatable)")
    s.set_defaults(f=cmd_feedback)
    s = sub.add_parser("next", help="commit the approved step and send the same agent the next one")
    s.add_argument("name"); s.add_argument("--spec", required=True, help="the next step's spec file, or - for stdin")
    s.add_argument("--message", "-m", required=True, help="commit subject for the approved step")
    s.add_argument("--test", help="the next step's acceptance command (default: unchanged)")
    s.add_argument("--fresh", action="store_true", help="a fresh session instead of the same one")
    s.add_argument("--take-claude-md", action="store_true",
                   help="put the agent's proposed CLAUDE.md/AGENTS.md (<name>.proposed) over the "
                   "file before committing; without it they are dropped")
    s.add_argument("--spec-gap", action="append", metavar="WHAT",
                   help="a round this step took was the spec's fault: what it missed (repeatable)")
    s.add_argument("--allow", action="append", metavar="PATTERN",
                   help="let the agent run a command outside the everyday list, e.g. \"curl *\"")
    s.add_argument("--skill", action="append", metavar="NAME",
                   help="the skills for the next step (repeatable; replaces the set; default: unchanged)")
    s.set_defaults(f=cmd_next)

    s = sub.add_parser("skill", help="Claude's skill library for the agents: list, show, add, "
                       "patch, scan, import, retire, restore; hand a task a skill or take it back")
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
    s = sub.add_parser("notes", help="what Claude learned about how the user wants delegation "
                       "run, carried into every project")
    s.add_argument("action", nargs="?", choices=["add", "drop"])
    s.add_argument("args", nargs="*", help="the note (add), or its numbers (drop)")
    s.set_defaults(f=cmd_notes)
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
    s.add_argument("--wait", action="store_true", help="wait for the tasks holding the GPU instead "
                   "of refusing (long: run it in the background)")
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
    s.add_argument("--last", type=int, default=0, help=argparse.SUPPRESS)  # with --json: the newest N lines
    s.set_defaults(f=cmd_watch)

    s = sub.add_parser("status", help="open tasks at a glance")
    s.add_argument("name", nargs="?"); s.add_argument("--all", action="store_true")
    s.add_argument("--repo", metavar="DIR", help="only the tasks of a session started in DIR")
    s.add_argument("--session", action="append", metavar="ID",
                   help="only the tasks of Claude Code session ID (repeatable; the mod passes its ids)")
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
    s = sub.add_parser("allow", help="let a task's agent run a command outside the everyday list "
                       "(a shell pattern, e.g. \"curl *\"); answers its request")
    s.add_argument("name"); s.add_argument("pattern", nargs="*")
    s.add_argument("--path", action="append", metavar="DIR",
                   help="let it read and write this directory outside its lane (repeatable)")
    s.add_argument("--always", action="store_true", help="for every task from now on (commands.json)")
    s.add_argument("--text", help="a word for the agent with it")
    s.set_defaults(f=cmd_allow)
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
    s.add_argument("--repo", metavar="DIR", help="with --all: only those of a session started in DIR")
    s.add_argument("--session", action="append", metavar="ID",
                   help="with --all: only those of Claude Code session ID (repeatable)")
    s.set_defaults(f=cmd_takeover)
    s = sub.add_parser("stop", help="stop a running task (wb resume or wb feedback continues it)")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true", help="every running agent")
    s.add_argument("--repo", metavar="DIR", help="with --all: only those of a session started in DIR")
    s.add_argument("--session", action="append", metavar="ID",
                   help="with --all: only those of Claude Code session ID (repeatable)")
    s.set_defaults(f=cmd_stop)
    s = sub.add_parser("resume", help="continue a stopped task in a fresh session (not a review round)")
    s.add_argument("name"); s.set_defaults(f=cmd_resume)
    s = sub.add_parser("accept", help="commit the work on its branch and close the task")
    s.add_argument("name"); s.add_argument("--message", "-m", required=True)
    s.add_argument("--merge", action="store_true"); s.add_argument("--force", action="store_true")
    s.add_argument("--take-claude-md", action="store_true",
                   help="put the agent's proposed CLAUDE.md/AGENTS.md (<name>.proposed) over the "
                   "file before committing; without it they are dropped")
    s.add_argument("--spec-gap", action="append", metavar="WHAT",
                   help="a round this task took was the spec's fault: what it missed (repeatable)")
    s.set_defaults(f=cmd_accept)
    s = sub.add_parser("discard", help="drop the work and close the task")
    s.add_argument("name"); s.add_argument("--reason")
    s.add_argument("--spec-gap", action="append", metavar="WHAT",
                   help="the spec's part in it: what it missed (repeatable)")
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
    s.add_argument("--url", help="add: the baseURL of the OpenAI-compatible server it runs on "
                   "(e.g. http://127.0.0.1:8080/v1), when OpenCode has no provider for it yet")
    s.add_argument("--start", metavar="CMD", help="add: the shell command that starts its server "
                   "(models.json start), for a server that runs one model at a time")
    s.add_argument("--stop", metavar="CMD", help="add: the shell command that stops it (models.json stop)")
    s.add_argument("--label", help="add: the name OpenCode shows")
    s.add_argument("--context", type=int, help="add: context tokens (default: what Ollama serves "
                   "it, else what the provider's other models use; another server needs it)")
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
    s = sub.add_parser("chat", help="the web chat: serve it, its link, reply to the user there, "
                       "the thread (log), receipts (ack), what waits (pending)")
    s.add_argument("action", choices=["serve", "url", "reply", "log", "ack", "pending"], nargs="?",
                   default="log")
    s.add_argument("id", nargs="?", help="ack: the message id")
    s.add_argument("--port", type=int, default=chat.PORT)
    s.add_argument("--bind", action="append", help="serve: an address to listen on, `tailscale` "
                   "or 0.0.0.0 (default: chat.json's `bind`, else localhost)")
    s.add_argument("--host", action="append", help="serve: another host name to answer to (* any)")
    s.add_argument("--text", help="reply: the text"); s.add_argument("--message", help="reply: file, or -")
    s.add_argument("--by", help=argparse.SUPPRESS); s.add_argument("-n", type=int, default=10)
    s.add_argument("--how", choices=["now", "queued"], default="now", help=argparse.SUPPRESS)
    s.set_defaults(f=cmd_chat)
    s = sub.add_parser("init", help="set a project up: git with an initial commit, "
                                    "AGENTS.md → CLAUDE.md")
    s.add_argument("dir"); s.set_defaults(f=cmd_init)
    s = sub.add_parser("bench", help="replay accepted tasks on any model, graded by the tests they "
                                     "were accepted with")
    bs = s.add_subparsers(dest="bench_cmd", required=True)
    x = bs.add_parser("add", help="make a case of one step of an accepted task")
    x.add_argument("task")
    x.add_argument("--step", type=int, help="which step of a wb next task (default: the last)")
    x.add_argument("--hidden", nargs="+", metavar="PATH",
                   help="the files graded with (default: the test files the step changed)")
    x.add_argument("--check", help="a command run after the acceptance command, to pass too")
    x.add_argument("--name", help="the case's name (default: the task's)")
    bs.add_parser("list", help="the cases")
    x = bs.add_parser("run", help="replay the cases in the background, one at a time")
    x.add_argument("--model", nargs="+", help="default: every model that can run and takes the case's size")
    x.add_argument("--case", nargs="+", help="default: every case")
    x.add_argument("--skill", nargs="+", help="hand these library skills to every replay")
    x.add_argument("--label", help="a name for this variant (a workbench branch, a skill), to compare by")
    x.add_argument("--repeat", type=int, default=1, help="replay each N times")
    bs.add_parser("stop", help="stop the run, closing its replay")
    x = bs.add_parser("report", help="pass rates by model and label, and each case")
    x.add_argument("--run")
    x.add_argument("--label")
    s.set_defaults(f=cmd_bench)
    sub.add_parser("stats", help="results by model").set_defaults(f=cmd_stats)
    sub.add_parser("tune", help="the settings tuned from results, per model and window").set_defaults(f=cmd_tune)
    s = sub.add_parser("trial", help="mark closed tasks as trial runs: their results stop counting")
    s.add_argument("names", nargs="+")
    s.set_defaults(f=cmd_trial)
    s = sub.add_parser("setup", help="link wb onto PATH and /workbench into Claude Code's skills, "
                                     "from this copy")
    s.add_argument("--force", action="store_true", help="replace links that point elsewhere")
    s.set_defaults(f=cmd_setup)
    s = sub.add_parser("update", help="update workbench (wb, the skill and the mod) from its origin")
    s.add_argument("--check", action="store_true", help="only say whether there is an update")
    s.add_argument("--if-due", action="store_true", help="with --check: ask origin at most weekly")
    s.set_defaults(f=cmd_update)
    sub.add_parser("doctor", help="check the setup").set_defaults(f=cmd_doctor)
    return ap


if __name__ == "__main__":
    main()
