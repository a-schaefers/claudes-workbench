"""The detached process that drives one task's rounds until it needs Claude.

    python3 -m workbench.runner <task>

Each round sends rounds/<n>/prompt.md to the agent (`opencode run`, continuing the task's session
after the first, unless that session is full: see fresh_reason), with the skills note
(skills_note) before it, streams its events into events.jsonl and status.json, and stops it if it
loops, errors repeatedly, goes silent or runs too long. After a clean round the task's test
command runs in the worktree, in the agent's lane; a failure goes back to the agent on its own while auto-fix rounds remain, so
Claude is only woken for work that passes, or for a task that has run out of road.
"""
import contextlib
import ctypes
import hashlib
import json
import os
import queue
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zlib
from collections import deque
from pathlib import Path

from . import (ask_mcp, context, gitguard, gitops, hardware, lane, lessons, models, progress, skills,
               stats, store, tuning)
from .events import StuckDetector, normalize
from .watch import Watcher

PKG_ROOT = Path(__file__).resolve().parent.parent
CURRENT: dict = {}  # the agent process of the round in flight, for the SIGTERM handler
# The run ids this process gave out (WB_RUN: a round's agent, each test, setup or check run) and
# not yet swept: all its exit's backstop may end (#504).
ISSUED: set[str] = set()


def _issue() -> str:
    run = uuid.uuid4().hex
    ISSUED.add(run)
    return run
# Keep build droppings out of the worktree, so the review diff is only the agent's work.
CLEAN_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


class Stopped(BaseException):  # not an Exception: no `except Exception` on the way may swallow a stop
    pass


SIGNALLED: list = []  # when the SIGTERM came: a shutdown's runners are batched by it (#544)


def _on_term(signum, frame):
    SIGNALLED.append(time.time())
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # one stop is enough: a second (systemd sends
    proc = CURRENT.get("proc")                      # more) must not cut the cleanup short
    if proc and proc.poll() is None:
        _stop(proc)  # the agent (or the tests) runs in its own process group; take it down with us
    raise Stopped()


def opencode_bin() -> list[str]:
    fake = os.environ.get("WB_OPENCODE_BIN")
    # by the runner's own PATH: the agent's puts the worktree's .venv/bin first (#490), where an
    # `opencode` the agent wrote would run its next round
    return shlex.split(fake) if fake else [shutil.which("opencode") or "opencode"]


# The OpenCode versions the rules were checked against (the real-OpenCode tests, CLAUDE.md's
# quirks): from 2.3 up to, not including, 3.0. Outside it the permission rules, the config layer's
# shape and the events may not hold, and only wb doctor's "2.x or newer" noticed (the 2026-11-19
# audit). Move it when the real-OpenCode tests pass on a newer one.
OPENCODE_TESTED = ((2, 3), (3, 0))

# How to get 2.x, said wherever wb finds it missing or older (#654): opencode.ai/install, npm's
# opencode-ai and GitHub's "Latest" release are the 1.x line, so "upgrade it" led back to 1.x.
# Checked against OpenCode's installers, 2026-11-21: /install refuses 2.x and names these two.
OPENCODE_GET = ("curl -fsSL https://opencode.ai/v2/install | bash (on 1.x: opencode upgrade --major; "
                "npm's opencode-ai and GitHub's Latest release are 1.x)")


def opencode_version() -> tuple[int, int] | None:
    """OpenCode's major and minor version, or None when it doesn't run or say."""
    try:
        r = subprocess.run([*opencode_bin(), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = re.search(r"(\d+)\.(\d+)", r.stdout) if r.returncode == 0 else None
    return (int(v[1]), int(v[2])) if v else None


def untested(version: tuple[int, int] | None) -> str:
    """A line when ``version`` is outside OPENCODE_TESTED, else ""."""
    lo, hi = OPENCODE_TESTED
    if version is None or lo <= version < hi:
        return ""
    return (f"OpenCode {version[0]}.{version[1]} is outside what workbench is tested with "
            f"({lo[0]}.{lo[1]} up to {hi[0]}.{hi[1]}): the agent's rules and lane may not hold as "
            "documented; watch its first rounds")


# OpenCode compacts a session once its context passes the model's input limit less
# `compaction.buffer`, and its compaction is the model's own summary, which a small model can get
# wrong: seen 2026-11-16, a summary that recounted CLAUDE.md instead of the task, and the agent
# built what CLAUDE.md said came next. So the runner hands a filling session to a fresh one first
# (the fresh_at knob, checked at every step), and OpenCode's compaction is only the backstop past that:
# its buffer leaves room for one reply at the model's output cap, plus one step's growth.
STEP_GROWTH = 4096


def usable_input(lim: dict) -> int:
    """What OpenCode 2.3 compacts against: the smaller of `limit.input` and `limit.context`,
    else whichever is set. Its compaction sits at this less `compaction.buffer`. Taking it as
    context less output (OpenCode 1.x) wrote a 4K buffer for a hand-written {context 32K, output
    16K}, so OpenCode compacted at 87.5% while wb planned the handover against 37.5% (#675)."""
    inp, window = lim.get("input") or 0, lim.get("context") or 0
    return min(inp, window) if inp and window else inp or window


def compaction_buffer(cfg: dict) -> int | None:
    """The buffer that puts OpenCode's compaction where a request still fits the window."""
    lim, opts = cfg.get("limit") or {}, cfg.get("options") or {}
    window = lim.get("context")
    if not window:
        return None
    usable = usable_input(lim)
    reply = opts.get("max_tokens") or lim.get("output") or 0
    return max(usable + reply - window, 0) + STEP_GROWTH


def compaction_point(cfg: dict) -> float | None:
    """Where OpenCode's compaction sits, as a share of the window."""
    lim, buf = cfg.get("limit") or {}, compaction_buffer(cfg)
    window = lim.get("context")
    if not window or buf is None:
        return None
    return (usable_input(lim) - buf) / window


def fresh_ceiling(cfg: dict) -> float | None:
    """The latest a fresh session can take over (tuning.py): one step's growth (STEP_GROWTH, or
    tuning.CEILING_MARGIN of the window if more) before OpenCode's compaction. The runner checks
    at each step's end and OpenCode before the next request, with that step's tool results in:
    a ceiling at the compaction point itself left 819 tokens at 32K (0 at 16K), so one 3K file
    read compacted the session before the runner handed it over (#644, the audit of main)."""
    top, window = compaction_point(cfg), (cfg.get("limit") or {}).get("context")
    if top is None:
        return None
    return round(top - max(STEP_GROWTH / window, tuning.CEILING_MARGIN), 6)


def handover_warning(cfg: dict) -> str:
    """What to say of an entry whose output cap puts OpenCode's compaction too close to the
    earliest handover point (fresh_ceiling under every fresh_at value), or "". Nothing said so,
    and every long step compacted, the small-model summary the handover exists to avoid (#595,
    the audit of 1.90). At HANDOVER_ROOM or under, no successor has room, so no handover fits."""
    top, at = fresh_ceiling(cfg), compaction_point(cfg)
    lo = min(tuning.KNOBS["fresh_at"].values)
    if top is None or top >= lo:
        return ""
    lim, opts = cfg.get("limit") or {}, cfg.get("options") or {}
    caps = ", ".join(f"{k} {v}" for k, v in (("limit.output", lim.get("output")),
                                             ("options.max_tokens", opts.get("max_tokens"))) if v)
    return (f"its output cap ({caps} of a {lim['context'] // 1024}K window) puts OpenCode's "
            f"compaction at {max(at, 0):.0%} of the window, under a step's growth past the earliest "
            f"handover point ({lo:.0%}): " + ("no handover fits, so long steps compact" if top <= HANDOVER_ROOM else
                               f"handovers come at {top:.0%}, with little room")
            + "; lower limit.output and options.max_tokens")


# OpenCode 2.3 gives every provider a 300 s wait for the response headers (`headerTimeout`) and
# 900 s between streamed chunks (`chunkTimeout`). A server that sends its headers with the first
# token (Ollama on the CPU, a model spilled into RAM) reads a 10-20K-token prompt for 8-30
# minutes: at 300 s OpenCode aborted the request and retried it, four times, then failed the round
# ("Timed out waiting for response headers"), and no read speed was learned to stretch the next
# task's budgets (#491, the audit of 1.90). The runner's own guards (silence, the round's limit)
# decide how long a round may wait, so the task's provider waits for its headers as long as they
# allow, and between chunks at least as long as the silence budget. Provider options merge, so
# the user's own entry keeps the rest; the user's files stay untouched.
PROVIDER_TIMEOUTS = ("headerTimeout", "chunkTimeout")


def provider_timeouts(task: store.Task) -> dict:
    """{"provider": {<the task's provider>: {"options": {headerTimeout, chunkTimeout}}}}."""
    pid = str(task.spec.get("model") or "").split("/", 1)[0]
    if not pid:
        return {}
    chunk = max(900, int(float(task.spec.get("silence_timeout") or 900)) + 60) * 1000
    return {"provider": {pid: {"options": {"headerTimeout": False, "chunkTimeout": chunk}}}}


def write_config_layer(task: store.Task) -> Path:
    """The per-task OpenCode config: the providers OpenCode wouldn't load by itself
    (models.agent_providers), the task's provider's timeouts (provider_timeouts), the agent's
    context and the ask_reviewer server."""
    path = context.rules_dir(task) / "opencode.json"
    wait_ms = int(task.spec.get("ask_timeout", 1200) + 60) * 1000
    try:
        buffer = compaction_buffer(models.resolve(task.spec["model"]).config)
    except KeyError:
        buffer = None
    store.write_json(path, {
        "$schema": "https://opencode.ai/config.json",
        **models._merge(models.agent_providers(), provider_timeouts(task)),
        **context.layer(task),
        "share": "disabled",  # a user's `share: "auto"` would publish every agent session
        # the user's global small_model (titles, summaries) could be a cloud model whose key
        # auth.json holds, sent the task's text; an update mid-task could move OpenCode past
        # OPENCODE_TESTED (#353, the audit of 1.4)
        "small_model": task.spec["model"],
        "autoupdate": False,
        **({"compaction": {"auto": True, "buffer": buffer}} if buffer else {}),
        "mcp": {"servers": {**users_servers_off(), "workbench": {  # 2.x shape: mcp.servers.<name>
            "type": "local",
            "command": [sys.executable, str(PKG_ROOT / "bin" / "wb-ask-mcp")],
            "environment": {"WB_TASK": task.name, "WB_HOME": str(store.home())},
            "codemode": False,  # a plain tool the model sees, not one behind code mode
            # the tool blocks until Claude answers; OpenCode must not give up on it first
            "timeout": {"startup": 15000, "execution": wait_ms},
        }}},
    })
    os.chmod(path, 0o600)  # a provider copied in may carry its apiKey
    return path


def users_servers_off() -> dict:
    """The user's own MCP servers (their global OpenCode config), each replaced by one that
    can't run: OpenCode would start them for the agent and connect to a remote one with the
    user's token (the 2026-11-16 audit). OpenCode 2.3 starts a server whose entry says
    `"enabled": false` all the same, so the command itself goes. Their tools are denied besides
    (context.MCP_RULES)."""
    try:
        servers = (models.read_config().get("mcp") or {}).get("servers") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {name: {"type": "local", "command": ["false"], "enabled": False}
            for name in servers if name != "workbench"}


# The rules again, as OPENCODE_CONFIG_CONTENT, which OpenCode loads after every config file: a
# project's opencode.json (the worktree's, or one in a directory above it) loads after the layer
# and its permissions come later, so `{"action": "shell", "resource": "*", "effect": "allow"}`
# there lifted every deny (the 2026-11-16 audit). OPENCODE_DISABLE_PROJECT_CONFIG would shut it
# out too, but it drops AGENTS.md and the project's skills with it, so it is only the fallback for
# rules too long for one environment string (MAX_ARG_STRLEN, 128 KiB).
CONFIG_CONTENT_MAX = 120_000


# What the layer sets that a project's opencode.json, loaded after it, must not change either:
# the compaction backstop past the handover point, the model for titles and summaries, OpenCode
# updating itself (#353). A project config set compaction `auto: false` (a session past the
# handover point then overflowed), sent titles to its cloud small_model, and turned updates on
# past OPENCODE_TESTED (#519).
RESTATED = ("small_model", "autoupdate", "compaction")


def model_pins(model_id) -> dict:
    """The task's model entry's `limit` and `options.max_tokens`, as a `provider` overlay: what
    wb planned the handover and the compaction buffer against. A project config's own window for
    the model would move OpenCode's compaction off them (#519). {} for a model wb doesn't know."""
    try:
        m = models.resolve(model_id) if isinstance(model_id, str) else None
    except KeyError:
        m = None
    if m is None:
        return {}
    cfg, entry = m.config, {}
    if isinstance(cfg.get("limit"), dict):
        entry["limit"] = cfg["limit"]
    if isinstance(cfg.get("options"), dict) and "max_tokens" in cfg["options"]:
        entry["options"] = {"max_tokens": cfg["options"]["max_tokens"]}
    if not entry:
        return {}
    return {"provider": {m.provider: {"models": {m.id.split("/", 1)[1]: entry}}}}


# The user's own OPENCODE_CONFIG_CONTENT goes under the rules, not away: replaced, the providers,
# instructions or permissions kept there were lost to the agent (#347, the audit of 1.4). Its
# permissions come first, so the rules' (the last match wins) still decide; its MCP servers stay
# off like the global files' ones (the layer's `mcp`, which this would load after). A key the
# layer sets too is left to the layer: loaded last, the user's `compaction` would undo the
# handover's backstop, and their `skills` or `instructions` replace the task's. But `provider`,
# which the layer always sets now (its timeouts, #491): the user's providers stay, and the
# layer's timeouts go on top of them here too, so neither their content nor a project's
# opencode.json puts the 300 s header wait back. The RESTATED keys and the task model's pins
# (model_pins; the layer's small_model is the task's model) are said again here, on top, so a
# project config can't change them either (#519).
def rules_env(layer: dict) -> dict:
    users = models.users_config_content()
    theirs = users.get("permissions")
    users = {k: v for k, v in users.items()
             if (k == "provider" or k not in layer) and k not in ("mcp", "permissions")}
    timeouts = {pid: {"options": {k: v["options"][k] for k in PROVIDER_TIMEOUTS if k in v["options"]}}
                for pid, v in (layer.get("provider") or {}).items()
                if isinstance(v, dict) and isinstance(v.get("options"), dict)
                and any(k in v["options"] for k in PROVIDER_TIMEOUTS)}
    if timeouts:
        users["provider"] = models._merge(users["provider"] if isinstance(users.get("provider"), dict)
                                          else {}, timeouts)
    pinned = {k: layer[k] for k in RESTATED if k in layer}
    if pins := model_pins(layer.get("small_model")):
        mine = users.get("provider")  # theirs, with the layer's timeouts
        pinned["provider"] = models._merge(mine if isinstance(mine, dict) else {}, pins["provider"])
    rules = {"permissions": [*(theirs if isinstance(theirs, list) else []),
                             *layer["permissions"]], "share": "disabled", **pinned}
    content = json.dumps({**users, **rules}, separators=(",", ":"))
    if len(content.encode()) <= CONFIG_CONTENT_MAX:
        return {"OPENCODE_CONFIG_CONTENT": content}
    # too long: the rules stay in the layer, so the user's content may not lift them either
    rest = json.dumps({**users, "share": "disabled", **pinned}, separators=(",", ":"))
    return {"OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_CONFIG_CONTENT": rest if len(rest.encode()) <= CONFIG_CONTENT_MAX else "{}"}


# What OpenCode's own environment adds to the agent's: npx and `npm exec` run only what the
# project installed, instead of fetching a package unasked (npm says yes by itself without a
# terminal; the 2026-11-16 audit). Not the tests' environment: they run as they would for you.
OPENCODE_ENV = {"npm_config_yes": "false"}


# What the agent's environment leaves out of the runner's: credentials (API keys and tokens Claude
# Code or the user's shell carry, the SSH agent that would sign a push). The runner's own
# PYTHONPATH, which would put `python3 -m workbench.cli` in the agent's reach, gives way to the
# user's (context.python_path, #512), whatever `env` names. A provider that
# needs a key from the environment names it in commands.json `env`, which passes it through.
# The 2026-11-16 audit added the second line's names (SMTP_PASS, MYSQL_PWD, SENTRY_DSN, BW_SESSION
# an unlocked Bitwarden vault, OP_SESSION_<acct> 1Password's) and the value rule (USERINFO: a URL
# with a password, DATABASE_URL=postgres://u:p@h/db, whatever its name).
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY|"
                        r"_(PASS|PASSPHRASE|PWD|DSN|JWT|PAT|KEY)$|^(PASS|PASSPHRASE|KEY)$|"
                        r"(^|_)PASS_|WEBHOOK|^BW_SESSION$|^OP_SESSION_|^KRB5CCNAME$|"
                        r"^(ANTHROPIC|CLAUDE|GH|GITHUB|GITLAB|AWS|AZURE|GOOGLE|OPENAI|HF)_|"
                        r"^(SSH_AUTH_SOCK|SSH_AGENT_PID|GPG_AGENT_INFO|DBUS_SESSION_BUS_ADDRESS|"
                        r"DBUS_SYSTEM_BUS_ADDRESS)$", re.I)
USERINFO = re.compile(r"\w+://[^/@\s:]*:[^/@\s]+@")
# Configuration, not credentials, under the prefixes above: a test that reads Hugging Face models
# offline from HF_HOME, or a region, behaves in the lane as it does for the user (the audit).
ENV_KEEP = frozenset({"HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE",
                      "HF_ENDPOINT", "HF_HUB_DISABLE_TELEMETRY", "TRANSFORMERS_OFFLINE",
                      "TOKENIZERS_PARALLELISM", "AWS_REGION", "AWS_DEFAULT_REGION",
                      "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION"})


# OpenCode's switches that skip every .claude directory (OpenCode 2.3 reads either, as "1" or
# "true"), and the project's own .claude/skills with them, without a word (#384, the audit of
# 1.6). They stay out of the agent's environment: wb hands the instructions itself, and keeps the
# user's personal skills from the agent by its own rules (context.denied_skills), which are made
# for OpenCode's default.
CLAUDE_CODE_OFF = ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS")


def env_kept(name: str, value: str, through: set) -> bool:
    """Whether the agent's environment keeps ``name``: passed through by commands.json `env`,
    configuration (ENV_KEEP), or no credential by its name (SECRET_ENV) nor its value (USERINFO)."""
    return name in through or name in ENV_KEEP or not (SECRET_ENV.search(name) or USERINFO.search(value))


# Signing off for the agent's git: a user with commit.gpgsign or tag.gpgsign in ~/.gitconfig had
# every test that commits in a scratch repo fail in the lane (the keys hidden, SSH_AUTH_SOCK and
# the agent's sockets out of reach: 'gpg failed to sign the data'), the base check blamed on the
# project (#666). The agent can't commit to the task's repo anyway; only scratch repos see this.
NO_SIGNING = (("commit.gpgsign", "false"), ("tag.gpgsign", "false"))


def git_config_env(env: dict, pairs=NO_SIGNING) -> dict:
    """``pairs`` as git's GIT_CONFIG_COUNT/KEY_n/VALUE_n (git 2.31), after any already in ``env``."""
    try:
        n = max(int(env.get("GIT_CONFIG_COUNT") or 0), 0)
    except ValueError:  # git refuses a bogus count itself; start over rather than add to it
        n = 0
    out = {}
    for i, (k, v) in enumerate(pairs, n):
        out[f"GIT_CONFIG_KEY_{i}"], out[f"GIT_CONFIG_VALUE_{i}"] = k, v
    out["GIT_CONFIG_COUNT"] = str(n + len(pairs))
    return out


# Per-user build daemons crossed the lane both ways (#669, the 2026-11-21 audit): Gradle reuses
# any idle compatible daemon in ~/.gradle/daemon, whatever its environment, so the user's own
# `./gradlew build` ran in the agent's daemon under Landlock (and failed writing its build/, then
# lost the daemon to sweep), and the agent's could run in the user's, outside the lane; sccache
# shares one server on 127.0.0.1:4226, MSBuild its nodes and build server, Roslyn its compiler
# server. With the lane on, the agent gets its own: Gradle's registry in its runtime directory
# (`org.gradle.daemon.registry.base`), the Kotlin compiler in Gradle's process, a port of its
# own for sccache, and no MSBuild node reuse, build server or shared compilation.
DAEMONS_OFF = {"MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
               "UseSharedCompilation": "false"}
SCCACHE_PORTS = (20000, 40000)  # where a task's sccache port is looked for


def free_port(seed: str, lo: int = SCCACHE_PORTS[0], hi: int = SCCACHE_PORTS[1]) -> int:
    """A loopback port in [lo, hi) picked from ``seed`` (the same each time while it is free), the
    next free one when something holds it."""
    import socket
    start = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % (hi - lo)
    for i in range(64):
        port = lo + (start + i) % (hi - lo)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return lo + start


def daemon_env(task: store.Task, env: dict) -> dict:
    """The variables that give the agent build daemons of its own (DAEMONS_OFF and the rest)."""
    runtime = context.runtime_dir(task)
    gradle = (f"-Dorg.gradle.daemon.registry.base={runtime / 'gradle'} "
              "-Dkotlin.compiler.execution.strategy=in-process")
    return {**DAEMONS_OFF,
            "GRADLE_OPTS": f"{env['GRADLE_OPTS']} {gradle}" if env.get("GRADLE_OPTS") else gradle,
            "SCCACHE_SERVER_PORT": str(free_port(str(runtime)))}


def agent_env(task: store.Task, run: str = "") -> dict:
    """The agent's environment: its config layer, and git read-only (gitguard.py). ``run``: the
    one run it is for (WB_RUN), which sweep can then end alone."""
    through = set(context.commands_conf().get("env", []))
    keep = {k: v for k, v in os.environ.items() if env_kept(k, v, through) and k not in CLAUDE_CODE_OFF}
    layer = write_config_layer(task)
    env = {**keep, "PWD": str(task.workdir),  # opencode finds the project from $PWD
           "OPENCODE_CONFIG": str(layer), **rules_env(json.loads(layer.read_text())),
           "WB_TASK": task.name, "WB_HOME": str(store.home()), **CLEAN_ENV,
           **({"WB_RUN": run} if run else {})}
    env.update(git_config_env(env))
    env["PATH"] = env.get("PATH") or os.defpath
    places = context.writable(task)
    if places is not None:  # its own: the user's holds their bus and agents
        env["XDG_RUNTIME_DIR"] = str(context.runtime_dir(task))
        # Jupyter's kernel connection files, in the agent's runtime dir: its default,
        # ~/.local/share/jupyter/runtime, is outside the lane, and every notebook test
        # (nbmake, nbval, papermill) failed with PermissionError before a kernel started (#668)
        env.setdefault("JUPYTER_RUNTIME_DIR", str(context.runtime_dir(task) / "jupyter"))
        env.update(daemon_env(task, env))  # its own build daemons, not the user's (#669)
        # On WSL, not the Windows PATH, whose programs run outside the lane (context.agent_path),
        # nor the interop socket's name (the 0.90 review; /init finds it without: interop_gap)
        env.pop("WSL_INTEROP", None)
        env["PATH"] = context.agent_path(env["PATH"])
    # what the user trusts in their checkout, mise trusts in workbench's copies of it (#674):
    # the worktree and the scratch checkouts all live under the state directory
    if context.mise_trusted(task, env["PATH"]):
        env["MISE_TRUSTED_CONFIG_PATHS"] = os.pathsep.join(
            [*filter(None, [env.get("MISE_TRUSTED_CONFIG_PATHS")]), os.path.realpath(store.home())])
    # not the user's checkout's venv: the tests imported their checkout, not the worktree (#490)
    inside = set(context.python_env(task, env))
    # the real git found outside the worktree: the runner runs it (gitguard.install), outside
    # the lane, so a `git` the agent wrote into .venv/bin must not be the one it finds
    outside = os.pathsep.join(d for d in env["PATH"].split(os.pathsep) if d not in inside)
    shim = context.rules_dir(task) / "bin"
    # the lane's other directories are scratch to the shim too (#665): the kernel lets the
    # agent write there already, and writable() leaves the task's repo out of them
    scratch = [d for d in places or [] if os.path.isdir(d) and not d.startswith(("/dev", "/proc"))]
    if gitguard.install(shim, Path(task.spec["repo"]), PKG_ROOT, outside, task.worktree, scratch):
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    return env


# A proxy that holds a password (https_proxy=http://user:pass@proxy:3128, a corporate network) is
# a URL with a password like any other, so USERINFO leaves it out of the agent's environment, and
# the setup's `npm ci` or `pip install` and the tests failed with network errors that read as the
# project's, with nothing said (#380, the audit of 1.6). It stays out, a credential the user
# hasn't named: wb start says so, and a setup or a test run that fails on a connection says it
# again (NET_FAILED), each with the commands.json `env` that passes it through.
def dropped_proxies(env=None) -> list[str]:
    """The proxy variables (`https_proxy`, `ALL_PROXY` and the like) the agent's environment
    leaves out of ``env`` (the runner's own by default)."""
    env = os.environ if env is None else env
    through = set(context.commands_conf().get("env", []))
    return sorted(k for k, v in env.items()
                  if k.lower().endswith("_proxy") and not env_kept(k, v, through))


def proxy_line(names: list[str]) -> str:
    return (f"the agent's environment leaves out {', '.join(names)} (a URL with a password): "
            f"commands.json \"env\": [{', '.join(json.dumps(n) for n in names)}] passes it through")


# What a command that couldn't reach the network prints: npm's and Node's error codes, curl's,
# Python's (urllib, requests, pip), cargo's and Go's, and a proxy asking for its password.
NET_FAILED = re.compile(r"\b(ECONNREFUSED|ECONNRESET|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|"
                        r"EHOSTUNREACH)\b|Could not resolve (host|proxy)|Temporary failure in name "
                        r"resolution|Name or service not known|nodename nor servname|Network is "
                        r"unreachable|No route to host|Connection (refused|timed out|reset)|"
                        r"Failed to establish a new connection|ProxyError|Max retries exceeded|"
                        r"\b407\b.{0,40}Proxy|failed to connect to|dial tcp|getaddrinfo", re.I)


def proxy_gap(output: str) -> list[str]:
    """The dropped proxies a failed setup or test run may have needed: its output says it
    couldn't connect (NET_FAILED) and the agent's environment left one out (dropped_proxies)."""
    return dropped_proxies() if NET_FAILED.search(output[-200_000:]) else []


def claude_files_changed(files: str) -> list[str]:
    """The instruction files, .claude/ paths and OpenCode project config among `git status
    --short` lines (a project opencode.json loads into the agent's next round)."""
    out = []
    for line in files.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        name = Path(path).name
        if name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "opencode.json",
                    "opencode.jsonc") or path.startswith((".claude/", ".opencode/")) or \
                "/.claude/" in path or "/.opencode/" in path:
            out.append(path)
    return out


def _reader(stream, q):
    # the end marker whatever happens: a reader that died without it left the round waiting on a
    # finished agent until the silence timeout stopped it (#440)
    try:
        for line in stream:
            q.put(line)
    finally:
        q.put(None)


EXIT_GRACE = 30  # seconds OpenCode has to exit once its output closes; past that, it's stopped


def _stop(proc: subprocess.Popen):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def sweep(task: store.Task, run: str = "", runs=()) -> int:
    """Kill every process of ours the agent started for this task and left running: a server
    backgrounded with `&`, a `setsid`'d child, the agent itself after its runner died. They are
    found by the environment the runner gave them (WB_TASK and WB_HOME, agent_env), since
    OpenCode's shell starts each command in a group of its own. /proc's environ, else KERN_PROCARGS2
    (macOS, where they once outlived the round: #236). The 2026-11-16 audit: a leftover writer
    kept editing the worktree after Claude read the diff, and a crashed runner left its agent
    working. With ``run``, only what that run started (WB_RUN, agent_env): a wb verify's cleanup
    killed the agent wb feedback had just started on the same task, and two verifies each
    other's checks (#324, the audit of 1.4). With ``runs``, only what one of them started: a
    runner's backstop on exit ends the runs it issued, not a wb verify's (#504)."""
    proc = Path("/proc")
    if not proc.joinpath("self").exists():
        return _sweep_macos(task, run, runs) if sys.platform == "darwin" else 0
    want = {f"WB_TASK={task.name}".encode(), f"WB_HOME={store.home()}".encode(),
            *([f"WB_RUN={run}".encode()] if run else [])}
    anyof = {f"WB_RUN={r}".encode() for r in runs}
    me, uid, n = os.getpid(), os.getuid(), 0
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            env = set(d.joinpath("environ").read_bytes().split(b"\0"))
            if not want <= env or (anyof and not anyof & env):
                continue
            os.kill(int(d.name), signal.SIGKILL)
            n += 1
        except OSError:
            continue
    return n


def _mac_environ(procargs: bytes) -> set[bytes]:
    """Darwin KERN_PROCARGS2 returns argc, the padded executable path, argv, then environ."""
    argc = int.from_bytes(procargs[:4], sys.byteorder, signed=True)
    _, _, strings = procargs[4:].partition(b"\0")
    fields = strings.lstrip(b"\0").split(b"\0")
    if not 0 < argc < len(fields):
        return set()
    env = set()
    for field in fields[argc:-1]:  # only complete entries, up to the environment terminator
        if not field:
            break
        env.add(field)
    return env


def _sweep_macos(task: store.Task, run: str = "", runs=()) -> int:
    """ps -E flattens argv and environ together. Only kernel environment entries identify a task."""
    try:
        pids = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid="], capture_output=True,
                              check=True, timeout=10).stdout.split()
        buf = ctypes.create_string_buffer(os.sysconf("SC_ARG_MAX"))
        sysctl = ctypes.CDLL(None).sysctl
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    want = {os.fsencode(f"WB_TASK={task.name}"), os.fsencode(f"WB_HOME={store.home()}"),
            *([os.fsencode(f"WB_RUN={run}")] if run else [])}
    anyof = {os.fsencode(f"WB_RUN={r}") for r in runs}
    killed = 0
    for word in pids:
        if not word.isdigit() or (pid := int(word)) == os.getpid() or pid <= 1:
            continue
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(len(buf))
        if sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            continue
        env = _mac_environ(buf[:size.value])
        if want <= env and (not anyof or anyof & env):
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:  # exited since ps
                pass
    return killed


def _end_agent(task: store.Task) -> None:
    """Nothing a round started outlives it, however the round ended (the 2026-11-16 audit: a
    crash in the runner left the agent editing the worktree after the GPU lease was released):
    the agent if it still runs, what it left in its group, and what it started outside it."""
    proc = CURRENT.pop("proc", None)
    if proc is not None:
        if proc.poll() is None:
            _stop(proc)
        _kill_group(proc.pid)
    # The round's own; none known (main's backstop, a crash before it): every run this runner
    # issued and hasn't swept. Not the whole task's: that killed a wb verify run in the
    # background beside a feedback round, and its candidate read as failing (#504, the audit of
    # 1.90). wb stop, takeover, accept, discard and reap still sweep the whole task.
    run = CURRENT.pop("run", "")
    if runs := ({run} if run else set(ISSUED)):
        sweep(task, runs=runs)
        ISSUED.difference_update(runs)
    try:
        if task.status.get("agent_pid"):
            task.set_status(agent_pid=None)
        ask_mcp.close_all(task)  # its questions died with it: a stop unwound past run_round's close
        st = task.status
        if st.get("asking") or st.get("phase") == "asking":
            task.set_status(asking=None, **({"phase": "running"} if st.get("phase") == "asking" else {}))
    except OSError:  # a full disk must not hide the error that brought us here
        pass


# Claude steering a running task (`wb steer`): queued notes wait for the agent's current run to
# end; a --now note interrupts it. Either way the same session continues with the note as its
# next prompt. steer.jsonl holds every note; status.steer_taken counts the delivered ones.
INTERRUPT_HEAD = "Claude, your reviewer, interrupted you."
NOTE_HEAD = "A note from Claude, your reviewer, before you go on."


# The runner's own steer when the agent generates past its thinking budget with nothing to show.
NUDGE = ("You have been thinking for {mins} minutes without a tool call. Stop planning and act "
         "now: make the next edit, one file per tool call, smallest first, and run the tests "
         "as soon as there is something to test. Keep each thought short; decide as you go.")


def pending_steer(task: store.Task) -> list[dict]:
    rows, _ = store.read_jsonl(task.dir / "steer.jsonl")
    return rows[task.status.get("steer_taken", 0):]


def steer_prompt(notes: list[dict], interrupted: bool) -> str:
    body = "\n\n".join(r["text"].strip() for r in notes)
    if interrupted:
        return (f"{INTERRUPT_HEAD} Stop what you were doing and do this first; it overrides "
                f"anything earlier that conflicts with it:\n\n{body}\n\nThen carry on with the "
                "task unless this says otherwise, and finish with a short summary as before.")
    return (f"{NOTE_HEAD} Take it into account now:\n\n{body}\n\nThen carry on with the task, "
            "and finish with a short summary as before.")


def outside_spec(task: store.Task, files: str) -> list[str]:
    """Changed files the current spec never names, when it names any: the agent's drift shows
    here first (seen 2026-11-16: a flow-field module nobody asked for). Matched by path or name;
    a brace form names each (`src/replay_io.{h,c}`: flagged as unnamed on 2026-11-18)."""
    spec = current_task_prompt(task)
    spec += "\n" + " ".join(a + part + b for a, parts, b in
                             re.findall(r"([\w./-]*)\{([\w.-]+(?:,[\w.-]+)+)\}([\w./-]*)", spec)
                             for part in parts.split(","))
    paths = [line[3:].split(" -> ")[-1].strip('"') for line in files.splitlines()]
    if not any(p in spec or Path(p).name in spec for p in paths):
        return []
    return [p for p in paths if p not in spec and Path(p).name not in spec]


def _event(line: str) -> list[dict]:
    """[the JSON object on this line of OpenCode's output], or [] for anything else."""
    try:
        raw = store.loads(line) if line else None  # scrubbed: a lone surrogate (#438)
    except json.JSONDecodeError:
        return []
    return [raw] if isinstance(raw, dict) else []


def _clock(secs: float) -> str:
    return f"{int(secs // 60)}m{int(secs % 60):02d}s"


def active_so_far(task: store.Task, now: float | None = None) -> float:
    """The task's active work in seconds, as the ETA compares it with the typical length: status
    `active_s` (the rounds so far, written as each ends) plus the round under way, whole."""
    st, now = task.status, now or time.time()
    running = st.get("round_started_at") if st.get("phase") in ("running", "testing", "asking") else None
    return float(st.get("active_s") or 0) + (max(0.0, now - running) if running else 0.0)


def checkin_text(task: store.Task, n: int, out: dict, since: float, active: float | None = None) -> str:
    """The digest a check-in wakes Claude with: what changed since the last one, and timing.
    ``active``: the round's own count of active work so far, when the round knows it."""
    from . import router  # router -> models; imported late to keep the runner's startup lean
    spec, st, now = task.spec, task.status, time.time()
    took = now - spec["created_at"]
    typical = router.expected_minutes(spec["model"], spec.get("size", "significant"))
    eta = ""
    if typical:  # active work against active work: the wall clock held queues and reviews (#543)
        left = typical * 60 - (active_so_far(task, now) if active is None else active)
        eta = (f" · ~{_clock(left)} left (typically {typical:.0f}m)" if left > 0 else
               f" · {_clock(-left)} past its typical {typical:.0f}m")
    rows = [r for r in store.read_jsonl(task.dir / "events.jsonl")[0] if r.get("ts", 0) > since]
    tools: dict = {}
    for r in rows:
        if r.get("kind") == "tool":
            tools[r["tool"]] = tools.get(r["tool"], 0) + 1
    errs = [r for r in rows if r.get("kind") == "tool" and not r.get("ok")]
    tok_in = sum(r.get("in", 0) for r in rows if r.get("kind") == "step")
    tok_out = sum(r.get("out", 0) for r in rows if r.get("kind") == "step")
    lines = [f"task {task.name} · {spec['model_label']} · round {n} · running {_clock(took)}{eta}",
             f"since the last check-in ({_clock(now - since)}): "
             + (", ".join(f"{k}×{v}" for k, v in sorted(tools.items())) or "no tool calls")
             + (f", {len(errs)} failed" if errs else "")
             + f"; {tok_in} tokens read, {tok_out} written",
             f"now: {st.get('last') or '?'}"]
    if out["texts"]:
        lines.append("its latest words: " + out["texts"][-1].strip().replace("\n", " ")[:400])
    files = gitops.changed(task.worktree)
    lines.append("files changed so far: " + gitops.brief_paths(files))
    if extra := outside_spec(task, files):
        lines.append("NOTE: the spec doesn't name " + ", ".join(gitops.brief(extra))
                     + ": check it is still on its task")
    return "\n".join(lines)


def _steer_bell(task: store.Task, proc, q: queue.Queue) -> None:
    """Ring the round's loop the moment Claude writes a steer note, so `wb steer --now`
    interrupts at once instead of at the loop's next timeout, and when a question or its answer
    lands in ask/, so the runner relays it at once (ask_mcp.relay)."""
    ask = context.ask_dir(task)
    with Watcher(task.dir, ask) as w:
        while proc.poll() is None:
            if any(p.name == "steer.jsonl" or p.parent == ask and not p.name.startswith(".")
                   for p in w.wait(5)):
                q.put("")


# Linux takes at most 128 KiB in one argv string (MAX_ARG_STRLEN); a longer prompt is sent as a
# pointer to its file in the round's directory, which the agent can read.
PROMPT_ARG_MAX = 100_000


# The round's guards run on the monotonic clock, which a suspend doesn't advance and NTP never
# steps: on the wall clock a laptop shut for 20 minutes woke to a round stopped for silence (the
# 2026-11-16 audit). A loop pass far longer than its 5 s wait means the process was frozen (a
# host's sleep that the monotonic clock still counted, as WSL2's may): that time isn't the
# round's either. Check-ins stay on the wall clock: they compare with timestamps in files.
FROZEN_SECS = 60


class _Clock:
    """The round's time and its silence, less the waits that aren't the agent's: Claude's answer
    to a question, and a frozen machine."""

    def __init__(self, t: float):
        self.t0 = self.tick = self.seen = t
        self.paused = 0.0

    def step(self, t: float, asking: bool) -> None:
        dt, self.tick = t - self.tick, t
        if asking:  # waiting on Claude: neither the round's time nor silence
            self.paused += dt
            self.seen = t
        elif dt > FROZEN_SECS:
            self.paused += dt
            self.seen += dt

    def ran(self, t: float) -> float:
        return t - self.t0 - self.paused

    def quiet(self, t: float) -> float:
        return t - self.seen


# OpenCode's own lines, not the model's output: what read_bound counts as no answer yet
NOT_THE_MODEL = frozenset({"step_start", "retry", "error", "background_wait"})


def renudged(st: dict, n: int) -> bool:
    """Round n stalling is stopped, not nudged: the last round ended in the runner's nudge and
    this is the session it nudged. A fresh one never thought for long, and its first long think
    was stopped "even after a nudge" (#681)."""
    return st.get("nudged_round") == n - 1 and bool(st.get("session"))


def run_round(task: store.Task, n: int, keep: bool = False) -> dict:
    """One `opencode run`. keep: Claude asked for this session to carry on (wb feedback
    --keep-context), so the round never hands over mid-way."""
    spec, st = task.spec, task.status
    prompt = task.round_dir(n).joinpath("prompt.md").read_text()
    cmd = opencode_bin() + ["run", "--standalone", "-m", spec["opencode_model"], "--auto",
                            "--format", "json", "--title", f"wb: {task.name}"]
    marks = {gitops.mark(task.worktree)}  # the tree as the round and the session began
    if st.get("session"):
        cmd += ["--session", st["session"]]
        marks.add(st.get("session_mark"))
    else:
        task.set_status(session_mark=next(iter(marks)), session_floor=None)
    floor = task.status.get("session_floor")  # the context the session had read when it began work
    note = skills_note(task, n)
    if len((note + prompt).encode()) > PROMPT_ARG_MAX:
        prompt = (f"Your instructions for this round are long, so they are in the file "
                  f"{task.round_dir(n) / 'prompt.md'}. Read all of it first (in parts if it is "
                  f"too big for one read), then do what it says.")
    # After `--`: OpenCode reads a message that starts with '-' (a spec opening with a bullet or
    # front matter) as a flag, prints its usage and exits (the 2026-11-16 audit).
    cmd += ["--", note + prompt]
    CURRENT["run"] = run = _issue()
    env = {**agent_env(task, run), **OPENCODE_ENV}
    cmd = lane.wrap(context.writable(task), cmd,   # writes stay in the agent's lane (lane.py),
                    context.readable(task),       # and credentials out of its reach
                    context.unix_paths(task))
    try:  # this round's OpenCode marks it again: kept from round 1, the report's note about a
        (ask_mcp.ask_dir(task) / ask_mcp.LISTED).unlink()  # lost ask tool could fire only there (#481)
    except OSError:
        pass
    log = open(task.dir / "runner.log", "a")
    proc = subprocess.Popen(cmd, cwd=task.workdir, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=log, encoding="utf-8",
                            # a byte that isn't UTF-8 (a plugin's Latin-1 print) is U+FFFD, not
                            # the reader's death and a round lost as "no first output" (#440)
                            errors="replace",
                            start_new_session=True)  # its own group, so a stop takes MCP too
    CURRENT["proc"] = proc
    task.set_status(agent_pid=proc.pid)  # its own session: wb stop ends it if the runner can't
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    threading.Thread(target=_steer_bell, args=(task, proc, q), daemon=True).start()
    power = None
    if not CURRENT.get("remote") and not models._fake():  # the suite's fakes: the GPU isn't theirs
        power = _Power()
        power.start()

    detector = StuckDetector(**spec.get("stuck", {}), worktree=task.worktree)
    wall, silence = float(spec.get("round_timeout", 2400)), float(spec.get("silence_timeout", 900))
    think = float(spec.get("think_timeout") or 0)
    # A shell command OpenCode moved to the background (past 2 minutes it does, and the turn
    # ends waiting on it) prints nothing until it is done: a long test suite counted as thinking,
    # was nudged and killed every 7 minutes, and rerun (the 2026-11-16 audit). While it waits
    # (`background_wait`), no nudge, and silence runs to the test timeout; the round's limit holds.
    wait_hush = max(silence, float(spec.get("test_timeout") or 900))
    nudged = False
    renudge = renudged(st, n)
    edit_mark = gitops.mark(task.worktree)  # shell edits (sed -i, cat >) count as edits too
    started = time.time()  # the wall clock, for check-ins: they compare with timestamps in files
    clock = _Clock(time.monotonic())
    prior = float(task.status.get("active_s") or 0)  # earlier rounds' active work
    typical = progress.threshold(spec)  # the user's opt-in (#95); off (None), today's timing
    # The think budget starts at the round's first output: until then the model is reading its
    # prompt, which on the CPU or a model spilled into RAM takes many minutes, and a round nudged
    # for it was then stopped "even after a nudge" (the 2026-11-16 audit). Silence still applies.
    heard = False
    before_start = None  # the clocks before a step_start that a retry may take back
    waiting_bg = False
    # answered: the model's own output came (not only OpenCode's lines); retries: the requests
    # OpenCode sent again, each with why
    out = {"exit": None, "stuck": None, "texts": [], "tools": {}, "errors": [], "retries": [],
           "tok_in": 0, "tok_out": 0, "interrupted": False, "full": None, "guard": None,
           "answered": False}
    window = st.get("window")
    fresh_at = tuning.value(task, "fresh_at")
    opened: dict = {}  # the questions open this round (ask_mcp.relay)
    while True:
        try:  # woken by the agent's output or a steer note; the timeout runs the clocks below
            line = q.get(timeout=5)
        except queue.Empty:
            line = ""
        if line is None:
            break
        now, mono = time.time(), time.monotonic()
        ask_mcp.relay(task, opened)  # the agent's questions: checked, counted, raised
        asking = task.status.get("phase") == "asking"  # the agent is waiting on Claude: not silent
        clock.step(mono, asking)
        got = _event(line)
        kinds = {raw.get("type") for raw in got}
        # A `retry` line is OpenCode giving up on a request and sending it again, not the model's
        # output: it reset the silence clock, so a model that never answered was never stopped
        # for it, and "no first output" could not be said (#491, the audit of 1.90). OpenCode
        # prints the step's `step_start` as the response begins, or just before that retry: a
        # retry takes back what its step_start counted.
        if got and kinds <= {"retry"}:
            if before_start is not None:
                clock.seen, heard = before_start
                before_start = None
        elif line:
            before_start = (clock.seen, heard) if kinds == {"step_start"} else None
            clock.seen, heard = mono, True
        if not kinds <= NOT_THE_MODEL:  # its first token, or later ones (read_bound)
            out["answered"] = True
        # The line's event, when it is one (0 or 1 of them): a line that isn't (a stray print, a
        # number or list) or an event normalize drops skips only this body, never the clocks and
        # the steer check below. Those `continue`d past them, and a stream of unknown progress
        # events ran past the round's limit, ignored `wb steer --now` and held the GPU lease
        # (#195, the 2026-11-19 audit).
        for raw in got:
            if raw.get("type") == "background_wait":
                waiting_bg = True
            if raw.get("sessionID") and not task.status.get("session"):
                task.set_status(session=raw["sessionID"])
            row = normalize(raw, n)
            if row is None:
                continue
            if row["kind"] != "retry":
                waiting_bg = False  # the session moved on
            sig = row.pop("sig", None)
            if row["kind"] == "tool":
                out["tools"][row["tool"]] = out["tools"].get(row["tool"], 0) + 1
                if not row["ok"]:
                    out["errors"].append(f"{row['tool']}: {row.get('error', '')}")
                task.set_status(last=f"{row['tool']} {row['brief']}".strip(),
                                tools=task.status.get("tools", 0) + 1)
            elif row["kind"] == "text":
                out["texts"].append(row["text"])
                row["text"] = row["text"][:2000]
            elif row["kind"] == "step":
                out["tok_in"] += row["in"]
                out["tok_out"] += row["out"]
                used = row["in"] + row.get("cache", 0) + row["out"] + row.get("reasoning", 0)  # #644
                st = task.status
                task.set_status(tok_in=st.get("tok_in", 0) + row["in"], tok_out=st.get("tok_out", 0) + row["out"],
                                live_used={"session": st.get("session"), "used": used},  # #262
                                **peak(st, used))
                # Full: a fresh session takes over (drive), never OpenCode's own summary. Only
                # once this session has changed a file: one that filled up reading would hand
                # the same reading to the next, which fills up too (seen 2026-11-16: twenty
                # handovers in twelve minutes, no edit made). It carries on instead.
                # And only when a successor, re-reading what this one read before its first
                # change (its floor), would have room left to work: seen the same day, a step
                # whose reading came to 16K of a 19K handover point handed over three times in
                # four minutes, each fresh session getting 3K of work. It carries on instead.
                changed = None
                if floor is None and window:
                    changed = marks != {gitops.mark(task.worktree)}
                    if changed:
                        floor = used
                        task.set_status(session_floor=floor)
                if window and not keep and used >= fresh_at * window and not out["full"]:
                    if changed is None:
                        changed = marks != {gitops.mark(task.worktree)}  # since either began
                    if not changed:
                        if not out.get("reading_full"):
                            out["reading_full"] = used
                            _reading_full(task, n, used, window)
                    elif fresh_at * window - floor >= HANDOVER_ROOM * window:
                        out["full"] = used
                    elif not out.get("reading_full"):
                        out["reading_full"] = used
                        _reading_full(task, n, used, window, floor)
            elif row["kind"] == "error":
                out["errors"].append(f"opencode: {row['text']}")
            elif row["kind"] == "retry":
                out["retries"].append(row["text"])
                task.set_status(last=f"OpenCode retries a request: {row['text']}"[:200])
            task.event(row)
            why = detector.feed({**row, "sig": sig}) if sig else None
            if why and detector.since_edit >= detector.no_edit:
                if (m := gitops.mark(task.worktree)) != edit_mark:  # it edited through the shell
                    edit_mark, detector.since_edit, why = m, 0, None
            if why:
                out["stuck"], out["guard"] = why, detector.guard
        quiet = clock.quiet(mono)
        hush = wait_hush if waiting_bg else silence
        if not out["stuck"] and clock.ran(mono) > wall:
            out["stuck"], out["guard"] = f"the round ran past its {int(wall // 60)}-minute limit", "round"
        if not out["stuck"] and not asking and quiet > hush:
            out["guard"] = "silence"
            out["stuck"] = (f"no output for {int(hush // 60)} minutes" if heard else
                            f"no first output in {int(hush // 60)} minutes: the model may be "
                            "reading its prompt slowly (on the CPU, or partly in RAM?); "
                            "--silence-timeout gives a new task longer")
            if waiting_bg:
                out["stuck"] += ", waiting on a background job"
        # Generating with nothing to show past the budget: interrupt with a nudge (once a round)
        # and tell Claude. The nudge starts a new round, and that round stalling again is
        # stopped, whatever it did first: else a model that never answers, or a command that
        # prints nothing for longer than the budget, would be nudged and rerun forever.
        if (think and heard and not waiting_bg and not nudged and not out["stuck"] and not asking
                and quiet > think):
            mins = int(quiet // 60)
            if renudge:
                out["guard"] = "think"
                out["stuck"] = (f"no tool call or word for {mins} minutes, even after a nudge"
                                if not out["tools"] and not out["texts"] else
                                f"{mins} minutes again with no tool call, even after a nudge")
            else:
                nudged = True
                text = NUDGE.format(mins=mins)
                task.set_status(nudged_round=n)
                store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text,
                                                             "now": True, "by": "workbench"})
                task.event({"ts": store.now(), "round": n, "kind": "steer", "now": True,
                            "by": "workbench", "text": text})
                nudges = task.status.get("nudges", 0) + 1
                task.set_status(nudges=nudges)
                task.notify("checkin", checkin_text(task, n, out, max(started, float(
                    task.status.get("checkin_at") or 0)), prior + clock.ran(mono)) + f"\nthe runner nudged it: {mins} min of "
                    f"thinking with no tool call (nudge {nudges} this task; budget {int(think)}s)",
                    round=n)
        if out["stuck"]:
            task.event({"ts": store.now(), "round": n, "kind": "stopped", "text": out["stuck"]})
            _stop(proc)
            break
        if out["full"]:
            _stop(proc)
            break
        every = float(spec.get("checkin") or 0) * 60
        since = max(started, float(task.status.get("checkin_at") or 0))
        active = prior + clock.ran(mono)
        if every and now - since >= every and not asking:
            task.set_status(checkin_at=now)
            task.notify("checkin", checkin_text(task, n, out, since, active), round=n)
        if typical and not asking and progress.due(task, active, typical):
            mark = gitops.mark(task.worktree)
            files = gitops.changed(task.worktree)
            moved = mark != task.status.get("progress_mark", edit_mark)
            task.set_status(progress_checks=progress.passed(active, typical), progress_mark=mark,
                            progress_open=True)
            task.notify("checkin", checkin_text(task, n, out, since, active) + "\n" + progress.text(
                task, active, typical, moved, len(files.splitlines()) if files else 0,
                len(out["errors"])), round=n, progress=True)
        notes = pending_steer(task)
        if any(r.get("now") for r in notes):
            ours = all(r.get("by") == "workbench" for r in notes)
            task.event({"ts": store.now(), "round": n, "kind": "interrupted",
                        **({"by": "workbench"} if ours else {}),
                        "text": "the runner interrupted the agent with a nudge" if ours
                        else "Claude interrupted the agent"})
            out["interrupted"] = True
            _stop(proc)
            break
    # Its output closed: it exits now. One hung in its shutdown held the task and the GPU lease
    # for good (the 2026-11-18 audit, #83), so the silence limit still holds, at most EXIT_GRACE.
    grace = min(EXIT_GRACE, silence)
    try:
        out["exit"] = proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.write(f"workbench: opencode closed its output but was still running {grace:.0f}s "
                  "later: stopped\n")
        log.flush()
        _stop(proc)
        out["exit"] = proc.wait()
        out["lingered"] = True  # our stop's exit, not a crash: the round is what its events say
    log.close()
    ask_mcp.close(task, opened)  # its server is gone: closed, so nothing takes them for open
    if task.status.get("asking") or task.status.get("phase") == "asking":  # it ended
        task.set_status(asking=None, phase="running")  # mid-question: nothing reads that answer now
    if power:
        power.stop()
        power.join(timeout=1)
        st = task.status  # the round's watts too, for the Stats tab (#404): mean and peak
        task.set_status(gpu_wh=round((st.get("gpu_wh") or 0) + power.wh, 2),
                        gpu_w_sum=round((st.get("gpu_w_sum") or 0) + power.w_sum, 1),
                        gpu_w_n=(st.get("gpu_w_n") or 0) + power.n,
                        gpu_w_peak=max(st.get("gpu_w_peak") or 0, power.peak))
    out["secs"] = round(time.monotonic() - clock.t0, 1)
    out["waited"] = round(clock.ran(time.monotonic()), 1)
    # the agent's own working time, less Claude's answers and a frozen machine (#95)
    task.set_status(active_s=round(prior + clock.ran(time.monotonic()), 1))
    return out


# A test that floods its output (a print in a retry loop) must not fill the runner's memory, the
# disk or Claude's context (the 2026-11-16 audit): past TEST_OUTPUT_CAP bytes it is stopped, and
# only its first KEEP_HEAD and last KEEP_TAIL bytes are kept, for tests.txt and the tail.
TEST_OUTPUT_CAP = 20 << 20
KEEP_HEAD, KEEP_TAIL = 64 << 10, 256 << 10


class _Capture(threading.Thread):
    """Reads a test's output as it comes, keeping its head and tail and counting the rest. It
    never waits on the end of the output: a child that escaped the process group may hold the
    pipe open long after the test is done (a server started with setsid)."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.fd, self.head, self.tail = stream.fileno(), bytearray(), deque()
        self.total, self.kept, self.done = 0, 0, threading.Event()

    def run(self):
        while not self.done.is_set():
            if not select.select([self.fd], [], [], 0.2)[0]:
                continue
            chunk = os.read(self.fd, 65536)
            if not chunk:
                return
            self.total += len(chunk)
            if len(self.head) < KEEP_HEAD:
                take = KEEP_HEAD - len(self.head)
                self.head += chunk[:take]
                chunk = chunk[take:]
            if chunk:
                self.tail.append(chunk)
                self.kept += len(chunk)
                while self.kept - len(self.tail[0]) >= KEEP_TAIL:
                    self.kept -= len(self.tail.popleft())

    def text(self) -> str:
        tail = b"".join(self.tail)[-KEEP_TAIL:]
        cut = self.total - len(self.head) - len(tail)
        return (bytes(self.head) + (f"\n[... {cut} bytes of output cut ...]\n".encode() if cut > 0 else b"")
                + tail).decode(errors="replace")


POWER_EVERY = 10  # seconds between GPU power samples: fine enough for a round, cheap to read


class _Power(threading.Thread):
    """Samples the GPU's power draw every POWER_EVERY seconds while OpenCode runs a round and
    integrates it into watt-hours (the tests after it aren't counted). A None reading adds nothing
    for that interval. Each reading is the whole GPU's draw, so the watt-hours take this task's
    share of it: the draw over the live tasks holding the lease (`sharers`). Two tasks sharing a
    300 W GPU for an hour each counted 300 Wh, and the week's energy and its cost doubled (#706).
    The mean and peak stay the whole GPU's draw."""

    def __init__(self):
        super().__init__(daemon=True)
        self.done = threading.Event()
        self.wh = 0.0
        self.w_sum, self.n, self.peak = 0.0, 0, 0.0  # the samples themselves: mean and peak
        self.last: float | None = None
        self.watts: float | None = None
        self.clock = time.monotonic  # the tests' fake clock goes here

    def run(self):
        self.step(self.clock())  # the first sample is taken at once; it adds nothing yet
        while not self.done.is_set():
            self.done.wait(POWER_EVERY)
            self.step(self.clock())

    def step(self, now: float) -> None:
        """Add the last reading over the time since it, on the monotonic clock, and read again.
        An interval past FROZEN_SECS (a suspend that clock still counted, as WSL2's may) or
        backwards adds nothing: on the wall clock an 8-hour suspend mid-round added 150 W x 8 h,
        1.2 kWh, to gpu_wh, the results row and wb savings' cost, and a clock step back took
        energy away (#559)."""
        if self.last is not None and self.watts is not None and 0 <= now - self.last <= FROZEN_SECS:
            self.wh += self.watts * (now - self.last) / 3600
        self.last = now
        self.watts = self._sample()

    def _sample(self):
        watts = hardware.gpu_power_w()
        if watts is not None:
            self.w_sum, self.n, self.peak = self.w_sum + watts, self.n + 1, max(self.peak, watts)
            watts /= sharers()
        return watts

    def stop(self):
        self.done.set()


def sharers() -> int:
    """How many tasks hold this machine's GPU lease now (models.holders, those not waiting: one
    task's GPU tests are one task), at least 1: the share of the draw each one's energy counts."""
    try:
        held = models.holders()
    except OSError:
        return 1
    return max(1, len({h.get("task") or pid for pid, h in held.items() if not h.get("waiting")}))


# Shells OpenCode won't run a command in (it takes $SHELL, else bash): not POSIX `-c` shells
NOT_POSIX = {"fish", "nu", "xonsh", "elvish", "pwsh", "powershell", "tcsh", "csh"}


def test_shell() -> str:
    """The shell the agent's commands run in, so the tests run in it too: OpenCode 2.3 runs each
    command as `$SHELL -c`, or bash's when SHELL is unset (tests/RealOpenCode checks it). The
    runner used /bin/sh, which is dash on Debian and Ubuntu: `source .venv/bin/activate &&
    pytest` passed for the agent and failed for the runner, and each false failure cost an
    auto-fix round (#196, the 2026-11-19 audit)."""
    sh = os.environ.get("SHELL") or ""
    if os.path.isabs(sh) and os.access(sh, os.X_OK) and Path(sh).name not in NOT_POSIX:
        return sh
    return shutil.which("bash") or "/bin/sh"


def run_in_lane(task: store.Task, cmd: str, limit: int) -> tuple[int | str, str]:
    """Run `cmd` in the task's worktree, in the agent's lane and with its environment; the
    exit code ("timeout" past `limit` seconds, "output limit" past TEST_OUTPUT_CAP bytes) and the
    decoded output (its head and tail when long)."""
    # in the agent's lane, environment and shell: the tests run the agent's code, and must pass
    # or fail as they did when the agent ran them
    argv = lane.wrap(context.writable(task), [test_shell(), "-c", cmd], context.readable(task),
                     context.unix_paths(task))
    run = _issue()  # its own mark: sweep ends what it left, and nothing else (#324)
    proc = subprocess.Popen(argv, cwd=task.workdir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=agent_env(task, run),
                            start_new_session=True)  # its own group: a timeout ends all of it
    prev = CURRENT.get("proc")
    CURRENT["proc"] = proc  # a stop (SIGTERM, _on_term) takes the tests down too
    out = _Capture(proc.stdout)
    out.start()
    code, why, end = None, "", time.monotonic() + limit
    try:
        while code is None:
            try:
                code = proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                if time.monotonic() > end:
                    code, why = "timeout", f"[timed out after {limit}s]"
                elif out.total > TEST_OUTPUT_CAP:
                    code, why = "output limit", f"[stopped: its output passed {TEST_OUTPUT_CAP >> 20} MB]"
    finally:
        _kill_group(proc.pid)  # a timeout's remains, or what it left running in its group
        proc.wait()
        sweep(task, run)       # and what left the group
        ISSUED.discard(run)
        if prev is None:
            CURRENT.pop("proc", None)
        else:
            CURRENT["proc"] = prev
        out.join(2)            # what is still in the pipe
        out.done.set()
        out.join(1)
        proc.stdout.close()
    output = out.text()
    return code, output + (f"\n{why}" if why else "")


def clip_tail(output: str, lines: int, width: int = 300, total: int = 4000) -> str:
    """The end of a test's output for status, the report and the auto-fix prompt: the last
    `lines` lines, each cut to `width` characters, the whole to `total`. One multi-megabyte line
    (a big assertion diff, minified JS) once went whole into status.json and the notice (the
    2026-11-16 audit); the full output stays in tests.txt."""
    out = [ln if len(ln) <= width else ln[:width] + f" … ({len(ln) - width} more chars)"
           for ln in output.strip().splitlines()[-lines:]]
    s = "\n".join(out)
    return s if len(s) <= total else "… (earlier output cut)\n" + s[-total:]


# A refusal in a test's output: the phrase, and the absolute paths on its line. Not a URL's path
# (`https://h/x`), nor one inside a word.
REFUSAL = re.compile(r"permission denied|operation not permitted", re.I)
ABS_PATH = re.compile(r"(?<![\w:/.~-])/[\w.+@~-]+(?:/[\w.+@~-]+)*")
# and where a path may hold spaces (/mnt/c/Users/John Smith, ~/Library/Application Support):
# quoted, or running up to the refusal itself. ABS_PATH stopped at the first space, and the
# suggested grant walked up from `/mnt/c/Users/John` to the whole Users folder (#670). Not across
# " /", where a second path begins.
QUOTED_PATH = re.compile(r"[\"'`\u2018](/[^\"'`\u2019\n]+)[\"'`\u2019]")
MARKED_PATH = re.compile(r"(?<![\w:/.~-])(/(?:[^\n:'\"`\s]|\s(?!/))*?)(?::\s*|\s+\()"
                         r"(?:permission denied|operation not permitted)", re.I)


def refused_paths(line: str) -> list[str]:
    """The absolute paths on one line, in order: quoted ones and one that runs up to the refusal
    whole (spaces too), then ABS_PATH's that aren't part of those."""
    spans = [(m.start(1), m.end(1)) for r in (QUOTED_PATH, MARKED_PATH) for m in r.finditer(line)]
    spans += [(m.start(), m.end()) for m in ABS_PATH.finditer(line)
              if not any(a <= m.start() and m.end() <= b for a, b in spans)]
    return [line[a:b].rstrip(". ") for a, b in sorted(set(spans))]


def lane_refused(task: store.Task, output: str) -> list[str]:
    """The paths outside the task's lane a failed test run was refused, each one a grant could
    reach (`lane_grant`): /dev/kvm and /dev/fuse (#331, the audit of 1.4), and since #437 (the
    audit of 1.8) any path, an Android keystore, a Flutter lockfile, a NuGet cache, which read as
    the project's bug, and the agent spent its auto-fix rounds on them. The report names
    `wb allow --path` for each."""
    lane = context.writable(task)
    if lane is None:
        return []
    guard = context.repo_dirs(task)  # the user's checkout: no lane may write it
    found = []
    for line in output[-200_000:].splitlines():
        if not REFUSAL.search(line):
            continue
        for path in refused_paths(line):
            grant = lane_grant(path)
            if path not in found and grant and not any(
                    path == w or path.startswith(str(w).rstrip("/") + "/") for w in lane) \
                    and not any(grant == g or context._under(grant, g) for g in guard):
                found.append(path)
    return found[:5]


def lane_grant(path: str) -> str | None:
    """The `wb allow --path` that reaches a refused path: itself when it exists (a directory, a
    device node, a socket) or is a device node, else the nearest directory that does; None when no grant can (`/`,
    the home directory, a secret: context.lane_refusal)."""
    real = os.path.realpath(path)
    while not os.path.exists(real) and not real.startswith("/dev/"):  # a node: itself, or /dev
        real = os.path.dirname(real)
    return None if context.lane_refusal(real) else real


def run_tests(task: store.Task, n: int):
    cmd = task.spec.get("test_cmd")
    if not cmd:
        return None
    task.set_status(phase="testing", last=f"tests: {cmd}")
    limit = int(task.spec.get("test_timeout") or 900)
    code, output = run_in_lane(task, cmd, limit)
    task.round_dir(n).joinpath("tests.txt").write_text(
        f"$ {cmd}\nexit: {code}\nshell: {test_shell()}\n\n{output}")
    result = {"ok": code == 0, "exit": code, "tail": clip_tail(output, 40), "round": n}
    if code != 0 and (refused := lane_refused(task, output)):
        result["lane"] = refused
    if code != 0 and (gone := proxy_gap(output)):  # #380
        result["proxy"] = gone
    task.event({"ts": store.now(), "round": n, "kind": "tests", "ok": result["ok"],
                "text": f"exit {code}"})
    return result


# seconds the setup command may run (npm ci, a first cargo build), in the worktree and again in
# each scratch checkout (the base check, wb verify, wb shot), unless the task says otherwise
SETUP_SECS = 1800


def setup_limit(spec: dict) -> int:
    """The task's setup limit: task.json `setup_timeout` (wb start --setup-timeout, or its repo's
    commands.json entry), else SETUP_SECS. A fixed 1800 s failed a big first build before round
    1 every time, and starting again hit it again (#520)."""
    v = spec.get("setup_timeout")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else SETUP_SECS


def setup_timed_out(spec: dict) -> str:
    """What a setup past its limit says, the limit and how to raise it named."""
    return (f"timed out after {setup_limit(spec)}s; wb start --setup-timeout SECS, or "
            '"setup_timeout" in the repo\'s commands.json "repos" entry, raises it')


def _copy(src: Path, dest: Path) -> None:
    """`src` to `dest` whole, links kept, as a reflink where the filesystem has them (btrfs, XFS:
    node_modules in an instant), else a plain copy; anything already at `dest` replaced."""
    def clear():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
    clear()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # GNU's, then macOS's (-c clones on APFS); their own error when neither runs
    for argv in (["cp", "-a", "--reflink=auto"], ["cp", "-cRp"]):
        try:
            if subprocess.run([*argv, str(src), str(dest)], capture_output=True).returncode == 0:
                return
        except OSError:
            pass
        clear()
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)


def prepare(task: store.Task, prepared: Path | None = None) -> tuple[int | str, str]:
    """Make `task.worktree` the project as the user has it, before anything runs there: the
    links beside it to the repo's `siblings`, the ignored paths to `copy` from the user's
    checkout, then the `setup` command in the lane (#201, the 2026-11-19 audit: a worktree holds
    only committed files, so a check that needed node_modules, a .venv or ../lib failed on the
    base and in every round, for that and not for the step). The setup's exit code and output;
    (0, "") with nothing to do. A scratch checkout passes the task's ``prepared`` worktree: the
    ignored paths its preparation left (status `prepared_paths`, the setup's work and the copies)
    are copied from there, and the setup isn't run again. The base check, wb verify (twice) and
    wb shot each ran it from nothing, up to SETUP_SECS: a first build compiled three to five
    times a task (#249, the audit of 1.2)."""
    spec, wt = task.spec, task.worktree
    repo = Path(spec["repo"])
    made = task.status.get("prepared_paths") if prepared else None
    # A virtualenv names the checkout it was made in (pyvenv.cfg's home is fine, its activate
    # and its scripts' shebangs aren't): copied, the base check and wb verify ran the task's
    # worktree's interpreter and code, and failed where the task passed (#325, the audit of
    # 1.4). Such a checkout is prepared afresh, the setup run in it. So is one holding a build
    # tree configured for the worktree (CMake's CMakeCache.txt): copied, its targets ran the
    # candidate's code on the base, and verify said the test already passed there (#424).
    if made and any(venv_in(prepared / p) or _within(build_source(prepared / p), prepared)
                    for p in made):
        made = None
    # And one whose prepared paths the rounds rebuilt: the agent rebuilt a monorepo's
    # packages/lib/dist (or a C extension in place), the base got the candidate's build, its tests
    # imported it and passed, and verify said `base-passes` for a valid fix (#732, the 2026-11-21
    # audit). Recorded at setup (status `prepared_marks`); a task prepared before them copies as before.
    if made and (marks := task.status.get("prepared_marks")) and \
            any(p in marks and prepared_mark(prepared / p) != marks[p] for p in made):
        made = None
    # a scratch checkout (the base check's, wb verify's, wb shot's), not the task's own worktree
    own = store.Task(task.name).worktree if getattr(task, "name", None) else prepared
    scratch = own is None or os.path.realpath(wt) != os.path.realpath(own)
    others = [repo, *(p for p in (prepared, own) if p)]
    try:
        for name in spec.get("siblings") or ():  # where the repo's own paths reach them (#261)
            if not (at := context.sibling_link(wt, name)).is_symlink():
                at.parent.mkdir(parents=True, exist_ok=True)
                at.symlink_to(context.sibling_real(repo, name))
        if made is not None:
            # The two worktrees resolved first: a symlinked state directory (WB_HOME a link to
            # another volume, which store.absolute_env keeps) is trusted, and only a link inside
            # a worktree refuses the copy. Comparing each parent with its own resolution refused
            # every prepared input under a linked home (#345, the audit of 1.4).
            roots = prepared.resolve(), wt.resolve()
            for p in made:
                src, dest = prepared / p, wt / p
                # A later step can track an earlier build path or change its parent to a link.
                # Keep that checkout's own file and confine copies to the two worktrees (#249).
                if os.path.lexists(dest):
                    continue
                if (src.parent.resolve() != (roots[0] / p).parent
                        or dest.parent.resolve() != (roots[1] / p).parent
                        or not os.path.lexists(src)):
                    return "copy failed", f"prepared input {p} changed; use --setup for a fresh checkout"
                _copy(src, dest)
            return _foreign_builds(wt, made, others)
        for p in spec.get("copy") or ():
            _copy(repo / p, wt / p)
        if scratch and (code := _foreign_builds(wt, spec.get("copy") or (), others))[0] != 0:
            return code
    except OSError as e:
        return "copy failed", str(e)
    if not (cmd := spec.get("setup")):
        return 0, ""
    return run_in_lane(task, cmd, setup_limit(spec))


# what a test run rewrites in a prepared path without changing what the code is: Python's bytecode,
# the runners' and linters' caches. A directory holding CACHEDIR.TAG (cargo's target/, pytest's
# and ruff's caches) is one too: cargo rebuilds what changed itself, so its copy is safe (#732).
PREP_CACHES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
                         ".cache", ".vite", ".turbo", ".parcel-cache", ".nyc_output", ".eslintcache"})


def prepared_mark(path: Path) -> str:
    """A fingerprint of ``path`` as it is now, from lstat alone: the count, total size and a sum of
    each file's name, size and mtime (order-free, the same in every process), caches left out
    (PREP_CACHES). '' when it is gone. What tells a prepared path rebuilt since the setup (#732)."""
    if not os.path.lexists(path):
        return ""
    count = size = total = 0
    stack = [(path, "")]
    while stack:
        at, rel = stack.pop()
        try:
            st, entries = at.lstat(), None
            if stat.S_ISDIR(st.st_mode):
                with os.scandir(at) as it:
                    entries = list(it)
        except OSError:
            continue
        if entries is not None:
            if any(e.name == "CACHEDIR.TAG" for e in entries):
                continue
            stack.extend((Path(e.path), f"{rel}/{e.name}") for e in entries
                         if e.name not in PREP_CACHES)
            continue
        if rel.endswith(".pyc"):
            continue
        count += 1
        size += st.st_size
        total = (total + zlib.crc32(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}".encode(
            "utf-8", "surrogateescape"))) % (1 << 64)
    return f"{count}:{size}:{total:x}"


def build_source(path: Path) -> Path | None:
    """The source directory the build tree at ``path``, or one level inside it, was configured
    for, as its build system recorded it (absolute): CMake's CMakeCache.txt
    (CMAKE_HOME_DIRECTORY), Meson's meson-info.json; else None."""
    if not path.is_dir() or path.is_symlink():
        return None
    try:
        dirs = [path, *sorted(d for d in path.iterdir() if d.is_dir() and not d.is_symlink())]
    except OSError:
        return None
    for d in dirs:
        try:
            cache = d / "CMakeCache.txt"
            if cache.is_file() and not cache.is_symlink():
                with cache.open(errors="replace") as fh:
                    for line in fh:
                        if line.startswith("CMAKE_HOME_DIRECTORY:"):
                            return Path(line.partition("=")[2].strip())
            info = d / "meson-info" / "meson-info.json"
            if info.is_file() and not info.is_symlink():
                return Path(json.loads(info.read_text())["directories"]["source"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _within(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r.rstrip("/") + "/")


def _foreign_builds(checkout: Path, paths, others) -> tuple[int | str, str]:
    """(0, "") unless a build tree among ``paths`` in ``checkout`` (a scratch one) was configured
    for another checkout's tracked sources (the user's, the task's worktree): built there, it
    compiles and runs that checkout's code, so a check of the base ran the candidate's (#424, the
    audit of 1.8). A dependency's build whose source is ignored, the same everywhere, is fine."""
    for p in paths:
        src = build_source(checkout / p)
        if src is None or _within(src, checkout):
            continue
        for root in others:
            if not _within(src, root):
                continue
            rel = os.path.relpath(os.path.realpath(src), os.path.realpath(root))
            try:
                tracked = gitops.git(checkout, "ls-files", "-z", "--",
                                     "." if rel == "." else f":(literal){rel}")
            except gitops.GitError:
                tracked = "?"
            if tracked:
                return "copy failed", (
                    f"{p} is a build tree configured for {src}: here, its build would compile and "
                    "run that checkout's code, not this one's. Make it in the setup command "
                    "(wb start --setup; wb verify --setup and wb shot --setup run it in each "
                    "checkout) rather than copying it")
    return 0, ""


def venv_in(path: Path) -> Path | None:
    """The virtualenv at ``path`` or one level inside it (its pyvenv.cfg), else None."""
    if not path.is_dir() or path.is_symlink():
        return None
    if (path / "pyvenv.cfg").is_file():
        return path
    try:
        return next((d for d in sorted(path.iterdir()) if d.is_dir() and not d.is_symlink()
                     and (d / "pyvenv.cfg").is_file()), None)
    except OSError:
        return None


def _ignored_paths(wt: Path) -> list[str]:
    """The worktree's ignored files and directories (a directory once, not its files)."""
    out = gitops.git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    return [p.rstrip("/") for p in out.split("\0") if p.rstrip("/")]


def checked_out(task: store.Task) -> bool:
    """Whether the task's worktree has been made: by wb start itself for a task started before
    #529, else by the runner (task.json `checkout`, status `checked_out`)."""
    return task.spec.get("checkout") != "runner" or bool(task.status.get("checked_out"))


def pending_checkouts() -> int:
    """The bytes the open tasks' checkouts will still write: task.json `checkout_bytes` of each
    one whose runner hasn't made its worktree yet (a failed one never will). wb start counts them
    as taken (#676)."""
    return sum(int(t.spec.get("checkout_bytes") or 0) for t in store.all_tasks()
               if t.status.get("phase") not in store.CLOSED | {"failed"}
               and not checked_out(t))


# What a checkout leaves free at the least, and wb start's spare beside a task (cli.DISK_SPARE)
CHECKOUT_SPARE = 2 * 10**9


def _checkout_failed(task: store.Task, what: str, fix: str) -> bool:
    task.set_status(phase="failed", last=what[:200])
    task.notify("failed", f"task {task.name}: {what}, before any round. {fix}")
    return False


def checkout(task: store.Task) -> bool:
    """Once, first: the task's worktree and branch at its base, its submodules filled, then the
    --copy paths checked against the base's own ignore rules. wb start made all three, and on a
    big repo, one on /mnt/c or with Git LFS data the checkout passed Claude's two-minute Bash
    limit; the next start made the same checkout and was killed again (#529). A checkout cut
    short (a kill, a reboot) is made again from nothing: the agent never ran there."""
    if checked_out(task):
        return True
    spec, wt = task.spec, task.worktree
    # Claude has had this worktree (wb takeover refuses one not checked out, but not a task taken
    # over before it did): never removed and made again under Claude's edits (#655)
    if task.status.get("taken_over") and os.path.lexists(wt):
        return _checkout_failed(task, "the worktree was handed to Claude before its checkout "
                                "finished", f"wb discard {task.name} and start again")
    repo = Path(spec["repo"])
    task.set_status(last="checking out the base")
    # the disk may have filled since wb start measured it: a checkout cut short by a full disk
    # failed the status writes that would have said so (#342, #676)
    free, size = store.free_bytes(), int(spec.get("checkout_bytes") or 0)
    if free is not None and size and free < size + CHECKOUT_SPARE:
        return _checkout_failed(
            task, f"{store.gb(free)} free where the state directory is, and its checkout needs "
            f"{store.gb(size)} and 2 GB spare", f"Free some, then wb discard {task.name} and start again")
    began = time.monotonic()
    branch = gitops.branch_name(task.name)
    try:
        if os.path.lexists(wt):  # an earlier run's, cut short: unlocked and removed
            if warn := gitops.remove_worktree(repo, wt):
                raise gitops.GitError(warn)
        tip = gitops.git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}",
                         check=False).strip()
        made = task.status.get("branch_made")
        if tip and tip == made:  # this task's own, from that run: deleted only while unmoved
            gitops.delete_branch(repo, task.name, at=made)
        elif tip:  # an earlier task's, or moved since: never this checkout's to delete (#416)
            raise gitops.GitError(f"branch {branch} already exists in {repo}, and this task "
                                  "didn't make it")
        # recorded before worktree add makes it, so every cleanup of a checkout cut short (wb
        # discard, this rerun) deletes only the branch this task made, at that commit (#416)
        task.set_status(branch_made=spec["base"])
        wt.parent.mkdir(parents=True, exist_ok=True)
        gitops.create_worktree(repo, task.name, wt, spec["base"])
    except (gitops.GitError, OSError) as e:
        return _checkout_failed(task, f"making the worktree failed ({e})",
                                f"Fix it, then wb discard {task.name} and start again")
    # Ignored in the checkout isn't ignored where the task starts: at a tag older than the ignore
    # rule, a copied local-input.dat was a new file in the worktree, and wb accept committed it
    # (#322, the audit of 1.4). The task's worktree has the base's own .gitignore files.
    # (a directory as `dir/`: nothing is there yet to tell a `dir/` rule it is one)
    if loose := [p for p in spec.get("copy") or ()
                 if not gitops.ignored(wt, p + "/" * (repo / p).is_dir())]:
        return _checkout_failed(
            task, f"--copy {loose[0]}: ignored in {repo}, but not at {spec.get('base_ref') or spec['base']}, "
            "where the task starts: a copy would show in the review diff and be committed",
            f"wb discard {task.name}, ignore it in a commit the base holds or start from a newer "
            "--base, then start again")
    gitops.fill_submodules(repo, spec["base"], wt)  # the ones it can't, wb start said
    task.set_status(checked_out=True, checkout_secs=round(time.monotonic() - began, 1))
    return True


# How many tasks may run their setup or base check at once on this machine. Both run before the
# GPU lease, so a batch of N queued tasks ran N installs and N test suites together, beside the
# running agent: a small machine swapped, base checks timed out "not checked", and a test server
# on a fixed port failed EADDRINUSE in all but one clone, a base failure the base doesn't have
# (#590, the audit of 1.90). Taken in queue order (task.json created_at), in locks/, which no
# lane reads: prepare-<n>.lock the slots, prepare.json the queue.
PREP_SLOTS = 1


def _prep_queue() -> Path:
    return store.home() / "locks" / "prepare.json"


@contextlib.contextmanager
def prep_turn(task: store.Task):
    """Hold one of PREP_SLOTS, waiting behind the tasks queued before this one (status `last`
    names the one it waits for). A holder that died lets go: the kernel drops its flock, and
    its queue entry is passed over once its pid is gone."""
    import fcntl
    path = _prep_queue()
    path.parent.mkdir(parents=True, exist_ok=True)
    me = str(os.getpid())
    mine = {"task": task.name, "since": task.spec.get("created_at") or store.now(),
            "start": models._started(os.getpid()), "holding": False}

    def update(holding=None, leave=False) -> list:
        with store.locked(path.with_name(path.name + ".lock")):
            rows = {pid: r for pid, r in (store.read_json(path, {}) or {}).items()
                    if pid != me and isinstance(r, dict) and pid.isdigit()
                    and models._alive(int(pid), r.get("start"))}
            if not leave:
                rows[me] = {**mine, "holding": bool(holding)}
            store.write_json(path, rows)
            return sorted(((r.get("since") or 0, int(pid), r) for pid, r in rows.items()),
                          key=lambda x: x[:2])

    fh, said = None, None
    try:
        while fh is None:
            queue = update()
            waiting = [(pid, r) for _, pid, r in queue if not r.get("holding")]
            if waiting and waiting[0][0] == os.getpid():
                for i in range(PREP_SLOTS):
                    f = open(path.with_name(f"prepare-{i}.lock"), "a+")
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        f.close()
                        continue
                    fh = f
                    break
            if fh is None:
                ahead = next((r["task"] for _, pid, r in queue if r.get("holding")), None) or \
                    next((r["task"] for pid, r in waiting if pid != os.getpid()), None)
                if ahead and ahead != said:
                    task.set_status(phase="queued", last=f"waiting for {ahead}'s setup or base check "
                                    "(one at a time on this machine)")
                    said = ahead
                time.sleep(1)
        update(holding=True)
        yield
    finally:
        if fh:
            fh.close()  # the flock goes with it
        update(leave=True)


def _prepare(task: store.Task) -> bool:
    """Once, before the base check and the GPU: prepare() the worktree, its output in
    setup.txt. Failing, the task fails before any round, saying what the setup printed."""
    spec = task.spec
    if task.status.get("prepared") or not any(spec.get(k) for k in ("setup", "copy", "siblings")):
        return True
    with prep_turn(task):  # one task's setup at a time on this machine (#590)
        return _prepare_now(task)


def _prepare_now(task: store.Task) -> bool:
    spec = task.spec
    task.set_status(last="setting up the worktree"
                    + (f": {spec['setup']}" if spec.get("setup") else ""))
    began = time.monotonic()
    code, output = prepare(task)
    secs = round(time.monotonic() - began, 1)
    task.dir.joinpath("setup.txt").write_text(
        f"$ {spec.get('setup') or ''}\nexit: {code}\nseconds: {secs}\nshell: {test_shell()}\n\n{output}")
    if code == 0:
        try:
            made = _ignored_paths(task.worktree)  # what the scratch checkouts copy (#249)
        except gitops.GitError:
            made = None
        marks = {p: prepared_mark(task.worktree / p) for p in made} if made else None  # #732
        task.set_status(prepared=True, prepared_paths=made, prepared_marks=marks, setup_secs=secs)
        return True
    what = f"the setup command failed (exit {code})" if isinstance(code, int) else \
        f"setting up the worktree failed ({setup_timed_out(spec) if code == 'timeout' else code})"
    tail = "\n".join("    " + x for x in clip_tail(output, 8, total=1500).splitlines())
    proxy = f"\n{proxy_line(gone)}, if the setup needed it" if (gone := proxy_gap(output)) else ""
    task.set_status(phase="failed", last=what, infra="setup", failed_before="setup")  # #508
    # resume, not discard and start again: the agent never ran, and wb resume sets up again (#581)
    task.notify("failed", f"task {task.name}: {what} before any round "
                f"({task.dir / 'setup.txt'}):\n{tail}{proxy}\nfix it, then wb resume {task.name}: "
                "it sets the worktree up again (the agent never ran)")
    return False


# The base check runs in the runner before round 1 (it once ran inside `wb start`, which a slow
# suite pushed past Claude's two-minute Bash limit: the 2026-11-16 audit). Past this, not checked.
BASE_CHECK_SECS = 120


def base_check(task: store.Task) -> dict | None:
    """The acceptance command run once on the base, before the agent starts: it should fail, and
    only for what the step changes, which Claude checks from the tail (the runner can't tell).
    Passing already, it can't tell when the step is done. In a scratch checkout of the base, not
    the task's worktree: what it wrote there (a build stamp, a fixture, an updated snapshot)
    stayed for the agent, could make a later test pass, and showed in the review diff (#192,
    the 2026-11-19 audit)."""
    cmd = task.spec.get("test_cmd")
    if not cmd:
        return None
    limit = min(BASE_CHECK_SECS, int(task.spec.get("test_timeout") or 900))
    from . import verify  # verify imports this module
    code, output = verify.on_base(task, cmd, limit, say=lambda msg: task.set_status(last=msg),
                                  turn=lambda: prep_turn(task))  # one at a time, as setups (#590)
    task.dir.joinpath("base-tests.txt").write_text(
        f"$ {cmd}\nexit: {code}\nshell: {test_shell()}\n\n{output}")
    return {"ok": code == 0, "exit": code, "tail": clip_tail(output, 8, total=1500), "limit": limit}


def base_check_line(r: dict, task: store.Task) -> str:
    """What the acceptance command did on the base, for Claude to judge (the first report)."""
    if r["exit"] == "setup failed":
        tail = "\n".join("    " + line for line in r["tail"].splitlines())
        return (f"note: setting up the base's scratch checkout failed, so the base wasn't checked "
                f"({task.dir / 'base-tests.txt'}):\n{tail}")
    if r["exit"] == "timeout":
        limit = r.get("limit", BASE_CHECK_SECS)
        if limit < BASE_CHECK_SECS:  # --test-timeout set it: every round's tests stop there too
            return (f"warning: the acceptance command ran past --test-timeout ({limit}s) on the "
                    "base, so every round's tests will time out too: raise --test-timeout or "
                    "narrow the command (wb discard, then start again)")
        full = int(task.spec.get("test_timeout") or 900)  # #496: an 18-minute suite under 15
        return (f"note: the acceptance command ran past {limit}s on the base; not checked"
                + (f". If it runs that long, a round's run may pass --test-timeout ({full}s) too: "
                   "raise it for a slow suite" if full > limit else ""))
    if r["ok"]:
        # the usual case when the agent writes the step's tests: a warning that said to discard
        # and start again cost a whole restart in one task of two (the 2026-11-17 measurement)
        return ("note: the acceptance command already passes on the base, so only the tests this "
                "step adds can fail it: check the diff adds tests that fail without the change")
    tail = "\n".join("    " + line for line in r["tail"].splitlines())
    # an instruction, not a claim: the runner can't know why it fails (the 0.90 review: "as it
    # should, but only for what the step changes" read as checked, for a broken build too)
    return (f"on the base the acceptance command fails (exit {r['exit']}): check the tail shows it "
            f"failing only for what the step changes ({task.dir / 'base-tests.txt'}):\n{tail}")


# characters (~8K tokens): a diff this size comes with the review notice. Claude reads all of it
# anyway, and fetching it is a call over its whole session: at 12,000 both significant tasks of
# 2026-11-18 (13K and 15K characters) cost that extra call.
REVIEW_DIFF_MAX = 32000


# The instruction files an agent may propose a new version of (`<name>.proposed`, beside it): it
# can't edit them (context.CLAUDE_FILES), and Claude making each change itself was a call over its
# whole context on most tasks (2026-11-18). `wb accept --take-claude-md` takes them.
PROPOSABLE = ("CLAUDE.md", "AGENTS.md")


def proposals(worktree: Path) -> list[tuple[Path, Path]]:
    """(the instruction file, the agent's proposed new version) for each proposal in the
    worktree: a new file, untracked at the step's start (HEAD), never a link (it could point
    anywhere). A `CLAUDE.md.proposed` the project already tracked was settled too, and accept
    deleted a draft the agent never touched and the review never showed; and an rglob walked
    every copied node_modules on each review (#321, the audit of 1.4). Git skips ignored paths."""
    try:
        listed = gitops.git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--",
                            *(f":(glob)**/{name}.proposed" for name in PROPOSABLE))
    except gitops.GitError:  # no worktree (gone, or not a repo): nothing to settle
        return []
    out = []
    for rel in sorted(p for p in listed.split("\0") if p):
        prop = worktree / rel
        if not prop.is_symlink() and prop.is_file():
            out.append((prop.with_name(prop.name.removesuffix(".proposed")), prop))
    return out


def proposal_diff(task: store.Task) -> str:
    import difflib
    parts = []
    for real, prop in proposals(task.worktree):
        rel = real.relative_to(task.worktree)
        # against the file the agent read: a gitignored CLAUDE.md is the checkout's copy, and
        # the whole proposal read as new, diffed against nothing (#506, Claude's audit of 1.90)
        local = Path(task.spec["repo"]) / rel
        read = real if real.is_file() and not real.is_symlink() else local if (
            local.is_file() and gitops.ignored(local.parent, local.name)) else None
        old = read.read_text(errors="replace").splitlines(keepends=True) if read else []
        d = "".join(difflib.unified_diff(old, prop.read_text(errors="replace").splitlines(
            keepends=True), f"a/{rel}", f"b/{rel}"))
        parts.append(f"\n\nthe agent proposes this change to {rel} (wb accept --take-claude-md "
                     f"takes it; without it, accept drops it):\n{d or '(no change)'}")
    return "".join(parts)


def review_diff(task: store.Task) -> str:
    """The diff under review, for the review notice, when it is small; else its stat. Claude reads
    it anyway, and each call to fetch it re-reads the whole session: a wake that carries it saves
    the review one to three calls (the 2026-11-17 measurement). After Claude's feedback on the
    same step, only what changed since the last review notice: the wake after a feedback round
    carried the whole 14K-character diff again, which Claude already had (2026-11-18). Records
    the snapshot it showed (status `reviewed`)."""
    steps = task.status.get("steps", 1)
    base = "HEAD" if steps > 1 else task.spec["base"]
    last = task.status.get("reviewed") or {}
    since = last.get("tree") if last.get("step") == steps else None
    # Only the proposals accept settles (proposals()) are left out of the code's diff: every
    # `*.proposed` was, so an ordinary schema.proposed read as "no changes" and was committed
    # all the same (#79, the 2026-11-18 audit).
    props = [str(p.relative_to(task.worktree)) for _, p in proposals(task.worktree)]
    code = (".", *(f":(exclude,literal){p}" for p in props))
    try:
        tree, left = gitops.snapshot_left(task.worktree)  # bulky new files left out (#431)
        task.set_status(reviewed={"step": steps, "tree": tree})
        left = gitops.left_line(left)
        if since:
            # bounded: a big diff is never read whole only to show its stat (#348, the audit of 1.4)
            full = gitops.diff(task.worktree, since, paths=code, limit=REVIEW_DIFF_MAX)
            same = not props or not gitops.diff(task.worktree, since,
                                                 paths=tuple(f":(literal){p}" for p in props))
            prop = ("\n\nthe agent's proposed instruction files: unchanged since your last review"
                    if same and proposals(task.worktree) else proposal_diff(task)) + left
            if full == "":
                return "\n\nthe diff since your last review: (no changes)" + prop
            if full is not None:
                return (f"\n\nthe diff since your last review (wb diff {task.name} for all of "
                        f"it):\n{full}" + prop)
            base = since
        full = gitops.diff(task.worktree, base, paths=code, limit=REVIEW_DIFF_MAX)
        prop = proposal_diff(task) + left
        if full == "":
            return "\n\nthe diff: (no changes)" + prop
        if full is not None:
            return (f"\n\nthe diff (all of it; wb diff {task.name} again only after a change):"
                    f"\n{full}" + prop)
        return (f"\n\nthe diff is over {REVIEW_DIFF_MAX} characters, too big to include: "
                f"its stat:\n{gitops.diff(task.worktree, base, stat=True, stat_count=gitops.CHANGED_SHOWN)}" + prop)
    except (gitops.GitError, OSError):
        return ""


def needs_base_check(task: store.Task) -> bool:
    """Whether the base check is still to run: a --test task (not --no-base-check) with none
    recorded, on its first step and before any round has reported. Gated on round 1, a task that
    failed or was stopped before its first round (a server down at start, wb stop during the
    setup) and was then resumed, round 2, ran without one, and its report didn't say so (#510,
    the audit of 1.90)."""
    st = task.status
    return bool(task.spec.get("test_cmd") and not task.spec.get("no_base_check")
                and st.get("base_check") is None and st.get("steps", 1) == 1
                and not st.get("reviews") and not any(task.dir.glob("rounds/*/summary.md")))


def _base_check(task: store.Task) -> None:
    """Before round 1, once: the base check, its result in status `base_check` and the first
    report, and a check-in at once when it says the command can't do its job."""
    task.set_status(last="checking the acceptance command on the base")
    r = base_check(task)
    task.set_status(base_check=r, last="waiting for the GPU")
    if r["exit"] == "timeout" and r["limit"] < BASE_CHECK_SECS:  # every round's tests will too
        task.notify("checkin", f"task {task.name}: {base_check_line(r, task)}",
                    round=task.status.get("round", 1))


def steps_line(task: store.Task) -> str | None:
    """For a stuck or failed step past the first: the steps `wb next` committed are approved
    work, which wb discard keeps on the branch, and how to land them instead. A discard once
    force-deleted them with the step that stuck (#494, the audit of 1.90)."""
    steps = task.status.get("steps", 1)
    if steps <= 1:
        return None
    wt, one = shlex.quote(str(task.worktree)), steps == 2
    return (f"{'step 1' if one else f'steps 1-{steps - 1}'} committed on {task.spec['branch']} "
            f"(wb next): wb discard {task.name} drops this step and keeps that branch; to land "
            f"{'it' if one else 'them'} instead, drop this step's edits "
            f"(git -C {wt} reset -q --hard && git -C {wt} clean -fdq) and wb accept {task.name} --force")


def summary_text(task: store.Task, n: int, rnd: dict, tests) -> str:
    """The compact report Claude reads; kept short on purpose, the diff is fetched separately."""
    spec, st = task.spec, task.status
    lines = [f"task {task.name} · {spec['model_label']} · round {n} · {rnd['secs'] / 60:.1f} min"
             f" · local tokens {rnd['tok_in']} in / {rnd['tok_out']} out"]
    c = context_state(task)
    if c.get("used") and c.get("window"):
        lines.append(f"agent context: {c['used'] / 1000:.1f}K of {c['window'] // 1024}K "
                     f"({100 * c['used'] // c['window']}%)"
                     + (f", compacted {c['compactions']}x" if c.get("compactions") else "")
                     + ("; the next round starts a fresh session" if fresh_reason(task) else ""))
    full = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0]
            if e.get("kind") == "reading_full" and e.get("round") == n]
    if full:
        lines.append("reading: " + full[-1]["text"])
    if rnd["stuck"]:
        lines.append(f"STOPPED: {rnd['stuck']}")
    elif rnd["exit"] not in (0, None):
        lines.append(f"opencode exited {rnd['exit']}"
                     + (": the round didn't finish; what it changed so far stays in the worktree"
                        if died(rnd) and gitops.changed(task.worktree) else "")
                     + f" (see {task.dir / 'runner.log'})")
    if rnd.get("retries"):  # #491: a request cut at the provider's timeout, or refused, and resent
        why = rnd["retries"][-1]
        lines.append(f"OpenCode resent {len(rnd['retries'])} request(s) to the model's server: {why}"
                     + ("; it cut the request at the provider's timeout" if "Timed out" in why else ""))
    if (rnd["stuck"] or rnd["exit"] not in (0, None)) and (kept := steps_line(task)):
        lines.append(kept)
    if rnd["exit"] == 0 and not (ask_mcp.ask_dir(task) / ask_mcp.LISTED).exists():
        # an OpenCode that stopped loading the server would leave the agent no way to ask, and
        # nothing said so (the 2026-11-19 audit)
        lines.append("NOTE: the agent's OpenCode never listed workbench_ask_reviewer, so it can't "
                     f"ask you anything; PWD={task.workdir} OPENCODE_CONFIG="
                     f"{context.rules_dir(task) / 'opencode.json'} opencode mcp list --print-logs says why")
    if tests is not None:
        lines.append(f"tests: {'PASS' if tests['ok'] else 'FAIL'} (`{spec['test_cmd']}`, "
                     f"exit {tests['exit']})")
        if not tests["ok"]:
            lines.append("```\n" + "\n".join(tests["tail"].splitlines()[-25:]) + "\n```")
        for node in tests.get("lane") or ():
            grant = lane_grant(node) or node
            lines.append(f"the lane refused {shlex.quote(node)}: wb allow {task.name} --path "
                         f"{shlex.quote(grant)} if the "
                         "tests need it (not the agent's bug)")
        if tests.get("proxy"):  # #380
            lines.append(f"{proxy_line(tests['proxy'])}, if the tests need the network (not the "
                         "agent's bug)")
    if st.get("setup_secs") is not None and not st.get("reviews") and st.get("steps", 1) == 1:
        lines.append(f"setup: {st['setup_secs']:.0f}s, once; the scratch checkouts copy what it made")
    if st.get("base_check") and not st.get("reviews") and st.get("steps", 1) == 1:
        lines.append("base check: " + base_check_line(st["base_check"], task))
    if st.get("auto_fixes"):
        lines.append(f"auto-fix rounds used: {st['auto_fixes']}/{spec.get('auto_fix', 0)}")
    tools = ", ".join(f"{k}×{v}" for k, v in sorted(rnd["tools"].items())) or "none"
    lines.append(f"tools: {tools}")
    for err in rnd["errors"][-4:]:
        lines.append(f"tool error: {err[:200]}")
    final = (rnd["texts"][-1] if rnd["texts"] else "(the agent wrote no final message)").strip()
    lines += ["", "agent's summary:", final[:2500], ""]
    files = gitops.changed(task.worktree)
    lines += ["files changed:", "\n".join(gitops.brief(files.splitlines(), status=True)) or "(none)"]
    for d, k in gitops.build_dirs(task.worktree, files)[:gitops.CHANGED_SHOWN]:
        lines.append(f"NOTE: {d} holds {k:,} new files the project doesn't ignore: build output?")
    if extra := outside_spec(task, files):
        lines.append("NOTE: the spec doesn't name " + ", ".join(gitops.brief(extra))
                     + ": needed for the task, or drift?")
    if mine := claude_files_changed(files):
        lines.append(f"NOTE: the agent changed your files ({', '.join(gitops.brief(mine))}): instructions and "
                     ".claude/ are yours to write; keep, rewrite or revert them")
    if nested := gitops.nested_repos(task.worktree):
        lines.append(gitops.nested_line(nested))
    lines += [f"NOTE: {e}" for e in gitops.submodule_edits(task.worktree)]  # #419
    try:  # a git that refuses the diff costs the report a line, not the task (#493)
        stat = gitops.diff(task.worktree, spec["base"], stat=True).splitlines()
    except gitops.GitError as e:
        stat = [f"(no diffstat: {(str(e).splitlines() or [''])[0][:300]})"]
    if stat:
        lines.append(stat[-1].strip())
    return "\n".join(lines)


# A round continues the task's OpenCode session, so the agent keeps what it has read. Once that
# session has filled a share of the model's window (the fresh_at knob, tuning.py: learned per
# model and window from how tasks went), or OpenCode has compacted it
# (a small model's own summary of a coding session drops details), the next round starts a fresh
# session that is handed exactly what it needs: the task, the work so far and the new instructions.
ASKED_FRESH = "Claude asked for a fresh session"
COMPACTED = "OpenCode had to compact the last session"
# The handoff carries the diff while it is under this share of the window (at ~4 characters a
# token: 12K characters, ~3K tokens, on a 32K one); a bigger diff is listed, for the agent to read.
HANDOFF_DIFF_SHARE = 0.09
HANDOFF_DIFF_MAX = 60_000  # characters, whatever the window: the prompt is one argv string (see run_round)


def peak(st: dict, used) -> dict:
    """status `peak_share`, the fullest any of the task's sessions got (a share of the window),
    raised to ``used`` when that is more: whether fresh_at could act on the task (#721)."""
    window = st.get("window")
    if not (window and isinstance(used, (int, float)) and used / window > (st.get("peak_share") or 0)):
        return {}
    return {"peak_share": round(used / window, 3)}


def context_state(task: store.Task) -> dict:
    """How full the task's current session is: {"used", "window", "compactions"}."""
    sid = task.status.get("session")
    if not sid:
        return {}
    from .transcript import export  # transcript imports this module
    got = export(sid, strict=True)
    if got is None:
        # a timeout or a busy store read as an empty session: no usage, no compaction, so a full
        # or compacted one carried on, likeliest with several tasks sharing the store (#262, the
        # audit of 1.2). The round's own last step says how full it was.
        live = task.status.get("live_used") or {}
        task.event({"ts": store.now(), "round": task.status.get("round", 1), "kind": "export",
                    "text": "opencode session export failed: judged by the round's last step"})
        return {"used": live.get("used") if live.get("session") == sid else None,
                "window": task.status.get("window"), "compactions": 0}
    msgs = got.get("messages", [])
    def carried(tk) -> int:  # what the next request carries: all the message read, plus its reply
        if not isinstance(tk, dict):
            return 0
        cache = tk.get("cache") if isinstance(tk.get("cache"), dict) else {}
        return sum(n for n in (tk.get("input"), cache.get("read"), cache.get("write"), tk.get("output"),
                               tk.get("reasoning"))  # as OpenCode's compaction counts it (#644)
                   if isinstance(n, (int, float)))
    # the last one that counted anything: an aborted step recorded with every count 0 (an
    # interrupt) read as an empty session and skipped the between-rounds check (#481)
    last = next((m for m in reversed(msgs) if m.get("type") == "assistant" and carried(m.get("tokens"))), None)
    used = carried(last["tokens"]) if last else None
    if more := peak(task.status, used):
        task.set_status(**more)
    return {"used": used, "window": task.status.get("window"),
            "compactions": sum(m.get("type") == "compaction" and m.get("status") == "completed"
                               for m in msgs)}


def fresh_reason(task: store.Task, st: dict | None = None) -> str | None:
    """Why round n should start a fresh session, or None to continue the current one. ``st``: the
    status to judge by, when it isn't written yet (wb next, before its one status write)."""
    st = task.status if st is None else st
    if st.get("context") == "fresh":  # even with no session yet: the prompt still needs the task
        return ASKED_FRESH
    if not st.get("session"):  # the step's first round, or one whose session never started
        first = (st.get("step_round") or 1) == st.get("round", 1)
        return None if first else "the last session ended before it began"
    if st.get("context") == "keep":
        return None
    if st.get("context") == "full":
        return f"the last session was {st.get('full_pct')}% full"
    c = context_state(task)
    if c.get("compactions"):
        return COMPACTED
    if c.get("used") and c.get("window") and c["used"] >= tuning.value(task, "fresh_at") * c["window"]:
        return (f"the last session was {100 * c['used'] // c['window']}% full "
                f"({c['used'] / 1000:.1f}K of {c['window'] // 1024}K tokens)")
    return None


def handoff_prompt(task: store.Task, why: str, instructions: str) -> str:
    wt, spec = task.worktree, task.spec
    steps = gitops.git(wt, "log", "--oneline", f"{spec['base']}..HEAD").strip()
    room = min(int(HANDOFF_DIFF_SHARE * (task.status.get("window") or 32768) * 4), HANDOFF_DIFF_MAX)
    # read no further than the room: a 300 MB generated file, not ignored, was read whole into
    # the runner at each handover only to be cut, risking an OOM kill (#474, the audit of 1.8)
    diff = gitops.diff(wt, "HEAD", limit=room)
    work = (f"```diff\n{diff}\n```" if diff is not None else
            "The diff is too long to include; read the changed files:\n"
            f"```\n{gitops.diff(wt, 'HEAD', stat=True)}\n```")
    return (f"You are picking this task up in a fresh session ({why}). Nothing from the earlier "
            f"session carries over except what is in the working tree, so start from what is "
            f"below and re-read the files you need.\n\n## The task\n\n"
            f"{current_task_prompt(task)}\n\n## The work so far\n\n"
            + (f"Earlier steps, committed:\n```\n{steps}\n```\n\n" if steps else "")
            + (f"Uncommitted changes (`git diff HEAD`):\n{work}" if diff != "" else
               "No uncommitted changes yet.")
            + f"\n\n## Now\n\n{instructions}")


def skills_note(task: store.Task, n: int) -> str:
    """What the agent is told about the skills Claude handed it: the whole set at the start of a
    session, then only what changed (handed or taken back) since it was last told."""
    st = task.status
    now, told = st.get("skills") or [], st.get("skills_told")
    have = skills.available()
    lines = []
    if told is None:
        new, gone = now, []
        head = "Claude picked these skills for this task. Load each with the skill tool " \
               "before the part of the work it covers, and follow it:"
    else:
        new, gone = [x for x in now if x not in told], [x for x in told if x not in now]
        head = "Claude handed you more skills; load each before the part of the work it covers:"
    if new:
        lines += [head] + [f"- `{x}`: {have[x]['description']}" for x in new if x in have]
    if gone:
        lines.append("Claude took back: " + ", ".join(f"`{x}`" for x in gone)
                     + ". They no longer apply to this work; don't follow them.")
    task.set_status(skills_told=list(now))
    if not lines:
        return ""
    task.event({"ts": store.now(), "round": n, "kind": "skills", "text": "\n".join(lines)})
    return "\n".join(lines) + "\n\n"


def current_task_prompt(task: store.Task) -> str:
    """The spec the agent is working to now, with the protocol: round 1's, or the latest `wb next`."""
    # instructions.md when the step began in a fresh session: prompt.md is its handoff
    return store.round_prompt(task, task.status.get("step_round") or 1).read_text().strip()


def _start_fresh(task: store.Task, n: int, why: str) -> None:
    path = task.round_dir(n).joinpath("prompt.md")
    raw = path.read_text().strip()
    path.with_name("instructions.md").write_text(raw)  # round_asks reads it back
    # Not this round's prompt alone: what the step's rounds asked, back to the review feedback.
    # An auto-fix or a steer round after a feedback round, starting fresh, once carried only the
    # failing tests, and the fresh agent fixed them and reported done with the feedback never
    # made (the 2026-11-16 audit). A carry-on or a resume carries the asks already.
    if n == (task.status.get("step_round") or 1):
        now = "Start on the task above."
    elif raw.startswith((CARRY_ON[:60], RESUME[:60])):
        now = raw
    else:
        asks = round_asks(task, n)  # ends with this round's own: instructions.md is written
        now = raw if len(asks) < 2 else ASKED.format("\n\n".join(asks[:-1])).strip() + "\n\nNow:\n\n" + raw
    path.write_text(handoff_prompt(task, why, now))
    st = task.status
    task.set_status(session=None, sessions=(st.get("sessions") or []) + [st["session"]] * bool(st.get("session")),
                    context=None, skills_told=None)  # a new session hears the skills again
    task.event({"ts": store.now(), "round": n, "kind": "fresh", "overflow": why != ASKED_FRESH,
                "compacted": why == COMPACTED,
                "text": f"a fresh session for the agent: {why}"})


# The checks Claude's reviews kept making on 2026-11-17: a rule with no test, a test's expected
# value that passed by luck, an unchecked error path, small things (a float function on an int, a
# wrong comment). An error naming the wrong one of two inputs got past it twice (2026-11-18): the
# prompt now asks for that case by name. A rejection test that passed only because its file was
# also short, and an "exact bytes" test that never compared the whole file, cost a review round
# on 2026-11-18: point 1 asks for each. Its rerun's review round was a `static` in a file whose
# CLAUDE.md forbids globals and a proposed CLAUDE.md line describing the wrong flag: point 4 asks
# for the project's rules and for docs that say what the code does. The third run's agent was
# asked point 1 and called its count test fine; it wasn't (a short file, rejected for that): so
# the rejections are now checked by doing it, removing each check and watching a test fail. The
# next task's agent "restored" a check by retyping it and rebuilt the loop wrong, then spent
# its round debugging its own restore (2026-11-18): it copies the file back instead. One
# automatic round, before Claude is woken, costs local time only.
SELF_REVIEW = (
    "Before Claude reviews this, review your own change as a strict senior reviewer would: `git "
    "diff`, and `git status` for new files. Check each point, and fix what fails:\n"
    "1. Every rule in the task (each must, only, at most, rejects, limit and error message) has a "
    "test that fails if that rule alone breaks. A rule without one: write the test. Check each "
    "rejection and limit by doing it, one at a time: copy the file aside (`cp f.c /tmp/f.c`), "
    "comment out its check, run the acceptance command, see a test fail, then copy the file back "
    "(never retype it), and run the command again to see it pass. No test failed: the "
    "test's input breaks another rule too; make it valid in every other way. Each test the task "
    "names checks what it says (exact output: the whole of it, length too, against a literal "
    "written out by hand).\n"
    "2. Every expected value in the tests: work it out by hand from the rule, not from what your "
    "code prints. A test that passes only because of the values it happens to try is a bug.\n"
    "3. Every error path: each write, read, allocation and parse that can fail is checked and "
    "handled as the task says. Each error message is tested once for every input that can cause "
    "it (with two inputs, the first valid and the second bad too), checking the exact text, which "
    "names the input that failed.\n"
    "4. The project's rules (CLAUDE.md, AGENTS.md) hold for every line you changed. Nothing the "
    "task didn't ask for; no dead code, debug output, unused names or wrong comments; each type "
    "used as declared; every doc line you wrote (a proposed CLAUDE.md too) says what the code "
    "does.\n"
    "Then run the acceptance command (`{cmd}`). Do not commit. Finish with a short summary: what "
    "you found and fixed, and for each rule the test that covers it (and, for each rejection, "
    "that removing its check failed it).")


# An agent whose change compared the wrong way round spent 40 minutes sure the compiler was
# broken, compiling test programs in /tmp, until Claude pointed at its loop (2026-11-18). A test
# it wrote this task can be the wrong half too, so it checks that one by hand first.
# Behind a proxy the agent's environment leaves out (#380), a connection failure isn't the
# change's, and "the system works" sent the agent to edit code for it (#496, the audit of 1.90).
def auto_fix_prompt(tests: dict, cmd: str) -> str:
    net = (f" The output shows a failed connection, and your environment leaves out "
           f"{', '.join(tests['proxy'])} (a proxy that needs a password), so it may be the "
           "network's: don't change code for a connection failure; fix only the other "
           "failures." if tests.get("proxy") else "")
    vouch = "" if tests.get("proxy") or tests.get("lane") else \
        " The compiler, libraries and system work; the bug is in this change."
    return (f"The acceptance tests fail.{net} Command: `{cmd}` (exit {tests['exit']}). Output:\n\n"
            f"```\n{tests['tail']}\n```\n\nFix the code so the tests pass. When a failing check is "
            "one you wrote for this task, first work its expected value out by hand from the task: "
            f"if the test was wrong, correct it (that isn't weakening it).{vouch} Do not weaken, "
            "skip or delete tests to make them pass. Do not commit. Finish with a short summary of "
            "what you changed and the test result.")


def not_the_code(task: store.Task, tests) -> str:
    """Why a failed test run says nothing about the change, so no auto-fix round is spent on it
    (#496, the audit of 1.90): a run past --test-timeout went back as "the bug is in this change"
    and the agent edited code that may well have been right, nothing naming the limit; a device
    the lane refused (lane_refused, #331) was named only after both auto-fix rounds. The part
    before the first colon is the cause, for status.last. "" when the failure may be the code's
    (a dropped proxy may be either: its auto-fix prompt says so, auto_fix_prompt)."""
    if not tests or tests["ok"]:
        return ""
    if tests["exit"] == "timeout":
        return (f"the acceptance command ran past --test-timeout "
                f"({int(task.spec.get('test_timeout') or 900)}s): its failure says nothing about "
                "the change; run it in the worktree yourself, or narrow it (wb next --test); "
                "start a slow suite's task with a larger --test-timeout")
    if nodes := tests.get("lane"):
        # the grant that reaches each, as the report names it: the path itself, when it was a
        # file not made yet, sent Claude's `wb allow --path` to make a directory of that name (#670)
        grants = list(dict.fromkeys(lane_grant(n) or n for n in nodes))
        return (f"the lane refused {', '.join(map(shlex.quote, nodes))}: "
                + "; ".join(f"wb allow {task.name} --path {shlex.quote(g)}" for g in grants)
                + ", then wb feedback, if the tests need it")
    return ""


def measure_speed(task: store.Task, since: float) -> None:
    """Record how fast the model read and wrote in this round's requests (router.budgets), and,
    for the Stats tab, the task's own samples with its time to first token (status `speeds`,
    the newest TASK_SPEED_KEEP of each: the results row keeps their medians, #404)."""
    from . import router
    from .transcript import export
    sid = task.status.get("session")
    if not sid:
        return
    try:
        msgs = [m for m in export(sid).get("messages", [])
                if ((m.get("time") or {}).get("created") or 0) >= since * 1000]
        got = router.record_speed(task.spec["model"], msgs)
        if any(got.values()) and stats.recording():
            had = task.status.get("speeds") or {}
            task.set_status(speeds={k: ((had.get(k) or []) + v)[-TASK_SPEED_KEEP:]
                                    for k, v in got.items()})
    except Exception:  # a measurement, never a reason to stop the task
        pass


TASK_SPEED_KEEP = 200  # a task's own speed samples kept, per measure


# A round that ended before its server answered still says how fast the model read, at most: the
# prompt it was sent over the time it waited. On the machines the budgets stretch for, the first
# request is the one that never finished, the export had no firstToken, and speed.json stayed
# empty: the next wb start got the same 900 s and hit the same wall (#491, the audit of 1.90).
# The prompt's tokens are guessed low (its text and instructions at 4 characters a token, and
# OpenCode's own system prompt and tools), which guesses the speed low and the budgets long.
BOUND_MIN_S = 120
OPENCODE_PROMPT_TOKENS = 3000


def read_bound(task: store.Task, n: int, rnd: dict) -> int | None:
    """Record round ``n``'s read speed bound (tokens/s) when it ended before the model's first
    output, after BOUND_MIN_S or more, by the silence or round limit or OpenCode giving up; and
    return it; else None. Not after a nudge or Claude's interruption: a model thinking silently
    behind a server that sent its headers at once would read as a slow reader."""
    from . import router
    waited = float(rnd.get("waited") or 0)
    if rnd.get("answered") or rnd.get("interrupted") or waited < BOUND_MIN_S \
            or rnd.get("guard") not in (None, "silence", "round"):
        return None
    try:
        layer = json.loads((context.rules_dir(task) / "opencode.json").read_text())
        files = [task.round_dir(n) / "prompt.md", task.round_dir(n) / "instructions.md",
                 *map(Path, layer.get("instructions") or [])]
        chars = sum(f.stat().st_size for f in files if f.is_file())
        tok_s = max(1, int((chars / 4 + OPENCODE_PROMPT_TOKENS) / waited))
        router.record_read_bound(task.spec["model"], tok_s)
    except (OSError, ValueError, KeyError):
        return None
    return tok_s


def died(rnd: dict) -> bool:
    """Whether OpenCode itself ended the round with a non-zero exit (a crash, an out-of-memory
    kill, a provider error), not the runner's stop of a shutdown that hung (#83). Such a round
    failed whatever it printed: one that said "I will inspect the files" and exited 7 went on to
    the tests, and to review as "tests passed" (#427, the audit of 1.8)."""
    return rnd.get("exit") not in (0, None) and not rnd.get("lingered")


def _round_end(task: store.Task, n: int, rnd: dict, began: float) -> None:
    """How the round ended, for the Stats tab (#404): the guard that stopped it is counted in
    status `guards`, and a `round_end` event says how long it ran and how it ended. Only with
    stats recording on; no reader of events shows the kind, so the transcript is unchanged."""
    if not stats.recording():
        return
    how = ("stuck" if rnd.get("stuck") else "handover" if rnd.get("full") else
           "interrupted" if rnd.get("interrupted") else
           "failed" if died(rnd) else "done")
    if how == "stuck" and rnd.get("guard"):
        guards = dict(task.status.get("guards") or {})
        guards[rnd["guard"]] = guards.get(rnd["guard"], 0) + 1
        task.set_status(guards=guards)
    task.event({"ts": store.now(), "round": n, "kind": "round_end", "secs": round(time.time() - began, 1),
                "how": how, **({"guard": rnd["guard"]} if how == "stuck" and rnd.get("guard") else {})})


def _load_failed(task: store.Task, e: Exception) -> None:
    if isinstance(e, store.Damaged):  # no model to name: task.json lost its contents (#475)
        task.set_status(phase="failed", last=str(e))
        return task.notify("failed", str(e))
    from .results import ran  # results imports this module
    never = not ran(task)
    task.set_status(phase="failed", last=f"model: {e}", infra="model",
                    **({"failed_before": "load"} if never else {}))
    # the agent never ran: resuming loses nothing, and a discard records nothing of the model
    task.notify("failed", f"task {task.name}: could not load {task.spec['model']}: {e}"
                + (f"\nthe agent never ran: once the model can, wb resume {task.name} starts it"
                   if never else ""))


# A model whose server is only down (models.may_come_up: Ollama not answering, a server with no
# start command not running) is waited for, looking again after 2 s, 4 s, ... up to a minute
# apart, for as long as a server's first start may take. Failing at once, a task wb start --force
# queued "to wait for it" failed on its first check, and an Ollama restart or an out-of-memory
# kill between rounds failed the task for good (the 2026-11-19 audit). A configuration error
# (not pulled, a refused key) still fails at once. wb stop ends the runner, and the wait with it.
DOWN_WAIT = models.START_TIMEOUT
DOWN_PAUSES = (2, 60)


def _down(m: models.Model) -> bool:
    try:
        return models.may_come_up(models.status(m))
    except Exception:  # the config can't be read: not a server that is down
        return False


def _stopped(m: models.Model) -> bool:
    """Whether the model's server is one wb starts itself (models.json `start`) and it is not
    running now: one that died under a round (an out-of-memory kill, a CUDA or GGML assert) is
    started again by the loop's load. Only Ollama's and servers wb can't start were revived, and
    the same crash under llama-server failed the task (#579, the audit of 1.90)."""
    if not m.start:
        return False
    try:
        st = models.status(m)
    except Exception:  # the config can't be read: nothing to start
        return False
    return bool(st["available"]) and not st["resident"]


def _wait_up(task: store.Task, m: models.Model) -> None:
    """Wait, backing off, until the model's server is no longer only down; RuntimeError past
    DOWN_WAIT. Whatever it is then, the caller's load says."""
    deadline, pause = time.monotonic() + DOWN_WAIT, DOWN_PAUSES[0]
    said = False
    while (st := models.status(m)) and models.may_come_up(st):
        left = deadline - time.monotonic()
        if left <= 0:
            raise RuntimeError(f"{m.id} can't run: {st['why']}, for {DOWN_WAIT // 60} min"
                               + (f": {fix}" if (fix := models.remedy(m, st)) else ""))
        msg = (f"waiting for {m.id}: {st['why']} (up to {left / 60:.0f} min more; wb stop gives "
               "up)")
        task.set_status(last=msg)
        if not said:
            task.event({"ts": store.now(), "round": task.status.get("round", 1), "kind": "load",
                        "text": msg})
            said = True
        time.sleep(min(pause, left))
        pause = min(pause * 2, DOWN_PAUSES[1])


def _load(task: store.Task, lease, m: models.Model) -> float:
    """``lease.load()``, and once more after waiting when it failed because the server is only
    down (an Ollama restarting between rounds). Loaded, the window it serves is checked against
    the one OpenCode is told: only doctor, scout and wb model add compared them, and a server
    restarted without its setting trimmed every prompt silently (#248, the audit of 1.2)."""
    try:
        secs = lease.load()
    except (KeyError, RuntimeError, OSError):
        if not _down(m):
            raise
        lease.step_aside()  # a server that is down holds no GPU from other tasks (#257)
        _wait_up(task, m)
        _rejoin(task, lease)
        secs = lease.load()
    if why := models.short_window(m):
        raise RuntimeError(why)
    return secs


def _loaded(task: store.Task, secs: float) -> None:
    """A load's seconds: added to the task's total (status `load_secs`, after what earlier runners
    loaded: set, a `wb feedback` runner finding the model resident wrote 0 over a 40 s cold load,
    #567), and a load that happened kept alone in status `loads`, the typical load's sample: the
    median of totals made four reloads of a 40 s load read "loads ~2m40s" (#577)."""
    st, secs = task.status, round(secs, 1)
    task.set_status(load_secs=round((st.get("load_secs") or 0) + secs, 1),
                    **({"loads": [*(st.get("loads") or []), secs]} if secs > 0 else {}))


def _rejoin(task: store.Task, lease) -> None:
    """Back on the GPU before a round, after the tests or a server's wait let it go (#257):
    queued, in its first-come place, when the GPU is someone else's meanwhile."""
    if lease.aside:
        task.set_status(phase="queued", last="waiting for the GPU")
        lease.rejoin()


def drive(task: store.Task) -> None:
    # infra: why the machine, not the agent, failed the task's last run (wb bench grades none of it)
    task.set_status(phase="queued", last="waiting for the GPU", pid=os.getpid(), infra=None,
                    boot=store.boot_id())  # a later boot reaps it as a restart's (cli.reap)
    say = lambda msg: task.set_status(last=msg)  # noqa: E731
    try:
        m = models.resolve(task.spec["model"])
    except KeyError as e:
        return _load_failed(task, e)
    CURRENT["remote"] = m.remote  # the GPU is not ours to measure when the model is remote
    if m.remote:
        task.set_status(remote_gpu=True)  # so its savings say the energy wasn't measured (#117)
    if not checkout(task):  # the worktree itself, out of wb start's foreground (#529)
        return
    if not _prepare(task):  # the project as the user has it, before the base check (#201)
        return
    if _down(m):  # before the lease: a server that is down holds no GPU from other tasks
        try:
            _wait_up(task, m)
        except RuntimeError as e:
            return _load_failed(task, e)
    if needs_base_check(task):
        _base_check(task)  # on the untouched base, before the agent and the GPU
    # beside the running tasks' models when all fit the GPU, else alone once they finish
    with models.GpuLease(m, task.name, say) as lease:
        try:
            _loaded(task, _load(task, lease, m))
            window = (m.config.get("limit") or {}).get("context")
            task.set_status(window=window)
            own = task.spec.get("self_review")  # --self-review / --no-self-review, else learned
            tuning.tune(task, task.spec["model"], window, {"fresh_at": fresh_ceiling(m.config)},
                        {} if own is None else {"self_review": int(own)})
        except (KeyError, RuntimeError, OSError) as e:
            return _load_failed(task, e)
        handed = 0  # mid-round handovers in this runner: one review round (HANDOVER_MAX)
        first = True
        revived = None  # the round run again after its server went away under it
        while True:
            if _updated(task):  # before the round: its lane, shim and ask server would be new
                return
            if not first:
                # Again before every round: each request resets Ollama's keep-alive to its own
                # default (5 minutes), so after a longer test run the model was unloaded and the
                # auto-fix round's think budget paid for loading it (the 2026-11-16 audit).
                # Resident already, this is one look at the server.
                try:
                    _rejoin(task, lease)
                    if (again := _load(task, lease, m)) > 0:
                        _loaded(task, again)
                        task.event({"ts": store.now(), "round": task.status["round"], "kind": "load",
                                    "text": f"model reloaded ({again:.0f}s)"})
                except (KeyError, RuntimeError, OSError) as e:
                    return _load_failed(task, e)
            first = False
            n = task.status["round"]
            _early_notes(task, n)
            why = fresh_reason(task) if n > 1 else None
            keep = task.status.get("context") == "keep"
            if why:
                _start_fresh(task, n, why)
            elif keep:
                task.set_status(context=None)  # a one-round instruction, held by run_round
            task.set_status(phase="running", round=n, round_started_at=store.now(),
                            last="agent started")  # not "starting…": the pane shows its progress
            task.event({"ts": store.now(), "round": n, "kind": "round", "text": "started"})
            began = time.time()
            try:
                rnd = run_round(task, n, keep=keep)
            finally:  # before the lease is released: the GPU's next task must not meet our agent
                _end_agent(task)
            measure_speed(task, began)
            if (bound := read_bound(task, n, rnd)) and rnd["guard"] == "silence":
                from . import router
                rnd["stuck"] += (f" (it read at most ~{bound} tokens/s here: a new task on this "
                                 f"model now waits {router.budgets(m)['silence'] // 60} minutes)")
            if rnd["full"]:  # before notes: the handover carries them (_full_round)
                handed += 1
                if handed <= HANDOVER_MAX:
                    _round_end(task, n, rnd, began)
                    _full_round(task, n, rnd, handed)
                    continue
                rnd["guard"] = "handovers"
                rnd["stuck"] = (f"handed over to a fresh session {HANDOVER_MAX} times in this "
                                "round without finishing: the step is too big for this model's "
                                "window; split it (wb next) or take it over")
            _round_end(task, n, rnd, began)
            if rnd["interrupted"] or (pending_steer(task) and not rnd["stuck"]):
                _steer_round(task, n, rnd["interrupted"])
                continue
            if not rnd["stuck"] and rnd["exit"] not in (0, None) and not rnd["texts"] \
                    and revived != n and ((down := _down(m)) or _stopped(m)):
                # OpenCode died without a word while the model's server is down (restarted, or
                # killed out of memory): the round never reached the model. Once it is back, the
                # loop's load reloads it and the round runs again, once (the 2026-11-19 audit).
                # A server wb starts itself isn't waited for: the loop's load starts it (#579).
                revived = n
                task.event({"ts": store.now(), "round": n, "kind": "load",
                            "text": f"opencode exited {rnd['exit']} with the model's server down: "
                                    + ("the round runs again once it is back" if down else
                                       "wb starts it again and the round runs again")})
                lease.step_aside()  # #257: nothing of ours is on the GPU while it is down
                if down:
                    try:
                        _wait_up(task, m)
                    except RuntimeError as e:
                        return _load_failed(task, e)
                continue
            if rnd["stuck"] or died(rnd):
                text = summary_text(task, n, rnd, None) + _undelivered(task)
                task.round_dir(n).joinpath("summary.md").write_text(text)
                phase = "stuck" if rnd["stuck"] else "failed"
                task.set_status(phase=phase, last=rnd["stuck"] or f"opencode exited {rnd['exit']}")
                # what stopped it, for results.step: a guard or the agent's own exit (#582)
                task.notify(phase, text, round=n, cause="guard" if rnd["stuck"] else "agent")
                return
            # the tests need no model: the GPU is the queue's, or theirs (--unload-for-tests), #257
            lease.step_aside(unload=bool(task.spec.get("unload_for_tests")))
            tests = run_tests(task, n)
            if pending_steer(task):  # a note that came in while the tests ran
                _steer_round(task, n, False)
                continue
            text = summary_text(task, n, rnd, tests)
            if skip := not_the_code(task, tests):  # first: what Claude must do about it
                text = f"auto-fix skipped: {skip}\n" + text
            if pending_steer(task):  # a note that came in while the report was written
                _steer_round(task, n, False)
                continue
            task.round_dir(n).joinpath("summary.md").write_text(text)
            used = task.status.get("auto_fixes", 0)
            if tests and not tests["ok"] and not skip and used < int(task.spec.get("auto_fix", 0)):
                task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
                task.round_dir(n + 1).joinpath("prompt.md").write_text(
                    auto_fix_prompt(tests, task.spec["test_cmd"]))
                task.set_status(round=n + 1, auto_fixes=used + 1, tests=tests)
                task.event({"ts": store.now(), "round": n, "kind": "autofix",
                            "text": "tests failed; sent the output back to the agent"})
                continue
            mark = [task.status.get("steps", 1), task.status.get("reviews", 0)]
            if tuning.value(task, "self_review") and tests and tests["ok"] \
                    and task.status.get("self_reviewed") != mark:
                task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
                task.round_dir(n + 1).joinpath("prompt.md").write_text(
                    SELF_REVIEW.format(cmd=task.spec["test_cmd"]))
                task.set_status(round=n + 1, self_reviewed=mark, tests=tests)
                task.event({"ts": store.now(), "round": n, "kind": "self_review",
                            "text": "tests pass; the agent reviews its own change first"})
                continue
            task.set_status(phase="review", tests=tests,
                            last="tests passed: Claude reviews next" if tests and tests["ok"]
                            else f"{skip.split(':')[0]}: Claude reviews next" if skip
                            else "Claude reviews next")
            task.notify("review", text + review_diff(task), round=n)
            return


# What the fresh session is told after the runner took over from a full one.
CARRY_ON = ("Your last session filled up, so you are continuing in a fresh one. Carry on with "
            "the task above from where the work stands: re-read the files you are changing, then "
            "finish them and run the tests.{asked} Your last words before the switch:\n\n{last}")
ASKED = "\n\nWhat you were asked to do, and it isn't done until it is:\n\n{}\n\n"
# What the fresh session is told when Claude resumes a task it stopped (wb resume).
RESUME = ("Claude stopped your last session and is resuming the task in a fresh one. Carry on "
          "with the task above from where the work stands: re-read the files you are changing, "
          "then finish them and run the tests.{asked}")


# A round whose prompt is the runner's own think nudge (_steer_round): round_asks skips it, or a
# later fresh session was told, under "What you were asked to do", that Claude interrupted it
# after 12 minutes of thinking it never did (#681).
NUDGED = "nudged"


# Claude's notes folded into a round's prompt after its opening (a carry-on, a resume, the
# notes that came before the round began): round_asks skips a carry-on or resume whole, so it
# reads them from here (#677).
FOLDED = "notes.md"


def folded_notes(task: store.Task, n: int, notes: list[dict]) -> str:
    """The notes as round n's prompt carries them, kept beside it for round_asks."""
    text = steer_prompt(notes, False)
    path = task.round_dir(n) / FOLDED
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((path.read_text().rstrip() + "\n\n" if path.is_file() else "") + text + "\n")
    return text


def round_asks(task: store.Task, n: int) -> list[str]:
    """The instructions rounds n, n-1, ... gave since the step's task, back to the last review
    feedback: what a fresh session taking over mid-round must still carry out. A handover's own
    carry-on adds nothing (it is skipped). Seen 2026-11-16: two handovers in a feedback round
    each said only "carry on with the task", and the fresh sessions, finding the tests green,
    reported done with the feedback never made. A resume (wb resume) is skipped the same way."""
    first = task.status.get("step_round") or 1
    out: list[str] = []
    for r in range(n, first, -1):
        f = store.round_prompt(task, r)
        raw = f.read_text().strip() if f.is_file() else ""
        if (not raw or raw.startswith((CARRY_ON[:60], RESUME[:60]))
                or (task.round_dir(r) / NUDGED).exists()):
            # but the notes folded into it (folded_notes): skipped with it, a note sent as a
            # session filled reached that successor alone, and the next was never told (#677)
            if (notes := task.round_dir(r) / FOLDED).is_file() and (kept := notes.read_text().strip()):
                out.insert(0, kept)
            continue
        out.insert(0, raw)
        if raw.startswith(lessons.FEEDBACK_HEAD):
            break
    return out


# A handover needs a successor with room to work: at least this share of the window between
# the reading it repeats (the floor) and the handover point. Less, and the session carries on.
HANDOVER_ROOM = 0.25


def _reading_full(task: store.Task, n: int, used: int, window: int,
                  floor: int | None = None) -> None:
    pct = 100 * used // window
    if floor is None:
        what = "before it changed a file: the reading this step needs fills the handover point"
    else:
        what = (f"and had read {100 * floor // window}% before its first change: a fresh session "
                f"re-reading that would have little room to work")
    text = (f"the agent's session reached {pct}% of its window {what}, so it carries on in this "
            f"session (OpenCode compacts it if it must). Name fewer files to read, or put what "
            f"it needs in the spec.")
    # Not a wake: the advice is for the next spec, which Claude reads in the round's report.
    # As a check-in it woke Claude mid-round with nothing to do, every round of a step, and then
    # once even on a trivial task (2026-11-17)
    task.event({"ts": store.now(), "round": n, "kind": "reading_full", "text": text})


# Mid-round handovers in one review round before it is stopped as stuck. A session that changes
# something early and then fills up reading handed over again and again, for hours, with Claude
# told nothing (the 2026-11-16 audit). A big step needs a few; past this it is too big for the
# window. The one before the last is a check-in, so Claude can steer or stop it; each before it
# woke Claude to read "on course" and wait again (2026-11-17), and the last is a stuck report.
HANDOVER_MAX = 5


def _full_round(task: store.Task, n: int, rnd: dict, handed: int = 1) -> None:
    window = task.status["window"]
    pct = 100 * rnd["full"] // window
    text = (f"the agent's session reached {pct}% of its window "
            f"({rnd['full'] / 1000:.1f}K of {window // 1024}K): a fresh one takes over")
    task.event({"ts": store.now(), "round": n, "kind": "full", "text": text})
    if handed == HANDOVER_MAX - 1:
        files = gitops.changed(task.worktree)
        task.notify("checkin", f"task {task.name}: {text} (handover {handed} this round; "
                    f"stopped as stuck past {HANDOVER_MAX}); files changed so far: "
                    + gitops.brief_paths(files), round=n)
    last = "\n\n".join(t.strip() for t in rnd["texts"][-2:] if t.strip())[-1200:]
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    asks = round_asks(task, n)
    notes = pending_steer(task)  # Claude's notes that came in as it filled: the successor's too
    task.round_dir(n + 1).joinpath("prompt.md").write_text(CARRY_ON.format(
        asked=ASKED.format("\n\n".join(asks)) if asks else "",
        last="> " + last.replace("\n", "\n> ") if last else "(none)")
        + (f"\n\n{folded_notes(task, n + 1, notes)}" if notes else ""))
    task.set_status(round=n + 1, context="full", full_pct=pct,
                    handovers=task.status.get("handovers", 0) + 1,
                    steer_taken=task.status.get("steer_taken", 0) + len(notes))


def _undelivered(task: store.Task) -> str:
    """Claude's notes a stopped round never delivered, for its report (the runner's own nudges
    aside): wb feedback and wb next drop them, wb resume carries them (cli._stale_notes)."""
    notes = [r for r in pending_steer(task) if r.get("by") != "workbench"]
    return ("\nNOTE: not delivered, the round stopped first: "
            + " | ".join(r["text"].strip().replace("\n", " ")[:200] for r in notes)) if notes else ""


def _early_notes(task: store.Task, n: int) -> None:
    """Notes that came in before the round began (queued for the GPU, its model or the base
    check, or while the model reloaded) go with its prompt, as `wb steer` promises: they once
    waited for the round to end and came as a round of their own (#452)."""
    if not (notes := pending_steer(task)):
        return
    path = task.round_dir(n).joinpath("prompt.md")
    path.write_text(path.read_text().rstrip() + "\n\n" + folded_notes(task, n, notes))
    task.set_status(steer_taken=task.status.get("steer_taken", 0) + len(notes))


def _steer_round(task: store.Task, n: int, interrupted: bool) -> None:
    notes = pending_steer(task)
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    task.round_dir(n + 1).joinpath("prompt.md").write_text(steer_prompt(notes, interrupted))
    if any(r.get("by") == "workbench" for r in notes):  # the nudge isn't an ask (#681) ...
        task.round_dir(n + 1).joinpath(NUDGED).touch()
        if theirs := [r for r in notes if r.get("by") != "workbench"]:  # ... Claude's notes are
            folded_notes(task, n + 1, theirs)
    task.set_status(round=n + 1, steer_taken=task.status.get("steer_taken", 0) + len(notes))


# What a task's children load from the clone afresh each round, beside this runner's code loaded
# at its start: the lane (run as a file), the git shim (written by this runner, importing
# gitguard) and the ask server (ask_mcp, store, watch). A `git pull` or Claude Code's plugin
# update mid-task gave them new code under the old runner, and a changed interface between them
# ended the task stuck or failed with the cause only in runner.log (#603, the audit of v1.90).
# So the runner prints them at its start and, before each round, ends cleanly when they changed.
CHILD_CODE = ("bin/wb-ask-mcp", "workbench/__init__.py", "workbench/ask_mcp.py",
              "workbench/gitguard.py", "workbench/lane.py", "workbench/store.py", "workbench/watch.py")
UPDATED = "workbench was updated under this task"


def code_print() -> str:
    h = hashlib.sha256()
    for rel in CHILD_CODE:
        try:
            h.update((PKG_ROOT / rel).read_bytes())
        except OSError:
            h.update(b"-")
        h.update(b"\0")
    return h.hexdigest()


def _updated(task: store.Task) -> bool:
    """Whether the children's code changed since this runner started (CURRENT["code"]); if so,
    the task is left stuck for wb resume, which starts a runner on the new code."""
    if not (was := CURRENT.get("code")) or code_print() == was:
        return False
    msg = f"{UPDATED}: wb resume {task.name} continues it on the new code"
    task.set_status(phase="stuck", last=msg)
    task.notify("stuck", f"task {task.name}: {msg}", round=task.status.get("round"))
    return True


# A SIGTERM that didn't come from wb (a shutdown, a logout, a kill): wb marks its own stops
# (status `stopping`, cli._stop_runner); any other leaves the task stuck, with a notice, so it
# doesn't sit in a live phase that nothing will ever end (the 2026-11-16 audit).
SIGNAL_STOP = "the runner was stopped by a signal (a shutdown, a logout or a kill), not by Claude"
# A shutdown or logout signals every runner at once: each raised a stuck notice of its own, and
# the mod woke Claude once a task (#544). A runner stopped by a signal waits, up to this long
# after its own signal, while another task of its session still runs, so the others stop too
# (past _stop's 10 s wait on an agent); then one notice names every task of the session the
# signal stopped then (within the same span), raised on the first started, the rest acknowledged.
SIGNAL_SETTLE = 12


def _signal_notice(task: store.Task, at: float) -> None:
    owner = store.owner(task)
    ours = lambda: [t for t in store.all_tasks() if store.owner(t) == owner]  # noqa: E731
    deadline = at + SIGNAL_SETTLE
    while time.time() < deadline and any(
            t.name != task.name and t.status.get("phase") in store.ACTIVE
            and not t.status.get("stopping") and t.status.get("pid")
            and models.runs(t.status["pid"], "workbench.runner", t.name) for t in ours()):
        time.sleep(0.25)
    with store.locked(store.home() / "locks" / "signal-stop.lock"):  # one writer per batch
        named = {n for r in store.notices() if r["kind"] == "stuck" and r["ts"] >= at - SIGNAL_SETTLE
                 for n in (r.get("tasks") if isinstance(r.get("tasks"), list) else ())}
        if task.name in named:
            return  # another runner's notice named this one
        batch = [t for t in ours() if t.name not in named and t.status.get("phase") == "stuck"
                 and t.status.get("last") == SIGNAL_STOP
                 and abs((t.status.get("signalled") or 0) - at) <= SIGNAL_SETTLE]
        if task.name not in (t.name for t in batch):
            batch.append(task)  # resumed meanwhile, or no owner known: its own notice still
        names, where = [t.name for t in batch], owner[1]
        ts = store.now()
        # the steps `wb next` committed, which a discard keeps (#494), for each task they are in
        kept = [line for t in batch if (line := steps_line(t))]
        batch[0].notify("stuck", (
            f"task {task.name}: {SIGNAL_STOP}. wb resume {task.name} continues it in a fresh "
            f"session, wb discard {task.name} drops it" if len(batch) == 1 else
            f"{len(batch)} tasks' runners were stopped by a signal (a shutdown, a logout or a "
            f"kill), not by Claude: {', '.join(names)}. wb resume --all"
            + (f" --repo {shlex.quote(where)}" if where else "")
            + " continues them in the order they were started")
            + "".join(f"\n{k}" for k in kept),
            round=batch[0].status.get("round"), ts=ts, tasks=names,
            cause="signal")  # not the step's size (#582)
        for t in batch[1:]:
            store.write_ack(t, ts)  # had with the first's notice: neither wb wait nor the mod wakes for it


def lock_file(name: str, kind: str):
    """Task `name`'s lock of `kind`, in $WB_HOME/locks/, which no lane reads (store.task_lock)."""
    return store.task_lock(name, kind)


def _one_runner(task: store.Task):
    """Hold the task's runner lock (lock_file) for the runner's life, or None when another
    runner holds it: two wb commands acting on one task at once once spawned two runners on one
    worktree and session (the 2026-11-16 audit). The holder's pid is in the file."""
    import fcntl
    path = lock_file(task.name, "runner")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        held = fh.read().strip()
        fh.close()
        if held.isdigit():  # spawn_runner wrote this one's pid: put the live runner's back
            task.set_status_if(lambda st: st.get("pid") == os.getpid(), pid=int(held))
        return None
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main():
    store.absolute_env()
    task = store.Task(sys.argv[1])
    lock = _one_runner(task)  # noqa: F841 (held until the process exits)
    if lock is None:
        print(f"another runner drives {task.name}; this one exits", flush=True)
        return
    signal.signal(signal.SIGTERM, _on_term)
    CURRENT["code"] = code_print()  # what the children load; compared before each round (#603)
    from . import router, transcript, verify  # noqa: F401 (now, not mid-task from an updated clone)
    try:
        drive(task)
    except Stopped:
        st = task.status
        if st.get("stopping") or st.get("phase") not in store.ACTIVE:
            task.event({"ts": store.now(), "round": st.get("round"), "kind": "stopped",
                        "by": "reviewer", "text": "stopped by the reviewer"})
        else:
            task.event({"ts": store.now(), "round": st.get("round"), "kind": "stopped",
                        "by": "signal", "text": SIGNAL_STOP})
            at = SIGNALLED[0] if SIGNALLED else time.time()
            task.set_status(phase="stuck", last=SIGNAL_STOP, signalled=at)
            _signal_notice(task, at)
    except Exception as e:
        err = traceback.format_exc()
        full = store.disk_full(e)
        try:
            with (task.dir / "runner.log").open("a") as log:
                log.write(err)
        except OSError:
            pass
        try:
            task.set_status(phase="failed", last=f"the disk is full ({e.strerror}): free some, "
                            f"then wb resume {task.name}" if full else err.strip().splitlines()[-1][:200],
                            infra="disk full" if full else "crash")
            task.notify("failed", f"task {task.name}: the disk is full ({store.home()}); free "
                        f"some, then wb resume {task.name}" if full else
                        f"task {task.name}: the runner crashed\n{err[-1500:]}")
        except OSError as again:
            # no room even for this: wb's reap fails the task, naming the disk, once it can
            # write (#342, the audit of 1.4)
            if not store.disk_full(again):
                raise
    finally:
        try:
            _end_agent(task)  # a backstop: drive's own cleanup may not have been reached
        finally:
            try:
                task.set_status(pid=None, stopping=None)
            except OSError as e:
                if not store.disk_full(e):
                    raise


if __name__ == "__main__":
    main()
