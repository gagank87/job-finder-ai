"""
usercontext.py — make the single-tenant pipeline serve one user at a time.

The whole search/tracker/profile pipeline reads its paths and secrets from
module-level `config.*` attributes at call time. That is single-tenant by
design. Rather than thread a user object through every module, we *redirect*
those attributes to the current user's private folder for the duration of a
request, then restore them — exactly the rebinding trick cvprofile already uses.

This is only safe because the web app runs one such context at a time under a
global lock (searches share a process-global narration sink too). It is NOT
thread-parallel across users; that is a later refactor. For "me + a few friends"
it is correct and simple.

Layout (all under config.DATA_DIR, which is gitignored):

    data/users/<id>/
        profile.json            # CV-derived facts only (roles/skills/years) — no PII
        settings.json           # saved search preferences
        master_tracker.xlsx     # this user's jobs
        secrets/keys.json       # this user's BYO API keys (never returned/logged)
        output/                 # per-run workbooks + cache

Privacy: the uploaded CV is NEVER persisted here — webapp extracts the profile
from it in memory and deletes it immediately. Only non-identifying, CV-derived
professional facts (roles/skills/years/education) are kept in profile.json.
"""

import json
import os
from contextlib import contextmanager

import config
import cvprofile
import profileio

# The config path attributes we redirect per user, captured once at import from
# their canonical single-tenant values so a redirect is always computed from the
# original relative path (never a previously-redirected one).
_REDIRECT_ATTRS = [
    "MASTER_TRACKER", "SETTINGS_FILE", "PROFILE_FILE", "OUTPUT_DIR",
    "APPLICATIONS_DIR", "CACHE_DIR", "CACHE_FILE", "HEADLESS_PROFILE_DIR",
    "COOKIES_FILE", "MASTER_CV_PATH", "COVER_REFERENCE_PATH",
    "APPLICANT_PROFILE_PATH",
]
_ORIGINALS = {a: getattr(config, a) for a in _REDIRECT_ATTRS}

# Which providers a user may bring keys for. Order matters only for display.
KEY_PROVIDERS = ["groq", "gemini", "jsearch", "anthropic", "bedrock"]


def user_dir(user_id):
    """Absolute-safe folder for one user's data (relative to the project)."""
    return os.path.join(config.DATA_DIR, "users", str(int(user_id)))


def ensure_user_dirs(user_id):
    """Create the per-user folder tree. Idempotent."""
    ud = user_dir(user_id)
    for sub in ("", "secrets", "cv", "output",
                os.path.join("output", ".cache"),
                os.path.join("output", "applications")):
        os.makedirs(os.path.join(ud, sub), exist_ok=True)
    return ud


# ---------------------------------------------------------------------------
# Per-user BYO API keys. Stored in the user's own gitignored secrets folder.
# Values are NEVER returned to the browser or logged — only presence booleans.
# ---------------------------------------------------------------------------
def _keys_path(user_id):
    return os.path.join(user_dir(user_id), "secrets", "keys.json")


def load_keys(user_id):
    """Return the user's key dict {provider: value}, or {} if none saved.
    Best-effort: a missing/corrupt file yields {} (never raises, never logs)."""
    try:
        with open(_keys_path(user_id), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in KEY_PROVIDERS and v}


def save_keys(user_id, updates):
    """
    Merge `updates` into the user's stored keys and persist.

    A provider mapped to a blank/None value is CLEARED (removed). Unknown
    providers are ignored. Returns the presence-only status dict. Never logs
    key values.
    """
    ensure_user_dirs(user_id)
    keys = load_keys(user_id)
    for provider in KEY_PROVIDERS:
        if provider not in (updates or {}):
            continue
        value = (updates.get(provider) or "").strip()
        if value:
            keys[provider] = value
        else:
            keys.pop(provider, None)
    path = _keys_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keys, f)
    try:                       # keep the secrets file private on POSIX hosts
        os.chmod(path, 0o600)
    except OSError:
        pass
    return keys_status(user_id, keys)


def keys_status(user_id, keys=None):
    """Presence-only map {provider: bool} — safe to send to the browser."""
    keys = load_keys(user_id) if keys is None else keys
    return {p: bool(keys.get(p)) for p in KEY_PROVIDERS}


# ---------------------------------------------------------------------------
# The context switch.
# ---------------------------------------------------------------------------
@contextmanager
def active_user(user_id):
    """
    Redirect config paths + secrets + the active CV profile to `user_id` for the
    body of the `with`, then restore everything. Callers MUST hold the global
    search lock around this (the redirect mutates process-global config).
    """
    ensure_user_dirs(user_id)
    ud = user_dir(user_id)

    saved_paths = {a: getattr(config, a) for a in _REDIRECT_ATTRS}
    saved_override = config.SECRET_OVERRIDE
    saved_profile = cvprofile.current_profile()
    try:
        for attr, original in _ORIGINALS.items():
            setattr(config, attr, os.path.join(ud, original))
        config.SECRET_OVERRIDE = load_keys(user_id)
        # Load this user's saved profile (or defaults) and make it active so the
        # eligibility gates judge against THEIR CV facts.
        cvprofile.apply_profile(profileio.load_or_defaults())
        yield ud
    finally:
        for attr, value in saved_paths.items():
            setattr(config, attr, value)
        config.SECRET_OVERRIDE = saved_override
        cvprofile.apply_profile(saved_profile)
