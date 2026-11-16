"""`wb bench`: measure the local models, so the router can choose by evidence.

Two kinds of measurement, both appended to bench.jsonl with the model's weights fingerprint:

- probe: straight at the model's server. Cold load time, prefill and decode speed, and whether it
  makes correct tool calls.
- task: a graded coding task from bench/tasks, run through the real pipeline (worktree, OpenCode,
  test gate, auto-fix) exactly as a delegated task would be. The agent sees the spec and a
  visible test; it passes only if the task's hidden tests pass on what it left behind.

Bench tasks are marked in their task.json: they never notify Claude and never reach results.jsonl.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import gitops, models, store

PKG_ROOT = Path(__file__).resolve().parent.parent
TASKS = PKG_ROOT / "bench" / "tasks"
TASK_TIMEOUT = 45 * 60

TOOL_CASES = [
    ("What is the weather in Suva right now?", "get_weather", {"city": "Suva"}),
    ("Show me the first lines of src/app.py.", "read_file", {"path": "src/app.py"}),
    ("Multiply 37 by 43 with the calculator.", "calculate", {"a": 37, "b": 43, "op": "*"}),
]
TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "Current weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file from the project",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "calculate", "description": "Apply an arithmetic operator to two numbers",
        "parameters": {"type": "object", "properties": {
            "a": {"type": "number"}, "b": {"type": "number"},
            "op": {"type": "string", "enum": ["+", "-", "*", "/"]}},
            "required": ["a", "b", "op"]}}},
]


def suite() -> dict[str, dict]:
    """The bench tasks by name: their task.json, plus where they live."""
    out = {}
    for d in sorted(p for p in TASKS.iterdir() if (p / "task.json").exists()):
        out[d.name] = {**json.loads((d / "task.json").read_text()), "dir": d}
    return out


def _record(row: dict) -> dict:
    row = {"ts": store.now(), **row}
    store.append_jsonl(store.home() / "bench.jsonl", row)
    return row


def _chat(m: models.Model, body: dict, timeout: float = 900) -> tuple[dict, float]:
    req = urllib.request.Request(f"{m.base_url.rstrip('/')}/chat/completions",
                                 data=json.dumps({"model": m.name, **body}).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), time.time() - t


def _evict_all(say):
    """Nothing on the GPU, so the next load is a cold one."""
    for other in models.resident():
        if other.engine == "server" and other.stop:
            say(f"stopping {other.id}")
            subprocess.run(other.stop, shell=True, capture_output=True, timeout=300)
    for root in {o.root for o in models.discover().values() if o.engine == "ollama"}:
        for name in models._ollama_loaded(root):
            models._post(f"{root}/api/generate", {"model": name, "keep_alive": 0}, timeout=120)


def probe(m: models.Model, say=print) -> dict:
    """Load time, speeds and tool calls, measured directly. Holds the GPU lock throughout."""
    with store.locked(store.home() / "gpu.lock"):
        _evict_all(say)
        load_s = models.make_resident(m, say=say)
        _chat(m, {"messages": [{"role": "user", "content": "Reply with just: ok"}],
                  "max_tokens": 8})  # first request after a load can carry setup costs
        u, dt = {}, 0.0
        r, dt = _chat(m, {"messages": [{"role": "user", "content": (
            "Write a Python function that parses ISO 8601 durations like P1DT2H30M into "
            "seconds, with asserts. Code only.")}], "max_tokens": 400, "temperature": 0.2})
        u = r.get("usage", {})
        decode = u.get("completion_tokens", 0) / dt if dt else None
        filler = (PKG_ROOT / "workbench" / "runner.py").read_text()[:9000]  # ~2.5k fresh tokens
        r, dt = _chat(m, {"messages": [{"role": "user", "content":
                                        "Summarize this file in one sentence:\n\n" + filler}],
                          "max_tokens": 1})
        p_tok = r.get("usage", {}).get("prompt_tokens", 0)
        ok = 0
        for prompt, name, want in TOOL_CASES:
            r, _ = _chat(m, {"messages": [{"role": "user", "content": prompt}], "tools": TOOLS})
            calls = (r["choices"][0]["message"].get("tool_calls") or [])
            try:
                fn = calls[0]["function"]
                args = json.loads(fn["arguments"] or "{}")
                ok += fn["name"] == name and all(args.get(k) == v for k, v in want.items())
            except (IndexError, KeyError, ValueError):
                pass
        fp = models.fingerprint(m)
    return _record({"kind": "probe", "model": m.id, "fingerprint": fp, "load_s": round(load_s, 1),
                    "decode_tps": round(decode, 1) if decode else None,
                    "prefill_tps": round(p_tok / dt) if dt else None,
                    "tools_ok": ok, "tools_n": len(TOOL_CASES)})


def _grade(task: dict, worktree: Path) -> tuple[bool, str]:
    for f in (task["dir"] / "hidden").iterdir():
        shutil.copy(f, worktree / f.name)
    r = subprocess.run(task["hidden"], shell=True, cwd=worktree, capture_output=True, text=True,
                       timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    lines = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, lines[-1] if lines else ""


def run_task(m: models.Model, name: str, task: dict, say=print) -> dict:
    """One bench task end to end: seed repo, `wb start`, wait, grade on the hidden tests."""
    from . import cli  # cli imports this module; import here to keep the cycle out of import time
    stamp = time.strftime("%m%d%H%M%S")
    task_name = f"bench-{name}-{stamp}"[:48]
    repo = store.home() / "bench" / "repos" / task_name
    shutil.copytree(task["dir"] / "repo", repo)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=wb-bench", "-c", "user.email=bench@localhost",
                  "commit", "-qm", "bench seed"]):
        gitops.git(repo, *args)
    spec = task["dir"] / "spec.md"
    cli.main(["start", task_name, "--spec", str(spec), "--repo", str(repo), "--model", m.id,
              "--size", task["size"], "--test", task["test"], "--bench", "--force"])
    t = store.Task(task_name)
    deadline = time.time() + TASK_TIMEOUT
    while True:
        st = t.status
        if st.get("phase") in store.NEEDS_CLAUDE and not cli.runner_alive(t):
            break
        if time.time() > deadline:
            cli.main(["stop", task_name])
            break
        time.sleep(3)
    st = t.status
    passed, tail = _grade(task, t.worktree) if t.worktree.exists() else (False, "no worktree")
    row = _record({
        "kind": "task", "model": m.id, "fingerprint": models.fingerprint(m) or st.get("fingerprint"),
        "task": name, "size": task["size"], "passed": passed, "hidden": tail,
        "visible_ok": (st.get("tests") or {}).get("ok"), "phase": st.get("phase"),
        "rounds": st.get("round", 1), "minutes": round((time.time() - t.spec["created_at"]) / 60, 2),
        "tok_in": st.get("tok_in", 0), "tok_out": st.get("tok_out", 0)})
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet):
        cli.main(["discard", task_name, "--reason", "bench"])
    shutil.rmtree(repo, ignore_errors=True)
    return row


# How long an agent may generate without a tool call or a word before the runner nudges it: the
# time to write THINK_TOKENS at the model's measured decode speed plus to read a full context at
# its measured prefill speed. A plan that size is already more than any step needs; past it the
# model is circling (seen 2026-11-16: 20K-token plans and no edits, twice in a row).
THINK_TOKENS = 12000
THINK_DEFAULT_S, THINK_MIN_S, THINK_MAX_S = 480, 180, 900


def speeds(model_id: str) -> dict:
    """The model's latest probe (decode_tps, prefill_tps), or {} before any benchmark."""
    rows, _ = store.read_jsonl(store.home() / "bench.jsonl")
    mine = [r for r in rows if r["model"] == model_id]
    if not mine:
        return {}
    fp = mine[-1].get("fingerprint")
    return next((r for r in reversed(mine) if r["kind"] == "probe" and r.get("fingerprint") == fp), {})


def think_budget(model_id: str, window: int = 32768) -> int:
    p = speeds(model_id)
    decode, prefill = p.get("decode_tps"), p.get("prefill_tps")
    if not decode:
        return THINK_DEFAULT_S
    secs = THINK_TOKENS / decode + (window / prefill if prefill else 0)
    return int(min(THINK_MAX_S, max(THINK_MIN_S, secs)))


def table() -> str:
    """The latest measurements per model: speeds, then hidden-test pass rates per size."""
    rows, _ = store.read_jsonl(store.home() / "bench.jsonl")
    if not rows:
        return "no benchmarks yet: wb bench"
    out = [f"{'model':34} {'load':>6} {'prefill':>8} {'decode':>7} {'tools':>5}  "
           f"{'trivial':>9} {'significant':>11}  {'min/task':>8}"]
    for mid in dict.fromkeys(r["model"] for r in rows):
        mine = [r for r in rows if r["model"] == mid]
        fp = mine[-1].get("fingerprint")
        mine = [r for r in mine if r.get("fingerprint") == fp]
        p = next((r for r in reversed(mine) if r["kind"] == "probe"), {})
        tasks = [r for r in mine if r["kind"] == "task"]

        def rate(size):
            ts = [r for r in tasks if r["size"] == size]
            return f"{sum(r['passed'] for r in ts)}/{len(ts)}" if ts else "-"
        mins = [r["minutes"] for r in tasks]
        out.append(
            f"{mid:34} {p.get('load_s', '-'):>6} {p.get('prefill_tps', '-'):>8} "
            f"{p.get('decode_tps', '-'):>7} "
            f"{(str(p['tools_ok']) + '/' + str(p['tools_n'])) if p else '-':>5}  "
            f"{rate('trivial'):>9} {rate('significant'):>11}  "
            f"{(sum(mins) / len(mins)) if mins else 0:>8.1f}")
    return "\n".join(out)
