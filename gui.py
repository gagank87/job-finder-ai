"""
gui.py — a basic Tkinter front-end over the same core pipeline.

This is the graphical equivalent of the interactive CLI (jobfinder.py). It:
  * collects the same `settings` dict via widgets (roles, experience levels,
    countries + cities, sources, career/Workday companies),
  * runs core.run_search() on a WORKER THREAD so the window stays responsive,
  * streams the pipeline's narration into a scrolling log (via utils' output
    sink — the same lines the CLI prints),
  * shows the results in a sortable table that acts as the QC gate,
  * "Approve & Save" writes the per-run workbook + merges into the master
    tracker; "Discard" throws the fetch away. Both clear the run cache, so the
    exact same cache/auto-delete rules as the CLI apply (see runcache.py).

Tkinter ships with Python, so there is no extra dependency. Launch with:
    python gui.py
or  python jobfinder.py --gui

Non-negotiables are unchanged: this is only a front-end. Every link is still
validated live inside core.run_search before it ever reaches this table; no
link is fabricated; nothing is written until you approve.
"""

import queue
import re
import threading
from datetime import datetime

import tkinter as tk
from tkinter import messagebox, ttk

import apply
import autosubmit
import config
import cookies
import core
import cvprofile
import excelio
import llm
import runcache
import settingsio
import tracker
import utils


# Columns shown in the results table (header, job-dict key, width px).
_TABLE_COLUMNS = [
    ("Company", "company", 150),
    ("Title", "title", 240),
    ("Source", "source", 110),
    ("Type", "role_type", 90),
    ("Location", "location", 150),
    ("Match", "match_score", 60),
    ("Eligible", "eligibility", 90),
    ("Salary", "salary", 110),
    ("Link", "job_link", 320),
]


def _split_career_entries(raw):
    """Split the "Career companies" box into individual names OR URLs.

    Tolerates comma-, newline-, and (for URLs pasted together) whitespace-
    separated entries, and strips trailing sentence punctuation off URL entries
    so a pasted 'https://…/careers.' still resolves. Names are left untouched.
    """
    entries = []
    for chunk in re.split(r"[,\n]", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Multiple URLs pasted into one field (e.g. "urlA. urlB") -> split them.
        if chunk.lower().count("http") > 1:
            entries.extend(p for p in chunk.split() if p.strip())
        else:
            entries.append(chunk)
    cleaned = []
    for e in entries:
        e = e.strip()
        if re.match(r"https?://", e, re.I):
            e = e.rstrip(" ./,;")   # drop trailing sentence punctuation
        if e:
            cleaned.append(e)
    return cleaned


class JobFinderGUI:
    def __init__(self, root):
        self.root = root
        root.title("Job Finder v5")
        root.geometry("1100x760")

        # Thread-safe channel for narration lines produced on the worker thread.
        self._log_queue = queue.Queue()
        self._results = None          # populated when a search finishes
        self._worker = None

        self._build_inputs()
        self._build_log()
        # Actions are built (and pinned to the bottom) BEFORE the results table,
        # so the "Approve & Save" / "Prepare / apply" bar always reserves its
        # space and can never be clipped off-screen by the expanding table.
        self._build_actions()
        self._build_results()

        # Load cookies once (unlocks the LinkedIn hiring-team link when present).
        loaded = cookies.load_into_session()
        if loaded:
            self._append_log(f"   [ok] Loaded cookies for: {', '.join(loaded)}")

        # Offer to resume an interrupted fetch, exactly like the CLI does.
        self._maybe_offer_resume()

        self.root.after(120, self._drain_log_queue)

    # ------------------------------------------------------------------ inputs
    def _build_inputs(self):
        frm = ttk.LabelFrame(self.root, text="Search settings")
        frm.pack(fill="x", padx=10, pady=(10, 6))

        # Roles (comma-separated; prefilled with the CV defaults).
        ttk.Label(frm, text="Roles (comma-separated):").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        self.roles_var = tk.StringVar(value=", ".join(cvprofile.DEFAULT_ROLES))
        ttk.Entry(frm, textvariable=self.roles_var, width=90).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=6, pady=4)

        # Experience levels (multi-check).
        ttk.Label(frm, text="Experience levels:").grid(
            row=1, column=0, sticky="nw", padx=6, pady=4)
        exp_frame = ttk.Frame(frm)
        exp_frame.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=4)
        self.level_vars = {}
        for i, (_k, (label, code)) in enumerate(config.EXPERIENCE_LEVELS.items()):
            var = tk.BooleanVar(value=code in ("2", "3"))  # Entry+Associate default
            self.level_vars[code] = var
            ttk.Checkbutton(exp_frame, text=label, variable=var).grid(
                row=0, column=i, sticky="w", padx=(0, 10))

        # Countries (multi-check) + a per-run cities box.
        ttk.Label(frm, text="Countries:").grid(
            row=2, column=0, sticky="nw", padx=6, pady=4)
        ctry_frame = ttk.Frame(frm)
        ctry_frame.grid(row=2, column=1, columnspan=3, sticky="w", padx=6, pady=4)
        self.country_vars = {}
        for i, name in enumerate(config.COUNTRIES):
            var = tk.BooleanVar(value=(name == config.COUNTRIES[0]))
            self.country_vars[name] = var
            ttk.Checkbutton(ctry_frame, text=name, variable=var).grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 12))

        ttk.Label(frm, text="Cities (comma-separated, optional):").grid(
            row=3, column=0, sticky="w", padx=6, pady=4)
        self.cities_var = tk.StringVar(value="")
        ttk.Entry(frm, textvariable=self.cities_var, width=90).grid(
            row=3, column=1, columnspan=3, sticky="we", padx=6, pady=4)

        # Sources (multi-check).
        ttk.Label(frm, text="Sources:").grid(
            row=4, column=0, sticky="nw", padx=6, pady=4)
        src_frame = ttk.Frame(frm)
        src_frame.grid(row=4, column=1, columnspan=3, sticky="w", padx=6, pady=4)
        self.source_vars = {}
        enabled = [(k, lbl) for k, (lbl, on) in config.SOURCES.items() if on]
        for i, (key, label) in enumerate(enabled):
            var = tk.BooleanVar(value=True)
            self.source_vars[key] = var
            ttk.Checkbutton(src_frame, text=key, variable=var).grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 12))

        # Career companies (auto-detected ATS / JSON-LD) — names OR pasted URLs.
        ttk.Label(frm, text="Career companies (name or URL, comma-separated):").grid(
            row=5, column=0, sticky="w", padx=6, pady=4)
        self.career_var = tk.StringVar(value="")
        ttk.Entry(frm, textvariable=self.career_var, width=90).grid(
            row=5, column=1, columnspan=3, sticky="we", padx=6, pady=4)
        ttk.Label(frm, foreground="#666",
                  text="Names auto-detect Greenhouse/Lever/Ashby/SmartRecruiters. "
                       "You can also paste a careers/ATS URL here directly (it's "
                       "read via the page's ATS board or schema.org JobPosting "
                       "data). Enterprise portals (iCIMS/Taleo/SuccessFactors) "
                       "can't be fetched — for Workday, paste its URL below.").grid(
            row=5, column=1, columnspan=3, sticky="w", padx=6, pady=(28, 0))

        # Workday: "Name | URL" one per line (per-tenant, can't be guessed).
        ttk.Label(frm,
                  text="Workday (Name | URL, one per line):\n"
                       "e.g. KPMG | https://kpmg.wd1.myworkdayjobs.com/…").grid(
            row=6, column=0, sticky="nw", padx=6, pady=4)
        self.workday_text = tk.Text(frm, height=3, width=88)
        self.workday_text.grid(row=6, column=1, columnspan=3, sticky="we",
                               padx=6, pady=4)

        frm.columnconfigure(1, weight=1)

        # Run + Save-settings buttons live with the inputs.
        btns = ttk.Frame(frm)
        btns.grid(row=7, column=1, columnspan=3, sticky="w", padx=6, pady=(2, 8))
        self.run_btn = ttk.Button(btns, text="Search", command=self._on_search)
        self.run_btn.pack(side="left", padx=(0, 8))
        self.save_settings_btn = ttk.Button(
            btns, text="Save settings", command=self._on_save_settings)
        self.save_settings_btn.pack(side="left", padx=(0, 12))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(btns, textvariable=self.status_var).pack(side="left")

    # --------------------------------------------------------------------- log
    def _build_log(self):
        frm = ttk.LabelFrame(self.root, text="Progress")
        frm.pack(fill="both", expand=False, padx=10, pady=6)
        self.log_text = tk.Text(frm, height=9, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frm, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

    # ----------------------------------------------------------------- results
    def _build_results(self):
        frm = ttk.LabelFrame(self.root, text="Results (review before saving)")
        frm.pack(fill="both", expand=True, padx=10, pady=6)
        cols = [key for _h, key, _w in _TABLE_COLUMNS]
        self.tree = ttk.Treeview(frm, columns=cols, show="headings",
                                 selectmode="browse")
        for header, key, width in _TABLE_COLUMNS:
            self.tree.heading(key, text=header,
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frm, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self._sort_reverse = False

    def _build_actions(self):
        frm = ttk.Frame(self.root)
        # Pinned to the bottom of the window so it's always visible even when
        # the results table grows or the window is maximized.
        frm.pack(side="bottom", fill="x", padx=10, pady=(6, 10))
        self.approve_btn = ttk.Button(frm, text="Approve & Save",
                                      command=self._on_approve, state="disabled")
        self.approve_btn.pack(side="left", padx=(0, 8))
        self.discard_btn = ttk.Button(frm, text="Discard",
                                      command=self._on_discard, state="disabled")
        self.discard_btn.pack(side="left")
        # Tailor + prepare/apply to the eligible jobs in the master tracker.
        self.apply_btn = ttk.Button(frm, text="Prepare / apply (eligible)",
                                    command=self._on_apply)
        self.apply_btn.pack(side="left", padx=(8, 0))
        self.summary_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.summary_var).pack(side="left", padx=12)

    # --------------------------------------------------------------- log plumbing
    def _append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_log_queue(self):
        """Pull worker-thread narration into the log widget (main thread only)."""
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log_queue)

    # ------------------------------------------------------------ settings read
    def _collect_settings(self):
        roles = [r.strip() for r in self.roles_var.get().split(",") if r.strip()]
        roles = roles or list(cvprofile.DEFAULT_ROLES)

        level_codes = [code for code, var in self.level_vars.items()
                       if var.get()]
        if not level_codes:
            level_codes = ["2"]  # default Entry level

        countries = [name for name, var in self.country_vars.items()
                     if var.get()]
        if not countries:
            countries = [config.COUNTRIES[0]]

        cities = [c.strip() for c in self.cities_var.get().split(",")
                  if c.strip()]
        queries = []
        for country in countries:
            if country.lower().startswith("remote"):
                queries.append(country)
            elif cities:
                queries.extend(f"{c}, {country}" for c in cities)
            else:
                queries.append(country)

        sources = {key for key, var in self.source_vars.items() if var.get()}

        career_companies = _split_career_entries(self.career_var.get())

        workday_urls = []
        for line in self.workday_text.get("1.0", "end").splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            name, url = line.split("|", 1)
            name, url = name.strip(), url.strip()
            if name and url:
                workday_urls.append((name, url))

        return {
            "roles": roles,
            "level_codes": level_codes,
            "countries": countries,
            "country": countries[0],
            "queries": queries,
            "sources": sources,
            "career_companies": career_companies,
            "workday_urls": workday_urls,
        }

    # ---------------------------------------------------------- save settings
    def _on_save_settings(self):
        """
        Write the current search settings to settings.json so a headless /
        scheduled run reproduces exactly THESE selections (roles, levels,
        countries + cities, sources, companies) — never a hardcoded default.
        """
        settings = self._collect_settings()
        try:
            path = settingsio.save(settings)
        except OSError as e:
            messagebox.showerror("Couldn't save settings", str(e))
            return
        countries = ", ".join(settings.get("countries") or [])
        cities = ", ".join(
            c.strip() for c in self.cities_var.get().split(",") if c.strip())
        loc = countries + (f" (cities: {cities})" if cities else "")
        self.status_var.set(f"Settings saved to {path}.")
        messagebox.showinfo(
            "Settings saved",
            f"Saved your current search to:\n{path}\n\n"
            f"Locations: {loc or '(none)'}\n"
            f"Sources:   {', '.join(sorted(settings.get('sources') or []))}\n\n"
            f"A scheduled run will use exactly these settings:\n"
            f"    python jobfinder.py --headless\n\n"
            f"(It reads settings.json — no prompts, no QC gate — merges only "
            f"new jobs into the master tracker.)")

    # -------------------------------------------------------------- resume seam
    def _maybe_offer_resume(self):
        payload = runcache.load()
        if not payload:
            return
        when = payload.get("fetched_at", "an earlier run")
        total = payload.get("total", 0)
        resume = messagebox.askyesno(
            "Resume last fetch?",
            f"A previous fetch was interrupted before you approved it.\n\n"
            f"Saved {when} — {total} job(s), already fetched and filtered.\n\n"
            f"Resume from that fetch (skip re-fetching)?")
        if resume:
            self._results = payload
            self._populate_results(payload)
            self._append_log("   Resumed from the cached fetch. Review below.")
        else:
            runcache.clear()
            self._append_log("   Discarded the saved fetch; starting fresh.")

    # --------------------------------------------------------------- search run
    def _on_search(self):
        if self._worker and self._worker.is_alive():
            return
        settings = self._collect_settings()
        # Clear any prior results/table.
        self._results = None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.approve_btn.configure(state="disabled")
        self.discard_btn.configure(state="disabled")
        self.summary_var.set("")
        self.run_btn.configure(state="disabled")
        self.status_var.set("Searching… (this can take a few minutes)")

        self._worker = threading.Thread(
            target=self._search_worker, args=(settings,), daemon=True)
        self._worker.start()

    def _search_worker(self, settings):
        # Route narration to our thread-safe queue for the duration of the run.
        utils.set_output_sink(self._log_queue.put)
        try:
            results = core.run_search(settings, ask_url_fn=None)
            fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            runcache.save(results, settings, fetched_at)
            # Hand back to the UI thread.
            self.root.after(0, lambda: self._on_search_done(results))
        except Exception as e:  # noqa: BLE001
            self._log_queue.put(f"   [!] Search failed: {e}")
            self.root.after(0, lambda: self._on_search_failed(str(e)))
        finally:
            utils.reset_output_sink()

    def _on_search_failed(self, msg):
        self.run_btn.configure(state="normal")
        self.status_var.set("Search failed.")
        messagebox.showerror("Search failed", msg)

    def _on_search_done(self, results):
        self._results = results
        self.run_btn.configure(state="normal")
        total = results.get("total", 0)
        self.status_var.set(f"Done — {total} live job(s).")
        if total == 0:
            self.summary_var.set("No live jobs matched. Nothing to save.")
            runcache.clear()   # nothing to resume
            self.approve_btn.configure(state="disabled")
            self.discard_btn.configure(state="disabled")
            return
        self._populate_results(results)

    def _populate_results(self, results):
        jobs = (results.get("linkedin", []) + results.get("career", [])
                + results.get("other", []))
        for item in self.tree.get_children():
            self.tree.delete(item)
        # Rank by fit score when present, else title match — same as the CLI QC.
        jobs_sorted = sorted(
            jobs,
            key=lambda j: (j.get("fit_score") if j.get("fit_score") not in
                           (None, "") else j.get("match_score", 0)),
            reverse=True)
        for j in jobs_sorted:
            row = []
            for _h, key, _w in _TABLE_COLUMNS:
                if key == "remote":
                    row.append("Yes" if j.get("remote") else "No")
                else:
                    row.append(j.get(key, ""))
            self.tree.insert("", "end", values=row)
        self.summary_var.set(
            f"{len(jobs_sorted)} job(s): "
            f"{len(results.get('linkedin', []))} LinkedIn, "
            f"{len(results.get('career', []))} career, "
            f"{len(results.get('other', []))} other. "
            f"Review, then Approve & Save or Discard.")
        self.approve_btn.configure(state="normal")
        self.discard_btn.configure(state="normal")

    def _sort_by(self, key):
        rows = [(self.tree.set(iid, key), iid)
                for iid in self.tree.get_children("")]

        def sort_key(pair):
            val = pair[0]
            try:
                return (0, float(val))
            except (TypeError, ValueError):
                return (1, str(val).lower())

        rows.sort(key=sort_key, reverse=self._sort_reverse)
        for idx, (_val, iid) in enumerate(rows):
            self.tree.move(iid, "", idx)
        self._sort_reverse = not self._sort_reverse

    # ------------------------------------------------------------- save/discard
    def _on_approve(self):
        if not self._results:
            return
        r = self._results
        linkedin_jobs = r.get("linkedin", [])
        career_jobs = r.get("career", [])
        other_jobs = r.get("other", [])
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            path = excelio.write_workbook(linkedin_jobs, career_jobs,
                                          other_jobs, fetched_at)
            added, master_total, _new = tracker.merge(
                {"LinkedIn": linkedin_jobs,
                 "Career Pages": career_jobs,
                 "All Sources": other_jobs},
                fetched_at)
        except PermissionError as e:
            locked = getattr(e, "filename", "") or "an output file"
            messagebox.showerror(
                "File is open",
                f"Couldn't write '{locked}' — it looks open in Excel "
                f"(Windows locks open files).\n\nClose it, then click "
                f"'Approve & Save' again. Your fetch is still cached — nothing "
                f"was lost.")
            return   # keep the cache so the retry (or a relaunch) still works
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Save failed", str(e))
            return
        runcache.clear()   # written to master -> drop the cache
        self.approve_btn.configure(state="disabled")
        self.discard_btn.configure(state="disabled")
        self.summary_var.set(f"Saved. +{added} new in master ({master_total} total).")
        messagebox.showinfo(
            "Saved",
            f"This run saved to:\n{path}\n\n"
            f"Master tracker '{config.MASTER_TRACKER}': +{added} new job(s), "
            f"{master_total} total (no duplicates; your edits preserved).")

    # ------------------------------------------------------- prepare / apply
    def _on_apply(self):
        """
        Tailor + prepare/apply to the eligible jobs already in the master
        tracker. Both confirmations are resolved HERE on the main thread (Tkinter
        dialogs must not run on the worker), then the tailoring + submission run
        on a worker thread with those decisions baked in.
        """
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Busy", "A search or apply run is already in "
                                        "progress. Please wait for it to finish.")
            return

        jobs = apply.select_from_tracker()
        if not jobs:
            messagebox.showinfo(
                "Nothing to prepare",
                "No eligible, not-yet-actioned jobs found in "
                f"{config.MASTER_TRACKER}.\n\nRun a search and Approve & Save "
                "first, or check the Eligibility / QC status columns.")
            return

        # Gate 1: cost confirmation.
        if not messagebox.askyesno(
                "Tailor these applications?",
                f"Tailor {len(jobs)} application(s) via {llm.active_label()}?\n\n"
                f"This calls the language model (a small cost per job on paid "
                f"providers; free on Groq/Gemini) to rewrite your CV + draft "
                f"cover notes. Nothing is submitted yet."):
            return

        # Gate 2: batch auto-submit confirmation — resolved up front so no dialog
        # is needed on the worker thread. We show which jobs *could* be submitted
        # for real; the actual submit still verifies receipt before recording it.
        submittable = [j for j in jobs if autosubmit.can_autosubmit(j)]
        allow_submit = False
        if submittable and config.ENABLE_AUTO_SUBMIT:
            listing = "\n".join(
                f"   • {j.get('title','')} @ {j.get('company','')} "
                f"[{j.get('source','')}]" for j in submittable[:20])
            more = ("\n   … and more" if len(submittable) > 20 else "")
            allow_submit = messagebox.askyesno(
                "Auto-submit these for real?",
                f"{len(submittable)} of the {len(jobs)} job(s) are on boards we "
                f"can submit to WITHOUT a captcha/login:\n\n{listing}{more}\n\n"
                f"Submit these for real once tailored? (They'll be recorded as "
                f"Applied only on a confirmed response. Everything else is just "
                f"prepared for you.)\n\nChoose No to prepare everything without "
                f"submitting.")

        self.apply_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self.status_var.set(f"Preparing {len(jobs)} application(s)…")

        self._worker = threading.Thread(
            target=self._apply_worker, args=(jobs, allow_submit), daemon=True)
        self._worker.start()

    def _apply_worker(self, jobs, allow_submit):
        utils.set_output_sink(self._log_queue.put)
        try:
            summary = apply.process_jobs(
                jobs,
                confirm_fn=None,          # already confirmed on the main thread
                submit_confirm_fn=None,   # already confirmed on the main thread
                open_browser=True,
                allow_submit=allow_submit)
            self.root.after(0, lambda: self._on_apply_done(summary))
        except Exception as e:  # noqa: BLE001
            self._log_queue.put(f"   [!] Prepare/apply failed: {e}")
            self.root.after(0, lambda: self._on_apply_failed(str(e)))
        finally:
            utils.reset_output_sink()

    def _on_apply_done(self, summary):
        self.apply_btn.configure(state="normal")
        self.run_btn.configure(state="normal")
        if summary.get("error"):
            self.status_var.set("Prepare/apply stopped.")
            messagebox.showwarning("Prepare / apply", summary["error"])
            return
        self.status_var.set(
            f"Done — submitted {summary['submitted']}, prepared "
            f"{summary['prepared']}, skipped {summary['skipped']}.")
        flag_line = (f"\n\n{summary['flags']} application(s) had possible "
                     f"added-detail flags — check each folder's "
                     f"REVIEW_fabrication_flags.txt.") if summary["flags"] else ""
        messagebox.showinfo(
            "Prepare / apply complete",
            f"Submitted (confirmed): {summary['submitted']}\n"
            f"Prepared for you:       {summary['prepared']}\n"
            f"Skipped:                {summary['skipped']}\n\n"
            f"Files are in {config.APPLICATIONS_DIR}/{flag_line}")

    def _on_apply_failed(self, msg):
        self.apply_btn.configure(state="normal")
        self.run_btn.configure(state="normal")
        self.status_var.set("Prepare/apply failed.")
        messagebox.showerror("Prepare / apply failed", msg)

    def _on_discard(self):
        if not messagebox.askyesno(
                "Discard", "Discard this fetch without saving?"):
            return
        runcache.clear()   # rejected -> drop the cache (same rule as the CLI)
        self._results = None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.approve_btn.configure(state="disabled")
        self.discard_btn.configure(state="disabled")
        self.summary_var.set("Discarded — nothing written.")
        self.status_var.set("Ready.")


def launch():
    root = tk.Tk()
    JobFinderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    launch()
