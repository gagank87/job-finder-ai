# Job Finder — AI-Powered Job Discovery & Fit-Screening

An end-to-end Python automation that collects **real** job postings from seven
source families, reads each job description, judges whether a candidate is
actually eligible, and keeps one de-duplicated tracker across every run —
with an optional Claude layer that tailors the CV per posting and, where a
tenant genuinely allows it, submits the application.

**~9,200 lines across 38 modules.** Four front-ends (CLI, Tkinter GUI, browser
UI, unattended/scheduled) over one shared pipeline — the browser one is
multi-user, with accounts and per-user data isolation.

> **Design principle that shaped every decision:** never fabricate. No guessed
> URLs, no padded result rows, no invented CV content, no "Applied" status
> without a verified confirmation. Where a source is blocked, the tool says so
> and skips it. See [Honesty guarantees](#honesty-guarantees) — they're enforced
> in code, not just documented.

---

## The problem

Job hunting at volume is a data problem wearing a motivation costume. The real
work is: find postings that actually exist, filter out the ones you're not
eligible for, avoid re-reading the same job twice across five sites, and tailor
your CV for the ones worth applying to. Job boards actively make the first part
hard, and a posting titled "Data Analyst" routinely demands "5+ years, MBA
required" in its body.

## What it does

```
sources ──▶ validate links ──▶ salary filter ──▶ seniority filter
   │              (live HTTP 200)                        │
   │                                                     ▼
   │                                         global cross-source dedupe
   │                                                     │
   │                                                     ▼
   │                                    fetch each job description (JD)
   │                                                     │
   │                                                     ▼
   │                              eligibility analysis  ─── rule-based (offline, free)
   │                                                     └── or Claude (config-gated)
   │                                                     │
   ▼                                                     ▼
Excel workbook  ◀──  human QC gate  ◀────────  ranked by fit score
   +                                                     │
master tracker                                           ▼
(dedup across runs,                       optional: tailor CV + cover note
 preserves your edits)                              per posting (Claude)
                                                         │
                                                         ▼
                                       auto-submit where genuinely possible,
                                       else prepare-to-apply for a human
```

### 1. Discovery — seven source families

| Source | Mechanism | Credential |
|---|---|---|
| **LinkedIn** | public guest API, last 24h, multi experience-level | none |
| **Company career pages** | ATS auto-detect (Greenhouse, Lever, Ashby, SmartRecruiters), own-domain ATS sniff, `schema.org/JobPosting` JSON-LD fallback | none |
| **Workday** | per-tenant CXS API via pasted tenant URL | none |
| **Remote boards** | RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy, WorkingNomads, Jobspresso | none |
| **India boards** | Instahyre, Unstop | none |
| **JSearch** | Indeed / Glassdoor / ZipRecruiter / Monster / Google Jobs via one legitimate aggregator API | free RapidAPI key |
| **Headless-scraped** | SimplyHired, Naukri, Wellfound — real Chrome via Selenium for JS-rendered portals | none |
| *LinkedIn hiring-team link* | real "Meet the hiring team" profile URL from the authenticated page | `li_at` cookie |

The major portals that block automation are reached the **legitimate** way —
through the JSearch aggregator API rather than by defeating their CAPTCHAs.
The headless-Chrome path is reserved for portals that merely render with
JavaScript, and it **degrades honestly**: if Chrome or Selenium is missing,
`available()` returns `(False, reason)` and the source is skipped with a
one-line explanation rather than crashing or faking rows.

### 2. Screening — does the candidate actually qualify?

Title-only matching is near-useless, so the tool fetches the **full job
description** and judges three signals against the CV facts in
[`cvprofile.py`](cvprofile.py):

- **Experience** — minimum years the JD demands. `≥ 3` with no fresher signal
  drops the job; 1–2 years is *kept* but flagged *Unlikely*.
- **Education** — a hard-required PhD blocks; Bachelor's / Master's-equivalent
  are satisfied.
- **Skills (deliberately lenient)** — a job drops on skills *only* for a clear
  cross-domain mismatch (e.g. a Java/Spring/Kubernetes backend role). A merely
  missing BI tool never drops a job.

Every surviving job is tagged **Eligibility** (Eligible / Likely / Unlikely /
Unknown), **Fit reason** (plain-language), and **Fit score** (0–100).

**Two interchangeable analyzers behind one signature.** `jdfit.analyze()` is
rule-based, offline, and free — it needs no API key and is the default.
[`jdfit_claude.py`](jdfit_claude.py) is a drop-in replacement that returns the
exact same dict shape, so enabling it changes nothing downstream:

```python
# config.py
ENABLE_CLAUDE_JD_ANALYSIS = False   # True + a key -> Claude does the judging
```

The Claude path **falls back automatically** to the rule-based result if the
client is absent, the request errors, or the reply won't parse — a run never
breaks and never blocks on an API.

### 3. Tailoring — Claude, with a mechanical anti-fabrication guard

[`cvtailor.py`](cvtailor.py) asks Claude to rewrite the professional summary,
reorder existing experience bullets, and draft a cover note in the tone of a
reference letter you supply. The hard rule — **never invent experience, skills,
employers, dates, tools, or metrics** — is enforced in two layers:

1. the prompt forbids it and instructs rewrite/reorder only from CV content;
2. `_verify_no_fabrication()` **mechanically checks the output back against the
   source CV** and raises a flag if new claims appear.

A flagged result doesn't get quietly submitted — the job is prepared with a
visible warning for human review. [`cvdoc.py`](cvdoc.py) then fills a *copy* of
the master `.docx` in place, so original fonts and layout survive, and converts
to PDF.

### 4. Applying — real submission, narrowly scoped

[`autosubmit.py`](autosubmit.py) performs genuine multipart POSTs to
Greenhouse and Lever public application endpoints and **verifies the response
confirms receipt**. Its contract is deliberately narrow:

```python
capability(job)  ->  "greenhouse" | "lever" | None
```

`None` means "no known unauthenticated, non-CAPTCHA submit path exists" — and
the caller prepares the application for a human instead. A row is marked
**Applied** only after a verified confirmation comes back. Auto-submit disables
itself entirely if the applicant profile is missing or has blank essentials,
rather than submitting placeholder data.

### 5. Multi-provider resilience

[`llm.py`](llm.py) exposes one `complete()` call that walks a configured chain
of providers and returns the first success — Claude (direct Anthropic API *or*
Amazon Bedrock, auto-selected by which credential is present), then free
fallbacks. A rate-limited or out-of-quota primary degrades to the next provider
instead of failing the job.

---

## Quickstart

```bash
pip install -r requirements.txt
```

```bash
python jobfinder.py                                    # interactive CLI
python jobfinder.py --gui                              # Tkinter GUI
python webapp.py                                       # browser UI on 127.0.0.1:8000
python jobfinder.py --headless --config settings.json  # unattended
```

The browser UI needs `fastapi` and `uvicorn`; nothing else does. It binds to
localhost by default; `--host 0.0.0.0` exposes it on your network or behind a
tunnel. It does authenticate, and each account sees only its own data — but
**registration is open**, so anyone who can reach the port can create an account.
Put it behind a tunnel you control, not on a public address.

Everything runs with **zero credentials**: the rule-based analyzer, five of the
seven source families, Excel output, and the tracker all work out of the box.
Keys only unlock optional extras.

<details>
<summary><strong>Optional credentials — all read from the environment, never stored in the repo</strong></summary>

| Variable | Unlocks |
|---|---|
| `ANTHROPIC_API_KEY` | Claude JD analyzer, CV tailoring, cover-note drafting |
| `AWS_BEARER_TOKEN_BEDROCK` | same, routed via Amazon Bedrock instead |
| `JSEARCH_API_KEY` | the Indeed / Glassdoor / Monster / Google Jobs source |

In the browser UI, each user enters their own keys instead; they are stored in
that user's gitignored folder and used only for their own requests.

For auto-submission, copy
[`applicant_profile.sample.json`](applicant_profile.sample.json) to
`secrets/applicant_profile.json` and fill in real details. That path is
gitignored. Without it, the tool prepares applications instead of submitting
them.

For CV tailoring, supply `cv/master_cv.docx` and (optionally)
`cv/cover_reference.txt` — both gitignored. See [`cv/README.txt`](cv/README.txt).
</details>

<details>
<summary><strong>Schedule a daily run (Windows Task Scheduler)</strong></summary>

```bash
schtasks /Create /TN "JobFinder Daily" /SC DAILY /ST 09:00 /TR "cmd /c cd /d \"C:\path\to\job-finder-ai\" && python jobfinder.py --headless --config settings.json"
```

Headless mode takes no prompts and skips the QC gate. It merges only
genuinely-new jobs, writes `output/whats_new_<date>.xlsx` as that day's digest,
and fires a Windows notification. Copy
[`settings.sample.json`](settings.sample.json) to `settings.json` first — it
documents every field inline. Companies that can't be auto-resolved are skipped
honestly, never prompted for and never faked.
</details>

---

## Engineering notes

**One pipeline, four front-ends.** The CLI, Tkinter GUI, browser UI and headless
mode only gather settings and handle their own review/write step. All actual work
lives in `core.run_search(settings)`, which does no console I/O — narration flows
through an injectable output sink in `utils`, which the GUI captures to stream
progress into its log panel while the search runs on a worker thread.
[`interface.py`](interface.py) documents the settings/results contract.

Adding the browser UI touched no pipeline module, which is the payoff for that
seam: `webapp.py` gathers a form into the same settings dict the CLI builds and
calls the same `run_search`.

**Multi-user without rewriting a single-tenant pipeline.** Every module reads its
paths and secrets from `config.*` attributes *at call time* — which is
single-tenant by design. Rather than thread a user object through 38 modules,
[`usercontext.py`](usercontext.py) redirects those attributes to the calling
user's own folder for the duration of a request and restores them afterwards:

```python
with usercontext.active_user(user_id):   # config paths -> data/users/<id>/
    results = core.run_search(settings)  # the pipeline never learns about users
```

The same late-binding trick makes the CV profile per-user.
[`profileio.py`](profileio.py) builds a `profile.json` from whoever's CV is
supplied and rebinds the `cvprofile` globals; because consumers read
`cvprofile.YEARS_EXPERIENCE` at call time rather than importing the value, the
override reaches `match_score`, `jdfit` and `jdfit_claude` without touching any
of them.

The honest limitation: the redirect mutates process-global state, so it is *not*
thread-parallel across users. Searches are serialized under one lock — correct
for a handful of people, and a real refactor before it is anything more.

**Each user brings their own API keys.** A shared server env var would leak one
person's key to everyone, so when a per-user override is active the key getters
resolve **only** from that user's stored keys — never falling back to the process
environment or on-disk files. A provider with no key is skipped honestly. Keys
are returned to the browser as presence booleans only, never as values.

**Passwords and personal data.** [`accounts.py`](accounts.py) stores only a
PBKDF2-HMAC-SHA256 hash with a per-user random salt (240k iterations, stdlib
only) and compares in constant time; login tokens are cryptographically random,
and the cookie is `httponly` + `samesite=lax`. The uploaded CV is **never
persisted** — the profile facts are extracted in memory and the file deleted
immediately, so what remains on disk is roles/skills/years/education and no
name, e-mail or phone. Every data endpoint requires login, takes no filesystem
path, and ownership-checks the job it is asked about.

**Every source returns one normalized dict**, so filters, writers, and the
tracker never learn about source-specific shapes:

```python
{
  "source", "company", "title", "job_id", "job_link", "location",
  "salary", "salary_lpa", "remote", "match_score", "hiring_team_link",
  "role_type",      # Internship / Apprenticeship / Trainee / Full-time
  "job_description",
  "eligibility",    # Eligible / Likely / Unlikely / Ineligible / Unknown
  "fit_reason", "fit_score",
}
```

**Never lose a run.** Results are cached to `output/.cache/last_run.json`
*before* the QC gate, so an exit, mis-type, or dead terminal doesn't waste a
multi-minute fetch — the next launch offers "Resume from your last fetch?" and
jumps straight back to review. The gate itself demands an unambiguous `y`/`n`:
a stray key or pasted URL **reprompts** rather than counting as "no" and
discarding everything. The cache auto-deletes the moment a run resolves either
way.

**De-duplication at two levels.** `utils.dedupe` keys on `job_id` within a
source bucket; [`dedupe.py`](dedupe.py) then collapses *cross-source*
duplicates on normalized company+title (the same job from LinkedIn and JSearch
arrives with different IDs), keeping the richest copy — and pre-filters jobs
already in the tracker, so scheduled runs don't re-spend LLM tokens on postings
already seen.

**The tracker preserves your work.** `master_tracker.xlsx` is one growing
de-duplicated sheet. It never overwrites existing rows, so manual edits to
*Applied? / QC status / Notes* survive every run; only genuinely-new jobs are
appended with a first-seen date. Headers are read dynamically, so a tracker
written by an older schema migrates cleanly rather than breaking.

<details>
<summary><strong>Module map</strong></summary>

```
jobfinder.py     CLI + headless entry: menus, QC gate, --headless/--gui dispatch (thin shell)
gui.py           Tkinter front-end: settings form -> threaded search -> sortable results table
webapp.py        FastAPI front-end: same settings dict, same run_search; web/index.html is the UI
accounts.py      SQLite users + login sessions; PBKDF2 hashes, random tokens (no personal data)
usercontext.py   redirects config paths/secrets/profile to one user's folder for a request
profileio.py     build/save/load profile.json from a CV; overrides the cvprofile.py defaults
core.py          run_search(settings) — the settings-driven pipeline (no console I/O)
interface.py     documented contract/seam shared by CLI, GUI, headless, agent
config.py        sources, salary floor, FX rates, feature switches, cache paths   <- tweak here
cvprofile.py     target roles, skills, CV facts, match_score / role_type          <- tweak here

jdfetch.py       get_jd(job) -> JD plain text, per source; never raises
jdfit.py         rule-based eligibility analyzer (offline, free) — the swappable seam
jdfit_claude.py  Claude analyzer, same signature, auto-falls-back to jdfit
llm.py           one complete() call, multi-provider chain with automatic fallover

cvdoc.py         parse master CV .docx; write tailored copy -> PDF (layout preserved)
cvtailor.py      Claude CV tailoring + cover note, with anti-fabrication verification
applicant.py     load + validate the gitignored applicant profile
apply.py         per-job: tailor -> auto-submit where possible, else prepare-to-apply
autosubmit.py    real verified POST to Greenhouse / Lever; None = no honest path

dedupe.py        cross-source dedupe + already-tracked pre-filter
tracker.py       growing de-duplicated master tracker; preserves manual edits
excelio.py       per-run 3-sheet workbook + whats_new digest writer
runcache.py      pre-QC fetch cache: save/load/clear (resume + auto-delete)
salary.py        salary normalization to INR LPA + the floor filter
menus.py         numbered pickers: pick_one / pick_many / ask_yes_no_strict
cookies.py       optional cookie injection + li_at detection
notify.py        Windows toast via PowerShell NotifyIcon; no pip dependency
utils.py         HTTP session, narration sink, link validation, dedupe
sources/
  linkedin.py    guest API (multi experience-level) + cookie-gated hiring-team link
  careers.py     ATS auto-detect + URL fallback + own-domain sniff + JSON-LD
  workday.py     per-tenant CXS API
  aggregators.py seven remote boards
  indiaboards.py Instahyre / Unstop
  jsearch.py     major portals via the JSearch aggregator API
  headless.py    shared Selenium/Chrome framework; degrades honestly
  scraped.py     SimplyHired / Naukri / Wellfound via headless.py
```

**Extending it.** Add an ATS with a `_fetch_<name>` in `sources/careers.py`
registered in `_ATS_FETCHERS` — though the JSON-LD sniff often covers a custom
career site with no code at all. Add an aggregator via an adapter in
`sources/aggregators.py` registered in `_ADAPTERS`. Any new source just returns
the normalized dict behind its `config.SOURCES` flag.
</details>

---

## Honesty guarantees

These are enforced in code and verified; they are the point of the project.

1. **No fabricated links, ever.** Every URL is either echoed from a source's
   API/HTML or built from a **real parsed job ID** — never templated from a
   guessed slug.
2. **Every link validated live (HTTP 200)** before it reaches Excel. Dead links
   are dropped (`utils.validate_links`).
3. **No duplicates**, within or across runs.
4. **Blocked sources are reported, not padded.** If a source returns nothing,
   the tool says so.
5. **Only positive evidence drops a job.** A JD that can't be fetched → *Unknown*
   → **kept**. Over the fetch cap → *Unknown* → **kept**, cap narrated.
6. **No invented CV content**, checked mechanically, not just prompted for.
7. **No unverified "Applied" status**, and no submission with placeholder data.
8. **Nothing written without human approval** in interactive modes.

## Verification

The eligibility gate was verified end-to-end against the live network:

- **Unit tests** — `jdfit.analyze` returns the correct verdict on 8 crafted JDs:
  `5+ yrs / MBA` → Ineligible · `freshers welcome` → Eligible · `2–3 yrs` →
  Unlikely (kept) · `Java/Spring/Kubernetes` → Ineligible (cross-domain) ·
  `Bachelor's + Tableau` → Eligible · empty → Unknown (kept) · `PhD required` →
  Ineligible.
- **JD retrieval (live)** — real text from LinkedIn (~2.3 KB) and Greenhouse
  (~4.6 KB); inline reuse for aggregators; graceful `""` on an unreachable link.
- **Full pipeline (live)** — an entry-level "Data Analyst / India" run fetched
  37 jobs → 1 dead link dropped → salary filter → 1 senior title dropped → JD
  analysis on all 35 survivors, every one tagged (14 Eligible, 18 Likely,
  3 Unlikely). LinkedIn 429 rate-limiting handled gracefully.
- **Cross-run dedupe** — a second run over the same criteria added **0 duplicate
  rows**.
- **Schema migration** — a 60-row tracker on the older schema upgraded cleanly:
  header extended, old rows kept their *Applied?/Notes* edits with blank new
  cells.
- **Off switch** — `ENABLE_JD_ANALYSIS = False` skips the stage entirely,
  reproducing the prior behaviour exactly.

## Known limits

Stated plainly, because pretending otherwise would defeat the point.

- **Cloudflare/reCAPTCHA-gated portals** bind their challenge to a browser's TLS
  + JS fingerprint. A plain request — even with a pasted cookie — cannot pass.
  Those portals are covered through the JSearch API instead; defeating the gates
  directly is deliberately not attempted.
- **Fully JS-rendered career sites** that ship no JSON-LD in their initial HTML
  yield nothing from the requests path and are skipped honestly.
- **Reading JDs costs one polite HTTP fetch** per job for sources that don't
  carry the description inline, so large runs are slow. `JD_FETCH_MAX` caps it;
  overflow is kept as *Unknown*, never silently dropped.
- **`docx2pdf` needs Microsoft Word** (Windows). Without it, tailoring still
  produces the `.docx`.
- **Auto-submit covers Greenhouse and Lever only.** Everything else is
  prepare-to-apply by design.
- **Email/SMTP digest deliberately not built** — it would mean storing mail
  credentials. Headless mode writes a file digest plus a Windows notification.
- **The multi-user web mode runs one search at a time.** The per-user redirect
  and the narration sink are process-global, so requests serialize under a lock.
  Fine for a handful of users; genuine concurrency needs the pipeline to take an
  explicit context instead of reading module globals.

## Tech

Python 3.13 · `requests` · `beautifulsoup4` · `openpyxl` · `selenium` ·
`fastapi` · `uvicorn` · `sqlite3` · `hashlib`/`hmac` (PBKDF2) · `anthropic` ·
`python-docx` · Tkinter · Claude (Anthropic API / Amazon Bedrock)

## Author

**Gagan Khanna** — analytics & product. See [my other projects](https://github.com/gagank87).

## License

[MIT](LICENSE)
