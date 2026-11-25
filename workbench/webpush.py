"""Web push for the web chat, standard library only: VAPID (RFC 8292) with ES256 on P-256 done
in plain integers, and pushes with no payload (RFC 8030), so nothing needs encrypting and nothing
the user and Claude write passes through a push service: the page's service worker fetches the
news from the chat itself and shows it (the user, 2026-11-18: phone notifications like their other
app's, without adding infrastructure).

A push service is Google's, Apple's, Mozilla's or Microsoft's; only their hosts are accepted
(PUSH_HOSTS), so the token alone can't make this machine post to an address of its choosing."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import store

# P-256 (FIPS 186-4 D.1.2.3)
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)

# Chrome's are fcm.googleapis.com; Chromium's jmt17.google.com (seen 2026-11-18)
PUSH_HOSTS = (".googleapis.com", ".google.com", ".push.apple.com", ".push.services.mozilla.com",
              ".notify.windows.com")
SUBJECT = "https://github.com/a-schaefers/workbench"  # VAPID's contact: Apple wants one
TTL = 86400               # a phone off for a day still gets the news when it's back
SEND_TIMEOUT = 10
MAX_SUBS = 20             # a phone, a tablet, a laptop or two: more is someone else's loop
CERT = -1                 # a send's status: this machine couldn't check the service's certificate
MAC_CAS = "/etc/ssl/cert.pem"  # macOS's own bundle, for a python.org Python with an empty store


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    (x1, y1), (x2, y2) = p, q
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p == q:
        slope = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def _mul(k: int, p):
    r = None
    while k:
        if k & 1:
            r = _add(r, p)
        p, k = _add(p, p), k >> 1
    return r


def sign(d: int, msg: bytes, k: int | None = None) -> bytes:
    """ES256: r || s, 64 bytes, as JWS wants it. `k` only for the RFC 6979 test vectors."""
    z = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    while True:
        kk = k or secrets.randbelow(N - 1) + 1
        r = _mul(kk, G)[0] % N
        s = pow(kk, -1, N) * (z + r * d) % N
        if r and s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify(q, msg: bytes, sig: bytes) -> bool:
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if len(sig) != 64 or not (0 < r < N and 0 < s < N):
        return False
    z = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    w = pow(s, -1, N)
    pt = _add(_mul(z * w % N, G), _mul(r * w % N, q))
    return pt is not None and pt[0] % N == r


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def key_path(token_path: Path) -> Path:
    return token_path.with_name(token_path.name + "-push")  # beside the token: the same secrecy


def _key_in(path: Path) -> int | None:
    """The key a file holds, or None: missing, empty, or not a key."""
    try:
        d = int(path.read_text().strip(), 16)
    except (OSError, ValueError):
        return None
    return d if 0 < d < N else None


_making = threading.Lock()


def private_key(token_path: Path) -> int:
    """This machine's VAPID key, made on first use (mode 600, like the token). Written whole to a
    temp file beside it, fsynced, then linked into place: created first and written after, a
    full disk or a crash left it empty, and every chat request failed on it, not only push (#924).
    An empty or unparsable one is replaced the same way; two handler threads on first use make
    one (the lock), and another process's linked first is the key. Where hard links aren't, it is
    renamed into place instead."""
    path = key_path(token_path)
    if (d := _key_in(path)) is not None:
        return d
    with _making:
        if (d := _key_in(path)) is not None:
            return d
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # 600
        try:
            with os.fdopen(fd, "w") as f:
                f.write(f"{secrets.randbelow(N - 1) + 1:064x}\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except OSError:  # FileExistsError: a broken one (read above), or another process's
                # just made. Any other: a file system without hard links (exFAT, some FUSE or SMB
                # mounts: EPERM, ENOTSUP), where the key was never made and push was off for good
                # (the review of #924); a rename is atomic too, only a lost race's first writer loses
                if _key_in(path) is None:
                    os.replace(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    if (d := _key_in(path)) is None:
        raise RuntimeError(f"{path} holds no usable key: delete it and the chat makes a new one")
    return d


def public_key(d: int) -> str:
    """The applicationServerKey: the uncompressed point, base64url."""
    x, y = _mul(d, G)
    return b64u(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big"))


def accepted(endpoint) -> bool:
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        return False
    u = urlsplit(endpoint)
    return u.scheme == "https" and bool(u.hostname) and u.port in (None, 443) and \
        ("." + u.hostname).endswith(PUSH_HOSTS)


def vapid(d: int, endpoint: str, now: float | None = None) -> str:
    u = urlsplit(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "sub": SUBJECT,
                              "exp": int((now or time.time()) + 12 * 3600)},
                             separators=(",", ":")).encode())
    jwt = f"{head}.{claims}"
    return f"vapid t={jwt}.{b64u(sign(d, jwt.encode()))}, k={public_key(d)}"


_ctx: list = []


def tls() -> ssl.SSLContext:
    """The context pushes go out with: the default one, and macOS's bundle when its store is
    empty (python.org's Python before Install Certificates.command), as Apple's Python has it."""
    if not _ctx:
        ctx = ssl.create_default_context()
        if not has_cas(ctx) and os.path.isfile(MAC_CAS):
            try:
                ctx.load_verify_locations(MAC_CAS)
            except (OSError, ssl.SSLError):
                pass
        _ctx.append(ctx)
    return _ctx[0]


def has_cas(ctx: ssl.SSLContext | None = None) -> bool:
    """Whether this Python can check a push service's certificate: CA certificates loaded, or a
    CA directory with files in it (OpenSSL reads one only as it needs them, so they don't count yet)."""
    ctx = ctx or tls()
    if ctx.cert_store_stats().get("x509_ca"):
        return True
    capath = ssl.get_default_verify_paths().capath
    try:
        return bool(capath) and any(os.scandir(capath))
    except OSError:
        return False


def send(d: int, endpoint: str, urgency: str = "high") -> tuple[int, str]:
    """One push with no payload: the push service's status (0: not reached; CERT: its certificate
    couldn't be checked here, for good) and why it failed ("" when taken). The reason was dropped,
    so an empty CA store read as the push service's refusal on the page and left serve.log empty
    (#539, the 2026-11-20 audit)."""
    req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
        "TTL": str(TTL), "Urgency": urgency, "Authorization": vapid(d, endpoint),
        "Content-Length": "0"})
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT, context=tls()) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code} {e.reason}".strip()
    except (urllib.error.URLError, OSError) as e:
        why = e.reason if isinstance(e, urllib.error.URLError) else e
        if isinstance(why, ssl.SSLCertVerificationError):
            return CERT, f"certificate not verified: {why.verify_message or why}"
        return 0, str(why) or type(why).__name__


# ---- subscriptions: chat/push.json {"subs": [{endpoint, ts, ua}]} ----

def _subs_path() -> Path:
    return store.home() / "chat" / "push.json"


def subscriptions() -> list[dict]:
    return (store.read_json(_subs_path(), None) or {}).get("subs") or []


def subscribe(endpoint: str, ua: str = "") -> None:
    with store.locked(_subs_path().with_suffix(".lock")):
        subs = [s for s in subscriptions() if s.get("endpoint") != endpoint]
        subs.append({"endpoint": endpoint, "ts": store.now(), "ua": ua[:200]})
        store.write_json(_subs_path(), {"subs": subs[-MAX_SUBS:]})


def unsubscribe(endpoint: str) -> None:
    with store.locked(_subs_path().with_suffix(".lock")):
        store.write_json(_subs_path(),
                         {"subs": [s for s in subscriptions() if s.get("endpoint") != endpoint]})


def push_all(d: int, endpoints: list[str] | None = None) -> dict:
    """Push every subscription (or these of them); one the service says is gone (404, 410) is
    dropped. Each endpoint's (status, reason), as `send`."""
    out = {}
    for s in subscriptions():
        if endpoints is not None and s["endpoint"] not in endpoints:
            continue
        out[s["endpoint"]] = got = send(d, s["endpoint"])
        if got[0] in (404, 410):
            unsubscribe(s["endpoint"])
    return out
