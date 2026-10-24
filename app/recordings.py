"""Recorded demonstrations of scenarios that cannot run live in a public deployment (D and E).

The JSON fixtures in app/ui/recordings/ are produced by scripts/record_scenarios.py from real runs against a
local kind cluster. They are always labelled RECORDED, carry the date, commit and environment they came from,
and are validated on load (shape, label, and a scan for internal addresses) so a bad fixture cannot be served.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

RECORDINGS_DIR = Path(__file__).parent / "ui" / "recordings"
REQUIRED = ("id", "title", "source", "recorded_at", "git_commit", "environment", "summary", "timeline", "measurements")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TOKEN_HINTS = re.compile(r"(?i)(secret|password|token|api[_-]?key|authorization)\s*[:=]")


class RecordingError(ValueError):
    pass


def validate(rec: dict, name: str = "recording") -> dict:
    missing = [k for k in REQUIRED if k not in rec]
    if missing:
        raise RecordingError(f"{name}: missing keys {missing}")
    if rec["source"] != "recorded":
        raise RecordingError(f"{name}: source must be 'recorded'")
    if rec["id"] not in ("D", "E"):
        raise RecordingError(f"{name}: id must be D or E")
    if not isinstance(rec["timeline"], list) or not rec["timeline"]:
        raise RecordingError(f"{name}: empty timeline")
    text = json.dumps(rec)
    if IPV4.search(text):
        raise RecordingError(f"{name}: contains an IP address")
    if TOKEN_HINTS.search(text):
        raise RecordingError(f"{name}: looks like it contains a credential")
    return rec


@lru_cache(maxsize=1)
def load_all() -> dict:
    out = {}
    if RECORDINGS_DIR.is_dir():
        for path in sorted(RECORDINGS_DIR.glob("scenario_*.json")):
            rec = validate(json.loads(path.read_text(encoding="utf-8")), path.name)
            out[rec["id"]] = {**rec, "label": "RECORDED"}
    return out


def get(scenario_id: str):
    return load_all().get(scenario_id)
