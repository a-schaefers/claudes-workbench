"""Privacy for API roles (#1021): the per-repo `local_only`, what an API task may not take
along, and the OpenRouter routing the keyhole enforces.

With a GPU model everything the agent reads stays on this machine. With an API model every file
it reads and every command's output is model input, sent to the provider. Setting a role is the
user's consent (its dialog says where the code goes); this module refuses an API model for a
repo the user marked local-only, and a standing copy that holds a credential.

Standard library only."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from . import store

FREE_LINE = "free: the provider may keep or train on your code"
OPENROUTER = "openrouter"


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


# commands.json's standing `copy` entries an API task takes (cli._standing_gap, #1322's review):
# holds_credential reads any file it can't read or that is over the cap as holding one, right for
# a token file the lane hides but not for a copy, where it refused every API start for a test.db,
# a .so or a big .csv. A directory is walked (an entry like config/local/ went along unread), but
# not the package trees in it, whose code is others' and full of `token = …`. In it, only the
# strong signals count (#1322's second review): holds_credential's matcher, its bare keyword and
# its every key with `token` in it, refused a models/ copy for tokenizer_config.json's
# `bos_token`, a vocab.txt's `password`, a NOTES.txt's "your API token" and a deps/ copy for a
# hex_metadata.config, on every start. So a directory's .txt, extensionless and .config files
# aren't read; a dotfile or COPY_CONFIG_SUFFIXES file counts for a Bearer or Basic token, a
# private key or a URL with a password, or a key that ends in a secret's name (COPY_SECRET_KEY:
# `db_password`, `api_key`, `auth_token`, never `bos_token` or `max_tokens`) with a value.
COPY_SKIP_DIRS = frozenset({"node_modules", "bower_components", "vendor", ".venv", "venv",
                            "site-packages", "__pycache__", ".git", "target", ".gradle", "Pods",
                            ".tox", ".mypy_cache", ".pytest_cache", ".cache", "deps", "_build",
                            ".dart_tool", ".stack-work"})
COPY_CONFIG_SUFFIXES = frozenset({".env", ".ini", ".yml", ".yaml", ".toml", ".properties",
                                  ".netrc", ".json", ".pem", ".key"})
COPY_SECRET_KEY = re.compile(r"(?i)(?:password|passwd|passphrase|secret|api[_.-]?key|"
                             r"(?:secret|access|private|signing|encryption)[_.-]?key|"
                             r"(?:auth|access|api|refresh|bearer|session|secret|client|personal)[_.-]?token"
                             r"|^token|^_auth)$")
_COPY_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+(?=[a-z0-9._~+/-]*\d)[a-z0-9._~+/=-]{12,}"
                         r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|://[^\s/@:'\"<>]+:[^\s/@'\"<>]+@")
_NETRC_LOGIN = re.compile(r"(?i)\bpassword\s+\S")


def copy_holds_credential(path) -> bool:
    """Whether a standing `copy` entry at ``path`` holds a credential: a file, if it is text under
    CREDENTIAL_MAX and the matcher flags it; a directory, if one of its dotfiles or
    COPY_CONFIG_SUFFIXES files, outside COPY_SKIP_DIRS, shows a strong signal (_dir_file_holds).
    A binary file, one over the cap, a FIFO or one that can't be read goes along as configured."""
    p = str(path)
    if not os.path.isdir(p):
        text = _copied_text(p)
        if text is None:
            return False
        if p.endswith(".json"):
            try:
                return _json_holds(json.loads(text), False)
            except ValueError:
                pass                             # tsconfig's comments: line by line
        return any(_line_holds(line.strip()) for line in re.sub(r"(?s)<!--.*?-->", "", text).splitlines())
    if os.path.basename(os.path.normpath(p)) in COPY_SKIP_DIRS:
        return False
    for d, dirs, files in os.walk(p):           # links to directories aren't followed
        dirs[:] = [x for x in dirs if x not in COPY_SKIP_DIRS]
        for f in files:
            if (f.startswith(".") or os.path.splitext(f)[1].lower() in COPY_CONFIG_SUFFIXES) \
                    and _dir_file_holds(os.path.join(d, f)):
                return True
    return False


def _copied_text(p: str) -> str | None:
    """A copied file's text, or None if it isn't a regular text file under the cap."""
    try:
        st = os.stat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_size > CREDENTIAL_MAX:
            return None
        with open(p, "rb") as f:
            data = f.read(CREDENTIAL_MAX + 1)
        if len(data) > CREDENTIAL_MAX or b"\0" in data:
            return None
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _dir_file_holds(p: str) -> bool:
    """A file found in a copied directory: a strong signal only (the comment above)."""
    text = _copied_text(p)
    if text is None:
        return False
    if p.endswith(".json"):
        try:
            return _copy_json_holds(json.loads(text), False)
        except ValueError:
            pass
    netrc = os.path.basename(p) in (".netrc", "_netrc") or p.endswith(".netrc")
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;%!" or line.startswith("//"):
            continue
        if _COPY_VALUE.search(line) or (netrc and _NETRC_LOGIN.search(line)):
            return True
        m = re.search(r"=|:(?=\s|$)", line)
        if not m:
            continue
        key = line[:m.start()].strip().strip("\"'").removeprefix("export ").strip()
        value = line[m.end():].strip().rstrip(",").strip().strip("\"'")
        if COPY_SECRET_KEY.search(key) and value and not _ENV_REF.match(value):
            return True
    return False


def _copy_json_holds(node, secret: bool) -> bool:
    if isinstance(node, dict):
        return any(_copy_json_holds(v, bool(COPY_SECRET_KEY.search(k))) for k, v in node.items())
    if isinstance(node, list):
        return any(_copy_json_holds(v, secret) for v in node)
    if isinstance(node, str):
        v = node.strip()
        return bool(_COPY_VALUE.search(v)) or secret and bool(v) and not _ENV_REF.match(v)
    return False


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

def _marks() -> list[str]:
    """commands.json's `repos` keys that say `local_only: true`."""
    from . import context
    return [k for k, v in (context.commands_conf().get("repos") or {}).items()
            if v.get("local_only") is True]


def local_only(repo, sub: str | None = None, base: str | None = None) -> str | None:
    """The commands.json key that keeps this repo's tasks off API models, or None: any entry at
    or under the repo's top that says `local_only: true` (context.repo_entries ``whole``: by
    real path and by git dir), the spot's nearest first. A task's checkout holds the whole repo,
    whichever directory it starts in, so a package marked alone once went to the API model with
    every task started at the root, or in it when it had no CLAUDE.md of its own (#1255, the bar
    loop, 2026-11-24); a `false` lifts no other entry's `true` for the same reason. Its
    submodules' too (gitops.submodule_sources at ``base``, else HEAD), which fill_submodules
    puts in every task's worktree, and an entry at or above the repo in each repo it is a
    submodule of: repo_entries leaves a repo of its own out, so a marked submodule went to the
    API model with every task of the superproject, and a marked superproject's submodule with
    every task started in it (#1267, the bar loop, 2026-11-24)."""
    from . import context, gitops
    if not _marks():
        return None
    for top in [repo, *gitops.submodule_sources(Path(repo), base or "HEAD")]:
        for key, v in context.repo_entries(top, sub if top is repo else None, whole=True):
            if v.get("local_only") is True:
                return key
    at = repo
    while (sup := gitops.superproject(at)) is not None:
        rel = os.path.relpath(os.path.realpath(at), os.path.realpath(sup))
        for key, v in context.repo_entries(sup, rel):
            if v.get("local_only") is True:
                return key
        at = sup
    return None


def local_only_place(path) -> str | None:
    """The commands.json key whose `local_only: true` covers a directory an agent's lane reads
    whole beyond its checkout (a sibling, a `wb allow --path` grant, a standing `paths` entry;
    "*", the lane off, reads them all), or None: a key the directory is in or holds, by real
    path (in lower case where the file system ignores case, as repo_entries), then the repo it
    is in: local_only's whole marks when it is that repo's top, else an entry at or above it."""
    from . import context, gitops
    if not (marks := _marks()):
        return None
    if path == "*":
        return marks[0]
    path = Path(path).expanduser()
    fold = store.folds_case(path)
    real = lambda p: os.path.realpath(Path(p).expanduser()).lower() if fold \
        else os.path.realpath(Path(p).expanduser())  # noqa: E731
    place = real(path)

    def within(p: str, d: str) -> bool:
        return p == d or p.startswith(d.rstrip(os.sep) + os.sep)
    for key in marks:
        if within(place, real(key)) or within(real(key), place):
            return key
    try:
        top = gitops.toplevel(path)
    except gitops.GitError:
        return None
    if real(top) == place:
        return local_only(top)
    rel = os.path.relpath(place, real(top))
    return next((k for k, v in context.repo_entries(top, rel) if v.get("local_only") is True), None)


def local_only_reach(repo, sub: str | None = None, base: str | None = None,
                     places=()) -> tuple[str, str] | None:
    """The one check of what an API task may read (#1267, #1268, #1269, the bar loop,
    2026-11-24): the first local-only mark on anything its lane holds, as (key, how), or None.
    ``how`` is '' for its checkout (the repo, its submodules, its superprojects: local_only),
    else the word of the ``places`` entry, (path, how), that reaches it: its siblings, its
    `--allow-path`/`wb allow --path` grants, commands.json's standing `paths`. Only the repo was
    asked, so a local-only repo linked as a sibling or granted by path went to the provider.
    wb start, its busy opt-in and the mod's dry run (cli.lane_places), and the runner before
    every round (runner.local_only_now) ask this."""
    if key := local_only(repo, sub, base):
        return key, ""
    for path, how in places:
        if key := local_only_place(path):
            return key, how
    return None


def home_path(p) -> str:
    """A path as the user typed it: under the home directory with `~`."""
    p = str(p)
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home.rstrip("/") + "/") else p


def local_only_line(where_: str, why: str, role: str | None, model_id: str, asked: bool,
                    how: str = "") -> str:
    """wb start's exit-3 line for an API model in a local-only spot: the repo, why it is
    local-only, the role (or the --model), and the choices. Never a GPU model instead (#1008's
    decision 2). The role's way off API is routes.off_api's: `wb route <role> gpu` under Agents
    API changed nothing, and the retry was refused the same way (#1218, 2026-11-24). ``how``: a
    place beyond the repo the task's lane would read it by (local_only_reach), and one more
    choice when that is this command line's flag (#1268)."""
    from . import routes  # routes imports this module
    if asked:
        what = f"the task's model is API ({model_id}, --model)"
        fix = "a GPU model"
    else:
        what = f"the {role} role is on API ({model_id})"
        off = routes.off_api(role) or routes.off_api(None)  # a start on API: never None
        fix = f"{off[1]} ({off[2]}, the user's yes)"
    lift = ("the user drops --local-only for this task" if why == "--local-only"
            else "the user lifts local_only there" if how
            else "the user lifts local_only for this repo")
    if how:
        where_ += f", which the task's lane reads ({how}),"
        if how.startswith("--"):
            fix += f", without {how}"
    return f"{where_} is local-only ({why}): {what}. Choices: {fix}, or {lift}."


def since_start_line(task, key: str = "", how: str = "") -> str:
    """wb next/feedback/resume's exit-3 line for an API task whose repo became local-only after
    it started: its model is fixed per task, so there is nothing to switch to. ``how``: what
    beyond the repo its lane reads the marked ``key`` by (local_only_reach, #1268)."""
    pid = task.spec.get("provider") or str(task.spec.get("model") or "").split("/", 1)[0]
    what = f"{home_path(key)}, which its lane reads ({how}), is" if how else "this repo is"
    return (f"{what} local-only now; the task's model is API ({pid}): "
            f"wb takeover {task.name} or wb discard {task.name}")


def is_api_task(task) -> bool:
    """An API task, by task.json `via` (#1016), GPU when unset: every older task reads as GPU."""
    return task.spec.get("via") == "api"


def standing_refusal(entry: str, advice: bool = True) -> str:
    """_worktree_setup's exit-3 line for a commands.json `copy` entry that holds a credential
    (holds_credential): an API task never takes it, named or not (#1322; naming every standing
    entry on every start was #1021's way, a step that guarded nothing the user hadn't set up).
    ``advice`` False: the busy opt-in's note, its role on the GPU already."""
    fix = " (run on the GPU, or keep the secret in an environment variable)" if advice else ""
    return (f"commands.json copies {entry} into every task and it holds a credential: "
            f"an API task doesn't take it{fix}")
