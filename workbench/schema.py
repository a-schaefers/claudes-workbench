"""Structured results (#1278): a small JSON Schema subset, checked outside the agent's lane.

A report task ends with one JSON object: the `submit_result` tool's argument (ask_mcp.py), else
the last object in its final message (final.md). OpenCode validates an MCP tool's arguments
against its inputSchema and hands the errors back to the model (its tool runtime, 2.3.2), but
nothing the agent writes is trusted outside the lane: the runner checks the object again here.
Standard library only, so only the keywords a result needs: `type`, `properties`, `required`,
`additionalProperties` (a bool), `items`, `enum`, `minItems`, `maxItems`, `minLength`,
`maxLength`, `minimum`, `maximum`, plus `description` and `title`, which say and check nothing.
A schema naming any other keyword is refused (`valid`), never half-checked.
"""
import json

TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
                      "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
                      "description", "title", "$schema"})
ERRORS_MAX = 20  # errors one check reports: the repair prompt carries them, and a 9B model
                 # given 200 lines of them fixes none (each is a path, a few words)
TEXT_MAX = 512 * 1024  # of a final message's end, read for its last object


def _is(v, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, TYPES.get(t, ()))


def check(obj, schema: dict, path: str = "$") -> list[str]:
    """What is wrong with ``obj`` against ``schema``, as `<path>: <error>` lines (`$.findings[0]:
    missing required 'id'`), at most ERRORS_MAX; [] when it holds."""
    out: list[str] = []
    _check(obj, schema if isinstance(schema, dict) else {}, path, out)
    return out[:ERRORS_MAX]


def _check(v, s: dict, path: str, out: list) -> None:
    if len(out) >= ERRORS_MAX:
        return
    want = s.get("type")
    types = [want] if isinstance(want, str) else list(want) if isinstance(want, list) else []
    if types and not any(_is(v, t) for t in types):
        out.append(f"{path}: expected {' or '.join(types)}, got {_kind(v)}")
        return
    if "enum" in s and v not in s["enum"]:
        out.append(f"{path}: {json.dumps(v)[:60]} is not one of {json.dumps(s['enum'])[:200]}")
    if isinstance(v, str):
        if isinstance(s.get("minLength"), int) and len(v) < s["minLength"]:
            out.append(f"{path}: shorter than {s['minLength']} characters")
        if isinstance(s.get("maxLength"), int) and len(v) > s["maxLength"]:
            out.append(f"{path}: longer than {s['maxLength']} characters ({len(v)})")
    if _is(v, "number"):
        if _is(s.get("minimum"), "number") and v < s["minimum"]:
            out.append(f"{path}: below the minimum {s['minimum']}")
        if _is(s.get("maximum"), "number") and v > s["maximum"]:
            out.append(f"{path}: above the maximum {s['maximum']}")
    if isinstance(v, dict):
        props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
        for k in s.get("required") or ():
            if k not in v:
                out.append(f"{path}: missing required {k!r}")
        for k, sub in props.items():
            if k in v and isinstance(sub, dict):
                _check(v[k], sub, f"{path}.{k}", out)
        if s.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    out.append(f"{path}: unexpected key {k!r}")
    if isinstance(v, list):
        if isinstance(s.get("minItems"), int) and len(v) < s["minItems"]:
            out.append(f"{path}: fewer than {s['minItems']} items")
        if isinstance(s.get("maxItems"), int) and len(v) > s["maxItems"]:
            out.append(f"{path}: more than {s['maxItems']} items ({len(v)})")
        if isinstance(s.get("items"), dict):
            for i, item in enumerate(v):
                _check(item, s["items"], f"{path}[{i}]", out)


def _kind(v) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    return next((t for t, c in TYPES.items() if isinstance(v, c)), type(v).__name__)


def valid(schema) -> list[str]:
    """What makes ``schema`` unusable for a result: not an object schema at its top (an MCP tool's
    inputSchema must be one, and a result is one object), or a keyword outside KEYWORDS, which
    `check` would silently skip. [] when it can be used."""
    if not isinstance(schema, dict):
        return ["$: a schema is a JSON object"]
    out: list[str] = []
    if schema.get("type") != "object":
        out.append('$: the top must be {"type": "object", …}: a result is one object')
    _valid(schema, "$", out)
    return out


def _valid(s, path: str, out: list) -> None:
    if not isinstance(s, dict):
        out.append(f"{path}: a schema is a JSON object")
        return
    for k in s:
        if k not in KEYWORDS:
            out.append(f"{path}: {k!r} isn't checked here (only {', '.join(sorted(KEYWORDS - {'$schema'}))})")
    t = s.get("type")
    for one in [t] if isinstance(t, str) else t if isinstance(t, list) else []:
        if one not in (*TYPES, "integer", "number"):
            out.append(f"{path}: unknown type {one!r}")
    if t is not None and not isinstance(t, (str, list)):
        out.append(f"{path}: `type` is a name or a list of names")
    if "enum" in s and not isinstance(s["enum"], list):
        out.append(f"{path}: `enum` is a list")
    if "required" in s and not (isinstance(s["required"], list) and all(isinstance(x, str) for x in s["required"])):
        out.append(f"{path}: `required` is a list of names")
    if "additionalProperties" in s and not isinstance(s["additionalProperties"], bool):
        out.append(f"{path}: `additionalProperties` is true or false here")
    for k in ("minItems", "maxItems", "minLength", "maxLength"):
        if k in s and not (isinstance(s[k], int) and not isinstance(s[k], bool) and s[k] >= 0):
            out.append(f"{path}: `{k}` is a whole number")
    props = s.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            out.append(f"{path}: `properties` is an object")
        else:
            for k, sub in props.items():
                _valid(sub, f"{path}.{k}", out)
    if "items" in s:
        _valid(s["items"], f"{path}[]", out)


def last_object(text: str | None) -> dict | None:
    """The last JSON object in ``text`` (a final message: prose, then the object, maybe in a
    ```json fence), whole; None when there is none. Objects inside it aren't candidates: the
    scan jumps past each one it reads. DeepSeek put the object last after prose in 31 of 31
    rounds (#1278's study, 2026-11-25), and the report's 2,500-character cut lost it in 13."""
    if not text:
        return None
    text = text[-TEXT_MAX:]
    dec = json.JSONDecoder()
    found, i = None, 0
    while (i := text.find("{", i)) >= 0:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found = obj
        i = end
    return found


def load(path) -> dict:
    """A schema file, checked (`valid`); RuntimeError with what is wrong."""
    try:
        doc = json.loads(open(path, encoding="utf-8").read())
    except OSError as e:
        raise RuntimeError(f"{path}: {e.strerror}") from None
    except ValueError as e:
        raise RuntimeError(f"{path}: not JSON ({e})") from None
    if isinstance(doc, dict) and isinstance(doc.get("from"), str):
        doc.pop("from")  # `wb card copy --schema`'s record of the shipped blob (#1295): never the agent's
    if errs := valid(doc):
        raise RuntimeError(f"{path}: " + "; ".join(errs[:5]))
    return doc
