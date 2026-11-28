"""Posting to GitHub (the owner, 2026-11-27): what Claude may send upstream for the user, an issue,
a comment or a pull request (`wb patch send`), and when. The host's send dialog held work for hours
while the user was away ("i walk away from the computer, so i cannot approve your requests to post
... work stalls"), so the user picks one mode, beside routes.json in upstream.json:

- `auto`: sent at once, no dialog;
- `daily` (the default): never a dialog mid-work. A post waits in the outbox ($WB_HOME/outbox/),
  and at the user's first turn of each day the host asks once (`wb upstream ask`): Send all, or
  Not today. Settings shows the outbox with the same buttons; `wb upstream send` typed by the user
  sends it;
- `never`: fixes stay local patches (`wb patch`), posts stay drafts in the outbox; nothing goes to
  GitHub for the user.

Only the user sets the mode (Settings, or `wb upstream mode` at their own terminal): a model's call
is refused (store.under_model), and both hosts deny an edit of upstream.json and of its one-shot
yes, upstream-ok.json, as they do routes.json and route-ok.json. A post is kept as data (its kind,
repo, title, number, body) and sent with fixed gh arguments: never a shell line replayed."""
from __future__ import annotations

import os
import re
import secrets
import subprocess
from datetime import date
from pathlib import Path

from . import routes, store

MODES = ("auto", "daily", "never")
# `ask`: the host's dialog per send, as before; no Settings stop, only upstream.json written by hand
READ_MODES = (*MODES, "ask")
DEFAULT = "daily"
KINDS = ("issue", "comment", "pr")
OK = "upstream-ok.json"  # the host's daily dialog's yes to one Send all: its ids and when
OK_SECS = 120
ID = re.compile(r"[0-9a-f]{8}")
TITLE_MAX = 256
BODY_MAX = 60_000  # GitHub's body limit is 65,536 characters
# Settings' three stops, and what each one says (settings.py's row, `wb upstream`)
STOPS = [("auto", "Automatic", "Claude posts issues, comments and pull requests to GitHub without asking."),
         ("daily", "Once a day (default)", "Posts wait in the outbox; you're asked once, at your first turn of "
          "the day, and nothing waits on you meanwhile."),
         ("never", "Never", "Fixes stay local patches and posts stay drafts in the outbox: nothing is posted "
          "for you.")]
SAID = {v: says for v, _, says in STOPS}
ONLY_USER = ("only the user sets how Claude posts to GitHub: Settings › Posting to GitHub, or "
             "wb upstream mode auto|daily|never in their own terminal")


class UpstreamError(RuntimeError):
    """A refusal or a failure, one line (cli dies with it)."""


def path() -> Path:
    """upstream.json, beside routes.json ($WB_ROUTES's directory, else ~/.config/workbench)."""
    return routes.path().parent / "upstream.json"


def mode() -> str:
    """The user's mode; missing or unreadable is the default, never auto (fail closed)."""
    doc = store.read_json(path(), None)
    m = doc.get("mode") if isinstance(doc, dict) else None
    return m if m in READ_MODES else DEFAULT


def set_mode(m: str) -> str:
    if m not in MODES:
        raise UpstreamError(f"{m!r}: the modes are {', '.join(MODES)}")
    if store.under_model():
        raise UpstreamError(f"not changed: {ONLY_USER}")
    store.write_json(path(), {"mode": m, "at": store.now()})
    return f"Posting to GitHub: {SAID[m]}"


def default_repo() -> str:
    """Where a post goes unless --repo says: patches.json's upstream (wb patch's clone tracks
    it), else workbench's own repository."""
    from . import ocplugin, patches
    noted = store.read_json(patches.noted_path(), {})
    up = noted.get("upstream") if isinstance(noted, dict) else None
    return up if isinstance(up, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", up) else ocplugin.REPO


def outbox() -> Path:
    return store.home() / "outbox"


def items() -> list[dict]:
    """The posts waiting, oldest first."""
    out = []
    for f in sorted(outbox().glob("*.json")) if outbox().is_dir() else []:
        row = store.read_json(f, None)
        if isinstance(row, dict) and ID.fullmatch(str(row.get("id", ""))) and row.get("kind") in KINDS:
            out.append(row)
    return sorted(out, key=lambda r: (r.get("at") or 0, r["id"]))


def body_path(item_id: str) -> Path:
    return outbox() / f"{item_id}.md"


def line(row: dict) -> str:
    """One post, as the outbox lists it and the daily dialog shows it."""
    what = {"issue": "issue", "comment": f"comment on #{row.get('number')}",
            "pr": f"pull request pr/{row.get('slug')}"}[row["kind"]]
    title = f": {row['title']}" if row.get("title") else ""
    err = f" (last try: {row['error']})" if row.get("error") else ""
    return f"{row['id']}  {what} on {row.get('repo')}{title}{err}"


def _text(body_file: str) -> str:
    try:
        text = Path(body_file).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise UpstreamError(f"--body-file {body_file}: {e.strerror or e}") from None
    if not text.strip():
        raise UpstreamError(f"--body-file {body_file} is empty")
    if len(text) > BODY_MAX:
        raise UpstreamError(f"--body-file {body_file} is {len(text):,} characters: GitHub takes {BODY_MAX:,} at most")
    return text


def add(kind: str, repo: str, body_file: str, **fields) -> dict:
    """A post into the outbox, its body copied in (the file may change or go before it is sent)."""
    if kind not in KINDS:
        raise UpstreamError(f"{kind!r}: a post is one of {', '.join(KINDS)}")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise UpstreamError(f"{repo!r} isn't owner/name")
    if store.caller_kind() == "agent":  # an agent posts nothing: its reviewer does (#1310)
        raise UpstreamError("an agent posts nothing upstream: ask the reviewer (ask_reviewer)")
    title = " ".join(str(fields.get("title") or "").split())
    if kind in ("issue", "pr") and not title:
        raise UpstreamError("--title is empty")
    if len(title) > TITLE_MAX:
        raise UpstreamError(f"--title is {len(title)} characters: {TITLE_MAX} at most")
    text = _text(body_file)
    item_id = secrets.token_hex(4)
    body_path(item_id).parent.mkdir(parents=True, exist_ok=True)
    body_path(item_id).write_text(text, encoding="utf-8")
    row = {"id": item_id, "kind": kind, "repo": repo, "at": store.now(), "by": store.caller_kind(),
           **{k: v for k, v in fields.items() if v not in (None, "", False)}, "title": title or None}
    store.write_json(outbox() / f"{item_id}.json", {k: v for k, v in row.items() if v is not None})
    return row


def drop(ids: list[str]) -> list[str]:
    gone = []
    for row in items():
        if not ids or row["id"] in ids:
            for f in (outbox() / f"{row['id']}.json", body_path(row["id"])):
                f.unlink(missing_ok=True)
            gone.append(row["id"])
    return gone


def _asked_path() -> Path:
    return store.home() / "upstream-asked.json"


def ask() -> list[dict]:
    """The posts to ask the user about now, marked asked for today: the daily dialog's, once a
    day (the local date), only in daily mode and only when something waits. Under a lock, so two
    hosts (both CLIs open) never both ask."""
    if mode() != "daily":
        return []
    with store.locked(_asked_path().with_name("upstream-asked.lock")):
        waiting = items()
        today = date.today().isoformat()
        if not waiting or (store.read_json(_asked_path(), {}) or {}).get("day") == today:
            return []
        store.write_json(_asked_path(), {"day": today, "at": store.now()})
    return waiting


def may_send(ids: list[str]) -> str:
    """'' when this caller may send the posts ``ids`` now, else why not. The user at a terminal
    (or the web app's Settings, whose child is theirs) always may; a model's call in auto mode;
    otherwise only on the host's daily dialog's yes to exactly these posts (upstream-ok.json)."""
    kind = store.caller_kind()
    if kind == "agent":
        return "an agent posts nothing upstream: ask the reviewer (ask_reviewer)"
    if kind == "terminal" or mode() == "auto":
        return ""
    ok = store.take_approval(OK, OK_SECS)
    if ok is not None and sorted(ok.get("ids") or []) == sorted(ids):
        return ""
    if mode() == "never":
        return "Posting to GitHub is Never (the user's Settings): the posts stay drafts in the outbox"
    return ("the posts wait for the user: they're asked at their first turn of the day, or send them from "
            "Settings › Posting to GitHub, or with wb upstream send in their own terminal")


def gh(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, start_new_session=True,
                          env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})


def send_one(row: dict, send_pr) -> str:
    """Post one; its URL or what was said. ``send_pr``: wb patch send for a pull request
    (cli's, which knows the install), called as the user's approved send."""
    body = str(body_path(row["id"]))
    if row["kind"] == "pr":
        return send_pr(row, body) or "sent"
    if row["kind"] == "issue":
        argv = ["issue", "create", "-R", row["repo"], "--title", row["title"], "--body-file", body]
    else:
        argv = ["issue", "comment", str(int(row["number"])), "-R", row["repo"], "--body-file", body]
    r = gh(*argv)
    if r.returncode:
        from .patches import scrub
        raise UpstreamError(scrub(r.stderr or r.stdout) or f"gh exit {r.returncode}")
    return (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else "sent"


def send(ids: list[str], send_pr) -> list[str]:
    """Post ``ids`` (every waiting one when empty), each removed once sent; a failure stays, its
    error noted, and the rest go on. Lines to print."""
    waiting = [r for r in items() if not ids or r["id"] in ids]
    if ids and (missing := sorted(set(ids) - {r["id"] for r in waiting})):
        raise UpstreamError(f"not in the outbox: {', '.join(missing)} (wb upstream lists it)")
    if not waiting:
        return ["the outbox is empty"]
    if why := may_send([r["id"] for r in waiting]):
        raise UpstreamError(f"not sent: {why}")
    out = []
    for row in waiting:
        try:
            said = send_one(row, send_pr)
        except Exception as e:  # noqa: BLE001: one post's failure leaves the rest to go
            row["error"], row["tried"] = str(e)[:300], store.now()
            store.write_json(outbox() / f"{row['id']}.json", row)
            out.append(f"not sent {line(row)}")
            continue
        drop([row["id"]])
        out.append(f"sent {row['id']}: {said}")
    return out


def summary() -> list[str]:
    """`wb upstream`: the mode and the outbox."""
    m, waiting = mode(), items()
    out = [f"Posting to GitHub: {m}. {SAID.get(m, 'Each post waits for the host’s dialog (upstream.json says ask).')}"]
    out += [f"  {line(r)}" for r in waiting] or ["  the outbox is empty"]
    if waiting:
        out.append("send: wb upstream send [id …] (the user's own terminal or Settings); drop: wb upstream drop [id …]")
    return out


def queued_line(row: dict) -> str:
    """What a model is told when its post went into the outbox instead of to GitHub."""
    if mode() == "never":
        return (f"kept as a draft, not posted: {line(row)}. Posting to GitHub is Never (the user's Settings); "
                "tell the user it is in the outbox (wb upstream).")
    return (f"queued, not posted yet: {line(row)}. The user is asked once a day, at their first turn; "
            "keep working (wb upstream lists the outbox).")

