"""
cvtailor.py — Claude-powered CV tailoring + cover-letter drafting, with a
mechanical anti-fabrication guard.

Given a job description, your parsed master CV (cvdoc.load_master), and your
reference cover letter, this asks Claude to:
  * rewrite your professional summary to match the JD's language/priorities,
  * reorder/rephrase your EXISTING experience bullets for this role,
  * suggest a skills ordering,
  * draft a cover note in the tone + structure of your reference.

THE HARD RULE — never fabricate. Two layers enforce it:
  1) The prompt forbids inventing any experience, skill, employer, date, tool,
     or metric, and instructs rewrite/reorder ONLY from what's on the CV.
  2) _verify_no_fabrication() mechanically compares the output against the master
     CV text and flags hard tokens (numbers/percentages, ALL-CAPS acronyms) that
     appear in the output but not the source. Flagged output is surfaced to the
     caller as needs-review — never silently shipped.

No network/key handling leaks: get_client() returns None (with a reason) when
the SDK isn't installed or ANTHROPIC_API_KEY isn't set, so callers can degrade
to "tailoring unavailable" cleanly. The key itself is never logged.
"""

import json
import re

import config
import llm
import utils


# Tokens the fact-guard treats as "hard facts" that must trace back to the CV.
_NUMBER_RE = re.compile(r"\b\d[\d,\.]*%?\b")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9\.&/+-]{1,}\b")  # e.g. SQL, GA4, PL/SQL

# Common words that look like acronyms but aren't CV facts (don't flag these).
_ACRONYM_STOPWORDS = {
    "AND", "THE", "FOR", "WITH", "YOU", "YOUR", "OUR", "ARE", "USA", "UK", "US",
    "CV", "JD", "AI", "OK", "I", "A", "AN", "TO", "OF", "IN", "ON", "AS", "AT",
    "CEO", "CTO", "HR", "IT", "PDF",
}


def sdk_available() -> bool:
    return llm.sdk_available()


def get_client():
    """
    Return (client, reason) for the Anthropic (Claude) provider specifically.

    Kept for backward compatibility; tailoring itself now goes through
    llm.complete(), which additionally falls back to the free Groq/Gemini
    providers if Claude is unavailable. See llm.anthropic_client.
    """
    return llm.anthropic_client()


def load_cover_reference(path=None):
    """Return your reference cover letter text, or "" if none is present."""
    path = path or config.COVER_REFERENCE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Prompt construction.
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a careful, honest CV-tailoring assistant. You help a job seeker "
    "present their EXISTING experience in the best light for a specific role. "
    "You must NEVER invent, exaggerate, or imply any experience, skill, "
    "employer, job title, date, degree, certification, tool, or metric that is "
    "not already present in the candidate's master CV. You may only rephrase, "
    "reorder, and re-emphasise what is genuinely there. If the job wants "
    "something the candidate lacks, do not claim it — simply focus on their "
    "real, relevant strengths. Cover letters must be measured, specific, and "
    "genuine — never arrogant, cocky, boastful, or overconfident."
)


def _build_user_prompt(jd_text, cv_struct, job, cover_reference):
    cv_text = cv_struct.get("full_text", "")
    jd_block = jd_text.strip() if jd_text and jd_text.strip() else (
        "(The job description could not be retrieved. Tailor conservatively "
        "from the CV alone and keep claims general.)")
    ref_block = (cover_reference.strip() if cover_reference
                 else "(No reference letter provided — omit the cover_note "
                      "field, leaving it an empty string.)")
    n_bullets = len([i for i in cv_struct.get("bullet_para_idxs", [])
                     if i not in set(cv_struct.get("skills_para_idxs", []))])
    bullet_guidance = (
        f"Return at most {n_bullets} experience bullets (the number the CV has) "
        "so they map onto the document." if n_bullets else
        "Return a handful of experience bullets drawn only from the CV.")

    return (
        f"JOB TITLE: {job.get('title','')}\n"
        f"COMPANY: {job.get('company','')}\n\n"
        f"=== JOB DESCRIPTION ===\n{jd_block}\n\n"
        f"=== CANDIDATE MASTER CV (the ONLY source of facts) ===\n{cv_text}\n\n"
        f"=== REFERENCE COVER LETTER (match its tone + structure) ===\n"
        f"{ref_block}\n\n"
        "TASK: Tailor the candidate's materials for THIS job, using only facts "
        "from the master CV.\n"
        f"- summary: a 2-4 sentence professional summary rewritten for this role.\n"
        f"- bullets: reordered/rephrased experience bullets. {bullet_guidance}\n"
        "- skills_order: the candidate's real skills, ordered by relevance to "
        "this JD (do not add skills they don't have).\n"
        "- cover_note: a short cover letter in the reference's tone/structure "
        "(measured, not arrogant). Empty string if no reference was provided.\n"
        "- notes: one line on what you emphasised, or any gap you deliberately "
        "did NOT paper over.\n\n"
        "Respond with ONLY a JSON object with exactly these keys: "
        "summary, bullets (array of strings), skills_order (array of strings), "
        "cover_note (string), notes (string). No markdown, no commentary."
    )


def _parse_response(text):
    """Extract the JSON object from Claude's reply; tolerant of stray text."""
    text = (text or "").strip()
    # Strip a ```json fence if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Anti-fabrication guard.
# ---------------------------------------------------------------------------
def _hard_tokens(text):
    """Numbers/percentages + acronym-like tokens that should trace to the CV."""
    nums = set(_NUMBER_RE.findall(text or ""))
    acrs = {a for a in _ACRONYM_RE.findall(text or "")
            if a.upper() not in _ACRONYM_STOPWORDS}
    return nums, acrs


def _verify_no_fabrication(cv_struct, tailored):
    """
    Compare the tailored output against the master CV and return a list of
    human-readable flags for hard tokens (numbers, acronyms) that appear in the
    output but NOT in the source CV. Empty list => nothing suspicious.

    This is deliberately strict-but-shallow: it can't judge nuance, but it
    reliably catches invented metrics ("increased sales 35%") or tools the CV
    never mentions, which are exactly the fabrications we must never ship.
    """
    src = cv_struct.get("full_text", "") or ""
    src_low = src.lower()
    src_nums, src_acrs = _hard_tokens(src)
    src_acrs_low = {a.lower() for a in src_acrs}

    out_parts = [tailored.get("summary", "")]
    out_parts += list(tailored.get("bullets", []) or [])
    out_parts += list(tailored.get("skills_order", []) or [])
    # The cover note is prose about fit; we check it too for invented metrics.
    out_parts.append(tailored.get("cover_note", ""))
    out_text = "\n".join(str(p) for p in out_parts)

    out_nums, out_acrs = _hard_tokens(out_text)

    flags = []
    for n in sorted(out_nums):
        # Ignore trivial digits that are almost never a claim (e.g. "1", "2").
        if n in src_nums:
            continue
        if n.rstrip("%").replace(",", "").replace(".", "").isdigit() and \
                len(n.rstrip("%")) <= 1:
            continue
        if n not in src:
            flags.append(f"number '{n}' not found in your CV")
    for a in sorted(out_acrs):
        if a.lower() in src_acrs_low or a.lower() in src_low:
            continue
        flags.append(f"term '{a}' not found in your CV")
    return flags


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def tailor(jd_text, cv_struct, job, cover_reference="", client=None):
    """
    Tailor the CV + draft a cover note for one job.

    Returns a dict:
      {
        "ok": bool,
        "error": str,                # populated when ok is False
        "provider": str,             # which LLM produced this (label)
        "summary": str,
        "bullets": [str, ...],
        "skills_order": [str, ...],
        "cover_note": str,
        "notes": str,
        "fabrication_flags": [str, ...],   # non-empty => needs human review
      }

    Goes through llm.complete(), which tries the configured provider chain
    (Claude/Bedrock, then free Groq/Gemini) and returns the first success. On
    any failure (no provider, all providers error, unparseable response) returns
    ok=False with a reason — the caller decides whether to skip. Never raises.

    `client` is accepted for backward compatibility and ignored (the provider
    chain is resolved inside llm).
    """
    empty = {"ok": False, "error": "", "provider": "", "summary": "",
             "bullets": [], "skills_order": [], "cover_note": "", "notes": "",
             "fabrication_flags": []}

    if not cv_struct or not cv_struct.get("ok"):
        empty["error"] = (cv_struct or {}).get("error", "Master CV not loaded.")
        return empty

    prompt = _build_user_prompt(jd_text, cv_struct, job, cover_reference)
    result = llm.complete(_SYSTEM_PROMPT, prompt, max_tokens=2000)
    if not result["ok"]:
        empty["error"] = result["error"]
        return empty

    data = _parse_response(result["text"])
    if not isinstance(data, dict):
        empty["error"] = ("The model's response wasn't valid JSON "
                          f"({result['provider']}) — skipped this job.")
        return empty

    tailored = {
        "ok": True,
        "error": "",
        "provider": result["provider"],
        "summary": str(data.get("summary", "")).strip(),
        "bullets": [str(b).strip() for b in (data.get("bullets") or [])
                    if str(b).strip()],
        "skills_order": [str(s).strip() for s in (data.get("skills_order") or [])
                         if str(s).strip()],
        "cover_note": str(data.get("cover_note", "")).strip(),
        "notes": str(data.get("notes", "")).strip(),
        "fabrication_flags": [],
    }
    tailored["fabrication_flags"] = _verify_no_fabrication(cv_struct, tailored)
    return tailored
