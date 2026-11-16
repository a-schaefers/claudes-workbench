"""The local models workbench can hand work to, and whether each can run right now.

Only one model fits on the GPU at a time (a 16 GB RX 9070 XT), so availability is two questions:
is the model's server answering, and is another engine already holding the card. Overridable with
``~/.config/workbench/models.json`` (same shape as DEFAULT_MODELS, merged by key).
"""
import json
import os
import urllib.request
from pathlib import Path

DEFAULT_MODELS = {
    "strata": {
        "label": "Qwen3.8-Flash-Next Coder (Strata)",
        "opencode": "strata/strata",
        "engine": "strata",
        "probe": "http://127.0.0.1:8080/v1/models",
        "tier": "significant",
    },
    "qwen27b": {
        "label": "Qwen3.8 27B UD-Q3_K_XL (Ollama)",
        "opencode": "ollama/qwen3.8-27b",
        "engine": "ollama",
        "ollama_name": "qwen3.8-27b",
        "probe": "http://127.0.0.1:11434/api/tags",
        "tier": "significant",
    },
    "mimo": {
        "label": "MiMo-V2.6 9B Q8 (Ollama)",
        "opencode": "ollama/mimo-v2.6:9b-q8_0",
        "engine": "ollama",
        "ollama_name": "mimo-v2.6:9b-q8_0",
        "probe": "http://127.0.0.1:11434/api/tags",
        "tier": "trivial",
    },
}
OLLAMA_PS = "http://127.0.0.1:11434/api/ps"


def registry() -> dict:
    models = {k: dict(v) for k, v in DEFAULT_MODELS.items()}
    override = Path(os.environ.get("WB_MODELS") or Path.home() / ".config/workbench/models.json")
    if override.exists():
        for key, fields in json.loads(override.read_text()).items():
            models.setdefault(key, {}).update(fields)
    return models


def resolve(key_or_model: str) -> tuple[str, dict]:
    """Accept a registry key (``mimo``) or an OpenCode model id (``ollama/mimo-v2.6:9b-q8_0``)."""
    models = registry()
    if key_or_model in models:
        return key_or_model, models[key_or_model]
    for key, m in models.items():
        if m["opencode"] == key_or_model:
            return key, m
    raise KeyError(f"unknown model {key_or_model!r}; known: {', '.join(models)}")


def _get(url: str, timeout: float = 2.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def check(key: str, m: dict) -> dict:
    """``{ready, why}`` for one model, without loading anything onto the GPU."""
    if os.environ.get("WB_FAKE_MODELS"):  # tests drive a fake opencode; no servers involved
        return {"ready": True, "why": "fake"}
    body = _get(m["probe"])
    if body is None:
        return {"ready": False, "why": f"{m['engine']} server is not answering"}
    if m["engine"] == "ollama":
        names = {x.get("name") for x in body.get("models", [])}
        if m["ollama_name"] not in names and f"{m['ollama_name']}:latest" not in names:
            return {"ready": False, "why": f"{m['ollama_name']} is not pulled in Ollama"}
        if _get(registry()["strata"]["probe"], timeout=1.5) is not None:
            return {"ready": False,
                    "why": "Strata is running and holds the GPU (strata launch --stop first)"}
    return {"ready": True, "why": "up"}


def on_gpu() -> list[str]:
    """What is holding the card now, by registry key."""
    held = []
    if _get(registry()["strata"]["probe"], timeout=1.5) is not None:
        held.append("strata")
    loaded = {x.get("name") for x in (_get(OLLAMA_PS, timeout=1.5) or {}).get("models", [])}
    for key, m in registry().items():
        if m["engine"] == "ollama" and ({m["ollama_name"], f"{m['ollama_name']}:latest"} & loaded):
            held.append(key)
    return held
