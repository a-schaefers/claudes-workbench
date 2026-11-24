"""Privacy for API roles (#1021): where a task's code goes, the per-repo `local_only`, and what
an API task may not take along unasked.

With a GPU model everything the agent reads stays on this machine. With an API model every file
it reads and every command's output is model input, sent to the provider. Setting a role is the
user's consent (its dialog says where the code goes, from `where`); this module says the same
wherever else it matters, refuses an API model for a repo the user marked local-only, and holds
the OpenRouter routing the keyhole enforces. The route command's preview (#1014's
`wb route … --dry-run --json`) and the roles module re-export `where`.

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import store

# Each provider's own data-policy page, for the providers OpenCode's /connect offers. URLs only,
# never a claim about what the policy says: the user reads it there. Checked 2026-11-23 (each
# fetched and its title read; x.ai's behind Cloudflare, read through a headless browser, now
# titled SpaceXSI's). A provider not here gets None, and the line says "see <name>'s data policy".
PRIVACY_PAGES = {
    "anthropic": "https://www.anthropic.com/legal/privacy",
    "openai": "https://openai.com/enterprise-privacy/",
    "deepseek": "https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html",
    "deepinfra": "https://docs.deepinfra.com/account/data-privacy",
    "openrouter": "https://openrouter.ai/docs/features/privacy-and-logging",
    "opencode": "https://opencode.ai/docs/zen/",   # its Privacy section: the free models' exceptions
    "xai": "https://x.ai/legal/privacy-policy",
    "google": "https://aistudio.google.com/docs/terms",
    "mistral": "https://legal.mistral.ai/terms/privacy-policy",
    "groq": "https://groq.com/privacy-policy/",
    "togetherai": "https://www.together.ai/privacy",
    "fireworks": "https://fireworks.ai/privacy-policy",
    "cerebras": "https://www.cerebras.ai/privacy-policy",
}
OLLAMA_PAGE = "https://ollama.com/privacy"  # an Ollama cloud model's (its provider id is the user's)

FREE_LINE = "free: the provider may keep or train on your code"
# A provider whose free tier is a key's, not a model's (#1192): Gemini's paid and unpaid keys run
# the same models at the same catalog prices, so `free` can't tell them apart, and the line says
# it of every task there (the first-task note and the route dialog, never a warning of its own)
FREE_TIER = {"google": "on Gemini's free tier (a key without billing) Google may use your code "
                       "to improve its products"}
OPENROUTER = "openrouter"
OPENROUTER_ACCOUNT = "(OpenRouter's account setting decides whether backends may keep it)"


def where(m, order=None) -> dict:
    """Where an API model's task sends the code: {provider, name, line, policy, free, backends}.
    ``m`` is a models.Model of kind "api"; ``order`` the OpenRouter backends the role chose.
    The one wording for the route dialog, `wb route` and the start note (the mod reads it from
    `wb route … --dry-run --json`, never words of its own)."""
    from . import models
    pid = m.provider
    name = "ollama.com" if m.cloud else models.provider_name(m)
    policy = OLLAMA_PAGE if m.cloud else PRIVACY_PAGES.get(pid)
    backends = [str(b) for b in order or ()] if pid == OPENROUTER else []
    parts = [f"your code goes to {name}"]
    if pid == OPENROUTER:
        parts.append(f"via OpenRouter to {', '.join(backends) or 'its choice of backend'}, no fallbacks "
                     f"{OPENROUTER_ACCOUNT}")
    if not policy:
        parts.append(f"see {name}'s data policy")
    if m.free:
        parts.append(FREE_LINE)
    elif pid in FREE_TIER and not m.cloud:
        parts.append(FREE_TIER[pid])
    return {"provider": pid, "name": name, "line": " · ".join(parts), "policy": policy,
            "free": bool(m.free), "backends": backends}


def order_refusal(model_id: str, order) -> str | None:
    """Why `wb route <role> <OpenRouter id>` is refused without `--order`: one id is served by
    many backends that differ in quantization and data policy, and the role must never show a
    backend nobody chose. None for any other provider, or with an order."""
    if model_id.split("/", 1)[0] != OPENROUTER or [b for b in order or () if str(b).strip()]:
        return None
    rest = model_id.split("/", 1)[1] if "/" in model_id else model_id
    return (f"{model_id} is served by many backends that differ in quantization and data policy: "
            f"pick them with --order <name>[,<name>] (openrouter.ai/{rest}, Providers tab)")


def parse_order(text: str | None) -> list[str]:
    """`--order baseten,deepinfra/fp8` as a list, blanks and repeats dropped."""
    return list(dict.fromkeys(x.strip() for x in (text or "").split(",") if x.strip()))


def routing(order) -> dict:
    """The OpenRouter `provider` object an API task's requests carry: the chosen backends, in
    order, and no fallback to any other (OpenRouter's `allow_fallbacks` is true by default)."""
    return {"order": [str(b) for b in order or ()], "allow_fallbacks": False}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.split("/", 1)[0].lower())


# OpenRouter's backends whose display name doesn't fold to their slug, by the folded name: of the
# 123 its /api/v1/providers listed on 2026-11-23, these five. Without them a role ordered to
# `google-vertex` read a response served by "Google" as a backend outside the order (#1021's
# review). A backend added later with such a name reads as outside until it is listed here.
OPENROUTER_SLUGS = {"google": "googlevertex", "sakanaai": "sakana", "claudeplatformonaws": "claudeonaws",
                    "mancer2": "mancer", "voyageaibymongodb": "voyageai"}


def outside_order(served_by, order) -> bool:
    """Whether OpenRouter's `provider` field names a backend outside the role's order. It
    answers with a display name ("DeepInfra", "Google AI Studio") where the order holds slugs
    ("deepinfra", "google-ai-studio", "deepinfra/fp8"), so both are compared folded, through
    OPENROUTER_SLUGS for a name that folds to something else."""
    if not order or not isinstance(served_by, str) or not served_by:
        return False
    name = _slug(served_by)
    return OPENROUTER_SLUGS.get(name, name) not in {_slug(str(b)) for b in order}


# what makes a registry config hold a credential (context.API_HIDDEN)

# An API task's lane hides a token file only when it holds a credential (#1021's decision,
# 2026-11-23: the owner wants "just works", and Maven, Gradle, dotnet, Yarn Berry, poetry,
# composer, Bundler and `dart pub get` stop on every run with their file unreadable). The matcher
# is conservative: a line whose key says secret, a Bearer or Basic value, a private key, a URL
# with a user in it, or a Bundler host's login is one; so is a file it can't read or parse.
# Whole-line comments (and XML comments: Maven's settings.xml template is full of commented
# <password>s) don't count, nor a value that is only an environment reference (npm's and Yarn's
# `${NPM_TOKEN}`, bun's `$NPM_TOKEN`), which leaves the secret out of the file.
CREDENTIAL_MAX = 1 << 20      # bigger than any registry config: read as holding one
_SECRET_KEY = re.compile(r"(?i)_auth|password|passwd|passphrase|token|secret|bearer|oauth|credential"
                         r"|authident|api[_.-]?keys?|(?:write|read|auth|access|private|encrypted|signing)[_.-]?key"
                         r"|^[\w.-]*\.key\s*[=:]|^key\s*[=:]")  # gradle.properties' `gpr.key=`, not NuGet's key="…"
_SECRET_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
                           r"|://[^\s/@'\"<>]+@")
_ENV_REF = re.compile(r"""^(["']?)\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)\1$""")
# Bundler keeps a host's login under the host's name (BUNDLE_GEMS__EXAMPLE__COM: "user:pass",
# or the URL's); of its own settings, only these spell `__` (build.<gem>, mirror.<url>…)
_BUNDLE_HOST = re.compile(r"""(?i)^["']?BUNDLE_(?!(?:BUILD|MIRROR|LOCAL|PATH|GEM)__)\S*(?:__|://)""")
_JSON_HEADER = re.compile(r"(?i)header")   # composer's custom-headers: an auth header, as a rule


def holds_credential(path) -> bool:
    """Whether the registry config at ``path`` (a file, or NuGet's config/ folder of them)
    holds a credential, by the conservative matcher above: one that can't be read, decoded or
    (JSON) parsed counts as holding one; one that isn't there holds none."""
    p = str(path)
    if not os.path.lexists(p):
        return False
    if os.path.isdir(p):
        try:
            files = [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs]
        except OSError:
            return True
        return len(files) > 200 or any(holds_credential(f) for f in files)
    try:
        with open(p, "rb") as f:
            data = f.read(CREDENTIAL_MAX + 1)
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return True
    if len(data) > CREDENTIAL_MAX:
        return True
    if p.endswith(".json"):
        try:
            return _json_holds(json.loads(text), False)
        except ValueError:
            return True
    return any(_line_holds(line.strip()) for line in re.sub(r"(?s)<!--.*?-->", "", text).splitlines())


def _line_holds(line: str) -> bool:
    if not line or line[0] in "#;%!":
        return False
    if _SECRET_VALUE.search(line) or _BUNDLE_HOST.match(line):
        return True
    if not _SECRET_KEY.search(line):
        return False
    m = re.search(r"=|:(?=\s|$)", line)         # npm's keys hold `:` (//host/:_authToken=…)
    if not m:
        return True                              # <password>…</password>, a [pypi-token] table
    value = line[m.end():].strip()
    return bool(value) and not _ENV_REF.match(value)


def _json_holds(node, secret: bool) -> bool:
    if isinstance(node, dict):
        return any(_json_holds(v, secret or bool(_SECRET_KEY.search(k) or _JSON_HEADER.search(k)))
                   for k, v in node.items())
    if isinstance(node, list):
        return any(_json_holds(v, secret) for v in node)
    if isinstance(node, str):
        return bool(node.strip()) and secret or bool(_SECRET_VALUE.search(node))
    return secret and isinstance(node, (int, float)) and not isinstance(node, bool)


# local_only (commands.json `repos.<path>.local_only`, wb start --local-only)

def local_only(repo, sub: str | None = None) -> str | None:
    """The commands.json key that marks this spot local-only, or None. The nearest entry that
    says `local_only` decides (context.repo_entries: the spot, else the nearest directory above
    it, by real path, else by git dir), so a monorepo package can be local-only alone, or opt
    out with `false` under a local-only root; an entry for its setup alone doesn't lift the
    root's (the nearest-key rule for `setup` would have)."""
    from . import context
    for key, v in context.repo_entries(repo, sub):
        if "local_only" in v:
            return key if v["local_only"] is True else None
    return None


def home_path(p) -> str:
    """A path as the user typed it: under the home directory with `~`."""
    p = str(p)
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home.rstrip("/") + "/") else p


def local_only_line(where_: str, why: str, role: str | None, model_id: str, asked: bool) -> str:
    """wb start's exit-3 line for an API model in a local-only spot: the repo, why it is
    local-only, the role (or the --model), and the choices. Never a GPU model instead (#1008's
    decision 2)."""
    if asked:
        what = f"the task's model is API ({model_id}, --model)"
        fix = "a GPU model"
    else:
        what = f"the {role} role is on API ({model_id})"
        fix = f"put {role} on the GPU (wb route {role} gpu, the user's yes)"
    lift = ("the user drops --local-only for this task" if why == "--local-only"
            else "the user lifts local_only for this repo")
    return f"{where_} is local-only ({why}): {what}. Choices: {fix}, or {lift}."


def since_start_line(task) -> str:
    """wb next/feedback/resume's exit-3 line for an API task whose repo became local-only after
    it started: its model is fixed per task, so there is nothing to switch to."""
    pid = task.spec.get("provider") or str(task.spec.get("model") or "").split("/", 1)[0]
    return (f"this repo is local-only now; the task's model is API ({pid}): "
            f"wb takeover {task.name} or wb discard {task.name}")


def is_api_task(task) -> bool:
    """An API task, by task.json `via` (#1016), GPU when unset: every older task reads as GPU."""
    return task.spec.get("via") == "api"


# the where-note: the first task of each repo and provider

def first_for(repo, provider: str, me: str | None = None) -> bool:
    """Whether no earlier API task of this repo went to this provider: none in tasks/ or the
    archive (wb gc would otherwise make the note come back). Not results.jsonl: rows before #1022
    don't say a task was on API (no `kind`), and every closed task's directory stays, archived."""
    repo = os.path.realpath(str(repo))
    for t in store.all_tasks(archived=True):
        if t.name == me or not is_api_task(t):
            continue
        if os.path.realpath(t.spec.get("repo") or "") == repo and _provider_of(t.spec) == provider:
            return False
    return True


def _provider_of(row: dict) -> str:
    return str(row.get("provider") or str(row.get("model") or "").split("/", 1)[0])


def where_note(info: dict, rules: bool, named: list[str], whose: str = "") -> str:
    """The start's note on a repo's first API task: what goes and to whom. ``rules``: the user
    has a ~/.config/workbench/AGENTS.md the agent is handed (never Claude's files, #1061);
    ``named``: the copies and siblings named on the command line, which go too; ``whose``: whose
    choice the model is (`the trivial role's model`, `asked for`, #1016)."""
    what = "the repo, its spec, CLAUDE.md files" + (" and your workbench AGENTS.md" if rules else "")
    if named:
        what = what.replace(" and your", ", your") + " and " + ", ".join(named)
    policy = f"data policy: {info['policy']}" if info.get("policy") else f"see {info['name']}'s data policy"
    extra = "".join(f"; {p}" for p in info["line"].split(" · ")[1:] if not p.startswith("see "))
    return f"this task sends {what} to {info['name']} ({whose + '; ' if whose else ''}{policy}){extra}"


def standing_refusal(kind: str, entry: str, name: str, given: str | None = None) -> str:
    """_worktree_setup's exit-3 line for a commands.json `copy` or `siblings` entry an API task
    would take along unnamed. `given`: the --copy that names it, from a package its path from
    there (cli._copy_name, #1158)."""
    if kind == "copy":
        return (f'commands.json "repos" copies {entry} into every task, and this one is on API '
                f"({name}): name it (--copy {given or entry}) to send it, or put the role on the GPU")
    return (f'commands.json "repos" links {entry} beside every task, and this one is on API '
            f"({name}): name it (--sibling {entry}) to send it, or put the role on the GPU")
