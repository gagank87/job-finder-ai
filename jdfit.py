"""
JD eligibility analyzer — rule-based, offline (no API key, no network).

Given a job's full description text and its title, decide whether Gagan is
actually *eligible*, judging three real signals against his CV facts
(cvprofile.py):

  * Experience   — years required vs YEARS_EXPERIENCE (fresher signals help).
  * Education    — required degree vs HAS_BACHELORS / HAS_MASTERS_EQUIV / HAS_PHD.
  * Skills       — CV_SKILLS overlap; a *lenient* cross-domain mismatch test.

`analyze()` returns a verdict + a short human reason + a 0-100 fit score. It is
a single pure function so a future Claude-based analyzer can drop in behind the
same signature (see interface.py) without touching the pipeline.

Honesty rule: an empty / unavailable JD yields verdict "Unknown" — the caller
KEEPS such jobs. Only positive evidence of ineligibility produces "Ineligible".
"""

import re

import config
import cvprofile

# Verdicts, worst-to-best (also the drop set).
VERDICT_INELIGIBLE = "Ineligible"   # positive evidence you don't qualify -> drop
VERDICT_UNLIKELY = "Unlikely"       # stretch (e.g. 1-2 yrs over fresher) -> keep+flag
VERDICT_LIKELY = "Likely"           # good fit, minor gaps -> keep
VERDICT_ELIGIBLE = "Eligible"       # clear fit -> keep
VERDICT_UNKNOWN = "Unknown"         # JD unavailable -> keep, can't judge

DROP_VERDICTS = {VERDICT_INELIGIBLE}


# ---------------------------------------------------------------------------
# Experience parsing.
# ---------------------------------------------------------------------------
# "5+ years", "3-5 years", "minimum 4 yrs", "at least 2 years of experience"
_YEARS_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:\+|to|-|–)\s*(\d{1,2})\s*(?:\+)?\s*years?", re.I)
_YEARS_SINGLE_RE = re.compile(r"(\d{1,2})\s*\+?\s*years?", re.I)

_FRESHER_SIGNALS = [
    "fresher", "freshers", "entry level", "entry-level", "no experience",
    "no prior experience", "0-1 year", "0-2 year", "0 to 2 year",
    "graduate", "recent graduate", "trainee", "apprentice", "internship",
    "students", "final year", "campus", "early career", "0-3 year",
]


def _min_required_years(text):
    """
    Best-effort minimum years of experience the JD requires.

    Returns an int (0 if none stated). Uses the SMALLEST plausible figure so
    ranges like "3-5 years" gate on 3, and we never over-reject. Only counts
    figures that appear near the word 'experience' to avoid false hits on
    unrelated numbers (e.g. "5 years founded", "team of 5 years running").
    """
    years = []
    # Look only within windows around the word "experience".
    for m in re.finditer(r"experience", text, re.I):
        window = text[max(0, m.start() - 60): m.end() + 40]
        rng = _YEARS_RANGE_RE.search(window)
        if rng:
            years.append(min(int(rng.group(1)), int(rng.group(2))))
            continue
        one = _YEARS_SINGLE_RE.search(window)
        if one:
            years.append(int(one.group(1)))
    return min(years) if years else 0


def _has_fresher_signal(low):
    return any(sig in low for sig in _FRESHER_SIGNALS)


# ---------------------------------------------------------------------------
# Education parsing.
# ---------------------------------------------------------------------------
_PHD_RE = re.compile(r"\bph\.?\s?d\b|\bdoctorate\b|\bdoctoral\b", re.I)
_MASTERS_RE = re.compile(r"\bmaster'?s?\b|\bm\.?\s?tech\b|\bmba\b|\bm\.?\s?sc\b|\bpost[\s-]?graduat", re.I)
_BACHELORS_RE = re.compile(r"\bbachelor'?s?\b|\bb\.?\s?tech\b|\bb\.?\s?sc\b|\bb\.?\s?e\b|\bgraduate\b|\bdegree\b|\bundergraduate\b", re.I)
_REQUIRED_NEAR = re.compile(r"require|must|mandatory|minimum|essential", re.I)


def _education_gap(low):
    """
    Return a reason string if the JD hard-requires a degree we DON'T have,
    else "". PhD is the only realistic blocker (we have Bachelor's + PGDM).
    """
    if _PHD_RE.search(low) and not cvprofile.HAS_PHD:
        # Only block if the PhD is framed as required, not "PhD a plus".
        for m in _PHD_RE.finditer(low):
            window = low[max(0, m.start() - 50): m.start()]
            if _REQUIRED_NEAR.search(window):
                return "requires a PhD"
    return ""


# ---------------------------------------------------------------------------
# Skills.
# ---------------------------------------------------------------------------
def _skill_overlap(low):
    """Count distinct CV skills that appear in the JD."""
    hits = {s for s in cvprofile.CV_SKILLS if s in low}
    return hits


def _foreign_dominance(low):
    """Count distinct foreign-domain skills present in the JD."""
    return {s for s in cvprofile.FOREIGN_DOMAIN_SKILLS if s in low}


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def analyze(jd_text, title=""):
    """
    Judge eligibility from the JD text.

    Returns a dict:
      {
        "verdict":  one of the VERDICT_* strings,
        "reason":   short human explanation,
        "fit_score": int 0-100,
        "req_years": int minimum years the JD asks for (0 if none),
      }

    An empty / whitespace JD -> "Unknown" (caller keeps the job).
    """
    if not jd_text or not str(jd_text).strip():
        return {"verdict": VERDICT_UNKNOWN,
                "reason": "JD unavailable — kept for manual check",
                "fit_score": cvprofile.match_score(title),
                "req_years": 0}

    text = str(jd_text)
    low = text.lower()

    reasons = []
    drop = False

    # --- experience ---
    req_years = _min_required_years(low)
    fresher = _has_fresher_signal(low)
    exp_ok = True
    if fresher:
        reasons.append("fresher/entry welcome")
    elif req_years:
        if req_years >= config.JD_DROP_MIN_YEARS and req_years > cvprofile.YEARS_EXPERIENCE:
            drop = True
            exp_ok = False
            reasons.append(f"requires {req_years}+ yrs; you're a fresher")
        elif req_years > cvprofile.YEARS_EXPERIENCE:
            exp_ok = False
            reasons.append(f"asks for {req_years} yr(s) — a stretch")
        else:
            reasons.append(f"exp {req_years} yr ok")

    # --- education ---
    edu_gap = _education_gap(low)
    if edu_gap:
        drop = True
        reasons.append(edu_gap)

    # --- skills ---
    overlap = _skill_overlap(low)
    foreign = _foreign_dominance(low)
    # Lenient cross-domain drop: JD is clearly another field AND we match ~nothing.
    if len(overlap) == 0 and len(foreign) >= 3:
        drop = True
        reasons.append("different domain (little skill overlap)")
    elif overlap:
        reasons.append(f"{len(overlap)} matching skill(s)")

    # --- fit score (0-100) ---
    title_score = cvprofile.match_score(title)
    skill_pts = min(len(overlap) * 8, 40)
    exp_pts = 25 if (fresher or exp_ok) else 0
    edu_pts = 15 if not edu_gap else 0
    fit_score = max(0, min(100, int(0.35 * title_score + skill_pts + exp_pts + edu_pts)))

    # --- verdict ---
    if drop:
        verdict = VERDICT_INELIGIBLE
    elif not exp_ok:
        # experience is a stretch but not a hard blocker -> keep, flag
        verdict = VERDICT_UNLIKELY
    elif fit_score >= 70 and (fresher or not req_years or exp_ok):
        verdict = VERDICT_ELIGIBLE
    else:
        verdict = VERDICT_LIKELY

    reason = "; ".join(reasons) if reasons else "no blocking requirements found"
    return {"verdict": verdict, "reason": reason,
            "fit_score": fit_score, "req_years": req_years}
