"""Claude's skill library for the local agents.

Claude writes skills from what review teaches (a correction that will recur, a recipe the repo
can't tell an agent) and hands each task only the ones that fit it. A handed skill reaches
OpenCode through the task's config layer (`skills`: one path per skill directory, which lists
exactly that skill); everything else in the library stays invisible, so an agent's prompt never
fills with skills it doesn't need. Between rounds the set can change: a skill is taken back by
leaving it out of the next layer.

The library is a git repository of its own (`$WB_SKILLS`, default ~/.local/share/workbench/skills),
so every skill has a history saying why it was written or changed. It never lives in
~/.claude/skills, which is the user's. Retired skills move to `retired/` and can come back.
Usage (offered to a task, loaded by the agent, the task's outcome) comes from results.jsonl,
which is what tells Claude a skill is clutter or needs rewriting.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import store

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_DESCRIPTION = 300   # all an agent sees before it decides to load the skill
MAX_LINES = 120         # a loaded skill spends a 32K context; most should be 20-60 lines
MAX_BYTES = 8000


def library() -> Path:
    return Path(os.environ.get("WB_SKILLS") or Path.home() / ".local/share/workbench/skills")


def _git(*args: str) -> str:
    lib = library()
    if not (lib / ".git").exists():
        lib.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(lib), "init", "-q", "-b", "master"], check=True)
    r = subprocess.run(["git", "-C", str(lib), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout


def _commit(message: str) -> None:
    """Commit as the user; a machine without a git identity still gets its history."""
    who = [] if subprocess.run(["git", "-C", str(library()), "config", "user.email"],
                               capture_output=True).stdout.strip() else \
        ["-c", "user.name=workbench", "-c", "user.email=workbench@localhost"]
    r = subprocess.run(["git", "-C", str(library()), *who, "commit", "-q", "-m", message],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())


def parse(skill_md: Path) -> dict:
    """{name, description, body} from a SKILL.md (YAML-ish front matter, one line per key)."""
    text = skill_md.read_text()
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip().strip("\"'")
    return {"name": meta.get("name", ""), "description": meta.get("description", ""),
            "body": body, "text": text}


def check(skill_md: Path, name: str) -> tuple[list[str], list[str]]:
    """(problems that refuse it, warnings worth fixing) for a skill about to enter the library."""
    problems, warnings = [], []
    if not skill_md.is_file():
        return [f"no {skill_md}"], []
    s = parse(skill_md)
    if not NAME.match(name):
        problems.append(f"name {name!r}: lowercase words joined by hyphens")
    if s["name"] != name:
        problems.append(f"front matter name is {s['name']!r}, the directory is {name!r}")
    if not s["description"]:
        problems.append("no description: it is all the agent sees before deciding to load it")
    elif len(s["description"]) > MAX_DESCRIPTION:
        problems.append(f"description is {len(s['description'])} chars (max {MAX_DESCRIPTION})")
    if len(s["text"].encode()) > MAX_BYTES:
        problems.append(f"{len(s['text'].encode())} bytes (max {MAX_BYTES}): split it or cut it")
    lines = s["body"].count("\n") + 1
    if lines > MAX_LINES:
        warnings.append(f"{lines} lines: a loaded skill spends the agent's context; aim for 20-60")
    if re.search(r"(?<![\w.])(/home/|~/)", s["text"]):
        warnings.append("names a path outside the repo: agents go looking for such paths; "
                        "describe the thing instead")
    if s["description"] and not re.search(r"\b(when|for|before|after|if)\b", s["description"], re.I):
        warnings.append("the description should say when to use it (\"Use when ...\")")
    return problems, warnings


def available() -> dict[str, dict]:
    """The library's live skills: name -> {path, description}."""
    lib = library()
    out = {}
    if lib.is_dir():
        for md in sorted(lib.glob("*/SKILL.md")):
            out[md.parent.name] = {"path": md.parent, "description": parse(md)["description"]}
    return out


def resolve(names: list[str]) -> list[Path]:
    """The skill directories for a task's config layer; unknown names are an error."""
    have = available()
    missing = [n for n in names if n not in have]
    if missing:
        raise KeyError(", ".join(missing))
    return [have[n]["path"] for n in names]


def add(src: Path, name: str, message: str) -> tuple[str, list[str]]:
    """Copy a skill (a SKILL.md, or a directory holding one and its files) into the library and
    commit it. Returns ("added"|"updated", warnings); raises ValueError with the problems."""
    src_md = src / "SKILL.md" if src.is_dir() else src
    problems, warnings = check(src_md, name)
    if problems:
        raise ValueError("; ".join(problems))
    dest = library() / name
    verb = "updated" if dest.exists() else "added"
    _git("status")  # makes the repository on first use
    if src.is_dir():
        if dest.resolve() != src.resolve():
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    else:
        dest.mkdir(parents=True, exist_ok=True)
        if (dest / "SKILL.md").resolve() != src.resolve():
            shutil.copyfile(src, dest / "SKILL.md")
    _git("add", "-A", "--", name)
    if _git("status", "--porcelain", "--", name).strip():
        _commit(f"{name}: {message}")
    return verb, warnings


def retire(name: str, message: str) -> None:
    if name not in available():
        raise KeyError(name)
    (library() / "retired").mkdir(exist_ok=True)
    shutil.rmtree(library() / "retired" / name, ignore_errors=True)
    _git("add", "-A")
    _git("mv", name, f"retired/{name}")
    _commit(f"{name}: retired: {message}")


def restore(name: str) -> None:
    if not (library() / "retired" / name / "SKILL.md").is_file():
        raise KeyError(name)
    _git("mv", f"retired/{name}", name)
    _commit(f"{name}: restored")


def history(name: str, n: int = 5) -> list[str]:
    try:
        return [l for l in _git("log", f"-{n}", "--format=%ad %s", "--date=short", "--",
                                 name, f"retired/{name}").splitlines() if l]
    except RuntimeError:
        return []


def loaded(task: store.Task) -> list[str]:
    """The skills the agent actually loaded during the task, from its events."""
    rows, _ = store.read_jsonl(task.dir / "events.jsonl")
    seen = []
    for r in rows:
        if r.get("kind") == "tool" and r.get("tool") == "skill" and r.get("ok") and r.get("brief"):
            if r["brief"] not in seen:
                seen.append(r["brief"])
    return seen


def usage() -> dict[str, dict]:
    """Per skill, over finished real tasks: offered, loaded, accepted (of the tasks it was loaded
    in), last offered (ts)."""
    out: dict[str, dict] = {}
    for r in store.read_jsonl(store.home() / "results.jsonl")[0]:
        for n in r.get("skills_offered") or []:
            u = out.setdefault(n, {"offered": 0, "loaded": 0, "accepted": 0, "last": 0})
            u["offered"] += 1
            u["last"] = max(u["last"], r.get("ts", 0))
            if n in (r.get("skills_loaded") or []):
                u["loaded"] += 1
                u["accepted"] += r.get("outcome") == "accepted"
    return out


def advice(u: dict | None, now: float) -> str:
    """One line for Claude: is the skill pulling its weight?"""
    if not u or not u["offered"]:
        return "never offered yet"
    if u["offered"] >= 3 and not u["loaded"]:
        return "offered but never loaded: rewrite the description, or retire it"
    if u["loaded"] >= 3 and u["accepted"] / u["loaded"] < 0.5:
        return "loaded, yet most of those tasks failed: check it still says the right thing"
    if now - u["last"] > 60 * 86400:
        return "not offered in 60 days: retire it if it's no longer useful"
    return ""
