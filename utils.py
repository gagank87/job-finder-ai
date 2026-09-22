"""
Shared helpers: HTTP session, narration, link validation, dedupe, timing.
"""

import sys
import time

import requests

import config

# ---------------------------------------------------------------------------
# A single shared session with a browser-like User-Agent.
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


def get_session() -> requests.Session:
    return _session


def polite_sleep():
    """Small delay between calls to stay under rate limits."""
    time.sleep(config.REQUEST_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Conversational narration — the tool "tells you what it's doing".
#
# Narration goes through a single sink so alternate front-ends (the Tkinter
# GUI) can capture the same lines the CLI prints. The default sink is print();
# set_output_sink(fn) swaps it for any callable taking one string, and
# reset_output_sink() restores print. Backward-compatible: unchanged for the CLI.
# ---------------------------------------------------------------------------
def _default_sink(message: str):
    print(message, flush=True)


_output_sink = _default_sink


def set_output_sink(sink):
    """Route all narration through `sink` (a callable taking one string)."""
    global _output_sink
    _output_sink = sink or _default_sink


def reset_output_sink():
    """Restore the default (stdout) narration sink."""
    global _output_sink
    _output_sink = _default_sink


def say(message: str):
    """Emit a narration line through the current sink."""
    _output_sink(message)


def step(message: str):
    say(f"\n>> {message}")


def ok(message: str):
    say(f"   [ok] {message}")


def warn(message: str):
    say(f"   [!] {message}")


def info(message: str):
    say(f"   - {message}")


# ---------------------------------------------------------------------------
# Link validation — proves authenticity by checking the URL is live (200).
# ---------------------------------------------------------------------------
def validate_link(url: str) -> bool:
    """
    Return True if the URL responds with a success status.

    Tries HEAD first (cheap); falls back to a light GET if HEAD is
    rejected (some servers/LinkedIn dislike HEAD). Never raises.
    """
    if not url:
        return False
    try:
        r = _session.head(url, timeout=config.HTTP_TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            return True
        # Some endpoints reject HEAD (405) — retry with GET.
        r = _session.get(url, timeout=config.HTTP_TIMEOUT, allow_redirects=True,
                         stream=True)
        return r.status_code < 400
    except requests.RequestException:
        return False


def validate_links(jobs: list[dict], link_key: str = "job_link",
                    label: str = "links") -> list[dict]:
    """
    Validate every job's link; keep only live ones. Narrates a progress
    counter so you can see it working. Returns the filtered list.
    """
    if not jobs:
        return []
    say(f"   Validating {len(jobs)} {label} (checking each returns 200)...")
    kept = []
    for i, job in enumerate(jobs, 1):
        alive = validate_link(job.get(link_key, ""))
        job["_validated"] = alive
        if alive:
            kept.append(job)
        # lightweight inline progress
        sys.stdout.write(f"\r   checked {i}/{len(jobs)}  kept {len(kept)}   ")
        sys.stdout.flush()
        polite_sleep()
    sys.stdout.write("\n")
    dropped = len(jobs) - len(kept)
    if dropped:
        warn(f"Dropped {dropped} dead/unreachable link(s).")
    ok(f"{len(kept)} live link(s) confirmed.")
    return kept


# ---------------------------------------------------------------------------
# Deduplication.
# ---------------------------------------------------------------------------
def dedupe(jobs: list[dict]) -> list[dict]:
    """
    Remove duplicate jobs. A job is identified by its job_id when present,
    otherwise by a normalized (company|title) pair.
    """
    seen = set()
    out = []
    for job in jobs:
        jid = str(job.get("job_id", "")).strip()
        if jid:
            key = ("id", jid)
        else:
            key = (
                "ct",
                job.get("company", "").strip().lower(),
                job.get("title", "").strip().lower(),
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out
