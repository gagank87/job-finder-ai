"""
settingsio.py — persist the search settings so a headless / scheduled run
(the "conductor") reproduces exactly the roles, levels, locations, sources and
companies you selected — NOT a hardcoded default (e.g. never a forced India).

On-disk format is the plain settings dict in its JSON-safe form (the same shape
runcache stores under its "settings" key): `sources` as a sorted list,
`workday_urls` as [name, url] pairs, everything else JSON-native. This is what
`python jobfinder.py --headless` reads back through load().

No secrets ever live here — only search preferences. API keys stay in env vars /
the gitignored secrets file, exactly as before.
"""

import json
import os

import config
import runcache


def save(settings, path=None):
    """
    Write the current settings to settings.json (JSON-safe). Returns the path.

    Raises OSError if the file can't be written (caller surfaces it); never
    writes secrets — settings hold only search preferences.
    """
    path = path or config.SETTINGS_FILE
    data = runcache._serializable_settings(settings)  # noqa: SLF001 (shared norm)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load(path=None):
    """
    Read settings.json and return a settings dict in the shape core expects
    (sources -> set, workday_urls -> tuples), with the country/countries
    fallback applied so old/hand-written files still work.

    Raises OSError / json.JSONDecodeError to the caller so a headless run can
    report a missing or corrupt file clearly.
    """
    path = path or config.SETTINGS_FILE
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    settings = runcache.restore_settings(raw)
    if not settings.get("countries") and settings.get("country"):
        settings["countries"] = [settings["country"]]
    if settings.get("countries") and not settings.get("country"):
        settings["country"] = settings["countries"][0]
    return settings
