"""
accounts.py — user accounts and login sessions for the multi-user web app.

A tiny SQLite-backed store (config.APP_DB) holding ONLY:
  * users    — id, username, password hash + salt, created timestamp
  * sessions — opaque login token -> user, with an expiry

No personal data lives here. A user's CV-derived profile, saved searches, tracker
and BYO API keys live in their own folder (see usercontext.py), never in this DB.

Passwords are never stored in the clear and never logged: we keep only a
PBKDF2-HMAC-SHA256 hash with a per-user random salt (Python standard library —
no third-party crypto dependency). Login tokens are cryptographically random.

The single-user CLI/GUI never import this module.
"""

import os
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import config

# PBKDF2 work factor. High enough to be costly to brute-force, fast enough for
# an interactive login on a laptop. Stored per-hash so it can be raised later.
_PBKDF2_ITERATIONS = 240_000
_SESSION_DAYS = 30           # how long a login stays valid
_MIN_USERNAME = 3
_MIN_PASSWORD = 8


class AuthError(ValueError):
    """Raised for bad registration/login input (safe to show the user)."""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    """Open the accounts DB, creating its parent folder on first use."""
    os.makedirs(os.path.dirname(config.APP_DB) or ".", exist_ok=True)
    conn = sqlite3.connect(config.APP_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT UNIQUE NOT NULL,
                pw_hash    TEXT NOT NULL,
                pw_salt    TEXT NOT NULL,
                iterations INTEGER NOT NULL,
                created    TEXT NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token   TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created TEXT NOT NULL,
                expires TEXT NOT NULL
            )""")


def _hash_password(password, salt, iterations=_PBKDF2_ITERATIONS):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), iterations)
    return dk.hex()


def _normalize_username(username):
    return (username or "").strip().lower()


def create_user(username, password):
    """
    Register a new user. Returns a public user dict {id, username}.
    Raises AuthError on invalid input or a taken username. Never logs the
    password.
    """
    uname = _normalize_username(username)
    if len(uname) < _MIN_USERNAME:
        raise AuthError(f"Username must be at least {_MIN_USERNAME} characters.")
    if not all(c.isalnum() or c in "-_." for c in uname):
        raise AuthError("Username may use letters, numbers, and - _ . only.")
    if len(password or "") < _MIN_PASSWORD:
        raise AuthError(f"Password must be at least {_MIN_PASSWORD} characters.")

    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, pw_hash, pw_salt, iterations, created)"
                " VALUES (?, ?, ?, ?, ?)",
                (uname, pw_hash, salt, _PBKDF2_ITERATIONS, _iso(_now())))
            return {"id": cur.lastrowid, "username": uname}
    except sqlite3.IntegrityError:
        raise AuthError("That username is already taken.")


def verify_login(username, password):
    """Return the public user dict if the credentials are valid, else None.
    Uses a constant-time hash comparison. Never logs the password."""
    uname = _normalize_username(username)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, pw_hash, pw_salt, iterations FROM users"
            " WHERE username = ?", (uname,)).fetchone()
    if not row:
        return None
    candidate = _hash_password(password or "", row["pw_salt"], row["iterations"])
    if not hmac.compare_digest(candidate, row["pw_hash"]):
        return None
    return {"id": row["id"], "username": row["username"]}


def create_session(user_id):
    """Create a login session and return its opaque token."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created, expires)"
            " VALUES (?, ?, ?, ?)",
            (token, user_id, _iso(now), _iso(now + timedelta(days=_SESSION_DAYS))))
    return token


def get_session_user(token):
    """Return the public user dict for a valid, unexpired session, else None.
    Expired sessions are deleted lazily."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT s.token, s.expires, u.id, u.username FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,)).fetchone()
        if not row:
            return None
        if _iso(_now()) > row["expires"]:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    return {"id": row["id"], "username": row["username"]}


def delete_session(token):
    """Log out: drop a session token. No-op if it doesn't exist."""
    if not token:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
