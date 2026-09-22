"""
apply.py — tailor a CV + cover note per job, then auto-submit where genuinely
possible and prepare-to-apply everywhere else.

Two ways in:
  * Importable: `process_jobs(jobs, ...)` — used by the GUI button.
  * Standalone: `python apply.py [--all] [--top N] [--job-id ID] [--prepare-only]`
    reads master_tracker.xlsx, selects eligible-not-yet-actioned rows, and runs
    the same pipeline with console confirmations.

Honesty guarantees (do not weaken):
  * Claude only rephrases/reprioritises real CV content; a fabrication flag from
    cvtailor is surfaced and the job is prepared with a visible warning.
  * A job is recorded "Applied" ONLY after autosubmit.submit returns a verified
    confirmation. Any doubt -> prepared, never marked applied.
  * Two human gates: a cost confirmation before spending tokens, and a batch
    confirmation before any real submission.
  * Every failure degrades to prepare-to-apply with a clear one-line reason;
    nothing here crashes a run.
"""

import argparse
import os
import re
import sys
import webbrowser

import applicant
import autosubmit
import config
import cvdoc
import cvtailor
import jdfetch
import llm
import tracker
import utils


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------
def _safe(text, maxlen=40):
    """Filesystem-safe slug for a folder name."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()
    return (s[:maxlen] or "job").strip("-")


def _folder_for(job, out_dir):
    base = "_".join(filter(None, [
        _safe(job.get("company", ""), 30),
        _safe(job.get("title", ""), 30),
        _safe(job.get("job_id", ""), 16)]))
    return os.path.join(out_dir, base or "job")


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")


# ---------------------------------------------------------------------------
# Prepare one job: tailor, build files, open the posting.
# ---------------------------------------------------------------------------
def prepare_job(job, cv_struct, cover_reference, out_dir=None,
                open_browser=True):
    """
    Tailor the CV + cover note for one job and stage the application files.

    Returns a result dict:
      {
        "ok": bool, "error": str,
        "folder": str, "cv_path": str, "is_pdf": bool,
        "cover_text": str, "fabrication_flags": [str, ...],
        "job": job,
      }
    Never raises except PermissionError (a staged file open in Word/a viewer),
    which the caller handles like the tracker's locked-file case.
    """
    out_dir = out_dir or config.APPLICATIONS_DIR
    result = {"ok": False, "error": "", "folder": "", "cv_path": "",
              "is_pdf": False, "cover_text": "", "fabrication_flags": [],
              "job": job}

    jd_text = jdfetch.get_jd(job)
    tailored = cvtailor.tailor(jd_text, cv_struct, job, cover_reference)
    if not tailored.get("ok"):
        result["error"] = tailored.get("error", "tailoring failed")
        return result

    folder = _folder_for(job, out_dir)
    os.makedirs(folder, exist_ok=True)

    cv_path, is_pdf = cvdoc.build_tailored_pdf(
        cv_struct, tailored, folder, "tailored_cv")

    cover_text = tailored.get("cover_note", "")
    if cover_text:
        _write_text(os.path.join(folder, "cover_note.txt"), cover_text)

    details = (
        f"Title:   {job.get('title','')}\n"
        f"Company: {job.get('company','')}\n"
        f"Source:  {job.get('source','')}\n"
        f"Link:    {job.get('job_link','')}\n"
        f"Location:{job.get('location','')}\n\n"
        f"Tailoring notes: {tailored.get('notes','')}\n\n"
        f"=== JOB DESCRIPTION ===\n{jd_text or '(could not retrieve JD)'}\n")
    _write_text(os.path.join(folder, "job_details.txt"), details)
    _write_text(os.path.join(folder, "apply_link.txt"),
                job.get("job_link", "") + "\n")

    flags = tailored.get("fabrication_flags", []) or []
    if flags:
        _write_text(os.path.join(folder, "REVIEW_fabrication_flags.txt"),
                    "Review these — tokens in the tailored output not found on "
                    "your master CV:\n\n- " + "\n- ".join(flags) + "\n")

    if open_browser and job.get("job_link"):
        try:
            webbrowser.open(job["job_link"])
        except Exception:  # noqa: BLE001
            pass  # opening a browser is a convenience, never a failure

    result.update({"ok": True, "folder": folder, "cv_path": cv_path,
                   "is_pdf": is_pdf, "cover_text": cover_text,
                   "fabrication_flags": flags})
    return result


# ---------------------------------------------------------------------------
# Job selection.
# ---------------------------------------------------------------------------
def _already_actioned(row):
    applied = str(row.get("Applied?", "")).strip().lower()
    qc = str(row.get("QC status", "")).strip().lower()
    return applied in ("yes", "y", "true") or qc in ("prepared", "auto-applied")


def _row_to_job(row):
    """Rebuild a job dict (the keys the pipeline uses) from a tracker row."""
    return {
        "source": row.get("Source", ""),
        "company": row.get("Company name", ""),
        "title": row.get("Job role name", ""),
        "job_link": row.get("Job link", ""),
        "job_id": row.get("Job ID", ""),
        "location": row.get("Location", ""),
        "role_type": row.get("Role type", ""),
        "eligibility": row.get("Eligibility", ""),
    }


def select_from_tracker(path=None, top=None, job_id=None, ignore_cap=False):
    """
    Read the master tracker and return the list of eligible, not-yet-actioned
    jobs to work on (honoring the APPLY cap unless ignore_cap/--all).
    """
    path = path or config.MASTER_TRACKER
    rows, _keys = tracker._load_existing(path)  # noqa: SLF001 (same package)
    jobs = []
    for row in rows:
        if job_id and str(row.get("Job ID", "")).strip() != str(job_id).strip():
            continue
        if _already_actioned(row):
            continue
        elig = str(row.get("Eligibility", "")).strip() or "Unknown"
        if elig not in config.APPLY_ELIGIBILITY_VERDICTS:
            continue
        jobs.append(_row_to_job(row))

    if job_id:
        return jobs
    if top:
        return jobs[:int(top)]
    if not ignore_cap:
        return jobs[:config.APPLY_MAX_JOBS]
    return jobs


# ---------------------------------------------------------------------------
# The main driver.
# ---------------------------------------------------------------------------
def process_jobs(jobs, confirm_fn=None, submit_confirm_fn=None,
                 open_browser=True, allow_submit=True, tracker_path=None):
    """
    Tailor + prepare all eligible jobs, auto-submit the supported subset (after a
    batch confirmation), and record the outcome in the tracker.

    confirm_fn(count) -> bool         : gate before spending Claude tokens.
    submit_confirm_fn(list) -> bool   : gate before any REAL submission; receives
                                        the list of jobs we can auto-submit.
    allow_submit                      : master toggle for this run (--prepare-only
                                        sets it False).

    Returns a summary dict: {"prepared", "submitted", "skipped", "flags",
    "details":[...], "error"}.
    """
    summary = {"prepared": 0, "submitted": 0, "skipped": 0, "flags": 0,
               "details": [], "error": ""}
    if not jobs:
        utils.info("No eligible, not-yet-actioned jobs to prepare.")
        return summary

    # Load the master CV once, and confirm at least one LLM provider is ready.
    cv_struct = cvdoc.load_master()
    if not cv_struct.get("ok"):
        summary["error"] = cv_struct.get("error", "master CV not loaded")
        utils.warn(summary["error"])
        return summary
    ok, reason, _labels = llm.available()
    if not ok:
        summary["error"] = reason
        utils.warn(reason)
        return summary
    cover_reference = cvtailor.load_cover_reference()
    if not cover_reference:
        utils.info("No cv/cover_reference.txt found — cover notes will be "
                   "omitted (never invented).")

    # Gate 1: cost confirmation.
    if confirm_fn is not None and not confirm_fn(len(jobs)):
        utils.info("Cancelled before tailoring — no tokens spent.")
        summary["skipped"] = len(jobs)
        return summary

    utils.step(f"Tailoring {len(jobs)} application(s) via {llm.active_label()}")

    # Load the applicant profile once (auto-submit needs it).
    prof = applicant.load()
    profile_ok = prof["ok"]
    if allow_submit and config.ENABLE_AUTO_SUBMIT and not profile_ok:
        utils.info(f"Auto-submit off this run: {prof['error']}")

    prepared_records = []   # (job, prepare_result)
    for i, job in enumerate(jobs, 1):
        utils.info(f"[{i}/{len(jobs)}] {job.get('title','')} "
                   f"@ {job.get('company','')}")
        try:
            res = prepare_job(job, cv_struct, cover_reference,
                              open_browser=open_browser)
        except PermissionError as e:
            summary["error"] = (f"A staged file is open (locked): {e}. Close it "
                                f"and re-run.")
            utils.warn(summary["error"])
            return summary
        if not res["ok"]:
            utils.warn(f"   skipped: {res['error']}")
            summary["skipped"] += 1
            continue
        if res["fabrication_flags"]:
            summary["flags"] += 1
            utils.warn(f"   review needed — possible added detail: "
                       f"{'; '.join(res['fabrication_flags'][:3])}")
        prepared_records.append((job, res))

    # Decide which prepared jobs are auto-submittable.
    can_submit = []
    if allow_submit and config.ENABLE_AUTO_SUBMIT and profile_ok:
        can_submit = [(job, res) for (job, res) in prepared_records
                      if autosubmit.can_autosubmit(job)]

    submitted_jobs = []
    if can_submit:
        # Gate 2: batch submit confirmation.
        approved = True
        if submit_confirm_fn is not None:
            approved = submit_confirm_fn([j for (j, _r) in can_submit])
        if approved:
            utils.step(f"Auto-submitting {len(can_submit)} application(s) to "
                       f"supported boards (Greenhouse/Lever, no captcha)")
            for job, res in can_submit:
                r = autosubmit.submit(job, prof["profile"], res["cv_path"],
                                      res["cover_text"])
                if r["ok"]:
                    utils.ok(f"   submitted: {job.get('title','')} "
                             f"@ {job.get('company','')} — {r['confirmation']}")
                    submitted_jobs.append(job)
                    summary["details"].append(
                        {"job": job, "action": "submitted",
                         "confirmation": r["confirmation"],
                         "folder": res["folder"]})
                else:
                    utils.warn(f"   couldn't auto-submit "
                               f"({r['reason']}) — prepared instead.")
        else:
            utils.info("Batch submit declined — all jobs prepared, none "
                       "submitted.")

    # Record outcomes: submitted -> mark_applied; everything else -> mark_prepared.
    submitted_keys = {id(j) for j in submitted_jobs}
    prepared_only = [(job, res) for (job, res) in prepared_records
                     if id(job) not in submitted_keys]

    try:
        for job in submitted_jobs:
            note = ""
            for d in summary["details"]:
                if d["job"] is job:
                    note = f"Auto-applied — {d['confirmation']}"
                    break
            tracker.mark_applied([job], note=note, path=tracker_path)
            summary["submitted"] += 1
        for job, res in prepared_only:
            tracker.mark_prepared([job], note=f"Prepared — {res['folder']}",
                                  path=tracker_path)
            summary["prepared"] += 1
            summary["details"].append(
                {"job": job, "action": "prepared", "folder": res["folder"]})
    except PermissionError as e:
        summary["error"] = (f"master_tracker.xlsx is open in Excel — couldn't "
                            f"record results ({e}). Close it and re-run.")
        utils.warn(summary["error"])
        return summary

    utils.step("Done")
    utils.ok(f"Submitted {summary['submitted']}, prepared "
             f"{summary['prepared']}, skipped {summary['skipped']}. "
             f"Files in {config.APPLICATIONS_DIR}/")
    if summary["flags"]:
        utils.warn(f"{summary['flags']} application(s) had possible-added-detail "
                   f"flags — check their REVIEW_fabrication_flags.txt.")
    return summary


# ---------------------------------------------------------------------------
# Standalone entry point.
# ---------------------------------------------------------------------------
def _console_confirm(count):
    est_low = count * 0.02   # rough per-tailoring cost estimate (USD)
    est_high = count * 0.06
    ans = input(f"\nTailor {count} application(s) with Claude "
                f"(~${est_low:.2f}-${est_high:.2f})? [y/N] ").strip().lower()
    return ans in ("y", "yes")


def _console_submit_confirm(jobs):
    print(f"\nThese {len(jobs)} job(s) can be auto-submitted for real "
          f"(no captcha/login):")
    for j in jobs:
        print(f"   - {j.get('title','')} @ {j.get('company','')} "
              f"[{j.get('source','')}]")
    ans = input("Submit all of these now? [y/N] ").strip().lower()
    return ans in ("y", "yes")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Tailor + prepare/apply to eligible jobs from the tracker.")
    parser.add_argument("--all", action="store_true",
                        help="ignore the per-run safety cap")
    parser.add_argument("--top", type=int, default=None,
                        help="only the top N eligible jobs")
    parser.add_argument("--job-id", default=None,
                        help="only the job with this Job ID")
    parser.add_argument("--prepare-only", action="store_true",
                        help="never auto-submit this run (prepare everything)")
    args = parser.parse_args(argv)

    jobs = select_from_tracker(top=args.top, job_id=args.job_id,
                               ignore_cap=args.all)
    if not jobs:
        print("No eligible, not-yet-actioned jobs found in "
              f"{config.MASTER_TRACKER}. Run a search first, or check the "
              "Eligibility / QC status columns.")
        return 0

    summary = process_jobs(
        jobs,
        confirm_fn=_console_confirm,
        submit_confirm_fn=_console_submit_confirm,
        open_browser=True,
        allow_submit=not args.prepare_only,
    )
    if summary["error"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
