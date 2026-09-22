"""
Core search pipeline — settings-driven, free of console I/O.

This is the single place that runs the end-to-end flow:

    fetch (per selected source)
      -> validate every link live (HTTP 200)
      -> apply the salary rule (>= 8 LPA or unknown)
      -> apply the seniority filter (drop Senior/Sr/Lead/... for junior searches)
      -> tag every job with a "Role type" (Internship/Apprenticeship/Trainee/Full-time)

`run_search()` takes a plain `settings` dict and a few optional callbacks, and
returns a results dict. It never calls input()/print directly (narration goes
through utils.*, which the interactive CLI shows and a future GUI can capture),
so the SAME core is reused by:

  * jobfinder.py   — the interactive CLI (passes callbacks that prompt the user)
  * a future headless/scheduled mode (passes saved settings, no callbacks)
  * a future GUI / Claude JD-parse+apply agent (see interface.py)

Nothing here fabricates a link: every job comes from a real source response
and every link is validated before it reaches the caller.
"""

import sys

import config
import cvprofile
import dedupe as dedupemod
import jdfetch
import jdfit
import salary as salarymod
import utils
from sources import aggregators, careers, indiaboards, jsearch, linkedin, scraped, workday


# ---------------------------------------------------------------------------
# Settings schema (a plain dict; see interface.py for the full contract).
#
#   roles          list[str]   target role keywords
#   level_codes    list[str]   LinkedIn f_E experience codes selected
#   countries      list[str]   chosen country labels (multi-select)
#   country        str         (legacy) single country label; kept for back-compat
#   queries        list[str]   location query strings (cities or countries)
#   sources        set[str]    which source keys to run (config.SOURCES keys)
#   career_companies   list[str]           (optional) company names for ATS
#   workday_urls   list[(name, url)]       (optional) pasted Workday URLs
#
# Callbacks (optional; used only by interactive mode):
#   ask_url_fn(company)  -> url str   asked when an ATS can't be auto-detected
# ---------------------------------------------------------------------------


def _tag_role_types(jobs):
    """Stamp every job with its human-readable Role type."""
    for j in jobs:
        j["role_type"] = cvprofile.role_type(j.get("title", ""))
    return jobs


def _seniority_filter(jobs):
    """
    Drop clearly-senior titles. Returns (kept, dropped_count).
    Caller decides whether to invoke this (only for junior-only searches).
    """
    kept, dropped = [], 0
    for j in jobs:
        if cvprofile.is_senior_title(j.get("title", "")):
            dropped += 1
        else:
            kept.append(j)
    return kept, dropped


def _make_analyzer():
    """
    Return an eligibility analyzer function analyze(jd, title) -> verdict dict.

    When config.ENABLE_CLAUDE_JD_ANALYSIS is on AND a Claude client is available,
    this is the Claude-powered analyzer (jdfit_claude) with the master CV loaded
    once; otherwise it's the offline rule-based jdfit.analyze. The Claude path
    falls back to jdfit per-call on any error, so a run never breaks.

    The client + CV are built ONCE here (not per job), then closed over.
    """
    if not config.ENABLE_CLAUDE_JD_ANALYSIS:
        return jdfit.analyze

    # Lazy imports so a plain search never needs the optional deps.
    import cvdoc
    import jdfit_claude
    import llm

    ok, reason, labels = llm.available()
    if not ok:
        utils.warn(f"Claude JD analysis is ON but no LLM is available ({reason}). "
                   f"Using the offline rule-based check instead.")
        return jdfit.analyze

    cv_struct = cvdoc.load_master()
    if not cv_struct.get("ok"):
        utils.info(f"Claude JD analysis: {cv_struct.get('error','no master CV')} "
                   f"— judging from profile facts instead of the full CV.")
    utils.ok(f"Claude JD analysis is ON (nuanced eligibility verdicts) via "
             f"{' -> '.join(labels)}.")

    def _analyze(jd, title=""):
        return jdfit_claude.analyze_with_claude(jd, title, cv_struct)
    return _analyze


def _analyze_eligibility(jobs, analyze=None):
    """
    Fetch each job's JD and judge eligibility vs the CV (jdfetch + analyzer).

    `analyze` is a function analyze(jd, title) -> verdict dict (defaults to the
    rule-based jdfit.analyze). See _make_analyzer for the Claude-vs-rule choice.

    Tags every kept job with 'eligibility', 'fit_reason', 'fit_score', and
    drops only the clearly-ineligible ones. Returns (kept, dropped_count).

    Honesty guards:
      * a job whose JD can't be fetched -> "Unknown" verdict -> KEPT.
      * beyond JD_FETCH_MAX jobs, we stop fetching and mark the rest
        "Unknown" (kept) rather than silently ignoring them; the cap is
        narrated by the caller.
    """
    analyze = analyze or jdfit.analyze
    kept, dropped = [], 0
    total = len(jobs)
    for i, job in enumerate(jobs, 1):
        if i <= config.JD_FETCH_MAX:
            jd = jdfetch.get_jd(job)
        else:
            jd = ""  # over the cap: keep, but can't analyse
        res = analyze(jd, job.get("title", ""))
        job["eligibility"] = res["verdict"]
        job["fit_reason"] = res["reason"]
        job["fit_score"] = res["fit_score"]
        if res["verdict"] in jdfit.DROP_VERDICTS:
            dropped += 1
        else:
            kept.append(job)
        sys.stdout.write(f"\r   analysed {i}/{total}  kept {len(kept)}   ")
        sys.stdout.flush()
    sys.stdout.write("\n")
    return kept, dropped


def run_search(settings, ask_url_fn=None):
    """
    Run the full search pipeline for the given settings.

    Returns a dict:
      {
        "linkedin": [...], "career": [...], "other": [...],
        "dropped_salary": int, "dropped_senior": int,
        "total": int,
      }
    Links in every list are already validated. Job dicts carry "role_type".
    """
    roles = settings.get("roles") or list(cvprofile.DEFAULT_ROLES)
    level_codes = [str(c) for c in (settings.get("level_codes") or [])]
    # Countries: support a multi-select `countries` list; fall back to the
    # legacy scalar `country` for backward compatibility (old JSON / callers).
    countries = settings.get("countries") or [settings.get("country", "India")]
    queries = settings.get("queries") or list(countries)
    selected = set(settings.get("sources") or [])

    # Broadened keyword list (adds apprenticeship / management-trainee terms
    # for the selected levels). Used by keyword-driven sources.
    search_terms = cvprofile.build_search_terms(roles, level_codes)

    linkedin_jobs, career_jobs, other_jobs = [], [], []

    # 1) LinkedIn (spans all selected experience levels in one run).
    if "linkedin" in selected:
        raw = linkedin.fetch(search_terms, queries, level_codes)
        linkedin_jobs = utils.validate_links(raw, label="LinkedIn links")

    # 2) Company career pages (ATS auto-detect + own-domain sniff fallback).
    if "careers" in selected:
        for name in settings.get("career_companies") or []:
            raw = careers.fetch_company(name, roles, ask_url_fn=ask_url_fn)
            career_jobs += utils.validate_links(raw, label="career-page links")

    # 3) Workday (pasted per-tenant URLs).
    if "workday" in selected:
        for name, url in settings.get("workday_urls") or []:
            if not url:
                continue
            raw = workday.fetch_from_url(url, name, roles)
            career_jobs += utils.validate_links(raw, label="Workday links")

    # 4) Remote aggregators (run once per selected country, then dedupe).
    if "aggregators" in selected:
        agg_raw = []
        for country in countries:
            agg_raw += aggregators.fetch(search_terms, country)
        agg_raw = utils.dedupe(agg_raw)
        other_jobs += utils.validate_links(agg_raw, label="aggregator links")

    # 5) India boards.
    if "indiaboards" in selected:
        raw = indiaboards.fetch(search_terms)
        other_jobs += utils.validate_links(raw, label="India-board links")

    # 6) JSearch aggregator API (Indeed/Glassdoor/ZipRecruiter/Monster/Google
    #    Jobs/LinkedIn). Run once per selected country, then dedupe. Skips
    #    honestly if no API key is configured.
    if "jsearch" in selected:
        js_raw = []
        for country in countries:
            js_raw += jsearch.fetch(search_terms, country)
        js_raw = utils.dedupe(js_raw)
        other_jobs += utils.validate_links(js_raw, label="JSearch links")

    # 7) Headless-scraped portals (SimplyHired, ...). One browser for the whole
    #    batch, run once per selected country, then deduped.
    #
    #    These links are extracted from a live, non-blocked results page the
    #    browser actually loaded (HTTP 200). Those portals 403 a bare `requests`
    #    call, so utils.validate_links would give FALSE negatives and dishonestly
    #    drop real, currently-listed jobs. The live render IS the validation, so
    #    scraped jobs arrive marked `_validated=True` and are kept directly.
    if "scraped" in selected:
        sc_raw = []
        for country in countries:
            sc_raw += scraped.fetch(search_terms, country)
        other_jobs += utils.dedupe(sc_raw)

    # --- salary filter (across everything) ---
    utils.step(f"Applying salary rule (keep if unknown OR >= "
               f"{config.SALARY_FLOOR_LPA:g} LPA)")
    linkedin_jobs, d1 = salarymod.apply_filter(linkedin_jobs)
    career_jobs, d2 = salarymod.apply_filter(career_jobs)
    other_jobs, d3 = salarymod.apply_filter(other_jobs)
    dropped_salary = d1 + d2 + d3
    if dropped_salary:
        utils.warn(f"Dropped {dropped_salary} job(s) with a stated salary below "
                   f"{config.SALARY_FLOOR_LPA:g} LPA.")
    else:
        utils.ok("No jobs dropped by the salary rule.")

    # --- seniority filter (only when the search is entirely junior-level) ---
    dropped_senior = 0
    junior_only = bool(level_codes) and all(
        c in config.JUNIOR_LEVEL_CODES for c in level_codes)
    if junior_only:
        utils.step("Filtering out Senior / Sr / Lead / Principal / Director "
                   "titles (early-career search)")
        linkedin_jobs, s1 = _seniority_filter(linkedin_jobs)
        career_jobs, s2 = _seniority_filter(career_jobs)
        other_jobs, s3 = _seniority_filter(other_jobs)
        dropped_senior = s1 + s2 + s3
        if dropped_senior:
            utils.warn(f"Dropped {dropped_senior} clearly-senior posting(s) "
                       f"(kept Associate/Product Manager roles).")
        else:
            utils.ok("No senior-titled postings to drop.")

    # --- global dedupe + already-tracked pre-filter (BEFORE the costly JD +
    #     LLM step, so cross-source duplicates and jobs you've already seen
    #     don't burn fetches / LLM tokens) ---
    dropped_duplicate = 0
    dropped_tracked = 0
    if config.ENABLE_GLOBAL_DEDUPE:
        known_id_link, known_content = (set(), set())
        if config.DEDUPE_SKIP_TRACKED:
            known_id_link, known_content = dedupemod.load_known_keys()
        utils.step("De-duplicating across sources"
                   + (" and skipping jobs already in the tracker"
                      if config.DEDUPE_SKIP_TRACKED else ""))
        rebuilt, stats = dedupemod.dedupe_across(
            [("linkedin", linkedin_jobs),
             ("career", career_jobs),
             ("other", other_jobs)],
            known_id_link, known_content)
        linkedin_jobs = rebuilt["linkedin"]
        career_jobs = rebuilt["career"]
        other_jobs = rebuilt["other"]
        dropped_duplicate = stats["cross_source"]
        dropped_tracked = stats["already_tracked"]
        if dropped_duplicate or dropped_tracked:
            utils.warn(
                f"Dropped {dropped_duplicate} cross-source duplicate(s) and "
                f"{dropped_tracked} job(s) already in the master tracker "
                f"(saved that many JD fetches / LLM calls).")
        else:
            utils.ok("No cross-source duplicates or already-tracked jobs.")

    # --- JD eligibility analysis (fetch the description, judge fit vs CV) ---
    dropped_ineligible = 0
    if config.ENABLE_JD_ANALYSIS:
        surviving = len(linkedin_jobs) + len(career_jobs) + len(other_jobs)
        utils.step("Reading each job description and checking eligibility "
                   "vs your CV (experience / education / skills)")
        if surviving > config.JD_FETCH_MAX:
            utils.warn(f"{surviving} jobs to analyse but the per-run cap is "
                       f"{config.JD_FETCH_MAX}; the rest are kept and marked "
                       f"'Unknown' (not dropped).")
        # Build the analyzer ONCE (Claude client + master CV, or the rule-based
        # fallback), then reuse it for all three buckets.
        analyze = _make_analyzer()
        linkedin_jobs, e1 = _analyze_eligibility(linkedin_jobs, analyze)
        career_jobs, e2 = _analyze_eligibility(career_jobs, analyze)
        other_jobs, e3 = _analyze_eligibility(other_jobs, analyze)
        dropped_ineligible = e1 + e2 + e3
        if dropped_ineligible:
            utils.warn(f"Dropped {dropped_ineligible} job(s) you're clearly "
                       f"not eligible for (e.g. senior experience / different "
                       f"domain). Borderline ones are kept and flagged.")
        else:
            utils.ok("No jobs dropped by the eligibility check.")

    # --- tag role types (so internships + full-time coexist, distinguishable) ---
    _tag_role_types(linkedin_jobs)
    _tag_role_types(career_jobs)
    _tag_role_types(other_jobs)

    total = len(linkedin_jobs) + len(career_jobs) + len(other_jobs)
    return {
        "linkedin": linkedin_jobs,
        "career": career_jobs,
        "other": other_jobs,
        "dropped_salary": dropped_salary,
        "dropped_senior": dropped_senior,
        "dropped_duplicate": dropped_duplicate,
        "dropped_tracked": dropped_tracked,
        "dropped_ineligible": dropped_ineligible,
        "total": total,
    }
