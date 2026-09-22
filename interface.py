"""
interface.py — the contract doc for the front-ends + Claude agent. (BUILT.)

This module intentionally contains NO working UI. It exists to pin down, in code,
the contract every front-end and the Claude agent talk to, so that work never
requires reshaping the core. The GUI, headless mode, and the CV-tailoring /
prepare-apply agent described below are now all built (see the markers).

WHY THIS EXISTS
---------------
The tool is designed as several DIFFERENT FRONT-ENDS over one search pipeline:
  * The interactive CLI (jobfinder.py) and a Tkinter GUI (gui.py).
  * A headless/scheduled mode (jobfinder.run_headless).
  * A Claude agent that, per job, reads the job description (JD), tailors the
    CV to it, and prepares/submits the application (apply.py + cvtailor/cvdoc).

All of those are just different front-ends over the same search pipeline.
That pipeline already lives in `core.run_search(settings)` and is deliberately
free of console I/O, so a GUI/agent can call it directly and render the results.

THE CONTRACT (what a GUI / agent talks to)
------------------------------------------
Input — a plain `settings` dict (see core.run_search / core module docstring):

    settings = {
        "roles":        ["Data Analyst", ...],   # target role keywords
        "level_codes":  ["1", "2"],              # LinkedIn f_E codes (multi)
        "countries":    ["India", "United States"],  # multi-select
        "country":      "India",                 # legacy scalar; countries[0]
        "queries":      ["Bengaluru, India", ...],  # location query strings
        "sources":      {"linkedin", "careers", ...},  # config.SOURCES keys
        "career_companies": ["Figma", ...],      # optional
        "workday_urls":     [("Nvidia", "https://nvidia.wd5...")],  # optional
    }

    # `countries` is the multi-select list; `country` is kept equal to
    # countries[0] for backward compatibility. core.run_search reads
    # `countries` (falling back to `country`) and runs the aggregators once
    # per country.

Output — `core.run_search(settings)` returns:

    {
        "linkedin": [job, ...],   # each link already validated (HTTP 200)
        "career":   [job, ...],   # each job dict carries "role_type"
        "other":    [job, ...],
        "dropped_salary": int,
        "dropped_senior": int,
        "dropped_ineligible": int,
        "total": int,
    }

A job dict has the normalized shape documented in the README (company, title,
job_id, job_link, location, salary, salary_lpa, remote, match_score,
role_type, hiring_team_link).

WRITING RESULTS
---------------
Reuse the existing writers so output stays identical across front-ends:
  * excelio.write_workbook(linkedin, career, other, fetched_at)
  * tracker.merge({"LinkedIn": ..., "Career Pages": ..., "All Sources": ...},
                  fetched_at)

FRONT-ENDS BUILT ON THIS CONTRACT
---------------------------------
# GUI (built): gui.py is a Tkinter window that collects `settings`, calls
#              core.run_search(...) on a worker thread (narration streamed via
#              utils.set_output_sink), shows results in a table, and lets the
#              user Approve & Save (excelio + tracker) or Discard — the GUI
#              equivalent of jobfinder.qc_preview. Launch: python gui.py or
#              python jobfinder.py --gui.
# Headless (built): jobfinder.run_headless(config_path) reads a saved settings
#              JSON (settings.sample.json), runs the pipeline with no callbacks,
#              skips the QC gate, merges only genuinely-new jobs, writes a
#              "what's new" digest, and fires a Windows toast (notify.py).
#              Launch: python jobfinder.py --headless --config settings.json.

CLAUDE AGENT — CV TAILORING + PREPARE/APPLY (BUILT)
---------------------------------------------------
# The deferred "Claude agent" is now built as a set of small modules, all
# config-gated and degrading gracefully (missing SDK / key / CV / MS Word / profile
# each produce one clear line, never a crash). Never fabricates: Claude may only
# rephrase/reprioritise real CV content, enforced by prompt AND a mechanical guard.
#
#   apply.py      — orchestrator. `process_jobs(jobs, confirm_fn, submit_confirm_fn,
#                   allow_submit)` tailors each eligible job, auto-submits the
#                   supported subset (after a batch confirmation), and records the
#                   outcome. `prepare_job(...)` stages one application folder
#                   (tailored CV, cover_note.txt, job_details.txt) and opens the
#                   posting. `select_from_tracker(...)` picks eligible, not-yet-
#                   actioned rows. Standalone: `python apply.py [--all] [--top N]
#                   [--job-id ID] [--prepare-only]`.
#   cvtailor.py   — Claude client (key via config.get_api_key(); env var
#                   ANTHROPIC_API_KEY, never printed) + tailor() returning
#                   {summary, bullets, skills_order, cover_note, notes} and a
#                   fabrication guard flagging output tokens not on the master CV.
#   cvdoc.py      — reads cv/master_cv.docx, writes a tailored copy IN PLACE (fonts
#                   preserved), converts to PDF via MS Word (docx2pdf); degrades to
#                   .docx if Word/docx2pdf is unavailable.
#   jdfit_claude.py — Step 1 analyzer: analyze_with_claude(jd, title, cv_struct,
#                   client) returns the SAME shape as jdfit.analyze; automatic
#                   fallback to jdfit on any failure. Wired in core via
#                   config.ENABLE_CLAUDE_JD_ANALYSIS (OFF by default).
#   applicant.py + autosubmit.py — auto-submit only where a tenant's public
#                   application form has NO captcha/Cloudflare/login (Greenhouse /
#                   Lever); a REAL POST is verified to confirm receipt before
#                   tracker.mark_applied. Any doubt (captcha, unanswerable required
#                   question, non-2xx, no confirmation) -> falls back to prepare;
#                   screening answers come only from secrets/applicant_profile.json,
#                   never guessed. Coverage is deliberately narrow and reported
#                   honestly (submitted vs prepared).
#   Front-ends: gui.py "Prepare / apply (eligible)" button (two confirmations on
#               the main thread, work on a worker thread) + `python apply.py`.
#   Write-back: tracker.mark_prepared (QC status=Prepared) / tracker.mark_applied
#               (Applied?=Yes, QC status=Auto-applied) — Applied recorded only on a
#               verified real submission, never speculatively.
#
# JD ELIGIBILITY PIPELINE (live; rule-based by default, Claude when enabled):
#   core.run_search fetches each job's description via `jdfetch.get_jd(job)` and
#   judges eligibility via an analyzer chosen in `core._make_analyzer()`:
#   `jdfit.analyze(jd, title)` (offline, default) or, when
#   config.ENABLE_CLAUDE_JD_ANALYSIS is on, `jdfit_claude.analyze_with_claude(...)`
#   with the client + master CV loaded once per run. Both return the identical
#   {verdict, reason, fit_score, req_years} shape, so the drop logic and writers
#   are untouched, and the Claude path auto-falls-back to jdfit on any error.
#
# DEFERRED — Naukri / Indeed / Wellfound / Glassdoor: these are gated by
#   Cloudflare / reCAPTCHA tied to a browser's TLS + JS fingerprint, so a plain
#   requests call can't get past them (a pasted cookie doesn't help). Adding them
#   needs a fingerprinted headless browser (Playwright/undetected-chromedriver) —
#   fragile and account-risky — so it's its own careful sub-project. The seam is
#   the same: such a source becomes another sources/*.py returning the normalized
#   job dict, wired into core.run_search behind its config.SOURCES flag.
"""

# No runtime code yet — importing this module has no side effects.
# When the GUI/agent is built, it will `import core` and use the contract above.
