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
import tempfile
import threading
import time
import traceback
import uuid
import zlib
from collections import deque
from pathlib import Path

from . import (ask_mcp, context, gitguard, gitops, hardware, keyhole, lane, lessons, models, progress,
               skills, stats, store, tuning)
from . import meter
from . import privacy
from .events import EDIT_TOOLS, StuckDetector, compaction_failed, normalize, written
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


def opencode_moved(task: store.Task) -> bool:
    """Whether OpenCode changed under the task since the last wb command started its runner
    (status `opencode`, set by wb start, feedback, next and resume), or is now before 2.0; if so,
    the task is left stuck for wb resume, cause `opencode`: not the step's. Only wb start
    looked: OpenCode reinstalled from npm (1.x) mid-task failed the next round as "opencode
    exited 1", charged to the model's slice, and every resume repeated it (#866). Not installed
    or saying no version: the lane's own error says so."""
    if not (have := opencode_version()):
        return False
    now, was = f"{have[0]}.{have[1]}", task.status.get("opencode")
    if have >= (2, 0) and (not was or was == now):
        return False
    msg = (f"OpenCode {now} can't run tasks: workbench needs OpenCode 2.x: {OPENCODE_GET}; then wb "
           f"resume {task.name}" if have < (2, 0) else
           f"{OPENCODE_MOVED} ({was}, now {now}): wb resume {task.name} continues it on {now}"
           + (f". {drift}" if (drift := untested(have)) else ""))
    # status `infra` and the notice's `tasks`, as for an interruption: without them wb feedback
    # (the wake advised it, "it got stuck") counted a review round, and one real review after it
    # made the step go badly (#1176, 2026-11-24)
    task.set_status(phase="stuck", last=msg[:200], infra="opencode")
    task.notify("stuck", f"task {task.name}: {msg}", round=task.status.get("round"), cause="opencode",
                tasks=[task.name])
    return True


OPENCODE_MOVED = "OpenCode was changed under this task"


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


def handover_warning(cfg: dict, api: bool = False) -> str:
    """What to say of an entry whose output cap puts OpenCode's compaction too close to the
    earliest handover point (fresh_ceiling under every fresh_at value), or "". Nothing said so,
    and every long step compacted, the small-model summary the handover exists to avoid (#595,
    the audit of 1.90). At HANDOVER_ROOM or under, no successor has room, so no handover fits.
    An API model's (``api``): models.api_limits already bounds its output, so only a small
    catalog `limit.input` is left to cause it, which neither wb nor the user can lower: lowering
    the output was advice that changed nothing (#1073, 2026-11-23)."""
    top, at = fresh_ceiling(cfg), compaction_point(cfg)
    lo = min(tuning.KNOBS["fresh_at"].values)
    if top is None or top >= lo:
        return ""
    lim, opts = cfg.get("limit") or {}, cfg.get("options") or {}
    said = (("limit.input", lim.get("input")), ("limit.output", lim.get("output"))) if api else \
        (("limit.output", lim.get("output")), ("options.max_tokens", opts.get("max_tokens")))
    caps = ", ".join(f"{k} {v}" for k, v in said if v)
    return (f"{'its catalog limits' if api else 'its output cap'} ({caps} of a {lim['context'] // 1024}K "
            f"window) put{'' if api else 's'} OpenCode's "
            f"compaction at {max(at, 0):.0%} of the window, under a step's growth past the earliest "
            f"handover point ({lo:.0%}): " + ("no handover fits, so long steps compact" if top <= HANDOVER_ROOM else
                               f"handovers come at {top:.0%}, with little room")
            + ("; a catalog's limits aren't wb's or yours to change: a model with a larger input limit "
               "has room" if api else "; lower limit.output and options.max_tokens"))


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
# An API provider reads no long prompt here, and lifting the wait took away OpenCode's only
# guard on a request its provider never starts (DeepSeek closes one not started within 10
# minutes): its header wait is API_HEADER_S instead of none (#1017).
API_HEADER_S = 600


def provider_timeouts(task: store.Task) -> dict:
    """{"provider": {<the task's provider>: {"options": {headerTimeout, chunkTimeout}}}}."""
    pid = str(task.spec.get("model") or "").split("/", 1)[0]
    if not pid:
        return {}
    chunk = max(900, int(float(limit(task, "silence")) + 60)) * 1000
    header = API_HEADER_S * 1000 if api_entry(task) else False
    return {"provider": {pid: {"options": {"headerTimeout": header, "chunkTimeout": chunk}}}}


# A task's limits were fixed in task.json at wb start, so a task on a model not yet measured kept
# the defaults in every later round, after its first round had measured the model slow and said a
# new task would wait longer: a fresh session read its prompt again at the same speed and hit the
# same 900 s wall, and only a new task got the time (#814). Before each round the runner takes,
# for each limit no flag gave (task.json `timeouts_given`), the larger of task.json's value and the
# model's budget now, into status `limits`; a flag's value is the user's word, and a task from
# before the key keeps its own.
LIMITS = {"think": "think_timeout", "silence": "silence_timeout", "round": "round_timeout"}
LIMIT_DEFAULTS = {"think": 0, "silence": 900, "round": 2400}  # a task.json missing the key


def limit(task: store.Task, which: str) -> float:
    """The round's limit ``which`` (think, silence, round) in seconds: status `limits`, stretched
    for a model measured slow since wb start (stretch_limits), else task.json's."""
    if (now := (task.status.get("limits") or {}).get(which)) is not None:
        return float(now)
    return float(task.spec.get(LIMITS[which]) or LIMIT_DEFAULTS[which])


def stretch_limits(task: store.Task, m) -> dict:
    """Status `limits`: each limit no flag gave, raised to the model's budget now
    (router.budgets) where that is longer than task.json's; returns what was raised."""
    from . import router
    spec = task.spec
    given = spec.get("timeouts_given")
    if given is None:  # a task from before #814: whether a flag gave them is unknown
        return {}
    fresh = router.budgets(m, spec.get("weights_id"))  # no server read for it (#905)
    raised = {k: fresh[k] for k, key in LIMITS.items()
              if k not in given and fresh[k] > float(spec.get(key) or 0)}
    task.set_status(limits=raised or None)
    return raised


def write_config_layer(task: store.Task) -> Path:
    """The per-task OpenCode config: the providers OpenCode wouldn't load by itself
    (models.agent_providers), the task's provider's timeouts (provider_timeouts), the agent's
    context and its rules (the user's own OPENCODE_CONFIG_CONTENT permissions, USERS_RULES,
    before them: rules_env), and the ask_reviewer server."""
    path = context.rules_dir(task) / "opencode.json"
    rules = context.rules_dir(task)
    if (rules / "CLAUDE.md").is_file() or (rules / "user-rules").is_dir():
        # an earlier wb's copy of Claude's global files, which context.layer now deletes (#1061):
        # this round's report says so, as nothing told an updating user or a task already open
        # that its agent had lost them (#1119, 2026-11-23)
        task.set_status(claude_rules_gone=task.status.get("round", 1))
    mine = context.layer(task)
    users = models.users_config_content()
    theirs = users.get("permissions")
    mine["permissions"] = [*(theirs if isinstance(theirs, list) else []), *mine["permissions"]]
    # and their 1.x `permission` object, which OpenCode 2.3.2 still reads, as rules ahead of the
    # same document's `permissions`: left in the content, loaded after the layer, it came after
    # the rules, and `{"*": "allow"}` there lifted every one (#994's review, 2026-11-22)
    if "permission" in users:
        mine = {"permission": users["permission"], **mine}
    wait_ms = int(task.spec.get("ask_timeout", 1200) + 60) * 1000
    try:
        cfg = api_entry(task) or models.resolve(task.spec["model"]).config
    except KeyError:
        cfg = {}
    buffer = compaction_buffer(cfg)
    store.write_json(path, thinking_layer({  # an API task's own thinking variant (#1018)
        "$schema": "https://opencode.ai/config.json",
        # the keyhole's base and token last: they are the provider's way out (#1012); an API
        # task's capped window and output (#1017) and OpenRouter's routing (#1021) before them
        **models._merge(models._merge(models._merge(models.agent_providers(), provider_timeouts(task)),
                                      models._merge(models._merge(stored_key(task), api_pins(task)),
                                                    openrouter_routing(task))),
                        keyhole_overlay(task)),
        **mine,
        "share": "disabled",  # a user's `share: "auto"` would publish every agent session
        # the user's global small_model (titles, summaries) could be a cloud model whose key
        # auth.json holds, sent the task's text; an update mid-task could move OpenCode past
        # OPENCODE_TESTED (#353, the audit of 1.4)
        # with the task's variant: the title request would otherwise think by default (#1018)
        "small_model": task.spec.get("opencode_model") or task.spec["model"],
        "autoupdate": False,
        # background: OpenCode 2.3.2's summary written ahead (on by default; 2.3.0 ignores the
        # key) is a full-context request on the agent's own server, below the handover point,
        # queued ahead of the agent's next step on a one-slot server and evicting its cache,
        # then thrown away at the handover (#661)
        "compaction": {"background": False, **({"auto": True, "buffer": buffer} if buffer else {})},
        # with probe on, one tool call's output sized to the window (output_cap)
        **({"tool_output": output_cap(cfg)} if context.experiment(task, "probe") else {}),
        "mcp": {"servers": {**users_servers_off(), "workbench": {  # 2.x shape: mcp.servers.<name>
            "type": "local",
            "command": [sys.executable, str(PKG_ROOT / "bin" / "wb-ask-mcp")],
            "environment": {"WB_TASK": task.name, "WB_HOME": str(store.home())},
            "codemode": False,  # a plain tool the model sees, not one behind code mode
            # the tool blocks until Claude answers; OpenCode must not give up on it first
            "timeout": {"startup": 15000, "execution": wait_ms},
        }}},
    }, task.spec))
    os.chmod(path, 0o600)  # a provider copied in may carry its apiKey
    return path


def openrouter_routing(task: store.Task) -> dict:
    """An OpenRouter task's routing in the layer, where `wb watch` and the layer's readers see it:
    the role's `order` and `allow_fallbacks: false` under the task's model entry (V1
    `provider.openrouter.models.<m>.options.provider`, which OpenCode 2.3.2 migrates to the
    model's `settings.provider` and its OpenRouter package sends as the body's `provider`). The
    keyhole sets the same on every request, so this is the record, not the guard (#1021)."""
    order = task.spec.get("order")
    mid = str(task.spec.get("model") or "")
    if task.spec.get("via") != "api" or not order or mid.split("/", 1)[0] != privacy.OPENROUTER:
        return {}
    return {"provider": {privacy.OPENROUTER: {"models": {mid.split("/", 1)[1]: {
        "options": {"provider": privacy.routing(order)}}}}}}


def backend_lines(task: store.Task, n: int) -> list[str]:
    """The round report's lines for a backend OpenRouter served outside the role's order
    (keyhole `backend` events of round ``n``): never silent (#1021)."""
    return [f"NOTE: {e.get('text', '')}" for e in store.read_jsonl(task.dir / "events.jsonl")[0]
            if e.get("kind") == "backend" and e.get("round") == n]


def stored_key(task: store.Task) -> dict:
    """The task's provider's key from OpenCode's own credential store, as a `provider` overlay
    for the layer, or {}: the agent's OpenCode runs in the task's home, which holds no
    credentials (#1010), so a local server's key kept by `opencode auth login` reaches it here,
    in the 0600 layer, never in its environment (/proc/<pid>/environ is readable in the lane).
    Only for a provider on this machine (or models.json `local: true`) with no options.apiKey of
    its own (models.stored_key); a cloud provider's key never."""
    try:
        pid = models.resolve(task.spec["model"]).provider
    except KeyError:
        return {}
    key = models.stored_key(pid)
    return {"provider": {pid: {"options": {"apiKey": key}}}} if key else {}


# One tool call's output, at most, with probe on (the config layer's `tool_output`): about a tenth
# of the usable input. OpenCode's default, 51,200 bytes, is about 13-14K tokens, a third of a 40K
# window from one result; the outlines, extracts and greps that stayed in context filled the
# window in the A/B (2026-11-21, #857). Measured on OpenCode 2.3.2 (RealOpenCode): it bounds the
# shell's output (the last lines kept, "[showing lines A-B of N; full output saved to <file>]"),
# not the built-in `read`, which keeps its own 2,000 lines or 50 KB ("[Output truncated. Continue
# reading with offset: N]"): the probe note says so instead.
OUTPUT_CAP_SHARE = 0.1
OUTPUT_CAP_MIN = 8000     # bytes: under it a test failure's traceback is cut
OUTPUT_CAP_MAX = 51_200   # bytes: OpenCode 2.3's own default, never raised
OUTPUT_CAP_LINE = 40      # bytes a line, for max_lines


def output_cap(cfg: dict) -> dict:
    """`tool_output` for a model entry: max_bytes about OUTPUT_CAP_SHARE of its usable input
    (usable_input, at context.CHARS_PER_TOKEN bytes a token), within OUTPUT_CAP_MIN and
    OUTPUT_CAP_MAX; max_lines at OUTPUT_CAP_LINE bytes a line."""
    usable = usable_input(cfg.get("limit") or {})
    cap = int(usable * OUTPUT_CAP_SHARE * context.CHARS_PER_TOKEN) if usable else OUTPUT_CAP_MAX
    cap = max(OUTPUT_CAP_MIN, min(OUTPUT_CAP_MAX, cap))
    return {"max_bytes": cap, "max_lines": cap // OUTPUT_CAP_LINE}


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


# The project's own OpenCode config stays out of the agent's OpenCode: a project's opencode.json
# (the worktree's, or one in a directory above it) and its .opencode/ load after the layer, so
# `{"action": "shell", "resource": "*", "effect": "allow"}` there lifted every deny (the
# 2026-11-16 audit). The rules then went again in OPENCODE_CONFIG_CONTENT, loaded last of all,
# with OPENCODE_DISABLE_PROJECT_CONFIG only the fallback for rules too long for one environment
# string, since it drops AGENTS.md and the project's skills too; a bare task's rules reached
# ~110 KB of the 120,000 bytes and the allowlist couldn't grow (#994, 2026-11-22). Now the switch
# is always on (both its names: OpenCode 2.3.2 reads OPENCODE_CONFIG_PROJECT_DISABLE first, so a
# user's `=0` there would turn it back off), the rules live in the layer file alone, whatever their
# size, and wb hands back what the switch drops (context.agents_files, project_skill_dirs,
# project_config_instructions). With it, `opencode debug config` lists the global files, the
# layer and OPENCODE_CONFIG_CONTENT, nothing else, and the content is wb's (rules_env).
PROJECT_CONFIG_OFF = ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_CONFIG_PROJECT_DISABLE")
# What OPENCODE_CONFIG_CONTENT may hold: one environment string can't pass MAX_ARG_STRLEN
# (128 KiB) on Linux, or OpenCode wouldn't start. Past it, the user's own content is left out.
CONFIG_CONTENT_MAX = 120_000


# What the layer sets that a project's opencode.json, loaded after it, must not change either:
# the compaction backstop past the handover point, the model for titles and summaries, OpenCode
# updating itself (#353). A project config set compaction `auto: false` (a session past the
# handover point then overflowed), sent titles to its cloud small_model, and turned updates on
# past OPENCODE_TESTED (#519). And probe's output cap (output_cap, #857), which a project's
# `tool_output` would lift. With the project's config shut out (PROJECT_CONFIG_OFF, #994) they
# are said again in OPENCODE_CONFIG_CONTENT all the same: should an OpenCode release ever load
# the project's config despite the switch, these still hold (RealOpenCodeRun plants one, #519).
RESTATED = ("small_model", "autoupdate", "compaction", "tool_output")


def model_pins(model_id) -> dict:
    """The task's model entry's `limit` and `options.max_tokens`, as a `provider` overlay: what
    wb planned the handover and the compaction buffer against. A project config's own window for
    the model would move OpenCode's compaction off them (#519). {} for a model wb doesn't know."""
    try:
        m = models.resolve(model_id.partition("#")[0]) if isinstance(model_id, str) else None
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


def api_pins(task: store.Task) -> dict:
    """An API task's model pins (model_pins' shape) for the layer file itself: a catalog model has
    no entry in any config the agent's OpenCode loads, so without them it compacted at the
    catalog's 1M window and asked for 256K out (#1017). A GPU model's entry is the user's, already
    loaded. From the task's api_entry, never the catalog again."""
    entry, model = api_entry(task), str(task.spec.get("model") or "")
    if not entry or "/" not in model:
        return {}
    pid, name = model.split("/", 1)
    return {"provider": {pid: {"models": {name: entry}}}}


# An API task's capped entry is recorded once, when drive resolves its model (status `api_entry`),
# and every round reads it from there: the layer's pins and compaction buffer, the header guard,
# where its speed samples go. Worked out again each round from hosted.json, a catalog rebuild that
# failed mid-task (past HOSTED_TTL or a stamp change; a failed read keeps no models) took them all
# away: the next round compacted at the catalog's 1M window, asked for 256K out, waited on headers
# with no limit, and wrote its speeds into this machine's speed.json (the review of #1017). A later
# runner resolves the model again, and on a catalog that lost it fails the load (_load_failed)
# rather than run a round unpinned.
def pin_api(task: store.Task, m: models.Model) -> None:
    """Status `api_entry`: an API model's `limit` and `options.max_tokens` (models.api_limits'
    caps), or None on the GPU."""
    cfg, entry = m.config, None
    if m.kind == "api" and isinstance(cfg.get("limit"), dict):
        entry = {"limit": cfg["limit"], **({"options": {"max_tokens": cfg["options"]["max_tokens"]}}
                                           if "max_tokens" in (cfg.get("options") or {}) else {})}
    if entry or task.status.get("api_entry"):
        task.set_status(api_entry=entry)


def api_entry(task: store.Task) -> dict | None:
    """The capped entry pin_api recorded for an API task, or None: a GPU task."""
    entry = task.status.get("api_entry")
    return entry if isinstance(entry, dict) and entry else None


def fixed_knobs(task: store.Task, m: models.Model) -> dict:
    """The knobs tuning.tune doesn't choose for this task: Claude's --self-review /
    --no-self-review; else, for an API model, self_review off (#1017): a round Claude never
    sees costs money there, so neither the record nor the explore turns it on."""
    own = task.spec.get("self_review")
    if own is not None:
        return {"self_review": int(own)}
    return {"self_review": 0} if m.kind == "api" else {}


# The user's own OPENCODE_CONFIG_CONTENT goes under the rules, not away: replaced, the providers,
# instructions or permissions kept there were lost to the agent (#347, the audit of 1.4). Its
# permissions (USERS_RULES: `permissions` and the 1.x `permission` object) go into the layer file,
# before the rules (write_config_layer; the last match wins), and leave the content, which loads
# after the layer and would otherwise come after the rules (#994); its MCP servers stay off like
# the global files' ones (the layer's `mcp`, which this would load after). A key the layer sets too
# is left to the layer: loaded last, the user's `compaction` would undo the handover's backstop,
# and their `skills` or `instructions` replace the task's. But `provider`, which the layer always
# sets now (its timeouts, #491): the user's providers stay, and the layer's timeouts go on top of
# them here too, so their content doesn't put the 300 s header wait back. The RESTATED keys and the
# task model's pins (model_pins; the layer's small_model is the task's model) are said again here,
# on top (#519).
USERS_RULES = ("permissions", "permission")


def rules_env(layer: dict, keyhole_over: dict | None = None) -> dict:
    """OpenCode's environment for its config: the project's config off, and
    OPENCODE_CONFIG_CONTENT (above). ``keyhole_over``: the task's keyhole overlay
    (keyhole_overlay), said again in the content's `provider`, on top of the user's own: loaded
    last, a user's `provider.<pid>.options.baseURL` there pointed the agent's OpenCode back at the
    provider's real endpoint, past the keyhole (#1012). A V2 `providers.<pid>` of theirs gets it
    too (keyhole_v2), since it replaces the V1 entry beside it."""
    users = models.users_config_content()
    users = {k: v for k, v in users.items()
             if (k == "provider" or k not in layer) and k not in ("mcp", *USERS_RULES)}
    timeouts = {pid: {"options": {k: v["options"][k] for k in PROVIDER_TIMEOUTS if k in v["options"]}}
                for pid, v in (layer.get("provider") or {}).items()
                if isinstance(v, dict) and isinstance(v.get("options"), dict)
                and any(k in v["options"] for k in PROVIDER_TIMEOUTS)}
    ours = {"share": "disabled", **{k: layer[k] for k in RESTATED if k in layer}}
    pins = model_pins(layer.get("small_model")) or _own_pins(layer)  # an API task's: its record (#1017)
    over = models._merge(timeouts, pins.get("provider") or {})
    if over := models._merge(over, (keyhole_over or {}).get("provider") or {}):
        ours["provider"] = over
    content = {**users, **ours}
    if over and isinstance(users.get("provider"), dict):
        content["provider"] = models._merge(users["provider"], over)
    if keyhole_over and isinstance(theirs := users.get("providers"), dict):
        content["providers"] = {**theirs, **{
            pid: models._merge(theirs[pid] if isinstance(theirs[pid], dict) else {}, v2)
            for pid, v2 in keyhole_v2(keyhole_over).items() if pid in theirs}}
    content, ours = restate_thinking(layer, content), restate_thinking(layer, ours)  # #1018
    text = json.dumps(content, separators=(",", ":"))
    if len(text.encode()) > CONFIG_CONTENT_MAX:  # the user's own, too long to add to: wb's alone
        text = json.dumps(ours, separators=(",", ":"))
    return {**dict.fromkeys(PROJECT_CONFIG_OFF, "1"), "OPENCODE_CONFIG_CONTENT": text}


def _own_pins(layer: dict) -> dict:
    """The layer's own entry for its small_model (an API task's api_pins), as model_pins gives
    one: restated from the task's record when the catalog no longer knows the model (#1017)."""
    # the small_model names the task's thinking variant (#1018): the entry is the bare id's
    pid, _, name = str(layer.get("small_model") or "").partition("#")[0].partition("/")
    entry = (((layer.get("provider") or {}).get(pid) or {}).get("models") or {}).get(name)
    entry = {k: entry[k] for k in ("limit", "options") if k in entry} if isinstance(entry, dict) else {}
    if isinstance(entry.get("options"), dict):
        entry["options"] = {k: v for k, v in entry["options"].items() if k == "max_tokens"}
    return {"provider": {pid: {"models": {name: entry}}}} if entry else {}


# What OpenCode's own environment adds to the agent's: npx and `npm exec` run only what the
# project installed, instead of fetching a package unasked (npm says yes by itself without a
# terminal; the 2026-11-16 audit). Not the tests' environment: they run as they would for you.
# And OpenCode reads ~/.claude/CLAUDE.md itself as a global instruction (with the user's own
# ~/.config/opencode/AGENTS.md), which only the lane's SECRETS kept out: with no Landlock (macOS,
# an old kernel, `"paths": ["*"]`) the user's CLAUDE.md, written to Claude, reached every agent
# whatever wb handed it (measured on OpenCode 2.3.2 with tests/capture_llm.py, #1061, 2026-11-23).
# The switch drops only that file: the project's CLAUDE.md is wb's to hand (PROJECT_CONFIG_OFF).
OPENCODE_ENV = {"npm_config_yes": "false", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1"}


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


# The cloud SDKs' own switches off their instance metadata lookup (#993, 2026-11-22): on a cloud
# VM, 169.254.169.254 hands the machine's role credentials to anyone who asks over plain HTTP,
# and the lane doesn't restrict TCP, so an SDK that finds no credentials in the environment
# (SECRET_ENV drops them) or in the hidden files falls back to it. AWS's SDKs and CLI
# (AWS_EC2_METADATA_DISABLED, the AWS SDK reference), google-auth for Python (NO_GCE_CHECK:
# compute_engine/_metadata.is_on_gce returns False for "true") and gcp-metadata for Node
# (METADATA_SERVER_DETECTION=none: "don't try to use it"). Not GCE_METADATA_HOST or
# GCE_METADATA_IP: they only move the server, and Google's Go metadata client
# (cloud.google.com/go/compute/metadata) takes a set GCE_METADATA_HOST as being on GCE. Azure's
# SDKs have no such switch, and neither has that Go client: a known gap, as is any program that
# asks the address itself. The tests get them too
# (run_in_lane takes agent_env): they run the agent's code, which must meet the same lane.
METADATA_OFF = {"AWS_EC2_METADATA_DISABLED": "true", "NO_GCE_CHECK": "true",
                "METADATA_SERVER_DETECTION": "none"}
# and the build tools' remote caches and remote execution, which a repo's own config turns on
# without a flag (#999, 2026-11-22): the agent's unreviewed worktree mustn't write the team's
# cache, nor run on its remote workers, and the tests run the same code. Nx: NX_NO_CLOUD and
# --no-cloud stop only Nx Cloud (isNxCloudDisabled), and a self-hosted cache (@nx/s3-cache,
# @nx/gcs-cache, @nx/azure-cache, @nx/shared-fs-cache, NX_SELF_HOSTED_REMOTE_CACHE_SERVER) was
# still read and written; NX_SKIP_REMOTE_CACHE=true sets skipRemoteCache, which _getRemoteCache
# checks first ("Includes Nx Cloud and self-hosted caches", its reference), and a flag can't undo
# it. Pants: its environment beats pants.toml (its options docs), so writes and remote execution
# stay off whatever the repo says; its reads stay. Their flag and variable spellings that turn
# them back on are context.OUTWARD's.
REMOTE_CACHES_OFF = {"NX_SKIP_REMOTE_CACHE": "true", "PANTS_REMOTE_CACHE_WRITE": "false",
                     "PANTS_REMOTE_EXECUTION": "false"}


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
           "OPENCODE_CONFIG": str(layer),
           **rules_env(json.loads(layer.read_text()), keyhole_overlay(task)),
           "WB_TASK": task.name, "WB_HOME": str(store.home()), **CLEAN_ENV, **METADATA_OFF,
           **REMOTE_CACHES_OFF, **({"WB_RUN": run} if run else {})}
    env.update(git_config_env(env))
    env["PATH"] = env.get("PATH") or os.defpath
    if keyhole_of(task):  # an http_proxy kept for the agent must not carry the keyhole's
        # loopback URL away (#1012). Both spellings get the union of both: Python and curl read
        # no_proxy first, Go NO_PROXY, and each extended alone, the one the user hadn't set
        # held 127.0.0.1 only, which dropped their own hosts (the review of #1015, 2026-11-23)
        hosts = [h.strip() for name in ("NO_PROXY", "no_proxy")
                 for h in (env.get(name) or "").split(",") if h.strip()]
        env["NO_PROXY"] = env["no_proxy"] = ",".join(dict.fromkeys([*hosts, "127.0.0.1"]))
    places = context.writable(task)
    if places is not None:  # its own: the user's holds their bus and agents
        env["XDG_RUNTIME_DIR"] = str(context.runtime_dir(task))
        # Jupyter's kernel connection files, in the agent's runtime dir: its default,
        # ~/.local/share/jupyter/runtime, is outside the lane, and every notebook test
        # (nbmake, nbval, papermill) failed with PermissionError before a kernel started (#668)
        env.setdefault("JUPYTER_RUNTIME_DIR", str(context.runtime_dir(task) / "jupyter"))
        # helm's registry logins, its own: the user's are hidden (context.SECRETS, #992), and
        # helm stops at every start, `helm lint` and `helm template` too, when the file it is
        # given exists but can't be opened; one not made yet is an empty store
        env["HELM_REGISTRY_CONFIG"] = str(context.runtime_dir(task) / "helm" / "registry.json")
        # and k6's config, its own too (#992, 2026-11-22): `k6 run` reads ~/.config/k6/config.json,
        # hidden for `k6 cloud login`'s token, at every start and stops when it can't. Made, since
        # k6 forgives only a missing default; at the runtime dir's top, where O_EXCL won't follow
        # a link the agent left
        k6 = context.runtime_dir(task) / "k6.json"
        try:
            with open(k6, "x") as f:
                f.write("{}\n")
        except OSError:  # made already (an earlier round), or the agent's: k6 says what's wrong
            pass
        env["K6_CONFIG"] = str(k6)
        # and semgrep's settings (#999, 2026-11-22): ~/.semgrep/settings.yml holds `semgrep
        # login`'s token, hidden, and both semgrep's Python and its OCaml core find it there (the
        # lane leaves access() and stat() alone: access(R_OK) said yes in a real lane) and then
        # fail on the read, uncaught (settings.py has no except on it; Semgrep_settings.from_file
        # catches only Failure). Not made: both take a missing one for the defaults, and save
        # there what they keep (SEMGREP_SETTINGS_FILE, read by env.py and Semgrep_envvars.ml)
        env["SEMGREP_SETTINGS_FILE"] = str(context.runtime_dir(task) / "semgrep-settings.yml")
        env.update(daemon_env(task, env))  # its own build daemons, not the user's (#669)
        # poetry's and pipenv's venv in the worktree, as `uv sync` makes it (own_venv): their
        # homes are split around an active venv (`poetry shell`, direnv), where no new one can
        # be made, so a setup's `poetry install` was refused (#825); the venv ignores itself
        env.setdefault("POETRY_VIRTUALENVS_IN_PROJECT", "true")
        env.setdefault("PIPENV_VENV_IN_PROJECT", "1")
        # On WSL, not the Windows PATH, whose programs run outside the lane (context.agent_path),
        # nor the interop socket's name (the 0.90 review; /init finds it without: interop_gap)
        env.pop("WSL_INTEROP", None)
        env["PATH"] = context.agent_path(env["PATH"])
    # probe search has no output limit of its own: one plain query on workbench printed 169 KB,
    # about 42K tokens, more than a 40K window holds. Capped unless the user set one (#857)
    if context.experiment(task, "probe"):
        env["PROBE_MAX_TOKENS"] = os.environ.get("PROBE_MAX_TOKENS") or str(context.PROBE_MAX_TOKENS)
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
    # the repository's main worktree when the linked one the task started from was removed: its
    # git dir stays, and `git -C <removed>` crashed the runner on every round (#776)
    # the worktree's file system as wb's own git sees it, for the agent's git too (#796)
    pins = gitops.fs_pins(task.worktree, differing=True) if task.worktree.is_dir() else []
    if gitguard.install(shim, gitops.repo_of(task.spec), PKG_ROOT, outside, task.worktree, scratch,
                        pins):
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
# A job left in the background after the agent's turn ended (step_finish reason `stop`): OpenCode
# waits on it before it exits, and a dev server never ends, so the round sat out the long wait
# below and the finished work was reported stuck, its tests never run (#658). Past this much
# quiet (or the round's silence limit, when shorter) the round is done: the job is stopped with
# the agent, and the tests run. A job the turn itself waits on (reason `tool-calls`) keeps the
# long wait.
BG_GRACE = 120


def _stop(proc: subprocess.Popen):
    # PermissionError: macOS's answer (EPERM, not Linux's ESRCH) for a group whose members have all
    # exited, the leader not yet reaped. OpenCode exits right after a failed compaction, and the
    # handover's stop crashed the runner there, the task failed (the macOS nightly, 2026-11-22)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
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


OUTLINE_TIMEOUT = 20  # seconds for each `probe symbols`: a probe that hangs costs round 1 no more


def add_outline(task: store.Task) -> None:
    """Round 1's prompt, before its first OpenCode call, gains the outline of the long files its
    spec names (outline_section), once: a restarted runner doesn't add it again. Only with probe
    on; nothing it meets (no probe, a failure, a timeout) fails the round or adds anything."""
    if not context.experiment(task, "probe") or "outline" in task.status or task.status.get("session"):
        return
    path = task.round_dir(1) / "prompt.md"
    try:
        section, files = outline_section(task, path.read_text())
        if section:
            path.write_text(path.read_text().rstrip("\n") + "\n\n" + section)
    except Exception as e:  # noqa: BLE001: the outline is a help, never the round's end
        print(f"workbench: no outline for round 1 ({e})", file=sys.stderr)
        files = []
    task.set_status(outline=files)


def outline_section(task: store.Task, prompt: str) -> tuple[str, list[str]]:
    """(the outline section, the files in it): for each file of more than PROBE_FILE_LINES lines
    the spec names (context.long_files, in the worktree), `probe symbols`, its symbols longest
    first, all of them within PROBE_OUTLINE_TOKENS, shared evenly (what a file leaves goes to the
    next: a 5,900-line file took it all and two others named beside it got nothing, trying it on
    workbench, 2026-11-21); a file cut short ends with OUTLINE_MORE. Only files in a language
    probe reads (context.probe_languages): probe symbols of a CSV or a Nim file prints its lines
    as `"kind": "text"`, no outline at all (#857, 2026-11-21)."""
    exe = context.probe_bin()
    kinds = context.probe_languages(exe) if exe else None
    if not kinds:
        return "", []
    from .cli import PROTOCOL  # cli imports this module
    spec = prompt.split(PROTOCOL.split("{branch}")[0])[0]  # the spec, not the protocol's names
    left = context.PROBE_OUTLINE_TOKENS * context.CHARS_PER_TOKEN
    found = [(rel, syms) for rel in context.long_files(spec, task.worktree, task.spec.get("subdir"),
                                                       kinds=kinds)
             if (syms := probe_symbols(exe, task.worktree, rel))]
    parts, files = [], []
    for k, (rel, syms) in enumerate(found):
        room = left // (len(found) - k)
        # named as the agent reaches it from where it runs (task.workdir): in a package task
        # `pkg/big.py`, from the worktree's top, sent it to pkg/pkg/big.py (review, 2026-11-21)
        shown = os.path.relpath(task.worktree / rel, task.workdir)
        head = context.OUTLINE_HEAD.format(file=shown)
        more = lambda n: context.OUTLINE_MORE.format(n=n, file=shown)  # noqa: E731
        if room < len(head) + len(more(len(syms))) + 2:
            continue
        lines, room = [head], room - len(head) - 1
        for i, (a, b, sig, parent) in enumerate(syms):
            line = (f"{a}-{b}" if b > a else f"{a}") + f" {sig}" + (f"  (in {parent})" if parent else "")
            if len(line) + 1 + (len(more(len(syms))) + 1 if i < len(syms) - 1 else 0) > room:
                lines.append(more(len(syms) - i))
                break
            lines.append(line)
            room -= len(line) + 1
        text = "\n".join(lines)
        left -= len(text) + 2
        parts.append(text)
        files.append(rel)
    return ("\n\n".join(parts) + "\n" if parts else ""), files


def probe_symbols(exe: str, root: Path, rel: str) -> list[tuple[int, int, str, str]] | None:
    """`probe symbols --allow-tests` of one file, as (first line, last line, signature, the class
    it is in), longest first, or None when probe fails, times out or prints what isn't its JSON.
    Read with errors="replace": a probe that isn't probelabs' may print anything (#518)."""
    try:
        r = subprocess.run([exe, "symbols", "--allow-tests", "-o", "json", rel], cwd=root,
                           capture_output=True, text=True, errors="replace", timeout=OUTLINE_TIMEOUT,
                           stdin=subprocess.DEVNULL)
        found = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    out: list[tuple[int, int, str, str]] = []

    def walk(syms, parent: str) -> None:
        for x in syms if isinstance(syms, list) else ():
            if not isinstance(x, dict) or not isinstance(x.get("line"), int):
                continue
            end = x.get("end_line") if isinstance(x.get("end_line"), int) else x["line"]
            sig = " ".join(str(x.get("signature") or x.get("name") or "").split())[:160]
            out.append((x["line"], max(end, x["line"]), sig, parent))
            walk(x.get("children"), str(x.get("name") or ""))
    for f in found if isinstance(found, list) else ():
        walk(f.get("symbols") if isinstance(f, dict) else None, "")
    out.sort(key=lambda s: (s[0] - s[1], s[0]))
    return out or None


def outside_spec(task: store.Task, files: str) -> list[str]:
    """Changed files the current spec never names, when it names any: the agent's drift shows
    here first (seen 2026-11-16: a flow-field module nobody asked for). Matched by path or name;
    a brace form names each (`src/replay_io.{h,c}`: flagged as unnamed on 2026-11-18)."""
    spec = current_task_prompt(task)
    spec += "\n" + " ".join(context.brace_names(spec))
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
    """The step's active work in seconds, as the ETA compares it with the typical length (a
    step's, router.score): status `active_s` (the rounds so far, written as each ends) less
    `step_active_s` (the earlier steps': wb next, #815) plus the round under way, whole."""
    st, now = task.status, now or time.time()
    running = st.get("round_started_at") if st.get("phase") in ("running", "testing", "asking") else None
    return float(st.get("active_s") or 0) - float(st.get("step_active_s") or 0) + \
        (max(0.0, now - running) if running else 0.0)


def typical_now(task: store.Task) -> dict:
    """Status `typical` {min, n} for a round's start: the step's typical active minutes on this
    model and size (router.expected, the files alone, #819; a writing task's on writing's record,
    as progress.typical reads it) and how many timed tasks back it, so the pane can set its
    estimate against them with no read of its own (#1048); none with no history, nor for an API
    model hosted.json doesn't list (lookup finds no model to time). And `late_min`, the progress
    checks' first point (the user's --after, else the typical length at least FLOOR_MIN): the
    pane colours a task late only from there, as a check would come, never from a raw median of
    a few minutes (step A's review); none while that is only the size's default."""
    from . import router  # router -> models; imported late, as checkin_text does
    spec = task.spec
    try:  # the pane's estimate only: a record unreadable never stops the round it would time
        mins, n = router.expected(spec.get("model", ""), spec.get("size", "significant"),
                                  spec.get("weights_id"), router.record_role(spec.get("role")))
        late, source = progress.typical(spec)
    except Exception:
        return {}
    return {**({"typical": {"min": round(float(mins), 2), "n": n}} if mins is not None else {}),
            **({"late_min": round(float(late), 2)} if source != "default" else {})}


def cards_now(task: store.Task, m: models.Model) -> dict:
    """Status `cards`, the cards a local GPU model's Ollama sees (models.card_numbers, #1023), for
    the pane's `GPU 1` (#1048): only on a machine of two or more discrete cards, where it tells
    tasks apart; one card stays `GPU`, and an API or remote model has none. A runner after one
    that wrote cards clears them when it can't tell."""
    cards = None
    try:  # the pane's label only: a server or sysfs that can't be read never stops the round
        if m.kind == "gpu" and m.engine == "ollama" and not m.remote:
            cards = models.card_numbers(m.root)
            if cards and len(cards) < 2:
                devs = hardware.gpus(windows=False)
                if len([g for g in devs if g.get("discrete")] or devs) < 2:
                    cards = None
    except Exception:
        cards = None
    return {"cards": cards} if cards or task.status.get("cards") else {}


def checkin_text(task: store.Task, n: int, out: dict, since: float, active: float | None = None) -> str:
    """The digest a check-in wakes Claude with: what changed since the last one, and timing.
    ``active``: the round's own count of active work so far, when the round knows it."""
    from . import router  # router -> models; imported late to keep the runner's startup lean
    spec, st, now = task.spec, task.status, time.time()
    took = now - spec["created_at"]
    # the role's record only for writing (router.record_role), as the pane and progress checks
    # read it: the raw role timed `--role trivial --size significant` on trivial rows (#1113, 2026-11-23)
    typical = router.expected_minutes(spec["model"], spec.get("size", "significant"),
                                      spec.get("weights_id"), router.record_role(spec.get("role")))
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
    to a question, an API provider's rate limit, outage or queue (#1020), and a frozen machine."""

    def __init__(self, t: float):
        self.t0 = self.tick = self.seen = t
        self.paused = 0.0

    def step(self, t: float, asking: bool, waiting: bool = False) -> None:
        dt, self.tick = t - self.tick, t
        if asking or waiting:  # waiting on Claude, or on the provider: neither the round's time nor silence
            self.paused += dt
            self.seen = t
        elif dt > FROZEN_SECS:
            self.paused += dt
            self.seen += dt

    def ran(self, t: float) -> float:
        return t - self.t0 - self.paused

    def quiet(self, t: float) -> float:
        return t - self.seen


# An API provider making the agent wait (#1020). The keyhole (keyhole.py) holds a request through
# a 429 or an outage and writes status `ratelimit_until`/`ratelimit`; OpenCode prints nothing
# while it waits, so without this the silence guard said "no output for 15 minutes", or "the model
# may be reading its prompt slowly", of a provider that had said "wait", and the think nudge fired
# on DeepSeek's queue. Held this long, the round stops as `rate_limited` or `outage`, the
# provider's and not the step's, and runs again once (drive).
RATE_WAIT_S = 600
RETRY_PAUSE_S = 60   # an API `retry` line pauses the clocks this long: OpenCode's own backoff
HELD = ("rate_limited", "outage")
# stops that are the provider's (cause "provider"); `setup` is this machine's way to it (a
# certificate, a proxy: the keyhole's api_stop, #1080), never the agent's either, and not revived.
# `refused`: a request the provider turned down for a reason no retry or key fixes (a 404 for a
# model gone, a 400/422 schema, a 403 moderation or permission): the agent's, with no remedy, or
# under 403 "refused the API key" with /connect (#1082, #1084, 2026-11-23)
PROVIDER_GUARDS = frozenset({*HELD, "quota", "auth", "setup", "refused"})
# no 413: OpenCode 2.3.2 calls it payload-too-large, beside context-overflow, a step that read too
# much and so the step's (the review of #1082, 2026-11-23)
REFUSED_CODES = frozenset({400, 403, 404, 405, 409, 410, 415, 422})
REFUSED_KINDS = frozenset({"provider.content-filter", "provider.invalid-request", "provider.no-route",
                           "provider.unsupported-operation"})
# a 403 is the key's only when it says so: moderation, a model or region the key may not use and
# the keyhole's own "this task may use only <model>" are 403s too (#1084). OpenCode's
# provider.auth with another status is the key's when it says so too: 2.3.2 types a 400
# "Incorrect API key provided", api_key_invalid and authentication_error that way (the review of
# #1084, 2026-11-23)
KEY_WORDS = re.compile(r"(?i)api.?key|token|credential|unauthori[sz]ed|authenticat")
# a request over the model's window is the step's (too big a read), whoever answered it (#1082)
CONTEXT_WORDS = re.compile(r"(?i)context.{0,20}(length|window|limit)|too many tokens|too long|"
                           r"maximum.{0,30}tokens")
# a provider queue (the keyhole's `queued`: SSE keep-alives only) pauses the clocks, and OpenCode's
# ten retries of the requests it closed unanswered held a round ~110 minutes on paused clocks, then
# failed it as the agent's (#1083, 2026-11-23): a queued run this long stops as an `outage`
QUEUE_WAIT_S = 2 * RATE_WAIT_S
# #1019's day spend cap: status `api_stop {guard: "spend"}` or its own stop between rounds. A cause
# of its own, `spend`, outside results.THE_STEP like `provider`: the user's budget, never held
# against the model or its slice (the engineer's call, 2026-11-23)
SPEND = "spend"


def stopped_by(guard: str | None) -> str | None:
    """Whose stop a guard is when it isn't the agent's: `provider` (PROVIDER_GUARDS) or `spend`
    (the cap), the notice's cause, status `infra` and the `stopped` event's `by`; else None."""
    return "provider" if guard in PROVIDER_GUARDS else SPEND if guard == SPEND else None


def api_task(task: store.Task) -> bool:
    """Whether the task runs an API model: task.json `via` (#1016), else its model's kind in the
    cached catalog. Every task before #1016 reads as GPU."""
    via = task.spec.get("via")
    if via in ("gpu", "api"):
        return via == "api"
    try:
        return models.is_api(task.spec.get("model") or "")
    except Exception:  # a config that can't be read: a GPU task, as before
        return False


def settle_via(task: store.Task, api: bool) -> None:
    """task.json `via` (and an API model's `provider`) written once, from the model drive resolved,
    for a task from before #1016 that has none. A 1.91 task on an opted-in Ollama `:cloud` model ran
    as an API task (lease, meter, keyhole) while results, privacy's local_only and the lane's
    token hiding read it as GPU by `via` alone (#1111, 2026-11-23): now every reader agrees."""
    if task.spec.get("via") in ("gpu", "api"):
        return
    spec = {**task.spec, "via": "api" if api else "gpu"}
    if api:
        spec.setdefault("provider", provider_of(task))
    store.write_json(task.dir / "task.json", spec)


def self_review_aside(task: store.Task) -> Path:
    """Where SELF_REVIEW has the agent copy a file aside: `aside/` in its runtime directory
    (context.runtime_dir: the task's own, 0700, writable in its lane, removed at accept and
    discard), never a name in /tmp another task shares (#1085)."""
    d = context.runtime_dir(task) / "aside"
    d.mkdir(exist_ok=True)
    return d


def provider_of(task: store.Task) -> str:
    """The task's provider id: task.json `provider` (#1016), else its model id before the first /."""
    return str(task.spec.get("provider") or (task.spec.get("model") or "").partition("/")[0])


def _span(secs: float) -> str:
    secs = int(max(0, secs))
    if secs >= 120:
        return f"{secs // 60} minutes"
    return "1 minute" if secs >= 60 else f"{secs} seconds"


class _ProviderWait:
    """One round's view of its API provider (#1020): the keyhole's status keys and OpenCode's own
    `retry` and `error` lines. ``waiting`` pauses the clocks, ``stop`` says when the provider (not
    the agent) ends the round, ``end`` closes it: the `rate_limited` event and the stop after
    OpenCode gave up. A GPU task's is inert: nothing pauses, nothing stops."""

    def __init__(self, task: store.Task, n: int):
        self.task, self.n = task, n
        self.api = api_task(task)
        self.provider = provider_of(task) if self.api else ""
        self.began = time.time()
        self.st: dict = {}
        self.retry: dict | None = None  # the run OpenCode's retry lines show: kind, code, since, why
        self.retry_until = 0.0
        self.run: dict | None = None    # the wait now: kind, code, since, held_since, why
        self.was = False                # waiting at the last tick: the gap since counts as the wait
        self.fatal = None               # (guard, text): a 402 or 401 OpenCode gave up on at once
        self.held = False               # any wait this round (measure_speed skips the round)
        self.queued_since = None        # a queue no answer has ended yet (#1083)
        self.cut = ""                   # OpenCode's provider.invalid-output: answers that never ended
        self.worst = None               # the room's refused request at its worst, for stop_text (#1150)
        st = task.status
        if self.api and (st.get("ratelimit_until") or st.get("ratelimit")):
            # a runner killed mid-hold left them: the new round starts with none (edge cases)
            task.set_status(ratelimit_until=None, ratelimit=None)

    def waiting(self, now: float) -> bool:
        if not self.api:
            return False
        self.st = st = self.task.status
        rl, until = st.get("ratelimit"), st.get("ratelimit_until")
        now_wait = None
        if isinstance(rl, dict) and isinstance(until, (int, float)) and until > now:
            since = rl.get("since") if isinstance(rl.get("since"), (int, float)) else now
            now_wait = {"kind": rl.get("kind"), "code": rl.get("code"), "why": rl.get("why"),
                        "since": max(since, self.began)}
        elif self.retry and self.retry_until > now:
            now_wait = dict(self.retry)
        if now_wait is None:
            if self.run:
                self._ended(now)
            was, self.was = self.was, False
            return was
        if now_wait["kind"] == "queued" and self.queued_since is None:
            self.queued_since = now_wait["since"]
        if self.run is None:
            self.run = {**now_wait, "held_since": None}
        else:
            self.run.update({k: v for k, v in now_wait.items() if k != "since" and v is not None})
            self.run["since"] = min(self.run["since"], now_wait["since"])
        if self.run["kind"] in HELD and self.run["held_since"] is None:
            self.run["held_since"] = now_wait["since"] if now_wait["kind"] in HELD else now
        self.was = self.held = True
        return True

    def on_retry(self, raw: dict, now: float) -> None:
        """An OpenCode `retry` line: a 429, 5xx, rate limit or transport failure pauses the clocks
        RETRY_PAUSE_S (or to its nextRetryAt), across the gap before the keyhole's next write."""
        if not self.api:
            return
        err = raw.get("error") if isinstance(raw.get("error"), dict) else {}
        code = err.get("status") if isinstance(err.get("status"), int) else None
        text = f"{err.get('type') or ''} {err.get('message') or ''}"
        rate = code in (429, 529) or bool(re.search(r"(?i)rate.?limit", text))
        if not (rate or (code or 0) >= 500 or "transport" in text):
            return
        why = "connection refused" if "ECONNREFUSED" in text else "no answer"
        self.retry = {"kind": "rate_limited" if rate else "outage", "code": code, "why": why,
                      "since": self.retry["since"] if self.retry else now}
        nxt = raw.get("nextRetryAt")
        later = nxt / 1000 + 5 if isinstance(nxt, (int, float)) and nxt / 1000 < now + 900 else 0
        self.retry_until = max(now + RETRY_PAUSE_S, later)

    def on_error(self, raw: dict) -> None:
        """An OpenCode `error` line: a 402 (provider.quota), a 400 whose text is a spent balance
        (keyhole.CREDIT), a 401 or a 403 that names the key (provider.auth), or a request the
        provider refused (REFUSED_CODES, REFUSED_KINDS; not one over the window) ends the round at
        once, the provider's; a rate limit, an outage or answers cut short
        (provider.invalid-output) it gave up on, `end` stops."""
        if not self.api:
            return
        err = raw.get("error") if isinstance(raw.get("error"), dict) else {}
        kind, code = str(err.get("type") or ""), err.get("status")
        said = (str(err.get("message") or "").strip().splitlines() or [""])[0][:160]
        # Anthropic's spent balance is a 400 invalid_request_error (#1079): its wording,
        # keyhole.CREDIT, checked before REFUSED_CODES' 400 makes it a `refused`
        if kind == "provider.quota" or code == 402 or (
                code == 400 and keyhole.CREDIT.search(str(err.get("message") or ""))):
            self.fatal = ("quota", f"{self.provider} says the account is out of credit "
                                   f"(HTTP {code or 402})")
        elif code == 401 or ((kind == "provider.auth" or code == 403) and
                             (code is None or KEY_WORDS.search(said))):
            self.fatal = ("auth", f"{self.provider} refused the API key (HTTP {code or 401})")
        elif (code in REFUSED_CODES or kind in REFUSED_KINDS or kind == "provider.auth") \
                and not CONTEXT_WORDS.search(said):
            self.fatal = ("refused", f"{self.provider} refused the request"
                                     + (f" (HTTP {code})" if code else "") + (f": {said}" if said else ""))
        elif kind == "provider.invalid-output" and (code == 200 or self.queued_since is not None):
            # only an HTTP answer the provider cut short, or one out of its queue: 2.3.2 types the
            # model's own overrun ("exceeded its output limit") and bad structured output this way
            # too, and those are the step's (the review of #1083, 2026-11-23)
            self.cut = said or "its answer ended early"

    def answered(self) -> None:
        """The model's own output: whatever OpenCode retried got through, out of any queue."""
        self.retry, self.retry_until = None, 0.0
        self.queued_since, self.cut = None, ""

    def stop(self, now: float, gave_up: bool = False) -> tuple[str, str] | None:
        """(guard, text) when the provider ends the round: a quota or key error, the keyhole's (or
        #1019's) `api_stop`, or a rate limit or outage held RATE_WAIT_S (``gave_up``: any held
        run, OpenCode having exited on it)."""
        if not self.api:
            return None
        stop = self.st.get("api_stop")
        if isinstance(stop, dict) and stop.get("guard"):
            # consumed: the next round starts clean. #1019's `spend` comes through here too:
            # cause `spend`, not `provider` (stopped_by). Before `fatal`: the keyhole answers its
            # own quota and spend stops with a 400, which on_error would call `refused` (#1082)
            self.task.set_status(api_stop=None)
            # its `worst` goes on with the round (out["worst"], in end): stop_text, after the
            # round, would find no api_stop and say "the cap is reached" under it (#1150, 2026-11-24)
            self.worst = stop.get("worst")
            return str(stop["guard"]), str(stop.get("text") or f"{self.provider} stopped the round")
        if self.fatal:
            return self.fatal
        if self.queued_since is not None and (gave_up or (self.run and self.run["kind"] == "queued" and
                                                           now - self.queued_since >= QUEUE_WAIT_S)):
            return "outage", (f"{self.provider} held the agent's requests in its queue for "
                              f"{_span(now - self.queued_since)} without answering")
        if gave_up and self.cut:  # OpenCode's ten resends, every answer cut short
            return "outage", f"{self.provider} cut its answers short ({self.cut})"
        run = self.run or ({**self.retry, "held_since": self.retry["since"]} if gave_up and self.retry else None)
        if not run or run["kind"] not in HELD or run["held_since"] is None:
            return None
        secs = now - run["held_since"]
        if secs < RATE_WAIT_S and not gave_up:
            return None
        if run["kind"] == "rate_limited":
            return "rate_limited", (f"{self.provider} rate-limited the agent's requests for "
                                    f"{_span(secs)} (HTTP {run.get('code') or 429})")
        detail = f"HTTP {run['code']}" if run.get("code") else run.get("why") or "no answer"
        return "outage", f"{self.provider} didn't answer for {_span(secs)} ({detail})"

    def _ended(self, now: float) -> None:
        run, self.run = self.run, None
        if not run:
            return
        secs = round(now - run["since"], 1)
        code = f" (HTTP {run['code']})" if run.get("code") else ""
        what = {"rate_limited": f"rate-limited by {self.provider}{code}",
                "outage": f"{self.provider} didn't answer{code}",
                "queued": f"{self.provider} held the request in its queue"}.get(run["kind"], "waited")
        self.task.event({"ts": store.now(), "round": self.n, "kind": "rate_limited",
                         "provider": self.provider, "code": run.get("code"), "state": run["kind"],
                         "since": run["since"], "secs": secs,
                         "text": f"{what} for {_span(secs)}: the round's clocks were paused"})

    def end(self, out: dict, now: float) -> None:
        """After the round: OpenCode that exited on a rate limit, an outage or a quota error is
        the provider's stop; the wait's event; the status keys cleared; `held` for drive."""
        if not self.api:
            return
        if not out["stuck"] and out["exit"] not in (0, None) and not (
                out.get("lingered") or out.get("settled") or out.get("interrupted") or out.get("full")):
            self.st = self.task.status
            if why := self.stop(now, gave_up=True):
                out["guard"], out["stuck"] = why
                self.task.event({"ts": store.now(), "round": self.n, "kind": "stopped", "text": why[1],
                                 **({"by": by} if (by := stopped_by(why[0])) else {})})
        self._ended(now)
        out["held"] = self.held
        if self.worst is not None:
            out["worst"] = self.worst
        st = self.task.status
        if st.get("ratelimit_until") or st.get("ratelimit"):
            self.task.set_status(ratelimit_until=None, ratelimit=None)


# OpenCode's own lines, not the model's output: what read_bound counts as no answer yet
NOT_THE_MODEL = frozenset({"step_start", "retry", "error", "background_wait"})


def renudged(st: dict, n: int) -> bool:
    """Round n stalling is stopped, not nudged: the last round ended in the runner's nudge and
    this is the session it nudged. A fresh one never thought for long, and its first long think
    was stopped "even after a nudge" (#681)."""
    return st.get("nudged_round") == n - 1 and bool(st.get("session"))


# How often a round waiting on a background job or on Claude's answer renews the model's
# keep-alive: under Ollama's default 5 minutes, which each of OpenCode's requests sets again (#864)
KEEP_WARM_S = 180


def _keep_warm(model_id) -> None:
    try:
        models.keep_warm(models.resolve(model_id))
    except Exception:  # an unknown model or a config that can't be read: nothing to renew
        pass


def opencode_env(task: store.Task, run: str = "") -> dict:
    """OpenCode's own environment: the agent's (agent_env), OPENCODE_ENV, the task's OpenCode
    home (context.opencode_home_env, made first: the lane holds only what exists) and the shell
    shim as its SHELL, so its commands get the user's data directories back (#1010). Not in
    agent_env: the tests' environment never has OpenCode's."""
    context.make_opencode_home(task)
    # an API task's own catalog, with the fetch still off (write_catalog, #1015)
    catalog = {"OPENCODE_MODELS_PATH": str(catalog_path(task))} \
        if keyhole_of(task) and catalog_path(task).is_file() else {}
    return {**agent_env(task, run), **OPENCODE_ENV, **context.opencode_home_env(task), **catalog,
            "SHELL": str(write_shell_shim(task))}


# The explore guard's calls without an edit, per 32K of an API model's window: 40 was set where 40
# reads fill a 32K window, and stopped API one-shots (131K) still exploring at 16% of theirs
# (#1087, 2026-11-23). A loop that never fills the window still stops, at NO_EDIT_MAX. A GPU
# task's stays 40 whatever its window, as in 1.91.x: the 40 was tuned on local models, the ones
# that loop on reads (the review of #1087, 2026-11-23)
NO_EDIT_PER_32K = 40
NO_EDIT_MAX = 160


def no_edit_limit(window, *, api: bool) -> int:
    """events.StuckDetector's `no_edit` for a model of this window (status `window`): on an API
    model 40 for 32K or less (or unknown), 40 more for each 32K over it, at most NO_EDIT_MAX; on
    the GPU (``api`` false) always NO_EDIT_PER_32K."""
    w = window if api and isinstance(window, int) and window > 0 else 0
    return min(NO_EDIT_MAX, NO_EDIT_PER_32K * max(1, w // 32768))


def run_round(task: store.Task, n: int, keep: bool = False) -> dict:
    """One `opencode run`. keep: Claude asked for this session to carry on (wb feedback
    --keep-context), so the round never hands over mid-way."""
    spec, st = task.spec, task.status
    prompt = task.round_dir(n).joinpath("prompt.md").read_text()
    cmd = opencode_bin() + ["run", "--standalone", "-m", spec["opencode_model"], "--auto",
                            "--format", "json", "--title", f"wb: {task.name}"]
    began = gitops.mark(task.worktree)  # the tree as the round began
    marks = {began}  # and as the session began
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
    env = opencode_env(task, run)
    if not task.status.get("opencode_home"):  # its sessions are in the task's home from now on
        task.set_status(opencode_home=True)
    cmd = lane.wrap(context.writable(task), cmd,   # writes stay in the agent's lane (lane.py),
                    context.readable(task),       # and credentials out of its reach,
                    context.unix_paths(task), lane_ports(task))  # local daemons' TCP too
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
    if measures_power(task):
        power = _Power()
        power.start()

    detector = StuckDetector(**{"no_edit": no_edit_limit(st.get("window"), api=api_task(task)),
                                **spec.get("stuck", {})},
                             worktree=task.worktree, workdir=task.workdir)
    wall, silence, think = limit(task, "round"), limit(task, "silence"), limit(task, "think")
    # A shell command OpenCode moved to the background (past 2 minutes it does, and the turn
    # ends waiting on it) prints nothing until it is done: a long test suite counted as thinking,
    # was nudged and killed every 7 minutes, and rerun (the 2026-11-16 audit). While it waits
    # (`background_wait`), no nudge, and silence runs to the test timeout; the round's limit holds.
    wait_hush = max(silence, float(spec.get("test_timeout") or 900))
    turn_over = False  # the last step finished with reason `stop`: the agent's turn ended (#658)
    bg_jobs: list[str] = []  # what OpenCode waits on once the turn is over
    nudged = False
    renudge = renudged(st, n)
    edit_mark = gitops.mark(task.worktree)  # shell edits (sed -i, cat >) count as edits too
    started = time.time()  # the wall clock, for check-ins: they compare with timestamps in files
    clock = _Clock(time.monotonic())
    provider = _ProviderWait(task, n)  # an API provider's rate limit, outage or queue (#1020)
    prior = float(task.status.get("active_s") or 0)  # earlier rounds' active work
    # what the ETA and the progress checks compare: this step's alone (#815)
    step_prior = prior - float(task.status.get("step_active_s") or 0)
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
    # The last step's (session, context): a later step in it carrying under half of one that was
    # near the handover point is OpenCode's compaction, which `run --format json` doesn't print.
    # One tool result bigger than the margin before it (a long file, a test log) compacted the
    # session between two steps, and the agent worked on from its summary (#774).
    last_step: tuple = (None, 0)
    # The tool output since that step, in tokens (bytes / CHARS_PER_TOKEN, OpenCode's own
    # estimate): OpenCode counts it in before the next request, and six parallel 50 KB results
    # from a step at 40K compacted the session with no step near the point (#1086, 2026-11-23)
    pending = 0
    opened: dict = {}  # the questions open this round (ask_mcp.relay)
    spend = _spend_round(task, n)  # an API task's meter (#1019), else None
    warmed = time.monotonic()  # the model's keep-alive, renewed while the round waits (#864)
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
        clock.step(mono, asking, provider.waiting(now))
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
            provider.answered()
        # The line's event, when it is one (0 or 1 of them): a line that isn't (a stray print, a
        # number or list) or an event normalize drops skips only this body, never the clocks and
        # the steer check below. Those `continue`d past them, and a stream of unknown progress
        # events ran past the round's limit, ignored `wb steer --now` and held the GPU lease
        # (#195, the 2026-11-19 audit).
        for raw in got:
            if raw.get("type") == "background_wait":
                waiting_bg = True
                bg_jobs = [str(j.get("title") or j.get("id") or "a job")
                           for j in raw.get("jobs") or () if isinstance(j, dict)]
            elif raw.get("type") == "step_finish":
                turn_over = (raw.get("part") or {}).get("reason") == "stop"
            elif raw.get("type") not in ("retry", None):
                turn_over = False
            if raw.get("sessionID") and not task.status.get("session"):
                task.set_status(session=raw["sessionID"])
            row = normalize(raw, n)
            if row is None:
                continue
            if row["kind"] != "retry":
                waiting_bg = False  # the session moved on
            sig = row.pop("sig", None)
            if row["kind"] == "tool":
                state = (raw.get("part") or {}).get("state") if isinstance(raw.get("part"), dict) else None
                if isinstance(state, dict) and isinstance(state.get("output"), str):
                    pending += len(state["output"]) // context.CHARS_PER_TOKEN
                out["tools"][row["tool"]] = out["tools"].get(row["tool"], 0) + 1
                if not row["ok"]:
                    out["errors"].append(f"{row['tool']}: {row.get('error', '')}")
                st = task.status
                used = context.experiment_use(task, row)
                task.set_status(last=f"{row['tool']} {row['brief']}".strip(),
                                tools=st.get("tools", 0) + 1,
                                # what the agent really used, live (#857): wb status, the row
                                **({"experiment_use": {**(st.get("experiment_use") or {}),
                                                       **{u: (st.get("experiment_use") or {}).get(u, 0) + 1
                                                          for u in used}}} if used else {}),
                                **({"long_reads": st.get("long_reads", 0) + 1}
                                   if context.long_read(task, row) else {}))
            elif row["kind"] == "text":
                out["texts"].append(row["text"])
                row["text"] = row["text"][:2000]
            elif row["kind"] == "step":
                if spend:
                    spend.step(raw.get("part") or {})  # the ledger, before the guard below
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
                sid = raw.get("sessionID") or task.status.get("session")
                before = last_step[1] + pending  # what OpenCode had when it compacted
                if window and compacted_between(last_step, sid, used, fresh_at * window, window, pending):
                    changed = marks != {gitops.mark(task.worktree)}
                    if changed and floor is None:  # it changed nothing by the step before
                        floor = last_step[1]
                        task.set_status(session_floor=floor)
                    # and not on its turn's last step (#1151): the agent is done, so the event,
                    # and fresh_reason (the export counts the compaction) starts any next round fresh
                    if changed and not keep and not out["full"] and not turn_over \
                            and fresh_at * window - floor >= HANDOVER_ROOM * window:
                        out["full"], out["compacted"] = min(before, window), True
                    else:
                        task.event({"ts": store.now(), "round": n, "kind": "compacted",
                                    "text": f"OpenCode compacted the agent's session mid-round at "
                                            f"{min(100, 100 * before // window)}% of its window: "
                                            + ("its turn ended there" if turn_over else
                                               "it works on from its own summary")})
                if used:
                    last_step, pending = (sid, used), 0
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
                        # Not on the step that ended the agent's turn (reason `stop`): it is done,
                        # and a handover there killed it and paid a fresh session to redo the
                        # round, whose summary Claude got instead (#1151, 2026-11-24). The round
                        # ends as it would; fresh_reason starts any next one fresh.
                        if not turn_over:
                            out["full"] = used
                    elif not out.get("reading_full"):
                        out["reading_full"] = used
                        _reading_full(task, n, used, window, floor)
            elif row["kind"] == "error":
                out["errors"].append(f"opencode: {row['text']}")
                provider.on_error(raw)
                if compaction_failed(raw):
                    # OpenCode's compaction failed ("Compaction produced no summary"): the session
                    # is over its window and can't go on, so a fresh one takes over, whether or
                    # not a file has changed and even under --keep-context; HANDOVER_MAX still
                    # bounds it. Before, OpenCode exited 1 and the task failed (a bench replay
                    # whose agent read a long test file whole), or exited 0 and auto-fix sent the
                    # failing tests back to the dead session (2026-11-21, #969).
                    out["compact_failed"] = row["text"]
                    # the dead session, named: past HANDOVER_MAX it stays the task's `session`,
                    # and wb feedback / wb next clear `context`, so fresh_reason knows it by this
                    # (the export counts only completed compactions), never continuing it (#969)
                    task.set_status(compact_failed_session=raw.get("sessionID") or task.status.get("session"))
                    if not out["full"]:  # the context it carried: its last step's, else all of it
                        out["full"], out["compacted"] = last_step[1] or window or 1, True
            elif row["kind"] == "retry":
                out["retries"].append(row["text"])
                provider.on_retry(raw, now)
                task.set_status(last=f"OpenCode retries a request: {row['text']}"[:200])
            task.event(row)
            why = detector.feed({**row, "sig": sig}) if sig else None
            if why and detector.since_edit >= detector.no_edit:
                if (m := gitops.mark(task.worktree)) != edit_mark:  # it edited through the shell
                    edit_mark, detector.since_edit, why = m, 0, None
                elif m == began and task.status.get("round_for") == "self_review":
                    # A self-review round is reads and runs by design (break a check, see a test
                    # fail, copy the file back): on a change whose tests passed, the explore
                    # guard stopped it as stuck, so a green task woke Claude as stuck and its
                    # step was recorded stuck (#1168, 2026-11-24). Its tree as the round began,
                    # it ends as the agent's finish: the tests, then review. A tree it changed
                    # is stopped as before.
                    out["settled"], why = why, None
            if why:
                out["stuck"], out["guard"] = why, detector.guard
        if out.get("settled"):  # #1168: not a guard's stop
            task.event({"ts": store.now(), "round": n, "kind": "settled",
                        "text": f"self-review: {out['settled']}, the tree as the round began: "
                                "stopped as its finish"})
            _stop(proc)
            break
        quiet = clock.quiet(mono)
        hush = wait_hush if waiting_bg else silence
        # The agent's turn is over (its last step's reason `stop`) and OpenCode is still open:
        # past BG_GRACE with a job in the background, or when the round's wall, silence or think
        # guard comes due, the round ends as the agent's finish, never a guard's stop. Only the
        # job's grace looked at the turn, so the wall, due 85 s after an agent's "Work is
        # complete", stopped it as stuck: no tests, a false stuck notice, and a guard in the
        # step's results row (#1160, 2026-11-24). An agent still working is stopped as before.
        done = turn_over and not out["stuck"] and not asking and (
            (waiting_bg and quiet > min(BG_GRACE, silence)) or clock.ran(mono) > wall
            or quiet > hush or (think and heard and not waiting_bg and quiet > think))
        if done and waiting_bg:
            # the agent finished and left a job running (a dev server): the round is done (#658)
            out["left_bg"] = bg_jobs or ["a background job"]
            task.event({"ts": store.now(), "round": n, "kind": "left_bg",  # not a guard's stop
                        "text": "the agent's turn ended with " + ", ".join(out["left_bg"])
                                + " still running in the background: stopped"})
            _stop(proc)
            break
        if done:  # nothing in the background it says: OpenCode stayed open, stopped (#1160)
            out["lingered"] = True  # our stop's exit, not a crash, as below (#83)
            task.event({"ts": store.now(), "round": n, "kind": "lingered",  # not a guard's stop
                        "text": f"the agent's turn ended {_span(quiet)} ago and OpenCode was "
                                "still open: stopped"})
            _stop(proc)
            break
        if not out["stuck"] and (why := provider.stop(now)):  # before silence: never "no output"
            out["guard"], out["stuck"] = why
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
                    task.status.get("checkin_at") or 0)), step_prior + clock.ran(mono)) + f"\nthe runner nudged it: {mins} min of "
                    f"thinking with no tool call (nudge {nudges} this task; budget {int(think)}s)",
                    round=n)
        if not out["stuck"] and spend:
            _spend_guard(task, out)
        if out["stuck"]:
            # by: the provider's or the spend cap's stop is no lesson about the agent
            # (lessons.gather; #1020)
            task.event({"ts": store.now(), "round": n, "kind": "stopped", "text": out["stuck"],
                        **({"by": by} if (by := stopped_by(out["guard"])) else {})})
            _stop(proc)
            break
        if out["full"]:
            _stop(proc)
            break
        # an API model has no keep-alive of ours to renew (#1015)
        if (waiting_bg or asking) and mono - warmed >= KEEP_WARM_S and not CURRENT.get("api"):
            warmed = mono  # off the loop: a server slow to answer holds no clock or steer up
            threading.Thread(target=_keep_warm, args=(task.spec.get("model"),), daemon=True).start()
        every = float(spec.get("checkin") or 0) * 60
        since = max(started, float(task.status.get("checkin_at") or 0))
        active = step_prior + clock.ran(mono)
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
    provider.end(out, time.time())
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


def measures_power(task: store.Task) -> bool:
    """Whether a round samples this GPU's draw (_Power): not for a remote model (another machine's
    GPU), the suite's fakes (the GPU isn't theirs), or an API task, which runs on no GPU of ours:
    its watt-hours would be whatever else the GPU did meanwhile (#1022)."""
    return not CURRENT.get("remote") and not CURRENT.get("api") and not models._fake() \
        and task.spec.get("via") != "api"  # CURRENT["api"]: the model's kind, for a task before #1016's via


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
    auto-fix round (#196, the 2026-11-19 audit). Run it with `shell_args`, as OpenCode does."""
    sh = os.environ.get("SHELL") or ""
    if os.path.isabs(sh) and os.access(sh, os.X_OK) and Path(sh).name not in NOT_POSIX:
        return sh
    return shutil.which("bash") or "/bin/sh"


def shell_args(shell: str) -> list[str]:
    """The flags before the command, as OpenCode 2.3.2 picks them by the shell's name (its
    lowercased basename): zsh's `+o nomatch -c`, else `-c`. With a bare `-c` under zsh an
    unmatched glob (`pip install -e .[dev]`, a pytest id `test_x[1]`) failed "no matches found"
    for the setup, the base check and the tests while it passed for the agent: a task failed
    before round 1, or an auto-fix round was spent on a false FAIL (#1170, 2026-11-24; the
    #1010 review had fixed only the agent's shim)."""
    return ["+o", "nomatch", "-c"] if Path(shell).name.lower() == "zsh" else ["-c"]


# The variables OpenCode's environment moves into the task's home (context.opencode_home_env),
# which the shell shim gives back to the agent's commands as the user has them
SHIM_RESTORES = ("XDG_DATA_HOME", "XDG_STATE_HOME", "OPENCODE_DB")
# rules/<task>/shell/, out of the lane and off the agent's PATH. The shim is named after the
# shell it runs: OpenCode picks its flags by $SHELL's name (zsh's `+o nomatch -c`, the PTY's
# `-l`, the rc file, the tool's "Commands run … using <name>"), and a shim called opencode-shell
# matched none, so under zsh `pip install -e .[dev]` failed "no matches found" (the #1010
# review, 2026-11-22)
SHELL_SHIM_DIR = "shell"


def write_shell_shim(task: store.Task) -> Path:
    """`rules/<task>/shell/<the shell's name>`, OpenCode's $SHELL: OpenCode runs each command as
    `$SHELL -c`, and its own XDG_DATA_HOME and XDG_STATE_HOME (the task's home, #1010) would
    move every other tool's data too (uv's Pythons, pipx, poetry, pnpm). POSIX sh, the user's
    values baked in: each put back (or unset, where the user had none), SHELL back to the
    user's, then the shell test_shell picks, as the tests run in, with OpenCode's arguments.
    Paths, never a secret: the lane can read it."""
    q = shlex.quote
    shell = test_shell()
    lines = ["#!/bin/sh", "# workbench: the agent's commands get the user's own data and state "
             "directories back (#1010)"]
    for name in (*SHIM_RESTORES, "SHELL"):
        value = os.environ.get(name)
        lines.append(f"{name}={q(value)}; export {name}" if value is not None else f"unset {name}")
    lines.append(f'exec {q(shell)} "$@"')
    folder = context.rules_dir(task) / SHELL_SHIM_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / Path(shell).name
    tmp = folder / f".shim.{os.getpid()}"
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    return path


def lane_ports(task: store.Task) -> list[int]:
    """The TCP ports the task's lane refuses (context.deny_ports), less those its round needs:
    its model server's and its keyhole's (#1197), so a model served on one of them still works
    with no grant."""
    m, kh = CURRENT.get("model"), keyhole_of(task)
    return context.deny_ports(task, keep=[context.url_port(m.base_url) if m else None,
                                          kh.port if kh else None])


def run_in_lane(task: store.Task, cmd: str, limit: int) -> tuple[int | str, str]:
    """Run `cmd` in the task's worktree, in the agent's lane and with its environment; the
    exit code ("timeout" past `limit` seconds, "output limit" past TEST_OUTPUT_CAP bytes) and the
    decoded output (its head and tail when long)."""
    # in the agent's lane, environment and shell: the tests run the agent's code, and must pass
    # or fail as they did when the agent ran them
    sh = test_shell()
    argv = lane.wrap(context.writable(task), [sh, *shell_args(sh), cmd], context.readable(task),
                     context.unix_paths(task), lane_ports(task))
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


# A pty the lane refused, which names no path: Python's pty.openpty and pexpect ("out of pty
# devices"), node-pty and others; the grant is /dev/pts (#829, the second 2026-11-21 audit)
PTY_REFUSED = re.compile(r"out of pty devices|failed to create pseudo-?terminal|forkpty\(3\) failed",
                         re.I)
# A refusal in a test's output: the phrase, and the absolute paths on its line. Not a URL's path
# (`https://h/x`), nor one inside a word.
REFUSAL = re.compile(r"permission denied|operation not permitted|"
                     # Bundler's PermissionError and RubyGems' FilePermissionError, which say
                     # neither (#832): "while trying to write to `…`", "write permissions for"
                     r"error while trying to (?:write to|create)|write permissions for", re.I)
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
    `wb allow --path` for each. Only a refusal the lane made: without Landlock (macOS, a kernel
    without it) there is none, and a path the runner, outside the lane, may not use either is the
    user's own permissions (#773, the second 2026-11-21 audit: a test's Permission denied on
    /usr/share skipped auto-fix and woke Claude with a grant that couldn't help)."""
    places = context.writable(task)
    if places is None or not lane.abi():
        return []
    guard = context.repo_dirs(task)  # the user's checkout: no lane may write it
    # From ABI 9 a socket made outside the lane is refused even inside a writable place: the
    # user's Postgres or MySQL socket in /tmp read as a broken database, no grant named (#826)
    sockets = context.unix_paths(task) if lane.abi() >= 9 else None
    hidden = context.api_hidden(task)  # once: each path's check reads every token file
    found = []
    for line in output[-200_000:].splitlines():
        if PTY_REFUSED.search(line) and "/dev/pts" not in found and "/dev/pts" not in places:
            found.append("/dev/pts")  # no path, no "permission denied" (#829)
        if not REFUSAL.search(line):
            continue
        for path in refused_paths(line):
            real = os.path.realpath(path)
            if any(real == h or context._under(real, h) for h in hidden):
                if path not in found and runner_may(path):
                    found.append(path)  # named as hidden, with no grant (#1093)
                continue
            grant = lane_grant(path, hidden=hidden)
            if path not in found and grant and (not any(
                    path == w or path.startswith(str(w).rstrip("/") + "/") for w in places)
                    or sockets is not None and is_socket(path) and not any(
                        path == u or context._under(path, u) for u in sockets)) \
                    and not any(grant == g or context._under(grant, g) for g in guard) \
                    and runner_may(path):
                found.append(path)
    return found[:5]


def runner_may(path: str) -> bool:
    """Whether this process, outside the lane, may use `path`: read or write it when it exists,
    else make it in the nearest directory that does (#773)."""
    real = os.path.realpath(path)
    if os.path.exists(real):
        return os.access(real, os.R_OK) or os.access(real, os.W_OK)
    if real.startswith("/dev/"):  # a pty index gone by now (#829): the kernel's, not a mode's
        return True
    while not os.path.exists(real):
        real = os.path.dirname(real)
    return os.access(real, os.W_OK | os.X_OK)


def is_socket(path: str) -> bool:
    """Whether ``path`` is a UNIX socket (followed: a link to one is one)."""
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def lane_grant(path: str, task: store.Task | None = None, hidden: set[str] | None = None) -> str | None:
    """The `wb allow --path` that reaches a refused path: /dev/pts for a pty, itself when it
    exists (a directory, a device node, a socket) or is a device node, else the nearest directory that does; the
    toolchain home it falls in when that isn't made yet, or the one below it (`~/go`: `go/pkg`,
    context.tool_home_of, #824: the walk reached the home directory), or else the RubyGems
    home it is in (context.gem_home_of, #832); then, while that place holds what the lane is split around
    (context.split_points: a grant of it would be split again), the next directory down
    towards the path. None when no grant can (`/`, the home directory, a secret:
    context.lane_refusal; for ``task``, a file its API lane hides: context.api_hidden, #1093, or
    ``hidden``, those files already read)."""
    target = real = os.path.realpath(path)
    hidden = hidden if hidden is not None else context.api_hidden(task) if task else set()
    if any(target == h or context._under(target, h) for h in hidden):
        return None  # hidden over any grant (#1093)
    if real == "/dev/pts" or real.startswith("/dev/pts/"):
        return "/dev/pts"  # every pty, not the one index this run got (#829)
    while not os.path.exists(real) and not real.startswith("/dev/"):  # a node: itself, or /dev
        real = os.path.dirname(real)
    if (home := context.tool_home_of(target)) and (not os.path.exists(home)
                                                   or context._under(home, target)):
        real = home  # made by wb allow --path (cli._lane_dir), then in the lane (#824)
    elif gems := context.gem_home_of(target):
        real = gems  # the whole gem home, which `bundle install` writes all over (#832)
    if context.lane_refusal(real):
        return None
    if real in context.secret_sockets():
        return real  # Docker Desktop's, in .docker: connected to alone (#441)
    out = context.split_points() | hidden
    while real != target and context._under(target, real) and \
            any(context._under(h, real) for h in out):
        real = os.path.join(real, target[len(real):].strip("/").split("/")[0])
    if real in out or any(context._under(real, h) for h in out):
        return None  # what the lane keeps out: ~/.cargo/bin, a secret
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
    if code == "timeout" and (stalled := hang(task, limit)):  # #779
        result["hang"] = stalled
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


PREPARED = "prepared.json"


def prepared_record(task) -> dict:
    """What the setup left, for the scratch checkouts: {paths, marks, bytes} from the task's
    prepared.json, else from status (a task prepared before the file, or a stand-in with status
    alone). In status.json, an in-tree C build's one entry per object file was megabytes, parsed
    and rewritten on every tool call (#867)."""
    d = getattr(task, "dir", None)
    try:
        got = store.read_json(d / PREPARED) if d is not None else None
    except OSError:
        got = None
    if isinstance(got, dict):
        return got
    st = task.status
    return {"paths": st.get("prepared_paths"), "marks": st.get("prepared_marks"),
            "bytes": st.get("prepared_bytes")}


# paths given to one cp at a time: well under any ARG_MAX, with the paths' own lengths
COPY_BATCH = 500


def _copy_all(src: Path, dest: Path, paths: list[str]) -> None:
    """Each of ``paths`` from under ``src`` to the same place under ``dest``, as _copy does, in
    one GNU cp a batch (`--parents`): one cp per path forked tens of thousands of times for an
    in-tree C build's object files before the base check ran (#867). A batch that fails (macOS's
    or busybox's cp, a read error) is copied path by path."""
    for i in range(0, len(paths), COPY_BATCH):
        batch = paths[i:i + COPY_BATCH]
        if len(batch) > 1 and sys.platform != "darwin":
            try:
                if subprocess.run(["cp", "-a", "--reflink=auto", "--parents", "-t", str(dest), "--", *batch],
                                  cwd=src, capture_output=True).returncode == 0:
                    continue
            except OSError:
                pass
        for p in batch:
            _copy(src / p, dest / p)  # what a failed batch left is replaced


def _reflinks(at: Path) -> bool:
    """Whether a copy made in ``at`` can be a clone that takes no space (btrfs, XFS, APFS):
    one small file cloned there, or not."""
    argv = ["cp", "-c"] if sys.platform == "darwin" else ["cp", "--reflink=always"]
    try:
        with tempfile.TemporaryDirectory(dir=at) as d:
            Path(d, "a").write_bytes(b"x" * 4096)
            return subprocess.run([*argv, str(Path(d, "a")), str(Path(d, "b"))],
                                  capture_output=True).returncode == 0
    except OSError:
        return False


def _no_room_for(task: store.Task, wt: Path) -> str | None:
    """Why the setup's work (prepared.json `bytes`) can't be copied into the scratch checkout
    ``wt`` without filling the disk, or None. wb start counted only the checkout, measured before
    any setup ran: a 12 GB target/ copied on ext4 filled the disk twice over (cp, then copytree),
    and every other open task's status write failed meanwhile (#860). Only freeing space helps:
    the scratch running the setup itself (wb verify --setup) writes about as much, with no check,
    and this line once offered that (the review of #860)."""
    need = int(prepared_record(task).get("bytes") or 0)
    free = store.free_bytes(wt)
    if not need or free is None or free >= need + CHECKOUT_SPARE or _reflinks(wt.parent):
        return None
    return (f"the setup's work in the worktree ({store.gb(need)}: its build output, installs and "
            f"copies) doesn't fit in a scratch copy: {store.gb(free)} free where the state directory "
            f"is, 2 GB kept spare. Free {store.gb(need + CHECKOUT_SPARE - free)} (another task's "
            "build, wb gc) and run it again")


def prepare(task: store.Task, prepared: Path | None = None) -> tuple[int | str, str]:
    """Make `task.worktree` the project as the user has it, before anything runs there: the
    links beside it to the repo's `siblings`, the ignored paths to `copy` from the user's
    checkout, then the `setup` command in the lane (#201, the 2026-11-19 audit: a worktree holds
    only committed files, so a check that needed node_modules, a .venv or ../lib failed on the
    base and in every round, for that and not for the step). The setup's exit code and output;
    (0, "") with nothing to do. A scratch checkout passes the task's ``prepared`` worktree: the
    ignored paths its preparation left (prepared.json `paths`, the setup's work and the copies)
    are copied from there, and the setup isn't run again. The base check, wb verify (twice) and
    wb shot each ran it from nothing, up to SETUP_SECS: a first build compiled three to five
    times a task (#249, the audit of 1.2)."""
    spec, wt = task.spec, task.worktree
    repo = Path(spec["repo"])
    record = prepared_record(task) if prepared else {}
    made = record.get("paths") if prepared else None
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
    # audit). Recorded at setup (prepared.json `marks`); a task prepared before them copies as before.
    if made and (marks := record.get("marks")) and \
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
            if why := _no_room_for(task, wt):
                return "copy failed", why
            todo = []
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
                todo.append(p)
            _copy_all(prepared, wt, todo)
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


def agent_wrote(task: store.Task) -> list[str]:
    """The files the agent wrote with its edit tools, relative to the worktree, from events.jsonl:
    what an ignored path would hide from the diff and the commit (gitops.ignored_files). A patch
    row's every file too (`paths`, #1148); a relative path is the workdir's, as OpenCode resolves
    it against its directory, the package in a monorepo."""
    root = os.path.realpath(task.worktree)
    base = os.path.realpath(task.workdir)
    out = set()
    for r in store.read_jsonl(task.dir / "events.jsonl")[0]:
        if r.get("kind") == "tool" and r.get("ok") and r.get("tool") in EDIT_TOOLS:
            for p in written(r):
                full = os.path.realpath(os.path.join(base, p))
                if full.startswith(root + os.sep):
                    out.add(os.path.relpath(full, root))
    return sorted(out)


def hand_wrote(task: store.Task) -> list[str]:
    """The ignored files written by hand in the worktree while no runner ran (gitops.hand_written):
    Claude's after a takeover or in a review fix, which the agent's events never show (#1179,
    2026-11-24). Kept in status `hand_wrote` by each runner that starts, and since the agent last
    stopped (`hands_off_at`)."""
    return _by_hand(task)[0]


def hand_left(task: store.Task) -> list[str]:
    """The rest of the ignored files changed while no runner ran, outside a cache, that
    hand_written took for a build's or a test run's: the commit leaves them out, said (#1179's
    review, 2026-11-24). Kept in status `hand_left` as `hand_wrote` is; only those still there."""
    return [p for p in _by_hand(task)[1] if os.path.isfile(os.path.join(task.worktree, p))]


def _by_hand(task: store.Task) -> tuple[list[str], list[str]]:
    wrote, left = set(task.status.get("hand_wrote") or []), set(task.status.get("hand_left") or [])
    if since := task.status.get("hands_off_at"):
        rest: list[str] = []
        wrote |= set(gitops.hand_written(task.worktree, since, rest))
        left |= set(rest)
    return sorted(wrote), sorted(left - wrote)


def hands_off(task: store.Task) -> None:
    """The agent has stopped: from now the worktree's ignored files are Claude's to write, unless
    already so (a stop of a runner that had ended keeps the earlier time, #1179)."""
    task.set_status_if(lambda st: not st.get("hands_off_at"), hands_off_at=store.now())


def _hands_on(task: store.Task) -> None:
    """Before the agent's rounds: what was written by hand since it last stopped, kept in
    `hand_wrote`, as the agent's own writes from now on aren't Claude's (#1179)."""
    if not task.status.get("hands_off_at"):
        return
    try:
        wrote, left = _by_hand(task)
    except (gitops.GitError, OSError):
        wrote, left = task.status.get("hand_wrote") or [], task.status.get("hand_left") or []
    task.set_status(hand_wrote=wrote, hand_left=left, hands_off_at=None)


def ignored_writes(task: store.Task) -> list[str]:
    """The files the agent wrote that git ignores, so no diff or commit holds them; [] when git
    can't say."""
    try:
        return gitops.ignored_files(task.worktree, agent_wrote(task))
    except (gitops.GitError, OSError):
        return []


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
# lane reads: prepare-<n>.lock the slots, prepare.json the queue. Setups and base checks queue
# apart (prepare-check-<n>.lock, prepare-check.json): one queue made a 5-second base check, held
# to 120 s, wait out another task's 25-minute build, with the GPU idle (#868). Two base checks
# (two test suites, a fixed port) still never run together, nor two setups.
PREP_SLOTS = 1
PREP_KINDS = {"setup": ("prepare", "setup"), "check": ("prepare-check", "base check")}


def _prep_queue(kind: str = "setup") -> Path:
    return store.home() / "locks" / f"{PREP_KINDS[kind][0]}.json"


@contextlib.contextmanager
def prep_turn(task: store.Task, kind: str = "setup"):
    """Hold one of PREP_SLOTS for a setup or (``kind`` "check") a base check, waiting behind the
    tasks queued for the same before this one (status `last` names the one it waits for). A
    holder that died lets go: the kernel drops its flock, and its queue entry is passed over
    once its pid is gone."""
    import fcntl
    path = _prep_queue(kind)
    stem, what = PREP_KINDS[kind]
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
                    f = open(path.with_name(f"{stem}-{i}.lock"), "a+")
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
                    task.set_status(phase="queued", last=f"waiting for {ahead}'s {what} "
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
        # what each scratch checkout's copy of them takes, checked against the free space (#860)
        size = sum(store.dir_size(task.worktree / p, budget=10)[0] for p in made) if made else 0
        # a file of its own, not status.json, which every status write rewrites whole (#867)
        store.write_json(task.dir / PREPARED, {"paths": made, "marks": marks, "bytes": size})
        task.set_status(prepared=True, setup_secs=secs)
        return True
    what = f"the setup command failed (exit {code})" if isinstance(code, int) else \
        f"setting up the worktree failed ({setup_timed_out(spec) if code == 'timeout' else code})"
    tail = "\n".join("    " + x for x in clip_tail(output, 8, total=1500).splitlines())
    proxy = f"\n{proxy_line(gone)}, if the setup needed it" if (gone := proxy_gap(output)) else ""
    # what the lane refused, as a test run's report names it: a first `npm ci` failed on a
    # ~/.npm not made yet, and the notice named no grant (#824)
    proxy += "".join(f"\nthe lane refused {shlex.quote(n)}: {context.API_HIDDEN_WHY}"
                     if context.api_hidden_refusal(task, n) else  # (#1093)
                     f"\nthe lane refused {shlex.quote(n)}: wb allow {task.name} --path "
                     f"{shlex.quote(lane_grant(n, task) or n)}, if the setup needs it"
                     for n in lane_refused(task, output))
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
    timed: list[float] = []  # its scratch checkout and run, not the waits for the GPU or a turn (#779)
    code, output = verify.on_base(task, cmd, limit, say=lambda msg: task.set_status(last=msg),
                                  turn=lambda: _timed(prep_turn(task, "check"), timed))  # one at a time (#590), apart from setups (#868)
    task.dir.joinpath("base-tests.txt").write_text(
        f"$ {cmd}\nexit: {code}\nshell: {test_shell()}\n\n{output}")
    return {"ok": code == 0, "exit": code, "tail": clip_tail(output, 8, total=1500), "limit": limit,
            **({"secs": round(timed[0], 1), "cmd": cmd} if timed else {})}


@contextlib.contextmanager
def _timed(inner, into: list):
    """``inner`` (a context manager), with the seconds spent inside it appended to ``into``."""
    with inner:
        began = time.monotonic()
        try:
            yield
        finally:
            into.append(time.monotonic() - began)


# A base check that passed in under this share of --test-timeout says the whole suite is fast: a
# round's run past the limit is the change's hang (an infinite loop, a blocking read, a server
# never shut down), and goes back to the agent. Skipped as "the suite may be slow", each one cost
# a 15-minute run and a wake that auto-fix could have handled (#779).
HANG_SHARE = 0.25


def hang(task: store.Task, limit: int) -> dict | None:
    """{"base", "limit"} when a round's run past ``limit`` is the change's hang: the base check
    ran this same command to exit 0 in under HANG_SHARE of it. None when it timed out or failed
    there, or was never checked: then the suite may well be slow."""
    base = task.status.get("base_check") or {}
    secs = base.get("secs")
    if base.get("ok") and secs is not None and base.get("cmd") == task.spec.get("test_cmd") \
            and secs <= HANG_SHARE * limit:
        return {"base": secs, "limit": limit}
    return None


# The shell's own line for a command it can't find on PATH, and the command it names: bash's
# `<name>: command not found`, zsh's `command not found: <name>`, dash's and ksh's `<name>: not
# found` (exit 127). Not `No such file or directory`, which a script the step creates
# (`./check.sh`) says on the base too in bash and zsh; dash says `./check.sh: not found` for it,
# so a name with a `/` is never a missing tool (nightly run 45362198509, 2026-11-24: under a
# dash $SHELL a --test the step was to make checked in at once as a tool the agent lacks).
NOT_FOUND = re.compile(r"command not found: (?P<z>.+)$|(?P<n>[^:]+): (?:command )?not found$")
# Python's own line for `python -m <module>` it can't import (exit 1): a test runner missing from
# the agent's Python (`python3 -m pytest` on a system Python without it, or with the user's venv
# that python_env leaves out) ran round 1 and both auto-fix rounds on correct code, each told the
# bug was the change's, where `pytest` alone (exit 127) checked in (#1181, 2026-11-24). A dotted
# module's parent missing says it the second way. `m` (or `d`) is the module `-m` named.
NO_MODULE = re.compile(r"\S*python[\w.]*: (No module named (?P<m>[\w.]+)|Error while finding module "
                       r"specification for '(?P<d>[\w.]+)' \(ModuleNotFoundError: No module named "
                       r"'[\w.]+'\))")
# What gives the agent's environment that command, for the check-in and the skipped auto-fix
GIVE_TOOL = ("no round can fix that: put the tool in the worktree (a .venv there comes first on "
             "the agent's PATH; a Python module: python3 -m venv .venv && .venv/bin/pip install "
             "<module>), then wb feedback; or wb discard and start again with a --setup that "
             "installs it (a module: that .venv), or a --test that names it by its path")


def missing_command(r: dict | None, task: store.Task | None = None) -> str | None:
    """The line saying the command's tool isn't there, when run ``r`` (the base check's, or a
    round's tests) ended with it: at exit 127 the shell's not-found line, worded by wb as
    `<name>: command not found`, so the check-in and the base's line compared with a round's
    read the same under bash, zsh, dash or ksh (#1152; nightly run 45362198509, 2026-11-24), a
    name with a `/` never; or Python's no-module line for `-m` at exit 1 (#1181). A tool the
    agent's environment lacks (one outside the agent's PATH, which python_env and agent_path
    trim), which no edit in a round can give it; else None. A module of ``task``'s own code
    (own_module) is none: the step may make it."""
    if not r or r.get("exit") not in (1, 127):
        return None
    last = (r.get("tail") or "").strip().splitlines()[-1:]
    line = last[0].strip() if last else ""
    if r["exit"] == 127:
        name = missing_name(line)
        return f"{name}: command not found" if name and "/" not in name else None
    if r["exit"] == 1 and (m := NO_MODULE.fullmatch(line)):
        return None if task and own_module(task, m["m"] or m["d"]) else line
    return None


def _missing_key(line: str | None) -> str | None:
    """What a missing_command line says is missing, for the base's and a round's to compare: a
    Python module's name, as the line starts with the interpreter's path, the base check's in its
    scratch checkout (a --setup .venv there) and a round's in the worktree, which never matched,
    and auto-fix ran on correct code (#1181's review, 2026-11-24); else the line itself."""
    m = NO_MODULE.fullmatch(line or "")
    return f"module {m['m'] or m['d']}" if m else line


# Where a prompt's spec ends and the protocol begins (cli.PROTOCOL, cli.WRITING_PROTOCOL; bench.FOLD).
PROTOCOL_FOLD = "\n---\nHow to work"


def own_module(task: store.Task, module: str) -> bool:
    """Whether ``module`` (`pkg.newcli`) is the repo's own code, which the step may be making:
    its top-level name a `<top>/` or `<top>.py` where the tests run, in the worktree or at the
    base commit, or the module named in the step's spec (its file, or its dotted name; the spec
    alone, not the protocol after it, whose acceptance line names --test's own). Counted
    as a missing tool, `--test 'python3 -m pkg.newcli'` for a step adding pkg/newcli.py checked
    in before round 1 with a false "no round can fix that" (and its pip install advice), and
    skipped the agent's one auto-fix round, where a script the step makes was already left out
    for saying `No such file or directory` on the base too (#1152) (#1181's review, 2026-11-24)."""
    top, sub = module.split(".")[0], task.spec.get("subdir", "")
    names = [top, f"{top}.py"]
    if (task.workdir / top).is_dir() or (task.workdir / f"{top}.py").is_file():
        return True
    try:
        if gitops.git(task.worktree, "ls-tree", "--name-only", task.spec["base"], "--",
                      *(f"{sub}/{n}" if sub else n for n in names)).strip():
            return True
    except (gitops.GitError, KeyError, OSError):
        pass
    try:
        spec = current_task_prompt(task)
    except OSError:
        return False
    # the spec alone: the protocol after it names the acceptance command (cli.test_line), so
    # every dotted `-m` of --test read as the repo's own, and a tool the agent's environment
    # lacks never checked in nor skipped auto-fix (#1181's second review, 2026-11-24)
    spec = re.sub(r"The acceptance command is `[^`\n]*`", "", spec.split(PROTOCOL_FOLD, 1)[0])
    path = module.replace(".", "/")
    if any(f"/{p}".endswith(f"/{path}.py") or f"/{path}/" in f"/{p}"
           for p in context.spec_paths(spec)):  # pkg/newcli.py, pkg/newcli/__main__.py
        return True
    # a dotted name in the spec is the module's own; a top-level one (`pytest`) is a tool the
    # spec may well name
    return "." in module and re.search(rf"(?<![\w.]){re.escape(module)}(?!\w)", spec) is not None


def missing_name(line: str) -> str | None:
    """The command a not-found line names (NOT_FOUND): bash's `bash: line 1: X: command not
    found`, dash's `sh: 1: X: not found`, zsh's `zsh:1: command not found: X`, and wb's own
    `X: command not found` (missing_command's)."""
    m = NOT_FOUND.search(line)
    return (m["z"] or m["n"]).strip() if m else None


def command_words(cmd: str | None) -> set[str]:
    """The words of shell command ``cmd``, its operators split off (`cd x&&pytest`): what the
    command itself names, which no round can change (#1172)."""
    lex = shlex.shlex(cmd or "", posix=True, punctuation_chars=True)
    lex.whitespace_split = True  # `g++`, `npx:tsc` stay one word
    try:
        return set(lex)
    except ValueError:  # an unbalanced quote: the shell's own error, not a missing tool
        return set()


def base_check_line(r: dict, task: store.Task) -> str:
    """What the acceptance command did on the base, for Claude to judge (the first report)."""
    if r["exit"] == "setup failed":
        tail = "\n".join("    " + line for line in r["tail"].splitlines())
        return (f"note: setting up the base's scratch checkout failed, so the base wasn't checked "
                f"({task.dir / 'base-tests.txt'}):\n{tail}")
    if r["exit"] == "filtered":  # its checkout isn't the task's bytes: a failure there says nothing (#932)
        return f"note: the base wasn't checked: {r['tail'].strip().strip('[]')}"
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
    if line := missing_command(r, task):  # every round's tests will fail so too (#1152, 2026-11-24)
        return (f"warning: the acceptance command can't run in the agent's environment: {line} "
                f"(exit {r['exit']} on the base), so every round's tests will fail the same way; {GIVE_TOOL}")
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
    every copied node_modules on each review (#321, the audit of 1.4). Git skips ignored paths.
    An instruction file that is a link inside the worktree (wb init's AGENTS.md -> CLAUDE.md)
    pairs its proposal with the link's target, so the diff and the write go to the real file:
    paired with the link, the review diffed against nothing and --take-claude-md put a plain
    AGENTS.md over the link, leaving CLAUDE.md unchanged (#871, Claude's audit of 1.90.24). A
    link out of the worktree stays paired with the link, which `link_problem` refuses."""
    try:
        listed = gitops.git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--",
                            *(f":(glob)**/{name}.proposed" for name in PROPOSABLE))
    except gitops.GitError:  # no worktree (gone, or not a repo): nothing to settle
        return []
    out = []
    for rel in sorted(p for p in listed.split("\0") if p):
        prop = worktree / rel
        if not prop.is_symlink() and prop.is_file():
            real = prop.with_name(prop.name.removesuffix(".proposed"))
            if real.is_symlink():
                try:
                    real = worktree / real.resolve().relative_to(worktree.resolve())
                except (OSError, RuntimeError, ValueError):
                    pass  # out of the worktree, or a loop: link_problem says so
            out.append((real, prop))
    return out


def link_problem(worktree: Path) -> str | None:
    """Why the proposals can't be taken as they are, or None: two proposals for one file
    (CLAUDE.md.proposed and AGENTS.md.proposed through wb init's link), or one whose file is a
    link out of the worktree. Taking either replaced a link or kept one proposal at random (#871)."""
    seen: dict[Path, Path] = {}
    for real, prop in proposals(worktree):
        if real.is_symlink():
            return (f"{real.relative_to(worktree)} is a link out of the worktree, so no commit "
                    f"would take {prop.name}: apply {prop} by hand")
        if real in seen:
            return (f"{seen[real].name} and {prop.name} both propose "
                    f"{real.relative_to(worktree)}: delete one ({seen[real]} or {prop})")
        seen[real] = prop
    return None


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
        via = "" if prop.name == f"{real.name}.proposed" and prop.parent == real.parent else \
            f" (as {prop.relative_to(task.worktree)}, through the link)"
        parts.append(f"\n\nthe agent proposes this change to {rel}{via} (wb accept "
                     f"--take-claude-md takes it; without it, accept drops it):\n{d or '(no change)'}")
    problem = link_problem(task.worktree)
    return "".join(parts) + (f"\n\n--take-claude-md is refused: {problem}" if problem else "")


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
    gone = ""
    # Only the proposals accept settles (proposals()) are left out of the code's diff: every
    # `*.proposed` was, so an ordinary schema.proposed read as "no changes" and was committed
    # all the same (#79, the 2026-11-18 audit).
    props = [str(p.relative_to(task.worktree)) for _, p in proposals(task.worktree)]
    code = (".", *(f":(exclude,literal){p}" for p in props))
    try:
        tree, left = gitops.snapshot_left(task.worktree)  # bulky new files left out (#431)
        task.set_status(reviewed={"step": steps, "tree": tree})
        left = gitops.left_line(left)
        if since and not gitops.has_tree(task.worktree, since):
            # nothing reaches the last snapshot, and git gc pruned it: the diff against it failed,
            # and the notice dropped the whole diff without a word (#795, the second 2026-11-21
            # audit). The whole diff again, said why.
            since, gone = None, "\n\nthe last review's snapshot is gone (git gc pruned it): all of it again"
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
            return gone + "\n\nthe diff: (no changes)" + prop
        if full is not None:
            return (f"{gone}\n\nthe diff (all of it; wb diff {task.name} again only after a change):"
                    f"\n{full}" + prop)
        return (f"{gone}\n\nthe diff is over {REVIEW_DIFF_MAX} characters, too big to include: "
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
    # an API task waits on no GPU: drive's "starting" stood until here (#1112, 2026-11-23)
    task.set_status(base_check=r, last="waiting for the GPU" if _gpu_side(task) else "starting")
    # every round's tests will too: a cut-short run, or a command the agent's environment lacks
    # (#1152, 2026-11-24: round 1 and both auto-fix rounds ran first, told the bug was the change's)
    if r["exit"] == "timeout" and r["limit"] < BASE_CHECK_SECS or missing_command(r, task):
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


def _spend_part(task: store.Task) -> str:
    """The round report's money for an API task (#1022): ` · ≈$0.12 so far (catalog prices)`
    (`$0.12 so far (your prices)` at the user's own, #1135), ` · cost unknown` (no price: never a
    false $0); nothing for a GPU task."""
    if task.spec.get("via") != "api":
        return ""
    from .results import live_basis, spend_so_far
    from .savings import money
    v, b = spend_so_far(task), live_basis(task.status)
    return f" · {money(v, b)} so far{meter.NOTE[b] if v else ''}" if v is not None else " · cost unknown"


# The round report's line for a task whose agent had a copy of Claude's global files until this
# round (status `claude_rules_gone`, write_config_layer; #1119)
RULES_GONE = ("note: until this round this task's agent read {claude}/CLAUDE.md and {claude}/rules/; "
              "agents get none of Claude's files now. What it must still follow goes in your feedback; "
              "for every task, the user puts it in {mine}: tell them")


def summary_text(task: store.Task, n: int, rnd: dict, tests) -> str:
    """The compact report Claude reads; kept short on purpose, the diff is fetched separately."""
    spec, st = task.spec, task.status
    # `agent`, not `local`, for an API task's tokens alone (#1022): a GPU task's line is as it was
    # (wave 2's review)
    who = "agent" if spec.get("via") == "api" else "local"
    lines = [f"task {task.name} · {spec['model_label']} · round {n} · {rnd['secs'] / 60:.1f} min"
             f" · {who} tokens {rnd['tok_in']} in / {rnd['tok_out']} out" + _spend_part(task)]
    c = context_state(task)
    if c.get("used") and c.get("window"):
        after = {**st, "round": n + 1}  # judged as the step's next round is (#781)
        lines.append(f"agent context: {c['used'] / 1000:.1f}K of {c['window'] // 1024}K "
                     f"({100 * c['used'] // c['window']}%)"
                     + (f", compacted {c['compactions']}x" if c.get("compactions") else "")
                     + ("; the next round starts a fresh session" if fresh_reason(task, after) else
                        "; the next round carries on in this session: a fresh one would re-read "
                        "what it read and have little room to work"
                        if full_session(task, after, c) and no_room(task, after, c["window"])
                        else ""))
    full = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0]
            if e.get("kind") == "reading_full" and e.get("round") == n]
    if full:
        lines.append("reading: " + full[-1]["text"])
    lines += backend_lines(task, n)  # #1021
    if st.get("claude_rules_gone") == n:  # #1119
        lines.append(RULES_GONE.format(claude=privacy.home_path(context.claude_home()),
                                       mine=privacy.home_path(context.user_agents_md())))
    if rnd["stuck"]:
        lines.append(f"STOPPED: {rnd['stuck']}")
        if rnd.get("guard") in PROVIDER_GUARDS:
            lines.append(provider_remedy(task, rnd["guard"], str(rnd.get("stuck") or "")))
    elif rnd.get("left_bg"):  # #658: what the agent left running, stopped with it
        lines.append("the agent left " + ", ".join(f"`{j}`" for j in rnd["left_bg"])
                     + " running in the background when its turn ended; stopped")
    elif rnd.get("lingered"):  # #1160: the runner's stop, not OpenCode's exit code (#83)
        lines.append("OpenCode was still open after the agent's output ended; stopped")
    elif rnd.get("settled"):  # #1168: a self-review that read and left the tree as it was
        lines.append(f"self-review: {rnd['settled']}, the tree as the round began; stopped as "
                     "its finish")
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
            if context.api_hidden_refusal(task, node):  # no grant reaches it (#1093)
                lines.append(f"the lane refused {shlex.quote(node)}: {context.API_HIDDEN_WHY} "
                             "(not the agent's bug)")
                continue
            grant = lane_grant(node, task) or node
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
    if ignored := ignored_writes(task):
        lines.append("NOTE: " + gitops.ignored_line(ignored))
    edits, new = gitops.submodule_check(task.worktree)
    lines += [f"NOTE: {e}" for e in edits + new]  # #419, #889
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
# OpenCode's compaction failed (events.compaction_failed): the session was over its window, so
# it counts as compacted (results.step, the tuning), and the successor is told why (#969).
COMPACTION_FAILED = "OpenCode failed to compact the last session"
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
    got = export(sid, strict=True, task=task)
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


# A task started before v1.91.2 has its session in the user's own OpenCode store, which the
# agent's OpenCode, in the task's home now, can't continue (#1010): its next round hands over
# once, to a fresh session in the task's home, with the earlier one's last words (_start_fresh)
OLD_STORE = ("its session is in OpenCode's shared store, which agents no longer reach "
             "(v1.91.2)")


def old_store(st: dict) -> bool:
    """Whether the task's session began in the user's OpenCode store (status `session` with no
    `opencode_home` mark, which run_round sets)."""
    return bool(st.get("session")) and not st.get("opencode_home")


def fresh_reason(task: store.Task, st: dict | None = None) -> str | None:
    """Why round n should start a fresh session, or None to continue the current one. ``st``: the
    status to judge by, when it isn't written yet (wb next, before its one status write)."""
    st = task.status if st is None else st
    if st.get("context") == "fresh":  # even with no session yet: the prompt still needs the task
        return ASKED_FRESH
    if not st.get("session"):  # the step's first round, or one whose session never started
        first = (st.get("step_round") or 1) == st.get("round", 1)
        return None if first else "the last session ended before it began"
    if old_store(st):  # even a kept one: the agent's OpenCode can't reach it any more
        return OLD_STORE
    if st["session"] == st.get("compact_failed_session"):  # it can't go on, nor be kept (#969)
        return COMPACTION_FAILED
    if st.get("context") == "keep":
        return None
    if st.get("context") == "full":  # handed over mid-round; after OpenCode compacted it (#774)
        if st.get("full_compact_failed"):  # or failed to (#969)
            return COMPACTION_FAILED
        return COMPACTED if st.get("full_compacted") else f"the last session was {st.get('full_pct')}% full"
    c = context_state(task)
    if c.get("compactions"):
        return COMPACTED
    if full_session(task, st, c) and not no_room(task, st, c["window"]):
        return (f"the last session was {100 * c['used'] // c['window']}% full "
                f"({c['used'] / 1000:.1f}K of {c['window'] // 1024}K tokens)")
    return None


def full_session(task: store.Task, st: dict, c: dict) -> bool:
    """Whether the session's context (`context_state`) reached the handover point."""
    return bool(c.get("used") and c.get("window")
                and c["used"] >= tuning.value(task, "fresh_at") * c["window"])


def no_room(task: store.Task, st: dict, window: int) -> bool:
    """Whether a fresh session in this step would re-read what this one read before its first
    change (status `session_floor`; all of it when it changed nothing) and be left less than
    HANDOVER_ROOM to work: the mid-round rule, between rounds too. Ignored there, every auto-fix
    and feedback round on a small window paid the re-read and filled up again (#781). Not on a
    step's first round (wb next): the new step's reading is its own."""
    if (st.get("step_round") or 1) >= st.get("round", 1):
        return False
    floor = st.get("session_floor")
    return floor is None or tuning.value(task, "fresh_at") * window - floor < HANDOVER_ROOM * window


ANSWERS_MAX = 4000  # characters of Claude's answers a fresh session carries; the latest first kept


def answered(task: store.Task) -> str:
    """Claude's answers to the agent's questions in this step, as `Q: …` / `A: …` pairs, or "".
    An answer reached only the session that asked, as a tool result: a fresh one asked again,
    waking Claude twice, or, past the task's budget, was told to assume and could contradict it
    (#659). From the runner's own `ask`/`answer` events (the ask files are in the agent's lane),
    plain questions only (status `asks` "q"), never the answers nobody gave (NO_ANSWER)."""
    first = task.status.get("step_round") or 1
    kinds = task.status.get("asks") or {}
    asked, pairs = {}, []
    for e in store.read_jsonl(task.dir / "events.jsonl")[0]:
        if (e.get("round") or 0) < first or kinds.get(e.get("id")) != "q":
            continue
        text = str(e.get("text") or "").strip()
        if e.get("kind") == "ask":
            asked[e["id"]] = text
        elif e.get("kind") == "answer" and e["id"] in asked and text and \
                text not in (ask_mcp.NO_ANSWER, ask_mcp.BUDGET_USED):
            pairs.append(f"Q: {asked[e['id']][:800]}\nA: {text[:1600]}")
    kept, size = [], 0
    for pair in reversed(pairs):  # the latest answers matter most: kept first
        if size + len(pair) > ANSWERS_MAX:
            break
        kept.insert(0, pair)
        size += len(pair) + 2
    return "\n\n".join(kept)


LAST_WORDS_MAX = 2000  # characters of an earlier session's last words a fresh one carries


def last_words(task: store.Task, sid: str) -> str:
    """The session's last message to Claude (its last assistant text), exported by wb outside
    the lane (the task's home, else the user's store), or ""."""
    from .transcript import export  # transcript imports this module
    for m in reversed(export(sid, task=task).get("messages") or []):
        if isinstance(m, dict) and m.get("type") == "assistant":
            text = "\n\n".join(str(p.get("text") or "").strip() for p in m.get("content") or []
                               if isinstance(p, dict) and p.get("type") == "text").strip()
            if text:
                return text[-LAST_WORDS_MAX:]
    return ""


def handoff_prompt(task: store.Task, why: str, instructions: str, last: str = "") -> str:
    wt, spec = task.worktree, task.spec
    steps = gitops.git(wt, "log", "--oneline", f"{spec['base']}..HEAD").strip()
    room = min(int(HANDOFF_DIFF_SHARE * (task.status.get("window") or 32768) * 4), HANDOFF_DIFF_MAX)
    # read no further than the room: a 300 MB generated file, not ignored, was read whole into
    # the runner at each handover only to be cut, risking an OOM kill (#474, the audit of 1.8)
    diff = gitops.diff(wt, "HEAD", limit=room)
    # its stat capped as the review's is: uncapped, a venv or a build directory the repo doesn't
    # ignore put 3,000 stat lines ahead of "## Now", past the fresh session's reach (#863)
    work = (f"```diff\n{diff}\n```" if diff is not None else
            "The diff is too long to include; read the changed files:\n"
            f"```\n{gitops.diff(wt, 'HEAD', stat=True, stat_count=gitops.CHANGED_SHOWN)}\n```"
            + "".join(f"\n{d} holds {k:,} new files the project doesn't ignore: build output, "
                      "probably; don't read them" for d, k in
                      gitops.build_dirs(wt, gitops.changed(wt))[:gitops.CHANGED_SHOWN]))
    return (f"You are picking this task up in a fresh session ({why}). Nothing from the earlier "
            f"session carries over except what is in the working tree, so start from what is "
            f"below and re-read the files you need.\n\n## The task\n\n"
            f"{current_task_prompt(task)}\n\n## The work so far\n\n"
            + (f"Earlier steps, committed:\n```\n{steps}\n```\n\n" if steps else "")
            + (f"Uncommitted changes (`git diff HEAD`):\n{work}" if diff != "" else
               "No uncommitted changes yet.")
            + (f"\n\n## Claude's answers to your questions\n\nAn earlier session asked, and "
               f"Claude answered; follow these, and don't ask them again:\n\n{qa}"
               if (qa := answered(task)) else "")
            + (f"\n\n## Your last words in the earlier session\n\n{last}" if last else "")
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
    kept = path.with_name("instructions.md")
    # Round n began fresh already, and runs again (its server died before the first event: the
    # revive): its own prompt is the one kept, and prompt.md is the handoff. Taken as the round's
    # prompt, the handoff went over instructions.md and was wrapped in a second one, the spec and
    # the diff twice, and every later reader got the handoff (#780). Made again from the kept one,
    # for the work as it stands now, and said once.
    again = kept.is_file()
    raw = (kept if again else path).read_text().strip()
    if not again:
        kept.write_text(raw)  # round_asks reads it back
    st = task.status
    if again and not st.get("session") and (said := _fresh_said(task, n)):
        why = said  # not "the last session ended before it began": the reason it began fresh
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
        # ... unless round n is the runner's nudge, which round_asks skips but for Claude's
        # notes folded into it: dropping the last ask then dropped the newest real one, the
        # review feedback, and the fresh session got the nudge alone and reported done (#859)
        notes = task.round_dir(n) / FOLDED
        own = (not (task.round_dir(n) / NUDGED).exists()
               or (notes.is_file() and bool(notes.read_text().strip())))
        prior = asks[:-1] if own else asks
        now = raw if not prior else ASKED.format("\n\n".join(prior)).strip() + "\n\nNow:\n\n" + raw
    # a session from before the task's own OpenCode home: its last words, from the user's store
    last = last_words(task, st["session"]) if why == OLD_STORE and st.get("session") else ""
    path.write_text(handoff_prompt(task, why, now, last))
    task.set_status(session=None, sessions=(st.get("sessions") or []) + [st["session"]] * bool(st.get("session")),
                    context=None, skills_told=None)  # a new session hears the skills again
    if again and not st.get("session"):  # no session was replaced: the handover is counted once
        return
    task.event({"ts": store.now(), "round": n, "kind": "fresh", "overflow": why != ASKED_FRESH,
                "compacted": why in (COMPACTED, COMPACTION_FAILED),  # results.step (#969)
                "text": f"{FRESH_SAID}{why}"})


FRESH_SAID = "a fresh session for the agent: "


def _fresh_said(task: store.Task, n: int) -> str | None:
    """Why round n began in a fresh session, as its `fresh` event says, or None."""
    rows = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0]
            if e.get("kind") == "fresh" and e.get("round") == n
            and str(e.get("text") or "").startswith(FRESH_SAID)]
    return rows[-1]["text"][len(FRESH_SAID):] if rows else None


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
# {aside}: self_review_aside, the task's own: `cp f.c /tmp/f.c` sent every task's backup to one
# shared name, and two tasks self-reviewing a cli.py at once could copy back each other's (#1085,
# 2026-11-23); /tmp held 27 such backups, never removed
SELF_REVIEW = (
    "Before Claude reviews this, review your own change as a strict senior reviewer would: `git "
    "diff`, and `git status` for new files. Check each point, and fix what fails:\n"
    "1. Every rule in the task (each must, only, at most, rejects, limit and error message) has a "
    "test that fails if that rule alone breaks. A rule without one: write the test. Check each "
    "rejection and limit by doing it, one at a time: copy the file aside (`cp f.c {aside}/f.c`, "
    "this task's own directory: never a bare /tmp name, which another task may use too), "
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
    if h := tests.get("hang"):  # #779: the whole suite finished in seconds on the base
        net += (f" They now run past {h['limit']}s, where they finished in {h['base']:g}s on the "
                "base: look for a loop that never ends, a read that blocks (input(), a socket), "
                "or a process or server the tests start that never exits.")
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
    the lane refused (lane_refused, #331) was named only after both auto-fix rounds, and so was a
    command the agent's environment lacks (missing_command, #1152; a Python module, #1181; one a
    later step's --test names, #1172). The part before the first colon is the cause, for
    status.last. "" when the failure may be the code's (a dropped proxy may be either: its auto-fix
    prompt says so, auto_fix_prompt)."""
    if not tests or tests["ok"]:
        return ""
    if tests["exit"] == "timeout" and not tests.get("hang"):  # a hang is the change's (#779)
        return (f"the acceptance command ran past --test-timeout "
                f"({int(task.spec.get('test_timeout') or 900)}s): its failure says nothing about "
                "the change; run it in the worktree yourself, or narrow it (wb next --test); "
                "start a slow suite's task with a larger --test-timeout")
    base = task.status.get("base_check") or {}
    cmd = task.spec.get("test_cmd")
    if (line := missing_command(tests, task)) and base.get("cmd") == cmd \
            and _missing_key(line) == _missing_key(missing_command(base, task)):
        # the same command (or Python module, #1181) not found as on the base: auto-fix rounds
        # told "the system works; the bug is in this change" each failed the same way, before
        # Claude heard (#1152, 2026-11-24). A command the step makes says something else once made.
        return (f"the acceptance command can't run in the agent's environment: {line} (exit "
                f"{tests['exit']}, as on the base); {GIVE_TOOL}")
    if line and base.get("cmd") != cmd and missing_name(line) in command_words(cmd):
        # no base check ran this command (wb next --test, --no-base-check), and the tool it names
        # itself isn't on the agent's PATH: both auto-fix rounds went to edits that couldn't help
        # (#1172, 2026-11-24). Not one a script it runs names: the step may write that script.
        # (A module line names no command: missing_name is None, never a command word.)
        return (f"the acceptance command can't run in the agent's environment: {line} (exit 127, "
                f"a tool the command itself names); {GIVE_TOOL}")
    if nodes := tests.get("lane"):
        # the grant that reaches each, as the report names it: the path itself, when it was a
        # file not made yet, sent Claude's `wb allow --path` to make a directory of that name (#670)
        # and one an API lane hides, which no grant reaches, as that (#1093)
        hidden = [n for n in nodes if context.api_hidden_refusal(task, n)]
        nodes = [n for n in nodes if n not in hidden]
        grants = list(dict.fromkeys(lane_grant(n, task) or n for n in nodes))
        said = [f"the lane refused {', '.join(map(shlex.quote, nodes))}: "
                + "; ".join(f"wb allow {task.name} --path {shlex.quote(g)}" for g in grants)
                + ", then wb feedback, if the tests need it"] if nodes else []
        return "; ".join(said + [f"the lane refused {', '.join(map(shlex.quote, hidden))}: "
                                 f"{context.API_HIDDEN_WHY}"] if hidden else said)
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
        msgs = [m for m in export(sid, task=task).get("messages", [])
                if ((m.get("time") or {}).get("created") or 0) >= since * 1000]
        got = router.record_speed(task.spec["model"], msgs, api=api_entry(task) is not None)
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


def read_bound(task: store.Task, n: int, rnd: dict, m: models.Model | None = None) -> int | None:
    """Record round ``n``'s read speed bound (tokens/s) when it ended before the model's first
    output, after BOUND_MIN_S or more, by the silence or round limit or OpenCode giving up; and
    return it; else None. Not after a nudge or Claude's interruption: a model thinking silently
    behind a server that sent its headers at once would read as a slow reader. Nor when the
    server (model ``m``'s) is down or stopped now, or OpenCode's retries say it refused or failed
    a request (anything but a timeout): a server that died before the first token read as ~12
    tokens/s, and the model's next rounds and tasks waited an hour of silence (#862)."""
    from . import router
    if m is not None and m.kind == "api":  # the network's and the queue's time, not a read (#1017)
        return None
    waited = float(rnd.get("waited") or 0)
    if rnd.get("answered") or rnd.get("interrupted") or waited < BOUND_MIN_S \
            or rnd.get("guard") not in (None, "silence", "round"):
        return None
    if any("timed out" not in str(t).lower() for t in rnd.get("retries") or ()):
        return None
    if m is not None and (_down(m) or _stopped(m)):
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
    return rnd.get("exit") not in (0, None) and not rnd.get("lingered") and not rnd.get("left_bg") \
        and not rnd.get("settled")  # #1168: the runner's stop of a self-review that only read


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


def _repo_gone(task: store.Task) -> None:
    """The task's repository is nowhere: not its checkout, nor a main worktree of its git dir.
    A runner sent on (wb feedback, next, resume) crashed with a CalledProcessError traceback and
    never named wb relocate, which accept's own check does (#776)."""
    text = (f"{task.spec['repo']} moved or was deleted: wb relocate {task.name} --repo <its new "
            "path>, then wb resume")
    task.set_status(phase="failed", last=text[:200])
    task.notify("failed", f"task {task.name}: {text}")


def left_catalog(task: store.Task) -> str:
    """The line for an API task whose model OpenCode's catalog no longer lists (#1016): the
    runner fails the task with it, and wb feedback, next and resume refuse with it. A catalog
    whose read failed lists nothing (models.hosted is {}): that says so, never that the model
    left, which would send the user to discard a task a working read would run (#1016's review)."""
    if why := models.hosted_error():
        return (f"task {task.name}: {task.spec.get('model')} can't be checked: OpenCode's catalog can't be "
                f"read now ({why}). Try again once it reads (a failed task: wb resume {task.name})")
    return (f"task {task.name}: {task.spec.get('model')} left OpenCode's catalog: workbench never "
            f"switches a task's model. wb takeover {task.name} to finish it, or wb discard {task.name}")


def _left_catalog(task: store.Task) -> None:
    """drive's end for an API task whose model left the catalog: failed, the machine's doing
    (infra model), before any round: a bare KeyError said `could not load <id>: '<id>'`
    (robustness minor 5). Never run: a discard records nothing of the model, as _load_failed."""
    from .results import ran  # results imports this module
    text = left_catalog(task)
    task.set_status(phase="failed", last=text[:200], infra="model",
                    **({} if ran(task) else {"failed_before": "load"}))
    task.notify("failed", text)


def _load_failed(task: store.Task, e: Exception, api: bool = False) -> None:
    """The task failed before its model could run. ``api``: an API model's keyhole couldn't
    start (keyhole_failure), which loads nothing."""
    if isinstance(e, store.Damaged):  # no model to name: task.json lost its contents (#475)
        task.set_status(phase="failed", last=str(e))
        return task.notify("failed", str(e))
    from .results import ran  # results imports this module
    never = not ran(task)
    n = task.status.get("round", 1)
    task.set_status(phase="failed", last=f"model: {e}", infra="model",
                    **({"failed_before": "load"} if never else {}))
    # the agent never ran: resuming loses nothing, and a discard records nothing of the model.
    # It ran, and round n (wb next's step, a review's feedback) never reached it: the notice
    # named no next step, and the "carry on" sent went without the round's prompt (#1180)
    task.notify("failed", f"task {task.name}: "
                + (f"{task.spec['model']} can't run: {e}" if api else
                   f"could not load {task.spec['model']}: {e}")
                + (f"\nthe agent never ran: once the model can, wb resume {task.name} starts it"
                   if never else
                   f"\nround {n} never reached the agent: once the model can run, wb feedback "
                   f"{task.name} --text \"carry on\" sends its prompt" if not reached(task, n) else ""))


# The keyhole (#1012): an API model's key stays in the runner, and OpenCode gets the keyhole's
# base and token instead. Started by drive before the first round; CURRENT["keyhole"] while it
# serves. Its provider's options go in the layer (write_config_layer) and again in
# OPENCODE_CONFIG_CONTENT (rules_env), loaded last, so a user's own content can't point the
# provider back at its real endpoint; the agent's NO_PROXY covers 127.0.0.1 (agent_env).
def needs_keyhole(m: models.Model) -> bool:
    """Whether an API model's requests need the keyhole: not an Ollama cloud model (this
    machine's Ollama holds that login, and OpenCode sends no key), nor a keyless free one."""
    return m.kind == "api" and not m.cloud and m.key != "none"


def start_keyhole(task: store.Task, m: models.Model) -> keyhole.Keyhole:
    """The task's keyhole, its answers recorded as the provider's health (api-health.json, which
    status reads), and the task's meter's hooks (#1019): each response's usage booked, and the
    day cap's room asked before each request goes out."""
    mt = meter.for_task(task, m)
    return keyhole.start(task, m,
                         on_health=lambda state, why, until: models.note_api(m.provider, state, why, until),
                         **(mt.hooks() if mt is not None else {}))


def keyhole_failure(e: Exception) -> str:
    """A KeyholeError's text with its remedy, when one is a key /connect can store (OAuth, a key
    only in the environment, none stored) and the text doesn't name one already. A provider that
    signs requests, or speaks a way the keyhole can't, has none, nor does OpenCode failing to run
    or answer `auth export`: storing a key again doesn't mend that (#1070, 2026-11-23)."""
    text = str(e)
    if "/connect" in text or "signs each request" in text or "doesn't speak" in text \
            or "API base" in text or "opencode auth " in text:
        return text
    return f"{text}: {keyhole.REMEDY}"


def keyhole_of(task: store.Task) -> keyhole.Keyhole | None:
    """The keyhole this runner serves for ``task``, if any."""
    kh = CURRENT.get("keyhole")
    return kh if kh is not None and getattr(kh.task, "name", None) == task.name else None


def keyhole_overlay(task: store.Task) -> dict:
    """The layer's `provider` overlay for the task's keyhole: its base and token (never the key,
    keyhole.layer_options) as provider.<pid>.options; with the task's own catalog file
    (catalog_path) the model's variants, which the file makes none of (models.catalog_doc); and
    the base and token as the model's own wherever the model has an entry; {} with no keyhole."""
    kh = keyhole_of(task)
    if not kh:
        return {}
    opts = keyhole.layer_options(kh)
    entry, own = {"options": opts}, {}
    if catalog_path(task).is_file() and (variants := models.catalog_variants(kh.model)):
        own["variants"] = variants
    # the model's own base and key too: OpenCode 2.3.2 sends a model's `provider.api` or
    # `options.baseURL` over its provider's (v1/config/migrate.ts migrateModel), and one in the
    # user's global file or content sent the token to that base instead (the review of #1015,
    # 2026-11-23). Only where the model has an entry anyway (the file's, or the user's config):
    # an entry of wb's alone makes OpenCode a model its catalog lacks, with none of its limits
    if own or models.has_model_entry(kh.model.provider, kh.model.name):
        entry["models"] = {kh.model.name: {"provider": {"api": kh.base}, "options": dict(opts),
                                           **own}}
    return {"provider": {kh.model.provider: entry}}


def keyhole_v2(over: dict) -> dict:
    """keyhole_overlay's base and token in OpenCode's V2 form (`providers.<pid>.settings`, a
    model's `settings`), for a document that has a `providers.<pid>` of its own: within one
    document, V2's entry replaces V1's whole (normalize.ts mergeMaps), and a user's content in
    the V2 form pointed the provider past the keyhole (the review of #1015, 2026-11-23)."""
    out = {}
    for pid, entry in (over.get("provider") or {}).items():
        opts = entry.get("options") or {}
        out[pid] = {"settings": dict(opts), "models": {
            name: {"settings": dict(m.get("options") or opts)}
            for name, m in (entry.get("models") or {}).items()}}
    return out


CATALOG_FILE = "catalog.json"


def catalog_path(task: store.Task) -> Path:
    """The task's own catalog for OPENCODE_MODELS_PATH: in its rules directory, readable in the
    lane and never writable from it."""
    return context.rules_dir(task) / CATALOG_FILE


def write_catalog(task: store.Task) -> Path | None:
    """The catalog the agent's OpenCode reads, holding the task's API model alone
    (models.catalog_doc), or None (and no file) when it can't be rebuilt: the bundled catalog then
    serves, as before. The agent's OpenCode fetches no catalog (#1010), and the one in its binary
    may lack a model the user's OpenCode lists. Only a task with a keyhole gets one (a keyless
    free model or an Ollama cloud one has the bundled catalog, ARCHITECTURE.md *The task's own
    catalog*)."""
    path = catalog_path(task)
    kh = keyhole_of(task)
    doc = models.catalog_doc(kh.model) if kh else None
    if doc is None:
        path.unlink(missing_ok=True)
        return None
    store.write_json(path, doc)
    return path


# A model whose server is only down (models.may_come_up: Ollama not answering, a server with no
# start command not running) is waited for, looking again after 2 s, 4 s, ... up to a minute
# apart, for as long as a server's first start may take. Failing at once, a task wb start --force
# queued "to wait for it" failed on its first check, and an Ollama restart or an out-of-memory
# kill between rounds failed the task for good (the 2026-11-19 audit). A configuration error
# (not pulled, a refused key) still fails at once. wb stop ends the runner, and the wait with it.
DOWN_WAIT = models.START_TIMEOUT
DOWN_PAUSES = (2, 60)


def _down(m: models.Model) -> bool:
    if m.kind == "api":  # no server of the user's to wait for, nor to revive (#1015)
        return False
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


def _wait_up(task: store.Task, m: models.Model) -> models.Model:
    """Wait, backing off, until the model's server is no longer only down; RuntimeError past
    DOWN_WAIT. Whatever it is then, the caller's load says. The model is read again each pass,
    and returned as last read: a task queued after a Windows reboot, before its server was up,
    waited 20 minutes on Windows' old address while discovery followed it to the new one (#1167,
    2026-11-24)."""
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
        with contextlib.suppress(KeyError, RuntimeError, OSError, ValueError):
            m = models.resolve(m.id)  # gone from the config: the one it had
    return m


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
    if m.kind != "api" and (why := models.short_window(m)):  # an API model: no server to ask
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


@contextlib.contextmanager
def _tests_gpu(task: store.Task, lease):
    """This machine's GPU to a round's tests alone, with --unload-for-tests, once `step_aside`
    has let the model go: on this GPU the lease's own pool, held for them and waited empty, and
    what the other tasks left loaded freed (free_gpu, keep_loaded kept) and let go of; for a model
    on another machine's GPU (a wb remote box), whose pool the tests left, this machine's pool,
    taken alone as the base check's is (verify.gpu). The tests run here whatever the model's
    pool: before, a peer's model, a local task's or a wb image job's shared this GPU with them
    (#943)."""
    if not task.spec.get("unload_for_tests") or not task.spec.get("test_cmd"):
        yield
        return
    from . import media, verify  # verify imports this module; media only for these tests
    say = lambda msg: task.set_status(last=msg)  # noqa: E731
    if lease.pool == "gpu":
        models.free_gpu(say=say)
        media.settle(say)
        yield
        return
    with verify.gpu(task, say):
        yield


def provider_remedy(task: store.Task, guard: str, said: str = "") -> str:
    """What to do about a stop that is the provider's (#1020): never a model or route switched
    by wb or Claude (the 2.0 rules); `wb route` only with a role, and only with the user's yes.
    ``said``: the stop's line, whose HTTP 404 may mean the model is gone (#1082)."""
    p, name = provider_of(task), task.name
    resume = f'wb feedback {name} --text "carry on"'
    role = task.spec.get("role")
    if guard == "refused":  # the provider's reason is in the STOPPED line: no wait or key fixes it
        gone = (f"; if {p} no longer serves {task.spec.get('model')}, workbench never switches a "
                f"task's model: wb takeover {name} or wb discard {name}") if "HTTP 404" in said else ""
        return (f"the provider's stop, not the agent's: once what {p} said is fixed, {resume} "
                f"resumes it" + gone
                + (f"; or wb route {role} gpu (the user's yes) for the next tasks" if role else ""))
    if guard == "quota":
        return (f"the provider's stop, not the agent's: top up your {p} account, or wait for the "
                f"plan's reset; then {resume} resumes it")
    if guard == "auth":
        return (f"the provider's stop, not the agent's: store {p}'s API key again with /connect "
                f"in OpenCode; then {resume} resumes it")
    if guard == "setup":  # #1080: no route change or wait mends this machine's own setup
        return (f"this machine's stop, not the agent's or {p}'s: mend what the line above says; "
                f"then {resume} resumes it")
    return (f"the provider's stop, not the agent's: {resume} resumes it once {p} allows"
            + (f"; or wb route {role} gpu (the user's yes) for the next tasks" if role else ""))


def _provider_revive(task: store.Task, n: int, rnd: dict, lease) -> bool:
    """Round n stopped by its provider's rate limit or outage, the first time: wait out the
    provider's cooldown (api-health.json's `until`, else RATE_LIMIT_SECS; at most RATE_WAIT_S),
    then run it again, continuing the session. One revive a round: it re-sends the whole context,
    which costs money. True to run it again; False when a steer note came in meanwhile (the
    caller's _steer_round takes it). wb stop ends the runner, and the wait with it."""
    p = provider_of(task)
    task.event({"ts": store.now(), "round": n, "kind": "load",
                "text": (f"{p} rate-limited the round" if rnd["guard"] == "rate_limited" else
                         f"{p} didn't answer the round") + ": it runs again once the cooldown ends"})
    # nothing of ours needs the GPU while the provider makes us wait; #1015's NoLease may lack it
    getattr(lease, "step_aside", lambda: None)()
    now = time.time()
    try:
        until = (models.api_health(p) or {}).get("until")
    except Exception:  # an unreadable file: the plain wait
        until = None
    until = until if isinstance(until, (int, float)) and until > now else now + models.RATE_LIMIT_SECS
    end = min(until, now + RATE_WAIT_S)
    task.set_status(last=f"{rnd['stuck']}: the round runs again at "
                         f"{time.strftime('%H:%M:%S', time.localtime(end))} (wb stop gives up)")
    while (left := end - time.time()) > 0:
        if pending_steer(task):
            return False
        time.sleep(min(1.0, left))
    return not pending_steer(task)


def _rejoin(task: store.Task, lease) -> None:
    """Back on the GPU before a round, after the tests or a server's wait let it go (#257):
    queued, in its first-come place, when the GPU is someone else's meanwhile."""
    if lease.aside:
        task.set_status(phase="queued", last="waiting for the GPU")
        lease.rejoin()


def _gpu_side(task: store.Task) -> bool:
    try:
        return models.gpu_side(task.spec["model"])
    except Exception:  # a config that doesn't parse: resolve's to report
        return True


def _spend_round(task: store.Task, n: int):
    """An API task's meter for round n (meter.Meter: its live figures from 0), else None."""
    if (spend := meter.for_task(task)) is not None:
        spend.begin(n)
    return spend


def _spend_guard(task: store.Task, out: dict) -> None:
    """The in-round spend guard (#1019), each tick: the keyhole's own stop (status `api_stop`,
    its guard: the room it refused for, #1020's quota) or the day cap reached
    (meter.stop_reason). The keyhole refuses at once (a 400 OpenCode doesn't retry), then the
    round stops: no retry loop runs to the silence limit."""
    stop = task.status.get("api_stop")
    if isinstance(stop, dict) and stop.get("text"):
        why, guard = str(stop["text"]), str(stop.get("guard") or "spend")
        if stop.get("worst") is not None:  # for stop_text, as _ProviderWait.stop keeps it (#1150)
            out["worst"] = stop["worst"]
    elif why := meter.stop_reason(task):
        guard = "spend"
    else:
        return
    if (kh := CURRENT.get("keyhole")) is not None:
        kh.close(why)
    out["stuck"], out["guard"] = why, guard


def _spend_end(task: store.Task) -> None:
    """An API task's round over: OpenCode's figure for its sessions, reconciled into the ledger."""
    if (spend := meter.for_task(task)) is None or spend.plan:
        return
    from .transcript import export, finished_export
    st = task.status
    sessions = {}
    try:
        for sid in dict.fromkeys([*(st.get("sessions") or []), st.get("session")]):
            if sid:
                sessions[sid] = export(sid, task=task) if sid == st.get("session") \
                    else finished_export(task, sid)
        spend.end_round(sessions)
    except Exception:  # a measurement: never a reason to stop the task
        pass


def _spend_lead(task: store.Task, rnd: dict) -> str:
    """The stuck notice's first line for a spend stop: what to ask the user."""
    return meter.stop_text(task, rnd.get("worst")) + "\n" if rnd.get("guard") == "spend" else ""


def first_tokens(task: store.Task | None = None, prompt: str | None = None,
                 fresh: bool | None = None) -> int:
    """What the next round's first request carries, in tokens as the keyhole's room counts a
    body (its bytes / 4), for the day cap's look ahead (meter.request_floor). Left out, a day
    within that request's input of the cap passed wb start, feedback, next, resume and _go_on,
    and the room refused the request at once; raised to the cap the refusal named, it was
    refused again (#1173, 2026-11-24). A continued session carries its context (status
    `live_used`, its last step's), a fresh one OpenCode's own (meter.FIRST_TOKENS); then the
    round's prompt (``prompt``, else the round's own: store.round_prompt), in a fresh session
    after an earlier one inside its handoff (handoff_prompt), never past PROMPT_ARG_MAX, past
    which it goes as a file the agent reads. ``task`` None: wb start's, round 1's prompt.
    ``fresh``: as the caller knows it (wb resume's, --fresh), else from the status alone, not
    fresh_reason's export: a full session carried on counts its context, more than a handoff.
    True is a later round begun fresh (status `context` "fresh"), which the runner always hands
    over (fresh_reason, _start_fresh), session or not: judged from the status, a resume after a
    round that never made a session counted its bare prompt, under half the first request, and
    the room refused it at the cap named (#1173's review, 2026-11-24)."""
    st = task.status if task is not None else {}
    if prompt is None:
        path = store.round_prompt(task, st.get("round", 1)) if task is not None else None
        prompt = path.read_text() if path is not None and path.is_file() else ""
    handoff = fresh is True
    if fresh is None:
        fresh = st.get("context") in ("fresh", "full") or not st.get("session") or old_store(st)
        handoff = fresh and bool(st.get("session") or (st.get("step_round") or 1) < st.get("round", 1))
    live = st.get("live_used") or {}
    carried = live.get("used") if st.get("session") and live.get("session") == st.get("session") else 0
    carried = int(carried) if isinstance(carried, (int, float)) and carried > 0 else 0
    base = meter.FIRST_TOKENS if fresh else max(carried, meter.FIRST_TOKENS)
    size = len(prompt.encode())
    if handoff and task is not None:
        try:  # what _start_fresh will send: the task, the work so far, then the prompt
            size = len(handoff_prompt(task, "", prompt).encode())
        except Exception:  # an estimate: never a reason to refuse, so the most argv can carry
            size = PROMPT_ARG_MAX
    return base + -(-min(size, PROMPT_ARG_MAX) // 4)


def _go_on(task: store.Task, n: int, what: str) -> bool:
    """Before each `continue` in drive (#1017, #1019): for an API task, today's spend under the
    cap by at least one request at its worst (meter.request_floor, #1150), its input the next
    round's first request's (first_tokens, #1173). Short of that, the task stops before
    ``what`` runs: phase `stuck`, status `guard` `spend`, a `stuck` notice with cause `spend`
    (not the step's own: results.THE_STEP). A GPU task always goes on, for nothing."""
    if meter.for_task(task) is None:
        return True
    tokens = first_tokens(task)
    if not (why := meter.stop_reason(task, ahead=True, tokens=tokens)):
        return True
    if (kh := CURRENT.get("keyhole")) is not None:
        kh.close(why)
    # infra and `by` as the in-round stop has them (stopped_by, #1020): the user's budget, never
    # the step's, nor a lesson about the agent
    task.set_status(phase="stuck", last=why[:200], guard="spend", infra=SPEND)
    task.event({"ts": store.now(), "round": n, "kind": "stopped", "text": f"{why}: {what} didn't run",
                "by": SPEND})
    text = f"{meter.stop_text(task, tokens=tokens)}\n{what} didn't run: {why}."
    # auto-fix and self-review have moved status `round` on to n + 1, which never ran: round n's
    # report goes with the notice, and says where it is (wb report alone said "no report yet")
    if (summary := task.round_dir(n) / "summary.md").exists():
        text += f"\nRound {n}'s report (wb report {task.name} --round {n}):\n{summary.read_text()}"
    task.notify("stuck", text, round=n, cause="spend")
    return False


def drive(task: store.Task) -> None:
    # infra: why the machine, not the agent, failed the task's last run (wb bench grades none of it).
    # `last` at once, from the config alone (gpu_side): left to after resolve, which may read the
    # catalog, a GPU task showed the last run's line ("Claude reviews next") as queued (the
    # review of #1015, 2026-11-23)
    task.set_status(phase="queued", pid=os.getpid(), infra=None,
                    last="waiting for the GPU" if _gpu_side(task) else "starting",
                    boot=store.boot_id(),  # a later boot reaps it as a restart's (cli.reap)
                    machine=store.machine(), host=store.host(),  # whose pid it is (#849)
                    beat=BEAT_SECS)  # and that its lock file beats (#872)
    say = lambda msg: task.set_status(last=msg)  # noqa: E731
    CURRENT.pop("model", None)  # an earlier task's, driven in-process (the tests)
    try:
        m = models.resolve(task.spec["model"])
    except KeyError as e:
        if task.spec.get("via") == "api":  # never another model in its place (#1016)
            return _left_catalog(task)
        return _load_failed(task, e)
    api = m.kind == "api"
    CURRENT["model"] = m  # its server's port stays open in the lane (lane_ports)
    settle_via(task, api)
    # an API task waits for no GPU (#1015): its first line says so
    task.set_status(last="starting" if api else "waiting for the GPU")
    CURRENT["api"] = api  # no GPU power to sample, no keep-alive to renew (run_round)
    pin_api(task, m)  # an API task's caps, read from the task from now on (#1017)
    meter.for_task(task, m)  # an API task's meter (#1019): the ledger, the spend guard, the keyhole's hooks
    # the GPU is not ours to measure when the model is remote; an API model's isn't a GPU's at
    # all, and its savings line is its own (#1015)
    CURRENT["remote"] = m.remote and not api
    if CURRENT["remote"]:
        task.set_status(remote_gpu=True)  # so its savings say the energy wasn't measured (#117)
    if api and needs_keyhole(m):
        try:
            CURRENT["keyhole"] = start_keyhole(task, m)
        except keyhole.KeyholeError as e:
            return _load_failed(task, RuntimeError(keyhole_failure(e)), api=True)
        write_catalog(task)
    try:
        _drive(task, m, say)
    finally:
        if kh := CURRENT.pop("keyhole", None):  # a runner's own: it dies with it; closed for a
            kh.close()                          # runner driven in-process (the tests)


def _drive(task: store.Task, m: models.Model, say) -> None:
    """drive, once the model is known (and an API model's keyhole is serving)."""
    if not gitops.repo_of(task.spec).is_dir():  # moved, renamed, or its git dir removed (#776)
        return _repo_gone(task)
    if not checkout(task):  # the worktree itself, out of wb start's foreground (#529)
        return
    _hands_on(task)  # Claude's ignored files from the review, before the agent writes (#1179)
    if not _prepare(task):  # the project as the user has it, before the base check (#201)
        return
    if _down(m):  # before the lease: a server that is down holds no GPU from other tasks
        try:
            m = CURRENT["model"] = _wait_up(task, m)  # at Windows' new address, when discovery followed it (#1167)
        except RuntimeError as e:
            return _load_failed(task, e)
    if needs_base_check(task):
        _base_check(task)  # on the untouched base, before the agent and the GPU
    if opencode_moved(task):  # before the GPU is taken for a round that can't run (#866)
        return
    if task.status.get("round", 1) == 1:  # before round 1's first OpenCode call (probe on), and
        # before the lease: it needs no GPU, and a probe that hangs (20 s a file) held it from the
        # other tasks, the model already loaded (review, 2026-11-21)
        add_outline(task)
    # beside the running tasks' models when all fit the GPU, else alone once they finish; an API
    # model takes one of its provider's slots instead, and nothing of the GPU (#1015)
    with models.lease_for(m, task.name, say) as lease:
        # this runner reached the lease: from now on gpu.json says whether it holds the GPU, so
        # wb start no longer counts it as a holder while it steps aside for its tests (#805)
        task.set_status(leased=os.getpid(), **cards_now(task, m))  # the pane's `GPU 1` (#1048)
        try:
            _loaded(task, _load(task, lease, m))
            window = (m.config.get("limit") or {}).get("context")
            task.set_status(window=window)
            tuning.tune(task, task.spec["model"], window, {"fresh_at": fresh_ceiling(m.config)},
                        fixed_knobs(task, m))  # --self-review / --no-self-review, API's, else learned
        except (KeyError, RuntimeError, OSError) as e:
            return _load_failed(task, e)
        handed = 0  # mid-round handovers in this runner: one review round (HANDOVER_MAX)
        first = True
        revived = None  # the round run again after its server went away under it
        while True:
            if _updated(task):  # before the round: its lane, shim and ask server would be new
                return
            if not first and opencode_moved(task):  # reinstalled between rounds (#866)
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
            why = fresh_reason(task) if n > 1 or old_store(task.status) else None
            keep = task.status.get("context") == "keep"
            if why:
                _start_fresh(task, n, why)
            elif keep:
                task.set_status(context=None)  # a one-round instruction, held by run_round
            if raised := stretch_limits(task, m):  # measured slow since wb start (#814)
                task.event({"ts": store.now(), "round": n, "kind": "limits",
                            "text": "the model was measured slow here: " + ", ".join(
                                f"{k} {v // 60} min" for k, v in raised.items())})
            task.set_status(phase="running", round=n, round_started_at=store.now(),
                            last="agent started",  # not "starting…": the pane shows its progress
                            **typical_now(task))
            task.event({"ts": store.now(), "round": n, "kind": "round", "text": "started"})
            began = time.time()
            try:
                rnd = run_round(task, n, keep=keep)
            finally:  # before the lease is released: the GPU's next task must not meet our agent
                _end_agent(task)
            if not rnd.get("held"):  # a held request's first token waited on the provider (#1020)
                measure_speed(task, began)
            _spend_end(task)
            if (bound := read_bound(task, n, rnd, m)) and rnd["guard"] == "silence":
                from . import router
                rnd["stuck"] += (f" (it read at most ~{bound} tokens/s here: this task's next "
                                 f"round and a new task on this model now wait "
                                 f"{router.budgets(m)['silence'] // 60} minutes)")
            if rnd["full"]:  # before notes: the handover carries them (_full_round)
                handed += 1
                if handed <= HANDOVER_MAX:
                    _round_end(task, n, rnd, began)
                    _full_round(task, n, rnd, handed)
                    if not _go_on(task, n, "the handover"):
                        return
                    continue
                rnd["guard"] = "handovers"
                rnd["stuck"] = (f"handed over to a fresh session {HANDOVER_MAX} times in this "
                                "round without finishing"
                                # the last one a failed compaction, named: not a silent failure,
                                # and the reading, not OpenCode, is what to cut (#969)
                                + (f" (the last when OpenCode's compaction failed: "
                                   f"{rnd['compact_failed']})" if rnd.get("compact_failed") else "")
                                + ": the step is too big for this model's "
                                "window; split it (wb next) or take it over")
            _round_end(task, n, rnd, began)
            if rnd["interrupted"] or (pending_steer(task) and not rnd["stuck"]):
                _steer_round(task, n, rnd["interrupted"])
                if not _go_on(task, n, "Claude's note"):
                    return
                continue
            if rnd["guard"] in HELD and revived != n:  # the provider's rate limit or outage: once (#1020)
                revived = n
                if not _provider_revive(task, n, rnd, lease):
                    _steer_round(task, n, False)
                if not _go_on(task, n, "the round again"):  # the day cap holds here too (#1019)
                    return
                continue
            crashed = not rnd["stuck"] and rnd["exit"] not in (0, None)
            hushed = rnd["guard"] == "silence"  # OpenCode retrying a server that is gone
            if (crashed or hushed) and revived != n and ((down := _down(m)) or _stopped(m)):
                # OpenCode died (or went quiet) while the model's server is down (restarted, or
                # killed out of memory). Once it is back, the loop's load reloads it and the round
                # runs again, once, continuing the session (the 2026-11-19 audit). A server wb
                # starts itself isn't waited for: the loop's load starts it (#579). Whatever the
                # model said first: an OOM kill comes late in a round, after its first words, and
                # failed the task against the model (#657)
                revived = n
                task.event({"ts": store.now(), "round": n, "kind": "load",
                            "text": (f"opencode exited {rnd['exit']}" if crashed else "the round went silent")
                                    + " with the model's server down: "
                                    + ("the round runs again once it is back" if down else
                                       "wb starts it again and the round runs again")})
                lease.step_aside()  # #257: nothing of ours is on the GPU while it is down
                if down:
                    try:
                        _wait_up(task, m)
                    except RuntimeError as e:
                        return _load_failed(task, e)
                if not _go_on(task, n, "the round again"):
                    return
                continue
            if rnd["stuck"] or died(rnd):
                text = _spend_lead(task, rnd) + summary_text(task, n, rnd, None) + _undelivered(task)
                task.round_dir(n).joinpath("summary.md").write_text(text)
                phase = "stuck" if rnd["stuck"] else "failed"
                # its server gone again after the round's one revive: the machine's, not the
                # step's, so the model's slice isn't shrunk for it (#657)
                server = (crashed or hushed) and revived == n and (_down(m) or _stopped(m))
                last = rnd["stuck"] or f"opencode exited {rnd['exit']}"
                said = [e[len("opencode: "):] for e in rnd["errors"] if e.startswith("opencode: ")]
                if not rnd["stuck"] and CURRENT.get("api") and said:
                    # the provider's error, as the keyhole redacted it (an auth or quota error
                    # ends the run at once): what Claude needs to know, not only the exit (#1012)
                    last = f"{last}: {said[-1]}"[:200]
                # the provider's or the spend cap's, not the step's (#1020, #1019)
                ours = None if server else stopped_by(rnd["guard"])
                task.set_status(phase=phase, last=last,
                                **({"infra": "model"} if server else {"infra": ours} if ours else {}))
                # what stopped it, for results.step: a guard or the agent's own exit (#582)
                task.notify(phase, text, round=n, cause="server" if server else ours
                            or ("guard" if rnd["stuck"] else "agent"))
                return
            # the tests need no model: the GPU is the queue's, or theirs (--unload-for-tests), #257.
            # Theirs is waited for (a peer's round, up to its timeout; this machine's pool for a
            # remote model): queued meanwhile, as _rejoin is, since left `running`, wb steer --now
            # promised an interrupt within seconds and the wait counted as active work (#943's
            # review, 2026-11-21)
            if unload := bool(task.spec.get("unload_for_tests")):
                task.set_status(phase="queued", last="waiting for the GPU: the tests use it alone")
            lease.step_aside(unload=unload)
            with _tests_gpu(task, lease):
                tests = run_tests(task, n)
            if pending_steer(task):  # a note that came in while the tests ran
                _steer_round(task, n, False)
                if not _go_on(task, n, "Claude's note"):
                    return
                continue
            text = summary_text(task, n, rnd, tests)
            if skip := not_the_code(task, tests):  # first: what Claude must do about it
                text = f"auto-fix skipped: {skip}\n" + text
            if pending_steer(task):  # a note that came in while the report was written
                _steer_round(task, n, False)
                if not _go_on(task, n, "Claude's note"):
                    return
                continue
            task.round_dir(n).joinpath("summary.md").write_text(text)
            used = task.status.get("auto_fixes", 0)
            if tests and not tests["ok"] and not skip and used < int(task.spec.get("auto_fix", 0)):
                task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
                task.round_dir(n + 1).joinpath("prompt.md").write_text(
                    auto_fix_prompt(tests, task.spec["test_cmd"]))
                task.set_status(round=n + 1, auto_fixes=used + 1, tests=tests, round_for="fix")
                task.event({"ts": store.now(), "round": n, "kind": "autofix",
                            "text": "tests failed; sent the output back to the agent"})
                if not _go_on(task, n, "auto-fix"):
                    return
                continue
            if tests and tests["ok"] and not task.status.get("self_review_due"):
                # self-review could run, whether or not it does: only such a task is evidence
                # of the knob, which a test-less one could turn off (tuning.acted, #816)
                task.set_status(self_review_due=True)
            mark = [task.status.get("steps", 1), task.status.get("reviews", 0)]
            if tuning.value(task, "self_review") and tests and tests["ok"] \
                    and task.status.get("self_reviewed") != mark:
                task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
                task.round_dir(n + 1).joinpath("prompt.md").write_text(
                    SELF_REVIEW.format(cmd=task.spec["test_cmd"], aside=self_review_aside(task)))
                task.set_status(round=n + 1, self_reviewed=mark, tests=tests, round_for="self_review")
                task.event({"ts": store.now(), "round": n, "kind": "self_review",
                            "text": "tests pass; the agent reviews its own change first"})
                if not _go_on(task, n, "self-review"):
                    return
                continue
            task.set_status(phase="review", tests=tests,
                            hands_off_at=store.now(),  # Claude's to edit from now (#1179)
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


# A wb feedback round after a stop that wasn't the agent's (status `infra`: the provider's, a
# reaped runner's): its "carry on" is under FEEDBACK_HEAD but is no review, so round_asks keeps
# it and walks on to the feedback the stop interrupted (#1169).
RESUMED = "resumed"


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
    reported done with the feedback never made. A resume (wb resume) is skipped the same way,
    and a feedback round after an infra stop (RESUMED) is no stop: its "carry on" once hid the
    review feedback before it (#1169)."""
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
        if raw.startswith(lessons.FEEDBACK_HEAD) and not (task.round_dir(r) / RESUMED).exists():
            break
    return out


# A handover needs a successor with room to work: at least this share of the window between
# the reading it repeats (the floor) and the handover point. Less, and the session carries on.
HANDOVER_ROOM = 0.25


def compacted_between(last: tuple, sid, used: int, point: float, window: int, pending: int = 0) -> bool:
    """Whether OpenCode compacted the session between the last step (`last`: its session and
    context) and this one: in the same session, this step carries under half of what the last
    did, and the last, with the tool output since (``pending``, tokens: OpenCode compacts with
    it counted in, #1086), was within one step's growth (fresh_ceiling's margin) of the handover
    point, where only OpenCode's compaction shrinks a context (#774)."""
    margin = max(STEP_GROWTH, tuning.CEILING_MARGIN * window)
    return bool(sid) and last[0] == sid and 0 < used < last[1] / 2 and last[1] + pending >= point - margin


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
    window = task.status.get("window")
    pct = 100 * rnd["full"] // window if window else None
    failed = rnd.get("compact_failed")  # #969: OpenCode prints no size for it, so its message is said
    text = (f"OpenCode's compaction of the agent's session failed ({failed}): a fresh one takes over"
            if failed else
            f"the agent's session reached {pct}% of its window "
            f"({rnd['full'] / 1000:.1f}K of {window // 1024}K)"
            + (", then OpenCode compacted it" if rnd.get("compacted") else "")  # #774
            + ": a fresh one takes over")
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
    task.set_status(round=n + 1, context="full", full_pct=pct, round_for="fresh",
                    full_compacted=True if rnd.get("compacted") else None,  # #774: fresh_reason
                    full_compact_failed=True if failed else None,  # #969: and its reason
                    handovers=task.status.get("handovers", 0) + 1,
                    steer_taken=task.status.get("steer_taken", 0) + len(notes))


def _undelivered(task: store.Task) -> str:
    """Claude's notes a stopped round never delivered, for its report (the runner's own nudges
    aside): wb feedback and wb next drop them, wb resume carries them (cli._stale_notes)."""
    notes = [r for r in pending_steer(task) if r.get("by") != "workbench"]
    return ("\nNOTE: not delivered, the round stopped first: "
            + " | ".join(r["text"].strip().replace("\n", " ")[:200] for r in notes)) if notes else ""


def reached(task: store.Task, n: int) -> bool:
    """Whether round n's prompt went to the agent: drive logs the round's `round` event as it
    starts it. Not when the runner stopped before (the model's load, the server's wait, the
    keyhole, the checkout, the spend cap): wb feedback then runs round n again (#1180)."""
    return any(e.get("kind") == "round" and e.get("round") == n
               for e in store.read_jsonl(task.dir / "events.jsonl")[0])


def add_to_round(task: store.Task, n: int, text: str) -> None:
    """``text`` after round n's prompt, which has not run, and beside it for round_asks (FOLDED:
    it skips a carry-on, a resume or a nudge whole, #677)."""
    folded = task.round_dir(n) / FOLDED
    folded.write_text((folded.read_text().rstrip() + "\n\n" if folded.is_file() else "") + text + "\n")
    path = task.round_dir(n).joinpath("prompt.md")
    for f in (path, path.with_name("instructions.md")):  # a revived fresh round's own too (#780)
        if f == path or f.is_file():
            f.write_text(f.read_text().rstrip() + "\n\n" + text)


def _early_notes(task: store.Task, n: int) -> None:
    """Notes that came in before the round began (queued for the GPU, its model or the base
    check, or while the model reloaded) go with its prompt, as `wb steer` promises: they once
    waited for the round to end and came as a round of their own (#452)."""
    if not (notes := pending_steer(task)):
        return
    add_to_round(task, n, steer_prompt(notes, False))
    task.set_status(steer_taken=task.status.get("steer_taken", 0) + len(notes))


def _steer_round(task: store.Task, n: int, interrupted: bool) -> None:
    notes = pending_steer(task)
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    task.round_dir(n + 1).joinpath("prompt.md").write_text(steer_prompt(notes, interrupted))
    if any(r.get("by") == "workbench" for r in notes):  # the nudge isn't an ask (#681) ...
        task.round_dir(n + 1).joinpath(NUDGED).touch()
        if theirs := [r for r in notes if r.get("by") != "workbench"]:  # ... Claude's notes are
            folded_notes(task, n + 1, theirs)
    task.set_status(round=n + 1, steer_taken=task.status.get("steer_taken", 0) + len(notes),
                    round_for="steer")


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
    # not the step's: an update mid-step shrank the model's slice (#660). Status `infra`, so wb
    # feedback counts no review round, and the notice's `tasks`, so the mod's wake says it was
    # interrupted and names wb resume: it said "it got stuck" and advised wb feedback, which
    # counted a round, and one real review after it made the step go badly (#1176, 2026-11-24)
    task.set_status(phase="stuck", last=msg, infra="updated")
    task.notify("stuck", f"task {task.name}: {msg}", round=task.status.get("round"), cause="updated",
                tasks=[task.name])
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
        names = [t.name for t in batch]
        ts = store.now()
        # the steps `wb next` committed, which a discard keeps (#494), for each task they are in
        kept = [line for t in batch if (line := steps_line(t))]
        row = batch[0].notify("stuck", (
            f"task {task.name}: {SIGNAL_STOP}. wb resume {task.name} continues it in a fresh "
            f"session, wb discard {task.name} drops it" if len(batch) == 1 else
            f"{len(batch)} tasks' runners were stopped by a signal (a shutdown, a logout or a "
            f"kill), not by Claude: {', '.join(names)}. wb resume {' '.join(names)}"
            # by name: `--all --repo <cwd>` resumed nothing for a session in ~, and another
            # session's tasks in a shared repo (#692)
            + " continues them in the order they were started")
            + "".join(f"\n{k}" for k in kept),
            round=batch[0].status.get("round"), ts=ts, tasks=names,
            cause="signal")  # not the step's size (#582)
        store.ack_batch(batch, row, ts)  # had with the first's notice: neither wb wait nor the mod wakes for it


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


# How often the runner touches its lock file (lock_file "runner"): another machine sharing the
# state directory can't see its pid, so it tells a live runner from a dead one by this alone
# (cli.runner_alive). A runner killed with its machine (power loss, a recreated container with a
# new host name, a reimaged machine) kept its task live there for good: no command could stop,
# discard or resume it (#872, Claude's audit of 1.90.24). The lock file's name ends in .lock, so
# no `wb events` wakes for the touch.
BEAT_SECS = 60


def _beat(path: Path) -> None:
    def touch():
        while True:
            time.sleep(BEAT_SECS)
            try:
                os.utime(path)
            except OSError:
                pass  # a full or gone state directory: the beat is best effort
    threading.Thread(target=touch, daemon=True, name="beat").start()


def load_up_front() -> None:
    """Import now every module the runner imports late, so a mid-task pull can't pair this old
    process with a new file's code (#603): results (each API round's report, _load_failed) and
    cli (add_outline's PROTOCOL) were missed and loaded mid-task (#1115, 2026-11-23); a test
    walks this file's late imports. Cheap: cli and results import this module already."""
    from . import cli, results, router, transcript, verify  # noqa: F401


# A backlog task that waits on Claude keeps the machine awake this long for Claude's review, its
# feedback and the morning summary, or until that Claude Code exits (#1105)
REVIEW_AWAKE_SECS = 1800
# the waiter keep_awake holds the inhibitor for (argv: Claude Code's pid, the seconds)
AWAIT_CLAUDE = """import os, sys, time
pid, until = int(sys.argv[1]), time.monotonic() + float(sys.argv[2])
while time.monotonic() < until:
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(min(5, max(0.0, until - time.monotonic())))
"""


def awake_for_review(task: store.Task):
    """A backlog task that stopped waiting on Claude (review, stuck, failed): hold the machine
    awake for REVIEW_AWAKE_SECS while its Claude Code lives. Every inhibitor was a process wb had
    detached, so when a batch's last task (or its last parallel ones) reached review none was
    held, and on Linux and WSL2, where Claude Code holds none of its own, the machine could
    idle-sleep mid-review, the feedback round and the summary waiting for the user (#1105,
    2026-11-23). The waiter process, its pid the inhibitor's, or None."""
    from . import cli  # cli imports this module
    pid, spec = task.spec.get("claude_pid"), task.spec
    if not spec.get("backlog") or task.status.get("phase") not in ("review", "stuck", "failed") \
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 \
            or spec.get("claude_machine") not in (None, store.machine()) \
            or not models._alive(pid, spec.get("claude_start")) or not cli.awake_how():
        return None
    try:
        waiter = subprocess.Popen([sys.executable, "-c", AWAIT_CLAUDE, str(pid), str(REVIEW_AWAKE_SECS)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    cli.keep_awake(waiter.pid, f"task {task.name} waits on Claude's review")
    return waiter


def main():
    store.absolute_env()
    gitops.drop_repo_env()  # the caller's repository variables, for the agent too (#798)
    task = store.Task(sys.argv[1])
    lock = _one_runner(task)  # noqa: F841 (held until the process exits)
    if lock is None:
        print(f"another runner drives {task.name}; this one exits", flush=True)
        return
    _beat(lock_file(task.name, "runner"))
    signal.signal(signal.SIGTERM, _on_term)
    CURRENT["code"] = code_print()  # what the children load; compared before each round (#603)
    load_up_front()
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
                hands_off(task)  # the agent is stopped: what is written next is Claude's (#1179)
            except OSError as e:
                if not store.disk_full(e):
                    raise
            try:
                awake_for_review(task)
            except Exception:  # never the runner's exit
                pass


# Thinking per task (#1018). API providers think by default (DeepSeek at effort high), and
# reasoning bills at the output rate, so an API task runs with thinking off unless its role says
# on (decision 5 of #1008). It is fixed at wb start (with_thinking): DeepSeek drops the prompt
# cache when the switch or the effort changes between requests, so feedback, next, resume and a
# handover keep the task's opencode_model as it was. Thinking off is the catalog's own variant
# that turns it off when the model lists one (`none`: OpenCode 2.3.2 lists it for DeepSeek's,
# DeepInfra's and most OpenAI and OpenRouter models, but for only 2 of Anthropic's 20); else, for
# a model that reasons with no variants listed (a config entry's `reasoning: true`), wb's own
# OFF_VARIANT variant in the layer, as the provider speaks it (THINKING_OFF). A model whose
# variants have no off position never gets one that thinks more than its bare id (_no_off_listed,
# the #1018 review): a variant there can turn thinking on that the bare id leaves off. A GPU task
# gets none of this: it thinks as the user's OpenCode entry says, as before.
OFF_VARIANT = "wb-off"  # reserved: a user's own variant of that name loses to wb's in the layer
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")  # lowest first
# A body field (DeepSeek's `thinking`) on a catalog provider's package goes only in a V2 variant,
# `providers.<p>.models.<m>.variants: [{id, body}]`: a V1 variant keeps only keys the package has a
# setting for (the spike, #1009; checked again on OpenCode 2.3.2, 2026-11-23: a V1 `thinking` on
# the catalog's deepseek-flash left the agent's request without it). And a document holding a
# provider in both forms loses the V1 one (`provider.deepseek.options.baseURL` beside
# `providers.deepseek` went unread), so that provider's whole entry moves to V2 with it (_as_v2).
# A config provider on an npm package (@ai-sdk/openai-compatible aimed at DeepSeek's host) sends
# a V1 variant's keys in the body as they are, so its body field stays V1 (_off_form): moved to
# V2 whole, its `npm` was no package to V2 and the run failed "Unsupported package" (2026-11-23).
# Settings stay V1, as the spike checked them. The title request carries `thinking: disabled` of
# OpenCode's own whatever the variant: a check of wb-off reads the agent's request (with tools).
THINKING_OFF = {
    "deepseek": {"body": {"thinking": {"type": "disabled"}}},
    "openrouter": {"settings": {"reasoning": {"enabled": False}}},
    "anthropic": {"settings": {"thinking": {"type": "disabled"}}},
    "openai": {"settings": {"reasoningEffort": "none"}},  # every OpenAI-compatible package
}
SPEAKS_HOST = {"deepseek": "deepseek.com", "openrouter": "openrouter.ai", "anthropic": "anthropic.com"}
THINKING_ON = "thinking on"
THINKING_NOT = "thinking off (the model doesn't reason)"
THINKING_OFF_LINE = "thinking off"
THINKING_LOWEST = "thinking at its lowest effort (it can't turn off)"
THINKING_STAYS = "thinking on (it can't turn off)"
THINKING_DEFAULT = "thinking as the model's default (it lists no lower setting)"
# The effort a provider runs a reasoning model at when the request names none (OpenAI's API and
# OpenRouter: medium; DeepSeek: high), which a listed effort must be under to be picked. None for
# Anthropic: a bare id there may not think at all (_no_off_listed).
DEFAULT_EFFORT = {"openai": "medium", "openrouter": "medium", "deepseek": "high"}
CATALOG_PACKAGE = "@opencode/ai/providers/"  # a catalog provider's package (keyhole.PACKAGE_PREFIX)


def speaks(m) -> str:
    """What the model's provider speaks, for THINKING_OFF: its package (the catalog's, or the
    config's npm), else its base URL's host; anything else is OpenAI-compatible."""
    from urllib.parse import urlparse
    pkg = str(m.package or m.config.get("npm") or "").lower()
    host = (urlparse(m.base_url or "").hostname or "").lower()
    return next((k for k, dom in SPEAKS_HOST.items()
                 if k in pkg or host == dom or host.endswith("." + dom)), "openai")


def _listed_variants(cfg: dict) -> list[dict]:
    """The model's own variants as the catalog lists them, [{id, settings | body}]; a config
    entry's V1 `{name: settings}` read the same way."""
    v = cfg.get("variants")
    if isinstance(v, dict):
        v = [{"id": k, "settings": s} for k, s in v.items() if isinstance(s, dict)]
    return [x for x in v if isinstance(x, dict) and isinstance(x.get("id"), str)
            and x["id"] != OFF_VARIANT] if isinstance(v, list) else []


def _turns_off(v: dict) -> bool:
    if v["id"] == "none":
        return True
    s = {**(v.get("settings") if isinstance(v.get("settings"), dict) else {}),
         **(v.get("body") if isinstance(v.get("body"), dict) else {})}
    r, t = s.get("reasoning"), s.get("thinking")
    return ("none" in (s.get("reasoningEffort"), s.get("reasoning_effort"))
            or isinstance(r, dict) and (r.get("enabled") is False or r.get("effort") == "none")
            or isinstance(t, dict) and t.get("type") == "disabled")


def _lowest(ids: list[str]) -> str:
    """The lowest effort of these (EFFORTS' order; a name it doesn't know ranks last, the first
    listed of equals first)."""
    return min(ids, key=lambda i: EFFORTS.index(i) if i in EFFORTS else len(EFFORTS))


def _thinking(m, thinking: str) -> tuple[str | None, str, dict | None]:
    """(variant or None, the line that says it, wb's OFF_VARIANT variant or None)."""
    cfg = m.config if isinstance(m.config, dict) else {}
    caps = cfg.get("capabilities") if isinstance(cfg.get("capabilities"), dict) else {}
    rcap = caps.get("reasoning")
    if cfg.get("reasoning") is False or isinstance(rcap, dict) and rcap.get("supported") is False:
        return None, THINKING_NOT, None
    listed = _listed_variants(cfg)
    if thinking == "on":
        return _thinking_on(speaks(m), listed)
    if off := next((v["id"] for v in listed if _turns_off(v)), None):
        return off, THINKING_OFF_LINE, None
    kind = speaks(m)
    if listed:  # a switch with no off position
        return _no_off_listed(kind, listed)
    if isinstance(rcap, dict) and not rcap.get("toggle"):  # reasons, can't be turned off
        efforts = [e for e in rcap.get("effort") or [] if isinstance(e, str)]
        if kind == "openrouter":
            return OFF_VARIANT, THINKING_LOWEST, {"settings": {"reasoning": {"effort": "minimal"}}}
        if efforts:
            return OFF_VARIANT, THINKING_LOWEST, {"settings": {"reasoningEffort": _lowest(efforts)}}
        return None, THINKING_STAYS, None
    if cfg.get("reasoning") is True or isinstance(rcap, dict):
        return OFF_VARIANT, THINKING_OFF_LINE, _off_form(m, THINKING_OFF[kind])
    # OpenCode lists variants for every model its catalog says reasons: none, it doesn't
    return None, THINKING_NOT, None


def _thinking_on(kind: str, listed: list[dict]) -> tuple[str | None, str, None]:
    """Thinking on: a listed effort, never the bare id where one is listed. The bare id is the
    provider's default, and not every provider's default thinks: a DeepInfra model
    gave 0 reasoning tokens bare and reasoned at `#high` (2026-11-23, the API calibration), while
    the start line said `thinking on`. The provider's default effort (DEFAULT_EFFORT; `high` where
    it names none) when listed, else the lowest listed above it, else the highest listed; with no
    effort listed, the bare id, as before."""
    efforts = [v["id"] for v in listed if v["id"] in EFFORTS[2:] and not _turns_off(v)]
    if not efforts:
        return None, THINKING_ON, None
    want = EFFORTS.index(DEFAULT_EFFORT.get(kind, "high"))
    above = [e for e in efforts if EFFORTS.index(e) >= want]
    pick = _lowest(above) if above else max(efforts, key=EFFORTS.index)
    return pick, f"{THINKING_ON} (effort {pick})", None


def _no_off_listed(kind: str, listed: list[dict]) -> tuple[str | None, str, None]:
    """A model whose listed variants can't turn thinking off: never one that thinks more than
    the bare id. Anthropic's manual-thinking models (every variant `thinking.type: enabled`, a
    `budgetTokens`) don't think on the bare id, and OpenCode sends them no `thinking` (checked
    2026-11-23, OpenCode 2.3.2): off as they are. Its adaptive ones may not either (the 4.6-4.8
    line; others think on the bare id and refuse `disabled`, and the catalog doesn't say which):
    the bare id, whose `#low` turned thinking on. Elsewhere the lowest listed effort when it is
    under the provider's default (DEFAULT_EFFORT), else the bare id."""
    if kind == "anthropic":
        types = {(v.get("settings") or {}).get("thinking", {}).get("type") for v in listed
                 if isinstance(v.get("settings"), dict) and isinstance(v["settings"].get("thinking"), dict)}
        return (None, THINKING_OFF_LINE if types == {"enabled"} else THINKING_DEFAULT, None)
    efforts = [v["id"] for v in listed if v["id"] in EFFORTS]
    default = DEFAULT_EFFORT.get(kind)
    if efforts and default and EFFORTS.index(low := _lowest(efforts)) < EFFORTS.index(default):
        return low, THINKING_LOWEST, None
    return None, THINKING_DEFAULT, None


def _off_form(m, off: dict) -> dict:
    """THINKING_OFF's variant for this model: a body field as a V1 setting on a provider of the
    user's config (an npm package, which sends V1 keys in the body), else as it is."""
    pkg = str(m.package or m.config.get("npm") or "")
    if "body" in off and pkg and not pkg.startswith(CATALOG_PACKAGE):
        return {"settings": off["body"]}
    return off


def thinking_variant(m, thinking: str) -> tuple[str | None, str]:
    """The variant that gives an API model this thinking ("off" or "on"), or None for the
    model's default, and the line that says what it does (task.json `thinking_shown`)."""
    variant, shown, _ = _thinking(m, thinking)
    return variant, shown


def with_thinking(m, spec: dict) -> dict:
    """task.json with an API task's thinking fixed (cmd_start, once): `thinking` (task.json's,
    which the role sets; default off), `thinking_shown`, `opencode_model` with the variant, and
    `thinking_off`, wb's own variant when one goes in the layer. A GPU task's is left as it was."""
    if getattr(m, "kind", "gpu") != "api":
        return spec
    thinking = "on" if spec.get("thinking") == "on" else "off"
    variant, shown, off = _thinking(m, thinking)
    return {**spec, "opencode_model": f"{m.id}#{variant}" if variant else m.id,
            "thinking": thinking, "thinking_shown": shown, **({"thinking_off": off} if off else {})}


def _as_v2(doc: dict, pid: str, mid: str, variant: dict) -> dict:
    """``doc`` with provider ``pid``'s V1 entry moved to the V2 form (options as settings, a V1
    variants object as a list, `npm` as `package`) and ``variant`` added to model ``mid``'s variants."""
    doc = dict(doc)
    v1 = dict(((doc.get("provider") or {}).get(pid)) or {})
    rest = {k: v for k, v in (doc.get("provider") or {}).items() if k != pid}
    v2 = models._merge(((doc.get("providers") or {}).get(pid)) or {},
                       {**{k: v for k, v in v1.items() if k not in ("options", "models", "npm")},
                        # V2 names it `package`: a V2 `npm` failed "Unsupported package" (2026-11-23)
                        **({"package": v1["npm"]} if "npm" in v1 else {}),
                        **({"settings": v1["options"]} if isinstance(v1.get("options"), dict) else {})})
    found = dict(v2.get("models") or {})
    for name, entry in (v1.get("models") or {}).items():
        entry = dict(entry) if isinstance(entry, dict) else {}
        if isinstance(entry.get("options"), dict):
            entry["settings"] = entry.pop("options")
        # a V1 model's own base (`provider.api`, keyhole_overlay's) is a V2 model's
        # settings.baseURL, as keyhole_v2 says it: moved as it was, the keyhole's base would be
        # a key V2 doesn't read (#1015 with #1018)
        if isinstance(entry.get("provider"), dict) and "api" in entry["provider"]:
            base = entry.pop("provider")["api"]
            entry["settings"] = {"baseURL": base, **(entry.get("settings") or {})}
        if isinstance(entry.get("variants"), dict):
            entry["variants"] = [{"id": k, "settings": s} for k, s in entry["variants"].items()]
        found[name] = models._merge(found.get(name) or {}, entry)
    mine = dict(found.get(mid) or {})
    mine["variants"] = [v for v in mine.get("variants") or []
                        if isinstance(v, dict) and v.get("id") != OFF_VARIANT] + [variant]
    found[mid] = mine
    doc["providers"] = {**(doc.get("providers") or {}), pid: {**v2, "models": found}}
    if rest:
        doc["provider"] = rest
    else:
        doc.pop("provider", None)
    return doc


def thinking_layer(doc: dict, spec: dict) -> dict:
    """The config layer with the task's OFF_VARIANT variant (task.json `thinking_off`) under its
    model, when its opencode_model names it: a settings variant in V1 form, a body one in V2."""
    off = spec.get("thinking_off")
    model, _, variant = str(spec.get("opencode_model") or "").partition("#")
    if variant != OFF_VARIANT or not isinstance(off, dict) or "/" not in model:
        return doc
    pid, _, mid = model.partition("/")
    if "body" in off:
        return _as_v2(doc, pid, mid, {"id": OFF_VARIANT, **off})
    return models._merge(doc, {"provider": {pid: {"models": {mid: {"variants": {
        OFF_VARIANT: off.get("settings") or {}}}}}}})


def restate_thinking(layer: dict, doc: dict) -> dict:
    """OPENCODE_CONFIG_CONTENT (``doc``) with the layer's OFF_VARIANT variant said again, as the
    model's pins are (rules_env): the task's model is the layer's small_model. Its provider goes
    to V2 here too when the layer's did, keeping only its timeouts (never a key or baseURL)."""
    pid, _, mid = str(layer.get("small_model") or "").partition("#")[0].partition("/")
    v1 = ((((layer.get("provider") or {}).get(pid) or {}).get("models") or {}).get(mid) or {}).get("variants")
    if isinstance(v1, dict) and OFF_VARIANT in v1:
        return models._merge(doc, {"provider": {pid: {"models": {mid: {"variants": {OFF_VARIANT: v1[OFF_VARIANT]}}}}}})
    v2 = (layer.get("providers") or {}).get(pid) or {}
    mine = [v for v in ((v2.get("models") or {}).get(mid) or {}).get("variants") or []
            if isinstance(v, dict) and v.get("id") == OFF_VARIANT]
    if not mine:
        return doc
    doc = _as_v2(doc, pid, mid, mine[0])
    settings = v2.get("settings") if isinstance(v2.get("settings"), dict) else {}
    if timeouts := {k: settings[k] for k in PROVIDER_TIMEOUTS if k in settings}:
        doc["providers"][pid]["settings"] = {**(doc["providers"][pid].get("settings") or {}), **timeouts}
    return doc


if __name__ == "__main__":
    main()
