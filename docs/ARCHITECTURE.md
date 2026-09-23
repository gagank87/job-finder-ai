# Job Finder — Architecture & Contributor Guide

A working reference for anyone (human or AI assistant) picking up this codebase.
Read this before making changes, then read the code it points at. The
non-negotiable rules in §3 are the design, not preferences — preserve them.

See [CHANGELOG.md](../CHANGELOG.md) for version history and
[README.md](../README.md) for what the tool does and why.

---

## 1. Candidate configuration

The tool is candidate-agnostic: everything personal lives in two places, and
nothing personal is committed.

**`cvprofile.py`** — the single source of truth for "what fits this CV":
- `DEFAULT_ROLES` — target role keywords; drive the default searches.
- `CV_SKILLS` / `FOREIGN_DOMAIN_SKILLS` — vocabulary for the match score and the
  lenient cross-domain drop test.
- **CV facts** used by the eligibility analyzer: `YEARS_EXPERIENCE`,
  `HAS_INTERNSHIP`, `HAS_BACHELORS`, `HAS_MASTERS_EQUIV`, `HAS_PHD`.

The skill check is deliberately **lenient**: drop only on a clear cross-domain
mismatch (e.g. heavy backend engineering), never on a missing BI tool.

**Gitignored personal files** — `cv/master_cv.docx`, `cv/cover_reference.txt`,
`secrets/applicant_profile.json`, and all credentials. See `cv/README.txt` and
`applicant_profile.sample.json` for the expected shapes. Credentials are read
from environment variables; none is ever stored in the repo or printed.

## 2. What the tool does

A Python tool with **four front-ends over one pipeline** (`core.run_search`), on Windows, Python 3.13:
- **Interactive CLI** — `python jobfinder.py` (menu-driven).
- **GUI** — `python gui.py` or `python jobfinder.py --gui` (Tkinter).
- **Browser** — `python webapp.py` (FastAPI, multi-user; see §4a).
- **Headless/scheduled** — `python jobfinder.py --headless --config settings.json` (no prompts, no QC gate).

All four fetch **real** job postings from multiple sources, filter them, check eligibility against the CV, and write them to Excel.

CLI run flow: greet → (optional cookie load) → **offer to resume a cached interrupted fetch** → pick roles → **multi-select experience levels** → **multi-select countries** + cities per country → multi-select sources → (career/Workday company inputs gathered up front) → `core.run_search(settings)` does: fetch each source (live narration) → validate every link (HTTP 200) → salary filter → seniority filter (junior-only searches) → **JD eligibility analysis** → tag Role type → return results → **cache results to disk** → QC gate (**strict y/n**, top 5 by fit) → on approval, write per-run Excel workbook AND merge new jobs into the growing master tracker → **clear cache** (also cleared on reject).

## 3. NON-NEGOTIABLE RULES

1. **No dummy, fake, fabricated, or guessed links — EVER.** Every job link must be either (a) echoed directly from a source's API/HTML response, or (b) built from a **real parsed job ID**. Never template a URL from a guessed slug. If unsure a link is real, validate it or drop it.
2. **Every link is validated live (HTTP 200)** before it is written to Excel. Dead links are dropped. Implemented in `utils.validate_links()`.
3. **No duplicate links**, within a run or across runs. The master tracker dedupes by `source+job_id` AND by normalized link. Verified working (zero dupes).
4. **Be honest about limitations.** If a source blocks automated access, SKIP it and say so — never pad results with fake rows or pretend a blocked source worked. **This extends to the JD eligibility gate: a job whose JD can't be fetched is NEVER dropped — it's kept with an "Unknown" verdict. Only positive evidence of ineligibility drops a job.**
5. **The tool must narrate what it's doing** (conversational stdout) so the user can follow along. It must stay modular so it's easy to extend.

## 4. Current architecture (all implemented and tested)

```
jobfinder.py          # CLI + headless entry: menus, QC gate, resume, --headless/--gui/--config dispatch (THIN shell)
gui.py                # Tkinter front-end: settings form -> threaded core.run_search -> results table QC gate -> approve/discard
webapp.py             # FastAPI front-end: same settings dict, same run_search; auth-gated, one search at a time
accounts.py           # SQLite users + login sessions; PBKDF2-HMAC-SHA256 + per-user salt; no personal data
usercontext.py        # active_user(id) context manager: redirects config paths/secrets/profile to data/users/<id>/
profileio.py          # build/save/load profile.json from a CV; rebinds the cvprofile globals (per-user CV facts)
core.py               # run_search(settings, ask_url_fn) — the settings-driven pipeline (no console I/O)
runcache.py           # save/load/clear the pre-QC fetch cache (resume + auto-delete); pure, never raises
notify.py             # Windows toast via PowerShell NotifyIcon (headless digest); no pip dependency; never raises
jdfetch.py            # get_jd(job) -> JD plain text, per source; never raises, "" if unavailable
jdfit.py              # analyze(jd_text, title) -> verdict/reason/fit_score/req_years (rule-based; SWAPPABLE seam)
jdfit_claude.py       # v6: analyze_with_claude(jd, title, cv) — same dict shape; auto-falls-back to jdfit
llm.py                # v6: complete(system, user) over a provider chain (Anthropic direct | Bedrock | free fallbacks)
cvdoc.py              # v6: load_master() parses master_cv.docx; build_tailored_pdf() fills a COPY -> PDF (layout kept)
cvtailor.py           # v6: Claude CV tailoring + cover note; _verify_no_fabrication() mechanically diffs vs source CV
applicant.py          # v6: load()+validate the gitignored applicant profile; blank essentials DISABLE auto-submit
apply.py              # v6: process_jobs() — tailor, then auto-submit where possible else prepare-to-apply
autosubmit.py         # v6: capability(job) -> "greenhouse"|"lever"|None; submit() does a REAL verified multipart POST
dedupe.py             # v6: cross-source dedupe (normalized company+title) + already-tracked pre-filter
settingsio.py         # settings JSON load/save helpers
interface.py          # documented contract/seam shared by the CLI, GUI, headless mode and the apply pipeline
config.py             # sources, salary floor, FX rates, JD switches, countries, CACHE paths, delays, timeouts — TWEAK HERE
cvprofile.py          # target roles + skills + CV FACTS + match_score()/role_type()/is_senior_title()/build_search_terms() — TWEAK HERE
menus.py              # numbered menu pickers: pick_one() / pick_many() / ask_text() / ask_yes_no() / ask_yes_no_strict()
salary.py             # salary normalization to INR LPA + the >=8 LPA filter
cookies.py            # optional cookie injection (cookies.json) + has_linkedin_auth() (li_at detection)
tracker.py            # single growing, de-duplicated master_tracker.xlsx; preserves user edits; merge() returns new jobs
excelio.py            # per-run 3-sheet workbook writer + write_whats_new() digest
utils.py              # shared HTTP session, narration (say/step/ok/warn/info + injectable output sink), link validation, dedupe
settings.sample.json  # headless config template (copy to settings.json); self-documenting via _*_help keys
requirements.txt      # requests, beautifulsoup4, openpyxl + optional: selenium, anthropic, python-docx, docx2pdf
output/               # per-run xlsx files land here; output/.cache/last_run.json is the resume cache (gitignored)
secrets/              # gitignored: credentials + the real applicant_profile.json
data/                 # gitignored: app.db (accounts) + users/<id>/ (per-user profile, settings, tracker, keys, output)
web/index.html        # the browser UI, served by webapp.py; no build step, no framework
sources/
  linkedin.py         # LinkedIn public guest API; fetch(search_terms, cities, exp_codes); + cookie-gated hiring-team link enrichment
  careers.py          # ATS auto-detect (Greenhouse, Lever, Ashby, SmartRecruiters) + pasted-URL fallback + own-domain sniff + JSON-LD JobPosting fallback
  workday.py          # Workday per-tenant CXS API (via pasted company Workday URL)
  aggregators.py      # RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy, WorkingNomads, Jobspresso (most carry job_description inline)
  indiaboards.py      # Instahyre, Unstop
  jsearch.py          # v6: Indeed/Glassdoor/ZipRecruiter/Monster/Google Jobs via the JSearch aggregator API (free key; skips without one)
  headless.py         # v6: shared Selenium/Chrome framework; available() -> (False, reason) degrades honestly
  scraped.py          # v6: SimplyHired / Naukri / Wellfound / Glassdoor adapters over headless.py; per-portal config.SCRAPERS
```

**Scraped jobs skip link re-validation.** These portals 403 a bare `requests`
call, so the pipeline's requests-based validator would give **false negatives**
and dishonestly drop live postings. A job the browser actually loaded (HTTP 200,
not a block page) is marked pre-validated — the live SERP render *is* its
validation. See `core.run_search` step 7.

**Normalized job dict** (every source returns this shape; keep it consistent if you add sources):
```python
{
  "source": str, "company": str, "title": str, "job_id": str, "job_link": str,
  "location": str, "salary": str, "salary_lpa": float|None, "remote": bool,
  "match_score": int,           # 0-100 from cvprofile.match_score() (title-only)
  "hiring_team_link": str,      # LinkedIn only; real /in/ profile URL when li_at cookie present, else "" (never faked)
  "role_type": str,             # v3: "Internship"/"Apprenticeship"/"Trainee"/"Full-time" (set in core)
  "job_description": str,       # v4: JD text set INLINE by sources that expose it (aggregators/Lever); else ""
  "eligibility": str,           # v4: "Eligible"/"Likely"/"Unlikely"/"Ineligible"/"Unknown" (set in core JD stage)
  "fit_reason": str,            # v4: short human explanation
  "fit_score": int,             # v4: 0-100 JD-vs-CV fit
}
```

**`settings` dict passed to `core.run_search`** (also the contract the GUI and headless mode use — see `interface.py`):
```python
{
  "roles": list[str], "level_codes": list[str],   # LinkedIn f_E codes, multi-select
  "countries": list[str],                         # v5: MULTI-select countries
  "country": str,                                 # legacy scalar; kept == countries[0] for back-compat
  "queries": list[str],                           # location query strings ("City, Country" / "Country")
  "sources": set[str],                            # config.SOURCES keys
  "career_companies": list[str],                  # optional (careers source)
  "workday_urls": list[(name, url)],              # optional (workday source)
}
```
`core.run_search` reads `countries` (falling back to `[country]`) and runs the remote aggregators **once per country**, de-duped. LinkedIn loops the `queries`. For headless JSON, `runcache.restore_settings` normalizes `sources` (list→set) and `workday_urls` (lists→tuples); underscore-prefixed keys in settings.sample.json are ignored.

## 4a. Multi-user web mode (`webapp.py` only — the CLI/GUI ignore all of it)

The pipeline reads its paths and secrets from `config.*` **at call time**, which
is single-tenant by design. Instead of threading a user object through 38
modules, `usercontext.active_user(id)` redirects those attributes to the caller's
own folder for the body of a request and restores them in a `finally`:

```
config.MASTER_TRACKER  "master_tracker.xlsx"  ->  "data/users/7/master_tracker.xlsx"
config.PROFILE_FILE    "profile.json"         ->  "data/users/7/profile.json"
config.SECRET_OVERRIDE None                   ->  {"anthropic": "...", ...}
cvprofile globals      process defaults       ->  that user's CV facts
```

Every redirected constant is **relative** in `config.py`, and `_ORIGINALS` is
captured once at import, so a redirect is always computed from the canonical
relative value and never from an already-redirected one. Keep them relative: an
absolute path would make `os.path.join` discard the user folder and silently
collapse all users onto one file.

**Secrets never cross accounts.** When `config.SECRET_OVERRIDE` is a dict, the
key getters resolve *only* from it — a provider missing there yields `None` and
is skipped honestly, rather than falling through to the process environment and
spending the host's key on someone else's search.

**Concurrency.** The redirect mutates process-global state and the narration sink
is process-global, so `search` / `write` / `meta` are serialized under
`_run_lock`. This is a real limit, not an oversight: genuine parallelism requires
`run_search` to take an explicit context instead of reading module globals.

**Privacy invariants.** No endpoint accepts a filesystem path; there is no static
mount of the repo or `data/`; jobs are ownership-checked before status or write
(`_owned_job` 404s on someone else's id); the uploaded CV is written only to the
user's folder, parsed, and deleted in a `finally`; `profile.json` keeps
roles/skills/years/education and no name, e-mail or phone; API keys go back to
the browser as presence booleans only. `data/` is gitignored in full.

## 5. Sources — what works and what is blocked (all live-tested)

**WORKING (real public JSON/HTML, no login required):**
- **LinkedIn** guest endpoint `.../jobs-guest/jobs/api/seeMoreJobPostings/search` — real IDs, `f_TPR=r86400` (last 24h), `f_E` experience code (looped over multiple codes), pagination via `start`. JD detail via `.../jobs-guest/jobs/api/jobPosting/{id}` (used by `jdfetch`).
- **Greenhouse** `boards-api.greenhouse.io/v1/boards/{token}/jobs` (+ `/jobs/{id}` detail has full JD in `content` field); **Lever** `api.lever.co/v0/postings/{token}?mode=json` (carries `descriptionPlain` inline); **Ashby** `api.ashbyhq.com/posting-api/job-board/{token}`; **SmartRecruiters** `api.smartrecruiters.com/v1/companies/{token}/postings`.
- **Own-domain ATS sniff (v3):** `careers.sniff_ats_from_page(url)` fetches a pasted careers page (e.g. `figma.com/careers`, `notion.so/careers`) and detects an embedded ATS board. Verified: Figma→greenhouse/figma, Notion→ashby/notion. Stripe skips honestly (token only in CSP header, not body).
- **Workday** per-tenant: `POST https://{host}/wday/cxs/{tenant}/{board}/jobs`. Input via pasted URL.
- **RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy, WorkingNomads, Jobspresso** (remote boards; Himalayas/Jobicy expose salary; Remotive/Arbeitnow/Himalayas/Jobicy carry JD inline).
- **Instahyre, Unstop** (India boards).
- **JSearch (v6)** `jsearch.p.rapidapi.com` — a legitimate aggregator returning real, currently-listed jobs from **Indeed, Glassdoor, ZipRecruiter, Monster, Google Jobs, LinkedIn** with canonical apply links. Needs a free RapidAPI key (`JSEARCH_API_KEY` env var or `secrets/jsearch_key.txt`); the source skips itself when no key is configured. **This is how the major portals are covered — via an API, not by defeating their gates.**
- **Headless-scraped (v6)** `sources/scraped.py` over the shared Chrome framework in `sources/headless.py` — **SimplyHired** (JS-rendered SERP that 403s plain HTTP), **Naukri** (server-rendered cards, no login/CAPTCHA), **Wellfound**. Enabled per-portal in `config.SCRAPERS`. Selenium Manager fetches the matching chromedriver on first use, so there is no manual driver install. `headless.available()` returns `(False, reason)` when Selenium or a usable Chrome is missing, and callers **skip with a one-line explanation** — never crash, never fabricate.

**STILL BLOCKED — do NOT attempt via plain requests; do NOT fake (tested, confirmed):**
- Dice / Glassdoor direct (Cloudflare JS 403), Y Combinator / workatstartup (Algolia key not exposed), Handshake (login-only), TimesJobs / Herkey / Hirect / Cutshort / Hasjob / Hirist.tech / sprouts.gallery / startups.gallery / internlist (no usable public endpoint).
- Defeating a Cloudflare/reCAPTCHA challenge bound to a browser TLS+JS fingerprint is **deliberately out of scope** — it is fragile, risks accounts, and yields broken links. Where such a portal matters, reach it through the JSearch API instead. The headless path in `scraped.py` is for portals that merely **render** with JavaScript, not for ones actively challenging automation.

**Cookies:** `cookies.json` (site → cookie string) loaded by `cookies.py`. Realistically helps **LinkedIn only** (`li_at` → more results, sometimes hiring-team link). Does NOT defeat Cloudflare/reCAPTCHA (those bind to a browser TLS+JS fingerprint, not a cookie). Do not claim otherwise.

## 6. Key feature rules (already implemented — preserve this behavior)

- **Standardized menus, no free-typing** for experience / country / sources. Options live in `config.py` (`EXPERIENCE_LEVELS`, `COUNTRIES`, `SOURCES`).
- **Experience is MULTI-SELECT (v3):** one run can span Internship + Entry + Associate; `linkedin.fetch` loops over all selected f_E codes; results are clubbed together. Each job is tagged with a **Role type** column.
- **Broadened early-career search terms (v3):** `cvprofile.build_search_terms(roles, level_codes)` adds `apprenticeship`/`apprentice` terms when Internship is selected, and `management trainee` terms (scoped to marketing/data-analytics) when Entry level is selected. `match_score` recognizes trainee/apprentice/graduate titles ONLY when a marketing/data/analytics domain word is present (so "Management Trainee (Banking Operations)" scores 0).
- **Seniority filter (v3):** `cvprofile.is_senior_title()` drops Senior/Sr/Lead/Principal/Staff/Director/VP/Head/II/III titles — but ONLY when the search is entirely junior (`config.JUNIOR_LEVEL_CODES = {"1","2","3"}`). It PRESERVES "Associate Product Manager" and "Product Manager" (never keys off the bare word "manager"). Verified.
- **JD eligibility gate (v4):** in `core.run_search`, after the salary + seniority filters, for each surviving job (up to `config.JD_FETCH_MAX = 120`): `jd = jdfetch.get_jd(job)` then `res = jdfit.analyze(jd, title)`. Sets `eligibility`/`fit_reason`/`fit_score`; drops only `verdict == "Ineligible"` (positive evidence: min required years >= `JD_DROP_MIN_YEARS = 3` with no fresher signal AND candidate is fresher; OR required PhD; OR clear cross-domain mismatch). Empty/unfetchable JD → "Unknown" → KEPT. Over the fetch cap → "Unknown" → KEPT, cap narrated. Master switch `config.ENABLE_JD_ANALYSIS` (False = exact pre-v4 behavior). **`jdfit.analyze` is a single pure function — a future `analyze_with_claude(jd, title, cv)` can replace it behind the same signature.**
- **Salary filter (hard rule):** keep a job **only if** salary is unknown (None) **OR** normalized annual **≥ 8 LPA** (`config.SALARY_FLOOR_LPA`). Jobs with a stated salary clearly below 8 LPA are dropped, count reported. **Internships/trainee roles are KEPT** — title is never used to infer low pay; only an explicitly stated number drops a job. Foreign currencies convert via fixed rates in `config.FX_TO_INR`. For a range, the MAX is used for the ≥8 test.
- **Excel output:** per-run `output/jobs_<timestamp>.xlsx` with 3 sheets — **LinkedIn** (has Hiring-team link column), **Company Career Pages** (ATS + Workday, collated), **All Sources** (remote + India boards). Columns: Company · Role · Job link · Job ID · [Hiring-team link] · Source · **Role type** · Location · Salary · Remote · CV match score · **Eligibility · Fit reason · Fit score** · First seen · Applied? · QC status · Notes. Job links are clickable hyperlinks. Rows sorted by fit/match desc.
- **Master tracker** `master_tracker.xlsx`: one growing sheet, dedupes across runs, **preserves user's manual edits** to Applied?/QC status/Notes (never overwrites existing rows; only appends genuinely new jobs with a First-seen date). Headers are read dynamically, so an OLD tracker missing the new v3/v4 columns migrates cleanly (old rows get blank new cells, edits preserved).
- **QC gate (STRICT, v5):** top 5 results (by fit_score, else match_score) are shown with eligibility + reason; nothing is written unless the user explicitly approves. `menus.ask_yes_no_strict` loops on any unclear input — a stray key or pasted URL **reprompts**, it never silently counts as "no". (This fixed the original bug that motivated v5: a mis-paste at the gate used to discard a whole multi-minute run.)
- **Run cache + resume + auto-delete (v5):** `core.run_search` results are saved to `output/.cache/last_run.json` (`runcache.save`) right BEFORE the QC gate. On the next launch, `jobfinder.offer_resume()` (and the GUI's resume dialog) offer to jump straight back to the QC gate with the cached jobs — no re-fetch. The cache is **cleared the moment the run resolves either way**: on QC reject (nothing written) AND on successful master-tracker write. `runcache` is pure (no console I/O), never raises, and is schema-versioned (stale caches ignored). The GUI follows identical rules.
- **Multi-country (v5):** `jobfinder.choose_location` returns `(countries, queries)` via `pick_many`; settings carry both `countries` (list) and `country` (=`countries[0]`, back-compat). Aggregators run once per country then dedupe; LinkedIn loops all queries.
- **Wider ATS (v5):** `careers.sniff_jsonld_jobs(url, company, roles)` parses `<script type="application/ld+json">` for `schema.org/JobPosting` objects (handles single / `@graph` / array shapes) and emits jobs **only** when a real title + real apply URL are present (relative URLs resolved via `urljoin`; nothing guessed). Wired into `fetch_company` as a fallback (source label "Company site (JobPosting)") before the honest bail-out. **Fully JS-rendered sites with no JSON-LD in initial HTML (e.g. HCL) still yield nothing — skipped honestly, documented, expected, not a bug.**
- **Hiring-team link (LinkedIn, v5):** `linkedin._enrich_hiring_team` runs only when `cookies.has_linkedin_auth()` (an `li_at` cookie is loaded); it fetches the authenticated `/jobs/view/{id}` page and reads the real "Meet the hiring team" `/in/...` profile href into `hiring_team_link`. No cookie → no fetch, column stays blank. **Never fabricated** (guest API has no recruiter data).
- **Headless / scheduled (v5):** `jobfinder.run_headless(config_path)` loads settings JSON (`_load_settings_json` → `runcache.restore_settings`), runs `core.run_search` with `ask_url_fn=None` (companies that can't auto-resolve are skipped, never prompted), **skips QC**, merges new jobs, writes `output/whats_new_<date>.xlsx` (`excelio.write_whats_new` over `tracker.merge`'s returned new jobs), and fires `notify.notify` (Windows toast). Dispatched by `argparse` in `__main__`. Task Scheduler `schtasks` command documented in README.
- **GUI (v5):** `gui.py` (Tkinter). Collects the same settings, runs `core.run_search` on a worker thread, streams narration via `utils.set_output_sink(queue.put)` drained on the UI thread, shows a sortable `ttk.Treeview` results table (the QC gate), Approve & Save → `excelio.write_workbook` + `tracker.merge` + `runcache.clear`; Discard → `runcache.clear`. Also offers resume from cache on launch.

## 7. Implementation status

Everything described in §4–§6 is implemented. Verified end-to-end against the
live network (see [README.md](../README.md#verification) for the full results):
the eligibility analyzer's unit cases, live JD retrieval, a full 37-job pipeline
run, cross-run dedupe at zero duplicates, and clean schema migration of an older
tracker.

The v6 AI layer (`jdfit_claude.py`, `llm.py`, `cvtailor.py`, `cvdoc.py`,
`apply.py`, `autosubmit.py`) is built and wired, with the Claude JD analyzer
**config-gated off by default** (`config.ENABLE_CLAUDE_JD_ANALYSIS = False`) so
the tool runs fully without any credential. See [CHANGELOG.md](../CHANGELOG.md).

**Performance note.** The JD stage adds one HTTP fetch plus
`utils.polite_sleep()` (~1.5 s) per job for sources that don't carry the
description inline (LinkedIn / Greenhouse / Ashby / SmartRecruiters / India
boards / Workday). A large LinkedIn run takes minutes; `config.JD_FETCH_MAX`
(default 120) caps it, and overflow is kept as *Unknown* rather than dropped. If
it's too slow, lower the cap or reduce `REQUEST_DELAY_SECONDS` — parallelising JD
fetches is possible but must stay polite to the source.

## 8. Sensible next steps

- **Broaden verified auto-submit coverage.** `autosubmit.capability()` currently
  recognises Greenhouse and Lever. Ashby and SmartRecruiters are plausible next
  candidates — but only with a *verified* confirmation response, per §3.
- **Parallelise JD fetching** behind a concurrency cap, to cut the dominant cost
  of a large run without hammering any single host.
- **Extend `config.SCRAPERS`** to further JS-rendered (not challenge-gated)
  portals, following the `sources/scraped.py` adapter pattern.

## 9. Explicitly out of scope (do NOT build unless asked)

- **Defeating Cloudflare / reCAPTCHA fingerprint gates** (Dice, Glassdoor
  direct, YC/workatstartup). Fragile, risks accounts, yields broken links — use
  the JSearch API for those portals instead. Note this is a different thing from
  the headless path in `sources/scraped.py`, which only handles JS *rendering*.
- **Email/SMTP digest** — deliberately not built; it would mean storing mail
  credentials. Headless mode writes a file digest plus a Windows notification.
  Only add if a maintainer explicitly accepts credential handling.
- **Any feature that requires relaxing a §3 rule.** If it can only work by
  guessing a link, padding results, inventing CV content, or reporting an
  unverified "Applied", the answer is no.
## 10. How to run / verify

```bash
pip install -r requirements.txt
python jobfinder.py                                     # CLI
python jobfinder.py --gui                               # GUI (or: python gui.py)
python jobfinder.py --headless --config settings.json   # headless (copy settings.sample.json first)
```
- Menus should appear for roles / experience (multi-select) / **countries (multi-select)** / sources (no free-typed values).
- Confirm the tool reads JDs and narrates the eligibility check; confirm clearly-ineligible jobs (senior-experience-required, wrong-domain) are dropped with a count, while borderline ones are kept and flagged.
- Confirm links open the exact postings; confirm sub-8-LPA stated-salary jobs are dropped while unknown-salary jobs are kept.
- Confirm Role type + Eligibility/Fit reason/Fit score columns are populated in the per-run file and master tracker.
- Run twice with overlapping criteria → confirm the master tracker adds **no duplicate rows** and preserves any Applied?/Notes edits.
- Confirm blocked/unsupported sources are reported, not faked.
- **v5 — strict gate:** at the final Y/n, type junk / paste a URL → it reprompts, does NOT exit. `n` → "nothing written"; `y`/Enter → writes.
- **v5 — cache/resume:** reject at QC → `output/.cache/last_run.json` is deleted. Kill after fetch (or reject) then relaunch → "Resume from your last fetch?" appears and jumps to QC without re-fetching. Approve → cache deleted after the tracker write.
- **v5 — multi-country:** pick India + United States (+ a city each) → LinkedIn searches all queries, aggregators run per country, links validate.
- **v5 — ATS/JSON-LD:** a `schema.org/JobPosting` career page yields real jobs with real apply links; HCL still skips **honestly** (expected).
- **v5 — hiring link:** with a valid `li_at` in `cookies.json`, some LinkedIn rows get a real `/in/...` URL; without it the column is blank and no extra fetches happen.
- **v5 — headless:** run with `settings.sample.json` → no prompts, no QC, merges new jobs, writes `output/whats_new_<date>.xlsx`, fires a toast. Second run same day → 0 dupes.
- **v5 — GUI:** search runs on a thread (window stays responsive), narration streams, table populates, Approve writes + clears cache, Discard clears cache.

## 11. Contributing conventions

- Be direct and honest about what is and isn't technically feasible. Authenticity
  beats impressive-looking but fake output — that is the whole premise here.
- Before adding sources, **actually test** each endpoint with a quick `requests`
  probe rather than assuming. Many boards block automation; report which are real
  versus blocked instead of shipping something that silently returns nothing.
- Keep modules single-purpose and consistent with existing patterns. Any new
  source returns the normalized job dict behind its `config.SOURCES` flag.
- Narrate progress through `utils`' output sink, never `print()` — the GUI
  captures that sink to stream progress.

---

**Reading order:** [README.md](../README.md) → `jobfinder.py` → `core.py` →
`config.py` / `cvprofile.py` → the `sources/` modules → `jdfit.py` /
`jdfetch.py` → `llm.py` / `cvtailor.py` / `autosubmit.py`.
