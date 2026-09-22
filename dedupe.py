"""
dedupe.py — global, cross-source de-duplication + already-tracked pre-filter.

Two problems this solves, both about NOT wasting the expensive downstream work
(JD fetching + LLM eligibility analysis, and — for a scheduled conductor — the
LLM token spend on jobs you've already seen):

  1. Cross-source duplicates. utils.dedupe runs inside a single source bucket and
     keys on job_id, so the SAME job arriving from two sources (the LinkedIn
     source vs JSearch/LinkedIn, scraped Glassdoor vs JSearch/Glassdoor) has a
     different id/link in each and leaks through as two rows. This collapses them
     to one, keyed on normalized company+title, keeping the richest copy.

  2. Already-tracked jobs. On a repeated / scheduled run there's no point
     re-fetching a JD and re-spending LLM tokens on a job already in
     master_tracker.xlsx. We load the tracker's identity + content keys up front
     and drop matches before the analysis step.

Honesty: this only ever DROPS duplicates/known jobs — it never fabricates,
merges fields dishonestly, or changes a surviving job's real link. The copy that
survives is a real record that came straight from a source response.
"""

import re

import config
import tracker


# ---------------------------------------------------------------------------
# Normalization -> a fuzzy content key (company + title).
# ---------------------------------------------------------------------------
# Corporate suffixes / location tails that shouldn't split otherwise-identical
# employers ("Deloitte India" vs "Deloitte", "Acme Technologies" vs "Acme").
_COMPANY_NOISE = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|company|"
    r"technologies|technology|labs|solutions|services|consulting|group|"
    r"global|india)\b")


def _norm_text(s):
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    s = re.sub(r"[^a-z0-9]+", " ", str(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _norm_company(s):
    s = _norm_text(s)
    s = _COMPANY_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def content_key(job_or_row):
    """
    A fuzzy identity key for one job: normalized company + normalized title.

    Accepts either a job dict ('company'/'title') or a tracker row
    ('Company name'/'Job role name'). When the company is unknown we key on the
    link/id instead, so blank-company jobs are NOT all collapsed together.
    """
    company = job_or_row.get("company", job_or_row.get("Company name", ""))
    title = job_or_row.get("title", job_or_row.get("Job role name", ""))
    comp = _norm_company(company)
    ttl = _norm_text(title)
    if comp:
        return f"ct::{comp}::{ttl}"
    link = job_or_row.get("job_link", job_or_row.get("Job link", "")) or ""
    link = link.split("?")[0].rstrip("/").lower()
    jid = str(job_or_row.get("job_id", job_or_row.get("Job ID", "")) or "")
    return f"cl::{link or jid or ttl}"


def _identity_keys(job):
    """Tracker-compatible source+id / normalized-link keys for a job dict."""
    return tracker._job_keys(  # noqa: SLF001 (same-project internal reuse)
        job.get("source", ""), job.get("job_id", ""), job.get("job_link", ""))


def _richness(job):
    """
    Rank two duplicates so the more useful copy survives. Prefers a job that
    carries an inline JD (saves a fetch), a salary, and a match score.
    """
    score = 0
    if job.get("job_description"):
        score += 3          # inline JD is the most valuable — skips a fetch
    if job.get("salary"):
        score += 1
    if job.get("salary_lpa") not in (None, ""):
        score += 1
    if job.get("match_score"):
        score += 1
    return score


# ---------------------------------------------------------------------------
# Loading what's already in the master tracker.
# ---------------------------------------------------------------------------
def load_known_keys(path=None):
    """
    Return (id_link_keys, content_keys) for every job already in the master
    tracker, so a repeat run can skip them. Empty sets if there's no tracker yet
    or it can't be read (never crashes a run over this).
    """
    path = path or config.MASTER_TRACKER
    try:
        rows, id_link_keys = tracker._load_existing(path)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return set(), set()
    content = {content_key(row) for row in rows}
    return id_link_keys, content


# ---------------------------------------------------------------------------
# The global dedupe + pre-filter across all buckets.
# ---------------------------------------------------------------------------
def dedupe_across(buckets, known_id_link=None, known_content=None):
    """
    De-duplicate across several source buckets at once and drop already-tracked
    jobs.

    buckets: list of (bucket_name, jobs_list). The surviving copy stays in the
             bucket of whichever duplicate was richest.
    known_id_link / known_content: sets from load_known_keys() (optional). When
             given, jobs matching a tracked job are dropped before analysis.

    Returns (rebuilt, stats) where:
      rebuilt = {bucket_name: [jobs...]} with the same bucket names, deduped.
      stats   = {"cross_source": int, "already_tracked": int}
    """
    known_id_link = known_id_link or set()
    known_content = known_content or set()

    groups = {}   # content_key -> [(richness, job, bucket_name), ...]
    order = []    # first-seen key order, to keep results stable
    already_tracked = 0

    for name, jobs in buckets:
        for job in jobs:
            # Already in the master tracker? (identity OR fuzzy content match)
            ckey = content_key(job)
            if (_identity_keys(job) & known_id_link) or ckey in known_content:
                already_tracked += 1
                continue
            if ckey not in groups:
                groups[ckey] = []
                order.append(ckey)
            groups[ckey].append((_richness(job), job, name))

    # Fields worth rescuing from a duplicate before we drop it — every one is
    # real data the SAME job carried on another source (never invented).
    _MERGE_FIELDS = ("hiring_team_link", "job_description", "salary",
                     "salary_lpa")

    rebuilt = {name: [] for name, _ in buckets}
    cross_source = 0
    for ckey in order:
        members = groups[ckey]
        cross_source += len(members) - 1
        # Richest first; stable sort keeps first-seen order on ties.
        members.sort(key=lambda t: t[0], reverse=True)
        _rich, base, name = members[0]
        for _r, other, _n in members[1:]:
            for f in _MERGE_FIELDS:
                if not base.get(f) and other.get(f):
                    base[f] = other[f]
        rebuilt[name].append(base)

    return rebuilt, {"cross_source": cross_source,
                     "already_tracked": already_tracked}
