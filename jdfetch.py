"""
JD text retrieval — fetch a job's full description, per source.

`get_jd(job)` returns the posting's description as plain text, or "" when it
can't be obtained. It NEVER raises and NEVER fabricates: an unreachable JD
simply comes back empty, and the caller keeps the job with an "Unknown"
verdict (see jdfit + core).

Coverage (all real public endpoints, no login, no blocked sources):
  * Inline    — aggregators / Lever already carry the text; we set
                job["job_description"] at fetch time, so we reuse it for free.
  * LinkedIn  — guest job-detail endpoint (…/jobPosting/{id}) -> parse HTML.
  * Greenhouse— boards-api job-detail `content` field -> unescape HTML.
  * Fallback  — best-effort GET of job_link, strip tags. "" on any failure.
"""

import html
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import config
import utils

_LINKEDIN_DETAIL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
)


def _clean(text):
    """Collapse whitespace; keep it plain."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _strip_html(raw):
    """Turn an HTML fragment/string into readable plain text."""
    if not raw:
        return ""
    soup = BeautifulSoup(html.unescape(raw), "html.parser")
    return _clean(soup.get_text(" ", strip=True))


def _from_linkedin(job):
    jid = str(job.get("job_id", "")).strip()
    if not jid.isdigit():
        return ""
    session = utils.get_session()
    try:
        r = session.get(_LINKEDIN_DETAIL.format(job_id=jid),
                        timeout=config.HTTP_TIMEOUT)
    except Exception:  # noqa: BLE001
        return ""
    if r.status_code != 200 or not r.text:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    desc = soup.find("div", class_=re.compile(
        r"description__text|show-more-less-html"))
    if desc:
        return _clean(desc.get_text(" ", strip=True))
    return ""


_GH_TOKEN_RE = re.compile(r"greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_-]+)")
_GH_JID_RE = re.compile(r"(?:gh_jid=|/jobs/)(\d+)")


def _from_greenhouse(job):
    link = job.get("job_link", "")
    tok = _GH_TOKEN_RE.search(link)
    jid = _GH_JID_RE.search(link)
    if not (tok and jid):
        return ""
    url = (f"https://boards-api.greenhouse.io/v1/boards/"
           f"{tok.group(1)}/jobs/{jid.group(1)}")
    session = utils.get_session()
    try:
        r = session.get(url, timeout=config.HTTP_TIMEOUT)
    except Exception:  # noqa: BLE001
        return ""
    if r.status_code != 200:
        return ""
    try:
        content = r.json().get("content", "")
    except ValueError:
        return ""
    return _strip_html(content)


def _from_link(job):
    """Last resort: GET the posting page and strip tags."""
    link = job.get("job_link", "")
    if not link:
        return ""
    session = utils.get_session()
    try:
        r = session.get(link, timeout=config.HTTP_TIMEOUT, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return ""
    if r.status_code != 200 or not r.text:
        return ""
    return _strip_html(r.text)


def get_jd(job):
    """
    Return the job's description as plain text, or "" if unavailable.

    Order: inline text set by the source -> source-specific detail endpoint
    -> generic link fetch. Always polite-sleeps after a network call.
    """
    # 1) Already provided inline by the source (aggregators / Lever).
    inline = job.get("job_description")
    if inline and str(inline).strip():
        return _clean(_strip_html(str(inline)) if "<" in str(inline)
                      else str(inline))

    source = (job.get("source") or "").lower()
    jd = ""
    if source.startswith("linkedin"):
        jd = _from_linkedin(job)
    elif source.startswith("greenhouse"):
        jd = _from_greenhouse(job)

    if not jd:
        jd = _from_link(job)

    utils.polite_sleep()
    return jd
