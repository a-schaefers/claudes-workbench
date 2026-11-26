"""wb schedule (#1204): a backlog that starts on a timer, with no Claude Code session open.

The night itself was built already (an overnight backlog: `wb start … --queue --backlog`, the
keep-awake inhibitors, the web chat, `wb backlog` as the morning summary); what was missing is the
trigger. `wb schedule add` writes the user's own timer, a systemd user .timer/.service pair (Linux,
WSL2 with systemd) or a launchd agent (macOS), whose one command is `wb schedule fire <name>`:
under a lock, and only when the day cap leaves an API role room, it runs Claude Code headless
(`claude -p`) on the backlog file, with wb commands and read-only tools alone, and writes one row
per fire to schedules/<name>/runs.jsonl: the run history and its costs.

A schedule of a flow (`--flow`, #1295) needs no Claude at night: each fire runs `wb run start
<name>-YYMMDD --flow … --overnight --budget-usd N` itself, and the run's own driver, budget and
final stage (Claude's, in the morning) do the rest; its row names the run.

Not OpenCode's own automations: they run OpenCode sessions from its server, with no Claude spec
or review, outside wb's runner and so outside the lane and the keyhole.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import meter, models, routes, store
from .runner import PKG_ROOT

# a schedule's name, short enough that its batch, `<name>-YYYY-MM-DD` or a second fire's that day
# `<name>-YYYY-MM-DD-2` (up to -99: batch_name), is a backlog name (store.NAME_RE)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,33}\Z")
# a flow schedule's (#1295) is shorter: its run, `<name>-YYMMDD[-NN]`, is a run name (runs.RUN_RE, 24)
FLOW_NAME_MAX = 14
SAME_DAY = 99         # fires on one date, each its own batch
TURNS = 300           # claude -p's --max-turns for one fire, unless `wb schedule add --max-turns`
FIRE_HOURS = 12       # a fire's Claude is stopped past this, whatever its turns
UNIT = "wb-schedule-"             # systemd: wb-schedule-<name>.timer / .service
LABEL = "workbench.schedule."     # launchd: workbench.schedule.<name>
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LAUNCHD_DAY = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
# What headless Claude may use: wb, and reading. No Edit or Write (a spec goes to `wb start --spec -`
# on stdin), no git or gh: `dontAsk` refuses whatever isn't allowed rather than prompting nobody,
# whatever the user's own default mode is; `--tools` takes the rest out of its tool list. The
# user's own allow rules still add to `--allowedTools` (Claude Code merges them), so the two that
# reach outside the machine are denied by name too: a deny wins.
TOOLS = "Bash,Read,Grep,Glob"
ALLOWED = ("Bash(wb:*)", "Read", "Grep", "Glob")
DENIED = ("Bash(git push:*)", "Bash(gh:*)")
# Claude Code's own marks: inherited from a session the fire was started from (`wb schedule run`),
# CLAUDECODE made claude refuse to start nested, and the session id would have made the night's
# tasks that session's rather than headless Claude's; an OpenCode driver's session id the same
# (#1309: headless Claude's CLAUDECODE outranks it, but bench and optimize drop it too)
CLAUDE_MARKS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT",
                "OPENCODE_SESSION_ID")
PROMPT = """\
A scheduled workbench run, `{name}`, with nobody at the terminal: Claude Code runs headless (claude -p).
First read the /workbench skill, {skill}, and in its reference/driving.md the sections *Overnight:
a backlog left to run* and *Headless: a scheduled run*; follow them. You may run wb commands and
read files, nothing else.
The batch: --backlog {batch}. The repository: {repo}.
The backlog ({file}):

{text}
"""


class ScheduleError(RuntimeError):
    pass


def _macos() -> bool:
    return sys.platform == "darwin"


def root() -> Path:
    return store.home() / "schedules"


def sched_dir(name: str) -> Path:
    return root() / name


def read(name: str) -> dict | None:
    return store.read_json(sched_dir(name) / "schedule.json", None)


def names() -> list[str]:
    try:
        return sorted(p.name for p in root().iterdir() if (p / "schedule.json").is_file())
    except OSError:
        return []


def runs(name: str) -> list[dict]:
    return [r for r in store.read_jsonl(sched_dir(name) / "runs.jsonl")[0] if isinstance(r, dict)]


def lock_path(name: str) -> Path:
    # in $WB_HOME/locks, which no lane reads (store.task_lock's reason)
    return store.home() / "locks" / f"schedule-{name}.fire.lock"


def running(name: str) -> dict | None:
    """The fire under way (schedules/<name>/fire.json), when its lock is held; else None."""
    path = lock_path(name)
    if not path.exists():
        return None
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return store.read_json(sched_dir(name) / "fire.json", {}) or {"started": None}
        fcntl.flock(fh, fcntl.LOCK_UN)
    return None


# --- When: "Sun 02:00", or systemd's own OnCalendar ------------------------------------------------

def _day(word: str) -> str:
    # the day's name or a start of it (`Sun`, `Sunday`, `thurs`): not any text that starts with
    # one, or `Mon *` read as Monday and systemd's OnCalendar after it never seen
    for d, full in zip(DAYS, ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")):
        if len(word) >= 3 and full.startswith(word.lower()):
            return d
    raise ScheduleError(f"{word!r} is not a day (Mon … Sun)")


def _days(text: str) -> list[str]:
    """`Sun`, `Mon,Wed`, `Mon..Fri` (or `Mon-Fri`), `daily` → the days in week order ([] daily)."""
    if text.lower() in ("", "daily", "*"):
        return []
    out = []
    for part in text.split(","):
        ends = re.split(r"\.\.|-", part.strip())
        if len(ends) == 1:
            out.append(_day(ends[0]))
        elif len(ends) == 2:
            a, b = DAYS.index(_day(ends[0])), DAYS.index(_day(ends[1]))
            out += [DAYS[i % 7] for i in range(a, (b if b >= a else b + 7) + 1)]
        else:
            raise ScheduleError(f"{part!r} is not a day or a range of days")
    return [d for d in DAYS if d in out]


def parse_at(text: str) -> dict:
    """--at: `[DAYS] HH:MM` (`Sun 02:00`, `Mon..Fri 01:30`, `daily 03:00`, `02:00`), the one form
    both systemd and launchd take, as {said, calendar (OnCalendar), launchd (StartCalendarInterval)};
    on Linux any other text is systemd's own OnCalendar (`systemd-analyze calendar` checks it), with
    no launchd form. ScheduleError when neither."""
    text = " ".join(text.split())
    m = re.fullmatch(r"(?:(.+?)\s+)?([01]?\d|2[0-3]):([0-5]\d)", text)
    try:
        days = _days(m[1] or "") if m else None
    except ScheduleError as e:
        # systemd's own forms end in HH:MM too (`*-*-* 02:00`, `*-*-01 03:00`, `Mon *-*-* 02:00`):
        # read as [DAYS] they were turned away before systemd-analyze saw them (the review of #1204)
        if _macos():
            raise
        days, not_days = None, e
    else:
        not_days = None
    if days is not None:
        hh, mm = int(m[2]), int(m[3])
        cal = (",".join(days) + " " if days else "") + f"*-*-* {hh:02d}:{mm:02d}:00"
        when = [{"Hour": hh, "Minute": mm, **({"Weekday": LAUNCHD_DAY[d]} if days else {})}
                for d in (days or [None])]
        said = (",".join(days) if days else "daily") + f" {hh:02d}:{mm:02d}"
        return {"said": said, "calendar": cal, "launchd": when}
    if _macos():
        raise ScheduleError(f"{text!r}: on macOS --at is [DAYS] HH:MM (Sun 02:00, Mon..Fri 01:30, daily 03:00)")
    if not shutil.which("systemd-analyze"):
        raise ScheduleError(f"{text!r} is not [DAYS] HH:MM{f' ({not_days})' if not_days else ''}, and no "
                            "systemd-analyze here checks it as OnCalendar")
    r = subprocess.run(["systemd-analyze", "calendar", text], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise ScheduleError(f"{text!r} is neither [DAYS] HH:MM{f' ({not_days})' if not_days else ''} nor "
                            f"an OnCalendar systemd takes: {(r.stderr or r.stdout).strip()[:200]}")
    return {"said": text, "calendar": text, "launchd": None}


# --- The units ---------------------------------------------------------------------------------

def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def plist_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}{name}.plist"


def unit_files(name: str) -> list[Path]:
    if _macos():
        return [plist_path(name)]
    return [unit_dir() / f"{UNIT}{name}.timer", unit_dir() / f"{UNIT}{name}.service"]


# A fire from OpenCode's install: the newest generation's bin/wb under the cache key, found at
# fire time (newest_wb's, standard library only: it runs before any wb). A unit naming the adding
# generation's ran its code until OpenCode collected it, then exited 2 unseen (#1497, 2026-11-26)
_NEWEST = ("import glob,os,sys; k,r,*a=sys.argv[1:]; "
           "f=sorted(glob.glob(os.path.join(glob.escape(k),'*',r)),"
           "key=lambda p:os.path.relpath(p,k).split(os.sep)[0].zfill(20)); "
           "f or sys.exit('no Workbench left under '+k+': OpenCode removed its install'); "
           "os.execv(sys.executable,[sys.executable,'-X','utf8',f[-1],*a])")


def unit_wb() -> dict:
    """The wb a schedule's unit runs, kept in schedule.json: `wb`, this copy's bin/wb; from
    OpenCode's install, whose every update is a new generation `<key>/<n>/node_modules/<package>`
    and which removes the old ones, also `wb_cache`: [the key's directory, bin/wb's path under a
    generation] (#1497)."""
    from . import ocplugin
    root = PKG_ROOT.resolve()
    out: dict = {"wb": str(PKG_ROOT / "bin" / "wb")}
    key = root.parent.parent.parent
    if ocplugin.runs_from_cache(root) and root.parent.name == "node_modules" and any(
            key != c.resolve() and key.is_relative_to(c.resolve()) for c in ocplugin.package_caches()):
        out["wb_cache"] = [str(key), str(Path("node_modules", root.name, "bin", "wb"))]
    return out


def newest_wb(s: dict) -> Path | None:
    """The wb a schedule's fire would run now (_NEWEST's choice for a cache install), None when
    it is gone; a schedule from before #1497 names none: this copy's."""
    if cache := s.get("wb_cache"):
        key, rel = cache
        found = sorted(Path(key).glob(f"*/{rel}"), key=lambda p: p.relative_to(key).parts[0].zfill(20))
        return found[-1] if found else None
    wb = Path(s.get("wb") or PKG_ROOT / "bin" / "wb")
    return wb if wb.exists() else None


def fire_argv(name: str, s: dict | None = None) -> list[str]:
    """What the unit runs: this Python and this clone's bin/wb, by full path, so neither the unit's
    PATH nor a missing ~/.local/bin link decides which wb fires; from OpenCode's install the newest
    generation's (``s``'s `wb_cache`, _NEWEST)."""
    w = s if s is not None else read(name) or unit_wb()
    if cache := w.get("wb_cache"):
        return [sys.executable, "-X", "utf8", "-c", _NEWEST, *cache, "schedule", "fire", name]
    return [sys.executable, "-X", "utf8", w.get("wb") or str(PKG_ROOT / "bin" / "wb"), "schedule", "fire", name]


def unit_env() -> dict:
    """The unit's environment: this PATH (Claude Code's, where claude, opencode and git were found;
    a user manager's is /usr/local/bin:/usr/bin) and the WB_* and config paths set here
    (store.PATH_VARS). Paths only: never a key."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    env.update({v: os.environ[v] for v in store.PATH_VARS if os.environ.get(v)})
    return env


def _sd_quote(word: str, exec_line: bool = False) -> str:
    """One quoted word of a systemd unit line: `%` is a specifier everywhere, `$` a variable only
    in ExecStart (``exec_line``); Environment= takes `$` as it is."""
    word = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (word.replace("$", "$$") if exec_line else word) + '"'


def systemd_units(name: str, s: dict) -> dict[str, str]:
    env = "".join(f"Environment={_sd_quote(f'{k}={v}')}\n" for k, v in s["env"].items())
    what = f"a run of flow {s['flow']}" if s.get("flow") else f"Claude runs the backlog {s['file']}"
    service = (f"[Unit]\nDescription=workbench schedule {name}: {what.replace('%', '%%')}\n\n"
               "[Service]\nType=oneshot\n"
               f"WorkingDirectory={s['repo'].replace('%', '%%')}\n{env}"  # a path, never quoted
               f"ExecStart={' '.join(_sd_quote(w, exec_line=True) for w in fire_argv(name, s))}\n"
               # a night's run: no start timeout (FIRE_HOURS bounds it); the runners it starts
               # live on in scopes of their own (cli.detach), and KillMode=process keeps them where
               # systemd-run can't make one
               "TimeoutStartSec=infinity\nKillMode=process\n")
    timer = (f"[Unit]\nDescription=workbench schedule {name}: {s['at']['said'].replace('%', '%%')}\n\n"
             f"[Timer]\nOnCalendar={s['at']['calendar']}\n"
             # a fire missed while the machine was off or asleep runs once when it is back
             "Persistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return {f"{UNIT}{name}.service": service, f"{UNIT}{name}.timer": timer}


def launchd_plist(name: str, s: dict) -> bytes:
    when = s["at"]["launchd"]
    log = str(sched_dir(name) / "fire.log")
    return plistlib.dumps({"Label": f"{LABEL}{name}", "ProgramArguments": fire_argv(name, s),
                           "EnvironmentVariables": s["env"], "WorkingDirectory": s["repo"],
                           "StartCalendarInterval": when[0] if len(when) == 1 else when,
                           "StandardOutPath": log, "StandardErrorPath": log})


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stderr or r.stdout).strip()


def install(name: str, s: dict) -> None:
    """Write the unit and have the service manager take it; ScheduleError, files undone, when it
    won't."""
    if _macos():
        if not shutil.which("launchctl"):
            raise ScheduleError("no launchctl here")
        path = plist_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(launchd_plist(name, s))
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}{name}"])  # an earlier one, if any
        code, said = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        if code:
            path.unlink(missing_ok=True)
            raise ScheduleError(f"launchctl bootstrap refused it: {said[:300]}")
        return
    if not shutil.which("systemctl"):
        raise ScheduleError(NO_SYSTEMD.format(name=name, why="no systemctl"))
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for file, text in systemd_units(name, s).items():
        (d / file).write_text(text, encoding="utf-8")
        written.append(d / file)
    for argv in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", f"{UNIT}{name}.timer"]):
        code, said = _run(argv)
        if code:
            for p in written:
                p.unlink(missing_ok=True)
            _run(["systemctl", "--user", "daemon-reload"])
            raise ScheduleError(NO_SYSTEMD.format(name=name, why=f"{' '.join(argv[1:])}: {said[:300]}"))


NO_SYSTEMD = ("no systemd user manager takes the timer here ({why}): wb schedule needs one (WSL2: "
              "systemd=true under [boot] in /etc/wsl.conf), or run `wb schedule fire {name}` from your "
              "own cron")


def uninstall(name: str) -> None:
    if _macos():
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}{name}"])
        plist_path(name).unlink(missing_ok=True)
        return
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", f"{UNIT}{name}.timer"])
    for p in unit_files(name):
        p.unlink(missing_ok=True)
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"])


def linger_note() -> str | None:
    """A user manager without linger stops at the last logout, its timers with it."""
    if _macos():
        return ("launchd runs it while you are logged in (a fire missed asleep runs at wake, one "
                "missed logged out does not)")
    from .cli import _loginctl
    if not shutil.which("loginctl"):
        return None
    user = os.environ.get("USER") or str(os.getuid())
    if _loginctl("show-user", user, "-p", "Linger", "--value") == "no":
        return (f"logind stops your user manager at logout, and the timer with it: loginctl "
                f"enable-linger {user} keeps it (a fire missed meanwhile runs once at the next login)")
    return None


# --- The commands --------------------------------------------------------------------------------

def _flow_check(name: str, flow_name: str, params: list[str], budget: float | None, top: Path) -> tuple[dict, list[str]]:
    """A flow schedule's checks at add (#1295): the flow by name (the user's or a shipped one; a
    repo's never), its params against the repo's HEAD, a budget. (its budget, the notes to print)."""
    from . import gitops, runs
    if len(name) > FLOW_NAME_MAX:
        raise ScheduleError(f"a flow schedule's name is at most {FLOW_NAME_MAX} characters: its runs, "
                            "<name>-YYMMDD[-N], are run names (at most 24)")
    try:
        flow = runs.load_flow(flow_name)
        head = gitops.base_commit(top, "HEAD")
        runs.parse_params(flow, params, top, head)
    except RuntimeError as e:
        raise ScheduleError(str(e)) from None
    usd = budget if budget is not None else flow.get("budget_usd")
    if usd is None:
        raise ScheduleError(f"flow {flow_name} has no budget_usd: --budget-usd sets each run's dollars "
                            "(0 for a GPU-only night)")
    if usd < 0:
        raise ScheduleError("--budget-usd is 0 or more")
    notes = [f"note: stage {s['id']} is a checkpoint of Claude's: each run waits there until you open a session"
             for s in runs.stages(flow) if s.get("claude")]
    return {"flow": flow_name, "params": list(params), "budget_usd": usd}, notes


def add(name: str, file: str | None, repo: str, at: str, turns: int | None = None, flow: str | None = None,
        params: list[str] | None = None, budget_usd: float | None = None) -> list[str]:
    """`wb schedule add`: schedule.json and the unit. Returns the lines to print."""
    from . import gitops
    if not NAME_RE.match(name):
        raise ScheduleError("a schedule name is lowercase letters, digits and hyphens, at most 34 "
                            "(its batch, <name>-YYYY-MM-DD[-N], is a backlog name)")
    if read(name) is not None:
        raise ScheduleError(f"schedule {name!r} exists: wb schedule rm {name} first")
    if bool(file) == bool(flow):
        raise ScheduleError("--file <backlog.md> or --flow <name>, one of them")
    if not flow and (params or budget_usd is not None):
        raise ScheduleError("--param and --budget-usd go with --flow")
    path = None
    if file:
        path = Path(file).expanduser().resolve()
        if not path.is_file():
            raise ScheduleError(f"{file}: no such file (the backlog: what Claude is to run, as you'd tell it)")
        if not path.read_text(encoding="utf-8").strip():
            raise ScheduleError(f"{file} is empty")
    try:
        top = gitops.toplevel(Path(repo).expanduser().resolve())
    except gitops.GitError:
        raise ScheduleError(f"{repo} is not inside a git repository") from None
    if turns is not None and (turns < 1 or flow):
        raise ScheduleError("--max-turns goes with --file (Claude's turns), and is at least 1")
    fl, notes = _flow_check(name, flow, params or [], budget_usd, top) if flow else ({}, [])
    s = {"name": name, **({"file": str(path)} if path else fl), "repo": str(top), "at": parse_at(at),
         **({} if flow else {"max_turns": turns or TURNS}), "env": unit_env(), **unit_wb(), "created_at": store.now()}
    d = sched_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    try:
        install(name, s)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    store.write_json(d / "schedule.json", s)
    if flow:
        out = [f"scheduled {name}: {s['at']['said']} · {top} · flow {flow}",
               f"each fire: wb run start {name}-YYMMDD --flow {flow} --overnight --budget-usd "
               f"{s['budget_usd']:g} (-2 for a second that day), its own driver, no Claude; the run waits "
               "at its final stage for you (wb run status; wb schedule list shows its runs)", *notes]
    else:
        out = [f"scheduled {name}: {s['at']['said']} · {top} · {path}",
               f"each fire: claude -p on the backlog as batch {name}-YYYY-MM-DD (-2 for a second that day), "
               f"wb commands and reading "
               f"only, at most {s['max_turns']} turns; wb schedule list shows its runs"]
    missing = [p for p in needs(s) if not shutil.which(p, path=s["env"]["PATH"])]
    if missing:
        out.append(f"note: {' and '.join(missing)} not on this PATH, which the unit runs with: "
                   "a fire fails until they are")
    if note := linger_note():
        out.append(f"note: {note}")
    return out


def needs(s: dict) -> tuple[str, ...]:
    """What a fire runs, which the unit's PATH must find: a flow's run needs no Claude (#1295)."""
    return ("opencode",) if s.get("flow") else ("claude", "opencode")


def remove(name: str) -> str:
    if read(name) is None:
        raise ScheduleError(f"no schedule {name!r} (wb schedule list)")
    if (r := running(name)) is not None:
        raise ScheduleError(f"{name}'s fire is running (pid {r.get('pid', '?')}): wait for it, or "
                            f"stop it (kill {r.get('pid', '<pid>')}), then wb schedule rm {name}")
    uninstall(name)
    shutil.rmtree(sched_dir(name), ignore_errors=True)
    lock_path(name).unlink(missing_ok=True)
    return f"removed schedule {name} (its timer too; the tasks it started stay)"


def summary(row: dict) -> str:
    """One runs.jsonl row as a line: when, what happened, its tasks, its API dollars, Claude's tokens."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("ts") or 0))
    if row.get("outcome") == "skipped":
        return f"{when} skipped: {row.get('why')}"
    if row.get("run"):  # a flow's fire (#1295): its run, as it stands now
        from . import runs as runs_
        if row.get("outcome") != "started":
            return f"{when} {row.get('outcome')}: run {row['run']}" + (f" ({row['why']})" if row.get("why") else "")
        r = runs_.Run(row["run"])
        live = runs_.status_line(row["run"]).split("\n")[0] if r.exists else f"run {row['run']} (gone)"
        return f"{when} started {live}"
    phases: dict = {}
    for t in row.get("tasks") or []:
        phases[t.get("phase")] = phases.get(t.get("phase"), 0) + 1
    tasks = (f"{len(row.get('tasks') or [])} task(s)"
             + (f" ({', '.join(f'{n} {p}' for p, n in phases.items())})" if phases else ""))
    parts = [f"{when} {row.get('outcome')}" + (f" ({row['why']})" if row.get("why") else ""),
             f"batch {row.get('batch')}", tasks]
    if row.get("api_usd"):
        parts.append(f"API {meter.usd(row['api_usd'])}")
    if c := row.get("claude"):
        parts.append(f"Claude {c.get('turns', 0)} turns, "
                     f"{_tok(sum(c.get(k) or 0 for k in ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')))} tokens")
    return " · ".join(parts)


def _tok(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def listing() -> list[str]:
    out = []
    for name in names():
        s = read(name) or {}
        what = f"flow {s['flow']}" if s.get("flow") else s.get("file")
        out.append(f"{name}: {(s.get('at') or {}).get('said')} · {s.get('repo')} · {what}")
        if (r := running(name)) is not None:
            since = r.get("started")
            out.append("  running" + (f" since {time.strftime('%H:%M', time.localtime(since))}" if since else "")
                       + (f", batch {r['batch']}" if r.get("batch") else ""))
        rows = runs(name)
        out.append(f"  last: {summary(rows[-1])}" if rows else "  never fired yet")
    return out or ["no schedules (wb schedule add <name> --file <backlog.md> | --flow <flow> --repo DIR --at 'Sun 02:00')"]


def run_now(name: str) -> str:
    """`wb schedule run`: a fire now, detached (it lasts the night; Claude's Bash stops at 2 min)."""
    from .cli import detach
    if read(name) is None:
        raise ScheduleError(f"no schedule {name!r} (wb schedule list)")
    if running(name) is not None:
        raise ScheduleError(f"{name}'s fire is running already (wb schedule list)")
    log = sched_dir(name) / "fire.log"
    with open(log, "a") as fh:
        detach(fire_argv(name), cwd=read(name)["repo"], stdout=fh, stderr=fh, env=_fire_env(name))
    return f"fired {name} (wb schedule list shows it; its log: {log})"


def _fire_env(name: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CLAUDE_MARKS}
    env["WB_SCHEDULE"] = name  # wb accept --merge refuses under it
    return env


def spend_skip() -> str | None:
    """Why tonight's fire is skipped for money: an API role (routes.effective_via) that meter.refusal
    refuses: no day cap at all (meter.no_cap), today's spend at it, or no price. None when every
    role is on the GPU, or each API role has room. routes.json unreadable is a skip too."""
    try:
        doc = routes.read()
    except routes.RoutesError as e:
        return f"routes.json can't be read ({e})"
    for role in routes.ROLES:
        mid = doc["roles"][role].get("api")
        if routes.effective_via(doc, role) != "api" or not mid:
            continue
        try:
            m = models.lookup(mid, cached=True)
        except Exception:
            m = None
        if m is not None:
            why = meter.refusal(m, role, advice=False)
        elif (cap := meter.cap()) is None:
            why = f"{role} role: {meter.no_cap()}"
        elif (t := meter.today()["usd"]) >= cap:
            why = f"{role} role: API spend today is {meter.usd(t)} of the {meter.usd(cap)} day cap"
        else:
            why = None
        if why:
            return why
    return None


def _row(name: str, row: dict) -> dict:
    return store.append_jsonl(sched_dir(name) / "runs.jsonl", row)


def batch_name(name: str, started: float) -> str:
    """This fire's batch: `<name>-YYYY-MM-DD`, or `-2`, `-3` … after it when an earlier fire that
    day had the name (a task in it, or a runs.jsonl row that wasn't a skip). One name for every
    fire on a date gave the second the first's tasks to review and count again in its row, or,
    with those closed 6 h, every `wb start --backlog` refused (cli._backlog_done; the review of
    #1204). Called under the fire's lock: two fires never pick one name."""
    base = f"{name}-{time.strftime('%Y-%m-%d', time.localtime(started))}"
    used = {r.get("batch") for r in runs(name) if r.get("outcome") != "skipped"}
    used |= {t.spec.get("backlog") for t in store.all_tasks(archived=True)}
    for n in range(1, SAME_DAY + 1):
        if (batch := base if n == 1 else f"{base}-{n}") not in used:
            return batch
    raise ScheduleError(f"{name}: {SAME_DAY} fires on one date already")


def run_name(name: str, started: float) -> str:
    """A flow fire's run (#1295): `<name>-YYMMDD`, or `-2` … `-99` after it once a run had the
    name (run names are never reused, runs.taken). Under the fire's lock, as batch_name."""
    from . import runs as runs_
    base = f"{name}-{time.strftime('%y%m%d', time.localtime(started))}"
    for n in range(1, SAME_DAY + 1):
        if not runs_.taken(r := base if n == 1 else f"{base}-{n}"):
            return r
    raise ScheduleError(f"{name}: {SAME_DAY} fires on one date already")


def _fire_flow(name: str, s: dict, started: float) -> int:
    """A flow's fire (#1295): `wb run start` itself, as the user would, with no Claude: the run's
    own driver, budget and `--overnight` carry the night, and its final stage waits for Claude in
    the morning (claimed by its directory, the repo: the run has no session)."""
    from . import runs as runs_
    last = next((r for r in reversed(runs(name)) if r.get("run")), None)
    if last and (lr := runs_.Run(last["run"])).exists and lr.open() and runs_.driver_alive(lr):
        _row(name, {"ts": started, "outcome": "skipped", "why": f"run {last['run']} is still going"})
        print(f"{name}: skipped, run {last['run']} is still going")
        return 0
    if why := spend_skip():
        _row(name, {"ts": started, "outcome": "skipped", "why": why})
        print(f"{name}: skipped: {why}")
        return 0
    try:
        run = run_name(name, started)
    except ScheduleError as e:
        _row(name, {"ts": started, "outcome": "skipped", "why": str(e).split(": ", 1)[-1]})
        print(f"{e}: skipped")
        return 0
    argv = [sys.executable, "-X", "utf8", str(PKG_ROOT / "bin" / "wb"), "run", "start", run, "--flow", s["flow"],
            "--repo", s["repo"], "--overnight", "--budget-usd", f"{float(s.get('budget_usd') or 0):g}"]
    for kv in s.get("params") or ():
        argv += ["--param", kv]
    try:
        p = subprocess.run(argv, cwd=s["repo"], env=_fire_env(name), capture_output=True, text=True, timeout=115)
        ok, said = p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr or p.stdout).strip()
    except (OSError, subprocess.SubprocessError) as e:
        ok, said = False, str(e)
    row = {"ts": started, "run": run, "flow": s["flow"], "outcome": "started" if ok else "error",
           **({} if ok else {"why": said.removeprefix("wb: ")[:300]}), "end": store.now()}
    _row(name, row)
    print(f"{name}: {summary(row)}")
    return 0 if ok else 1


def _tasks_of(batch: str) -> tuple[list[dict], float | None]:
    """The batch's tasks and their phases, and the API dollars of the closed ones (savings.api_totals)."""
    from . import savings
    ts = [t for t in store.all_tasks(archived=True) if t.spec.get("backlog") == batch]
    rows = {r.get("task"): r for r in store.results(trials=True)}
    api = savings.api_totals([r for t in ts if (r := rows.get(t.name))])
    return ([{"task": t.name, "phase": t.status.get("phase", "?")} for t in ts],
            round(api["cost"], 4) if api["tasks"] else None)


def fire(name: str) -> int:
    """`wb schedule fire`, the unit's command: one headless Claude run on the backlog, one runs.jsonl
    row. Skips (a row saying why) while the last fire lives, or for money (spend_skip)."""
    s = read(name)
    if s is None:
        print(f"wb schedule fire: no schedule {name!r}", file=sys.stderr)
        return 1
    started = store.now()
    lock = lock_path(name)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # no batch of its own: it starts nothing
            _row(name, {"ts": started, "outcome": "skipped", "why": "the last fire is still running"})
            print(f"{name}: skipped, the last fire is still running")
            return 0
        if s.get("flow"):
            try:
                return _fire_flow(name, s, started)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        try:
            batch = batch_name(name, started)
        except ScheduleError as e:
            _row(name, {"ts": started, "outcome": "skipped", "why": str(e).split(": ", 1)[-1]})
            print(f"{e}: skipped")
            fcntl.flock(fh, fcntl.LOCK_UN)
            return 0
        try:
            return _fire(name, s, started, batch)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _fire(name: str, s: dict, started: float, batch: str) -> int:
    from . import bench, cli, media
    if why := spend_skip():
        _row(name, {"ts": started, "outcome": "skipped", "batch": batch, "why": why})
        print(f"{name}: skipped: {why}")
        return 0
    row = {"ts": started, "batch": batch, "outcome": "error"}
    claude = shutil.which("claude")
    text = None
    try:
        text = Path(s["file"]).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        row["why"] = f"the backlog file: {e}"
    if text is not None and not claude:
        row["why"] = "claude (Claude Code) isn't on the unit's PATH"
    if "why" in row:
        _row(name, {**row, "end": store.now()})
        print(f"{name}: {row['why']}", file=sys.stderr)
        return 1
    store.write_json(sched_dir(name) / "fire.json", {"pid": os.getpid(), "started": started, "batch": batch})
    # a stop (systemctl stop, a kill) ends Claude's whole group through run_group, and the row is written
    old = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    cli.keep_awake(os.getpid(), f"schedule {name}")  # claude -p between tasks too, not only runners
    prompt = PROMPT.format(name=name, skill=PKG_ROOT / "skill" / "workbench" / "SKILL.md", batch=batch,
                           repo=s["repo"], file=s["file"], text=text.strip())
    argv = [claude, "-p", "--output-format", "json", "--max-turns", str(s.get("max_turns") or TURNS),
            "--permission-mode", "dontAsk", "--tools", TOOLS, "--allowedTools", *ALLOWED,
            "--disallowedTools", *DENIED, "--", prompt]
    try:
        p = media.run_group(argv, FIRE_HOURS * 3600, cwd=s["repo"], env=_fire_env(name),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (sched_dir(name) / "last.json").write_text(p.stdout or "", encoding="utf-8")
        row["exit"] = p.returncode
        try:
            result, calls = bench._self_result(json.loads(p.stdout))
        except ValueError:
            row["why"] = (f"claude -p exited {p.returncode} without a JSON result: "
                          f"{(p.stderr or p.stdout or '').strip()[:200]}")
        else:
            row["claude"] = bench._self_usage(result, calls)
            if p.returncode == 0 and not result.get("is_error"):
                row["outcome"] = "ran"
            else:
                row["why"] = str(result.get("subtype") or f"exit {p.returncode}")
    except subprocess.TimeoutExpired:
        row["why"] = f"stopped after {FIRE_HOURS} h"
    except SystemExit:
        row["why"] = "stopped by a signal"
    finally:
        signal.signal(signal.SIGTERM, old)
        tasks, usd = _tasks_of(batch)
        _row(name, {**row, "end": store.now(), "tasks": tasks, **({"api_usd": usd} if usd is not None else {})})
        (sched_dir(name) / "fire.json").unlink(missing_ok=True)
    print(f"{name}: {summary(runs(name)[-1])}")
    return 0 if row["outcome"] == "ran" else 1


def doctor_lines() -> list[tuple[bool, str]]:
    """wb doctor's: each schedule, its last run, its unit, and whether its PATH finds claude and
    opencode and its wb is there."""
    out = []
    for name in names():
        s = read(name) or {}
        rows = runs(name)
        last = summary(rows[-1]) if rows else "never fired yet"
        path = (s.get("env") or {}).get("PATH", "")
        gaps = [f"{p} not on its PATH" for p in needs(s) if not shutil.which(p, path=path)]
        if newest_wb(s) is None:  # the unit's own, not this wb's (#1497): it said ok while each fire exited 2
            where = s["wb_cache"][0] if s.get("wb_cache") else s.get("wb") or PKG_ROOT / "bin" / "wb"
            gaps.append(f"its wb is gone ({where}): wb schedule rm {name}, then add")
        if not all(p.exists() for p in unit_files(name)):
            gaps.append("its unit is gone (wb schedule rm, then add)")
        out.append((not gaps, f"schedule {name} ({(s.get('at') or {}).get('said')}): last {last}"
                    + (f"; {'; '.join(gaps)}" if gaps else "")))
    return out


def cmd(a) -> None:
    """wb schedule add | list | rm | run | fire."""
    from .cli import die
    try:
        if a.action == "add":
            print("\n".join(add(a.name, a.file, a.repo, a.at, a.max_turns, a.flow, a.param, a.budget_usd)))
        elif a.action == "list":
            print("\n".join(listing()))
        elif a.action == "rm":
            print(remove(a.name))
        elif a.action == "run":
            print(run_now(a.name))
        elif a.action == "fire" and (code := fire(a.name)):
            sys.exit(code)
    except ScheduleError as e:
        die(str(e))

