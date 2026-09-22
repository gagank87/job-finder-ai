# Changelog

Version history and the reasoning behind each step. Earlier entries are kept
verbatim in spirit — including what was deliberately *not* built, and why.

## v6 — AI layer + application pipeline

The seam left open in v4 was filled in.

- **Claude JD analyzer** (`jdfit_claude.py`) — a drop-in replacement for
  `jdfit.analyze()` returning the identical dict shape, so `core.py`'s drop
  logic and every writer stay untouched. Gated by
  `config.ENABLE_CLAUDE_JD_ANALYSIS` (default `False`). Falls back
  automatically to the rule-based result when the client is absent, the request
  errors, or the reply won't parse — a run never breaks on an API.
- **Multi-provider LLM chain** (`llm.py`) — one `complete()` call walks a
  configured provider chain and returns the first success. Claude via the
  direct Anthropic API *or* Amazon Bedrock, auto-selected by which credential is
  present, then free fallbacks. A rate-limited primary degrades instead of
  failing.
- **CV tailoring + cover notes** (`cvtailor.py`, `cvdoc.py`) — rewrites the
  summary, reorders existing bullets, drafts a cover note in the tone of a
  supplied reference. Anti-fabrication is enforced *twice*: the prompt forbids
  invention, and `_verify_no_fabrication()` mechanically diffs the output back
  against the source CV. `cvdoc.build_tailored_pdf()` fills a copy of the master
  `.docx` in place so fonts and layout survive, then converts to PDF.
- **Real application submission** (`autosubmit.py`, `apply.py`, `applicant.py`)
  — genuine verified multipart POSTs to Greenhouse and Lever public endpoints.
  `capability(job)` returns `None` when no unauthenticated, non-CAPTCHA path
  exists, and the caller prepares-to-apply instead. A row is marked *Applied*
  only on a verified confirmation. Auto-submit disables itself when the
  applicant profile is missing or has blank essentials.
- **Two new source families** — `sources/jsearch.py` reaches Indeed / Glassdoor
  / ZipRecruiter / Monster / Google Jobs through one legitimate aggregator API
  (the honest answer to the v3–v5 "blocked portals" problem);
  `sources/headless.py` + `sources/scraped.py` drive a real Chrome via Selenium
  for JS-rendered portals (SimplyHired, Naukri, Wellfound), degrading honestly
  to a skip when Chrome or Selenium is unavailable.
- **Cross-source de-duplication** (`dedupe.py`) — collapses the same posting
  arriving from two sources with different IDs, keyed on normalized
  company+title, keeping the richest copy. Also pre-filters already-tracked jobs
  so scheduled runs don't re-spend LLM tokens on postings already seen.

## v5 — resilience, reach, and two new front-ends

- **Never lose a run to a mis-click.** The QC gate now insists on a clear
  `y`/`n` — a stray key or pasted URL reprompts instead of counting as "no" and
  discarding everything. Every finished fetch is cached to
  `output/.cache/last_run.json` *before* the gate, so the next launch offers
  "Resume from your last fetch?" and jumps straight back to review with no
  re-fetch. The cache auto-deletes the moment a run resolves either way.
- **Multiple countries per run** — LinkedIn searches every location query; the
  remote boards run once per country, de-duplicated.
- **Wider career-page coverage** — reads `schema.org/JobPosting` JSON-LD
  embedded in a company's own careers page, a general fallback that finds real
  postings with real apply links on many custom sites. Handles single / `@graph`
  / array shapes; emits a job only when a real title *and* real apply URL are
  present, with relative URLs resolved via `urljoin`.
- **LinkedIn hiring-team link** — with an `li_at` cookie, reads the real "Meet
  the hiring team" profile URL from the authenticated job page. Without a
  cookie the column stays blank and no extra fetch happens; it is never
  fabricated, because the guest API exposes no recruiter data.
- **Headless / scheduled mode** — no prompts, no QC gate, merges only new jobs,
  writes an `output/whats_new_<date>.xlsx` digest, fires a Windows notification.
- **Tkinter GUI** — same settings, search on a worker thread with live streamed
  narration (via `utils`' injectable output sink), sortable results table as the
  QC gate, Approve & Save / Discard with identical cache rules to the CLI.

Design choices settled here: blocked portals kept deferred rather than faked;
hiring link cookies-only with an honest blank; digest as file + notification
rather than SMTP (no credentials to store).

## v4 — the JD eligibility gate

Earlier versions judged a job by its **title**, so a posting titled "Data
Analyst" that demanded "5+ years, MBA required" in its body still slipped
through. v4 fetches the actual job description and judges eligibility against
the CV facts in `cvprofile.py`.

- Clearly-ineligible jobs are dropped with a reported count.
- Everything else is kept and flagged with **Eligibility**, **Fit reason**, and
  **Fit score** (0–100); results and the QC preview rank by fit.
- **Honesty first:** a JD that can't be fetched is never dropped — it's kept as
  *Unknown*. Only positive evidence of ineligibility drops a job.
- Rule-based and offline (no API key), structured as one swappable
  `jdfit.analyze()` so an LLM analyzer could drop in behind the same signature
  — which is exactly what v6 did.

## v3 — early-career coverage

- **Multi-select experience levels** in one run (Internship + Entry +
  Associate), results combined in the same sheets with a **Role type** tag
  (Internship / Apprenticeship / Trainee / Full-time) to keep them
  distinguishable.
- **Broadened search vocabulary** — internship searches also look for
  *apprenticeship*; entry-level searches also look for *management trainee*,
  scoped to the marketing / data-analytics domain so "Management Trainee
  (Banking Operations)" scores 0.
- **Own-domain ATS sniff** — paste a company's own careers URL and the tool
  detects an embedded ATS board and fetches the real postings. Verified on
  Figma (→ Greenhouse) and Notion (→ Ashby). Stripe skips honestly (its token
  appears only in a CSP header, not the body).
- **Seniority filter** — drops *Senior / Sr / Lead / Principal / Staff /
  Director / VP / Head / II / III* titles, but only when every selected level is
  junior. Never keys off the bare word "manager", so *Associate Product Manager*
  and *Product Manager* are preserved.
