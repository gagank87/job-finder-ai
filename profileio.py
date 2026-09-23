"""
profileio.py — build and persist a per-user CV profile.

The pipeline judges jobs against a set of CV-derived facts — target roles,
skills, years of full-time experience, and education — that used to be hardcoded
for one person in cvprofile.py. This module makes those facts per-user data:

  * build_from_cv(cv_text) — ask the LLM (llm.complete, which uses whatever FREE
    provider is configured: Groq/Gemini/…) to extract the facts from the user's
    ACTUAL CV text. Honesty-first: the model is told to report only what the CV
    supports and never invent qualifications; any field it can't determine keeps
    the built-in default, and if no LLM is available the whole build degrades to
    the defaults with a clear note (never a crash, never fabricated facts).
  * save() / load() — persist to config.PROFILE_FILE (profile.json), normalized
    through cvprofile so the file is always well-typed. This file holds ONLY
    CV-derived facts — never a credential — but it's personal, so it's gitignored.
  * build_and_save() — read the master CV (via cvdoc), extract, save, and make it
    the active profile in one call.

cvprofile.apply_profile() consumes the same dict shape and rebinds the active
profile for the whole pipeline; cvprofile.normalize_profile() is the shared
validator, so this module never has to re-implement type/shape checks.
"""

import json
import os
import re

import config
import cvprofile
import llm

_SYSTEM_PROMPT = (
    "You extract structured facts from a CV. Report ONLY what the CV supports; "
    "never invent experience, skills, or degrees. If something is not stated, "
    "omit that key entirely. Be conservative about years of full-time "
    "experience: internships, apprenticeships, and student projects are NOT "
    "full-time years."
)


def _build_prompt(cv_text):
    return (
        "From the CV below, extract the candidate's job-search profile.\n\n"
        f"=== CV ===\n{cv_text.strip()}\n\n"
        "Respond with ONLY a JSON object using these keys:\n"
        '  "roles": array of 3-8 job TITLES this candidate should search for, '
        "best-fit first (their real target roles, not skills).\n"
        '  "skills": array of concrete skills/tools/domain keywords from the CV '
        "(lowercase, single words or short phrases).\n"
        '  "years_experience": integer full-time professional years (0 for a '
        "fresher; do NOT count internships).\n"
        '  "has_internship": boolean.\n'
        '  "has_bachelors": boolean.\n'
        '  "has_masters_equiv": boolean (a Master\'s degree or an equivalent '
        "postgraduate diploma).\n"
        '  "has_phd": boolean.\n'
        "No markdown, no commentary. Omit any key you cannot determine from the "
        "CV rather than guessing."
    )


def _parse(text):
    """Best-effort JSON extraction from the model reply (tolerates ``` fences)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                return None
    return None


def build_from_cv(cv_text):
    """
    Extract a profile dict from CV text using the configured (free) LLM.

    Returns (profile_dict, note). profile_dict is ALWAYS complete — LLM-extracted
    fields merged over the built-in defaults, then normalized — so no field is
    ever missing. `note` explains what happened (which provider, or why it fell
    back to defaults). Never raises; never fabricates: undetermined fields keep
    the default and the model is instructed to omit what the CV doesn't support.
    """
    base = cvprofile.defaults()
    if not cv_text or not str(cv_text).strip():
        return base, "No CV text to read — kept the built-in default profile."

    result = llm.complete(_SYSTEM_PROMPT, _build_prompt(str(cv_text)),
                          max_tokens=800)
    if not result["ok"]:
        return base, ("No LLM provider available — kept the default profile. "
                      f"Set a free Groq/Gemini key, then re-run. ({result['error']})")

    data = _parse(result["text"])
    if not isinstance(data, dict):
        return base, "Couldn't parse the model's reply — kept the default profile."

    merged = dict(base)
    used = []
    for key in cvprofile.PROFILE_FIELDS:
        if key in data and data[key] not in (None, "", [], {}):
            merged[key] = data[key]
            used.append(key)
    merged = cvprofile.normalize_profile(merged)
    note = (f"Built from your CV via {result['provider']} "
            f"(read: {', '.join(used) if used else 'nothing — used defaults'}).")
    return merged, note


def save(profile, path=None):
    """
    Write the profile to profile.json, normalized so the file is always
    well-typed. CV facts only — never a secret. Returns the path. Raises OSError
    if the file can't be written (caller surfaces it).
    """
    path = path or config.PROFILE_FILE
    clean = cvprofile.normalize_profile(profile)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return path


def load(path=None):
    """
    Read profile.json and return a complete, normalized profile dict. Raises
    OSError / json.JSONDecodeError to the caller (a missing/corrupt file).
    """
    path = path or config.PROFILE_FILE
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return cvprofile.normalize_profile(raw)


def load_or_defaults(path=None):
    """Like load(), but returns the built-in defaults on any read/parse error."""
    try:
        return load(path)
    except (OSError, ValueError):
        return cvprofile.defaults()


def build_and_save(cv_path=None, path=None):
    """
    Read the master CV, extract a profile, save it, and make it active.

    Returns {ok, error, profile, note, path}. On a CV-read problem, ok is False
    with a human-readable error and the built-in defaults left active — never a
    crash. Importing cvdoc lazily keeps python-docx optional for plain searches.
    """
    import cvdoc
    cv = cvdoc.load_master(cv_path)
    if not cv.get("ok"):
        return {"ok": False, "error": cv.get("error", "couldn't read the CV"),
                "profile": cvprofile.defaults(), "note": "", "path": ""}

    profile, note = build_from_cv(cv.get("full_text", ""))
    saved = save(profile, path)
    cvprofile.apply_profile(profile)   # active immediately, this process
    return {"ok": True, "error": "", "profile": profile, "note": note,
            "path": saved}
