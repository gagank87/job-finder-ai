"""
Run cache — persist an in-progress fetch so a mis-click never costs a re-fetch.

The core pipeline (fetch -> validate -> filter -> JD-check) can take minutes.
Before the QC gate, we save the fully-processed results here; if the user then
mis-types the Y/n prompt, exits, or the terminal dies, the next launch can
"resume from your last fetch" and jump straight back to the QC gate — no
network calls repeated.

Lifecycle (enforced by the caller, jobfinder / gui):
  * save()  — right after core.run_search(), before the QC gate.
  * clear() — the moment the run is resolved EITHER way:
                - QC rejected            (nothing written), OR
                - QC approved + written  (merged into the master tracker).
So the cache never lingers once a run is done.

These functions are pure (no console I/O) so the CLI, GUI, and scheduler all
reuse them. They never raise: a missing/corrupt cache simply reads back as
None, and clearing a nonexistent cache is a no-op.
"""

import json
import os

import config

# Bump if the cached payload shape ever changes, so a stale cache from an
# older version is ignored rather than mis-read.
_SCHEMA_VERSION = 1


def _serializable_settings(settings):
    """
    Make a settings dict JSON-safe: `sources` is a set, `workday_urls` holds
    tuples — both need normalizing. Everything else is already JSON-native.
    """
    s = dict(settings or {})
    if isinstance(s.get("sources"), (set, frozenset)):
        s["sources"] = sorted(s["sources"])
    if s.get("workday_urls"):
        s["workday_urls"] = [list(pair) for pair in s["workday_urls"]]
    return s


def restore_settings(settings):
    """Reverse _serializable_settings: JSON lists -> the shapes core expects."""
    s = dict(settings or {})
    if "sources" in s and not isinstance(s["sources"], (set, frozenset)):
        s["sources"] = set(s["sources"] or [])
    if s.get("workday_urls"):
        s["workday_urls"] = [tuple(pair) for pair in s["workday_urls"]]
    return s


def save(results, settings, fetched_at, path=None):
    """
    Write the processed results + the settings that produced them to the cache.

    `results` is the dict returned by core.run_search (its job lists are the
    only parts we need to resume the QC gate). Returns the path written, or
    "" if writing failed (caching is best-effort — a failure never blocks a run).
    """
    path = path or config.CACHE_FILE
    payload = {
        "schema": _SCHEMA_VERSION,
        "fetched_at": fetched_at,
        "settings": _serializable_settings(settings),
        "linkedin": results.get("linkedin", []),
        "career": results.get("career", []),
        "other": results.get("other", []),
        "dropped_salary": results.get("dropped_salary", 0),
        "dropped_senior": results.get("dropped_senior", 0),
        "dropped_ineligible": results.get("dropped_ineligible", 0),
        "total": results.get("total", 0),
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except OSError:
        return ""


def load(path=None):
    """
    Return the cached payload dict, or None if there is none / it's unreadable
    / it's from an incompatible schema. Never raises.
    """
    path = path or config.CACHE_FILE
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA_VERSION:
        return None
    return payload


def clear(path=None):
    """Delete the cache file if present. Idempotent; never raises."""
    path = path or config.CACHE_FILE
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
