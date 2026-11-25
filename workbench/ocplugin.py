"""The OpenCode plugin (opencode-plugin/): its id, its install and what wb doctor says of it (#1311).

OpenCode installs it itself, the owner's choice (#1357, 2026-11-25: "installs through OpenCode's
own plugin mechanism, staying close to OpenCode"): `opencode plugin add` with GIT_SPEC, a Git
package with npm's `::path:` subdirectory selector, which OpenCode 2.4.1 accepts and follows on its
branch (its docs, plugins.mdx *Manage*; cli/src/commands/handlers/plugin/add.ts writes the
`plugins` entry). `wb setup --opencode` is the fallback, for a clone: the entry points at this
copy's opencode-plugin/, written through models.own_config, and either way it carries `options`
with this copy's bin/wb and the WB_* paths set where setup ran, since OpenCode's background service
starts without the user's shell (#1296's host-shape decision).

Inside an agent the plugin never runs: every task's config layer removes it by id (PLUGIN_OFF,
context.layer), which OpenCode applies after the user's global `plugins`
(core/src/plugin/supervisor.ts `remove`; core/src/config.ts: the global files load below
OPENCODE_CONFIG), and the plugin does nothing under WB_TASK besides (#1363)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import models, store

PKG_ROOT = Path(__file__).resolve().parent.parent
DIR = PKG_ROOT / "opencode-plugin"
PLUGIN_ID = "workbench"           # server.ts's and tui.ts's `id`
PLUGIN_OFF = f"-{PLUGIN_ID}"      # OpenCode's remove-by-id entry: in every task's layer
PACKAGE = "opencode-workbench"    # opencode-plugin/package.json's name: how a copy is told apart
REPO = "a-schaefers/workbench"
GIT_SPEC = f"github:{REPO}#main::path:opencode-plugin"
ADD = f"opencode plugin add '{GIT_SPEC}'"
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
    """Whether a `plugins` entry loads this plugin: a Git spec of REPO's opencode-plugin/, or a
    local directory holding a copy (that copy may be gone: still ours, said as missing)."""
    spec = target(entry)
    if not spec or spec.startswith("-"):
        return False
    if (d := local_dir(spec, base)) is not None:
        # a copy, or one that is gone (a clone moved or removed): said as missing, never another
        # project's directory that happens to share the name
        return is_copy(d) or (d.name == "opencode-plugin" and not d.exists())
    # a Git install of opencode-plugin/: this repo's, or a fork's (npm's `::path:` selector)
    return spec.split("#", 1)[-1].endswith("::path:opencode-plugin") or (
        REPO in spec.lower() and "opencode-plugin" in spec)


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
    user's OPENCODE_CONFIG_CONTENT, plus the global plugins/ directory OpenCode discovers."""
    out = []
    plugins_dir = models._config_dir() / "plugins"
    try:
        found = sorted(d for d in plugins_dir.iterdir() if d.is_dir() and is_copy(d))
    except OSError:
        found = []
    out += [{"where": str(plugins_dir), "spec": str(d), "kind": "discovered", "dir": d, "options": {}}
            for d in found]
    layers = [(str(p), p.parent, layer) for p, layer in models._layers()]
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
    change = (f'add {json.dumps({"package": str(DIR), "options": opts})} to "plugins" '
              f"(or run {ADD})")
    lines = []
    with models.own_config(change) as (path, own):
        plugins = own.get("plugins", [])
        if not isinstance(plugins, list):
            raise RuntimeError(f'{path}: "plugins" isn\'t a list; make this change by hand: {change}')
        at = [i for i, e in enumerate(plugins) if ours(e, path.parent)]
        found = [f["dir"] for f in installs() if f["kind"] == "discovered"]
        if not at and found:  # OpenCode's plugins directory loads it: an entry would be a duplicate
            lines.append(f"--  {found[0]} loads the OpenCode plugin already (OpenCode's plugins "
                         f"directory), without options: it runs wb by name; left as it is")
        elif not at:
            plugins.append({"package": str(DIR), "options": opts})
            lines.append(f"ok  the OpenCode plugin {DIR} added to {path} (made)")
        else:
            i = at[0]  # the one OpenCode loads; a second of the same id is a duplicate it refuses
            spec = target(plugins[i])
            old = plugins[i].get("options") if isinstance(plugins[i], dict) else None
            d = local_dir(spec, path.parent)
            if d is not None and d.resolve() != DIR.resolve():
                plugins[i] = {"package": str(DIR), "options": opts}
                lines.append(f"ok  the OpenCode plugin in {path} → {DIR} (was {spec})")
            else:
                plugins[i] = {"package": spec, "options": {**(old if isinstance(old, dict) else {}), **opts}}
                lines.append(f"ok  the OpenCode plugin {spec} in {path}: its options name {opts['wb']}")
            lines += [f"--  {path} loads the plugin twice ({target(plugins[j])} too): remove one by hand"
                      for j in at[1:]]
        if PLUGIN_OFF in plugins:
            lines.append(f'--  "{PLUGIN_OFF}" in {path} turns it off: left as it is')
        if plugins or "plugins" in own:
            own["plugins"] = plugins
    return lines


def doctor_lines() -> list[tuple[bool | None, str]]:
    """wb doctor's lines for the OpenCode plugin: (True ok, False BAD, None informs), naming each
    install and which wb it runs. Optional: none installed only informs."""
    found = installs()
    on = [f for f in found if f["kind"] != "off"]
    if not on:
        return [(None, f"the OpenCode plugin (Workbench inside OpenCode) isn't installed (optional): "
                       f"{ADD}, or wb setup --opencode from a clone")]
    out: list[tuple[bool | None, str]] = []
    which = shutil.which("wb")
    for f in on:
        d, opts = f["dir"], f["options"]
        how = {"git": "OpenCode's own install, from Git",
               "package": "OpenCode's own install, from npm",
               "discovered": "found in OpenCode's plugins directory",
               "local": "this clone's opencode-plugin/" if d and d.resolve() == DIR.resolve()
               else f"another copy's: {d}"}[f["kind"]]
        if d is not None and not is_copy(d):
            out.append((False, f"the OpenCode plugin in {f['where']} names {d}, which holds no copy of "
                               f"it: wb setup --opencode from your clone"))
            continue
        wb = opts.get("wb") if isinstance(opts.get("wb"), str) else None
        if wb:
            good = os.path.isabs(wb) and os.path.isfile(wb) and os.access(wb, os.X_OK)
            out.append((good, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) runs {wb}"
                              + ("" if good else ": it isn't there; wb setup --opencode from your clone")))
        else:
            out.append((True, f"the OpenCode plugin {f['spec']} ({how}, in {f['where']}) runs wb by "
                              f"name from OpenCode's PATH ({which or 'none on this shell'}'s here; the "
                              f"background service's may differ: wb setup --opencode names it)"))
    for f in found:
        if f["kind"] == "off":
            out.append((None, f'"{PLUGIN_OFF}" in {f["where"]} turns the OpenCode plugin off'))
    return out
