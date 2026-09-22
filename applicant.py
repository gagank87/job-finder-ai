"""
applicant.py — load your (gitignored) applicant profile for auto-submission.

Auto-submit needs real personal details (name, email, phone, links, work-
authorization answers). Those live in `secrets/applicant_profile.json`, which is
gitignored and never committed. A committed `applicant_profile.sample.json`
documents the shape. `load()` reads the real file if present and validates that
the fields auto-submit actually depends on are filled in.

Design rules that keep this honest:
  * If the profile is missing or the essentials are blank, auto-submit is
    DISABLED (everything falls back to prepare-to-apply) — the tool never
    submits with placeholder or guessed data.
  * `screening_answer(question)` only returns an answer when the question text
    clearly matches a default you supplied; otherwise it returns None, which the
    submitter treats as "unanswerable -> abort this auto-submit -> prepare".
  * Nothing here is ever fabricated: a field you left blank stays blank.
"""

import json
import os

import config

# The fields a Greenhouse-style application genuinely can't proceed without.
_REQUIRED_FIELDS = ("first_name", "last_name", "email")


def load(path=None):
    """
    Load the applicant profile.

    Returns a dict:
      {
        "ok": bool,
        "error": str,           # populated when ok is False
        "profile": dict,        # the raw profile (empty when not ok)
      }
    Never raises. A missing file / unreadable JSON / missing essentials all
    return ok=False with a one-line reason, so the caller cleanly disables
    auto-submit and prepares instead.
    """
    path = path or config.APPLICANT_PROFILE_PATH
    if not os.path.exists(path):
        return {"ok": False, "profile": {},
                "error": (f"No applicant profile at '{path}'. Copy "
                          f"applicant_profile.sample.json there and fill it in "
                          f"to enable auto-submit (see cv/README.txt).")}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "profile": {},
                "error": f"Couldn't read '{path}' as JSON: {e}"}

    if not isinstance(data, dict):
        return {"ok": False, "profile": {},
                "error": f"'{path}' isn't a JSON object."}

    missing = [k for k in _REQUIRED_FIELDS
               if not str(data.get(k, "")).strip()]
    if missing:
        return {"ok": False, "profile": data,
                "error": (f"Applicant profile is missing required field(s): "
                          f"{', '.join(missing)}. Auto-submit stays off until "
                          f"these are filled; jobs are prepared instead.")}
    return {"ok": True, "error": "", "profile": data}


def full_name(profile) -> str:
    first = str(profile.get("first_name", "")).strip()
    last = str(profile.get("last_name", "")).strip()
    return (first + " " + last).strip()


def screening_answer(profile, question_text):
    """
    Return a default answer for a screening question, or None if we don't have
    one that clearly matches. Matching is case-insensitive substring against the
    keys in `screening_defaults`. None => the submitter must NOT guess; it aborts
    the auto-submit for that job and prepares it instead.
    """
    defaults = profile.get("screening_defaults") or {}
    if not isinstance(defaults, dict):
        return None
    q = (question_text or "").strip().lower()
    if not q:
        return None
    for key, val in defaults.items():
        ans = str(val).strip()
        if ans and str(key).strip().lower() in q:
            return ans
    return None
