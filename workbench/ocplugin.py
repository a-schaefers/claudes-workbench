"""The OpenCode plugin (opencode-plugin/): its id, its install and what wb doctor says of it (#1311).

OpenCode installs it itself, the owner's choice (#1357, 2026-11-25: "installs through OpenCode's
own plugin mechanism, staying close to OpenCode"): `opencode plugin add` with GIT_SPEC, this
repository as a Git package (the root package.json), which OpenCode 2.4.1 installs into its package
cache and follows on its branch (its docs, plugins.mdx *Manage*; cli/src/commands/handlers/plugin/
add.ts writes the `plugins` entry). The package's `files` bring wb with the plugin (bin/, workbench/,
agent/, skill/), and the plugin runs that wb when its entry names none (wb.ts, OWN): #1311's review
(2026-11-25) found the first form, `::path:opencode-plugin`, brought the plugin alone, which could
do nothing without a clone. `wb setup --opencode` is the fallback, for a clone: the entry points at
this copy's opencode-plugin/, written through models.own_config, and carries `options` with this
copy's bin/wb and the WB_* paths set where setup ran, since OpenCode's background service starts
without the user's shell (#1296's host-shape decision); after `opencode plugin add` it gives that
entry the options.

Inside an agent the plugin never runs: every task's config layer removes it by id (PLUGIN_OFF,
context.layer), which OpenCode applies after the user's global `plugins`
(core/src/plugin/supervisor.ts `remove`; core/src/config.ts: the global files load below
OPENCODE_CONFIG), and the plugin does nothing under WB_TASK besides (#1363). Where OpenCode's
package cache holds it, or is about to (in_cache), the agent's lane keeps that cache out
(context.not_writable_paths): wb itself runs from there."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from . import gitops, models, store

PKG_ROOT = Path(__file__).resolve().parent.parent
DIR = PKG_ROOT / "opencode-plugin"
PLUGIN_ID = "workbench"           # server.ts's and tui.ts's `id`
PLUGIN_OFF = f"-{PLUGIN_ID}"      # OpenCode's remove-by-id entry: in every task's layer
# the name in both package.json files (the root's, which a Git install fetches, and
# opencode-plugin/'s, which a clone's entry loads): how a copy is told apart
PACKAGE = "opencode-workbench"
REPO = "a-schaefers/workbench"
GIT_SPEC = f"github:{REPO}#main"
ADD = f"opencode plugin add '{GIT_SPEC}'"
# what the TUI's toast and doctor say when no wb is found (tui.ts's GET_WB, word for word)
GET_WB = f"{ADD} brings it, or bin/wb setup --opencode in a clone"
# npm's `::path:` subdirectory selector on opencode-plugin/: a Git install of the plugin alone, no
# wb (this module's first form, and a fork's that copies it)
SUBDIR = "::path:opencode-plugin"
# the directories of OpenCode's config directory whose entries it loads as plugins
# (core/src/plugin/source-directory.ts `names`)
DISCOVERED = ("plugin", "plugins")
GIT_SCHEMES = ("github:", "gitlab:", "bitbucket:", "git+", "git:", "git@")  # npm's Git specs


def target(entry) -> str | None:
    """A `plugins` entry's package (a string, or the object form's `package`)."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("package"), str):
        return entry["package"]
    return None


def local_dir(spec: str, base: Path) -> Path | None:
    """The directory a local entry names, as OpenCode resolves it (core/src/config/plugin/source.ts:
    file:// URLs, ./ and ../ from the config file's directory, absolute paths); None for a package."""
    if spec.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return Path(unquote(urlparse(spec).path))
    if spec.startswith(("./", "../")):
        return (base / spec).resolve()
    return Path(spec) if os.path.isabs(spec) else None


def is_copy(d: Path) -> bool:
    """A directory holding a copy of this plugin (its package.json names PACKAGE)."""
    try:
        return json.loads((d / "package.json").read_text()).get("name") == PACKAGE
    except (OSError, ValueError, AttributeError):
        return False


def ours(entry, base: Path) -> bool:
    """Whether a `plugins` entry loads this plugin: a Git spec of REPO (or of any repository's
    opencode-plugin/), or a local directory holding a copy (that copy may be gone: still ours,
    said as missing)."""
    spec = target(entry)
    if not spec or spec.startswith("-"):
        return False
    if (d := local_dir(spec, base)) is not None:
        # a copy, or one that is gone (a clone moved or removed): said as missing, never another
        # project's directory that happens to share the name
        return is_copy(d) or (d.name == "opencode-plugin" and not d.exists())
    # a Git install of this repository, or of opencode-plugin/ alone (npm's `::path:` selector,
    # a fork's too), or any Git spec OpenCode installed this package from (a fork, a git+file://
    # clone: doctor called a working install missing, #1311's live check, 2026-11-25)
    return spec.split("#", 1)[-1].endswith(SUBDIR) or bool(
        re.search(rf"(^|[/:]){re.escape(REPO)}(\.git)?($|#|::)", spec.lower())) or (
        spec.startswith(GIT_SCHEMES) and copy_of(spec) is not None)


def brings_wb(spec: str) -> bool:
    """Whether a Git or npm install of ``spec`` brings wb: the root package does, opencode-plugin/
    alone (SUBDIR) doesn't."""
    return not spec.split("#", 1)[-1].endswith(SUBDIR)


def package_caches() -> list[Path]:
    """OpenCode's package caches (packages/util/src/npm.ts: global.cache/npm): under
    XDG_CACHE_HOME, else ~/.cache, and ~/.cache's too when XDG_CACHE_HOME moves it (the lane
    keeps both)."""
    home = Path.home() / ".cache" / "opencode" / "npm"
    moved = os.environ.get("XDG_CACHE_HOME") or ""
    return [home, *([Path(moved) / "opencode" / "npm"] if os.path.isabs(moved) else [])]


def _git_key(spec: str) -> str:
    """The cache directory OpenCode installs a Git ``spec`` in, as packages/util/src/npm.ts names
    it (`key`, `gitSlug`): git-<the repository's last name>-<sha256 of the spec, 12 digits>."""
    from urllib.parse import unquote
    base = spec.split("#", 1)[0]
    try:
        base = unquote(base, errors="strict")
    except UnicodeDecodeError:
        pass
    last = re.split(r"[/:\\]", re.sub(r"\.git$", "", base, flags=re.I))[-1]
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", last).strip("-") or "repository"
    return f"git-{slug}-{hashlib.sha256(spec.encode()).hexdigest()[:12]}"


def runs_from_cache(root: Path | None = None) -> bool:
    """Whether this wb is OpenCode's install: a package in OpenCode's package cache, which
    OpenCode fetches and replaces, not a git clone. wb update and wb doctor said git's error
    for it (#1311's third review, 2026-11-25)."""
    try:
        here = (root or PKG_ROOT).resolve()
        return any(here.is_relative_to(npm.resolve()) for npm in package_caches())
    except OSError:
        return False


def updated_by() -> str:
    """What wb update and wb doctor say of OpenCode's install (runs_from_cache): the `plugins`
    entry OpenCode installed this copy from, GIT_SPEC when none names it (a removed entry)."""
    here, spec = PKG_ROOT.resolve(), GIT_SPEC
    try:
        spec = next((f["spec"] for f in installs() if f["kind"] == "git"
                     and (c := copy_of(f["spec"])) is not None and here.is_relative_to(c.resolve().parent.parent.parent)),
                    GIT_SPEC)
    except (RuntimeError, OSError):
        pass
    return f"OpenCode updates it (it follows {spec}, checked at its start and daily)"


def brought_wb(d: Path) -> Path | None:
    """The wb the plugin in directory ``d`` runs without options (wb.ts's OWN: ../bin/wb from the
    server part's own file, as Bun resolves it through links): read from d's package.json
    `exports`, so the repository root (the root package.json, `opencode plugin add <clone>`) is
    d/bin/wb and opencode-plugin/ is its parent's. ``d.parent / "bin/wb"`` named the wrong one for
    a root entry and for a copy in OpenCode's plugins/ directory (#1311's third review)."""
    try:
        real = d.resolve()
        exports = json.loads((real / "package.json").read_text()).get("exports")
    except (OSError, ValueError, AttributeError):
        return None
    entry = exports if isinstance(exports, str) else (
        exports.get("./server") or exports.get(".")) if isinstance(exports, dict) else None
    if not isinstance(entry, str):
        return None
    return (real / entry).resolve().parent.parent / "bin" / "wb"


def loaded_from(root: Path | None = None) -> bool:
    """Whether OpenCode's config loads the plugin from this clone (an entry naming its
    opencode-plugin/ or the clone itself) or runs this clone's wb (an entry's options): wb update
    then updates the clone for OpenCode (#1321's findings 4 and 15)."""
    root = (root or PKG_ROOT).resolve()
    try:
        found = installs()
    except (RuntimeError, OSError):
        return False
    for f in found:
        if f["kind"] == "off":
            continue
        d, wb = f.get("dir"), f["options"].get("wb")
        if d is not None and d.resolve() in (root, root / "opencode-plugin"):
            return True
        if d is not None and (note := copy_info(d)) and Path(str(note.get("clone"))).resolve() == root:
            return True
        if isinstance(wb, str) and os.path.isabs(wb) and Path(wb).resolve() == root / "bin" / "wb":
            return True
    return False


COPY_NOTE = "copy.json"  # in each copy's directory: {commit, tree, clone}


def copies_dir() -> Path:
    """Where wb setup --opencode keeps the copies of opencode-plugin/ that OpenCode loads (#1321's
    finding 16): beside wb patch init's clone, out of every lane's writable places, and readable
    there, as the agent's OpenCode imports the module to learn its id before its layer removes it."""
    return Path.home() / ".local/share/workbench/opencode-plugin"


def copy_info(d: Path) -> dict | None:
    """A copy's note (its commit, its tree, the clone it came from) when ``d`` is a copy wb made,
    copies_dir()/<tree>/opencode-plugin; else None."""
    try:
        if d.resolve().parent.parent != copies_dir().resolve():
            return None
        note = json.loads((d.parent / COPY_NOTE).read_text())
    except (OSError, ValueError):
        return None
    return note if isinstance(note, dict) else None


def shared_files(root: Path | None = None, rev: str = "HEAD") -> list[tuple[str, str]]:
    """(path, blob) at the clone's ``rev`` of each Claude Code mod file the plugin imports from beside
    it (`../mod/hooks/…`: spans.ts's splash.ts and apprentice.ts, #1333), as HEAD's root
    package.json `files` name them under mod/ (the Git install's list), there; a copy carries them."""
    root = root or PKG_ROOT
    try:
        files = json.loads(gitops.git(root, "show", f"{rev}:package.json")).get("files")
        names = [f for f in files if isinstance(f, str) and f.startswith("mod/")] if isinstance(files, list) else []
        if not names:
            return []
        out = gitops.git_bytes(root, "ls-tree", "-z", rev, "--", *names)
    except (gitops.GitError, OSError, ValueError):
        return []
    rows = [r.partition(b"\t") for r in out.split(b"\0") if r]
    return sorted((name.decode("utf-8", "surrogateescape"), meta.split()[2].decode())
                  for meta, _, name in rows if meta.split()[1:2] == [b"blob"])


def head_tree(root: Path | None = None) -> tuple[str, str] | None:
    """(the commit, the copy's key) at the clone's HEAD: opencode-plugin/'s tree, or with the mod
    files it imports (shared_files) a hash of that tree and their blobs, so a change to one of them
    alone makes a new copy too; None off git, or without opencode-plugin/."""
    try:
        out = gitops.git(root or PKG_ROOT, "rev-parse", "HEAD", "HEAD:opencode-plugin").split()
    except (gitops.GitError, OSError):
        return None
    if len(out) != 2:
        return None
    if shared := shared_files(root, out[0]):
        joined = out[1] + "".join(f"\0{path}\0{blob}" for path, blob in shared)
        return out[0], hashlib.sha1(joined.encode()).hexdigest()
    return out[0], out[1]


def make_copy(root: Path | None = None) -> tuple[Path, str] | None:
    """opencode-plugin/ as committed at the clone's HEAD (its tests left out), with the mod files it
    imports beside it (shared_files, at <tree>/mod/…), copied once per key (head_tree's)
    to copies_dir()/<tree>/opencode-plugin: (that directory, the commit it was copied at). What wb
    setup --opencode's entry loads: OpenCode watches a configured local plugin and reloads it on
    every change (core/src/config/plugin/source.ts), so an entry on the clone's live
    opencode-plugin/ ran each save uncommitted, a patch under test with it, and wb patch init
    deleted it (#1321's finding 16). None off git: the live directory then."""
    root = root or PKG_ROOT
    if (got := head_tree(root)) is None:
        return None
    commit, tree = got
    final = copies_dir() / tree[:12]
    if (note := copy_info(final / "opencode-plugin")) is not None:  # this tree's, made before
        return final / "opencode-plugin", str(note.get("commit") or commit)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        for row in gitops.git_bytes(root, "ls-tree", "-r", "-z", f"{commit}:opencode-plugin").split(b"\0"):
            meta, _, name = row.partition(b"\t")
            mode, kind, sha = (meta.split() + [b"", b"", b""])[:3]
            rel = name.decode("utf-8", "surrogateescape")
            if kind != b"blob" or mode == b"120000" or not rel or rel.split("/")[0] == "test":
                continue
            out = tmp / "opencode-plugin" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(gitops.git_bytes(root, "cat-file", "blob", sha.decode()))
            out.chmod(0o755 if mode == b"100755" else 0o644)
        for rel, blob in shared_files(root, commit):  # beside it, where its `../mod/hooks/…` imports look
            out = tmp / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(gitops.git_bytes(root, "cat-file", "blob", blob))
            out.chmod(0o644)
        (tmp / COPY_NOTE).write_text(json.dumps({"commit": commit, "tree": tree, "clone": str(root.resolve())}))
        try:
            os.rename(tmp, final)
        except OSError:  # another wb made it meanwhile
            if copy_info(final / "opencode-plugin") is None:
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return final / "opencode-plugin", commit


def _prune_copies(keep: set[Path]) -> None:
    """The copies no entry loads any more, but ``keep``: the new one and those the file named before
    the change (a running OpenCode may still hold one until it reloads)."""
    keep = {k.resolve() for k in keep}
    try:
        old = [d for d in copies_dir().iterdir() if re.fullmatch(r"[0-9a-f]{12}", d.name)]
    except OSError:
        return
    for d in old:
        if d.resolve() not in keep and copy_info(d / "opencode-plugin") is not None:
            shutil.rmtree(d, ignore_errors=True)


def copy_of(spec: str) -> Path | None:
    """The package OpenCode installed for the Git ``spec``: its newest generation's (not one it
    rejected), or None before OpenCode has installed it."""
    for npm in package_caches():
        root = npm / _git_key(spec)
        try:
            names = os.listdir(root)
        except OSError:
            continue
        gens = sorted((n for n in names if n.isdigit() and f"{n}.rejected" not in names), key=int)
        if gens and is_copy(d := root / gens[-1] / "node_modules" / PACKAGE):
            return d
    return None


def cached() -> list[Path]:
    """The copies of this package in OpenCode's package caches, each generation's
    `<key>/<generation>/node_modules/opencode-workbench`, newest generation last."""
    out = []
    for npm in package_caches():
        try:
            found = npm.glob(f"*/*/node_modules/{PACKAGE}")
            out += sorted((d for d in found if is_copy(d)), key=lambda d: d.parent.parent.name.zfill(20))
        except OSError:
            pass
    return out


def in_cache() -> bool:
    """Whether OpenCode's package cache holds this plugin, or is about to: a generation of it
    there, or a Git or npm `plugins` entry of it, which OpenCode installs at its next start. wb
    and the plugin run from there as the user, so the lane keeps the cache out then
    (context.not_writable_paths); else ~/.cache stays whole (#1311's review, 2026-11-25)."""
    if cached():
        return True
    try:
        return any(f["kind"] in ("git", "package") for f in installs())
    except (RuntimeError, OSError):  # a config OpenCode can't read either: it installs nothing
        return False


def options() -> dict:
    """The entry's options: this copy's bin/wb, and each WB_* path set here (store.PATH_VARS,
    absolute), for a service whose environment has neither (the plugin's wb.ts)."""
    env = {v: os.path.abspath(os.path.expanduser(os.environ[v]))
           for v in store.PATH_VARS if v.startswith("WB_") and os.environ.get(v)}
    return {"wb": str(PKG_ROOT / "bin" / "wb"), **({"env": env} if env else {})}


def installs() -> list[dict]:
    """Every entry or discovered directory that loads the plugin, in OpenCode's load order: each
    `{where, spec, kind ("git", "package", "local", "discovered"), dir, options}`; and the
    removals, `{where, spec: PLUGIN_OFF, kind: "off"}`. Read from the files models layers and the
    user's OPENCODE_CONFIG_CONTENT, plus the global plugin/ and plugins/ directories OpenCode discovers."""
    out = []
    for name in DISCOVERED:
        plugins_dir = models._config_dir() / name
        try:
            found = sorted(d for d in plugins_dir.iterdir() if d.is_dir() and is_copy(d))
        except OSError:
            found = []
        out += [{"where": str(plugins_dir), "spec": str(d), "kind": "discovered", "dir": d, "options": {}}
                for d in found]
    try:
        read = models._layers()
    except OSError as e:  # unreadable: wb doctor said BAD for its traceback (#1311's review)
        raise RuntimeError(f"OpenCode's config can't be read ({e.strerror or e})") from None
    layers = [(str(p), p.parent, layer) for p, layer in read]  # objects (models._layers)
    if content := models.users_config_content():
        layers.append((models.CONTENT, Path.cwd(), content))
    for where, base, layer in layers:
        entries = layer.get("plugins")
        for entry in entries if isinstance(entries, list) else []:
            spec = target(entry)
            if spec == PLUGIN_OFF:
                out.append({"where": where, "spec": spec, "kind": "off"})
            elif ours(entry, base):
                d = local_dir(spec, base)
                opts = entry.get("options") if isinstance(entry, dict) else None
                out.append({"where": where, "spec": spec, "dir": d,
                            "kind": "local" if d else "git" if spec.startswith(GIT_SCHEMES) else "package",
                            "options": opts if isinstance(opts, dict) else {}})
    return out


def install() -> list[str]:
    """`wb setup --opencode`: this plugin in OpenCode's config file wb writes (models.own_config),
    with options(). An entry already there keeps its source (OpenCode's own Git install stays
    OpenCode's to update) and gets the options; one pointing at another copy's directory is moved
    to this one; with none, this copy's opencode-plugin/ is added. Its lines, as wb setup prints
    them. RuntimeError (own_config's: a file with comments, two layered files) says the change."""
    opts = options()
    made = make_copy()
    load = made[0] if made else DIR
    change = (f'add {json.dumps({"package": str(load), "options": opts})} to "plugins" '
              f"(or run {ADD})")
    lines, before = [], set()
    with models.own_config(change) as (path, own):
        plugins = own.get("plugins", [])
        if not isinstance(plugins, list):
            raise RuntimeError(f'{path}: "plugins" isn\'t a list; make this change by hand: {change}')
        at = [i for i, e in enumerate(plugins) if ours(e, path.parent)]
        before = {d.parent for i in at if (d := local_dir(target(plugins[i]) or "", path.parent)) is not None
                  and copy_info(d) is not None}
        found = [f["dir"] for f in installs() if f["kind"] == "discovered"]
        if not at and found:  # OpenCode's plugins directory loads it: an entry would be a duplicate
            lines.append(f"--  {found[0]} loads the OpenCode plugin already (OpenCode's plugins "
                         f"directory), without options: it runs wb by name; left as it is")
        elif not at:
            plugins.append({"package": str(load), "options": opts})
            lines.append(f"ok  the OpenCode plugin {load} added to {path} (made)")
        else:
            i = at[0]  # the one OpenCode loads; a second of the same id is a duplicate it refuses
            spec = target(plugins[i])
            old = plugins[i].get("options") if isinstance(plugins[i], dict) else None
            d = local_dir(spec, path.parent)
            if d is not None and d.resolve() != load.resolve():
                plugins[i] = {"package": str(load), "options": opts}
                lines.append(f"ok  the OpenCode plugin in {path} → {load} (was {spec})")
            else:
                plugins[i] = {"package": spec, "options": {**(old if isinstance(old, dict) else {}), **opts}}
                lines.append(f"ok  the OpenCode plugin {spec} in {path}: its options name {opts['wb']}")
            lines += [f"--  {path} loads the plugin twice ({target(plugins[j])} too): remove one by hand"
                      for j in at[1:]]
        if PLUGIN_OFF in plugins:
            lines.append(f'--  "{PLUGIN_OFF}" in {path} turns it off: left as it is')
        if plugins or "plugins" in own:
            own["plugins"] = plugins
    if made:
        _prune_copies({load.parent, *before})
        lines.append(f"ok  OpenCode loads a copy of {DIR} as committed at {made[1][:7]}, not the live "
                     f"directory it reloads on every change: wb update refreshes it")
        if gitops.git(PKG_ROOT, "status", "--porcelain", "--", "opencode-plugin", check=False).strip():
            lines.append("--  opencode-plugin/ has uncommitted changes, which the copy leaves out: commit "
                         "them, then wb setup --opencode")
    else:
        lines.append(f"--  {PKG_ROOT} isn't a git clone: OpenCode loads its live opencode-plugin/, "
                     f"reloading it on every change")
    return lines


def refresh() -> list[str]:
    """wb update's: an entry loading a copy of this clone (or its live opencode-plugin/) moved to
    the copy of HEAD's, when opencode-plugin/ changed; [] when nothing did (a rewrite of the
    config would reload OpenCode's plugins for nothing)."""
    try:
        found = installs()
    except (RuntimeError, OSError):
        return []
    here, made = PKG_ROOT.resolve(), None
    for f in found:
        d = f.get("dir")
        if f["kind"] != "local" or d is None:
            continue
        note = copy_info(d)
        if d.resolve() in (DIR.resolve(), here) or (note and Path(str(note.get("clone"))).resolve() == here):
            made = made or make_copy()
            if made and d.resolve() != made[0].resolve():
                return install()
    return []


def doctor_lines() -> list[tuple[bool | None, str]]:
    """wb doctor's lines for the OpenCode plugin: (True ok, False BAD, None informs), naming each
    install and which wb it runs. Optional: none installed only informs."""
    found = installs()
    on = [f for f in found if f["kind"] != "off"]
    if not on:
        return [(None, f"the OpenCode plugin (Workbench inside OpenCode) isn't installed (optional): "
                       f"{ADD} (it brings its wb), or wb setup --opencode from this clone")]
    out: list[tuple[bool | None, str]] = []
    which = shutil.which("wb")
    for f in on:
        d, opts = f["dir"], f["options"]
        mine = d is not None and d.resolve() in (DIR.resolve(), PKG_ROOT.resolve())
        note = copy_info(d) if d is not None and f["kind"] == "local" else None
        clone = Path(str(note.get("clone"))) if note else None
        how = {"git": "OpenCode's own install, from Git",
               "package": "OpenCode's own install, from npm",
               "discovered": "found in OpenCode's plugins directory",
               "local": (f"a copy of {'this clone' if clone.resolve() == PKG_ROOT.resolve() else clone}'s "
                         f"opencode-plugin/ as committed at {str(note.get('commit'))[:7]}") if note
               else ("this clone's opencode-plugin/" if d.resolve() == DIR.resolve() else "this clone")
               if mine else f"another copy's: {d}"}[f["kind"]]
        if d is not None and not is_copy(d):
            out.append((False, f"the OpenCode plugin in {f['where']} names {d}, which holds no copy of "
                               f"it: wb setup --opencode from your Workbench clone"))
            continue
        after: list[tuple[bool | None, str]] = []
        if note and (now := head_tree(clone)) and now[1] != note.get("tree"):
            after.append((None, f"the OpenCode plugin's copy is as of {str(note.get('commit'))[:7]}, older "
                                f"than {clone}'s opencode-plugin/ at {now[0][:7]}: wb update (or wb setup "
                                f"--opencode there) refreshes it"))
        elif mine and f["kind"] == "local":  # the live directory OpenCode reloads on every save
            after.append((None, f"the OpenCode plugin loads {d} as it is, reloaded on every change there "
                                f"(#1321): wb setup --opencode loads a copy of what is committed"))
        wb = opts.get("wb") if isinstance(opts.get("wb"), str) else None
        # without options the plugin runs the wb its package brings (wb.ts's OWN, ../bin/wb):
        # a clone's, or the Git install's in OpenCode's package cache (#1311's review)
        brings = f["kind"] in ("git", "package") and brings_wb(f["spec"])
        pkg = copy_of(f["spec"]) if brings else None   # the root package: bin/wb inside it
        own = brought_wb(d) if d is not None else brought_wb(pkg) if pkg else None
        if wb:
            good = os.path.isabs(wb) and os.path.isfile(wb) and os.access(wb, os.X_OK)
            out.append((good, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) runs {wb}"
                              + ("" if good else ": it isn't there; wb setup --opencode from your Workbench clone")))
        elif own is not None and os.path.isfile(own) and os.access(own, os.X_OK):
            out.append((True, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) runs the wb "
                              f"it brings, {own}"))
        elif brings and pkg is None:
            out.append((None, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) isn't in "
                              f"OpenCode's package cache yet: OpenCode installs it, with its wb, at "
                              f"its next start"))
        elif not which:  # opencode-plugin/ alone (SUBDIR), or a copy without bin/wb beside it
            out.append((False, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) brings no "
                               f"wb and finds none on PATH: {GET_WB}"))
        else:
            out.append((True, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) runs wb by "
                              f"name from OpenCode's PATH ({which}'s here; the background "
                              f"service's may differ: wb setup --opencode names it)"))
        out += after
    for f in found:
        if f["kind"] == "off":
            out.append((None, f'"{PLUGIN_OFF}" in {f["where"]} turns the OpenCode plugin off'))
    return out
