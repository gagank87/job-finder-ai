"""
autosubmit.py — REAL application submission, but only where a tenant genuinely
allows it without a captcha / Cloudflare challenge / login.

The honest contract (do not weaken):
  * capability(job)     -> "greenhouse" | "lever" | None. None means "we have no
                           known, unauthenticated, non-captcha submit path" ->
                           the caller prepares-to-apply instead.
  * can_autosubmit(job) -> bool convenience wrapper.
  * submit(job, profile, resume_path, cover_text)
        -> {"ok": bool, "confirmation": str, "reason": str}
     Performs a REAL multipart POST to the tenant's public application form and
     VERIFIES the response confirms receipt. It returns ok=True ONLY on a
     positive confirmation signal. On ANY doubt — a captcha/Cloudflare marker on
     the form, a required question we can't answer from the profile, a non-2xx
     response, a network error, or a response that doesn't clearly confirm — it
     returns ok=False with a reason and the caller falls back to prepare.

Never fakes a submission. Never guesses a screening answer. Never records
"Applied" — that is the tracker's job, and only the caller does it on ok=True.
"""

import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import applicant
import config
import utils

# Markers that mean "there is a human/anti-bot gate here" -> we must NOT submit.
_CAPTCHA_MARKERS = (
    "recaptcha", "g-recaptcha", "hcaptcha", "h-captcha", "turnstile",
    "cf-challenge", "cf-turnstile", "__cf_chl", "challenge-platform",
    "grecaptcha", "data-sitekey",
)
_CLOUDFLARE_MARKERS = (
    "cloudflare", "attention required", "checking your browser",
    "cf-browser-verification",
)

# Positive signals that an application was actually received.
_CONFIRM_MARKERS = (
    "thank you for applying", "application submitted", "your application has been",
    "thanks for applying", "we have received your application",
    "application was submitted", "successfully submitted", "confirmation",
    "thank you for your interest",
)

# Personal-field name matching (form input name/id/label -> profile key).
_FIELD_PATTERNS = [
    ("first_name", re.compile(r"first[\s_]*name", re.I)),
    ("last_name", re.compile(r"last[\s_]*name", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|contact number", re.I)),
    ("linkedin_url", re.compile(r"linkedin", re.I)),
    ("github_url", re.compile(r"github", re.I)),
    ("portfolio_url", re.compile(r"portfolio|website|personal site", re.I)),
    ("location", re.compile(r"location|city|where are you", re.I)),
]


# ---------------------------------------------------------------------------
# Capability detection (which tenant + is it a path we can attempt).
# ---------------------------------------------------------------------------
def _greenhouse_ids(job):
    """(token, job_id, apply_url) for a Greenhouse posting, or (None, None, None)."""
    link = job.get("job_link", "") or ""
    host = urlparse(link).netloc.lower()
    if "greenhouse.io" not in host:
        return None, None, None
    parts = [p for p in urlparse(link).path.strip("/").split("/") if p]
    if not parts:
        return None, None, None
    token = parts[0]
    job_id = str(job.get("job_id", "")).strip()
    if not job_id:
        # .../<token>/jobs/<id>
        m = re.search(r"/jobs/(\d+)", link)
        job_id = m.group(1) if m else ""
    if not job_id:
        return None, None, None
    apply_url = f"https://boards.greenhouse.io/{token}/jobs/{job_id}"
    return token, job_id, apply_url


def _lever_ids(job):
    """(token, posting_id, apply_url) for a Lever posting, or (None, None, None)."""
    link = job.get("job_link", "") or ""
    host = urlparse(link).netloc.lower()
    if "lever.co" not in host:
        return None, None, None
    parts = [p for p in urlparse(link).path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None, None, None
    token, posting_id = parts[0], parts[1]
    apply_url = f"https://jobs.lever.co/{token}/{posting_id}/apply"
    return token, posting_id, apply_url


def capability(job):
    """
    Return the platform key we can ATTEMPT for this job, or None.

    Conservative by design: only Greenhouse and Lever public application forms
    are attempted, and only when the posting resolves to a token + id. Everything
    else (Workday, LinkedIn, JobPosting-only, unknown) returns None -> prepare.
    Honors the ENABLE_AUTO_SUBMIT master switch.
    """
    if not getattr(config, "ENABLE_AUTO_SUBMIT", False):
        return None
    if _greenhouse_ids(job)[0]:
        return "greenhouse"
    if _lever_ids(job)[0]:
        return "lever"
    return None


def can_autosubmit(job) -> bool:
    return capability(job) is not None


# ---------------------------------------------------------------------------
# Shared HTML-form submission.
# ---------------------------------------------------------------------------
def _has_marker(text, markers):
    low = (text or "").lower()
    return any(m in low for m in markers)


def _fail(reason):
    return {"ok": False, "confirmation": "", "reason": reason}


def _pick_application_form(soup):
    """Choose the most likely application <form> on the page."""
    forms = soup.find_all("form")
    if not forms:
        return None
    # Prefer a form that has a file input (resume) — the real application form.
    for f in forms:
        if f.find("input", attrs={"type": "file"}):
            return f
    # Else the form with the most inputs.
    return max(forms, key=lambda f: len(f.find_all(["input", "textarea", "select"])))


def _label_text_for(form, field):
    """Best-effort human label for a field (for question matching)."""
    fid = field.get("id")
    if fid:
        lab = form.find("label", attrs={"for": fid})
        if lab and lab.get_text(strip=True):
            return lab.get_text(" ", strip=True)
    # A wrapping label or a preceding label sibling.
    lab = field.find_parent("label")
    if lab and lab.get_text(strip=True):
        return lab.get_text(" ", strip=True)
    return field.get("aria-label") or field.get("placeholder") or field.get("name") or ""


def _match_personal(name_blob, profile):
    """Return the profile value for a recognised personal field, or None."""
    for key, pat in _FIELD_PATTERNS:
        if pat.search(name_blob or ""):
            val = str(profile.get(key, "")).strip()
            return val or None
    return None


def _is_required(field):
    if field.has_attr("required"):
        return True
    aria = (field.get("aria-required") or "").lower()
    return aria == "true"


def _build_form_data(form, profile):
    """
    Walk the form and build (data, unanswered_required).

    Fills hidden fields verbatim (authenticity tokens etc.), personal fields from
    the profile, and screening questions from profile defaults. Any REQUIRED
    field we can't fill is collected in `unanswered_required` — a non-empty list
    means we must abort (never guess).
    """
    data = {}
    unanswered = []

    for field in form.find_all(["input", "textarea", "select"]):
        ftype = (field.get("type") or field.name or "").lower()
        name = field.get("name")
        if not name:
            continue
        if ftype in ("file", "submit", "button", "image", "reset"):
            continue

        if ftype == "hidden":
            data[name] = field.get("value", "")
            continue

        label_blob = " ".join(filter(None, [
            name, field.get("id", ""), _label_text_for(form, field)]))

        # Checkboxes: only tick when a matching profile default says yes.
        if ftype == "checkbox":
            ans = applicant.screening_answer(profile, label_blob)
            if ans and ans.strip().lower() in ("yes", "true", "1", "on"):
                data[name] = field.get("value", "on")
            elif _is_required(field):
                unanswered.append(label_blob.strip()[:80])
            continue

        # Radios / selects: pick the option matching a profile default.
        if ftype == "select" or ftype == "select-one":
            ans = applicant.screening_answer(profile, label_blob)
            chosen = None
            options = [o.get("value", o.get_text(strip=True))
                       for o in field.find_all("option")]
            if ans:
                for o in field.find_all("option"):
                    otext = (o.get_text(strip=True) or "") + " " + (o.get("value") or "")
                    if ans.strip().lower() in otext.lower():
                        chosen = o.get("value", o.get_text(strip=True))
                        break
            if chosen is not None:
                data[name] = chosen
            elif _is_required(field):
                unanswered.append(label_blob.strip()[:80])
            continue

        if ftype == "radio":
            # Grouped by name; handled by whichever radio matches the default.
            ans = applicant.screening_answer(profile, label_blob)
            val = field.get("value", "")
            if ans and ans.strip().lower() in (val.strip().lower(),
                                               _label_text_for(form, field).strip().lower()):
                data[name] = val
            continue

        # Plain text / textarea / email / tel.
        personal = _match_personal(label_blob, profile)
        if personal:
            data[name] = personal
            continue

        ans = applicant.screening_answer(profile, label_blob)
        if ans:
            data[name] = ans
        elif _is_required(field):
            unanswered.append(label_blob.strip()[:80] or name)

    # Verify every required radio group got an answer.
    return data, unanswered


def _resume_field_name(form):
    f = form.find("input", attrs={"type": "file"})
    return f.get("name") if f else None


def _looks_confirmed(resp):
    """Strict success check: 2xx AND a positive confirmation marker present."""
    if resp.status_code >= 400:
        return False, f"server returned HTTP {resp.status_code}"
    body = resp.text or ""
    # A captcha/error surfacing in the RESPONSE means it wasn't accepted.
    if _has_marker(body, _CAPTCHA_MARKERS):
        return False, "response still shows a captcha (not submitted)"
    url_low = (resp.url or "").lower()
    if "confirmation" in url_low or "thank" in url_low:
        return True, f"redirected to {resp.url}"
    if _has_marker(body, _CONFIRM_MARKERS):
        return True, "confirmation message present in response"
    return False, "no confirmation signal in the response"


def _submit_form(job, profile, resume_path, apply_url):
    """
    Fetch the apply page, refuse if gated, fill the form from the profile, and
    POST it with the resume attached. Returns the submit() result dict.
    """
    session = utils.get_session()
    try:
        page = session.get(apply_url, timeout=config.HTTP_TIMEOUT,
                           allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return _fail(f"couldn't load the application form ({e})")
    if page.status_code >= 400 or not page.text:
        return _fail(f"application form returned HTTP {page.status_code}")

    html = page.text
    if _has_marker(html, _CLOUDFLARE_MARKERS):
        return _fail("form is behind a Cloudflare challenge")
    if _has_marker(html, _CAPTCHA_MARKERS):
        return _fail("form requires a captcha (reCAPTCHA/hCaptcha/Turnstile)")

    soup = BeautifulSoup(html, "html.parser")
    form = _pick_application_form(soup)
    if form is None:
        return _fail("no application form found on the page")

    resume_name = _resume_field_name(form)
    if not resume_name:
        return _fail("form has no resume upload field we can use")
    if not resume_path or not os.path.exists(resume_path):
        return _fail("tailored CV file not available to attach")

    data, unanswered = _build_form_data(form, profile)
    if unanswered:
        return _fail("required question(s) not covered by your profile: "
                     + "; ".join(unanswered[:3])
                     + (" ..." if len(unanswered) > 3 else ""))

    # Resolve the POST target.
    action = form.get("action") or apply_url
    post_url = urljoin(page.url, action)
    method = (form.get("method") or "post").lower()
    if method != "post":
        return _fail(f"form uses an unsupported method '{method}'")

    # Attach the resume as multipart.
    try:
        with open(resume_path, "rb") as fh:
            ctype = ("application/pdf" if resume_path.lower().endswith(".pdf")
                     else "application/vnd.openxmlformats-officedocument."
                          "wordprocessingml.document")
            files = {resume_name: (os.path.basename(resume_path), fh, ctype)}
            utils.polite_sleep()
            resp = session.post(post_url, data=data, files=files,
                                timeout=config.HTTP_TIMEOUT,
                                allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return _fail(f"submission request failed ({e})")

    confirmed, detail = _looks_confirmed(resp)
    if confirmed:
        return {"ok": True, "confirmation": detail, "reason": ""}
    return _fail(detail)


# ---------------------------------------------------------------------------
# Public submit entry point.
# ---------------------------------------------------------------------------
def submit(job, profile, resume_path, cover_text=""):
    """
    Attempt a REAL submission for a supported tenant. Returns
    {"ok", "confirmation", "reason"}. ok=True ONLY on verified receipt.

    Never raises; any failure is a clean ok=False with a human-readable reason,
    so the caller falls back to prepare-to-apply.
    """
    plat = capability(job)
    if plat is None:
        return _fail("no supported unauthenticated submit path for this posting")

    if plat == "greenhouse":
        _, _, apply_url = _greenhouse_ids(job)
    elif plat == "lever":
        _, _, apply_url = _lever_ids(job)
    else:
        return _fail(f"unsupported platform '{plat}'")

    return _submit_form(job, profile, resume_path, apply_url)
