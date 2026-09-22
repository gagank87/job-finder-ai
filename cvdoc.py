"""
cvdoc.py — read the master CV (.docx) and write a tailored copy, delivered as PDF.

Two jobs:
  * load_master()        -> parse your master_cv.docx into a light structure the
                            tailoring engine can reason about (summary paragraph,
                            experience bullets, skills paragraphs) WITHOUT losing
                            the original document (we keep the Document object so
                            we can edit it in place later, preserving fonts/layout).
  * build_tailored_pdf() -> fill a COPY of the master with Claude's tailored text
                            (replacing paragraph text in place, so styling is kept)
                            and convert it to PDF via MS Word (docx2pdf).

Honesty / robustness rules:
  * We only ever REPLACE existing paragraph text — never fabricate structure. If
    the master can't be confidently parsed, we still produce a clean readable
    .docx from the tailored fields and say so.
  * PDF conversion needs MS Word (docx2pdf uses it via COM on Windows). If Word
    isn't available or conversion fails, we KEEP the .docx as the deliverable and
    warn — never a hard crash.
  * Missing python-docx / missing master file are reported with one clear line,
    not a traceback.

Nothing here touches the network or the API key.
"""

import os

import config
import utils

# python-docx is an optional dependency (only needed for this feature). Import
# lazily-guarded so importing cvdoc never crashes a plain search run.
try:
    from docx import Document
    _DOCX_OK = True
except Exception:  # noqa: BLE001
    Document = None
    _DOCX_OK = False


# Leading markers that suggest a paragraph is a bullet / list item.
_BULLET_PREFIXES = ("•", "-", "–", "—", "*", "·", "▪", "‣", "◦")

# Words that identify the "skills" area of a CV (whole-line headings or labels).
_SKILLS_HINTS = ("skill", "technical proficienc", "tools", "technologies",
                 "competenc")

# Words that identify a professional-summary / profile / objective heading.
_SUMMARY_HINTS = ("summary", "profile", "objective", "about")


def available() -> bool:
    """True if python-docx is installed (the feature can read/write .docx)."""
    return _DOCX_OK


def _is_bulletish(paragraph) -> bool:
    """Heuristic: does this paragraph read like an experience bullet?"""
    text = paragraph.text.strip()
    if not text:
        return False
    if text[0] in _BULLET_PREFIXES:
        return True
    style = (paragraph.style.name or "").lower() if paragraph.style else ""
    if "list" in style or "bullet" in style:
        return True
    # numPr in the paragraph properties => a Word list item.
    try:
        p = paragraph._p  # noqa: SLF001 (documented python-docx access)
        if p.pPr is not None and p.pPr.numPr is not None:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _is_headingish(paragraph) -> bool:
    style = (paragraph.style.name or "").lower() if paragraph.style else ""
    return style.startswith("heading") or style == "title"


def load_master(path=None):
    """
    Parse the master CV into a structure the tailoring engine can use.

    Returns a dict:
      {
        "ok": bool,               # False if unreadable (with "error" set)
        "error": str,             # populated when ok is False
        "path": str,
        "doc": Document | None,   # the live document (for in-place editing)
        "full_text": str,         # all paragraph text joined (for the fact-guard)
        "summary_para_idx": int | None,
        "bullet_para_idxs": [int, ...],
        "skills_para_idxs": [int, ...],
        "paragraphs": [str, ...], # snapshot of paragraph texts by index
      }
    Never raises: a missing library / missing file returns ok=False with a
    human-readable reason.
    """
    path = path or config.MASTER_CV_PATH
    if not _DOCX_OK:
        return {"ok": False, "path": path, "doc": None,
                "error": "python-docx isn't installed. Run: pip install python-docx",
                "full_text": "", "summary_para_idx": None,
                "bullet_para_idxs": [], "skills_para_idxs": [], "paragraphs": []}
    if not os.path.exists(path):
        return {"ok": False, "path": path, "doc": None,
                "error": f"Master CV not found at '{path}'. Drop your CV there "
                         f"(see cv/README.txt).",
                "full_text": "", "summary_para_idx": None,
                "bullet_para_idxs": [], "skills_para_idxs": [], "paragraphs": []}

    try:
        doc = Document(path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": path, "doc": None,
                "error": f"Couldn't open '{path}' as a Word document: {e}",
                "full_text": "", "summary_para_idx": None,
                "bullet_para_idxs": [], "skills_para_idxs": [], "paragraphs": []}

    paragraphs = [p.text for p in doc.paragraphs]
    bullet_idxs, skills_idxs = [], []
    summary_idx = None
    in_skills_section = False

    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        low = text.lower()

        # Track whether we're inside a skills block (heading persists until the
        # next heading).
        if _is_headingish(para) or (text and text == text.upper() and len(text) < 40):
            in_skills_section = any(h in low for h in _SKILLS_HINTS)

        if not text:
            continue

        if _is_bulletish(para):
            bullet_idxs.append(i)
            if in_skills_section:
                skills_idxs.append(i)
            continue

        # First prose block under a summary heading (or first non-heading prose
        # near the top) is the professional summary.
        if summary_idx is None and not _is_headingish(para):
            if any(h in low for h in _SUMMARY_HINTS) and len(text) < 40:
                # This is the "Summary" heading itself; the summary text follows.
                continue
            # A reasonably long prose paragraph early in the doc = the summary.
            if len(text.split()) >= 12 and i < max(12, len(paragraphs) // 3):
                summary_idx = i

    return {"ok": True, "error": "", "path": path, "doc": doc,
            "full_text": "\n".join(paragraphs),
            "summary_para_idx": summary_idx,
            "bullet_para_idxs": bullet_idxs,
            "skills_para_idxs": skills_idxs,
            "paragraphs": paragraphs}


def _set_paragraph_text(paragraph, new_text):
    """
    Replace a paragraph's text while preserving its first run's formatting.

    Word paragraphs are made of "runs" (each with its own font). We write the
    new text into the first run and clear the rest, so the bullet keeps its
    original look. If there are no runs, we add one.
    """
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(new_text)
        return
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""


def write_tailored_docx(cv_struct, tailored, out_docx):
    """
    Write a tailored .docx by editing a COPY of the master in place.

    We reopen the master fresh (so repeated calls don't accumulate edits),
    replace the summary paragraph and rewrite the experience bullets in the new
    order/wording Claude returned, then save to out_docx. Formatting, headings,
    and everything we didn't touch are preserved.

    Falls back to a clean generated .docx (plain, readable) if the master can't
    be reopened/parsed — and returns ("fallback", path) so the caller can warn.

    Returns (mode, path) where mode is "template" or "fallback".
    Raises PermissionError only if out_docx is locked (open in Word) — handled
    by callers exactly like the tracker's locked-file case.
    """
    summary = (tailored.get("summary") or "").strip()
    bullets = [b.strip() for b in (tailored.get("bullets") or []) if b.strip()]

    # Reopen the master fresh for a clean edit surface.
    fresh = load_master(cv_struct.get("path"))
    if not fresh["ok"] or fresh["doc"] is None:
        _write_fallback_docx(tailored, out_docx)
        return "fallback", out_docx

    doc = fresh["doc"]
    paras = doc.paragraphs

    # Replace the summary paragraph, if we found one.
    if summary and fresh["summary_para_idx"] is not None:
        idx = fresh["summary_para_idx"]
        if 0 <= idx < len(paras):
            _set_paragraph_text(paras[idx], summary)

    # Rewrite experience bullets in place, position by position. We only fill
    # existing bullet paragraphs (never invent new ones); extra tailored bullets
    # beyond the master's bullet count are dropped rather than appended blindly,
    # keeping the document's real structure intact.
    bullet_idxs = fresh["bullet_para_idxs"]
    # Don't overwrite skills-section bullets with experience bullets.
    exp_bullet_idxs = [i for i in bullet_idxs
                       if i not in set(fresh["skills_para_idxs"])]
    for pos, para_idx in enumerate(exp_bullet_idxs):
        if pos >= len(bullets):
            break
        if 0 <= para_idx < len(paras):
            # Preserve any original leading bullet glyph.
            orig = paras[para_idx].text.strip()
            prefix = ""
            if orig and orig[0] in _BULLET_PREFIXES:
                prefix = orig[0] + " "
            _set_paragraph_text(paras[para_idx], prefix + bullets[pos])

    doc.save(out_docx)  # PermissionError propagates if locked (open in Word)
    return "template", out_docx


def _write_fallback_docx(tailored, out_docx):
    """Generate a clean, plain tailored .docx from the tailored fields alone."""
    if not _DOCX_OK:
        raise RuntimeError("python-docx not installed")
    doc = Document()
    summary = (tailored.get("summary") or "").strip()
    if summary:
        doc.add_heading("Summary", level=1)
        doc.add_paragraph(summary)
    bullets = [b.strip() for b in (tailored.get("bullets") or []) if b.strip()]
    if bullets:
        doc.add_heading("Experience Highlights", level=1)
        for b in bullets:
            doc.add_paragraph(b, style="List Bullet")
    skills = [s.strip() for s in (tailored.get("skills_order") or []) if s.strip()]
    if skills:
        doc.add_heading("Skills", level=1)
        doc.add_paragraph(", ".join(skills))
    doc.save(out_docx)


def to_pdf(docx_path, pdf_path):
    """
    Convert docx_path -> pdf_path via MS Word (docx2pdf). Returns the PDF path on
    success, or None on any failure (missing docx2pdf / no Word / COM error),
    warning once. Never raises — the caller keeps the .docx as the deliverable.
    """
    try:
        from docx2pdf import convert
    except Exception:  # noqa: BLE001
        utils.warn("docx2pdf isn't installed — delivering the .docx instead of "
                   "a PDF. (pip install docx2pdf, needs MS Word.)")
        return None
    try:
        convert(docx_path, pdf_path)
    except Exception as e:  # noqa: BLE001
        utils.warn(f"Couldn't convert to PDF ({e}) — delivering the .docx "
                   f"instead. (PDF export needs MS Word installed.)")
        return None
    if os.path.exists(pdf_path):
        return pdf_path
    utils.warn("PDF conversion reported success but no file appeared — "
               "delivering the .docx instead.")
    return None


def build_tailored_pdf(cv_struct, tailored, out_dir, base_name):
    """
    Produce the tailored CV deliverable in out_dir.

    Writes <base_name>.docx (from the master template) then tries to convert to
    <base_name>.pdf. Returns (delivered_path, is_pdf) — delivered_path is the PDF
    when conversion worked, else the .docx. `is_pdf` lets the caller narrate the
    honest outcome.
    """
    os.makedirs(out_dir, exist_ok=True)
    docx_path = os.path.join(out_dir, f"{base_name}.docx")
    pdf_path = os.path.join(out_dir, f"{base_name}.pdf")

    mode, _ = write_tailored_docx(cv_struct, tailored, docx_path)
    if mode == "fallback":
        utils.warn("Couldn't map the tailored text onto your CV's layout — "
                   "produced a clean plain version instead. Check cv/README.txt "
                   "if the CV structure is unusual.")

    produced = to_pdf(docx_path, pdf_path)
    if produced:
        return produced, True
    return docx_path, False
