This folder holds YOUR personal application inputs. Both files below are
gitignored (they never get committed), and you supply them yourself.

1) master_cv.docx   (required for tailoring)
   Your master CV as a Word .docx. The tool reads it, and for each job it
   rewrites/reorders ONLY what is genuinely on it to match the job description
   — it never invents experience, skills, dates, or numbers. The tailored copy
   is delivered as a PDF (converted from a filled copy of this .docx, so your
   real fonts and layout are preserved).

   Put your file here as exactly:  cv/master_cv.docx

2) cover_reference.txt   (optional but recommended)
   A cover letter you have written before, in plain text. The tool mirrors its
   tone and structure when drafting a per-job cover note — kept measured and
   genuine, never arrogant or overconfident. If this file is absent, cover-note
   drafting is skipped (the tool won't invent a style).

   Put your reference here as:  cv/cover_reference.txt

Auto-submit also needs your apply details. Copy applicant_profile.sample.json
(in the project root) to secrets/applicant_profile.json and fill it in. Without
it, the tool prepares applications for you to submit rather than auto-submitting.

Your Claude API key is read from the ANTHROPIC_API_KEY environment variable
(recommended) or, if you prefer, from secrets/anthropic_key.txt. It is never
stored in the project and never printed.
