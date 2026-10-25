import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


DB_PATH = Path(os.environ.get("CCSS_DB_PATH", Path(__file__).resolve().parent / "ccss.db"))
DIFFICULTIES = ("初級", "中級","中高級", "高級")
QUESTION_TYPES = ("choice", "fill_blank", "essay")
PASSWORD_ITERATIONS = 310_000
RESET_TOKEN_TTL_MINUTES = 30
DEFAULT_TEACHER_PASSWORD = "teacheremma123"


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    os.chmod(DB_PATH, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _require_teacher(conn, teacher_id):
    teacher = conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND role = 'teacher' AND is_active = 1",
        (teacher_id,),
    ).fetchone()
    if teacher is None:
        raise ValueError("此操作僅限啟用中的教師帳號。")


def _audit(conn, actor_id, action, entity_type, entity_id="", details=None):
    conn.execute(
        """INSERT INTO audit_logs(actor_id, action, entity_type, entity_id, details)
           VALUES (?, ?, ?, ?, ?)""",
        (
            actor_id,
            action,
            entity_type,
            str(entity_id),
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def initialize_database():
    with database() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
                must_change_password INTEGER NOT NULL DEFAULT 0,
                email TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                class_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY,
                teacher_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(teacher_id, name)
            );
            CREATE TABLE IF NOT EXISTS class_memberships (
                class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(class_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS question_banks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY,
                bank_id INTEGER REFERENCES question_banks(id) ON DELETE SET NULL,
                category TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                grade_level TEXT NOT NULL DEFAULT '',
                difficulty TEXT NOT NULL,
                question_type TEXT NOT NULL CHECK(question_type IN ('choice', 'fill_blank', 'essay')),
                prompt TEXT NOT NULL,
                options TEXT NOT NULL DEFAULT '[]',
                answer TEXT NOT NULL DEFAULT '',
                accepted_answers TEXT NOT NULL DEFAULT '[]',
                explanation TEXT NOT NULL DEFAULT '',
                rubric TEXT NOT NULL DEFAULT '',
                min_word_count INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL CHECK(points > 0),
                is_published INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                response TEXT NOT NULL DEFAULT '',
                earned_points INTEGER,
                feedback TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                quiz_id TEXT REFERENCES quiz_sessions(id),
                assignment_id INTEGER REFERENCES assignment_sets(id)
            );
            CREATE TABLE IF NOT EXISTS assignments (
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                due_at TEXT,
                PRIMARY KEY(student_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id TEXT PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                question_ids TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('in_progress', 'submitted')),
                score INTEGER NOT NULL DEFAULT 0,
                max_score INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                submitted_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                assignment_id INTEGER REFERENCES assignment_sets(id)
            );
            CREATE TABLE IF NOT EXISTS wheel_spins (
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                wheel_type TEXT NOT NULL
                    CHECK(wheel_type IN ('standard', 'milestone', 'streak')),
                score_milestone INTEGER,
                streak_day INTEGER,
                points_spent INTEGER NOT NULL CHECK(points_spent IN (0, 150)),
                points_awarded INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(student_id, request_id),
                UNIQUE(student_id, score_milestone),
                UNIQUE(student_id, streak_day),
                CHECK(
                    (wheel_type = 'standard' AND score_milestone IS NULL
                        AND streak_day IS NULL AND points_spent = 150
                        AND points_awarded IN (10, 20, 30, 40, 50))
                    OR
                    (wheel_type = 'milestone' AND score_milestone IS NOT NULL
                        AND streak_day IS NULL AND points_spent = 150
                        AND points_awarded IN (30, 40, 50, 60, 70))
                    OR
                    (wheel_type = 'streak' AND score_milestone IS NULL
                        AND streak_day IS NOT NULL AND points_spent = 0
                        AND points_awarded IN (10, 20, 30, 40, 50))
                )
            );
            CREATE TABLE IF NOT EXISTS daily_login_rewards (
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                login_date TEXT NOT NULL,
                streak_day INTEGER NOT NULL CHECK(streak_day > 0),
                points_awarded INTEGER NOT NULL CHECK(points_awarded IN (15, 30, 50)),
                free_wheel_awarded INTEGER NOT NULL DEFAULT 0
                    CHECK(free_wheel_awarded IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(student_id, login_date)
            );
            CREATE TABLE IF NOT EXISTS login_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                username TEXT NOT NULL,
                succeeded INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY,
                teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS note_reports (
                id INTEGER PRIMARY KEY,
                note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'resolved')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(note_id, reporter_id)
            );
            CREATE TABLE IF NOT EXISTS question_reports (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'resolved')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(question_id, reporter_id)
            );
            CREATE TABLE IF NOT EXISTS assignment_sets (
                id INTEGER PRIMARY KEY,
                teacher_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                starts_at TEXT,
                due_at TEXT,
                time_limit_minutes INTEGER,
                allow_retake INTEGER NOT NULL DEFAULT 0,
                show_answers INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'published'
                    CHECK(status IN ('draft', 'published', 'closed', 'cancelled')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS assignment_recipients (
                assignment_id INTEGER NOT NULL REFERENCES assignment_sets(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY(assignment_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS assignment_questions (
                assignment_id INTEGER NOT NULL REFERENCES assignment_sets(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                PRIMARY KEY(assignment_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY,
                actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY,
                teacher_id INTEGER NOT NULL REFERENCES users(id),
                content_hash TEXT NOT NULL UNIQUE,
                imported_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY,
                author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS note_replies (
                id INTEGER PRIMARY KEY,
                note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS essay_drafts (
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(student_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS essay_versions (
                id INTEGER PRIMARY KEY,
                attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL CHECK(version_number > 0),
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted'
                    CHECK(status IN ('submitted', 'revision_requested', 'graded')),
                score INTEGER,
                feedback TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(attempt_id, version_number)
            );
            CREATE TABLE IF NOT EXISTS essay_comments (
                id INTEGER PRIMARY KEY,
                version_id INTEGER NOT NULL REFERENCES essay_versions(id) ON DELETE CASCADE,
                teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                quoted_text TEXT NOT NULL,
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS attempts_student_question
                ON attempts(student_id, question_id);
            """
        )
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "is_active" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "class_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN class_name TEXT NOT NULL DEFAULT ''")
        question_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(questions)")
        }
        for column, definition in (
            ("bank_id", "INTEGER REFERENCES question_banks(id) ON DELETE SET NULL"),
            ("scope", "TEXT NOT NULL DEFAULT ''"),
            ("grade_level", "TEXT NOT NULL DEFAULT ''"),
            ("is_published", "INTEGER NOT NULL DEFAULT 1"),
            ("created_by", "INTEGER REFERENCES users(id)"),
            ("accepted_answers", "TEXT NOT NULL DEFAULT '[]'"),
            ("rubric", "TEXT NOT NULL DEFAULT ''"),
            ("min_word_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in question_columns:
                conn.execute(f"ALTER TABLE questions ADD COLUMN {column} {definition}")
        question_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'questions'"
        ).fetchone()["sql"]
        if "'fill_blank'" not in question_sql:
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute(
                """CREATE TABLE questions_new (
                    id INTEGER PRIMARY KEY,
                    bank_id INTEGER REFERENCES question_banks(id) ON DELETE SET NULL,
                    category TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    grade_level TEXT NOT NULL DEFAULT '',
                    difficulty TEXT NOT NULL,
                    question_type TEXT NOT NULL
                        CHECK(question_type IN ('choice', 'fill_blank', 'essay')),
                    prompt TEXT NOT NULL,
                    options TEXT NOT NULL DEFAULT '[]',
                    answer TEXT NOT NULL DEFAULT '',
                    accepted_answers TEXT NOT NULL DEFAULT '[]',
                    explanation TEXT NOT NULL DEFAULT '',
                    rubric TEXT NOT NULL DEFAULT '',
                    min_word_count INTEGER NOT NULL DEFAULT 0,
                    points INTEGER NOT NULL CHECK(points > 0),
                    is_published INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER REFERENCES users(id),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """INSERT INTO questions_new
                   (id, bank_id, category, scope, grade_level, difficulty, question_type,
                    prompt, options, answer, accepted_answers, explanation, rubric,
                    min_word_count, points,
                    is_published, created_by, created_at)
                   SELECT id, bank_id, category, scope, grade_level, difficulty, question_type,
                          prompt, options, answer, accepted_answers, explanation, rubric,
                          min_word_count, points,
                          is_published, created_by, created_at
                   FROM questions"""
            )
            conn.execute("DROP TABLE questions")
            conn.execute("ALTER TABLE questions_new RENAME TO questions")
        wheel_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'wheel_spins'"
        ).fetchone()["sql"]
        if "'streak'" not in wheel_sql:
            conn.execute(
                """CREATE TABLE wheel_spins_new (
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    wheel_type TEXT NOT NULL
                        CHECK(wheel_type IN ('standard', 'milestone', 'streak')),
                    score_milestone INTEGER,
                    streak_day INTEGER,
                    points_spent INTEGER NOT NULL CHECK(points_spent IN (0, 150)),
                    points_awarded INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, request_id),
                    UNIQUE(student_id, score_milestone),
                    UNIQUE(student_id, streak_day),
                    CHECK(
                        (wheel_type = 'standard' AND score_milestone IS NULL
                            AND streak_day IS NULL AND points_spent = 150
                            AND points_awarded IN (10, 20, 30, 40, 50))
                        OR
                        (wheel_type = 'milestone' AND score_milestone IS NOT NULL
                            AND streak_day IS NULL AND points_spent = 150
                            AND points_awarded IN (30, 40, 50, 60, 70))
                        OR
                        (wheel_type = 'streak' AND score_milestone IS NULL
                            AND streak_day IS NOT NULL AND points_spent = 0
                            AND points_awarded IN (10, 20, 30, 40, 50))
                    )
                )"""
            )
            conn.execute(
                """INSERT INTO wheel_spins_new
                   (id, student_id, request_id, wheel_type, score_milestone,
                    points_spent, points_awarded, created_at)
                   SELECT id, student_id, request_id, wheel_type, score_milestone,
                          points_spent, points_awarded, created_at
                   FROM wheel_spins"""
            )
            conn.execute("DROP TABLE wheel_spins")
            conn.execute("ALTER TABLE wheel_spins_new RENAME TO wheel_spins")
        conn.execute(
            """INSERT OR IGNORE INTO essay_versions
               (attempt_id, version_number, content, status, score, feedback, submitted_at)
               SELECT a.id, 1, a.response,
                      CASE WHEN a.earned_points IS NULL THEN 'submitted' ELSE 'graded' END,
                      a.earned_points, a.feedback, a.submitted_at
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE q.question_type = 'essay'"""
        )
        assignment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(assignments)")
        }
        if "due_at" not in assignment_columns:
            conn.execute("ALTER TABLE assignments ADD COLUMN due_at TEXT")
        attempt_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(attempts)")
        }
        if "quiz_id" not in attempt_columns:
            conn.execute("ALTER TABLE attempts ADD COLUMN quiz_id TEXT REFERENCES quiz_sessions(id)")
        if "assignment_id" not in attempt_columns:
            conn.execute(
                "ALTER TABLE attempts ADD COLUMN assignment_id INTEGER REFERENCES assignment_sets(id)"
            )
        quiz_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(quiz_sessions)")
        }
        if "assignment_id" not in quiz_columns:
            conn.execute(
                "ALTER TABLE quiz_sessions ADD COLUMN assignment_id INTEGER REFERENCES assignment_sets(id)"
            )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS attempts_once_per_quiz_question
               ON attempts(quiz_id, question_id) WHERE quiz_id IS NOT NULL"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS wheel_spins_student
               ON wheel_spins(student_id, created_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS daily_login_rewards_student_date
               ON daily_login_rewards(student_id, login_date DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS essay_versions_attempt
               ON essay_versions(attempt_id, version_number DESC)"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS users_unique_student_email
               ON users(email COLLATE NOCASE) WHERE role = 'student' AND email != ''"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS login_events_user_created
               ON login_events(user_id, created_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS questions_bank_status
               ON questions(bank_id, is_published)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS audit_logs_created
               ON audit_logs(created_at DESC)"""
        )
        teacher = conn.execute(
            """SELECT id, password_hash, must_change_password FROM users
               WHERE username = 'teacher' AND role = 'teacher'"""
        ).fetchone()
        if teacher is None:
            initial_password = os.environ.get(
                "CCSS_TEACHER_PASSWORD", DEFAULT_TEACHER_PASSWORD
            )
            validate_password(initial_password)
            conn.execute(
                """INSERT INTO users
                   (username, display_name, password_hash, role, must_change_password)
                   VALUES ('teacher', 'Teacher', ?, 'teacher', 1)""",
                (hash_password(initial_password),),
            )
        elif teacher["must_change_password"] and verify_password(
            "teacher", teacher["password_hash"]
        ):
            initial_password = os.environ.get(
                "CCSS_TEACHER_PASSWORD", DEFAULT_TEACHER_PASSWORD
            )
            validate_password(initial_password)
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(initial_password), teacher["id"]),
            )


def validate_username(username):
    username = username.strip()
    if not 3 <= len(username) <= 32 or any(char.isspace() for char in username):
        raise ValueError("帳號需為 3 至 32 個字元，且不能包含空白。")
    return username


def validate_password(password):
    if len(password) < 8:
        raise ValueError("密碼至少需要 8 個字元。")


def validate_email(email):
    email = email.strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("請輸入有效的電子郵件地址，供忘記密碼時重設使用。")
    return email


def register_student(username, password, email):
    username = validate_username(username)
    validate_password(password)
    email = validate_email(email)
    with database() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO users(username, display_name, password_hash, role, email)
                   VALUES (?, ?, ?, 'student', ?)""",
                (username, username, hash_password(password), email),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("此帳號已被使用。") from exc
        return cursor.lastrowid


def authenticate(username, password):
    with database() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    if (
        row is None
        or not row["is_active"]
        or not verify_password(password, row["password_hash"])
    ):
        return None
    return dict(row)


def login(username, password):
    user = authenticate(username, password)
    with database() as conn:
        matched_user = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
        conn.execute(
            """INSERT INTO login_events(user_id, username, succeeded)
               VALUES (?, ?, ?)""",
            (
                user["id"] if user else (matched_user["id"] if matched_user else None),
                username.strip()[:32],
                int(user is not None),
            ),
        )
        if user and user["role"] == "student":
            user["daily_reward"] = _claim_daily_login_reward(conn, user["id"])
    return user


def _essay_word_count(content):
    return len(re.findall(r"\b[\w'-]+\b", content, flags=re.UNICODE))


def list_student_essays(student_id):
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError("只有啟用中的學生可以查看作文題目。")
        rows = conn.execute(
            """SELECT q.id AS question_id, q.category, q.difficulty, q.prompt,
                      q.points, q.rubric, q.min_word_count,
                      a.id AS attempt_id, a.earned_points, a.feedback,
                      v.version_number, v.status AS essay_status,
                      v.content AS submitted_content, v.submitted_at,
                      d.content AS draft_content
               FROM questions q
               LEFT JOIN attempts a ON a.id = (
                   SELECT latest.id FROM attempts latest
                   WHERE latest.student_id = ? AND latest.question_id = q.id
                   ORDER BY latest.id DESC LIMIT 1
               )
               LEFT JOIN essay_versions v ON v.id = (
                   SELECT latest_version.id FROM essay_versions latest_version
                   WHERE latest_version.attempt_id = a.id
                   ORDER BY latest_version.version_number DESC LIMIT 1
               )
               LEFT JOIN essay_drafts d
                 ON d.student_id = ? AND d.question_id = q.id
               WHERE q.question_type = 'essay'
                 AND (q.is_published = 1 OR a.id IS NOT NULL)
               ORDER BY q.is_published DESC, q.category, q.id""",
            (student_id, student_id),
        )
        return [dict(row) for row in rows]


def save_essay_draft(student_id, question_id, content):
    content = str(content)
    if len(content) > 20_000:
        raise ValueError("作文草稿不可超過 20000 個字元。")
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
            (student_id,),
        ).fetchone()
        question = conn.execute(
            """SELECT question_type, is_published FROM questions
               WHERE id = ?""",
            (question_id,),
        ).fetchone()
        if student is None or question is None or question["question_type"] != "essay":
            raise ValueError("學生帳號或作文題目不存在。")
        latest = conn.execute(
            """SELECT v.status
               FROM attempts a JOIN essay_versions v ON v.attempt_id = a.id
               WHERE a.student_id = ? AND a.question_id = ?
               ORDER BY v.version_number DESC LIMIT 1""",
            (student_id, question_id),
        ).fetchone()
        if not question["is_published"] and latest is None:
            raise ValueError("此作文題目目前無法作答。")
        if latest and latest["status"] != "revision_requested":
            raise ValueError("作文已提交，只有老師退回修改後才能編輯。")
        conn.execute(
            """INSERT INTO essay_drafts(student_id, question_id, content, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(student_id, question_id) DO UPDATE SET
                   content = excluded.content, updated_at = CURRENT_TIMESTAMP""",
            (student_id, question_id, content),
        )


def submit_essay(student_id, question_id, content, assignment_id=None):
    content = str(content).strip()
    if not content or len(content) > 20_000:
        raise ValueError("作文需為 1 至 20000 個字元。")
    with database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
            (student_id,),
        ).fetchone()
        question = conn.execute(
            """SELECT question_type, is_published, min_word_count
               FROM questions WHERE id = ?""",
            (question_id,),
        ).fetchone()
        if student is None or question is None or question["question_type"] != "essay":
            raise ValueError("學生帳號或作文題目不存在。")
        if not question["is_published"]:
            raise ValueError("此作文題目目前無法提交。")
        word_count = _essay_word_count(content)
        if word_count < question["min_word_count"]:
            raise ValueError(
                f"作文至少需要 {question['min_word_count']} 個英文單字；目前 {word_count} 字。"
            )
        if assignment_id is not None:
            assignment = conn.execute(
                """SELECT s.status, s.starts_at, s.due_at
                   FROM assignment_sets s
                   JOIN assignment_recipients r ON r.assignment_id = s.id
                   JOIN assignment_questions aq ON aq.assignment_id = s.id
                   WHERE s.id = ? AND r.student_id = ? AND aq.question_id = ?""",
                (assignment_id, student_id, question_id),
            ).fetchone()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if assignment is None or assignment["status"] != "published":
                raise ValueError("此作文作業不存在或尚未指派給你。")
            if assignment["starts_at"] and assignment["starts_at"] > now:
                raise ValueError("作文作業尚未開始。")
            if assignment["due_at"] and assignment["due_at"] < now:
                raise ValueError("作文作業已超過截止時間。")
        latest = conn.execute(
            """SELECT a.id, v.version_number, v.status
               FROM attempts a JOIN essay_versions v ON v.attempt_id = a.id
               WHERE a.student_id = ? AND a.question_id = ?
               ORDER BY v.version_number DESC LIMIT 1""",
            (student_id, question_id),
        ).fetchone()
        submitted_at = datetime.now(timezone.utc).isoformat()
        if latest is None:
            cursor = conn.execute(
                """INSERT INTO attempts
                   (student_id, question_id, response, earned_points,
                    submitted_at, assignment_id)
                   VALUES (?, ?, ?, NULL, ?, ?)""",
                (student_id, question_id, content, submitted_at, assignment_id),
            )
            attempt_id = cursor.lastrowid
            version_number = 1
        else:
            if latest["status"] != "revision_requested":
                raise ValueError("作文已提交；如需修改，請等待老師退回。")
            attempt_id = latest["id"]
            version_number = latest["version_number"] + 1
            conn.execute(
                """UPDATE attempts SET response = ?, earned_points = NULL,
                          feedback = '', submitted_at = ?, assignment_id = COALESCE(?, assignment_id)
                   WHERE id = ? AND student_id = ?""",
                (content, submitted_at, assignment_id, attempt_id, student_id),
            )
        conn.execute(
            """INSERT INTO essay_versions
               (attempt_id, version_number, content, status, submitted_at)
               VALUES (?, ?, ?, 'submitted', ?)""",
            (attempt_id, version_number, content, submitted_at),
        )
        conn.execute(
            "DELETE FROM essay_drafts WHERE student_id = ? AND question_id = ?",
            (student_id, question_id),
        )
        quiz = conn.execute(
            "SELECT quiz_id FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if quiz and quiz["quiz_id"]:
            conn.execute(
                """UPDATE quiz_sessions SET score = (
                       SELECT COALESCE(SUM(earned_points), 0)
                       FROM attempts WHERE quiz_id = ?
                   ) WHERE id = ?""",
                (quiz["quiz_id"], quiz["quiz_id"]),
            )
        return {"attempt_id": attempt_id, "version_number": version_number}


def student_essay_versions(student_id, attempt_id):
    with database() as conn:
        attempt = conn.execute(
            """SELECT id FROM attempts
               WHERE id = ? AND student_id = ?""",
            (attempt_id, student_id),
        ).fetchone()
        if attempt is None:
            raise ValueError("找不到此學生作文。")
        return [
            dict(row)
            for row in conn.execute(
                """SELECT id, version_number, content, status, score, feedback,
                          submitted_at
                   FROM essay_versions WHERE attempt_id = ?
                   ORDER BY version_number DESC""",
                (attempt_id,),
            )
        ]


def essay_comments(attempt_id, version_number, viewer_id):
    with database() as conn:
        viewer = conn.execute(
            "SELECT role FROM users WHERE id = ? AND is_active = 1",
            (viewer_id,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT student_id FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if viewer is None or attempt is None:
            raise ValueError("無法查看作文批註。")
        if viewer["role"] == "student" and attempt["student_id"] != viewer_id:
            raise ValueError("只能查看自己的作文批註。")
        if viewer["role"] == "teacher":
            _require_teacher(conn, viewer_id)
        return [
            dict(row)
            for row in conn.execute(
                """SELECT c.quoted_text, c.comment, c.created_at, u.display_name
                   FROM essay_comments c
                   JOIN essay_versions v ON v.id = c.version_id
                   JOIN users u ON u.id = c.teacher_id
                   WHERE v.attempt_id = ? AND v.version_number = ?
                   ORDER BY c.id""",
                (attempt_id, version_number),
            )
        ]


def add_essay_comment(teacher_id, attempt_id, version_number, quoted_text, comment):
    quoted_text = str(quoted_text).strip()
    comment = str(comment).strip()
    if not quoted_text or len(quoted_text) > 1000:
        raise ValueError("請輸入作文中 1 至 1000 個字元的原文句子。")
    if not comment or len(comment) > 2000:
        raise ValueError("逐句批註需為 1 至 2000 個字元。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        version = conn.execute(
            """SELECT v.id, v.content FROM essay_versions v
               JOIN attempts a ON a.id = v.attempt_id
               JOIN questions q ON q.id = a.question_id
               WHERE v.attempt_id = ? AND v.version_number = ?
                 AND q.question_type = 'essay'""",
            (attempt_id, version_number),
        ).fetchone()
        if version is None:
            raise ValueError("找不到指定的作文版本。")
        if quoted_text not in version["content"]:
            raise ValueError("批註原文必須完整出現在此作文版本中。")
        conn.execute(
            """INSERT INTO essay_comments
               (version_id, teacher_id, quoted_text, comment)
               VALUES (?, ?, ?, ?)""",
            (version["id"], teacher_id, quoted_text, comment),
        )
        _audit(
            conn,
            teacher_id,
            "add_essay_comment",
            "essay_version",
            version["id"],
            {"quoted_text": quoted_text[:120]},
        )


def request_essay_revision(teacher_id, attempt_id, feedback):
    feedback = str(feedback).strip()
    if not feedback or len(feedback) > 5000:
        raise ValueError("請填寫 1 至 5000 個字元的修改建議。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        version = conn.execute(
            """SELECT v.id, v.version_number, a.quiz_id
               FROM essay_versions v
               JOIN attempts a ON a.id = v.attempt_id
               JOIN questions q ON q.id = a.question_id
               WHERE v.attempt_id = ? AND q.question_type = 'essay'
               ORDER BY v.version_number DESC LIMIT 1""",
            (attempt_id,),
        ).fetchone()
        if version is None:
            raise ValueError("找不到此作文作答。")
        conn.execute(
            """UPDATE essay_versions SET status = 'revision_requested', feedback = ?
               WHERE id = ?""",
            (feedback, version["id"]),
        )
        conn.execute(
            "UPDATE attempts SET earned_points = NULL, feedback = ? WHERE id = ?",
            (feedback, attempt_id),
        )
        if version["quiz_id"]:
            conn.execute(
                """UPDATE quiz_sessions SET score = (
                       SELECT COALESCE(SUM(earned_points), 0)
                       FROM attempts WHERE quiz_id = ?
                   ) WHERE id = ?""",
                (version["quiz_id"], version["quiz_id"]),
            )
        _audit(
            conn,
            teacher_id,
            "request_essay_revision",
            "essay_attempt",
            attempt_id,
            {"version_number": version["version_number"]},
        )


def get_user(user_id):
    with database() as conn:
        row = conn.execute(
            """SELECT id, username, display_name, role, must_change_password,
                      email, created_at, is_active, class_name
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def change_display_name(user_id, display_name):
    display_name = display_name.strip()
    if not 1 <= len(display_name) <= 40:
        raise ValueError("暱稱需為 1 至 40 個字元。")
    with database() as conn:
        cursor = conn.execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            (display_name, user_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此帳號。")


def change_password(user_id, new_password):
    validate_password(new_password)
    with database() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此帳號。")


def set_user_email(user_id, email):
    email = validate_email(email)
    with database() as conn:
        try:
            cursor = conn.execute(
                "UPDATE users SET email = ? WHERE id = ? AND is_active = 1",
                (email, user_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("此電子郵件已被其他學生使用。") from exc
        if cursor.rowcount != 1:
            raise ValueError("找不到啟用中的帳號。")


def create_student(teacher_id, username, display_name, password, email="", class_name=""):
    username = validate_username(username)
    validate_password(password)
    display_name = display_name.strip()
    class_name = class_name.strip()
    if not 1 <= len(display_name) <= 40 or len(class_name) > 80:
        raise ValueError("暱稱需為 1 至 40 個字元，班級不可超過 80 個字元。")
    email = validate_email(email) if email.strip() else ""
    with database() as conn:
        _require_teacher(conn, teacher_id)
        try:
            cursor = conn.execute(
                """INSERT INTO users
                   (username, display_name, password_hash, role, email, class_name)
                   VALUES (?, ?, ?, 'student', ?, ?)""",
                (username, display_name, hash_password(password), email, class_name),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("帳號名稱或電子郵件已被使用。") from exc
        _audit(
            conn,
            teacher_id,
            "create_student",
            "user",
            cursor.lastrowid,
            {"username": username},
        )
        return cursor.lastrowid


def update_student(teacher_id, student_id, display_name, email, class_name):
    display_name = display_name.strip()
    class_name = class_name.strip()
    if not 1 <= len(display_name) <= 40 or len(class_name) > 80:
        raise ValueError("暱稱需為 1 至 40 個字元，班級不可超過 80 個字元。")
    email = validate_email(email) if email.strip() else ""
    with database() as conn:
        _require_teacher(conn, teacher_id)
        try:
            cursor = conn.execute(
                """UPDATE users SET display_name = ?, email = ?, class_name = ?
                   WHERE id = ? AND role = 'student'""",
                (display_name, email, class_name, student_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("此電子郵件已被其他學生使用。") from exc
        if cursor.rowcount != 1:
            raise ValueError("找不到此學生。")
        _audit(
            conn,
            teacher_id,
            "update_student",
            "user",
            student_id,
            {"display_name": display_name, "email": email, "class_name": class_name},
        )


def set_student_active(teacher_id, student_id, active):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ? AND role = 'student'",
            (int(bool(active)), student_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此學生。")
        _audit(
            conn,
            teacher_id,
            "activate_student" if active else "deactivate_student",
            "user",
            student_id,
        )


def reset_student_password_by_teacher(teacher_id, student_id, new_password):
    validate_password(new_password)
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            """UPDATE users SET password_hash = ?, must_change_password = 1
               WHERE id = ? AND role = 'student'""",
            (hash_password(new_password), student_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此學生。")
        _audit(conn, teacher_id, "reset_student_password", "user", student_id)
        conn.execute(
            "DELETE FROM password_resets WHERE user_id = ?",
            (student_id,),
        )


def get_student_detail(teacher_id, student_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        student = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.email,
                      COALESCE((
                          SELECT group_concat(c.name, ', ')
                          FROM class_memberships cm
                          JOIN classes c ON c.id = cm.class_id
                          WHERE cm.student_id = u.id
                      ), u.class_name) AS class_name,
                      u.is_active, u.created_at
               FROM users u WHERE u.id = ? AND u.role = 'student'""",
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError("找不到此學生。")
    detail = dict(student)
    detail["progress"] = student_progress(student_id)
    detail["rank"] = next(
        (row["rank"] for row in leaderboard() if row["id"] == student_id),
        None,
    )
    detail["accuracy"] = student_accuracy(student_id)
    detail["attempts"] = student_attempts(student_id)
    detail["quizzes"] = student_quiz_history(student_id, limit=100)
    return detail


def student_overview(teacher_id, search=""):
    search = search.strip()
    with database() as conn:
        _require_teacher(conn, teacher_id)
        rows = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.email,
                      u.is_active, u.created_at,
                      COALESCE((
                          SELECT group_concat(c.name, ', ')
                          FROM class_memberships cm
                          JOIN classes c ON c.id = cm.class_id
                          WHERE cm.student_id = u.id
                      ), u.class_name) AS class_name,
                      COALESCE(scores.score, 0) + COALESCE(wheel_scores.net_score, 0)
                          + COALESCE(login_scores.points, 0) AS score,
                      COALESCE(accuracy.correct_count, 0) AS correct_count,
                      COALESCE(accuracy.answered_count, 0) AS answered_count
               FROM users u
               LEFT JOIN (
                   SELECT student_id, SUM(best_score) AS score
                   FROM (
                       SELECT student_id, question_id, MAX(earned_points) AS best_score
                       FROM attempts WHERE earned_points IS NOT NULL
                       GROUP BY student_id, question_id
                   ) GROUP BY student_id
               ) scores ON scores.student_id = u.id
               LEFT JOIN (
                   SELECT student_id, SUM(points_awarded - points_spent) AS net_score
                   FROM wheel_spins
                   GROUP BY student_id
               ) wheel_scores ON wheel_scores.student_id = u.id
               LEFT JOIN (
                   SELECT student_id, SUM(points_awarded) AS points
                   FROM daily_login_rewards
                   GROUP BY student_id
               ) login_scores ON login_scores.student_id = u.id
               LEFT JOIN (
                   SELECT a.student_id,
                          SUM(CASE WHEN a.earned_points = q.points THEN 1 ELSE 0 END)
                              AS correct_count,
                          COUNT(*) AS answered_count
                   FROM attempts a
                   JOIN questions q ON q.id = a.question_id
                   WHERE q.question_type IN ('choice', 'fill_blank')
                     AND a.earned_points IS NOT NULL
                   GROUP BY a.student_id
               ) accuracy ON accuracy.student_id = u.id
               WHERE u.role = 'student'
                 AND (? = '' OR u.username LIKE ? OR u.display_name LIKE ?
                      OR u.email LIKE ? OR u.class_name LIKE ?
                      OR EXISTS (
                          SELECT 1 FROM class_memberships cm
                          JOIN classes c ON c.id = cm.class_id
                          WHERE cm.student_id = u.id AND c.name LIKE ?
                      ))
               ORDER BY u.is_active DESC, u.username COLLATE NOCASE""",
            (
                search,
                f"%{search}%",
                f"%{search}%",
                f"%{search}%",
                f"%{search}%",
                f"%{search}%",
            ),
        )
        students = [dict(row) for row in rows]
    ranks = {row["id"]: row["rank"] for row in leaderboard()}
    for student in students:
        student["rank"] = ranks.get(student["id"])
        student["accuracy"] = (
            student["correct_count"] / student["answered_count"] * 100
            if student["answered_count"]
            else None
        )
    return students


def teacher_update_profile(teacher_id, display_name, email=""):
    display_name = display_name.strip()
    if not 1 <= len(display_name) <= 40:
        raise ValueError("顯示名稱需為 1 至 40 個字元。")
    email = validate_email(email) if email.strip() else ""
    with database() as conn:
        _require_teacher(conn, teacher_id)
        conn.execute(
            "UPDATE users SET display_name = ?, email = ? WHERE id = ?",
            (display_name, email, teacher_id),
        )
        _audit(conn, teacher_id, "update_profile", "user", teacher_id)


def update_assignment_deadline(teacher_id, assignment_id, starts_at, due_at):
    try:
        start_time = (
            datetime.strptime(starts_at, "%Y-%m-%d %H:%M:%S")
            if starts_at
            else None
        )
        due_time = (
            datetime.strptime(due_at, "%Y-%m-%d %H:%M:%S")
            if due_at
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("作業開始或截止時間格式無效。") from exc
    if start_time and due_time and start_time > due_time:
        raise ValueError("作業開始時間不可晚於截止時間。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            """UPDATE assignment_sets SET starts_at = ?, due_at = ?
               WHERE id = ? AND teacher_id = ?""",
            (starts_at, due_at, assignment_id, teacher_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此作業。")
        _audit(
            conn,
            teacher_id,
            "extend_assignment_deadline",
            "assignment",
            assignment_id,
            {"starts_at": starts_at, "due_at": due_at},
        )


def audit_log(teacher_id, limit=100):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        return [
            dict(row)
            for row in conn.execute(
                """SELECT l.action, l.entity_type, l.entity_id, l.details,
                          l.created_at, u.username AS actor
                   FROM audit_logs l LEFT JOIN users u ON u.id = l.actor_id
                   ORDER BY l.id DESC LIMIT ?""",
                (min(max(int(limit), 1), 500),),
            )
        ]


def create_password_reset(email):
    email = email.strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None
    now = datetime.now(timezone.utc)
    with database() as conn:
        conn.execute(
            "DELETE FROM password_resets WHERE created_at < datetime('now', '-30 days')"
        )
        row = conn.execute(
            "SELECT id FROM users WHERE email = ? AND role = 'student'",
            (email,),
        ).fetchone()
        if row is None:
            return None
        recent = conn.execute(
            """SELECT COUNT(*) FROM password_resets
               WHERE user_id = ? AND created_at >= datetime('now', '-1 hour')""",
            (row["id"],),
        ).fetchone()[0]
        if recent >= 3:
            return None
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires_at = (now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)).isoformat()
        conn.execute(
            "INSERT INTO password_resets(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (row["id"], token_hash, expires_at),
        )
        return token


def cancel_password_reset(token):
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    with database() as conn:
        conn.execute(
            "DELETE FROM password_resets WHERE token_hash = ? AND used_at IS NULL",
            (token_hash,),
        )


def reset_student_password(token, new_password):
    validate_password(new_password)
    if not isinstance(token, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{32,128}", token
    ):
        raise ValueError("重設連結無效或已過期，請重新申請。")
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with database() as conn:
        reset = conn.execute(
            """SELECT id, user_id FROM password_resets
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (token_hash, now),
        ).fetchone()
        if reset is None:
            raise ValueError("重設連結無效或已過期，請重新申請。")
        password_hash = hash_password(new_password)
        conn.execute(
            """UPDATE users SET password_hash = ?, must_change_password = 0
               WHERE id = ? AND role = 'student'""",
            (password_hash, reset["user_id"]),
        )
        cursor = conn.execute(
            """UPDATE password_resets SET used_at = ?
               WHERE id = ? AND used_at IS NULL""",
            (now, reset["id"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("重設連結已使用，請重新申請。")
        conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now, reset["user_id"]),
        )


def login_history(user_id, limit=20):
    with database() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT username, succeeded, created_at FROM login_events
                   WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, min(max(int(limit), 1), 100)),
            )
        ]


def create_announcement(teacher_id, title, content):
    title = title.strip()
    content = content.strip()
    if not title or len(title) > 120 or not content or len(content) > 5000:
        raise ValueError("公告標題需為 1 至 120 個字元，內容需為 1 至 5000 個字元。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        conn.execute(
            "INSERT INTO announcements(teacher_id, title, content) VALUES (?, ?, ?)",
            (teacher_id, title, content),
        )
        announcement_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _audit(conn, teacher_id, "create_announcement", "announcement", announcement_id)


def list_announcements(limit=5):
    with database() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT a.id, a.title, a.content, a.published_at,
                          u.display_name AS teacher_name
                   FROM announcements a JOIN users u ON u.id = a.teacher_id
                   ORDER BY a.published_at DESC, a.id DESC LIMIT ?""",
                (min(max(int(limit), 1), 50),),
            )
        ]


def update_announcement(teacher_id, announcement_id, title, content):
    title = title.strip()
    content = content.strip()
    if not title or len(title) > 120 or not content or len(content) > 5000:
        raise ValueError("公告標題需為 1 至 120 個字元，內容需為 1 至 5000 個字元。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            """UPDATE announcements SET title = ?, content = ?
               WHERE id = ? AND teacher_id = ?""",
            (title, content, announcement_id, teacher_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此公告或沒有權限修改。")
        _audit(conn, teacher_id, "update_announcement", "announcement", announcement_id)


def delete_announcement(teacher_id, announcement_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            "DELETE FROM announcements WHERE id = ? AND teacher_id = ?",
            (announcement_id, teacher_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此公告或沒有權限刪除。")
        _audit(conn, teacher_id, "delete_announcement", "announcement", announcement_id)


def student_quiz_history(student_id, limit=20):
    with database() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT id, score, max_score, correct_count,
                          json_array_length(question_ids) AS question_count,
                          (SELECT COUNT(*) FROM attempts a JOIN questions q
                           ON q.id = a.question_id
                           WHERE a.quiz_id = quiz_sessions.id
                             AND q.question_type IN ('choice', 'fill_blank')) AS choice_count,
                          (SELECT COUNT(*) FROM attempts a JOIN questions q
                           ON q.id = a.question_id
                           WHERE a.quiz_id = quiz_sessions.id
                             AND q.question_type = 'essay'
                             AND a.earned_points IS NULL) AS essays_pending,
                          submitted_at, status
                   FROM quiz_sessions
                   WHERE student_id = ? AND status = 'submitted'
                   ORDER BY submitted_at DESC, created_at DESC LIMIT ?""",
                (student_id, min(max(int(limit), 1), 100)),
            )
        ]


def quiz_results(quiz_id, student_id):
    with database() as conn:
        rows = conn.execute(
            """SELECT a.earned_points, q.id AS question_id,
                      q.question_type,
                      a.response, q.options, q.rubric, q.min_word_count,
                      CASE WHEN s.assignment_id IS NULL OR assignment.show_answers = 1
                           THEN q.answer ELSE '' END AS answer,
                      CASE WHEN s.assignment_id IS NULL OR assignment.show_answers = 1
                           THEN q.explanation ELSE '' END AS explanation,
                      q.prompt, q.points
               FROM attempts a JOIN questions q ON q.id = a.question_id
               JOIN quiz_sessions s ON s.id = a.quiz_id
               LEFT JOIN assignment_sets assignment ON assignment.id = s.assignment_id
               WHERE a.quiz_id = ? AND s.student_id = ?
                 AND s.status = 'submitted'
               ORDER BY a.id""",
            (quiz_id, student_id),
        )
        results = [dict(row) for row in rows]
        for result in results:
            result["options"] = json.loads(result["options"])
        return results


def change_username(user_id, username):
    username = validate_username(username)
    with database() as conn:
        try:
            cursor = conn.execute(
                "UPDATE users SET username = ?, display_name = ? WHERE id = ?",
                (username, username, user_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("此帳號已被使用。") from exc
        if cursor.rowcount != 1:
            raise ValueError("找不到此帳號。")


def list_students(teacher_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        return [
            dict(row)
            for row in conn.execute(
                """SELECT id, username, display_name, email, class_name, is_active
                   FROM users WHERE role = 'student' AND is_active = 1 ORDER BY username"""
            )
        ]


def create_class(teacher_id, name):
    name = str(name).strip()
    if not name or len(name) > 80:
        raise ValueError("班級名稱需為 1 至 80 個字元。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        try:
            cursor = conn.execute(
                "INSERT INTO classes(teacher_id, name) VALUES (?, ?)",
                (teacher_id, name),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("你已建立同名班級。") from exc
        _audit(conn, teacher_id, "create_class", "class", cursor.lastrowid)
        return cursor.lastrowid


def list_classes(teacher_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        return [
            dict(row)
            for row in conn.execute(
                """SELECT c.id, c.name, c.created_at,
                          COUNT(cm.student_id) AS student_count
                   FROM classes c
                   LEFT JOIN class_memberships cm ON cm.class_id = c.id
                   WHERE c.teacher_id = ?
                   GROUP BY c.id
                   ORDER BY c.name COLLATE NOCASE""",
                (teacher_id,),
            )
        ]


def class_student_ids(teacher_id, class_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        owned = conn.execute(
            "SELECT 1 FROM classes WHERE id = ? AND teacher_id = ?",
            (class_id, teacher_id),
        ).fetchone()
        if not owned:
            raise ValueError("找不到此班級。")
        return [
            row["student_id"]
            for row in conn.execute(
                "SELECT student_id FROM class_memberships WHERE class_id = ? ORDER BY student_id",
                (class_id,),
            )
        ]


def set_class_students(teacher_id, class_id, student_ids):
    ids = sorted({int(student_id) for student_id in student_ids})
    with database() as conn:
        _require_teacher(conn, teacher_id)
        owned = conn.execute(
            "SELECT 1 FROM classes WHERE id = ? AND teacher_id = ?",
            (class_id, teacher_id),
        ).fetchone()
        if not owned:
            raise ValueError("找不到此班級。")
        if ids:
            placeholders = ",".join("?" for _ in ids)
            active_count = conn.execute(
                f"""SELECT COUNT(*) FROM users
                    WHERE role = 'student' AND is_active = 1
                      AND id IN ({placeholders})""",
                ids,
            ).fetchone()[0]
            if active_count != len(ids):
                raise ValueError("學生不存在或帳號已停用，請重新選擇。")
        conn.execute("DELETE FROM class_memberships WHERE class_id = ?", (class_id,))
        conn.executemany(
            "INSERT INTO class_memberships(class_id, student_id) VALUES (?, ?)",
            [(class_id, student_id) for student_id in ids],
        )
        _audit(
            conn,
            teacher_id,
            "set_class_students",
            "class",
            class_id,
            {"student_ids": ids},
        )


def validate_question(question):
    category = str(question.get("category", "")).strip()
    difficulty = question.get("difficulty")
    question_type = question.get("type", question.get("question_type", "choice"))
    prompt = str(question.get("prompt", "")).strip()
    points = question.get("points", 1)
    options = question.get("options", [])
    answer = str(question.get("answer", "")).strip()
    rubric = str(question.get("rubric", "")).strip()
    min_word_count = question.get("min_word_count", 0)
    explanation = str(question.get("explanation", "")).strip()
    if not category or len(category) > 80:
        raise ValueError("category 必須是 1 至 80 個字元。")
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty 必須是其中之一：{', '.join(DIFFICULTIES)}。")
    if question_type not in QUESTION_TYPES:
        raise ValueError("type 必須是 choice、fill_blank 或 essay。")
    if not prompt or len(prompt) > 10_000:
        raise ValueError("prompt 必須是 1 至 10000 個字元。")
    if isinstance(points, bool) or not isinstance(points, int) or not 1 <= points <= 100:
        raise ValueError("points 必須是 1 至 100 的整數。")
    if (
        isinstance(min_word_count, bool)
        or not isinstance(min_word_count, int)
        or not 0 <= min_word_count <= 10_000
    ):
        raise ValueError("min_word_count 必須是 0 至 10000 的整數。")
    if question_type == "choice":
        if not isinstance(options, list) or len(options) < 2 or any(
            not isinstance(option, str) or not option.strip() for option in options
        ):
            raise ValueError("選擇題 options 必須至少有兩個非空選項。")
        options = [option.strip() for option in options]
        if len({option.casefold() for option in options}) != len(options):
            raise ValueError("選擇題選項不能重複。")
        if answer.casefold() not in {option.casefold() for option in options}:
            raise ValueError("選擇題 answer 必須符合其中一個選項。")
    elif question_type == "fill_blank":
        accepted_answers = question.get("accepted_answers", [answer])
        if isinstance(accepted_answers, str):
            accepted_answers = [part.strip() for part in accepted_answers.split("|")]
        if not isinstance(accepted_answers, list) or not accepted_answers or any(
            not isinstance(value, str) or not value.strip() for value in accepted_answers
        ):
            raise ValueError("填空題 answer 不可為空。")
        accepted_answers = list(
            dict.fromkeys(value.strip() for value in accepted_answers)
        )
        answer = accepted_answers[0]
        options = []
    else:
        options = []
        answer = ""
    if len(explanation) > 5000:
        raise ValueError("explanation 不可超過 5000 個字元。")
    if len(rubric) > 5000:
        raise ValueError("作文評分規準不可超過 5000 個字元。")
    return {
        "category": category,
        "scope": str(question.get("scope", "")).strip()[:80],
        "grade_level": str(question.get("grade_level", "")).strip()[:40],
        "bank_id": question.get("bank_id"),
        "is_published": bool(question.get("is_published", True)),
        "difficulty": difficulty,
        "question_type": question_type,
        "prompt": prompt,
        "options": options,
        "answer": answer,
        "accepted_answers": (
            accepted_answers if question_type == "fill_blank" else []
        ),
        "explanation": explanation,
        "rubric": rubric if question_type == "essay" else "",
        "min_word_count": min_word_count if question_type == "essay" else 0,
        "points": points,
    }


def normalize_question_import(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        raise ValueError("JSON 必須包含 questions 陣列。")
    outer_category = str(payload.get("category", "")).strip()
    outer_level = str(payload.get("level", "")).strip()
    difficulty_aliases = {
        "easy": "初級",
        "beginner": "初級",
        "elementary": "初級",
        "medium": "中級",
        "intermediate": "中級",
        "upper_intermediate": "中高級",
        "upper-intermediate": "中高級",
        "hard": "高級",
        "advanced": "高級",
    }
    type_aliases = {
        "single_choice": "choice",
        "multiple_choice": "choice",
        "choice": "choice",
        "fill_blank": "fill_blank",
        "essay": "essay",
    }
    normalized_questions = []
    for index, raw_question in enumerate(payload["questions"], start=1):
        if not isinstance(raw_question, dict):
            raise ValueError(f"第 {index} 題格式錯誤：題目必須是 JSON 物件。")
        question = dict(raw_question)
        question["category"] = question.get("category") or outer_category
        question["prompt"] = question.get("prompt", question.get("question", ""))
        raw_type = str(question.get("type", question.get("question_type", "choice"))).strip()
        question["type"] = type_aliases.get(raw_type.casefold(), raw_type)
        raw_difficulty = str(question.get("difficulty", outer_level)).strip()
        question["difficulty"] = difficulty_aliases.get(
            raw_difficulty.casefold(), raw_difficulty
        )
        if outer_level and not question.get("grade_level"):
            question["grade_level"] = difficulty_aliases.get(
                outer_level.casefold(), outer_level
            )
        options = question.get("options", [])
        if isinstance(options, str):
            question["options"] = [
                option.strip() for option in options.split("|") if option.strip()
            ]
        accepted_answers = question.get("accepted_answers")
        if isinstance(accepted_answers, str):
            question["accepted_answers"] = [
                answer.strip()
                for answer in accepted_answers.split("|")
                if answer.strip()
            ]
        if "points" in question and isinstance(question["points"], str):
            try:
                question["points"] = int(question["points"])
            except ValueError as exc:
                raise ValueError(f"第 {index} 題 points 必須是整數。") from exc
        if "min_word_count" in question and isinstance(
            question["min_word_count"], str
        ):
            try:
                question["min_word_count"] = int(question["min_word_count"])
            except ValueError as exc:
                raise ValueError(f"第 {index} 題 min_word_count 必須是整數。") from exc
        normalized_questions.append(question)
    return {
        "title": str(payload.get("title", "")).strip(),
        "category": outer_category,
        "level": outer_level,
        "questions": normalized_questions,
    }


def create_question_bank(teacher_id, name, description=""):
    name = name.strip()
    description = description.strip()
    if not name or len(name) > 100 or len(description) > 2000:
        raise ValueError("題庫名稱需為 1 至 100 個字元，說明不可超過 2000 個字元。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        try:
            cursor = conn.execute(
                """INSERT INTO question_banks(name, description, created_by)
                   VALUES (?, ?, ?)""",
                (name, description, teacher_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("同名題庫已存在。") from exc
        _audit(conn, teacher_id, "create_question_bank", "question_bank", cursor.lastrowid)
        return cursor.lastrowid


def list_question_banks(teacher_id=None):
    with database() as conn:
        if teacher_id is not None:
            _require_teacher(conn, teacher_id)
        rows = conn.execute(
            """SELECT b.*, COUNT(q.id) AS question_count
               FROM question_banks b LEFT JOIN questions q ON q.bank_id = b.id
               GROUP BY b.id ORDER BY b.name COLLATE NOCASE"""
        )
        return [dict(row) for row in rows]


def update_question_bank(teacher_id, bank_id, name, description):
    name = name.strip()
    description = description.strip()
    if not name or len(name) > 100 or len(description) > 2000:
        raise ValueError("題庫名稱需為 1 至 100 個字元，說明不可超過 2000 個字元。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        try:
            cursor = conn.execute(
                """UPDATE question_banks
                   SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (name, description, bank_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("同名題庫已存在。") from exc
        if cursor.rowcount != 1:
            raise ValueError("找不到題庫。")
        _audit(conn, teacher_id, "update_question_bank", "question_bank", bank_id)


def delete_question_bank(teacher_id, bank_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        count = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE bank_id = ?", (bank_id,)
        ).fetchone()[0]
        if count:
            raise ValueError("請先移動或刪除題庫中的題目，才能刪除題庫。")
        cursor = conn.execute("DELETE FROM question_banks WHERE id = ?", (bank_id,))
        if cursor.rowcount != 1:
            raise ValueError("找不到題庫。")
        _audit(conn, teacher_id, "delete_question_bank", "question_bank", bank_id)


def import_questions(teacher_id, payload, bank_id=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        raise ValueError("JSON 必須包含 questions 陣列。")
    raw_questions = payload["questions"]
    if not raw_questions or len(raw_questions) > 500:
        raise ValueError("每份題庫需包含 1 至 500 題。")
    validated = []
    for index, question in enumerate(raw_questions, start=1):
        try:
            validated.append(
                validate_question(
                    {
                        **question,
                        "bank_id": bank_id if bank_id is not None else question.get("bank_id"),
                    }
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 題格式錯誤：{exc}") from exc
    content_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    with database() as conn:
        _require_teacher(conn, teacher_id)
        if bank_id is not None and not conn.execute(
            "SELECT 1 FROM question_banks WHERE id = ?", (bank_id,)
        ).fetchone():
            raise ValueError("找不到指定題庫。")
        if conn.execute(
            "SELECT 1 FROM import_batches WHERE content_hash = ?", (content_hash,)
        ).fetchone():
            raise ValueError("這份考卷已經匯入過，系統已阻止重複匯入。")
        conn.executemany(
            """INSERT INTO questions
               (bank_id, category, scope, grade_level, difficulty, question_type, prompt,
                options, answer, accepted_answers, explanation, rubric, min_word_count,
                points, is_published, created_by)
               VALUES (:bank_id, :category, :scope, :grade_level, :difficulty,
                       :question_type, :prompt, :options, :answer, :accepted_answers, :explanation,
                       :rubric, :min_word_count, :points, :is_published, :created_by)""",
            [
                {
                    **question,
                    "options": json.dumps(question["options"], ensure_ascii=False),
                    "accepted_answers": json.dumps(
                        question["accepted_answers"], ensure_ascii=False
                    ),
                    "created_by": teacher_id,
                    "is_published": int(question["is_published"]),
                }
                for question in validated
            ],
        )
        conn.execute(
            """INSERT INTO import_batches(teacher_id, content_hash, imported_count)
               VALUES (?, ?, ?)""",
            (teacher_id, content_hash, len(validated)),
        )
        _audit(
            conn,
            teacher_id,
            "import_questions",
            "question_bank",
            bank_id or "",
            {"count": len(validated), "content_hash": content_hash},
        )
    return len(validated)


def add_question(teacher_id, question):
    validated = validate_question(question)
    return import_questions(teacher_id,
        {
            "questions": [
                {
                    **validated,
                    "type": validated["question_type"],
                }
            ]
        },
        bank_id=validated["bank_id"],
    )


def list_questions(
    category=None,
    difficulty=None,
    scope=None,
    grade_level=None,
    bank_id=None,
    include_drafts=False,
    teacher_id=None,
    search="",
):
    clauses = []
    params = []
    if include_drafts:
        if teacher_id is None:
            raise ValueError("只有教師可以檢視草稿題目。")
        with database() as conn:
            _require_teacher(conn, teacher_id)
    else:
        clauses.append("is_published = 1")
    if category:
        clauses.append("category = ?")
        params.append(category)
    if difficulty:
        clauses.append("difficulty = ?")
        params.append(difficulty)
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if grade_level:
        clauses.append("grade_level = ?")
        params.append(grade_level)
    if bank_id is not None:
        clauses.append("bank_id = ?")
        params.append(bank_id)
    if search.strip():
        clauses.append("(prompt LIKE ? OR category LIKE ? OR scope LIKE ?)")
        params.extend([f"%{search.strip()}%"] * 3)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with database() as conn:
        rows = conn.execute(
            f"SELECT * FROM questions {where} ORDER BY category, difficulty, id", params
        )
        return [_question_dict(row) for row in rows]


def update_question(teacher_id, question_id, question):
    validated = validate_question(question)
    with database() as conn:
        _require_teacher(conn, teacher_id)
        previous = conn.execute(
            "SELECT id FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if previous is None:
            raise ValueError("找不到題目。")
        conn.execute(
            """UPDATE questions
               SET bank_id = ?, category = ?, scope = ?, grade_level = ?,
                   difficulty = ?, question_type = ?, prompt = ?, options = ?,
               answer = ?, accepted_answers = ?, explanation = ?, rubric = ?,
               min_word_count = ?, points = ?, is_published = ?
               WHERE id = ?""",
            (
                validated["bank_id"],
                validated["category"],
                validated["scope"],
                validated["grade_level"],
                validated["difficulty"],
                validated["question_type"],
                validated["prompt"],
                json.dumps(validated["options"], ensure_ascii=False),
                validated["answer"],
                json.dumps(validated["accepted_answers"], ensure_ascii=False),
                validated["explanation"],
                validated["rubric"],
                validated["min_word_count"],
                validated["points"],
                int(validated["is_published"]),
                question_id,
            ),
        )
        _audit(conn, teacher_id, "update_question", "question", question_id)


def delete_question(teacher_id, question_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        exists = conn.execute(
            "SELECT prompt FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if exists is None:
            raise ValueError("找不到題目。")
        used = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE question_id = ?", (question_id,)
        ).fetchone()[0]
        if used:
            conn.execute(
                "UPDATE questions SET is_published = 0 WHERE id = ?", (question_id,)
            )
            action = "archive_question"
        else:
            conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
            action = "delete_question"
        _audit(
            conn,
            teacher_id,
            action,
            "question",
            question_id,
            {"prompt": exists["prompt"][:120], "has_attempts": bool(used)},
        )


def _question_dict(row):
    question = dict(row)
    question["options"] = json.loads(question["options"])
    question["accepted_answers"] = json.loads(question["accepted_answers"])
    return question


def get_question(question_id):
    with database() as conn:
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    return _question_dict(row) if row else None


def submit_answer(student_id, question_id, response):
    response = str(response).strip()
    if not response or len(response) > 20_000:
        raise ValueError("請填寫答案（最多 20000 個字元）。")
    with database() as conn:
        question = conn.execute(
            """SELECT question_type, answer, points, min_word_count
               FROM questions WHERE id = ?""",
            (question_id,),
        ).fetchone()
        student = conn.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'student'", (student_id,)
        ).fetchone()
        if question is None or student is None:
            raise ValueError("題目或學生帳號不存在。")
        earned = (
            int(response.casefold() == question["answer"].strip().casefold())
            * question["points"]
            if question["question_type"] == "choice"
            else None
        )
        cursor = conn.execute(
            "INSERT INTO attempts(student_id, question_id, response, earned_points) VALUES (?, ?, ?, ?)",
            (student_id, question_id, response, earned),
        )
        if question["question_type"] == "essay":
            word_count = _essay_word_count(response)
            if word_count < question["min_word_count"]:
                raise ValueError(
                    f"作文至少需要 {question['min_word_count']} 個英文單字；目前 {word_count} 字。"
                )
            conn.execute(
                """INSERT INTO essay_versions
                   (attempt_id, version_number, content, status)
                   VALUES (?, 1, ?, 'submitted')""",
                (cursor.lastrowid, response),
            )
        return {"attempt_id": cursor.lastrowid, "earned_points": earned, "max_points": question["points"]}


def grade_essay(teacher_id, attempt_id, earned_points, feedback=""):
    if isinstance(earned_points, bool) or not isinstance(earned_points, int):
        raise ValueError("分數必須是整數。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        row = conn.execute(
            """SELECT a.question_id, q.points, q.question_type
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.id = ?""",
            (attempt_id,),
        ).fetchone()
        if row is None or row["question_type"] != "essay":
            raise ValueError("找不到此作文作答。")
        if not 0 <= earned_points <= row["points"]:
            raise ValueError(f"分數需介於 0 至 {row['points']}。")
        conn.execute(
            "UPDATE attempts SET earned_points = ?, feedback = ? WHERE id = ?",
            (earned_points, str(feedback).strip()[:5000], attempt_id),
        )
        conn.execute(
            """UPDATE essay_versions SET status = 'graded', score = ?, feedback = ?
               WHERE attempt_id = ? AND version_number = (
                   SELECT MAX(version_number) FROM essay_versions WHERE attempt_id = ?
               )""",
            (earned_points, str(feedback).strip()[:5000], attempt_id, attempt_id),
        )
        quiz = conn.execute(
            "SELECT quiz_id FROM attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        if quiz["quiz_id"]:
            conn.execute(
                """UPDATE quiz_sessions
                   SET score = (
                       SELECT COALESCE(SUM(earned_points), 0)
                       FROM attempts WHERE quiz_id = ?
                   )
                   WHERE id = ?""",
                (quiz["quiz_id"], quiz["quiz_id"]),
            )
        _audit(
            conn,
            teacher_id,
            "grade_essay",
            "attempt",
            attempt_id,
            {"score": earned_points},
        )


def adjust_attempt_score(teacher_id, attempt_id, score, reason):
    reason = reason.strip()
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValueError("分數必須是整數。")
    if not reason or len(reason) > 500:
        raise ValueError("請填寫 1 至 500 個字元的評分調整原因。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        attempt = conn.execute(
            """SELECT a.quiz_id, a.earned_points, q.points
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.id = ?""",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise ValueError("找不到作答紀錄。")
        if not 0 <= score <= attempt["points"]:
            raise ValueError(f"分數需介於 0 至 {attempt['points']}。")
        conn.execute(
            "UPDATE attempts SET earned_points = ? WHERE id = ?",
            (score, attempt_id),
        )
        if attempt["quiz_id"]:
            conn.execute(
                """UPDATE quiz_sessions SET score = (
                       SELECT COALESCE(SUM(earned_points), 0)
                       FROM attempts WHERE quiz_id = ?
                   ) WHERE id = ?""",
                (attempt["quiz_id"], attempt["quiz_id"]),
            )
        _audit(
            conn,
            teacher_id,
            "adjust_attempt_score",
            "attempt",
            attempt_id,
            {"old_score": attempt["earned_points"], "new_score": score, "reason": reason},
        )


def assign_questions(teacher_id, student_id, question_ids, due_at=None):
    ids = sorted({int(question_id) for question_id in question_ids})
    if not ids:
        raise ValueError("至少選擇一題。")
    if due_at is not None:
        due_at = due_at.strip()
        try:
            datetime.strptime(due_at, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("截止日期格式無效。") from exc
    with database() as conn:
        _require_teacher(conn, teacher_id)
        student = conn.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'student'", (student_id,)
        ).fetchone()
        found = conn.execute(
            f"""SELECT COUNT(*) FROM questions
                WHERE is_published = 1 AND id IN ({','.join('?' for _ in ids)})""",
            ids,
        ).fetchone()[0]
        if student is None or found != len(ids):
            raise ValueError("學生或題目不存在。")
        conn.executemany(
            """INSERT INTO assignments(student_id, question_id, due_at)
               VALUES (?, ?, ?)
               ON CONFLICT(student_id, question_id)
               DO UPDATE SET due_at = excluded.due_at""",
            [(student_id, question_id, due_at) for question_id in ids],
        )
        _audit(
            conn,
            teacher_id,
            "assign_questions",
            "student",
            student_id,
            {"question_ids": ids, "due_at": due_at},
        )


def create_assignment(
    teacher_id,
    title,
    description,
    question_ids,
    student_ids=None,
    all_students=False,
    starts_at=None,
    due_at=None,
    time_limit_minutes=None,
    allow_retake=False,
    show_answers=True,
    status="published",
):
    title = title.strip()
    description = description.strip()
    question_ids = sorted({int(value) for value in question_ids})
    student_ids = sorted({int(value) for value in (student_ids or [])})
    if not title or len(title) > 120 or len(description) > 3000:
        raise ValueError("作業名稱需為 1 至 120 個字元，說明不可超過 3000 個字元。")
    if not question_ids or len(question_ids) > 200:
        raise ValueError("作業需包含 1 至 200 題。")
    if not all_students and not student_ids:
        raise ValueError("請選擇學生或指定全班。")
    if time_limit_minutes is not None and not 1 <= int(time_limit_minutes) <= 600:
        raise ValueError("作答時間需介於 1 至 600 分鐘。")
    try:
        start_time = (
            datetime.strptime(starts_at, "%Y-%m-%d %H:%M:%S")
            if starts_at
            else None
        )
        due_time = (
            datetime.strptime(due_at, "%Y-%m-%d %H:%M:%S")
            if due_at
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("作業開始或截止時間格式無效。") from exc
    if start_time and due_time and start_time > due_time:
        raise ValueError("作業開始時間不可晚於截止時間。")
    if status not in ("draft", "published"):
        raise ValueError("作業狀態無效。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        found = conn.execute(
            f"""SELECT COUNT(*) FROM questions
                WHERE id IN ({','.join('?' for _ in question_ids)})
                  AND is_published = 1""",
            question_ids,
        ).fetchone()[0]
        if found != len(question_ids):
            raise ValueError("作業題目必須存在且已發布。")
        if all_students:
            student_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM users WHERE role = 'student' AND is_active = 1"
                )
            ]
        if not student_ids:
            raise ValueError("沒有可指派的啟用中學生。")
        if student_ids:
            placeholders = ",".join("?" for _ in student_ids)
            found_students = conn.execute(
                f"""SELECT COUNT(*) FROM users
                    WHERE role = 'student' AND is_active = 1
                      AND id IN ({placeholders})""",
                student_ids,
            ).fetchone()[0]
            if found_students != len(student_ids):
                raise ValueError("學生不存在或帳號已停用。")
        cursor = conn.execute(
            """INSERT INTO assignment_sets
               (teacher_id, title, description, starts_at, due_at,
                time_limit_minutes, allow_retake, show_answers, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                teacher_id,
                title,
                description,
                starts_at,
                due_at,
                int(time_limit_minutes) if time_limit_minutes is not None else None,
                int(bool(allow_retake)),
                int(bool(show_answers)),
                status,
            ),
        )
        assignment_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO assignment_recipients(assignment_id, student_id) VALUES (?, ?)",
            [(assignment_id, student_id) for student_id in student_ids],
        )
        conn.executemany(
            "INSERT INTO assignment_questions(assignment_id, question_id) VALUES (?, ?)",
            [(assignment_id, question_id) for question_id in question_ids],
        )
        _audit(
            conn,
            teacher_id,
            "create_assignment",
            "assignment",
            assignment_id,
            {"students": student_ids, "questions": question_ids, "status": status},
        )
        return assignment_id


def list_assignments_for_teacher(teacher_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        return [
            dict(row)
            for row in conn.execute(
                """SELECT s.*,
                          (SELECT COUNT(*) FROM assignment_recipients r
                           WHERE r.assignment_id = s.id) AS student_count,
                          (SELECT COUNT(*) FROM assignment_questions q
                           WHERE q.assignment_id = s.id) AS question_count,
                          (SELECT COUNT(DISTINCT a.student_id)
                           FROM attempts a WHERE a.assignment_id = s.id) AS submitted_count,
                          (SELECT AVG(score * 1.0 / NULLIF(max_score, 0))
                           FROM quiz_sessions z
                           WHERE z.assignment_id = s.id AND z.status = 'submitted') AS average_ratio
                   FROM assignment_sets s
                   WHERE s.teacher_id = ?
                   ORDER BY s.created_at DESC, s.id DESC""",
                (teacher_id,),
            )
        ]


def update_assignment_status(teacher_id, assignment_id, status):
    if status not in ("draft", "published", "closed", "cancelled"):
        raise ValueError("作業狀態無效。")
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            "UPDATE assignment_sets SET status = ? WHERE id = ? AND teacher_id = ?",
            (status, assignment_id, teacher_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此作業。")
        _audit(
            conn,
            teacher_id,
            "update_assignment_status",
            "assignment",
            assignment_id,
            {"status": status},
        )


def assignment_submissions(teacher_id, assignment_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        owned = conn.execute(
            "SELECT 1 FROM assignment_sets WHERE id = ? AND teacher_id = ?",
            (assignment_id, teacher_id),
        ).fetchone()
        if not owned:
            raise ValueError("找不到此作業。")
        return [
            dict(row)
            for row in conn.execute(
                """SELECT u.id AS student_id, u.username, u.display_name,
                          COUNT(DISTINCT a.question_id) AS answered_count,
                          COALESCE((
                              SELECT SUM(best.best_score)
                              FROM (
                                  SELECT MAX(scored.earned_points) AS best_score
                                  FROM attempts scored
                                  WHERE scored.assignment_id = r.assignment_id
                                    AND scored.student_id = u.id
                                    AND scored.earned_points IS NOT NULL
                                  GROUP BY scored.question_id
                              ) best
                          ), 0) AS score,
                          (SELECT COALESCE(SUM(q.points), 0)
                           FROM assignment_questions aq
                           JOIN questions q ON q.id = aq.question_id
                           WHERE aq.assignment_id = r.assignment_id) AS max_score,
                          MAX(a.submitted_at) AS submitted_at
                   FROM assignment_recipients r
                   JOIN users u ON u.id = r.student_id
                   LEFT JOIN attempts a ON a.assignment_id = r.assignment_id
                                      AND a.student_id = u.id
                   WHERE r.assignment_id = ?
                   GROUP BY u.id ORDER BY u.display_name""",
                (assignment_id,),
            )
        ]


def assignment_question_attempts(teacher_id, assignment_id, student_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        permitted = conn.execute(
            """SELECT 1 FROM assignment_recipients r
               JOIN assignment_sets s ON s.id = r.assignment_id
               WHERE r.assignment_id = ? AND r.student_id = ?
                 AND s.teacher_id = ?""",
            (assignment_id, student_id, teacher_id),
        ).fetchone()
        if not permitted:
            raise ValueError("此學生不屬於該作業。")
        return [
            dict(row)
            for row in conn.execute(
                """SELECT q.id AS question_id, a.id AS attempt_id, q.prompt, q.points, q.answer,
                          q.question_type, a.response, a.earned_points, a.feedback,
                          a.submitted_at
                   FROM assignment_questions aq
                   JOIN questions q ON q.id = aq.question_id
                   LEFT JOIN attempts a ON a.id = (
                       SELECT latest.id FROM attempts latest
                       WHERE latest.question_id = q.id
                         AND latest.student_id = ?
                         AND latest.assignment_id = aq.assignment_id
                       ORDER BY latest.id DESC LIMIT 1
                   )
                   WHERE aq.assignment_id = ?
                   ORDER BY q.id""",
                (student_id, assignment_id),
            )
        ]


def assignment_statistics(teacher_id, assignment_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        owned = conn.execute(
            "SELECT 1 FROM assignment_sets WHERE id = ? AND teacher_id = ?",
            (assignment_id, teacher_id),
        ).fetchone()
        if not owned:
            raise ValueError("找不到此作業。")
        question_stats = [
            dict(row)
            for row in conn.execute(
                """SELECT q.id AS question_id, q.prompt, q.points, q.question_type,
                          COUNT(DISTINCT a.student_id) AS response_count,
                          COUNT(DISTINCT CASE
                              WHEN q.question_type IN ('choice', 'fill_blank')
                               AND a.earned_points = q.points
                              THEN a.student_id END) AS correct_count
                   FROM assignment_questions aq
                   JOIN questions q ON q.id = aq.question_id
                   LEFT JOIN attempts a ON a.assignment_id = aq.assignment_id
                                       AND a.question_id = q.id
                   WHERE aq.assignment_id = ?
                   GROUP BY q.id
                   ORDER BY q.id""",
                (assignment_id,),
            )
        ]
    for question in question_stats:
        question["correct_rate"] = (
            question["correct_count"] / question["response_count"]
            if question["response_count"]
            and question["question_type"] in ("choice", "fill_blank")
            else None
        )
    completed = [
        submission
        for submission in assignment_submissions(teacher_id, assignment_id)
        if submission["submitted_at"] is not None
    ]
    scores = [submission["score"] for submission in completed]
    return {
        "submission_count": len(completed),
        "average_score": sum(scores) / len(scores) if scores else None,
        "highest_score": max(scores) if scores else None,
        "lowest_score": min(scores) if scores else None,
        "questions": question_stats,
    }


def export_assignment_scores(teacher_id, assignment_id):
    import csv
    import io

    rows = assignment_submissions(teacher_id, assignment_id)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "student_id",
            "username",
            "display_name",
            "answered_count",
            "score",
            "max_score",
            "submitted_at",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def list_assignments(student_id):
    with database() as conn:
        rows = conn.execute(
            """SELECT q.*, a.assigned_at,
                      a.due_at,
                      EXISTS(SELECT 1 FROM attempts t
                             WHERE t.student_id = a.student_id AND t.question_id = q.id) AS attempted
               FROM assignments a JOIN questions q ON q.id = a.question_id
               WHERE a.student_id = ?
               ORDER BY attempted, a.assigned_at DESC""",
            (student_id,),
        )
        personal = [_question_dict(row) for row in rows]
        grouped = conn.execute(
            """SELECT q.*, s.id AS assignment_set_id, s.title AS assignment_title,
                      s.description AS assignment_description, s.starts_at,
                      s.due_at, s.time_limit_minutes, s.allow_retake,
                      s.show_answers, s.status AS assignment_status,
                      EXISTS(SELECT 1 FROM quiz_sessions z
                             WHERE z.assignment_id = s.id AND z.student_id = ?
                               AND z.status = 'submitted') AS attempted
               FROM assignment_recipients r
               JOIN assignment_sets s ON s.id = r.assignment_id
               JOIN assignment_questions aq ON aq.assignment_id = s.id
               JOIN questions q ON q.id = aq.question_id
               WHERE r.student_id = ? AND s.status = 'published'
                 AND (s.starts_at IS NULL OR s.starts_at <= CURRENT_TIMESTAMP)
               ORDER BY s.due_at, s.id, q.id""",
            (student_id, student_id),
        )
        return personal + [_question_dict(row) for row in grouped]


def begin_quiz(student_id, question_ids):
    if not isinstance(question_ids, (list, tuple)):
        raise ValueError("題目清單格式錯誤。")
    ids = list(dict.fromkeys(int(question_id) for question_id in question_ids))
    if not ids or len(ids) > 200:
        raise ValueError("每次練習需選擇 1 至 200 題。")
    quiz_id = secrets.token_urlsafe(24)
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student'",
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError("找不到學生帳號。")
        found = conn.execute(
            f"SELECT id, points FROM questions WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        if len(found) != len(ids):
            raise ValueError("部分題目不存在。")
        conn.execute(
            """INSERT INTO quiz_sessions(id, student_id, question_ids, status, max_score)
               VALUES (?, ?, ?, 'in_progress', ?)""",
            (quiz_id, student_id, json.dumps(ids), sum(row["points"] for row in found)),
        )
    return quiz_id


def begin_filtered_quiz(
    student_id, category, difficulty, question_count, assigned_only=False,
    assignment_id=None,
):
    if isinstance(question_count, bool) or not isinstance(question_count, int):
        raise ValueError("題數必須是整數。")
    max_question_count = 200 if assignment_id is not None else 50
    if not 1 <= question_count <= max_question_count:
        raise ValueError(
            f"每次練習題數需介於 1 至 {max_question_count}。"
        )
    clauses = ["q.is_published = 1"]
    params = []
    if category:
        clauses.append("q.category = ?")
        params.append(category)
    if difficulty:
        clauses.append("q.difficulty = ?")
        params.append(difficulty)
    if assigned_only and assignment_id is None:
        clauses.append(
            "EXISTS (SELECT 1 FROM assignments a WHERE a.student_id = ? AND a.question_id = q.id)"
        )
        params.append(student_id)
    if assignment_id is None:
        clauses.append(
            """NOT EXISTS (
                SELECT 1 FROM attempts previous
                WHERE previous.student_id = ? AND previous.question_id = q.id
            )"""
        )
        params.append(student_id)
    if assignment_id is not None:
        clauses.append(
            """EXISTS (
                SELECT 1 FROM assignment_recipients r
                JOIN assignment_sets s ON s.id = r.assignment_id
                JOIN assignment_questions aq ON aq.assignment_id = s.id
                WHERE r.student_id = ? AND s.id = ? AND s.status = 'published'
                  AND aq.question_id = q.id
                  AND (s.starts_at IS NULL OR s.starts_at <= CURRENT_TIMESTAMP)
                  AND (s.due_at IS NULL OR s.due_at >= CURRENT_TIMESTAMP)
                  AND q.is_published = 1
            )"""
        )
        params.extend([student_id, assignment_id])
    with database() as conn:
        if assignment_id is not None:
            assignment = conn.execute(
                """SELECT allow_retake, status, starts_at, due_at
                   FROM assignment_sets s
                   JOIN assignment_recipients r ON r.assignment_id = s.id
                   WHERE s.id = ? AND r.student_id = ?""",
                (assignment_id, student_id),
            ).fetchone()
            if assignment is None or assignment["status"] != "published":
                raise ValueError("作業不存在或尚未發布。")
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if assignment["starts_at"] and assignment["starts_at"] > now_text:
                raise ValueError("作業尚未開始。")
            if assignment["due_at"] and assignment["due_at"] < now_text:
                raise ValueError("作業已超過截止時間。")
            if not assignment["allow_retake"] and conn.execute(
                """SELECT 1 FROM quiz_sessions
                   WHERE assignment_id = ? AND student_id = ? AND status = 'submitted'""",
                (assignment_id, student_id),
            ).fetchone():
                raise ValueError("此作業已完成，教師未開放重做。")
        rows = conn.execute(
            f"""SELECT q.id FROM questions q
                WHERE {' AND '.join(clauses)}
                ORDER BY RANDOM() LIMIT ?""",
            [*params, question_count],
        ).fetchall()
    if not rows:
        raise ValueError("目前的篩選條件沒有可練習的題目。")
    quiz_id = begin_quiz(student_id, [row["id"] for row in rows])
    if assignment_id is None:
        return quiz_id
    with database() as conn:
        conn.execute(
            "UPDATE quiz_sessions SET assignment_id = ? WHERE id = ? AND student_id = ?",
            (assignment_id, quiz_id, student_id),
        )
    return quiz_id


def get_quiz(quiz_id, student_id):
    with database() as conn:
        session = conn.execute(
            """SELECT * FROM quiz_sessions
               WHERE id = ? AND student_id = ?""",
            (quiz_id, student_id),
        ).fetchone()
        if session is None:
            return None
        question_ids = json.loads(session["question_ids"])
        questions = conn.execute(
            f"SELECT * FROM questions WHERE id IN ({','.join('?' for _ in question_ids)})",
            question_ids,
        ).fetchall()
        questions_by_id = {row["id"]: row for row in questions}
    return {
        "id": session["id"],
        "status": session["status"],
        "score": session["score"],
        "max_score": session["max_score"],
        "correct_count": session["correct_count"],
        "submitted_at": session["submitted_at"],
        "questions": [
            _question_dict(questions_by_id[question_id])
            for question_id in question_ids
            if question_id in questions_by_id
        ],
    }


def submit_quiz(quiz_id, student_id, responses):
    if not isinstance(responses, dict):
        raise ValueError("作答資料格式錯誤。")
    with database() as conn:
        session = conn.execute(
            """SELECT question_ids, assignment_id, created_at FROM quiz_sessions
               WHERE id = ? AND student_id = ? AND status = 'in_progress'""",
            (quiz_id, student_id),
        ).fetchone()
        if session is None:
            raise ValueError("測驗不存在或已經提交，無法重複計分。")
        question_ids = json.loads(session["question_ids"])
        assignment = None
        if session["assignment_id"] is not None:
            assignment = conn.execute(
                """SELECT time_limit_minutes, due_at, show_answers, status
                   FROM assignment_sets WHERE id = ?""",
                (session["assignment_id"],),
            ).fetchone()
            if assignment is None or assignment["status"] != "published":
                raise ValueError("此作業已關閉，無法提交。")
            if assignment["due_at"] and assignment["due_at"] < datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                raise ValueError("作業已超過截止時間。")
            if assignment["time_limit_minutes"]:
                started_at = datetime.fromisoformat(session["created_at"])
                if datetime.now() - started_at > timedelta(
                    minutes=assignment["time_limit_minutes"]
                ):
                    raise ValueError("作答時間已結束，無法提交。")
        if set(map(str, question_ids)) != set(responses):
            raise ValueError("請完成所有題目後再交卷。")
        questions = conn.execute(
            f"SELECT * FROM questions WHERE id IN ({','.join('?' for _ in question_ids)})",
            question_ids,
        ).fetchall()
        by_id = {str(question["id"]): question for question in questions}
        score = 0
        max_score = 0
        correct_count = 0
        result_items = []
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """UPDATE quiz_sessions SET status = 'submitted', submitted_at = ?
               WHERE id = ? AND student_id = ? AND status = 'in_progress'""",
            (now, quiz_id, student_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("測驗已經提交，無法重複計分。")
        for question_id in question_ids:
            question = by_id[str(question_id)]
            response = str(responses[str(question_id)]).strip()
            if not response or len(response) > 20_000:
                raise ValueError("每一題都需要作答，且答案不可超過 20000 個字元。")
            earned = None
            max_score += question["points"]
            if question["question_type"] in ("choice", "fill_blank"):
                accepted_answers = (
                    [question["answer"]]
                    if question["question_type"] == "choice"
                    else json.loads(question["accepted_answers"])
                )
                earned = (
                    question["points"]
                    if response.casefold()
                    in {answer.strip().casefold() for answer in accepted_answers}
                    else 0
                )
                score += earned
                correct_count += int(earned > 0)
            attempt_cursor = conn.execute(
                """INSERT INTO attempts
                   (student_id, question_id, response, earned_points, quiz_id, assignment_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    student_id,
                    question_id,
                    response,
                    earned,
                    quiz_id,
                    session["assignment_id"],
                ),
            )
            if question["question_type"] == "essay":
                conn.execute(
                    """INSERT INTO essay_versions
                       (attempt_id, version_number, content, status, submitted_at)
                       VALUES (?, 1, ?, 'submitted', ?)""",
                    (attempt_cursor.lastrowid, response, now),
                )
            result_items.append(
                {
                    "question_id": question_id,
                    "response": response,
                    "earned_points": earned,
                    "max_points": question["points"],
                    "answer": (
                        question["answer"]
                        if assignment is None or assignment["show_answers"]
                        else ""
                    ),
                    "explanation": (
                        question["explanation"]
                        if assignment is None or assignment["show_answers"]
                        else ""
                    ),
                    "prompt": question["prompt"],
                }
            )
        conn.execute(
            """UPDATE quiz_sessions SET score = ?, max_score = ?, correct_count = ?
               WHERE id = ? AND student_id = ?""",
            (score, max_score, correct_count, quiz_id, student_id),
        )
        return {
            "score": score,
            "max_score": max_score,
            "correct_count": correct_count,
            "question_count": len(question_ids),
            "results": result_items,
        }


def student_progress(student_id):
    with database() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS attempt_count,
                      COUNT(DISTINCT question_id) AS questions_attempted,
                      COALESCE((
                          SELECT SUM(best_score) FROM (
                              SELECT MAX(earned_points) AS best_score
                              FROM attempts
                              WHERE student_id = ? AND earned_points IS NOT NULL
                              GROUP BY question_id
                          )
                      ), 0) AS total_score,
                      SUM(CASE WHEN q.question_type = 'essay' AND a.earned_points IS NULL
                               THEN 1 ELSE 0 END) AS essays_pending
               FROM attempts a
               JOIN questions q ON q.id = a.question_id
               WHERE a.student_id = ?""",
            (student_id, student_id),
        ).fetchone()
        total_score = _student_score(conn, student_id, row["total_score"])
    return {
        "attempt_count": row["attempt_count"],
        "questions_attempted": row["questions_attempted"],
        "total_score": total_score,
        "essays_pending": row["essays_pending"] or 0,
    }


def _earned_student_score(conn, student_id):
    return conn.execute(
        """SELECT COALESCE(SUM(best_score), 0) FROM (
               SELECT MAX(earned_points) AS best_score
               FROM attempts
               WHERE student_id = ? AND earned_points IS NOT NULL
               GROUP BY question_id
           )""",
        (student_id,),
    ).fetchone()[0]


def _student_score(conn, student_id, earned_score):
    wheel_adjustment = conn.execute(
        """SELECT COALESCE(SUM(points_awarded - points_spent), 0)
           FROM wheel_spins WHERE student_id = ?""",
        (student_id,),
    ).fetchone()[0]
    login_points = conn.execute(
        "SELECT COALESCE(SUM(points_awarded), 0) FROM daily_login_rewards WHERE student_id = ?",
        (student_id,),
    ).fetchone()[0]
    return earned_score + wheel_adjustment + login_points


def _claim_daily_login_reward(conn, student_id, login_day=None):
    login_day = login_day or date.today()
    if isinstance(login_day, str):
        try:
            login_day = date.fromisoformat(login_day)
        except ValueError as exc:
            raise ValueError("登入日期格式必須是 YYYY-MM-DD。") from exc
    existing = conn.execute(
        """SELECT streak_day, points_awarded, free_wheel_awarded
           FROM daily_login_rewards
           WHERE student_id = ? AND login_date = ?""",
        (student_id, login_day.isoformat()),
    ).fetchone()
    if existing is not None:
        return None
    student = conn.execute(
        "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
        (student_id,),
    ).fetchone()
    if student is None:
        return None
    previous = conn.execute(
        """SELECT login_date, streak_day FROM daily_login_rewards
           WHERE student_id = ? AND login_date < ?
           ORDER BY login_date DESC LIMIT 1""",
        (student_id, login_day.isoformat()),
    ).fetchone()
    if previous and previous["login_date"] == (login_day - timedelta(days=1)).isoformat():
        streak_day = previous["streak_day"] + 1
    else:
        streak_day = 1
    points_awarded = {5: 30, 10: 50}.get(streak_day, 15)
    free_wheel_awarded = int(streak_day >= 20 and streak_day % 10 == 0)
    conn.execute(
        """INSERT INTO daily_login_rewards
           (student_id, login_date, streak_day, points_awarded, free_wheel_awarded)
           VALUES (?, ?, ?, ?, ?)""",
        (
            student_id,
            login_day.isoformat(),
            streak_day,
            points_awarded,
            free_wheel_awarded,
        ),
    )
    return {
        "login_date": login_day.isoformat(),
        "streak_day": streak_day,
        "points_awarded": points_awarded,
        "free_wheel_awarded": bool(free_wheel_awarded),
    }


def claim_daily_login_reward(student_id, login_day=None):
    with database() as conn:
        return _claim_daily_login_reward(conn, student_id, login_day)


def student_wheel_state(student_id):
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError("只有啟用中的學生可以使用積分轉盤。")
        earned_score = _earned_student_score(conn, student_id)
        score = _student_score(conn, student_id, earned_score)
        special_spins = conn.execute(
            """SELECT COUNT(*) FROM wheel_spins
               WHERE student_id = ? AND wheel_type = 'milestone'""",
            (student_id,),
        ).fetchone()[0]
        free_spins = conn.execute(
            """SELECT COUNT(*) FROM daily_login_rewards r
               WHERE r.student_id = ? AND r.free_wheel_awarded = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM wheel_spins w
                     WHERE w.student_id = r.student_id
                       AND w.wheel_type = 'streak' AND w.streak_day = r.streak_day
                 )""",
            (student_id,),
        ).fetchone()[0]
    next_special_milestone = (special_spins + 1) * 1000
    return {
        "score": score,
        "earned_score": earned_score,
        "special_ready": earned_score >= next_special_milestone,
        "next_special_milestone": next_special_milestone,
        "free_spins": free_spins,
        "can_spin": score >= 150 or free_spins > 0,
    }


def spin_student_wheel(student_id, request_id):
    request_id = str(request_id).strip()
    if not 16 <= len(request_id) <= 128:
        raise ValueError("轉盤請求識別碼無效，請重新整理後再試。")
    with database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError("只有啟用中的學生可以使用積分轉盤。")
        previous_spin = conn.execute(
            """SELECT wheel_type, score_milestone, streak_day,
                      points_spent, points_awarded
               FROM wheel_spins WHERE student_id = ? AND request_id = ?""",
            (student_id, request_id),
        ).fetchone()
        if previous_spin is not None:
            result = dict(previous_spin)
            result["score"] = _student_score(
                conn, student_id, _earned_student_score(conn, student_id)
            )
            return result

        earned_score = _earned_student_score(conn, student_id)
        current_score = _student_score(conn, student_id, earned_score)
        free_spin = conn.execute(
            """SELECT r.streak_day FROM daily_login_rewards r
               WHERE r.student_id = ? AND r.free_wheel_awarded = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM wheel_spins w
                     WHERE w.student_id = r.student_id
                       AND w.wheel_type = 'streak' AND w.streak_day = r.streak_day
                 )
               ORDER BY r.streak_day LIMIT 1""",
            (student_id,),
        ).fetchone()
        if free_spin is not None:
            wheel_type = "streak"
            awarded = secrets.choice((10, 20, 30, 40, 50))
            score_milestone = None
            streak_day = free_spin["streak_day"]
            points_spent = 0
        elif current_score < 150:
            raise ValueError("積分不足 150 分，且目前沒有免費轉盤次數。")
        else:
            streak_day = None
            points_spent = 150
            special_spins = conn.execute(
                """SELECT COUNT(*) FROM wheel_spins
                   WHERE student_id = ? AND wheel_type = 'milestone'""",
                (student_id,),
            ).fetchone()[0]
            milestone = (special_spins + 1) * 1000
            if earned_score >= milestone:
                wheel_type = "milestone"
                awarded = secrets.choice((30, 40, 50, 60, 70))
                score_milestone = milestone
            else:
                wheel_type = "standard"
                awarded = secrets.choice((10, 20, 30, 40, 50))
                score_milestone = None
        conn.execute(
            """INSERT INTO wheel_spins
               (student_id, request_id, wheel_type, score_milestone, streak_day,
                points_spent, points_awarded)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                student_id,
                request_id,
                wheel_type,
                score_milestone,
                streak_day,
                points_spent,
                awarded,
            ),
        )
        return {
            "wheel_type": wheel_type,
            "score_milestone": score_milestone,
            "streak_day": streak_day,
            "points_spent": points_spent,
            "points_awarded": awarded,
            "score": current_score - points_spent + awarded,
        }


def student_accuracy(student_id):
    with database() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS answered_count,
                      SUM(CASE WHEN a.earned_points = q.points THEN 1 ELSE 0 END)
                          AS correct_count
               FROM attempts a
               JOIN questions q ON q.id = a.question_id
               WHERE a.student_id = ?
                 AND q.question_type IN ('choice', 'fill_blank')
                 AND a.earned_points IS NOT NULL""",
            (student_id,),
        ).fetchone()
    if not row["answered_count"]:
        return None
    return row["correct_count"] / row["answered_count"] * 100


def student_attempts(student_id):
    with database() as conn:
        rows = conn.execute(
            """SELECT a.id AS attempt_id, a.response, a.earned_points, a.feedback,
                      a.submitted_at, q.id AS question_id, q.category, q.difficulty,
                      q.question_type, q.prompt, q.options, q.points, q.rubric,
                      q.min_word_count, essay.version_number, essay.status AS essay_status,
                      CASE WHEN s.assignment_id IS NULL OR assignment.show_answers = 1
                           THEN q.answer ELSE '' END AS answer,
                      CASE WHEN s.assignment_id IS NULL OR assignment.show_answers = 1
                           THEN q.explanation ELSE '' END AS explanation
               FROM attempts a JOIN questions q ON q.id = a.question_id
               LEFT JOIN quiz_sessions s ON s.id = a.quiz_id
               LEFT JOIN assignment_sets assignment ON assignment.id = s.assignment_id
               LEFT JOIN essay_versions essay ON essay.id = (
                   SELECT latest.id FROM essay_versions latest
                   WHERE latest.attempt_id = a.id
                   ORDER BY latest.version_number DESC LIMIT 1
               )
               WHERE a.student_id = ?
               ORDER BY a.submitted_at DESC, a.id DESC""",
            (student_id,),
        )
        attempts = [dict(row) for row in rows]
        for attempt in attempts:
            attempt["options"] = json.loads(attempt["options"])
        return attempts


def list_essay_attempts(teacher_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        rows = conn.execute(
            """SELECT a.id AS attempt_id, a.student_id, a.response, a.earned_points,
                      a.submitted_at, a.feedback, u.username, u.display_name,
                      q.id AS question_id, q.prompt, q.points, q.category,
                      q.rubric, q.min_word_count,
                      essay.version_number, essay.status AS essay_status
               FROM attempts a JOIN users u ON u.id = a.student_id
               JOIN questions q ON q.id = a.question_id
               LEFT JOIN essay_versions essay ON essay.id = (
                   SELECT latest.id FROM essay_versions latest
                   WHERE latest.attempt_id = a.id
                   ORDER BY latest.version_number DESC LIMIT 1
               )
               WHERE q.question_type = 'essay'
               ORDER BY CASE WHEN essay.status = 'submitted' THEN 0 ELSE 1 END,
                        a.submitted_at DESC"""
        )
        return [dict(row) for row in rows]


def leaderboard():
    with database() as conn:
        rows = conn.execute(
            """SELECT u.id, u.username, u.display_name,
                      COALESCE(scores.score, 0) + COALESCE(wheel_scores.net_score, 0)
                          + COALESCE(login_scores.points, 0) AS score,
                      COALESCE(accuracy.correct_count, 0) AS correct_count,
                      COALESCE(accuracy.answered_count, 0) AS answered_count
               FROM users u
               LEFT JOIN (
                   SELECT student_id, SUM(score) AS score
                   FROM (
                       SELECT student_id, question_id, MAX(earned_points) AS score
                       FROM attempts
                       WHERE earned_points IS NOT NULL
                       GROUP BY student_id, question_id
                   )
                   GROUP BY student_id
               ) scores ON scores.student_id = u.id
               LEFT JOIN (
                   SELECT student_id, SUM(points_awarded - points_spent) AS net_score
                   FROM wheel_spins
                   GROUP BY student_id
               ) wheel_scores ON wheel_scores.student_id = u.id
               LEFT JOIN (
                   SELECT student_id, SUM(points_awarded) AS points
                   FROM daily_login_rewards
                   GROUP BY student_id
               ) login_scores ON login_scores.student_id = u.id
               LEFT JOIN (
                   SELECT a.student_id,
                          SUM(CASE WHEN a.earned_points = q.points THEN 1 ELSE 0 END)
                              AS correct_count,
                          COUNT(*) AS answered_count
                   FROM attempts a
                   JOIN questions q ON q.id = a.question_id
                   WHERE q.question_type IN ('choice', 'fill_blank')
                     AND a.earned_points IS NOT NULL
                   GROUP BY a.student_id
               ) accuracy ON accuracy.student_id = u.id
               WHERE u.role = 'student'
               GROUP BY u.id
               ORDER BY score DESC"""
        )
        result = [dict(row) for row in rows]
    previous_score = None
    rank = 0
    for position, student in enumerate(result, start=1):
        if student["score"] != previous_score:
            rank = position
            previous_score = student["score"]
        student["rank"] = rank
        student["accuracy"] = (
            student["correct_count"] / student["answered_count"] * 100
            if student["answered_count"]
            else None
        )
    return result


def create_note(author_id, content):
    content = str(content).strip()
    if not content or len(content) > 2000:
        raise ValueError("便利貼內容需為 1 至 2000 個字元。")
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student'",
            (author_id,),
        ).fetchone()
        if student is None:
            raise ValueError("只有學生可以發布便利貼。")
        cursor = conn.execute(
            "INSERT INTO notes(author_id, content) VALUES (?, ?)", (author_id, content)
        )
        return cursor.lastrowid


def reply_to_note(note_id, author_id, content):
    content = str(content).strip()
    if not content or len(content) > 1000:
        raise ValueError("回覆需為 1 至 1000 個字元。")
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student'",
            (author_id,),
        ).fetchone()
        exists = conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
        if student is None:
            raise ValueError("只有學生可以回覆便利貼。")
        if not exists:
            raise ValueError("找不到此便利貼。")
        conn.execute(
            "INSERT INTO note_replies(note_id, author_id, content) VALUES (?, ?, ?)",
            (note_id, author_id, content),
        )


def update_note(note_id, author_id, content):
    content = str(content).strip()
    if not content or len(content) > 2000:
        raise ValueError("便利貼內容需為 1 至 2000 個字元。")
    with database() as conn:
        cursor = conn.execute(
            "UPDATE notes SET content = ? WHERE id = ? AND author_id = ?",
            (content, note_id, author_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("只能編輯自己的便利貼。")


def delete_note(note_id, author_id):
    with database() as conn:
        cursor = conn.execute(
            "DELETE FROM notes WHERE id = ? AND author_id = ?",
            (note_id, author_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("只能刪除自己的便利貼。")


def delete_note_as_teacher(teacher_id, note_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if cursor.rowcount != 1:
            raise ValueError("找不到此便利貼。")
        _audit(conn, teacher_id, "delete_note", "note", note_id)


def report_note(note_id, reporter_id, reason):
    reason = str(reason).strip()
    if not reason or len(reason) > 500:
        raise ValueError("請提供 1 至 500 個字元的檢舉原因。")
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student'",
            (reporter_id,),
        ).fetchone()
        note = conn.execute(
            "SELECT author_id FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if student is None:
            raise ValueError("只有學生可以檢舉便利貼。")
        if note is None or note["author_id"] == reporter_id:
            raise ValueError("無法檢舉這張便利貼。")
        try:
            conn.execute(
                """INSERT INTO note_reports(note_id, reporter_id, reason)
                   VALUES (?, ?, ?)""",
                (note_id, reporter_id, reason),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("你已經檢舉過這張便利貼。") from exc


def list_note_reports(teacher_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        return [
            dict(row)
            for row in conn.execute(
                """SELECT r.id, r.reason, r.status, r.created_at, n.id AS note_id,
                          n.content AS note_content, author.display_name AS note_author,
                          reporter.display_name AS reporter_name
                   FROM note_reports r
                   JOIN notes n ON n.id = r.note_id
                   JOIN users author ON author.id = n.author_id
                   JOIN users reporter ON reporter.id = r.reporter_id
                   ORDER BY r.created_at DESC"""
            )
        ]


def resolve_note_report(teacher_id, report_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            "UPDATE note_reports SET status = 'resolved' WHERE id = ?",
            (report_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此檢舉。")


def report_question(question_id, reporter_id, reason):
    reason = str(reason).strip()
    if not reason or len(reason) > 500:
        raise ValueError("請提供 1 至 500 個字元的題目問題說明。")
    with database() as conn:
        student = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'student' AND is_active = 1",
            (reporter_id,),
        ).fetchone()
        attempted = conn.execute(
            "SELECT 1 FROM attempts WHERE question_id = ? AND student_id = ?",
            (question_id, reporter_id),
        ).fetchone()
        if student is None:
            raise ValueError("只有啟用中的學生可以回報題目問題。")
        if attempted is None:
            raise ValueError("只能回報自己作答過的題目。")
        try:
            conn.execute(
                """INSERT INTO question_reports(question_id, reporter_id, reason)
                   VALUES (?, ?, ?)""",
                (question_id, reporter_id, reason),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("你已經回報過這道題目。") from exc


def list_question_reports(teacher_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        reports = [
            dict(row)
            for row in conn.execute(
                """SELECT r.id, r.reason, r.status, r.created_at,
                          q.id AS question_id, q.prompt, q.options, q.category,
                          q.difficulty, reporter.display_name AS reporter_name
                   FROM question_reports r
                   JOIN questions q ON q.id = r.question_id
                   JOIN users reporter ON reporter.id = r.reporter_id
                   ORDER BY r.created_at DESC, r.id DESC"""
            )
        ]
        for report in reports:
            report["options"] = json.loads(report["options"])
        return reports


def resolve_question_report(teacher_id, report_id):
    with database() as conn:
        _require_teacher(conn, teacher_id)
        cursor = conn.execute(
            "UPDATE question_reports SET status = 'resolved' WHERE id = ?",
            (report_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("找不到此題目回報。")
        _audit(conn, teacher_id, "resolve_question_report", "question_report", report_id)


def list_notes():
    with database() as conn:
        notes = [
            dict(row)
            for row in conn.execute(
                """SELECT n.id, n.author_id, n.content, n.created_at,
                          u.username, u.display_name
                   FROM notes n JOIN users u ON u.id = n.author_id
                   ORDER BY n.created_at DESC, n.id DESC"""
            )
        ]
        for note in notes:
            note["replies"] = [
                dict(row)
                for row in conn.execute(
                    """SELECT r.content, r.created_at, u.username, u.display_name
                       FROM note_replies r JOIN users u ON u.id = r.author_id
                       WHERE r.note_id = ? ORDER BY r.created_at, r.id""",
                    (note["id"],),
                )
            ]
    return notes


def daily_backup(keep_days=14):
    backup_dir = Path(
        os.environ.get("CCSS_BACKUP_DIR", str(DB_PATH.parent / "backups"))
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    backup_path = backup_dir / f"ccss-{today.isoformat()}.db"
    if not backup_path.exists():
        temporary_path = backup_dir / f".{backup_path.stem}-{secrets.token_hex(6)}.tmp"
        try:
            destination = sqlite3.connect(temporary_path)
            try:
                with database() as source:
                    source.backup(destination)
            finally:
                destination.close()
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(backup_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    backups = sorted(
        path
        for path in backup_dir.iterdir()
        if re.fullmatch(r"ccss-\d{4}-\d{2}-\d{2}\.db", path.name)
    )
    for old_backup in backups[:-max(keep_days, 1)]:
        old_backup.unlink()
    return backup_path


if __name__ == "__main__":
    print(daily_backup())
