"""
CV-derived profile: default target roles and skills for Gagan Khanna.

Parsed from the master CV (analytics / product / business profile).
These drive (a) the default LinkedIn search keywords and (b) the offline
"CV match score" that ranks how well a job title fits your background.

Edit these lists freely — they are the single source of truth for what
"in coherence with my CV" means.
"""

import json
import os
import re

import config

# NOTE: The role/skill/experience/education values below are DEFAULTS (the
# original single-user profile). When a profile.json exists (built from the
# user's own CV via profileio.py), it overrides these at import time — see the
# "Per-user profile layer" section at the bottom of this file. Every consumer
# reads these names at call time (e.g. cvprofile.YEARS_EXPERIENCE), so the
# rebinding reaches the whole pipeline without touching those modules.

# Default role keywords used as LinkedIn search terms (you can accept/edit
# these at runtime). Ordered roughly by fit.
DEFAULT_ROLES = [
    "Business Analyst",
    "Data Analyst",
    "Product Analyst",
    "Associate Product Manager",
    "Marketing Analyst",
    "Research Analyst",
    "Business Intelligence Analyst",
]

# Skills / domain keywords from the CV. Used only for the match score,
# never sent as search terms.
CV_SKILLS = [
    "excel", "power bi", "powerbi", "python", "pandas", "numpy", "sql",
    "tableau", "google analytics", "ga4", "data analytics", "analytics",
    "data quality", "stakeholder", "project management", "sentiment analysis",
    "dashboard", "forecasting", "product", "marketing", "research",
    "business intelligence", "reporting", "insights", "strategy",
]

# Words in a job title that signal a strong fit with the CV.
_STRONG_TITLE_WORDS = [
    "analyst", "analytics", "business analyst", "data analyst",
    "product analyst", "product manager", "business intelligence",
    "insights", "reporting", "data",
]

# ---------------------------------------------------------------------------
# CV FACTS — used by the JD eligibility analyzer (jdfit.py) to judge whether
# Gagan actually qualifies for a posting, not just whether the title matches.
# Edit these to reflect your real profile.
# ---------------------------------------------------------------------------
# Full-time professional experience. Internships are tracked separately (they
# don't count as full-time years, but signal readiness for entry roles).
YEARS_EXPERIENCE = 0
HAS_INTERNSHIP = True

# Education. PGDM (a postgraduate management diploma) counts as a Master's-
# equivalent for JD filters; BCA is the Bachelor's.
HAS_BACHELORS = True
HAS_MASTERS_EQUIV = True   # PGDM (Marketing)
HAS_PHD = False

# Skills clearly OUTSIDE the analytics/product/marketing domain. Used ONLY for
# the lenient cross-domain drop test: a JD dominated by these, with ~zero
# overlap with CV_SKILLS, is a different job family (e.g. backend engineering).
# A merely-missing BI tool never drops a job — only a clear domain mismatch.
FOREIGN_DOMAIN_SKILLS = [
    "java", "spring", "kubernetes", "docker", "react", "angular", "vue",
    "node.js", "nodejs", "golang", "go lang", "rust", "c++", "c#", ".net",
    "devops", "terraform", "embedded", "firmware", "kernel", "android",
    "ios", "swift", "kotlin", "php", "laravel", "ruby", "rails",
    "microservices", "backend engineer", "frontend engineer", "full stack",
    "full-stack", "sre", "cybersecurity", "penetration testing",
]

# Domain words that keep a trainee/apprentice/graduate title relevant to the
# CV. A "Management Trainee" is only interesting when it's in marketing or
# data/analytics — a bare "Management Trainee" at a bank is not.
_DOMAIN_WORDS = [
    "market", "marketing", "brand", "digital",
    "data", "analyt", "analysis", "business intelligence", "bi ",
    "insight", "research", "product",
]

# Early-career signal words (used by match_score + role_type).
_EARLY_CAREER_WORDS = ["apprentice", "management trainee", "graduate trainee",
                       "trainee", "graduate", "fresher"]


# ---------------------------------------------------------------------------
# Level-specific extra search terms.
#
# When a given experience level is selected, we broaden the search vocabulary
# so we don't miss postings that use a different word for the same thing:
#   * Internship  -> also look for "apprenticeship" / "apprentice"
#   * Entry level -> also look for "management trainee", scoped to the
#                    marketing / data-analytics domains (per the CV).
# These are ADDED to the role keywords, never replace them.
# ---------------------------------------------------------------------------
def _internship_terms(roles):
    """Apprenticeship variants of each role, plus standalone terms."""
    terms = ["Apprenticeship", "Apprentice"]
    for role in roles:
        terms.append(f"{role} Apprentice")
    return terms


def _entry_terms(roles):
    """Management-trainee terms scoped to marketing / data analytics."""
    return [
        "Management Trainee",
        "Marketing Management Trainee",
        "Data Analytics Management Trainee",
        "Business Analytics Management Trainee",
        "Graduate Trainee",
    ]


# level code (config.EXPERIENCE_LEVELS f_E code) -> function(roles) -> [terms]
LEVEL_EXTRA_TERMS = {
    "1": _internship_terms,   # Internship
    "2": _entry_terms,        # Entry level
}


def build_search_terms(roles, level_codes=None):
    """
    Build the full list of search keywords for the selected roles and
    experience levels.

    Starts from the role keywords, then appends the level-specific extras
    (apprenticeship for internships, management trainee for entry level).
    De-duplicated, order-preserving. `level_codes` is a list of the f_E codes
    the user selected (may be empty / None).
    """
    terms = list(roles or [])
    for code in (level_codes or []):
        extra_fn = LEVEL_EXTRA_TERMS.get(str(code))
        if extra_fn:
            terms.extend(extra_fn(roles or []))

    seen, out = set(), []
    for t in terms:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _has_domain_word(title_lower):
    return any(w in title_lower for w in _DOMAIN_WORDS)


def role_type(job_title: str) -> str:
    """
    Classify a posting into a human-readable "kind of role" for the Excel
    Role type column, so internships and full-time roles can live in one
    place while staying distinguishable.

    Returns one of: "Apprenticeship", "Internship", "Trainee", "Full-time".
    """
    if not job_title:
        return "Full-time"
    t = job_title.lower()
    if "apprentic" in t:                       # apprentice / apprenticeship
        return "Apprenticeship"
    # "intern" but not "internal" / "international"
    if "intern" in t and "internal" not in t and "internation" not in t:
        return "Internship"
    if any(w in t for w in ("trainee", "graduate", "fresher")):
        return "Trainee"
    return "Full-time"


def is_senior_title(job_title: str) -> bool:
    """
    True if a title clearly signals a senior / leadership role that an
    early-career profile should NOT be shown.

    Keys off explicit senior TOKENS only — never the bare word "manager" —
    so target roles like "Associate Product Manager" and "Product Manager"
    are preserved (they return False).
    """
    if not job_title:
        return False
    t = job_title.lower()

    # Unambiguous senior signals (plain substring is safe for these).
    for token in ("senior", "principal", "vice president", "head of"):
        if token in t:
            return True

    # Word-boundary tokens, so we don't match inside unrelated words
    # (e.g. "sr" inside "usr", "lead" inside "leader"-free contexts). These
    # never fire on "Associate Product Manager" / "Product Manager" because
    # the bare word "manager" is deliberately NOT in this list.
    for token in ("sr", "lead", "staff", "director", "vp",
                  "ii", "iii", "iv"):
        if re.search(rf"\b{token}\b", t):
            return True
    return False


def match_score(job_title: str, roles: list[str] | None = None) -> int:
    """
    Lightweight, offline 0-100 relevance score of a job title vs the CV.

    No LLM/network. Combines:
      - overlap with the (possibly user-edited) target roles
      - presence of strong analyst/product/data title words
      - overlap with CV skill vocabulary appearing in the title
      - early-career (trainee/apprentice/graduate) titles in the CV's domain

    Returns an int 0-100. Higher = better fit.
    """
    if not job_title:
        return 0

    title = job_title.lower()
    roles = roles or DEFAULT_ROLES
    score = 0

    # 1) Direct role match (strongest signal).
    for role in roles:
        r = role.lower()
        if r in title:
            score += 55
            break
        # partial: all significant words of the role present in the title
        words = [w for w in r.split() if len(w) > 2]
        if words and all(w in title for w in words):
            score += 45
            break

    # 2) Strong title vocabulary.
    for w in _STRONG_TITLE_WORDS:
        if w in title:
            score += 25
            break

    # 3) Skill vocabulary appearing in the title.
    skill_hits = sum(1 for s in CV_SKILLS if s in title)
    score += min(skill_hits * 8, 20)

    # 4) Early-career titles (apprentice/management trainee/graduate/etc.)
    #    count as relevant ONLY when they sit in the CV's domain (marketing /
    #    data / analytics / product / research). This lets the broadened
    #    searches (apprenticeship, management trainee) survive the relevance
    #    gates without letting through unrelated trainee roles.
    if any(w in title for w in _EARLY_CAREER_WORDS) and _has_domain_word(title):
        score += 45

    # Penalise clearly senior/irrelevant titles for an early-career profile.
    for neg in ("director", "vp ", "vice president", "head of", "principal",
                "staff ", "lead ", "manager,"):
        if neg in title:
            score -= 15
            break

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Per-user profile layer.
#
# The facts above (roles, skills, years, education) are DEFAULTS. When a
# profile.json exists — built from the user's OWN CV by profileio.py — its
# values override these module globals at import time, so match_score, jdfit and
# jdfit_claude all judge against THAT user's real profile instead of a hardcoded
# one. Consumers read these names at call time, so rebinding here reaches them
# all with no change to those modules.
#
# profile.json holds only CV-derived facts (no credentials). See profileio.py.
# ---------------------------------------------------------------------------

# profile.json key  ->  the public module global it sets.
PROFILE_FIELDS = {
    "roles": "DEFAULT_ROLES",
    "skills": "CV_SKILLS",
    "foreign_domain_skills": "FOREIGN_DOMAIN_SKILLS",
    "years_experience": "YEARS_EXPERIENCE",
    "has_internship": "HAS_INTERNSHIP",
    "has_bachelors": "HAS_BACHELORS",
    "has_masters_equiv": "HAS_MASTERS_EQUIV",
    "has_phd": "HAS_PHD",
}

# Snapshot the hardcoded seeds so a profile that omits a field falls back to a
# sensible default rather than an empty/zero value.
_DEFAULTS = {key: (list(globals()[name]) if isinstance(globals()[name], list)
                   else globals()[name])
             for key, name in PROFILE_FIELDS.items()}


def defaults():
    """A fresh copy of the built-in default profile (CV-derived facts)."""
    return {k: (list(v) if isinstance(v, list) else v)
            for k, v in _DEFAULTS.items()}


def _coerce(key, value):
    """
    Coerce a loaded value to the type of its default; None/blank -> default.
    Keeps a hand-edited or model-produced profile.json from breaking scoring.
    """
    default = _DEFAULTS[key]
    if value is None:
        return list(default) if isinstance(default, list) else default
    if isinstance(default, bool):          # bool before int (bool is an int)
        return bool(value)
    if isinstance(default, int):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, (list, tuple)):
            items = [str(x).strip() for x in value if str(x).strip()]
        else:                              # tolerate a comma-separated string
            items = [x.strip() for x in str(value).split(",") if x.strip()]
        return items or list(default)
    return value


def normalize_profile(profile):
    """
    Return a COMPLETE, well-typed profile dict from a partial/raw one: every
    PROFILE_FIELDS key present, each coerced to its default's type, missing keys
    filled from the built-in defaults. Pure — does not touch module globals.
    """
    profile = profile or {}
    return {key: _coerce(key, profile.get(key)) for key in PROFILE_FIELDS}


def current_profile():
    """A dict snapshot of the currently-active profile facts."""
    out = {}
    for key, name in PROFILE_FIELDS.items():
        val = globals()[name]
        out[key] = list(val) if isinstance(val, list) else val
    return out


def apply_profile(profile):
    """
    Make `profile` the active profile by rebinding this module's fact globals.

    Normalizes first, so unknown keys are ignored, missing keys reset to the
    built-in default, and values are type-coerced. Safe to call repeatedly.
    """
    norm = normalize_profile(profile)
    for key, name in PROFILE_FIELDS.items():
        globals()[name] = norm[key]


def _load_profile_file():
    """
    Best-effort: if config.PROFILE_FILE exists, apply it over the defaults.
    Never raises and never logs personal data — a missing/corrupt file simply
    leaves the built-in defaults active.
    """
    try:
        path = config.PROFILE_FILE
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                apply_profile(json.load(f))
    except (OSError, ValueError, AttributeError):
        pass


_load_profile_file()
