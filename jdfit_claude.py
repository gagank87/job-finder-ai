"""
jdfit_claude.py — STEP 1: Claude-powered JD eligibility analyzer (config-gated).

This is a drop-in replacement for jdfit.analyze() that uses Claude to judge, in
nuanced plain language, whether the candidate is eligible for a posting given the
job description and their CV. It returns the EXACT same dict shape as
jdfit.analyze — {"verdict", "reason", "fit_score", "req_years"} — with `verdict`
drawn from the jdfit.VERDICT_* constants, so core.py's drop logic and every
writer stay unchanged.

Safety by construction:
  * Enabled only when config.ENABLE_CLAUDE_JD_ANALYSIS is True.
  * Honesty rules match the rule-based path: an empty JD -> VERDICT_UNKNOWN
    (KEPT, never dropped); only positive evidence of ineligibility -> Ineligible.
  * AUTOMATIC FALLBACK: if the client is None, the request errors, or the reply
    can't be parsed, we return jdfit.analyze(jd_text, title) — today's offline
    result — so a run never breaks and never blocks on the API.
"""

import json
import re

import cvprofile
import jdfit
import llm

_VALID_VERDICTS = {
    jdfit.VERDICT_ELIGIBLE, jdfit.VERDICT_LIKELY, jdfit.VERDICT_UNLIKELY,
    jdfit.VERDICT_INELIGIBLE, jdfit.VERDICT_UNKNOWN,
}

_SYSTEM_PROMPT = (
    "You are a careful, honest job-eligibility screener. Given a job description "
    "and a candidate's CV facts, judge whether the candidate is genuinely "
    "eligible — focusing on hard gates (required years of experience vs a "
    "fresher, required degrees they lack, or a clearly different job family). Be "
    "fair to early-career candidates: transferable skills and internships count. "
    "Never invent candidate qualifications. If the JD is empty or you cannot "
    "tell, say so honestly."
)


def _cv_facts_block():
    """A compact, factual summary of the candidate for the prompt."""
    return (
        f"Years of full-time experience: {cvprofile.YEARS_EXPERIENCE} "
        f"(has internship experience: {cvprofile.HAS_INTERNSHIP}).\n"
        f"Education: Bachelor's={cvprofile.HAS_BACHELORS}, "
        f"Master's-equivalent={cvprofile.HAS_MASTERS_EQUIV}, "
        f"PhD={cvprofile.HAS_PHD}.\n"
        f"Core skills/domain: {', '.join(cvprofile.CV_SKILLS)}.\n"
        f"Target roles: {', '.join(cvprofile.DEFAULT_ROLES)}."
    )


def _build_prompt(jd_text, title, cv_struct):
    # Prefer the full master-CV text when available; else the profile facts.
    cv_block = ""
    if cv_struct and cv_struct.get("ok") and cv_struct.get("full_text"):
        cv_block = cv_struct["full_text"]
    else:
        cv_block = _cv_facts_block()
    return (
        f"JOB TITLE: {title}\n\n"
        f"=== JOB DESCRIPTION ===\n{jd_text.strip()}\n\n"
        f"=== CANDIDATE ===\n{cv_block}\n\n"
        "Judge eligibility. Respond with ONLY a JSON object with keys:\n"
        '  "verdict": one of "Eligible", "Likely", "Unlikely", "Ineligible".\n'
        '  "reason": one short plain-language sentence.\n'
        '  "fit_score": integer 0-100.\n'
        '  "req_years": integer minimum years the JD requires (0 if none).\n'
        "Use \"Ineligible\" ONLY with positive evidence the candidate does not "
        "qualify (e.g. requires many more years than they have, or a degree they "
        "lack, or a clearly unrelated field). No markdown, no commentary."
    )


def _parse(text):
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


def analyze_with_claude(jd_text, title="", cv_struct=None, client=None):
    """
    Claude eligibility verdict with the same return shape as jdfit.analyze.

    Falls back to jdfit.analyze on any problem (no client, empty JD handled by
    jdfit's own Unknown rule, API error, bad JSON, or an out-of-range verdict).
    """
    # Empty JD: defer to the rule-based path, which returns a proper Unknown
    # verdict (kept) — no reason to spend a token on nothing.
    if not jd_text or not str(jd_text).strip():
        return jdfit.analyze(jd_text, title)

    prompt = _build_prompt(str(jd_text), title, cv_struct)
    result = llm.complete(_SYSTEM_PROMPT, prompt, max_tokens=400)
    if not result["ok"]:
        return jdfit.analyze(jd_text, title)

    data = _parse(result["text"])
    if not isinstance(data, dict):
        return jdfit.analyze(jd_text, title)

    verdict = str(data.get("verdict", "")).strip().title()
    if verdict not in _VALID_VERDICTS or verdict == jdfit.VERDICT_UNKNOWN:
        # An unexpected/blank verdict isn't trustworthy — fall back.
        return jdfit.analyze(jd_text, title)

    try:
        fit_score = int(data.get("fit_score", 0))
    except (TypeError, ValueError):
        fit_score = cvprofile.match_score(title)
    fit_score = max(0, min(100, fit_score))

    try:
        req_years = int(data.get("req_years", 0))
    except (TypeError, ValueError):
        req_years = 0

    reason = str(data.get("reason", "")).strip() or "assessed by Claude"
    return {"verdict": verdict, "reason": reason,
            "fit_score": fit_score, "req_years": max(0, req_years)}
