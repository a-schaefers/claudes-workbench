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
Usage (offered to a task, loaded by the agent, the task's outcome, and the skill's version at the
time) comes from results.jsonl, which is what tells Claude a skill is clutter, needs rewriting,
or stopped helping after an edit.

Everything that enters the library, written here or imported from outside, is linted (check)
and scanned (scan.py) on the way in. A skill from outside is fetched into the state directory
first and never goes in by itself: Claude reads it, adapts it and adds it like its own.
"""
from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import scan, store, tuning

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_DESCRIPTION = 300   # all an agent sees before it decides to load the skill
MAX_LINES = 120         # a loaded skill spends the agent's context; most should be 20-60 lines
MAX_BYTES = 8000
MAX_FILES = 10          # supporting files (references/, scripts/, templates/): depth, not a dump
SUPPORT = re.compile(r"(?<![\w/.-])((?:references|scripts|templates|assets)/[\w./-]*\w)")
INCIDENT = re.compile(r"(?:#|\bPR\s*|\bissue\s*)\d{2,}\b|\b20\d\d-\d\d-\d\d\b", re.I)
CLUTTER = {"README.md", "CHANGELOG.md", ".env", ".gitignore", "LICENSE.txt"}


def library() -> Path:
    return Path(os.environ.get("WB_SKILLS") or Path.home() / ".local/share/workbench/skills")


def _run_git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(library()), "-c", "log.showSignature=false", *args],  # its
                       capture_output=True, text=True)  # log lines are parsed (the 2026-11-16 audit)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout


def _git(*args: str) -> str:
    """git in the library, made on first use. Its branch is master, set after a plain init: git
    before 2.28 has no `init -b`, and the library was never made, every skill command ending in
    a traceback (#527)."""
    lib = library()
    if not (lib / ".git").exists():
        lib.mkdir(parents=True, exist_ok=True)
        _run_git("init", "-q")
        _run_git("symbolic-ref", "HEAD", "refs/heads/master")
    return _run_git(*args)


def rev(name: str) -> str:
    """The skill's version: the library commit that last changed it ("" before the first)."""
    try:
        return _git("log", "-1", "--format=%h", "--", name).strip()
    except RuntimeError:
        return ""


def revs(names: list[str]) -> dict[str, str]:
    return {n: rev(n) for n in names}


def _commit(message: str) -> None:
    """Commit as the user; a machine without a git identity still gets its history. Unsigned and
    without hooks: the library is workbench's own record, not one of the user's projects, and a
    signing prompt or a global hook made `wb skill add` fail from Claude's Bash (the 2026-11-16
    audit). gitops' commits land in the user's projects: they stay signed but run no hooks
    either (gitops.NO_HOOKS)."""
    who = [] if subprocess.run(["git", "-C", str(library()), "config", "user.email"],
                               capture_output=True).stdout.strip() else \
        ["-c", "user.name=workbench", "-c", "user.email=workbench@localhost"]
    r = subprocess.run(["git", "-C", str(library()), *who, "-c", "commit.gpgsign=false",
                        "-c", f"core.hooksPath={os.devnull}", "commit", "-q", "--no-verify", "-m", message],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())


def parse(skill_md: Path) -> dict:
    """{name, description, license, body} from a SKILL.md: YAML-ish front matter, top-level keys,
    a value folded from its indented lines (`description: >` as public skills often write it).
    A leading UTF-8 BOM (a Windows editor's) is dropped: before the `---` it hid the front matter,
    and the skill was refused for its name and description (#479, the audit of 1.8)."""
    text = skill_md.read_text(encoding="utf-8-sig")
    meta, body, key = {}, text, None
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if line[:1] in (" ", "\t"):
                if key:
                    meta[key] = f"{meta[key]} {line.strip()}".strip()
                continue
            k, sep, v = line.partition(":")
            key = k.strip() if sep else None
            if key:
                v = v.strip()
                meta[key] = "" if v in ("|", ">", "|-", ">-", "|+", ">+") else v.strip("\"'")
    return {"name": meta.get("name", ""), "description": meta.get("description", ""),
            "license": meta.get("license", ""), "body": body, "text": text}


def check(skill_md: Path, name: str) -> tuple[list[str], list[str]]:
    """(problems that refuse it, warnings worth fixing) for a skill about to enter the library."""
    problems, warnings = [], []
    if not skill_md.is_file():
        return [f"no {skill_md}"], []
    s = parse(skill_md)
    if not NAME.match(name):
        problems.append(f"name {name!r}: lowercase words joined by hyphens")
    elif name == "retired":
        problems.append("`retired` is where the library keeps retired skills; pick another name")
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
    if re.search(r"(?<![\w.])(/home/|/Users/|/root/|/var/home/|~/)", s["text"]):  # Linux, macOS, Silverblue
        warnings.append("names a path outside the repo: agents go looking for such paths; "
                        "describe the thing instead")
    if s["description"] and not re.search(r"\b(when|for|before|after|if)\b", s["description"], re.I):
        warnings.append("the description should say when to use it (\"Use when ...\")")
    # what a directory skill ships beside its SKILL.md
    root = skill_md.parent
    for ref in sorted(set(SUPPORT.findall(s["body"]))):
        if (root / ref).exists():
            continue
        # scripts/, templates/ and assets/ are ordinary project directories too: "put the page in
        # templates/polls/detail.html, run scripts/test.sh" was refused with no way in but
        # rewording (#738, the 2026-11-21 audit). Refused only where it must be the skill's own:
        # references/ (a skill's word), or a directory of that name the skill has.
        top = ref.split("/", 1)[0]
        if top == "references" or skill_md.name == "SKILL.md" and (root / top).is_dir():
            problems.append(f"names {ref}, which the skill doesn't have: the agent would go looking")
        else:
            warnings.append(f"names {ref}, which the skill doesn't have: if it is the skill's own "
                            "file, add it; if it is a path in the project, ignore this")
    if skill_md.name == "SKILL.md" and root.is_dir():
        extra = [p for p in root.rglob("*") if p.is_file() and p != skill_md
                 and ".git" not in p.relative_to(root).parts]
        clutter = sorted(p.name for p in extra if p.name in CLUTTER)
        if clutter:
            warnings.append(f"{', '.join(clutter)}: a skill's directory is for the agent; leave these out")
        if len(extra) > MAX_FILES:
            warnings.append(f"{len(extra)} supporting files (aim for at most {MAX_FILES}): keep "
                            "the depth an agent needs, by topic")
    if len(INCIDENT.findall(s["body"])) >= 3:
        warnings.append("reads like an incident log (issue numbers, dates): state the rule and why, "
                        "not the story")
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


def add(src: Path, name: str, message: str, source: str = "",
        reviewed: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """Copy a skill (a SKILL.md, or a directory holding one and its files) into the library and
    commit it. Returns ("added"|"updated", warnings); raises ValueError with the problems. The
    scan refuses a critical finding; on an update only what the change brought in counts.
    `source` (where an imported skill came from: url@commit, license) goes in the commit.
    `reviewed`: the scan's rules Claude has read the findings of and judged safe (`--reviewed`):
    those pass as warnings, recorded in the commit; a legitimate skill (rustup's install line,
    a Dockerfile) once had no way in (#455). Never one in scan.UNREVIEWABLE."""
    src_md = src / "SKILL.md" if src.is_dir() else src
    if src.resolve().is_relative_to(library().resolve()):  # it would be scanned against itself
        raise ValueError(f"{src} is in the library: edit a copy elsewhere and add that, or use "
                         "wb skill patch")
    problems, warnings = check(src_md, name)
    dest = library() / name
    found = scan.scan(src)
    if dest.exists():
        found = scan.new_findings(found, scan.scan(dest))
    bad = set(reviewed) & scan.UNREVIEWABLE
    if bad:
        raise ValueError(f"--reviewed can't pass {', '.join(sorted(bad))}: what it finds can't "
                         "be judged by reading the skill")
    passed = lambda f: f["severity"] == "critical" and f["rule"] in reviewed  # noqa: E731
    stopped = [(x, f) for x, f in zip(scan.lines(found), found)
               if f["severity"] == "critical" and not passed(f)]
    problems += [f"scan: {x}" for x, _ in stopped]
    warnings += [f"scan: {x}" for x, f in zip(scan.lines(found), found)
                 if f["severity"] != "critical"]
    passes = [x for x, f in zip(scan.lines(found), found) if passed(f)]
    warnings += [f"scan, reviewed: {x}" for x in passes]
    warnings += [f"--reviewed {r}: no such finding" for r in sorted(set(reviewed))
                 if not any(f["rule"] == r for f in found)]
    if problems:
        if any(f["rule"] not in scan.UNREVIEWABLE for _, f in stopped):
            problems.append("read each; if it is safe as written, --reviewed <rule> lets it in "
                            "and records that in the library")
        raise ValueError("; ".join(problems))
    if passes:
        message += "\n\nReviewed (the scan's critical findings, judged safe):\n" + \
            "\n".join(f"- {x}" for x in passes)
    verb = "updated" if dest.exists() else "added"
    _git("status")  # makes the repository on first use
    _put(src, dest)
    md = dest / "SKILL.md"
    if (raw := md.read_bytes()).startswith(codecs.BOM_UTF8):  # the library's copy without it (#479)
        md.write_bytes(raw[len(codecs.BOM_UTF8):])
    _git("add", "-A", "--", name)
    if _git("status", "--porcelain", "--", name).strip():
        _commit(f"{name}: {message}" + (f"\n\nSource: {source}" if source else ""))
    return verb, warnings


def _put(src: Path, dest: Path) -> None:
    """The skill's files at ``dest``, all or none: a directory is copied into a scratch directory
    in the library (`.adding-<name>-…`: on dest's file system, and no `*/SKILL.md` a skill is
    listed by) and renamed into place, a SKILL.md written beside its old one and renamed over
    it. Copied straight into the library, a link the copy couldn't follow left the skill half
    there, live but uncommitted, and an update's committed copy replaced by the partial one (#604,
    Claude's audit of 1.90). A failure raises OSError (shutil.Error), the old copy as it was."""
    lib = library()
    if not src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / f".SKILL.md.{os.getpid()}.tmp"
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest / "SKILL.md")
        finally:
            tmp.unlink(missing_ok=True)
        return
    staged = Path(tempfile.mkdtemp(prefix=f".adding-{dest.name}-", dir=lib))
    try:
        shutil.copytree(src, staged / "new", ignore=shutil.ignore_patterns(".git"))
        if dest.exists():
            os.rename(dest, staged / "old")
        try:
            os.rename(staged / "new", dest)
        except OSError:
            if (staged / "old").exists():
                os.rename(staged / "old", dest)
            raise
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def patch(name: str, old: str, new: str, message: str) -> list[str]:
    """Fix one passage of a skill in place (the sentence that misled, not an appended
    correction): `old` must occur exactly once in its SKILL.md. Checked, scanned and committed
    like add; returns the warnings."""
    have = available()
    if name not in have:
        raise KeyError(name)
    text = (have[name]["path"] / "SKILL.md").read_text()
    n = text.count(old)
    if n != 1:
        raise ValueError(f"the text to replace occurs {n} times in {name}'s SKILL.md; "
                         "quote exactly one passage (wb skill show " + name + ")")
    with tempfile.TemporaryDirectory() as tmp:
        draft = Path(tmp) / name
        shutil.copytree(have[name]["path"], draft)
        (draft / "SKILL.md").write_text(text.replace(old, new))
        return add(draft, name, message)[1]


def fetch(src: str, subdir: str | None = None) -> tuple[Path, dict, list[Path]]:
    """A skill from outside, for Claude to read: clone a git URL (owner/repo means GitHub) into
    the state directory's imports/, or take a local path as it is. Returns (the copy's root, its
    provenance {source, rev}, the skill directories in it, or just `subdir`)."""
    local = Path(src).expanduser()
    if local.exists():
        root, url = local.resolve(), str(local.resolve())
    else:
        url = src if "://" in src or src.startswith("git@") else f"https://github.com/{src}"
        slug = re.sub(r"[^\w.-]+", "-", url.split("://")[-1]).strip("-")[-60:]
        root = store.home() / "imports" / slug
        shutil.rmtree(root, ignore_errors=True)
        root.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--", url, str(root)],
                           capture_output=True, text=True, timeout=300,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode:
            raise RuntimeError(f"git clone {url}: {(r.stderr or r.stdout).strip()}")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    meta = {"source": url, "rev": head.stdout.strip() if head.returncode == 0 else ""}
    if subdir:
        found = [root / subdir] if (root / subdir / "SKILL.md").is_file() else []
    else:
        found = sorted(md.parent for md in root.rglob("SKILL.md")
                       if ".git" not in md.relative_to(root).parts)
    return root, meta, found


def license_of(skill: Path, root: Path) -> str:
    """What the skill says it may be used under: its front matter, else the nearest LICENSE file."""
    said = parse(skill / "SKILL.md")["license"]
    named = next((f for f in sorted(skill.glob("LICEN[CS]E*")) if f.name in said), None)
    if said and not named:
        return said
    for d in [skill, *skill.parents]:
        for f in [named] if named and d == skill else sorted(d.glob("LICEN[CS]E*")):
            heads = [l.strip() for l in f.read_text(errors="replace").splitlines() if l.strip()][:2]
            first = " ".join(heads) if heads and len(heads[0]) < 20 else (heads or [""])[0]
            return f"{first} ({f.relative_to(root)})" if first else str(f.relative_to(root))
        if d == root:
            break
    return "none found"


def retire(name: str, message: str) -> None:
    if name not in available():
        raise KeyError(name)
    (library() / "retired").mkdir(exist_ok=True)
    shutil.rmtree(library() / "retired" / name, ignore_errors=True)  # an older retired copy
    _git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", f"retired/{name}")  # only that
    _git("mv", name, f"retired/{name}")
    _commit(f"{name}: retired: {message}")


def restore(name: str) -> None:
    if not (library() / "retired" / name / "SKILL.md").is_file():
        raise KeyError(name)
    if (library() / name).exists():
        raise ValueError(f"{name} is in the library already: retire it first")
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


def _tally() -> dict:
    return {"n": 0, "accepted": 0, "first_try": 0, "reviews": 0}


def _count(t: dict, r: dict) -> None:
    t["n"] += 1
    t["accepted"] += tuning.credit(r)  # the agent's kept share: no takeover's (#647), landed steps (#722)
    t["first_try"] += tuning.agent_landed(r) and not tuning.agent_reviews(r)
    t["reviews"] += tuning.agent_reviews(r)  # a round the spec cost isn't the skill's


def usage(current: dict[str, str] | None = None) -> dict[str, dict]:
    """Per skill, over finished real tasks: how often it was offered and loaded, and how the
    tasks that loaded it went (`with`: accepted, accepted with no review round, review rounds)
    against tasks in the same repos that didn't load it (`without`). `since` counts the loads of
    its current version (`current`: name -> rev), so an edit can be judged on its own."""
    rows = [r for r in store.results() if tuning.judged(r)]
    out: dict[str, dict] = {}
    for r in rows:
        for n in r.get("skills_offered") or []:
            u = out.setdefault(n, {"offered": 0, "loaded": 0, "accepted": 0, "last": 0, "repos": set(),
                                   "with": _tally(), "without": _tally(), "since": _tally()})
            u["offered"] += 1
            u["last"] = max(u["last"], r.get("ts", 0))
            if n in (r.get("skills_loaded") or []):
                u["loaded"] += 1
                u["accepted"] += tuning.credit(r)
                u["repos"].add(r.get("repo"))
                _count(u["with"], r)
                if current and current.get(n) and (r.get("skills_rev") or {}).get(n) == current[n]:
                    _count(u["since"], r)
    for n, u in out.items():
        for r in rows:
            if r.get("repo") in u["repos"] and n not in (r.get("skills_loaded") or []):
                _count(u["without"], r)
    return out


def record_line(u: dict | None) -> str:
    """offered · loaded · how those went, against the same repos without it."""
    if not u:
        return "offered 0 · loaded 0"
    w, wo, s = u["with"], u["without"], u["since"]
    out = f"offered {u['offered']} · loaded {u['loaded']}"
    if w["n"]:
        out += (f" · {round(w['accepted'], 1):g} accepted, {w['first_try']} with no review round, "
                f"{w['reviews'] / w['n']:.1f} review rounds a task")
    if wo["n"]:
        out += (f" · same repos without it: {wo['accepted'] / wo['n']:.0%} accepted, "
                f"{wo['reviews'] / wo['n']:.1f} review rounds ({wo['n']} tasks)")
    if s["n"] and s["n"] < w["n"]:
        out += f" · since its last edit: {round(s['accepted'], 1):g} of {s['n']} accepted"
    if u["repos"]:
        out += f" · loaded in {len(u['repos'])} repo{'s' if len(u['repos']) > 1 else ''}"
    return out


def advice(u: dict | None, now: float) -> str:
    """One line for Claude: is the skill pulling its weight, and what to do if not? Too few
    tasks is not evidence either way (a skill nobody needed yet is not a bad skill)."""
    if not u or not u["offered"]:
        return "never offered yet"
    w, wo, s = u["with"], u["without"], u["since"]
    if u["offered"] >= 3 and not u["loaded"]:
        return "offered but never loaded: rewrite the description, or retire it"
    if s["n"] >= 2 and not s["accepted"] and s["n"] < w["n"]:
        return "its last edit hasn't helped: no task since was accepted; read wb lessons, patch it again"
    if w["n"] >= 3 and w["accepted"] / w["n"] < 0.5:
        return "loaded, yet most of those tasks failed: patch it with what review taught (wb lessons)"
    if w["n"] >= 3 and wo["n"] >= 3 and w["reviews"] / w["n"] >= wo["reviews"] / wo["n"] \
            and w["accepted"] / w["n"] <= wo["accepted"] / wo["n"]:
        return "tasks that load it go no better than those without: sharpen it, or retire it"
    if now - u["last"] > 60 * 86400:
        return "not offered in 60 days: retire it if it's no longer useful"
    return ""


WORD = re.compile(r"[a-z][a-z0-9]{3,}")
STOP = {"when", "with", "that", "this", "from", "into", "your", "what", "them", "then", "they",
        "have", "uses", "using", "used", "each", "only", "file", "files", "code", "make", "should",
        "must", "write", "task", "tasks", "agent", "skill", "test", "tests", "before", "after",
        # words any spec or skill says, whatever it is about (seen matching a pathfinding spec to
        # a parsing skill on "rows", "keep", "step", "passes")
        "check", "keep", "step", "steps", "rule", "rules", "never", "like", "stop", "need",
        "also", "same", "still", "where", "which", "work", "change", "first", "exactly", "every",
        "pass", "passes", "there", "other", "than", "more", "less", "does", "just"}
SUFFIXES = ("ing", "ers", "er", "es", "ed", "s", "e")


def _stem(w: str) -> str:
    """Crude, but enough that "parse", "parser" and "parsing" meet."""
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def words(text: str) -> set[str]:
    return {_stem(w) for w in WORD.findall(text.lower()) if w not in STOP} - STOP


def trigger(description: str) -> str:
    """A description's "Use when ..." part, before what the skill gives: what it is for."""
    return description.split(":", 1)[0]


SUGGEST_MIN = 3  # distinct shared words: one ("charts" in a spec that only says it draws none) is chance


def suggest(text: str, held: list[str] | None = None, n: int = 3) -> list[str]:
    """Library skills a spec seems to need: at least SUGGEST_MIN distinct words it shares with a
    skill's name and trigger, ranked by them (a name word counts double). A hint for Claude, who
    decides."""
    spec = words(text)
    scored = []
    for name, info in available().items():
        if name in (held or []):
            continue
        named = spec & words(name.replace("-", " "))
        shared = named | (spec & words(trigger(info["description"])))
        score = len(shared) + len(named)
        if len(shared) >= SUGGEST_MIN:
            scored.append((-score, name))
    return [name for _, name in sorted(scored)[:n]]
