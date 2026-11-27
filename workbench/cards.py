"""Role cards (#1278): what makes an agent a finder, a verifier, a judge or a fixer.

A card is OpenCode agent Markdown: a frontmatter, then the role's instructions. The user's own in
`$WB_ROLES` (~/.config/workbench/roles/) come before the shipped ones in agent/roles/. A report
card's body stands in HANDBOOK.md's place (context.handbook_text), as agent/WRITING.md does for
the writing role: a judge with no tools doesn't read the 5.8 KB coding handbook. A code card's
body is appended to HANDBOOK under `## Your role`. Its `steps` go on the task's own wb-code agent
entry (runner.rules_env), so the layer's denies still come last (#1290).

Frontmatter keys: `description`, `tools` (read, none or code; `readonly` is read), `schema` (a
name in schema_path's places, or a file), `steps`, `skills`, `ask` (questions; 0 for reports),
`role` (routes.ROLES), `est` ({in, out, min}: one item's tokens and minutes, the run's estimate),
`mcp` (#1463 P8: the user's MCP servers this role may use, of those commands.json gives the repo;
missing: all of them, `[]`: none; a card narrows that allow-list, never widens it: `narrow_mcp`).
`permissions` and `model` are dropped and said: a card never lifts a deny nor picks a model.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import routes, schema

PKG_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = PKG_ROOT / "agent" / "roles"
SCHEMAS = PKG_ROOT / "agent" / "schemas"
BODY_MAX = 3000  # bytes of a card's body: it is in every prompt of its role (prefill time)
TOOLS = ("read", "none", "code")
KEYS = ("description", "tools", "schema", "steps", "skills", "ask", "role", "est", "from", "mcp")
DROPPED = ("permissions", "permission", "model")  # a card's own: never the task's
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}\Z")


@dataclass
class Card:
    name: str
    path: Path
    description: str = ""
    tools: str = "read"
    schema: str | None = None
    steps: int | None = None
    skills: list = field(default_factory=list)
    ask: int | None = None
    role: str | None = None
    est: dict = field(default_factory=dict)
    mcp: list | None = None  # None: every server commands.json gives the repo (#1463 P8)
    body: str = ""
    dropped: list = field(default_factory=list)  # the keys a card may not set, said at load

    @property
    def report(self) -> bool:
        return self.tools != "code"


def roles_dir() -> Path:
    v = os.environ.get("WB_ROLES")
    return Path(v).expanduser() if v else Path.home() / ".config" / "workbench" / "roles"


def card_path(name: str) -> Path | None:
    """Card ``name``'s file: the user's first, then the shipped one; a path ending .md as given."""
    if name.endswith(".md") or "/" in name:
        p = Path(name).expanduser()
        return p if p.is_file() else None
    if not NAME_RE.match(name):
        return None
    for d in (roles_dir(), SHIPPED):
        if (d / f"{name}.md").is_file():
            return d / f"{name}.md"
    return None


def names() -> list[str]:
    """Every card a flow or wb start can name: the user's and the shipped ones."""
    return sorted({p.stem for d in (roles_dir(), SHIPPED) if d.is_dir() for p in d.glob("*.md")
                   if NAME_RE.match(p.stem)})


def _scalar(v: str):
    v = v.strip()
    if not v:
        return ""
    if v[0] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return _flow(v)
    if (v[0] == v[-1]) and v[0] in "\"'" and len(v) > 1:
        return v[1:-1]
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d*", v):
        return float(v)
    if v in ("true", "false"):
        return v == "true"
    if v in ("null", "~"):
        return None
    return v


def _flow(v: str):
    """YAML's flow style, as OpenCode's agent files write it: `{in: 60000, out: 4000}` or
    `[a, b]`, one level deep."""
    inner = v[1:-1].strip()
    parts = [p.strip() for p in inner.split(",") if p.strip()] if inner else []
    if v[0] == "[":
        return [_scalar(p) for p in parts]
    out = {}
    for p in parts:
        k, _, val = p.partition(":")
        out[k.strip().strip("\"'")] = _scalar(val)
    return out


def parse(text: str) -> tuple[dict, str]:
    """(frontmatter, body) of a card: `key: value` lines between `---` lines, a value plain, quoted,
    JSON or flow style, or a block of `  key: value` lines under a bare `key:`."""
    if not text.startswith("---"):
        return {}, text
    head, sep, body = text[3:].partition("\n---")
    if not sep:
        return {}, text
    front: dict = {}
    last = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in " \t" and last is not None and isinstance(front.get(last), dict):
            k, _, v = line.strip().partition(":")
            if line.strip().startswith("- "):
                continue
            front[last][k.strip()] = _scalar(v)
            continue
        if line[:1] in " \t" and last is not None and line.strip().startswith("- "):
            if not isinstance(front.get(last), list):
                front[last] = []
            front[last].append(_scalar(line.strip()[2:]))
            continue
        k, _, v = line.partition(":")
        last = k.strip()
        front[last] = _scalar(v) if v.strip() else {}
    return front, body.split("\n", 1)[1] if "\n" in body else ""


def load(name: str) -> Card:
    """Card ``name`` (card_path), checked; RuntimeError with what is wrong."""
    path = card_path(name)
    if path is None:
        raise RuntimeError(f"no role card {name!r} ({roles_dir()} or {SHIPPED}; known: "
                           f"{', '.join(names()) or 'none'})")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(f"{path}: {e}") from None
    front, body = parse(text)
    stem = path.stem
    if not NAME_RE.match(stem):
        raise RuntimeError(f"{path}: a card's name is lowercase letters, digits and hyphens")
    unknown = [k for k in front if k not in KEYS and k not in DROPPED]
    if unknown:
        raise RuntimeError(f"{path}: unknown key(s) {', '.join(unknown)} (a card takes {', '.join(KEYS)})")
    tools = front.get("tools", "read")
    tools = "read" if tools == "readonly" else tools
    if tools not in TOOLS:
        raise RuntimeError(f"{path}: tools is read, none or code, not {tools!r}")
    if len(body.encode()) > BODY_MAX:
        raise RuntimeError(f"{path}: the card's text is {len(body.encode()):,} bytes, over {BODY_MAX:,}: "
                           "it is in every prompt of its role; move the rest to a skill")
    role = front.get("role")
    if role is not None and role not in routes.ROLES:
        raise RuntimeError(f"{path}: role is one of {', '.join(routes.ROLES)}, not {role!r}")
    steps, ask = front.get("steps"), front.get("ask")
    for k, v in (("steps", steps), ("ask", ask)):
        if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            raise RuntimeError(f"{path}: {k} is a whole number")
    est = front.get("est") or {}
    if not isinstance(est, dict) or any(k not in ("in", "out", "min") or not isinstance(v, (int, float))
                                        or isinstance(v, bool) or v < 0 for k, v in est.items()):
        raise RuntimeError(f"{path}: est is {{in, out, min}}: tokens in, out and minutes per item")
    skills = front.get("skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    mcp = front.get("mcp")
    if isinstance(mcp, str):
        mcp = [s.strip() for s in mcp.split(",") if s.strip()]
    if mcp == {}:  # a bare `mcp:` with no list under it
        mcp = []
    if mcp is not None and not (isinstance(mcp, list) and all(isinstance(n, str) and n for n in mcp)):
        raise RuntimeError(f"{path}: mcp is a list of the MCP server names commands.json gives")
    sch = front.get("schema")
    if tools != "code" and not sch:
        raise RuntimeError(f"{path}: a report card (tools {tools}) names its schema")
    if sch is not None and schema_path(str(sch)) is None:
        raise RuntimeError(f"{path}: no schema {sch!r} ({schemas_dirs_said()})")
    return Card(name=stem, path=path, description=str(front.get("description") or ""), tools=tools,
                schema=str(sch) if sch else None, steps=steps, skills=list(skills),
                ask=ask, role=role, est=dict(est), mcp=list(mcp) if mcp is not None else None,
                body=body.strip() + "\n",
                dropped=[k for k in front if k in DROPPED])


def narrow_mcp(card: "Card | None", allowed: list[str]) -> tuple[list[str], list[str]]:
    """The servers a task of ``card`` gets of ``allowed`` (commands.json's for its repo,
    context.mcp_names), and the card's names that list doesn't give, left out (#1463 P8): a card
    narrows the user's allow-list, never widens it. No card, or one with no `mcp`: all of them."""
    if card is None or card.mcp is None:
        return list(allowed), []
    return [n for n in allowed if n in card.mcp], [n for n in card.mcp if n not in allowed]


def schema_dirs() -> list[Path]:
    """Where a schema named by a card or flow is looked for: the user's, then the shipped ones."""
    return [roles_dir() / "schemas", SCHEMAS]


def schemas_dirs_said() -> str:
    return " or ".join(str(d) for d in schema_dirs())


def schema_path(name: str) -> Path | None:
    """A schema's file: a name (findings) in schema_dirs, or a path to a .json file."""
    if name.endswith(".json") or "/" in name:
        p = Path(name).expanduser()
        return p if p.is_file() else None
    if not NAME_RE.match(name):
        return None
    return next((d / f"{name}.json" for d in schema_dirs() if (d / f"{name}.json").is_file()), None)


def load_schema(name: str) -> dict:
    """Schema ``name`` (schema_path), checked (schema.valid); RuntimeError with what is wrong."""
    path = schema_path(name)
    if path is None:
        raise RuntimeError(f"no schema {name!r} ({schemas_dirs_said()})")
    return schema.load(path)


# --- shipped ones shadowed (#1295): copy to customize, reset, and the drift said -----------------

def blob_sha(data: bytes) -> str:
    """git's blob id of ``data`` (`git hash-object`), with no git: a copy's `from`."""
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def shadow_mark(mine: Path, shipped: Path, recorded: str | None) -> str:
    """How the user's ``mine`` stands to the shipped file of its name: "" when nothing is shipped
    by it, else `shadows shipped`, and `shipped changed since` when the shipped file's blob is no
    longer the one its copy recorded (``recorded``, its `from`); a copy by hand records none."""
    if not shipped.is_file():
        return ""
    try:
        now = blob_sha(shipped.read_bytes())
    except OSError:
        return "shadows shipped"
    return "shadows shipped" + ("; shipped changed since" if recorded and recorded != now else "")


def recorded_from(path: Path) -> str | None:
    """A user's card's or schema's or flow's `from`, or None."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if path.suffix == ".json":
        try:
            v = json.loads(text).get("from")
        except (ValueError, AttributeError):
            return None
    else:
        v = parse(text)[0].get("from")
    return v if isinstance(v, str) else None


def copy_shipped(shipped: Path, dest: Path, force: bool = False) -> str:
    """Copy a shipped card, schema or flow to the user's place to customize it, its `from` the
    shipped blob's sha; the line to print. RuntimeError when there is none or a copy is there."""
    if not shipped.is_file():
        raise RuntimeError(f"nothing shipped is named {shipped.stem!r} ({shipped.parent})")
    if dest.exists() and not force:
        raise RuntimeError(f"{dest} is there already: yours (--force replaces it; reset puts the shipped one back)")
    data = shipped.read_bytes()
    sha = blob_sha(data)
    if shipped.suffix == ".json":
        doc = json.loads(data)
        out = json.dumps({**doc, "from": sha}, indent=2, ensure_ascii=False) + "\n"
    else:
        text = data.decode("utf-8")
        out = (f'---\nfrom: "{sha}"\n' + text[3:].lstrip("\n")) if text.startswith("---") \
            else f'---\nfrom: "{sha}"\n---\n{text}'
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, dest)
    return f"copied the shipped {shipped.stem} to {dest}: yours now, edit it there (from {sha[:12]})"


def reset_shipped(shipped: Path, mine: Path) -> str:
    """Put the shipped one back: the user's copy moved aside to <file>.bak (.bak.2, .bak.3, … when
    taken), never deleted."""
    if not mine.is_file():
        raise RuntimeError(f"no {mine.stem} of yours ({mine.parent}): the shipped one is used already")
    if not shipped.is_file():
        raise RuntimeError(f"{mine} is your own, with nothing shipped by its name to go back to")
    # a free name: a second reset replaced the first's .bak, so a copy edited, reset, copied and
    # edited again lost the first edit (#1520, 2026-11-26)
    bak, n = mine.with_name(mine.name + ".bak"), 1
    while bak.exists():
        n += 1
        bak = mine.with_name(f"{mine.name}.bak.{n}")
    os.replace(mine, bak)
    return f"reset {mine.stem}: the shipped one is used again; yours is kept as {bak}"


def rows() -> list[dict]:
    """Every card: {name, where (yours|shipped), mark, description}, the user's shadowing."""
    out = []
    for name in names():
        mine, shipped = roles_dir() / f"{name}.md", SHIPPED / f"{name}.md"
        p = mine if mine.is_file() else shipped
        try:
            desc = str(parse(p.read_text(encoding="utf-8"))[0].get("description") or "")
        except (OSError, UnicodeDecodeError):
            desc = "(unreadable)"
        out.append({"name": name, "where": "yours" if p == mine else "shipped",
                    "mark": shadow_mark(mine, shipped, recorded_from(mine)) if p == mine else "",
                    "description": desc})
    return out


def cmd_card(a) -> None:
    """wb card list | copy | reset [--schema]."""
    from .cli import die
    schema_ = getattr(a, "schema", False)
    shipped_dir, mine_dir, ext = (SCHEMAS, roles_dir() / "schemas", ".json") if schema_ else (SHIPPED, roles_dir(), ".md")
    if a.card_cmd == "list":
        if schema_:
            for name in sorted({p.stem for d in (mine_dir, shipped_dir) if d.is_dir() for p in d.glob("*.json")}):
                mine = mine_dir / f"{name}.json"
                mark = shadow_mark(mine, shipped_dir / f"{name}.json", recorded_from(mine)) if mine.is_file() else ""
                print(f"{name} ({'yours' if mine.is_file() else 'shipped'}{f', {mark}' if mark else ''})")
            return
        for r in rows():
            print(f"{r['name']} ({r['where']}{', ' + r['mark'] if r['mark'] else ''}): {r['description']}")
        return
    if not NAME_RE.match(a.name or ""):
        die(f"{a.name!r}: a {'schema' if schema_ else 'card'} name is lowercase letters, digits and hyphens")
    try:
        if a.card_cmd == "copy":
            print(copy_shipped(shipped_dir / f"{a.name}{ext}", mine_dir / f"{a.name}{ext}", a.force))
        else:
            print(reset_shipped(shipped_dir / f"{a.name}{ext}", mine_dir / f"{a.name}{ext}"))
    except (RuntimeError, OSError, ValueError) as e:
        die(str(e))


def handbook(card: dict, base: str) -> str:
    """The handbook text a card task's agent gets: a report card's body alone, a code card's after
    ``base`` (HANDBOOK.md) under `## Your role`. ``card``: task.json's `card` ({name, tools, body})."""
    if card.get("tools") == "code":
        return base.rstrip("\n") + "\n\n## Your role\n" + str(card.get("body") or "")
    return str(card.get("body") or "")


def said_dropped(card: Card) -> str:
    """The line for a card's dropped keys, or ""."""
    return (f"note: card {card.name} sets {', '.join(card.dropped)}, which a card can't: dropped "
            "(the task's rules and the user's roles decide)") if card.dropped else ""


def dumps(card: Card) -> dict:
    """task.json's `card`: fixed at wb start, so an edited card never changes a running task."""
    return {"name": card.name, "tools": card.tools, "body": card.body,
            **({"steps": card.steps} if card.steps is not None else {})}

