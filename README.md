# CCSS — English Learning Platform 

CCSS is a Streamlit and SQLite learning site with separate student and teacher workflows. Students can practise categorized English questions, review their scores on the leaderboard, and share notes. Teachers can manage practice assignments, create or import question banks, and grade essays.
a
The interface uses an 18 px base font size.

## Features

- Student registration requires a unique email for password recovery. Passwords are stored as salted PBKDF2 hashes; successful and failed login attempts are recorded.
- Students can change their display name and password, inspect account details and recent login history, and reset forgotten passwords with single-use, 30-minute email links.
- Teacher login uses the shared login form with username `teacher` and default password `teacheremma123`. The teacher must change this password on first login. Set `CCSS_TEACHER_PASSWORD` before first startup or database setup to choose a different initial password; the first-login change is still required.
- The student practice page is named "練習題". Multiple-choice and fill-in-the-blank questions are automatically scored; essays are graded by a teacher. Completed questions are hidden from general practice, while teacher assignments continue to follow their own retake settings. Students can build a random practice set by category, difficulty, and question count. Each set is saved as a single-use quiz; submitting it again cannot create duplicate scores.
- Once a quiz is submitted, its answers are locked and cannot be edited. The result and history views show scores, correctness, the student's submitted selections, and available explanations without displaying editable answer fields. Correct answers and explanations are hidden when a teacher disables answer review for an assignment. Essay scores and feedback appear after teacher grading. Personal test history and recent results are retained.
- The leaderboard totals each student's best score for each question and ranks only by total score; students with equal scores share the same rank. Automatically graded multiple-choice and fill-in-the-blank attempts are used to calculate student answer accuracy. The student home page shows current score, rank, answer accuracy, teacher-assigned homework and due dates, latest teacher announcements, recent quiz scores, and a centered leaderboard.
- Students can spend 150 points per spin on the student-home-page prize wheel. Standard spins award 10–50 points; every 1,000 points of earned quiz score unlocks a special spin awarding 30–70 points. Spin outcomes are drawn and recorded server-side, and the net points are reflected in student totals and rankings.
- Students receive a daily sign-in reward once per server-local calendar day: 15 points normally, 30 on streak day 5, and 50 on streak day 10. Streak days 20, 30, 40, and so on unlock one free standard wheel spin. Missed calendar days reset the streak; reward points and wheel prizes are included in totals and rankings.
- Students can review each multiple-choice question's options and their own submitted selection after a quiz; teacher-controlled settings still determine whether correct answers and explanations are revealed. The essay center saves drafts, enforces a teacher-set minimum word count, keeps submitted versions, shows rubric and teacher feedback, and supports revision requests and sentence-level comments.
- The authenticated sidebar shows the latest teacher announcements in a bulletin, a compact leaderboard with the top five students and the signed-in student's rank, and the logout button beneath them.
- Teachers can create classes and select student members, manage student accounts, view answer accuracy, create/edit/publish/search/preview/archive questions, assign question sets with schedules and time limits, review submissions, adjust scores with an audit record, export assignment scores, create essay rubrics and word-count requirements, grade essays with overall and sentence-level feedback, request revisions, publish announcements, moderate reported notes, and review teacher action logs.
- Students can publish, edit, and delete their own sticky notes, reply to classmates, and report inappropriate notes.
- SQLite data is persisted at `ccss.db` by default. Set `CCSS_DB_PATH` to a persistent path on the Linux server. `python -m ccss_db` creates a consistent SQLite backup in `CCSS_BACKUP_DIR` (defaults to `backups/` beside the database) and keeps the newest 14 daily backups. The app also checks for a daily backup when it starts.

## Deploy the Vercel version

Vercel does not run Streamlit as a persistent web server, so this repository now includes a separate Vercel-native web client (`public/`) and FastAPI serverless API (`api/`). The existing `streamlit_app.py` Streamlit site remains available for Linux-server deployment. The Streamlit file uses a different name because Vercel reserves `app.py` as a Python function entrypoint. The Vercel version uses Supabase PostgreSQL; do not point it at the local SQLite file or rely on Vercel's temporary filesystem for user data.

### 1. Create and prepare Supabase

Create a Supabase project and copy its PostgreSQL connection string. Use a connection-pooler URL with SSL for the hosted app. Install the Vercel API dependencies locally and initialize the schema; optionally import the current SQLite users, questions, classes, assignments, notes, scores, and audit history:

```bash
python -m pip install -r requirements.txt
export DATABASE_URL='postgresql://...?...sslmode=require'
export CCSS_TEACHER_PASSWORD='use-a-strong-initial-password'
python scripts/setup_supabase.py --sqlite ccss.db
```

On Windows PowerShell, set the setup-only variables this way:

```powershell
$env:DATABASE_URL = "postgresql://...?...sslmode=require"
$env:CCSS_TEACHER_PASSWORD = "use-a-strong-initial-password"
python scripts\setup_supabase.py --sqlite ccss.db
```

The source SQLite database is only read and is not modified. Without `--sqlite`, the setup command creates a fresh database. If importing, run the setup once against an empty Supabase database. Password-reset tokens and in-progress quiz sessions are intentionally not migrated; learners should request new reset links and begin new quizzes after the move. Imported account password hashes are retained.

Generate a separate signing key for sessions:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 2. Configure Vercel

Import this Git repository in Vercel with the repository root as the project root. The `[tool.vercel].entrypoint` setting in `pyproject.toml` points Vercel to `api.index:app`, and `public/` contains the static client. Use the **Other** preset if prompted so framework detection does not select an unrelated Python file. Add these environment variables to the Vercel project (Production and any Preview environments that should work):

- `DATABASE_URL` — the Supabase PostgreSQL connection string, including SSL.
- `CCSS_SESSION_SECRET` — the random string generated above; keep it private and at least 32 characters.
- `CCSS_COOKIE_SECURE` — `true` (or leave unset) for the HTTPS site.
- `CCSS_PUBLIC_URL` — the deployed HTTPS URL, used in password-reset emails.
- `CCSS_TEACHER_PASSWORD` — optional override for the initial teacher password; defaults to `teacheremma123`.
- `CCSS_SMTP_HOST`, `CCSS_SMTP_PORT`, `CCSS_SMTP_FROM`, `CCSS_SMTP_USER`, `CCSS_SMTP_PASSWORD` — mail service settings for forgotten-password links.

Deploy the project and verify `https://YOUR-DOMAIN/api/health` returns `{"status":"ok"}`. Teachers and students use the shared login page; the initial teacher account is `teacher` / `teacheremma123` unless `CCSS_TEACHER_PASSWORD` is set. The teacher must change the initial password at first login. When the setup script encounters a legacy teacher account still awaiting its first password change with the old default password, it updates it to the configured initial password. Existing teacher passwords that have already been changed are preserved. Students can register from the login page.

### Vercel version scope

The Vercel client includes student registration/login, password changes and configured email resets, the practice/locked-submission flow, result review, score leaderboard, login rewards and wheel, essay submission/drafts/revisions and grading, notes/replies/reports, and teacher screens for student profiles and status, class membership, question-bank management, question search/edit/publish/archive, JSON/CSV/XLSX question import with validation preview, assignments and schedule/status management, submission statistics and score adjustments, CSV score export, grading, announcements, reports, and audit history. Vercel PDF extraction is limited to text-based PDFs up to 3 MB; scanned-PDF OCR requires a separate OCR service and is not included in this serverless deployment.

This is a migration of the Streamlit UI, not a way to run `streamlit run streamlit_app.py` on Vercel. The two clients use separate backends: the existing Streamlit client continues to use SQLite, while Vercel uses Supabase PostgreSQL. Once users start working in Vercel, new activity is stored in Supabase and is not automatically synchronized back to SQLite.

## Import a question bank

Teachers can view and download complete JSON and CSV examples directly on the question-import page. Imports support UTF-8 JSON, CSV, and XLSX. JSON supports either a top-level `questions` array or a complete exam object with `title`, `category`, `level`, and `questions`. Exam JSON accepts `single_choice`, `fill_blank`, and `essay` question types, uses each question's `question` text, and maps English difficulty values (`easy`, `medium`, `hard`) to the site's difficulty levels. Essay questions may include `rubric` and `min_word_count`. If no destination bank is selected, an exam `title` is used to create or reuse a question bank. CSV/XLSX headers are `category`, `scope`, `grade_level`, `difficulty`, `type`, `prompt`, `options`, `answer`, `accepted_answers`, `explanation`, `rubric`, `min_word_count`, and `points`; separate options and accepted answers with `|`. Each question uses this format:

```json
{
  "category": "Grammar",
  "difficulty": "初級",
  "type": "choice",
  "prompt": "Choose the correct sentence.",
  "options": ["She go to school.", "She goes to school."],
  "answer": "She goes to school.",
  "explanation": "Use goes with she.",
  "points": 5
}
```

For an essay question, set `"type": "essay"` and provide `category`, `difficulty`, `prompt`, and `points`; optionally include `rubric` and `min_word_count`. `options` and `answer` are not needed. The supported difficulty values are `初級`, `中級`, `中高級`, and `高級`. Imports are limited to 500 questions and 2 MB, validated as a whole, and written only when every question is valid.

Teachers can also upload a text-based or scanned PDF (up to 20 pages and 15 MB). The app extracts embedded text and uses Tesseract OCR for image-only pages. OCR text is a draft source that must be reviewed; it does not automatically generate questions. Install the OCR engine and English language data on Linux:

```bash
sudo apt install -y tesseract-ocr tesseract-ocr-eng
```

The Python dependencies for PDF rendering and OCR are listed in `requirements.txt`. Set `CCSS_TESSERACT_LANG=eng+chi_tra` and install the corresponding Traditional Chinese Tesseract language data if those PDFs need Chinese text recognition.

The teacher signs in through the shared login form with username `teacher` and default password `teacheremma123`, and must change the initial password before using the teacher workspace. A legacy teacher account that still has the old default password and is awaiting its first password change is migrated on startup; a password already changed by the teacher is preserved. Set `CCSS_TEACHER_PASSWORD` before the first application startup to choose a different initial password. Update both `streamlit_app.py` and `ccss_db.py` together when deploying application updates, then restart Streamlit. If startup reports that `ccss_db` has no `daily_backup`, verify the module path with `python -c "import ccss_db; print(ccss_db.__file__); print(hasattr(ccss_db, 'daily_backup'))"`; the server may be loading an older or different copy of the module. The app displays a warning and continues without a startup backup when the loaded module lacks that function.

## Run locally

```bash
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
python -m pip install -r requirements-streamlit.txt
python -m streamlit run streamlit_app.py
```

## Deploy to a Linux server with Streamlit

From the project directory on the Linux server:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-streamlit.txt
CCSS_DB_PATH=/var/lib/ccss/ccss.db .venv/bin/python -m streamlit run streamlit_app.py \
  --server.address 0.0.0.0 --server.port 8501
```

Before startup, create the database directory with administrator privileges and assign it to the same Linux account that will run Streamlit (replace `ccssuser` with that account):

```bash
sudo mkdir -p /var/lib/ccss/backups
sudo chown -R ccssuser:ccssuser /var/lib/ccss
```

Keep that directory on persistent storage. Daily backups are written to `/var/lib/ccss/backups`; set `CCSS_BACKUP_DIR` to choose another persistent location. Copy backups off the server regularly for disaster recovery. If Streamlit runs as a system service, use the service's configured user instead of your interactive login account.

For a scheduled backup even when nobody is using the website, add a daily cron job (adjust the project path and log path for your server):

```cron
0 2 * * * cd /opt/ccss && CCSS_DB_PATH=/var/lib/ccss/ccss.db CCSS_BACKUP_DIR=/var/lib/ccss/backups /opt/ccss/.venv/bin/python -m ccss_db >> /var/log/ccss-backup.log 2>&1
```

Password reset email requires a mail server. Configure these environment variables before startup:

```bash
CCSS_PUBLIC_URL=https://ccss.example.edu
CCSS_SMTP_HOST=smtp.example.edu
CCSS_SMTP_PORT=587
CCSS_SMTP_FROM=ccss@example.edu
CCSS_SMTP_USER=ccss@example.edu
CCSS_SMTP_PASSWORD=replace-with-a-secret
```

Port `587` uses STARTTLS; port `465` uses implicit TLS. Keep credentials in the server's secret manager or protected service environment, never in Git. The reset link is single-use and expires after 30 minutes. For a public deployment, put Streamlit behind HTTPS using a reverse proxy, firewall the Streamlit port, and protect server access. Set `CCSS_TEACHER_PASSWORD` before the first application startup if you do not want to use the initial `teacheremma123` password. Do not commit secrets or the production database.

## Tests

```bash
python -m unittest -v
```
