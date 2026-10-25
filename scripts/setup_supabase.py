import argparse
import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEACHER_PASSWORD = "teacheremma123"


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def migrate_existing_users(conn):
    exists = conn.execute(
        "SELECT to_regclass('public.users') AS users"
    ).fetchone()["users"]
    if not exists:
        return
    conn.execute(
        """ALTER TABLE users
           ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '',
           ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
           ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE,
           ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"""
    )


def source_rows(connection, table):
    try:
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.OperationalError:
        return []


def parse_json(value, fallback):
    if not value:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def import_legacy_data(target, sqlite_path):
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA foreign_keys = ON")
    imported = {}

    for row in source_rows(source, "users"):
        target.execute(
            """INSERT INTO users
               (id, username, display_name, password_hash, role, must_change_password,
                email, is_active, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["username"], row["display_name"], row["password_hash"],
             row["role"], bool(row["must_change_password"]), row.get("email", ""),
             bool(row.get("is_active", 1)), row["created_at"]),
        )
    imported["users"] = len(source_rows(source, "users"))

    for table in ("classes",):
        for row in source_rows(source, table):
            target.execute(
                """INSERT INTO classes(id, teacher_id, name, created_at)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                (row["id"], row["teacher_id"], row["name"], row["created_at"]),
            )
        imported[table] = len(source_rows(source, table))
    for row in source_rows(source, "class_memberships"):
        target.execute(
            """INSERT INTO class_memberships(class_id, student_id, added_at)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (row["class_id"], row["student_id"], row["added_at"]),
        )
    imported["class_memberships"] = len(source_rows(source, "class_memberships"))

    for row in source_rows(source, "question_banks"):
        target.execute(
            """INSERT INTO question_banks(id, name, description, created_by, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["name"], row.get("description", ""), row["created_by"],
             row["created_at"], row.get("updated_at", row["created_at"])),
        )
    imported["question_banks"] = len(source_rows(source, "question_banks"))

    for row in source_rows(source, "questions"):
        target.execute(
            """INSERT INTO questions
               (id, bank_id, category, scope, grade_level, difficulty, question_type, prompt,
                options, answer, accepted_answers, explanation, rubric, min_word_count,
                points, is_published, created_by, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (row["id"], row.get("bank_id"), row["category"], row.get("scope", ""),
             row.get("grade_level", ""), row["difficulty"], row["question_type"],
             row["prompt"], Jsonb(parse_json(row.get("options"), [])), row.get("answer", ""),
             Jsonb(parse_json(row.get("accepted_answers"), [])), row.get("explanation", ""),
             row.get("rubric", ""), row.get("min_word_count", 0), row["points"],
             bool(row.get("is_published", 1)), row.get("created_by"), row["created_at"]),
        )
    imported["questions"] = len(source_rows(source, "questions"))

    direct_assignments = {}
    for row in source_rows(source, "assignment_sets"):
        target.execute(
            """INSERT INTO assignment_sets
               (id, teacher_id, title, description, starts_at, due_at, time_limit_minutes,
                allow_retake, show_answers, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["teacher_id"], row["title"], row.get("description", ""),
             row.get("starts_at"), row.get("due_at"), row.get("time_limit_minutes") or 0,
             bool(row.get("allow_retake", 0)), bool(row.get("show_answers", 1)),
             row["status"] if row["status"] in {"draft", "published", "closed"} else "closed",
             row["created_at"]),
        )
    for row in source_rows(source, "assignment_questions"):
        target.execute(
            """INSERT INTO assignment_questions(assignment_id, question_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (row["assignment_id"], row["question_id"]),
        )
    recipients = source_rows(source, "assignment_recipients") or source_rows(source, "assignment_students")
    for row in recipients:
        target.execute(
            """INSERT INTO assignment_students(assignment_id, student_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (row["assignment_id"], row["student_id"]),
        )
    for row in source_rows(source, "assignments"):
        teacher_row = target.execute(
            """SELECT u.id AS teacher_id FROM questions q
               LEFT JOIN users u ON u.id = q.created_by AND u.role = 'teacher'
               WHERE q.id = %s""",
            (row["question_id"],),
        ).fetchone()
        teacher_id = teacher_row["teacher_id"] if teacher_row else None
        if teacher_id is None:
            default_teacher = target.execute(
                "SELECT id FROM users WHERE role = 'teacher' ORDER BY id LIMIT 1"
            ).fetchone()
            if not default_teacher:
                continue
            teacher_id = default_teacher["id"]
        assignment_id = target.execute(
            """INSERT INTO assignment_sets
               (teacher_id, title, due_at, status)
               VALUES (%s, %s, %s, 'published') RETURNING id""",
            (teacher_id, f"指派題目 #{row['question_id']}", row.get("due_at")),
        ).fetchone()["id"]
        target.execute(
            """INSERT INTO assignment_questions(assignment_id, question_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (assignment_id, row["question_id"]),
        )
        target.execute(
            """INSERT INTO assignment_students(assignment_id, student_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (assignment_id, row["student_id"]),
        )
        direct_assignments[(row["student_id"], row["question_id"])] = assignment_id
    imported["assignments"] = (
        len(source_rows(source, "assignment_sets")) + len(source_rows(source, "assignments"))
    )

    for row in source_rows(source, "attempts"):
        target.execute(
            """INSERT INTO attempts
               (id, student_id, question_id, response, earned_points, feedback,
                submitted_at, quiz_id, assignment_id, revision_requested)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, FALSE)
               ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["student_id"], row["question_id"], row.get("response", ""),
             row.get("earned_points"), row.get("feedback", ""), row["submitted_at"],
             row.get("assignment_id")
             or direct_assignments.get((row["student_id"], row["question_id"]))),
        )
    imported["attempts"] = len(source_rows(source, "attempts"))

    for row in source_rows(source, "essay_versions"):
        target.execute(
            """INSERT INTO essay_versions
               (id, attempt_id, version_number, content, status, score, feedback, submitted_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["attempt_id"], row["version_number"], row["content"],
             row["status"], row.get("score"), row.get("feedback", ""), row["submitted_at"]),
        )
    for row in source_rows(source, "essay_drafts"):
        target.execute(
            """INSERT INTO essay_drafts(student_id, question_id, content, updated_at)
               VALUES (%s, %s, %s, %s) ON CONFLICT (student_id, question_id) DO NOTHING""",
            (row["student_id"], row["question_id"], row["content"], row["updated_at"]),
        )
    for row in source_rows(source, "essay_comments"):
        target.execute(
            """INSERT INTO essay_comments
               (id, attempt_id, teacher_id, version_number, quoted_text, comment, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["attempt_id"], row["teacher_id"], row.get("version_number", 1),
             row["quoted_text"], row["comment"], row["created_at"]),
        )

    for row in source_rows(source, "announcements"):
        target.execute(
            """INSERT INTO announcements(id, teacher_id, title, content, published_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["teacher_id"], row["title"], row["content"], row["published_at"]),
        )
    for row in source_rows(source, "notes"):
        target.execute(
            """INSERT INTO notes(id, author_id, content, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["author_id"], row["content"], row["created_at"],
             row.get("updated_at", row["created_at"])),
        )
    for row in source_rows(source, "note_replies"):
        target.execute(
            """INSERT INTO note_replies(id, note_id, author_id, content, created_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["note_id"], row["author_id"], row["content"], row["created_at"]),
        )
    for row in source_rows(source, "note_reports"):
        target.execute(
            """INSERT INTO note_reports(id, note_id, reporter_id, reason, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["note_id"], row["reporter_id"], row["reason"],
             "open" if row["status"] == "pending" else row["status"], row["created_at"]),
        )
    for row in source_rows(source, "question_reports"):
        target.execute(
            """INSERT INTO question_reports(id, question_id, reporter_id, reason, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["question_id"], row["reporter_id"], row["reason"],
             row["status"], row["created_at"]),
        )
    for row in source_rows(source, "daily_login_rewards"):
        target.execute(
            """INSERT INTO daily_login_rewards
               (id, student_id, login_date, streak_day, points_awarded, free_wheel_awarded, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["student_id"], row["login_date"], row["streak_day"],
             row["points_awarded"], bool(row["free_wheel_awarded"]), row["created_at"]),
        )
    for row in source_rows(source, "wheel_spins"):
        target.execute(
            """INSERT INTO wheel_spins
               (id, student_id, request_id, wheel_type, score_milestone, streak_day,
                points_spent, points_awarded, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["student_id"], row["request_id"], row["wheel_type"],
             row.get("score_milestone"), row.get("streak_day"), row["points_spent"],
             row["points_awarded"], row["created_at"]),
        )
    for row in source_rows(source, "audit_logs"):
        target.execute(
            """INSERT INTO audit_logs(id, actor_id, action, entity_type, entity_id, details, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row.get("actor_id"), row["action"], row["entity_type"],
             row.get("entity_id", ""), Jsonb(parse_json(row.get("details"), {})),
             row["created_at"]),
        )
    for row in source_rows(source, "login_events"):
        target.execute(
            """INSERT INTO login_events(id, user_id, username, succeeded, created_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row.get("user_id"), row["username"], bool(row["succeeded"]),
             row["created_at"]),
        )
    for row in source_rows(source, "import_batches"):
        target.execute(
            """INSERT INTO import_batches(id, teacher_id, content_hash, imported_count, created_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (row["id"], row["teacher_id"], row["content_hash"], row["imported_count"],
             row["created_at"]),
        )

    imported["announcements"] = len(source_rows(source, "announcements"))
    imported["notes"] = len(source_rows(source, "notes"))
    source.close()
    return imported


def main():
    parser = argparse.ArgumentParser(description="Prepare the CCSS Supabase database.")
    parser.add_argument(
        "--sqlite",
        type=Path,
        help="Optionally import existing CCSS SQLite users, classes, questions, scores, and content.",
    )
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL to the Supabase PostgreSQL connection string.")

    schema = (ROOT / "api" / "schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(database_url, connect_timeout=10, row_factory=dict_row) as conn:
        migrate_existing_users(conn)
        conn.execute(schema, prepare=False)
        imported = import_legacy_data(conn, args.sqlite) if args.sqlite else {}
        teacher = conn.execute(
            """SELECT id, password_hash, must_change_password FROM users
               WHERE LOWER(username) = 'teacher' AND role = 'teacher'"""
        ).fetchone()
        if not teacher:
            password = os.environ.get(
                "CCSS_TEACHER_PASSWORD", DEFAULT_TEACHER_PASSWORD
            )
            if len(password) < 8:
                raise SystemExit("CCSS_TEACHER_PASSWORD must be at least 8 characters.")
            conn.execute(
                """INSERT INTO users(username, display_name, password_hash, role,
                                      must_change_password)
                   VALUES ('teacher', 'Teacher', %s, 'teacher', TRUE)""",
                (hash_password(password),),
            )
        elif teacher["must_change_password"] and verify_password(
            "teacher", teacher["password_hash"]
        ):
            password = os.environ.get(
                "CCSS_TEACHER_PASSWORD", DEFAULT_TEACHER_PASSWORD
            )
            if len(password) < 8:
                raise SystemExit("CCSS_TEACHER_PASSWORD must be at least 8 characters.")
            conn.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (hash_password(password), teacher["id"]),
            )
        identity_tables = (
            "users", "classes", "question_banks", "import_batches", "questions",
            "assignment_sets", "attempts", "essay_versions", "essay_comments",
            "daily_login_rewards", "wheel_spins", "announcements", "notes",
            "note_replies", "note_reports", "question_reports", "audit_logs",
            "login_events",
        )
        for table in identity_tables:
            conn.execute(
                """SELECT setval(pg_get_serial_sequence(%s, 'id'),
                   COALESCE((SELECT MAX(id) FROM """ + table + """), 1),
                   (SELECT MAX(id) IS NOT NULL FROM """ + table + """))""",
                (table,),
            )
    print("Supabase schema is ready.")
    if imported:
        print("Imported row counts:")
        for table, count in imported.items():
            print(f"  {table}: {count}")
    print("Teacher account is available; the first login requires changing its password.")


if __name__ == "__main__":
    main()
