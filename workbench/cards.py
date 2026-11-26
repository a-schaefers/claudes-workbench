"""Role cards (#1278): what makes an agent a finder, a verifier, a judge or a fixer.

A card is OpenCode agent Markdown: a frontmatter, then the role's instructions. The user's own in
`$WB_ROLES` (~/.config/workbench/roles/) come before the shipped ones in agent/roles/. A report
card's body stands in HANDBOOK.md's place (context.handbook_text), as agent/WRITING.md does for
the writing role: a judge with no tools doesn't read the 5.8 KB coding handbook. A code card's
body is appended to HANDBOOK under `## Your role`. Its `steps` go on the task's own wb-code agent
entry (runner.rules_env), so the layer's denies still come last (#1290).

Frontmatter keys: `description`, `tools` (read, none or code; `readonly` is read), `schema` (a
name in schema_path's places, or a file), `steps`, `skills`, `ask` (questions; 0 for reports),
`role` (routes.ROLES), `est` ({in, out, min}: one item's tokens and minutes, the run's estimate).
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
KEYS = ("description", "tools", "schema", "steps", "skills", "ask", "role", "est")
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
    sch = front.get("schema")
    if tools != "code" and not sch:
        raise RuntimeError(f"{path}: a report card (tools {tools}) names its schema")
    if sch is not None and schema_path(str(sch)) is None:
        raise RuntimeError(f"{path}: no schema {sch!r} ({schemas_dirs_said()})")
    return Card(name=stem, path=path, description=str(front.get("description") or ""), tools=tools,
                schema=str(sch) if sch else None, steps=steps, skills=list(skills),
                ask=ask, role=role, est=dict(est), body=body.strip() + "\n",
                dropped=[k for k in front if k in DROPPED])


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

