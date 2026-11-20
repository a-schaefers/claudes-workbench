"""The pane's Stats tab: its settings (stats.json, `wb stats set`), read by the runner (what a
task records for it) and by `wb top` and the mod (the live view). The user's rule (2026-11-20):
a measurement that costs nothing is on by default, and every one can be turned off."""
from . import store

DEFAULTS = {"recording": True, "live": True, "every": 2}
EVERY = (1, 2, 5)  # seconds between the live view's samples (wb top)
SWITCHES = ("recording", "live")


def path():
    return store.home() / "stats.json"


def settings() -> dict:
    """The settings, each its default unless stats.json says otherwise (a value of the wrong
    type, hand-edited, is ignored rather than trusted)."""
    data = store.read_json(path(), {})
    if not isinstance(data, dict):  # the runner reads this every round: never a reason to fail
        data = {}
    out = dict(DEFAULTS)
    for key in SWITCHES:
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if data.get("every") in EVERY:
        out["every"] = data["every"]
    return out


def recording() -> bool:
    """Whether a task records what only the Stats tab reads (its row's speeds, watts, guards and
    timings; the round_end event). What the router, tuning and savings read is always kept."""
    return settings()["recording"]


def set_setting(key: str, value: str) -> dict:
    """`wb stats set KEY VALUE`. Raises ValueError with the allowed values."""
    data = store.read_json(path(), {})
    data = data if isinstance(data, dict) else {}
    if key in SWITCHES:
        if value not in ("on", "off"):
            raise ValueError(f"{key} is on or off")
        data[key] = value == "on"
    elif key == "every":
        if value not in {str(s) for s in EVERY}:
            raise ValueError(f"every is one of {', '.join(map(str, EVERY))} (seconds)")
        data["every"] = int(value)
    else:
        raise ValueError(f"no setting {key!r}: recording, live or every")
    data["ts"] = store.now()
    store.write_json(path(), data)
    return settings()


def line() -> str:
    s = settings()
    return (f"stats: recording {'on' if s['recording'] else 'off'} · live view "
            f"{'on' if s['live'] else 'off'} · every {s['every']} s")
