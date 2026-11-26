"""The driver, the model with the final say, on the OpenCode path (#1316, #1317).

The driver is whatever an OpenCode session runs on, picked in OpenCode's own /models (variants
included) or set by the `workbench` agent's `model`; wb doesn't own the choice. The plugin's
server part records it: every 30 s it lists its store's sessions and hands them to `wb driver
beat` (write_beat), each with the model its session was set to or last asked (the `model.request`
hook, which sees the agent's own model too) and when (`used`). That is the heartbeat store.py
reads (ARCHITECTURE.md, *The heartbeat*): who owns a task, and which models a live driver holds
on the GPU (store.driver_models, #1317).

What wb says of a driver (`wb driver`, the plugin's RPC `driver` method, the pane's label): its
model, GPU or API, routes.json's standing pin (`wb route driver`), and three notes, each said once
(`--once`, recorded in drivers/said.json) and never as a refusal, since wb can't make a weak
model hold the bar:
- own: the driver is the agents' own model (same id or the same weights): its review isn't
  independent, and the handoff saves nothing (also in `wb doctor`, every time);
- local: an API driver in a local_only repo: local_only keeps API models off the agents only, so
  the code reaches the API through the driver;
- small: a small window: the /workbench procedure alone takes a large share of it.
Under Claude Code the driver is Claude Code, fixed: nothing here applies."""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from . import models, routes, store

SAID = "said.json"   # in $WB_HOME/drivers/: {note key: when it was said}
SAID_KEEP = 400      # the newest kept: a key per model (and repo), so a few at most a month
CLAUDE_CODE = "Claude Code (fixed)"
MODEL_MAX = 256      # an OpenCode id longer than this isn't one the plugin should hand in
SKILL = Path(__file__).resolve().parent.parent / "skill" / "workbench" / "SKILL.md"
BYTES_PER_TOKEN = 4  # a rough guess, as the procedure's size is said: about, never exactly
# What a Workbench session's every request carries beside SKILL.md: OpenCode's own prompt and
# tools and the agent's additions (skill.ts). Measured on OpenCode 2.4.1 with a 32K Ollama model:
# the first request 27,822 input tokens, SKILL.md 16,860 (#1487, 2026-11-26).
DRIVER_BASE_TOKENS = 11_000


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def write_beat(doc) -> Path:
    """`wb driver beat`: the plugin's heartbeat (stdin's JSON: {pid, store?, ts, sessions: {<id>:
    {model?, variant?, dir?, used?}}}) as `drivers/opencode-<machine_tag>-<pid>.json`, this
    machine's `machine` added and every row checked, an odd one dropped. ValueError when the
    beat can't be one: `pid` must be this call's own OpenCode server, a process it runs under
    (store._lineage), so no other process's beat is ever written, nor one for a pid that isn't
    running; `ts` a time (when its listing began); `store` an absolute path, or left out (a
    listing that couldn't take the whole store: its sessions can say live, never gone)."""
    if not isinstance(doc, dict):
        raise ValueError("a beat is a JSON object")
    pid, ts, sessions = doc.get("pid"), doc.get("ts"), doc.get("sessions")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 2 or pid not in store._lineage():
        raise ValueError(f"pid {pid!r} isn't an OpenCode server this wb runs under")
    if not _num(ts) or ts > time.time() + 300:
        raise ValueError(f"ts {ts!r} isn't a time")
    if not isinstance(sessions, dict):
        raise ValueError("sessions isn't an object")
    rows: dict = {}
    for sid, row in sessions.items():
        if not isinstance(sid, str) or not store.SESSION_RE.fullmatch(sid) or not isinstance(row, dict):
            continue
        out: dict = {}
        if isinstance(m := row.get("model"), str) and len(m) <= MODEL_MAX and routes.driver_ref(m) \
                and "#" not in m:
            out["model"] = m
            if isinstance(v := row.get("variant"), str) and v and len(v) <= MODEL_MAX and "#" not in v:
                out["variant"] = v
        if isinstance(d := row.get("dir"), str) and os.path.isabs(d) and len(d) <= 4096:
            out["dir"] = d
        if _num(u := row.get("used")) and "model" in out:
            out["used"] = u
        rows[sid] = out
    st = doc.get("store")
    beat = {"tool": "opencode", "pid": pid, "machine": store.machine(),
            **({"store": st} if isinstance(st, str) and os.path.isabs(st) else {}),
            "ts": ts, "sessions": rows}
    path = store.beat_path(pid)
    store.write_json(path, beat)
    return path


def session_row(session: str) -> dict:
    """What the newest fresh beat listing ``session`` says of it ({model?, variant?, dir?,
    used?}), {} when none does (no plugin, a stalled service, another machine's session)."""
    state, hb = store.driver_server(session)
    row = hb["sessions"].get(session) if state == "live" and hb else None
    return dict(row) if isinstance(row, dict) else {}


def selected(row: dict, text: str) -> dict:
    """``row`` (a beat's) with the model the session runs on next, ``text`` (`provider/id#variant`):
    the TUI's own selection (`wb driver --model`), which OpenCode commits to the session only at
    its next prompt, so a /models pick and a new session's model reach no beat before then (seen
    live, 2026-11-26). The row's `dir` is kept, and its `used` only while it names that model."""
    parsed = routes.driver_ref(text)
    if parsed is None or len(text) > MODEL_MAX:
        raise ValueError(f"{text!r} isn't an OpenCode model id")
    provider, mid, variant = parsed
    model = f"{provider}/{mid}"
    out = {k: row[k] for k in ("dir",) if k in row}
    if row.get("model") == model and "used" in row:
        out["used"] = row["used"]
    return {**out, "model": model, **({"variant": variant} if variant else {})}


def ref(row: dict) -> str:
    """A row's driver in OpenCode's string form, `provider/id#variant`; "" for none."""
    m = row.get("model") or ""
    return f"{m}#{row['variant']}" if m and row.get("variant") else m


def _model(mid: str):
    """(Model or None, kind): a GPU model discover() finds (`gpu`), an API one OpenCode's catalog
    lists (`api`: the cached one first, else a read, models.lookup's), else (None, `unknown`): a
    provider wb doesn't read, or one not listed now."""
    if not mid:
        return None, "unknown"
    if m := models.discover().get(mid):
        return m, "gpu"
    m = models.lookup(mid, cached=True) or models.lookup(mid)
    return (m, "api") if m is not None and m.kind == "api" else (None, "unknown")


def _window(m) -> int | None:
    n = ((m.config or {}).get("limit") or {}).get("context") if m is not None else None
    return int(n) if _num(n) and n > 0 else None


def _input_window(m) -> int:
    """The input window OpenCode compacts against (core/src/session/compaction.ts inputWindow,
    opencode-plugin/upkeep.ts's twin): `limit.input` and `limit.context`, the smaller of two."""
    limit = ((m.config or {}).get("limit") or {}) if m is not None else {}
    c, i = (int(v) if _num(v) and v > 0 else 0 for v in (limit.get("context"), limit.get("input")))
    return min(i, c) if i and c else i or c


def auto_ceiling(window: int) -> int:
    """OpenCode's automatic compaction point for an input window (upkeep.ts `autoCeiling`, its
    calculateCeiling with no `buffer`): 15% kept free, at least 16K when the window holds 32,000."""
    if window <= 0:
        return 0
    return window - max(window * 15 // 100, 16_000 if window >= 32_000 else 0)


def agents_models(doc: dict | None = None) -> dict[str, list[str]]:
    """The models the agents run on now, each with its roles: a role on API its API model, on
    the GPU its pin, else the router's pick for its size (router.choose, as wb start's). Empty
    when routes.json can't be read (its own error is wb route's to say)."""
    from . import router  # router imports models; only here
    try:
        doc = doc or routes.read()
    except routes.RoutesError:
        return {}
    out: dict[str, list[str]] = {}
    for r in routes.ROLES:
        row = doc["roles"][r]
        if routes.effective_via(doc, r) == "api":
            mid = row.get("api")
        elif row["gpu"] != routes.AUTO:
            mid = row["gpu"]
        else:
            top, _ = router.choose(routes.role_and_size(r, None)[1], role=r)
            mid = top.model.id if top else None
        if mid:
            out.setdefault(mid, []).append(r)
    return out


def _own(mid: str, m, kind: str) -> list[str]:
    """The roles whose agents run on the driver's own model: the same id, or a GPU model of the
    same weights (models.identity: an alias of the same file)."""
    found = agents_models()
    if mid in found:
        return found[mid]
    me = models.identity(m) if kind == "gpu" else None
    if not me:
        return []
    gpu = models.discover()
    return [r for aid, roles in found.items() if (a := gpu.get(aid)) and models.identity(a) == me
            for r in roles]


def skill_tokens() -> int:
    try:
        return SKILL.stat().st_size // BYTES_PER_TOKEN
    except OSError:
        return 0


def notes(mid: str, m, kind: str, where: str | None) -> list[dict]:
    """The driver's notes ({key, text}), each keyed by what it is said of (the model, and the
    repo for `local`), so --once says it once."""
    from . import context, privacy  # both import much of the package; only here
    out = []
    if roles := _own(mid, m, kind):
        out.append({"key": f"own:{mid}", "text": (
            f"the driver {mid} is also the agents' model ({', '.join(roles)}): its review isn't "
            "independent, and handing it the work saves nothing; another driver in /models, or "
            "another model for the role (wb route), keeps the two apart")})
    if where and kind != "gpu":
        try:
            kept = privacy.local_only_place(where)
        except (OSError, RuntimeError):
            kept = None
        if kept:
            who = models.provider_name(m) if m is not None else mid.split("/", 1)[0]
            out.append({"key": f"local:{mid}:{kept}", "text": (
                f"{kept} is kept local (local_only), which keeps API models off the agents only: "
                f"the driver {mid} isn't a GPU model wb finds here, so this conversation, the code "
                f"in it included, goes to {who}; a GPU driver in /models keeps it on this machine")})
    window, skill = _window(m), skill_tokens()
    # #1487 (2026-11-26): a 32K or 40K local driver never answered in Workbench mode on OpenCode:
    # the procedure and OpenCode's own prompt are over its compaction point, so every request
    # compacts again; said plainly, as the old note's "reviews more at once" hid it
    fixed, ceiling = skill + DRIVER_BASE_TOKENS, auto_ceiling(_input_window(m))
    if skill and ceiling and fixed >= ceiling:
        out.append({"key": f"fit:{mid}", "text": (
            f"the driver {mid}'s window can't hold the Workbench procedure: OpenCode compacts at "
            f"about {round(ceiling / 1000)}K tokens, and the procedure with OpenCode's own prompt "
            f"takes about {round(fixed / 1000)}K of every request, so every request will compact "
            "and none will answer; pick a model with a 64K window or more in /models")})
    elif window and skill and skill >= context.FIXED_SHARE * window:
        out.append({"key": f"small:{mid}", "text": (
            f"the driver {mid} has a {window // 1024}K window, and the /workbench procedure alone "
            f"takes about {round(skill / 1000)}K tokens of it before any diff: a driver with a "
            "larger window in /models reviews more at once")})
    return out


def _said_path() -> Path:
    return store.home() / store.DRIVERS / SAID


def once(found: list[dict]) -> list[dict]:
    """The notes not said before, now recorded as said (drivers/said.json): each once, ever, per
    key. Two calls at once may both say one: a note twice, never a note lost."""
    if not found:
        return []
    said = store.read_json(_said_path(), {})
    said = said if isinstance(said, dict) else {}
    new = [n for n in found if n["key"] not in said]
    if new:
        now = time.time()
        said.update({n["key"]: now for n in new})
        keep = sorted(said.items(), key=lambda kv: kv[1] if _num(kv[1]) else 0)[-SAID_KEEP:]
        store.write_json(_said_path(), dict(keep))
    return new


def info(session: str, where: str | None = None, row: dict | None = None) -> dict:
    """What wb says of ``session``'s driver: {tool: opencode, session, model?, variant?, label,
    kind (gpu, api, unknown), window?, used?, pin (routes.json's `driver`, or session), notes}.
    ``where``: the session's directory (its beat's `dir`, else the caller's)."""
    row = session_row(session) if row is None else row
    mid = row.get("model") or ""
    m, kind = _model(mid)
    try:
        pin = routes.read().get("driver") or routes.DRIVER_SESSION
    except routes.RoutesError:
        pin = routes.DRIVER_SESSION
    label = ref(row) or "not known yet (the plugin records it at the session's next request)"
    out = {"tool": "opencode", "session": session,
           **{k: row[k] for k in ("model", "variant", "used") if k in row},
           "label": label, "kind": kind, "pin": pin,
           **({"window": w} if (w := _window(m)) else {}),
           "notes": notes(mid, m, kind, row.get("dir") or where) if mid else []}
    return out


def claude_info() -> dict:
    """Claude Code's driver: Claude, fixed (the OpenCode path alone picks one)."""
    return {"tool": "claude", "label": CLAUDE_CODE, "kind": "fixed", "notes": []}


def live() -> list[tuple[str, dict]]:
    """Every session a fresh beat says drove within store.DRIVER_HOLD, newest first, once each."""
    now, seen = time.time(), {}
    for hb in store.heartbeats():
        if not store._beating(hb):
            continue
        for sid, row in hb["sessions"].items():
            if isinstance(row, dict) and _num(row.get("used")) and now - row["used"] <= store.DRIVER_HOLD \
                    and row.get("model") and (sid not in seen or seen[sid]["used"] < row["used"]):
                seen[sid] = row
    return sorted(seen.items(), key=lambda kv: -kv[1]["used"])


def doctor_lines() -> list[str]:
    """wb doctor's driver lines (informing, never BAD): each live driver whose model the agents
    run on too (#1296's first note), said every time doctor runs."""
    out = []
    for sid, row in live():
        mid = row["model"]
        m, kind = _model(mid)
        if roles := _own(mid, m, kind):
            out.append(f"the OpenCode driver {ref(row)} (session {sid}) is also the agents' model "
                       f"({', '.join(roles)}): its review isn't independent, and handing it the work "
                       "saves nothing")
    return out
