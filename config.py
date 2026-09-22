"""
Central configuration for the Job Finder tool.

Everything here is meant to be edited freely as you extend the tool.
No secrets, no login required for the default sources — all are public.

The CV-tailoring / apply feature (cvtailor / cvdoc / apply / autosubmit) DOES
use Claude, but no credential is EVER stored here or anywhere in the repo. Two
auth backends are supported and auto-selected at runtime:
  * Amazon Bedrock  — used when AWS_BEARER_TOKEN_BEDROCK is set in the env; the
                      SDK reads that bearer token from the environment itself.
  * Direct Anthropic — otherwise, via the ANTHROPIC_API_KEY environment variable
                      (or an optional gitignored secrets file).
Neither credential is ever printed or logged. See get_api_key(), use_bedrock(),
and active_model_id() at the bottom.
"""

import os

# ---------------------------------------------------------------------------
# LinkedIn experience-level codes (the `f_E` query parameter).
# These are LinkedIn's own official codes for its guest job search.
# ---------------------------------------------------------------------------
EXPERIENCE_LEVELS = {
    "1": ("Internship", "1"),
    "2": ("Entry level", "2"),
    "3": ("Associate", "3"),
    "4": ("Mid-Senior level", "4"),
    "5": ("Director", "5"),
    "6": ("Executive", "6"),
}

# Experience-level codes considered "junior" / early-career. When every
# selected level is in this set, the seniority filter drops Senior/Sr/Lead/
# etc. titles (see cvprofile.is_senior_title). If the user also picks a
# senior level (e.g. Mid-Senior), the filter is disabled so nothing is lost.
JUNIOR_LEVEL_CODES = {"1", "2", "3"}  # Internship, Entry level, Associate

# "Posted in last 24 hours" filter for LinkedIn (f_TPR).
LINKEDIN_TPR_24H = "r86400"

# How many pages of LinkedIn results to pull per (role x city) query.
# Each page returns ~10 jobs. Keep modest to stay polite / avoid rate limits.
LINKEDIN_MAX_PAGES = 3

# 429 (rate-limit) handling for the LinkedIn guest API. A 429 is transient —
# LinkedIn is throttling our IP because too many requests arrived too fast
# (multi-country x cities x roles x levels x pages multiplies quickly). Rather
# than give up on the whole run, we pause and retry the SAME request with an
# escalating wait, honoring any Retry-After header. Only after this many failed
# retries in a row do we stop LinkedIn for the run (and say so honestly).
LINKEDIN_MAX_RETRIES = 3          # retries per request after a 429
LINKEDIN_BACKOFF_SECONDS = 20     # base wait; grows 20s, 40s, 60s...
LINKEDIN_BACKOFF_MAX_SECONDS = 90 # cap on any single wait

# Polite delay (seconds) between network calls, to avoid rate-limiting/blocks.
REQUEST_DELAY_SECONDS = 1.5

# Standard browser-like User-Agent so public endpoints respond normally.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Network timeout (seconds) for each HTTP request.
HTTP_TIMEOUT = 20

# ---------------------------------------------------------------------------
# ATS (Applicant Tracking System) registry for company career pages.
#
# Most companies host jobs on one of these platforms, each with a PUBLIC
# JSON API that returns REAL job IDs + canonical apply URLs. We try each
# in turn using a guessed "token" derived from the company name. If none
# resolve, the tool falls back to asking you to paste the careers URL.
#
# Workday is per-tenant (each company has its own subdomain + board name),
# so it is NOT auto-guessed from the name — it's handled via a pasted URL.
# ---------------------------------------------------------------------------
ATS_PLATFORMS = ["greenhouse", "lever", "ashby", "smartrecruiters"]

# ---------------------------------------------------------------------------
# Standardized location list (menu-driven; keeps vocabulary consistent).
# Each entry: label -> the string passed to LinkedIn's location param and
# used for aggregator country-matching.
# ---------------------------------------------------------------------------
COUNTRIES = [
    "India",
    "United States",
    "Remote / Worldwide",
]

# ---------------------------------------------------------------------------
# Source registry — the multi-select menu is built from this.
# key -> (display label, enabled). Disable a source by flipping to False.
# ---------------------------------------------------------------------------
SOURCES = {
    "linkedin":       ("LinkedIn (posted last 24h)", True),
    "careers":        ("Company career pages (Greenhouse/Lever/Ashby/SmartRecruiters)", True),
    "workday":        ("Workday career pages (paste company Workday URL)", True),
    "aggregators":    ("Remote boards (RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy, WorkingNomads, Jobspresso)", True),
    "indiaboards":    ("India boards (Instahyre, Unstop)", True),
    "jsearch":        ("Indeed / Glassdoor / ZipRecruiter / Monster / Google Jobs (via JSearch API key)", True),
    "scraped":        ("Headless-scraped portals (SimplyHired) - drives Chrome, no key needed", True),
}

# ---------------------------------------------------------------------------
# Headless-browser scraping (sources/headless.py + sources/scraped.py).
#
# For portals that render jobs with JavaScript and/or block plain HTTP, the
# tool drives a real Chrome (Selenium; the driver auto-installs on first use).
# Kept at the same importance as LinkedIn for search. No API key is needed.
#
# HEADLESS_MODE:
#   "headless" (default) — runs invisibly; right for portals with no login wall
#                          (SimplyHired).
#   "headed"             — opens a visible Chrome window so YOU can sign in or
#                          solve a one-time CAPTCHA for login-walled portals
#                          (Wellfound, SnagAJob). The session is saved in
#                          HEADLESS_PROFILE_DIR and reused on later runs.
# ---------------------------------------------------------------------------
HEADLESS_MODE = "headless"
HEADLESS_PAGE_TIMEOUT = 45   # seconds allowed for a single page load
HEADLESS_RENDER_WAIT = 4     # seconds to let JS render after a load
HEADLESS_RENDER_WAIT_MAX = 14  # max seconds to poll for a card selector to appear
HEADLESS_MAX_PAGES = 2       # result pages to pull per role, per portal
# Dedicated (NOT your real Chrome) profile dir; persists a manual sign-in for
# login-walled portals. Under output/ so it's gitignored. Used only in "headed".
HEADLESS_PROFILE_DIR = "output/.browser_profile"

# Which scraped portals are enabled (key = adapter name, lower-cased). Add more
# adapters in sources/scraped.py. Login/CAPTCHA-walled portals need
# HEADLESS_MODE='headed' + a one-time manual sign-in before enabling here.
SCRAPERS = {
    "simplyhired": True,
    "naukri": True,      # India's largest board; headless, no login/CAPTCHA
    "wellfound": True,   # startup/tech jobs; /role/ pages render without login
    "glassdoor": True,   # hostile to headless (eager load + card-presence gate);
                         # also covered via JSearch, so this is a direct fallback
}

# ---------------------------------------------------------------------------
# JSearch (RapidAPI) — one legitimate aggregator API that returns REAL postings
# from Indeed, Glassdoor, ZipRecruiter, Monster, Google Jobs, LinkedIn and more,
# with canonical apply links. This is how those major portals are covered
# without scraping or bans. It needs a free RapidAPI key (jsearch.p.rapidapi.com);
# the "jsearch" source honestly skips with a one-line instruction if no key is set.
#
# The key is a secret and is treated EXACTLY like the Claude key / AWS token:
# read at runtime from the JSEARCH_API_KEY environment variable (recommended) or
# an optional gitignored secrets file — NEVER stored in the repo, NEVER printed.
# ---------------------------------------------------------------------------
JSEARCH_API_KEY_FILE = "secrets/jsearch_key.txt"
JSEARCH_HOST = "jsearch.p.rapidapi.com"
# How many result pages to pull per role (each page ~10 jobs). Keep modest — the
# free RapidAPI tier has a monthly request quota.
JSEARCH_MAX_PAGES = 1

# ---------------------------------------------------------------------------
# Salary filter.
# Keep a job ONLY IF salary is unknown (not stated) OR annual >= this floor.
# Internships/trainee roles are kept (title never used to infer low pay).
# ---------------------------------------------------------------------------
SALARY_FLOOR_LPA = 8.0  # lakhs per annum (INR)

# ---------------------------------------------------------------------------
# JD (job-description) eligibility analysis.
#
# When enabled, the tool fetches each surviving job's full description and
# judges eligibility against your CV facts (experience / education / skills;
# see cvprofile.py + jdfit.py) — not just the title. Clearly-ineligible jobs
# are dropped (count reported, like the salary filter); the rest are kept and
# flagged with an Eligibility verdict + reason + fit score.
#
# Honesty rule: a job whose JD can't be fetched is NEVER dropped — it's kept
# with an "Unknown" verdict. Only positive evidence of ineligibility drops.
# ---------------------------------------------------------------------------
ENABLE_JD_ANALYSIS = True   # set False to reproduce exact pre-JD behavior

# Global cross-source dedupe: before the (expensive) JD-fetch + LLM eligibility
# step, collapse the same job appearing under multiple sources (e.g. the
# LinkedIn source vs JSearch/LinkedIn, or scraped Glassdoor vs JSearch/Glassdoor)
# to ONE record, keyed on normalized company+title. The per-source dedupe only
# catches dupes within a single bucket; this catches them across buckets.
ENABLE_GLOBAL_DEDUPE = True

# Skip jobs already in the master tracker BEFORE analysing them, so a repeated
# or scheduled (conductor) run doesn't re-fetch JDs and re-spend LLM tokens on
# jobs you've already seen. Matches on tracker identity keys AND company+title.
DEDUPE_SKIP_TRACKED = True

# Drop a job only when its stated MINIMUM required experience is >= this many
# years AND the JD shows no fresher/entry-level signal (you're a fresher).
# Postings asking for 1-2 years are kept but flagged.
JD_DROP_MIN_YEARS = 3

# Safety cap on how many JDs to fetch per run (protects runtime). If a run has
# more surviving jobs than this, the overflow is kept but analysed as
# "Unknown", and the cap is narrated — never silently truncated.
JD_FETCH_MAX = 120

# Fixed FX rates -> INR, for converting foreign salaries before the >=8 LPA
# test. Approximate; edit as needed. Rough threshold check only.
FX_TO_INR = {
    "INR": 1.0,
    "USD": 83.0,
    "EUR": 90.0,
    "GBP": 105.0,
    "CAD": 61.0,
    "AUD": 55.0,
    "SGD": 62.0,
    "AED": 22.6,
}

# ---------------------------------------------------------------------------
# Files / folders (relative to this file).
# ---------------------------------------------------------------------------
OUTPUT_DIR = "output"
MASTER_TRACKER = "master_tracker.xlsx"   # single growing, de-duplicated file

# Saved search settings for the headless / scheduled "conductor" run. Written
# by the GUI's "Save settings" button (and reusable from the CLI); read by
# `python jobfinder.py --headless`. Holds the roles/levels/locations/sources
# YOU selected, so the scheduled run never falls back to a hardcoded default.
SETTINGS_FILE = "settings.json"
COOKIES_FILE = "cookies.json"            # optional; site -> cookie string

# In-progress fetch cache. Results are written here right before the QC gate
# so an accidental exit (or a mis-typed "no") never costs a full re-fetch.
# It is auto-deleted the moment the run is resolved either way — on a rejected
# QC, and on a successful write into the master tracker (see runcache.py).
CACHE_DIR = "output/.cache"
CACHE_FILE = "output/.cache/last_run.json"

# How many aggregator/india-board results to pull per role query.
AGGREGATOR_MAX_PER_ROLE = 50

# ---------------------------------------------------------------------------
# CV tailoring + prepare/apply feature (cvtailor / cvdoc / apply / autosubmit).
#
# For each eligible job the tool can: read the JD, use Claude to tailor your CV
# (rewriting/reordering ONLY what's genuinely on it — never inventing facts),
# draft a cover letter in the tone of your reference, deliver a tailored PDF,
# and either auto-submit (only where a public ATS endpoint truly allows it, and
# only recording "Applied" on a confirmed submission) or stage everything for
# you to submit. A human approval step is always kept.
# ---------------------------------------------------------------------------

# Your master CV (Word .docx). Drop your file here; it is gitignored (personal).
MASTER_CV_PATH = "cv/master_cv.docx"

# Reference cover letter whose tone + structure Claude should mirror. Gitignored.
# If absent, cover-note drafting is skipped (never a fabricated style).
COVER_REFERENCE_PATH = "cv/cover_reference.txt"

# Your real apply data (name/email/phone/links/work-authorization answers), used
# ONLY for auto-submit. Gitignored. If missing/incomplete, auto-submit is
# disabled and everything falls back to prepare-to-apply.
APPLICANT_PROFILE_PATH = "secrets/applicant_profile.json"

# Where per-job application folders (tailored CV + cover note + details) go.
APPLICATIONS_DIR = "output/applications"

# Optional gitignored file holding the Claude key, used ONLY if the environment
# variable isn't set. The env var is the recommended, never-on-disk path.
API_KEY_FILE = "secrets/anthropic_key.txt"

# Optional gitignored file holding the AWS Bedrock bearer token, used ONLY if
# the AWS_BEARER_TOKEN_BEDROCK environment variable isn't set. Same as the key
# above: the env var is preferred; this file is a fallback so a `setx` that only
# affects new shells (or a token that never got persisted) still works.
BEDROCK_TOKEN_FILE = "secrets/aws_bedrock_token.txt"

# Claude model for tailoring and (optionally) the JD eligibility analyzer.
# On Bedrock this auto-maps to the cross-region inference profile for your
# region, e.g. "claude-opus-4-8" -> "us.anthropic.claude-opus-4-8" (see
# active_model_id / bedrock_model_id below).
CLAUDE_MODEL = "claude-opus-4-8"

# ---------------------------------------------------------------------------
# Multi-provider LLM fallback (see llm.py).
#
# CV tailoring and the (optional) JD analyzer need just one thing from a model:
# system + prompt -> text. llm.complete() tries these providers IN ORDER and
# returns the first success, so if your primary backend is down / rate-limited /
# out of quota, the tool automatically falls through to the next FREE provider
# instead of failing the job. A provider is skipped when its credential is absent.
#
# Every key is a secret: read at runtime from an env var (recommended) or an
# optional gitignored secrets file — NEVER stored in the repo, NEVER printed.
# All are free tiers you can create in minutes:
#   * Groq   — https://console.groq.com/keys        (OpenAI-compatible)
#   * Gemini — https://aistudio.google.com/app/apikey (Google AI Studio)
# ---------------------------------------------------------------------------
LLM_PROVIDER_ORDER = ["anthropic", "groq", "gemini"]

# Groq (free, OpenAI-compatible). Key: GROQ_API_KEY env var or the file below.
GROQ_API_KEY_FILE = "secrets/groq_key.txt"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Google Gemini via its OpenAI-compatible endpoint. Key: GEMINI_API_KEY (or
# GOOGLE_API_KEY) env var, or the file below.
GEMINI_API_KEY_FILE = "secrets/gemini_key.txt"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
# "gemini-flash-latest" is a moving alias that always points at the current
# free Flash model, so this won't 404 when a specific dated model is retired.
GEMINI_MODEL = "gemini-flash-latest"

# ---------------------------------------------------------------------------
# Auth backend: direct Anthropic API  vs  Amazon Bedrock (AWS bearer token).
#
# You can reach Claude two ways, and the tool auto-detects which:
#   * Direct Anthropic  — set ANTHROPIC_API_KEY (or the secrets file). Default.
#   * Amazon Bedrock    — set the AWS_BEARER_TOKEN_BEDROCK environment variable
#                         (Bedrock's API-key style auth). When that variable is
#                         present, the tool uses Bedrock automatically and the
#                         Anthropic key is not needed.
#
# The bearer token, exactly like the Anthropic key, is NEVER stored in the repo
# and NEVER printed or logged — it's read from the environment at runtime only.
#
# Bedrock needs a region (its endpoints are regional). We read AWS_REGION /
# AWS_DEFAULT_REGION from the environment, falling back to this default.
# ---------------------------------------------------------------------------
AWS_REGION = (os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION")
              or "us-east-1")

# Optional explicit Bedrock inference-profile ID / ARN. Leave "" to auto-derive
# from CLAUDE_MODEL + AWS_REGION (on-demand Claude on Bedrock is served through
# cross-region inference profiles, e.g. "us.anthropic.claude-sonnet-5"). Set this
# to pin an exact profile ID or ARN if your account uses a non-default one.
BEDROCK_MODEL = ""

# STEP 1 — Claude-powered JD eligibility analyzer. OFF by default: the offline
# rule-based jdfit.analyze runs as today (free, no key). When True, Claude
# judges eligibility with a nuanced verdict; if the key/SDK is missing or a call
# fails, it AUTOMATICALLY falls back to jdfit so a run never breaks. Because this
# runs once per surviving job per run, it uses far more tokens than tailoring —
# keep it opt-in.
ENABLE_CLAUDE_JD_ANALYSIS = False

# Master switch for real auto-submission. When False, the apply flow always
# prepares (never submits), regardless of ATS capability.
ENABLE_AUTO_SUBMIT = True

# Safety cap: at most this many jobs are tailored/prepared per apply run. The
# confirmation prompt shows the count so cost is never a surprise. Raise/lower
# freely; `python apply.py --all` ignores it for a one-off unlimited run.
APPLY_MAX_JOBS = 100

# Which eligibility verdicts are considered worth preparing/applying to. Anything
# dropped by the analyzer (Ineligible) is already gone; "Unknown" is included so
# JD-unavailable jobs aren't silently skipped.
APPLY_ELIGIBILITY_VERDICTS = {"Eligible", "Likely", "Unlikely", "Unknown"}


def _clean_key(raw):
    """
    Normalize a key string a human may have pasted with extra characters.

    Tolerates surrounding whitespace/quotes and a leading `ANTHROPIC_API_KEY=`
    (or `KEY=`) prefix, so a value copied from a .env line or a shell command
    still works. Returns the bare key, or None if nothing usable remains.
    """
    if not raw:
        return None
    key = raw.strip()
    if not key:
        return None
    # Drop a leading VAR= prefix if the whole thing was pasted (e.g. a .env line).
    if "=" in key and key.split("=", 1)[0].strip().upper().endswith("API_KEY"):
        key = key.split("=", 1)[1].strip()
    # Strip matching surrounding quotes.
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1].strip()
    return key or None


def get_api_key():
    """
    Return the Claude API key, or None if unavailable.

    Order: the ANTHROPIC_API_KEY environment variable (recommended — never
    touches the repo), then an optional gitignored secrets file (API_KEY_FILE).
    Both are cleaned by _clean_key so a pasted value with quotes / a VAR= prefix
    / stray whitespace still works. NEVER prints or logs the key. Callers treat
    None as "tailoring disabled".
    """
    key = _clean_key(os.environ.get("ANTHROPIC_API_KEY"))
    if key:
        return key
    try:
        # Read as bytes and decode tolerantly: a file saved from Notepad or
        # PowerShell `echo >` on Windows is often UTF-16 (with a BOM), not UTF-8.
        # We strip any BOM and try UTF-8 then UTF-16 so a correctly-pasted key
        # works regardless of how the editor saved it.
        with open(API_KEY_FILE, "rb") as f:
            raw = f.read()
        text = _decode_key_bytes(raw)
        file_key = _clean_key(text)
        if file_key:
            return file_key
    except OSError:
        pass
    return None


def _key_from(env_names, file_path):
    """
    Resolve a secret key: try each environment variable in `env_names` (in
    order), then an optional gitignored file. Values are cleaned by _clean_key
    and decoded tolerantly (UTF-8/UTF-16 BOM). Returns the bare key or None.
    NEVER prints or logs the value. Shared by the JSearch/Groq/Gemini getters.
    """
    for env_name in ([env_names] if isinstance(env_names, str) else env_names):
        key = _clean_key(os.environ.get(env_name))
        if key:
            return key
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
        file_key = _clean_key(_decode_key_bytes(raw))
        if file_key:
            return file_key
    except OSError:
        pass
    return None


def get_jsearch_key():
    """
    Return the JSearch (RapidAPI) key, or None if unavailable. Order: the
    JSEARCH_API_KEY env var (recommended — never touches the repo), then the
    gitignored JSEARCH_API_KEY_FILE. Callers treat None as "unconfigured — skip
    honestly". NEVER prints or logs the key.
    """
    return _key_from("JSEARCH_API_KEY", JSEARCH_API_KEY_FILE)


def get_groq_key():
    """Return the Groq API key (GROQ_API_KEY env var or GROQ_API_KEY_FILE), or
    None. Never printed or logged."""
    return _key_from("GROQ_API_KEY", GROQ_API_KEY_FILE)


def get_gemini_key():
    """Return the Google Gemini key (GEMINI_API_KEY or GOOGLE_API_KEY env var,
    or GEMINI_API_KEY_FILE), or None. Never printed or logged."""
    return _key_from(["GEMINI_API_KEY", "GOOGLE_API_KEY"], GEMINI_API_KEY_FILE)


def _decode_key_bytes(raw):
    """Decode key-file bytes, honoring a UTF-8/UTF-16 BOM if present."""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # No BOM but not valid UTF-8 — most likely UTF-16 without a BOM.
        return raw.decode("utf-16", errors="replace")


def get_bedrock_token():
    """
    Return the AWS Bedrock bearer token, or None. Order: the
    AWS_BEARER_TOKEN_BEDROCK environment variable (preferred), then an optional
    gitignored file (BEDROCK_TOKEN_FILE). Decoded tolerantly (UTF-8/UTF-16) like
    the Anthropic key, so a value saved from Notepad/PowerShell still works.
    NEVER prints or logs the token.
    """
    tok = (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
    if tok:
        return tok
    try:
        with open(BEDROCK_TOKEN_FILE, "rb") as f:
            raw = f.read()
        file_tok = _decode_key_bytes(raw).strip()
        if file_tok:
            return file_tok
    except OSError:
        pass
    return None


def use_bedrock():
    """
    True when the tool should authenticate to Claude via Amazon Bedrock rather
    than the direct Anthropic API. Bedrock is selected whenever a bearer token
    is available — from AWS_BEARER_TOKEN_BEDROCK or the gitignored token file.
    Only presence is tested here; the token value is never returned or logged.
    """
    return get_bedrock_token() is not None


def _bedrock_geo_prefix():
    """
    The geographic prefix for a Bedrock cross-region inference profile, derived
    from AWS_REGION. On-demand Claude access on Bedrock is served through these
    inference profiles (e.g. 'us.anthropic.claude-sonnet-5'), not the bare model
    ID — so the prefix must match the region's geography.
    """
    region = (AWS_REGION or "").lower()
    if region.startswith("eu-"):
        return "eu"
    if region.startswith(("ap-", "apac-")):
        return "apac"
    if region.startswith("us-gov-"):
        return "us-gov"
    # us-*, ca-*, sa-* and anything else default to the US profile group.
    return "us"


def bedrock_model_id():
    """
    The model identifier to send on a Bedrock request. Bedrock serves on-demand
    Claude through cross-region *inference profiles* whose IDs carry both a geo
    prefix and the 'anthropic.' vendor prefix, e.g. 'us.anthropic.claude-sonnet-5'
    (the bare 'anthropic.claude-sonnet-5' is rejected for on-demand throughput).
    The direct Anthropic API, by contrast, uses the bare name 'claude-sonnet-5'.

    If BEDROCK_MODEL is set explicitly it wins (use it to pin an exact profile ID
    or ARN); otherwise we derive the profile ID from CLAUDE_MODEL + the region.
    """
    if BEDROCK_MODEL.strip():
        return BEDROCK_MODEL.strip()
    model = CLAUDE_MODEL.strip()
    if model.startswith(("us.", "eu.", "apac.", "us-gov.")):
        return model  # already a fully-qualified inference-profile ID
    if model.startswith("anthropic."):
        return f"{_bedrock_geo_prefix()}.{model}"
    return f"{_bedrock_geo_prefix()}.anthropic.{model}"


def active_model_id():
    """The model ID for the currently-selected backend (Bedrock vs direct)."""
    return bedrock_model_id() if use_bedrock() else CLAUDE_MODEL
