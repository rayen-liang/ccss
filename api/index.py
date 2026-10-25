import hashlib
import hmac
import csv
import io
import json
import logging
import os
import re
import secrets
import smtplib
import zipfile
import xml.etree.ElementTree as ET
from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row


app = FastAPI(title="CCSS API", version="1.0.0")
logger = logging.getLogger("ccss")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

COOKIE_NAME = "ccss_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
PASSWORD_ITERATIONS = 310_000
DIFFICULTIES = {"初級", "中級", "中高級", "高級"}


def _serializer():
    secret = os.environ.get("CCSS_SESSION_SECRET")
    if not secret or len(secret) < 32:
        raise HTTPException(
            status_code=503,
            detail="管理員尚未設定至少 32 字元的 CCSS_SESSION_SECRET。",
        )
    return URLSafeTimedSerializer(secret, salt="ccss-session-v1")


@contextmanager
def database():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise HTTPException(
            status_code=503,
            detail="尚未設定 Supabase PostgreSQL 的 DATABASE_URL。",
        )
    try:
        with psycopg.connect(
            url,
            row_factory=dict_row,
            connect_timeout=10,
            autocommit=False,
        ) as conn:
            yield conn
    except psycopg.OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="無法連線資料庫，請檢查 DATABASE_URL。"
        ) from exc


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
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


def _validate_password(password: str):
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密碼至少需要 8 個字元。")


def _user_view(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "email": user["email"],
        "must_change_password": user["must_change_password"],
        "created_at": user["created_at"].isoformat(),
    }


def _current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="請先登入。")
    try:
        data = _serializer().loads(token, max_age=SESSION_TTL_SECONDS)
    except SignatureExpired as exc:
        raise HTTPException(status_code=401, detail="登入已逾期，請重新登入。") from exc
    except BadSignature as exc:
        raise HTTPException(status_code=401, detail="登入狀態無效，請重新登入。") from exc
    with database() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id = %s AND is_active = TRUE",
            (data.get("user_id"),),
        ).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="帳號已停用或不存在。")
    return user


def _require_ready_user(user=Depends(_current_user)):
    if user["must_change_password"]:
        raise HTTPException(status_code=403, detail="首次登入請先修改密碼。")
    return user


def _require_student(user=Depends(_current_user)):
    if user["role"] != "student":
        raise HTTPException(status_code=403, detail="此功能僅限學生使用。")
    if user["must_change_password"]:
        raise HTTPException(status_code=403, detail="首次登入請先修改密碼。")
    return user


def _require_teacher(user=Depends(_current_user)):
    if user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="此功能僅限教師使用。")
    if user["must_change_password"]:
        raise HTTPException(status_code=403, detail="首次登入請先修改密碼。")
    return user


def _daily_reward(conn, student_id: int):
    today = date.today()
    latest = conn.execute(
        """SELECT login_date, streak_day FROM daily_login_rewards
           WHERE student_id = %s ORDER BY login_date DESC LIMIT 1""",
        (student_id,),
    ).fetchone()
    if latest and latest["login_date"] == today:
        return None
    streak = (
        latest["streak_day"] + 1
        if latest and latest["login_date"] == today - timedelta(days=1)
        else 1
    )
    points = 30 if streak == 5 else 50 if streak == 10 else 15
    free_spin = streak >= 20 and streak % 10 == 0
    inserted = conn.execute(
        """INSERT INTO daily_login_rewards
           (student_id, login_date, streak_day, points_awarded, free_wheel_awarded)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (student_id, login_date) DO NOTHING
           RETURNING points_awarded, streak_day, free_wheel_awarded""",
        (student_id, today, streak, points, free_spin),
    ).fetchone()
    return inserted


def _audit(conn, actor_id, action, entity_type, entity_id="", details=None):
    conn.execute(
        """INSERT INTO audit_logs(actor_id, action, entity_type, entity_id, details)
           VALUES (%s, %s, %s, %s, %s)""",
        (actor_id, action, entity_type, str(entity_id), json.dumps(details or {})),
    )


def _question_view(question: dict[str, Any], reveal=False) -> dict[str, Any]:
    result = {key: question[key] for key in (
        "id", "bank_id", "category", "scope", "grade_level", "difficulty",
        "question_type", "prompt", "options", "points", "rubric", "min_word_count",
        "is_published",
    )}
    if reveal:
        result["answer"] = question["answer"]
        result["accepted_answers"] = question["accepted_answers"]
        result["explanation"] = question["explanation"]
    return result


def _student_score(conn, student_id):
    row = conn.execute(
        """WITH best_answers AS (
               SELECT question_id, MAX(COALESCE(earned_points, 0)) AS points
               FROM attempts WHERE student_id = %s GROUP BY question_id
           ), total AS (
               SELECT COALESCE((SELECT SUM(points) FROM best_answers), 0)
                    + COALESCE((SELECT SUM(points_awarded) FROM daily_login_rewards
                                WHERE student_id = %s), 0)
                    + COALESCE((SELECT SUM(points_awarded - points_spent)
                                FROM wheel_spins WHERE student_id = %s), 0) AS score
           )
           SELECT score FROM total""",
        (student_id, student_id, student_id),
    ).fetchone()
    return row["score"]


def _leaderboard_rows(conn):
    return conn.execute(
        """WITH student_score AS (
               SELECT u.id, u.display_name,
                   COALESCE((SELECT SUM(best.points) FROM (
                       SELECT MAX(COALESCE(a.earned_points, 0)) AS points
                       FROM attempts a WHERE a.student_id = u.id GROUP BY a.question_id
                   ) best), 0)
                   + COALESCE((SELECT SUM(points_awarded) FROM daily_login_rewards r
                               WHERE r.student_id = u.id), 0)
                   + COALESCE((SELECT SUM(points_awarded - points_spent)
                               FROM wheel_spins w WHERE w.student_id = u.id), 0) AS score
               FROM users u WHERE u.role = 'student' AND u.is_active = TRUE
           )
           SELECT id, display_name, score,
               RANK() OVER (ORDER BY score DESC) AS rank
           FROM student_score ORDER BY score DESC, display_name LIMIT 100"""
    ).fetchall()


@app.get("/api/health")
def health():
    try:
        with database() as conn:
            missing_tables = conn.execute(
                """SELECT name FROM unnest(ARRAY[
                       'users', 'login_events', 'daily_login_rewards'
                   ]) AS required(name)
                   WHERE to_regclass('public.' || name) IS NULL"""
            ).fetchall()
            if missing_tables:
                names = ", ".join(row["name"] for row in missing_tables)
                raise HTTPException(
                    status_code=503,
                    detail=f"資料庫尚未完成初始化，缺少必要資料表：{names}。請執行 scripts/setup_supabase.py。",
                )
            conn.execute(
                "SELECT email, must_change_password, is_active FROM users LIMIT 0"
            )
    except psycopg.Error as exc:
        logger.exception("Database schema check failed")
        raise HTTPException(
            status_code=503,
            detail="資料庫結構尚未更新，請管理員重新執行 scripts/setup_supabase.py。",
        ) from exc
    return {"status": "ok"}


@app.post("/api/auth/register")
def register(payload: dict[str, Any], response: Response):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    email = str(payload.get("email", "")).strip().casefold()
    if not 3 <= len(username) <= 32 or re.search(r"\s", username):
        raise HTTPException(status_code=400, detail="帳號需為 3 至 32 字元且不可有空白。")
    if payload.get("confirm") is not None and password != str(payload["confirm"]):
        raise HTTPException(status_code=400, detail="兩次輸入的密碼不一致。")
    _validate_password(password)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        raise HTTPException(status_code=400, detail="請輸入有效的電子郵件地址。")
    try:
        with database() as conn:
            with conn.transaction():
                user = conn.execute(
                    """INSERT INTO users(username, display_name, password_hash, role, email)
                       VALUES (%s, %s, %s, 'student', %s)
                       RETURNING *""",
                    (username, username, _hash_password(password), email),
                ).fetchone()
                reward = _daily_reward(conn, user["id"])
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="帳號或電子郵件已被使用。") from exc
    except psycopg.Error as exc:
        logger.exception("Student registration failed")
        raise HTTPException(
            status_code=503,
            detail="資料庫尚未完成初始化或結構更新，請管理員重新執行 scripts/setup_supabase.py。",
        ) from exc
    response.set_cookie(
        COOKIE_NAME,
        _serializer().dumps({"user_id": user["id"]}),
        httponly=True,
        secure=os.environ.get("CCSS_COOKIE_SECURE", "true").casefold() != "false",
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return {"user": _user_view(user), "daily_reward": reward}


@app.post("/api/auth/login")
def login(payload: dict[str, Any], response: Response):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or len(username) > 32 or re.search(r"\s", username):
        raise HTTPException(status_code=400, detail="請輸入有效的登入帳號。")
    if not password or len(password) > 1024:
        raise HTTPException(status_code=400, detail="請輸入有效的密碼。")
    reward = None
    succeeded = False
    try:
        with database() as conn:
            with conn.transaction():
                user = conn.execute(
                    "SELECT * FROM users WHERE LOWER(username) = LOWER(%s)",
                    (username,),
                ).fetchone()
                succeeded = bool(
                    user and user["is_active"] and _verify_password(password, user["password_hash"])
                )
                conn.execute(
                    """INSERT INTO login_events(user_id, username, succeeded)
                       VALUES (%s, %s, %s)""",
                    (user["id"] if user else None, username[:32], succeeded),
                )
                if succeeded:
                    reward = _daily_reward(conn, user["id"]) if user["role"] == "student" else None
    except psycopg.Error as exc:
        logger.exception("Authentication database operation failed")
        raise HTTPException(
            status_code=503,
            detail="登入服務的資料庫尚未初始化或結構過舊，請管理員執行 scripts/setup_supabase.py；若仍失敗，請檢查 Vercel Function logs。",
        ) from exc
    if not succeeded:
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤。")
    response.set_cookie(
        COOKIE_NAME,
        _serializer().dumps({"user_id": user["id"]}),
        httponly=True,
        secure=os.environ.get("CCSS_COOKIE_SECURE", "true").casefold() != "false",
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return {"user": _user_view(user), "daily_reward": reward}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=os.environ.get("CCSS_COOKIE_SECURE", "true").casefold() != "false",
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@app.post("/api/auth/request-reset")
def request_password_reset(payload: dict[str, Any], request: Request):
    email = str(payload.get("email", "")).strip().casefold()
    generic = {
        "message": "若此電子郵件已註冊，重設連結將會寄出。請檢查收件匣與垃圾郵件匣。"
    }
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return generic
    with database() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE LOWER(email) = %s AND is_active = TRUE",
            (email,),
        ).fetchone()
        if not user:
            return generic
        conn.execute(
            "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE user_id = %s AND used_at IS NULL",
            (user["id"],),
        )
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn.execute(
            """INSERT INTO password_resets(user_id, token_hash, expires_at)
               VALUES (%s, %s, CURRENT_TIMESTAMP + INTERVAL '30 minutes')""",
            (user["id"], token_hash),
        )
    host = os.environ.get("CCSS_SMTP_HOST")
    sender = os.environ.get("CCSS_SMTP_FROM")
    public_url = os.environ.get("CCSS_PUBLIC_URL", str(request.base_url).rstrip("/"))
    if not host or not sender:
        logger.error("Password reset email is not configured.")
        return generic
    message = EmailMessage()
    message["Subject"] = "CCSS 密碼重設"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"請在 30 分鐘內使用以下連結重設密碼：\n{public_url}/?reset={token}\n"
    )
    try:
        port = int(os.environ.get("CCSS_SMTP_PORT", "587"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10) as server:
                _send_reset_email(server, message)
        else:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.starttls()
                _send_reset_email(server, message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        logger.exception("Could not send a password reset email.")
        raise HTTPException(status_code=503, detail="目前無法寄送重設郵件，請稍後再試。") from exc
    return generic


def _send_reset_email(server, message):
    username = os.environ.get("CCSS_SMTP_USER")
    password = os.environ.get("CCSS_SMTP_PASSWORD")
    if username and password:
        server.login(username, password)
    server.send_message(message)


@app.post("/api/auth/reset")
def reset_password(payload: dict[str, Any]):
    token = str(payload.get("token", ""))
    password = str(payload.get("password", ""))
    _validate_password(password)
    if len(token) < 32 or len(token) > 200:
        raise HTTPException(status_code=400, detail="重設連結無效或已過期。")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with database() as conn:
        with conn.transaction():
            reset = conn.execute(
                """SELECT id, user_id FROM password_resets
                   WHERE token_hash = %s AND used_at IS NULL
                     AND expires_at > CURRENT_TIMESTAMP FOR UPDATE""",
                (token_hash,),
            ).fetchone()
            if not reset:
                raise HTTPException(status_code=400, detail="重設連結無效或已過期。")
            conn.execute(
                """UPDATE users SET password_hash = %s, must_change_password = FALSE
                   WHERE id = %s""",
                (_hash_password(password), reset["user_id"]),
            )
            conn.execute(
                """UPDATE password_resets SET used_at = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND used_at IS NULL""",
                (reset["user_id"],),
            )
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(_current_user)):
    return {"user": _user_view(user)}


@app.get("/api/me/login-history")
def login_history(user=Depends(_require_ready_user)):
    with database() as conn:
        return conn.execute(
            """SELECT succeeded, created_at FROM login_events
               WHERE user_id = %s ORDER BY created_at DESC LIMIT 20""",
            (user["id"],),
        ).fetchall()


@app.post("/api/auth/password")
def change_password(payload: dict[str, Any], user=Depends(_current_user)):
    old = str(payload.get("current_password", ""))
    new = str(payload.get("new_password", ""))
    if not _verify_password(old, user["password_hash"]):
        raise HTTPException(status_code=400, detail="目前密碼不正確。")
    _validate_password(new)
    with database() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
            (_hash_password(new), user["id"]),
        )
    return {"ok": True}


@app.patch("/api/me")
def update_profile(payload: dict[str, Any], user=Depends(_current_user)):
    name = str(payload.get("display_name", user["display_name"])).strip()
    email = str(payload.get("email", user["email"])).strip().casefold()
    if not 1 <= len(name) <= 60:
        raise HTTPException(status_code=400, detail="暱稱需為 1 至 60 個字元。")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="電子郵件格式不正確。")
    try:
        with database() as conn:
            conn.execute(
                "UPDATE users SET display_name = %s, email = %s WHERE id = %s",
                (name, email, user["id"]),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="此電子郵件已被其他帳號使用。") from exc
    return {"display_name": name, "email": email}


@app.get("/api/student/dashboard")
def student_dashboard(user=Depends(_require_student)):
    with database() as conn:
        score = _student_score(conn, user["id"])
        accuracy = conn.execute(
            """SELECT COALESCE(ROUND(100.0 * COUNT(*) FILTER (WHERE a.earned_points > 0)
                   / NULLIF(COUNT(*) FILTER (WHERE a.earned_points IS NOT NULL), 0)), 0) AS accuracy,
                   COUNT(*) AS completed
               FROM attempts a WHERE a.student_id = %s""",
            (user["id"],),
        ).fetchone()
        own_rank = next(
            (row for row in _leaderboard_rows(conn) if row["id"] == user["id"]), None
        )
        recent = conn.execute(
            """SELECT a.id, a.question_id, q.prompt, q.category, q.points,
                      a.earned_points, a.response, a.submitted_at
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.student_id = %s ORDER BY a.submitted_at DESC LIMIT 10""",
            (user["id"],),
        ).fetchall()
        announcements = conn.execute(
            """SELECT a.id, a.title, a.content, a.published_at, u.display_name AS teacher_name
               FROM announcements a JOIN users u ON u.id = a.teacher_id
               ORDER BY a.published_at DESC LIMIT 5"""
        ).fetchall()
    return {
        "score": score,
        "accuracy": accuracy["accuracy"],
        "completed": accuracy["completed"],
        "rank": own_rank["rank"] if own_rank else None,
        "recent": recent,
        "announcements": announcements,
        "leaderboard": _leaderboard_rows_for_api(),
    }


def _leaderboard_rows_for_api():
    with database() as conn:
        return _leaderboard_rows(conn)


@app.get("/api/leaderboard", dependencies=[Depends(_require_ready_user)])
def leaderboard():
    with database() as conn:
        return _leaderboard_rows(conn)


@app.get("/api/questions")
def question_catalog(
    category: str | None = None,
    difficulty: str | None = None,
    user=Depends(_require_student),
):
    with database() as conn:
        rows = conn.execute(
            """SELECT q.* FROM questions q
               WHERE q.is_published = TRUE
             AND (%s::text IS NULL OR q.category = %s)
             AND (%s::text IS NULL OR q.difficulty = %s)
                 AND NOT EXISTS (
                     SELECT 1 FROM attempts a
                     WHERE a.student_id = %s AND a.question_id = q.id
                       AND a.assignment_id IS NULL
                 )
               ORDER BY q.category, q.difficulty, q.id""",
            (category, category, difficulty, difficulty, user["id"]),
        ).fetchall()
    return [_question_view(row) for row in rows]


@app.post("/api/practice/start")
def start_practice(payload: dict[str, Any], user=Depends(_require_student)):
    count = payload.get("count", 10)
    if not isinstance(count, int) or not 1 <= count <= 50:
        raise HTTPException(status_code=400, detail="每次練習題數需為 1 至 50 題。")
    category = payload.get("category")
    difficulty = payload.get("difficulty")
    assignment_id = payload.get("assignment_id")
    with database() as conn:
        with conn.transaction():
            if assignment_id is not None:
                assignment = conn.execute(
                    """SELECT * FROM assignment_sets a
                       JOIN assignment_students s ON s.assignment_id = a.id
                       WHERE a.id = %s AND s.student_id = %s AND a.status = 'published'
                         AND (a.starts_at IS NULL OR a.starts_at <= CURRENT_TIMESTAMP)
                         AND (a.due_at IS NULL OR a.due_at >= CURRENT_TIMESTAMP)""",
                    (assignment_id, user["id"]),
                ).fetchone()
                if not assignment:
                    raise HTTPException(status_code=404, detail="找不到可作答的指派作業。")
                rows = conn.execute(
                    """SELECT q.* FROM questions q
                       JOIN assignment_questions aq ON aq.question_id = q.id
                       WHERE aq.assignment_id = %s AND q.is_published = TRUE
                       ORDER BY q.id""",
                    (assignment_id,),
                ).fetchall()
                existing = conn.execute(
                    "SELECT 1 FROM attempts WHERE student_id = %s AND assignment_id = %s LIMIT 1",
                    (user["id"], assignment_id),
                ).fetchone()
                if existing and not assignment["allow_retake"]:
                    raise HTTPException(status_code=409, detail="此作業已提交，不能重做。")
            else:
                rows = conn.execute(
                    """SELECT q.* FROM questions q
                       WHERE q.is_published = TRUE AND q.question_type <> 'essay'
                         AND (%s::text IS NULL OR q.category = %s)
                         AND (%s::text IS NULL OR q.difficulty = %s)
                         AND NOT EXISTS (
                             SELECT 1 FROM attempts a
                             WHERE a.student_id = %s AND a.question_id = q.id
                               AND a.assignment_id IS NULL
                         )
                       ORDER BY RANDOM() LIMIT %s""",
                    (category, category, difficulty, difficulty, user["id"], count),
                ).fetchall()
                assignment_id = None
            if not rows:
                raise HTTPException(status_code=404, detail="目前沒有符合條件的新題目。")
            questions = rows[:count] if assignment_id is not None else rows
            quiz_id = uuid4()
            conn.execute(
                """INSERT INTO quiz_sessions(id, student_id, question_ids, assignment_id)
                   VALUES (%s, %s, %s, %s)""",
                (quiz_id, user["id"], Jsonb([q["id"] for q in questions]), assignment_id),
            )
    return {
        "quiz_id": str(quiz_id),
        "questions": [_question_view(question) for question in questions],
    }


@app.post("/api/practice/{quiz_id}/submit")
def submit_practice(quiz_id: UUID, payload: dict[str, Any], user=Depends(_require_student)):
    responses = payload.get("responses")
    if not isinstance(responses, dict):
        raise HTTPException(status_code=400, detail="作答資料格式錯誤。")
    with database() as conn:
        with conn.transaction():
            session = conn.execute(
                """SELECT * FROM quiz_sessions
                   WHERE id = %s AND student_id = %s FOR UPDATE""",
                (quiz_id, user["id"]),
            ).fetchone()
            if not session or session["status"] != "in_progress":
                raise HTTPException(status_code=409, detail="測驗已提交或不存在。")
            if session["assignment_id"] is None:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (user["id"],))
            question_ids = [str(value) for value in session["question_ids"]]
            if set(responses) != set(question_ids):
                raise HTTPException(status_code=400, detail="請完成所有題目後再提交。")
            questions = conn.execute(
                "SELECT * FROM questions WHERE id = ANY(%s) ORDER BY id",
                ([int(value) for value in question_ids],),
            ).fetchall()
            question_map = {str(question["id"]): question for question in questions}
            reveal_answers = True
            if session["assignment_id"] is not None:
                assignment = conn.execute(
                    """SELECT show_answers, due_at, starts_at, time_limit_minutes,
                              allow_retake, status
                       FROM assignment_sets WHERE id = %s""",
                    (session["assignment_id"],),
                ).fetchone()
                if not assignment:
                    raise HTTPException(status_code=409, detail="作業不存在，無法提交。")
                now = datetime.now(timezone.utc)
                if assignment["status"] != "published":
                    raise HTTPException(status_code=400, detail="此作業已關閉。")
                if assignment["starts_at"] and assignment["starts_at"] > now:
                    raise HTTPException(status_code=400, detail="作業尚未開始。")
                if assignment["due_at"] and assignment["due_at"] < now:
                    raise HTTPException(status_code=400, detail="作業已超過截止時間。")
                if assignment["time_limit_minutes"] and (
                    now - session["created_at"]
                ) > timedelta(minutes=assignment["time_limit_minutes"]):
                    raise HTTPException(status_code=400, detail="作答時間已結束。")
                if not assignment["allow_retake"] and conn.execute(
                    "SELECT 1 FROM attempts WHERE student_id = %s AND assignment_id = %s LIMIT 1",
                    (user["id"], session["assignment_id"]),
                ).fetchone():
                    raise HTTPException(status_code=409, detail="此作業已提交，不能重做。")
                reveal_answers = assignment["show_answers"]
            score = max_score = correct_count = 0
            result = []
            for question_id in question_ids:
                question = question_map.get(question_id)
                if not question:
                    raise HTTPException(status_code=409, detail="測驗題目已不存在，請聯絡教師。")
                if session["assignment_id"] is None and conn.execute(
                    """SELECT 1 FROM attempts
                       WHERE student_id = %s AND question_id = %s AND assignment_id IS NULL
                       LIMIT 1""",
                    (user["id"], question["id"]),
                ).fetchone():
                    raise HTTPException(status_code=409, detail="此題已作答，無法重複計分。")
                answer = str(responses[question_id]).strip()
                if not answer or len(answer) > 20_000:
                    raise HTTPException(status_code=400, detail="每題都必須作答。")
                points = question["points"]
                max_score += points
                earned = None
                if question["question_type"] in ("choice", "fill_blank"):
                    accepted = (
                        [question["answer"]]
                        if question["question_type"] == "choice"
                        else question["accepted_answers"]
                    )
                    earned = points if answer.casefold() in {
                        str(item).strip().casefold() for item in accepted
                    } else 0
                    score += earned
                    correct_count += int(earned > 0)
                inserted = conn.execute(
                    """INSERT INTO attempts(student_id, question_id, response, earned_points,
                                             quiz_id, assignment_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING RETURNING id""",
                    (user["id"], question["id"], answer, earned, quiz_id, session["assignment_id"]),
                ).fetchone()
                if not inserted:
                    raise HTTPException(status_code=409, detail="題目已提交，不能重複計分。")
                if question["question_type"] == "essay":
                    conn.execute(
                        """INSERT INTO essay_versions(attempt_id, version_number, content)
                           VALUES (%s, 1, %s)""",
                        (inserted["id"], answer),
                    )
                result.append({
                    "question": _question_view(question, reveal=reveal_answers),
                    "response": answer,
                    "earned_points": earned,
                })
            conn.execute(
                """UPDATE quiz_sessions SET status = 'submitted', score = %s,
                   max_score = %s, correct_count = %s, submitted_at = CURRENT_TIMESTAMP
                   WHERE id = %s""",
                (score, max_score, correct_count, quiz_id),
            )
    return {
        "score": score,
        "max_score": max_score,
        "correct_count": correct_count,
        "question_count": len(result),
        "results": result,
    }


@app.get("/api/student/attempts")
def student_attempts(user=Depends(_require_student)):
    with database() as conn:
        rows = conn.execute(
            """SELECT a.id AS attempt_id, a.question_id, a.response, a.earned_points,
                      a.feedback, a.submitted_at, a.assignment_id, q.category, q.difficulty,
                      q.question_type, q.prompt, q.options, q.answer, q.accepted_answers,
                      q.explanation, q.points, s.show_answers
               FROM attempts a JOIN questions q ON q.id = a.question_id
               LEFT JOIN assignment_sets s ON s.id = a.assignment_id
               WHERE a.student_id = %s ORDER BY a.submitted_at DESC LIMIT 200""",
            (user["id"],),
        ).fetchall()
    output = []
    for row in rows:
        reveal = row["assignment_id"] is None or row["show_answers"] is True
        item = dict(row)
        if not reveal:
            item.pop("answer", None)
            item.pop("accepted_answers", None)
            item.pop("explanation", None)
        output.append(item)
    return output


@app.post("/api/student/questions/{question_id}/report")
def report_question(question_id: int, payload: dict[str, Any], user=Depends(_require_student)):
    reason = str(payload.get("reason", "")).strip()
    if not reason or len(reason) > 500:
        raise HTTPException(status_code=400, detail="請填寫題目回報原因。")
    with database() as conn:
        report = conn.execute(
            """INSERT INTO question_reports(question_id, reporter_id, reason)
               SELECT id, %s, %s FROM questions WHERE id = %s
               ON CONFLICT (question_id, reporter_id) DO UPDATE
               SET reason = EXCLUDED.reason, status = 'pending'
               RETURNING id""",
            (user["id"], reason, question_id),
        ).fetchone()
        if not report:
            raise HTTPException(status_code=404, detail="找不到題目。")
    return {"ok": True}


@app.get("/api/student/assignments")
def student_assignments(user=Depends(_require_student)):
    with database() as conn:
        return conn.execute(
            """SELECT a.id, a.title, a.description, a.starts_at, a.due_at,
                      a.time_limit_minutes, a.allow_retake, a.show_answers, a.status,
                      COUNT(DISTINCT q.question_id) AS question_count,
                      COUNT(DISTINCT t.id) AS submitted_count
               FROM assignment_students s JOIN assignment_sets a ON a.id = s.assignment_id
               LEFT JOIN assignment_questions q ON q.assignment_id = a.id
               LEFT JOIN attempts t ON t.assignment_id = a.id AND t.student_id = s.student_id
               WHERE s.student_id = %s GROUP BY a.id ORDER BY a.due_at NULLS LAST""",
            (user["id"],),
        ).fetchall()


@app.get("/api/student/essays")
def student_essays(user=Depends(_require_student)):
    with database() as conn:
        return conn.execute(
            """SELECT a.id AS attempt_id, a.response, a.earned_points, a.feedback,
                      a.revision_requested, a.assignment_id,
                      a.submitted_at, q.id AS question_id, q.prompt, q.points, q.rubric,
                      q.min_word_count,
                      COALESCE(json_agg(json_build_object(
                          'quoted_text', c.quoted_text, 'comment', c.comment
                      ) ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL), '[]') AS comments,
                      COALESCE((SELECT json_agg(json_build_object(
                          'version', v.version_number, 'content', v.content,
                          'status', v.status, 'score', v.score, 'feedback', v.feedback,
                          'submitted_at', v.submitted_at
                      ) ORDER BY v.version_number)
                      FROM essay_versions v WHERE v.attempt_id = a.id), '[]') AS versions
               FROM attempts a JOIN questions q ON q.id = a.question_id
               LEFT JOIN essay_comments c ON c.attempt_id = a.id
               WHERE a.student_id = %s AND q.question_type = 'essay'
               GROUP BY a.id, q.id ORDER BY a.submitted_at DESC""",
            (user["id"],),
        ).fetchall()


@app.get("/api/student/essays/{question_id}/draft")
def get_essay_draft(question_id: int, user=Depends(_require_student)):
    with database() as conn:
        row = conn.execute(
            """SELECT d.content, d.updated_at FROM essay_drafts d
               JOIN questions q ON q.id = d.question_id
               WHERE d.student_id = %s AND d.question_id = %s
                 AND q.question_type = 'essay'""",
            (user["id"], question_id),
        ).fetchone()
    return row or {"content": ""}


@app.put("/api/student/essays/{question_id}/draft")
def save_essay_draft(question_id: int, payload: dict[str, Any], user=Depends(_require_student)):
    content = str(payload.get("content", ""))
    if len(content) > 20_000:
        raise HTTPException(status_code=400, detail="作文最多 20000 個字元。")
    with database() as conn:
        question = conn.execute(
            "SELECT 1 FROM questions WHERE id = %s AND question_type = 'essay'",
            (question_id,),
        ).fetchone()
        if not question:
            raise HTTPException(status_code=404, detail="找不到作文題目。")
        conn.execute(
            """INSERT INTO essay_drafts(student_id, question_id, content)
               VALUES (%s, %s, %s)
               ON CONFLICT(student_id, question_id) DO UPDATE
               SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP""",
            (user["id"], question_id, content),
        )
    return {"ok": True}


@app.post("/api/student/essays/{question_id}/submit")
def submit_essay(question_id: int, payload: dict[str, Any], user=Depends(_require_student)):
    content = str(payload.get("content", "")).strip()
    assignment_id = payload.get("assignment_id")
    if not content or len(content) > 20_000:
        raise HTTPException(status_code=400, detail="作文不可空白且最多 20000 個字元。")
    words = len(re.findall(r"\b[\w'-]+\b", content, flags=re.UNICODE))
    with database() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (user["id"],))
            question = conn.execute(
                "SELECT * FROM questions WHERE id = %s AND question_type = 'essay'",
                (question_id,),
            ).fetchone()
            if not question:
                raise HTTPException(status_code=404, detail="找不到作文題目。")
            if words < question["min_word_count"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"作文至少需要 {question['min_word_count']} 個英文單字，目前 {words} 個。",
                )
            if assignment_id is not None:
                permitted = conn.execute(
                    """SELECT 1 FROM assignment_questions q
                       JOIN assignment_students s ON s.assignment_id = q.assignment_id
                       JOIN assignment_sets a ON a.id = q.assignment_id
                       WHERE q.assignment_id = %s AND q.question_id = %s
                         AND s.student_id = %s AND a.status = 'published'
                         AND (a.due_at IS NULL OR a.due_at >= CURRENT_TIMESTAMP)""",
                    (assignment_id, question_id, user["id"]),
                ).fetchone()
                if not permitted:
                    raise HTTPException(status_code=403, detail="目前無法提交此作文作業。")
            existing = conn.execute(
                """SELECT * FROM attempts WHERE student_id = %s AND question_id = %s
                   AND assignment_id IS NOT DISTINCT FROM %s ORDER BY submitted_at DESC
                   LIMIT 1 FOR UPDATE""",
                (user["id"], question_id, assignment_id),
            ).fetchone()
            if existing and not existing["revision_requested"]:
                raise HTTPException(status_code=409, detail="這篇作文已提交，等待教師批改或退回。")
            if existing:
                attempt_id = existing["id"]
                version = conn.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 AS number FROM essay_versions WHERE attempt_id = %s",
                    (attempt_id,),
                ).fetchone()["number"]
                conn.execute(
                    """UPDATE attempts SET response = %s, earned_points = NULL, feedback = '',
                       revision_requested = FALSE, submitted_at = CURRENT_TIMESTAMP
                       WHERE id = %s""",
                    (content, attempt_id),
                )
            else:
                attempt_id = conn.execute(
                    """INSERT INTO attempts(student_id, question_id, response, assignment_id)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (user["id"], question_id, content, assignment_id),
                ).fetchone()["id"]
                version = 1
            conn.execute(
                """INSERT INTO essay_versions(attempt_id, version_number, content, status)
                   VALUES (%s, %s, %s, 'submitted')""",
                (attempt_id, version, content),
            )
            conn.execute(
                "DELETE FROM essay_drafts WHERE student_id = %s AND question_id = %s",
                (user["id"], question_id),
            )
    return {"attempt_id": attempt_id, "version": version, "word_count": words}


@app.get("/api/student/notes", dependencies=[Depends(_require_ready_user)])
def student_notes():
    with database() as conn:
        return conn.execute(
            """SELECT n.id, n.content, n.created_at, n.author_id,
                      u.display_name AS author_name,
                      COALESCE(json_agg(json_build_object(
                          'id', r.id, 'content', r.content, 'author_id', r.author_id,
                          'author_name', ru.display_name, 'created_at', r.created_at
                      ) ORDER BY r.created_at) FILTER (WHERE r.id IS NOT NULL), '[]') AS replies
               FROM notes n JOIN users u ON u.id = n.author_id
               LEFT JOIN note_replies r ON r.note_id = n.id
               LEFT JOIN users ru ON ru.id = r.author_id
               GROUP BY n.id, u.display_name ORDER BY n.created_at DESC LIMIT 100"""
        ).fetchall()


@app.post("/api/student/notes")
def create_note(payload: dict[str, Any], user=Depends(_require_ready_user)):
    content = str(payload.get("content", "")).strip()
    if not 1 <= len(content) <= 2000:
        raise HTTPException(status_code=400, detail="便利貼內容需為 1 至 2000 字。")
    with database() as conn:
        return conn.execute(
            "INSERT INTO notes(author_id, content) VALUES (%s, %s) RETURNING id, content, created_at",
            (user["id"], content),
        ).fetchone()


@app.patch("/api/student/notes/{note_id}")
def update_own_note(
    note_id: int, payload: dict[str, Any], user=Depends(_require_ready_user)
):
    content = str(payload.get("content", "")).strip()
    if not 1 <= len(content) <= 2000:
        raise HTTPException(status_code=400, detail="便利貼內容需為 1 至 2000 字。")
    with database() as conn:
        updated = conn.execute(
            """UPDATE notes SET content = %s, updated_at = CURRENT_TIMESTAMP
               WHERE id = %s AND author_id = %s RETURNING id""",
            (content, note_id, user["id"]),
        ).fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="找不到你可以編輯的便利貼。")
    return {"ok": True}


@app.delete("/api/student/notes/{note_id}")
def delete_own_note(note_id: int, user=Depends(_require_ready_user)):
    with database() as conn:
        deleted = conn.execute(
            "DELETE FROM notes WHERE id = %s AND author_id = %s RETURNING id",
            (note_id, user["id"]),
        ).fetchone()
    if not deleted:
        raise HTTPException(status_code=404, detail="找不到你可以刪除的便利貼。")
    return {"ok": True}


@app.post("/api/student/notes/{note_id}/replies")
def create_note_reply(note_id: int, payload: dict[str, Any], user=Depends(_require_ready_user)):
    content = str(payload.get("content", "")).strip()
    if not 1 <= len(content) <= 1000:
        raise HTTPException(status_code=400, detail="回覆需為 1 至 1000 字。")
    with database() as conn:
        if not conn.execute("SELECT 1 FROM notes WHERE id = %s", (note_id,)).fetchone():
            raise HTTPException(status_code=404, detail="找不到便利貼。")
        return conn.execute(
            """INSERT INTO note_replies(note_id, author_id, content)
               VALUES (%s, %s, %s) RETURNING id, content, created_at""",
            (note_id, user["id"], content),
        ).fetchone()


@app.post("/api/student/notes/{note_id}/report")
def report_note(note_id: int, payload: dict[str, Any], user=Depends(_require_ready_user)):
    reason = str(payload.get("reason", "")).strip()
    if not reason or len(reason) > 500:
        raise HTTPException(status_code=400, detail="請填寫檢舉原因。")
    with database() as conn:
        inserted = conn.execute(
            """INSERT INTO note_reports(note_id, reporter_id, reason)
               SELECT id, %s, %s FROM notes WHERE id = %s RETURNING id""",
            (user["id"], reason, note_id),
        ).fetchone()
        if not inserted:
            raise HTTPException(status_code=404, detail="找不到便利貼。")
    return {"ok": True}


@app.get("/api/teacher/students", dependencies=[Depends(_require_teacher)])
def teacher_students(search: str = ""):
    with database() as conn:
        rows = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.email, u.is_active, u.created_at,
                      ROUND(100.0 * COUNT(a.id) FILTER (
                          WHERE q.question_type IN ('choice', 'fill_blank')
                            AND a.earned_points = q.points)
                          / NULLIF(COUNT(a.id) FILTER (
                              WHERE q.question_type IN ('choice', 'fill_blank')
                                AND a.earned_points IS NOT NULL), 0)) AS accuracy
               FROM users u LEFT JOIN attempts a ON a.student_id = u.id
               LEFT JOIN questions q ON q.id = a.question_id
               WHERE u.role = 'student'
                 AND (%s = '' OR u.username ILIKE %s OR u.display_name ILIKE %s
                      OR u.email ILIKE %s)
               GROUP BY u.id ORDER BY u.username""",
            (search, f"%{search}%", f"%{search}%", f"%{search}%"),
        ).fetchall()
        ranks = {row["id"]: row["rank"] for row in _leaderboard_rows(conn)}
        students = []
        for row in rows:
            student = dict(row)
            student["score"] = _student_score(conn, student["id"])
            student["rank"] = ranks.get(student["id"])
            students.append(student)
        return students


@app.get("/api/teacher/students/{student_id}")
def teacher_student_detail(student_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        student = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.email, u.is_active,
                      u.created_at,
                      COALESCE(ARRAY_AGG(c.name) FILTER (WHERE c.name IS NOT NULL), '{}')
                          AS classes
               FROM users u
               LEFT JOIN class_memberships m ON m.student_id = u.id
               LEFT JOIN classes c ON c.id = m.class_id
               WHERE u.id = %s AND u.role = 'student'
               GROUP BY u.id""",
            (student_id,),
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="找不到學生帳號。")
        accuracy = conn.execute(
            """SELECT COUNT(*) AS answered,
                      COUNT(*) FILTER (WHERE a.earned_points = q.points) AS correct
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.student_id = %s
                 AND q.question_type IN ('choice', 'fill_blank')
                 AND a.earned_points IS NOT NULL""",
            (student_id,),
        ).fetchone()
        attempts = conn.execute(
            """SELECT a.id, q.prompt, q.category, q.question_type, q.points,
                      a.response, a.earned_points, a.feedback, a.submitted_at
               FROM attempts a JOIN questions q ON q.id = a.question_id
               WHERE a.student_id = %s ORDER BY a.submitted_at DESC LIMIT 100""",
            (student_id,),
        ).fetchall()
        login_events = conn.execute(
            """SELECT succeeded, created_at FROM login_events
               WHERE user_id = %s ORDER BY created_at DESC LIMIT 20""",
            (student_id,),
        ).fetchall()
        rankings = _leaderboard_rows(conn)
        rank = next((row["rank"] for row in rankings if row["id"] == student_id), None)
        score = _student_score(conn, student_id)
    return {
        **dict(student),
        "score": score,
        "rank": rank,
        "accuracy": (
            accuracy["correct"] * 100 / accuracy["answered"]
            if accuracy["answered"] else None
        ),
        "attempts": attempts,
        "login_history": login_events,
    }


@app.post("/api/teacher/students")
def create_student(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    email = str(payload.get("email", "")).strip().casefold()
    display_name = str(payload.get("display_name") or username).strip()
    if not 3 <= len(username) <= 32 or re.search(r"\s", username):
        raise HTTPException(status_code=400, detail="帳號需為 3 至 32 字元且不可有空白。")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密碼至少需要 8 個字元。")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="電子郵件格式不正確。")
    try:
        with database() as conn:
            with conn.transaction():
                student = conn.execute(
                    """INSERT INTO users(username, display_name, password_hash, role,
                       email, must_change_password)
                       VALUES (%s, %s, %s, 'student', %s, TRUE) RETURNING id""",
                    (username, display_name, _hash_password(password), email),
                ).fetchone()
                _audit(conn, teacher["id"], "create_student", "user", student["id"])
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="帳號或電子郵件已被使用。") from exc
    return {"id": student["id"]}


@app.patch("/api/teacher/students/{student_id}")
def update_student(
    student_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    display_name = str(payload.get("display_name", "")).strip()
    email = str(payload.get("email", "")).strip().casefold()
    if not 1 <= len(display_name) <= 40:
        raise HTTPException(status_code=400, detail="暱稱需為 1 至 40 個字元。")
    if email and (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        raise HTTPException(status_code=400, detail="電子郵件格式不正確。")
    try:
        with database() as conn:
            with conn.transaction():
                updated = conn.execute(
                    """UPDATE users SET display_name = %s, email = %s
                       WHERE id = %s AND role = 'student' RETURNING id""",
                    (display_name, email, student_id),
                ).fetchone()
                if not updated:
                    raise HTTPException(status_code=404, detail="找不到學生帳號。")
                _audit(
                    conn, teacher["id"], "update_student", "user", student_id,
                    {"display_name": display_name, "email": email},
                )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="此電子郵件已被其他學生使用。") from exc
    return {"ok": True}


@app.post("/api/teacher/students/{student_id}/reset-password")
def reset_student_password(student_id: int, teacher=Depends(_require_teacher)):
    temporary_password = secrets.token_urlsafe(12)
    with database() as conn:
        with conn.transaction():
            updated = conn.execute(
                """UPDATE users SET password_hash = %s, must_change_password = TRUE
                   WHERE id = %s AND role = 'student' RETURNING id""",
                (_hash_password(temporary_password), student_id),
            ).fetchone()
            if not updated:
                raise HTTPException(status_code=404, detail="找不到學生帳號。")
            _audit(conn, teacher["id"], "reset_student_password", "user", student_id)
    return {"temporary_password": temporary_password}


@app.post("/api/teacher/students/{student_id}/status")
def set_student_status(
    student_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    active = bool(payload.get("active"))
    with database() as conn:
        with conn.transaction():
            updated = conn.execute(
                """UPDATE users SET is_active = %s WHERE id = %s AND role = 'student'
                   RETURNING id""",
                (active, student_id),
            ).fetchone()
            if not updated:
                raise HTTPException(status_code=404, detail="找不到學生帳號。")
            _audit(conn, teacher["id"], "set_student_status", "user", student_id, {"active": active})
    return {"ok": True}


@app.get("/api/teacher/classes")
def teacher_classes(teacher=Depends(_require_teacher)):
    with database() as conn:
        return conn.execute(
            """SELECT c.id, c.name, COUNT(m.student_id) AS student_count,
                      COALESCE(ARRAY_AGG(m.student_id) FILTER (WHERE m.student_id IS NOT NULL), '{}')
                          AS student_ids
               FROM classes c LEFT JOIN class_memberships m ON m.class_id = c.id
               WHERE c.teacher_id = %s GROUP BY c.id ORDER BY c.name""",
            (teacher["id"],),
        ).fetchall()


@app.post("/api/teacher/classes")
def create_class(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    name = str(payload.get("name", "")).strip()
    ids = payload.get("student_ids", [])
    if not 1 <= len(name) <= 80 or not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="班級名稱或學生清單無效。")
    try:
        with database() as conn:
            with conn.transaction():
                class_row = conn.execute(
                    "INSERT INTO classes(teacher_id, name) VALUES (%s, %s) RETURNING id, name",
                    (teacher["id"], name),
                ).fetchone()
                for student_id in set(int(value) for value in ids):
                    valid = conn.execute(
                        "SELECT 1 FROM users WHERE id = %s AND role = 'student' AND is_active",
                        (student_id,),
                    ).fetchone()
                    if not valid:
                        raise HTTPException(status_code=400, detail="學生清單包含無效帳號。")
                    conn.execute(
                        "INSERT INTO class_memberships(class_id, student_id) VALUES (%s, %s)",
                        (class_row["id"], student_id),
                    )
                _audit(conn, teacher["id"], "create_class", "class", class_row["id"])
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="同名班級已存在。") from exc
    return class_row


@app.patch("/api/teacher/classes/{class_id}")
def update_class(class_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)):
    name = str(payload.get("name", "")).strip()
    student_ids = payload.get("student_ids", [])
    if not 1 <= len(name) <= 80 or not isinstance(student_ids, list):
        raise HTTPException(status_code=400, detail="班級名稱或學生清單無效。")
    try:
        ids = {int(value) for value in student_ids}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="學生清單包含無效帳號。") from exc
    try:
        with database() as conn:
            with conn.transaction():
                owned = conn.execute(
                    "UPDATE classes SET name = %s WHERE id = %s AND teacher_id = %s RETURNING id",
                    (name, class_id, teacher["id"]),
                ).fetchone()
                if not owned:
                    raise HTTPException(status_code=404, detail="找不到可管理的班級。")
                if ids:
                    valid_count = conn.execute(
                        """SELECT COUNT(*) AS count FROM users
                           WHERE id = ANY(%s) AND role = 'student' AND is_active""",
                        (list(ids),),
                    ).fetchone()["count"]
                    if valid_count != len(ids):
                        raise HTTPException(status_code=400, detail="學生清單包含無效或停用帳號。")
                conn.execute("DELETE FROM class_memberships WHERE class_id = %s", (class_id,))
                conn.executemany(
                    "INSERT INTO class_memberships(class_id, student_id) VALUES (%s, %s)",
                    [(class_id, student_id) for student_id in ids],
                )
                _audit(
                    conn, teacher["id"], "update_class", "class", class_id,
                    {"name": name, "student_ids": sorted(ids)},
                )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="同名班級已存在。") from exc
    return {"ok": True}


@app.post("/api/teacher/banks")
def create_question_bank(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not 1 <= len(name) <= 100 or len(description) > 2000:
        raise HTTPException(status_code=400, detail="題庫名稱需為 1 至 100 個字元，說明不可超過 2000 個字元。")
    try:
        with database() as conn:
            with conn.transaction():
                bank = conn.execute(
                    """INSERT INTO question_banks(name, description, created_by)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (name, description, teacher["id"]),
                ).fetchone()
                _audit(conn, teacher["id"], "create_question_bank", "question_bank", bank["id"])
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="同名題庫已存在。") from exc
    return {"id": bank["id"]}


@app.patch("/api/teacher/banks/{bank_id}")
def update_question_bank(bank_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)):
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not 1 <= len(name) <= 100 or len(description) > 2000:
        raise HTTPException(status_code=400, detail="題庫名稱需為 1 至 100 個字元，說明不可超過 2000 個字元。")
    try:
        with database() as conn:
            with conn.transaction():
                updated = conn.execute(
                    """UPDATE question_banks SET name = %s, description = %s,
                              updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s AND created_by = %s RETURNING id""",
                    (name, description, bank_id, teacher["id"]),
                ).fetchone()
                if not updated:
                    raise HTTPException(status_code=404, detail="找不到可管理的題庫。")
                _audit(conn, teacher["id"], "update_question_bank", "question_bank", bank_id)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="同名題庫已存在。") from exc
    return {"ok": True}


@app.delete("/api/teacher/banks/{bank_id}")
def delete_question_bank(bank_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        with conn.transaction():
            bank = conn.execute(
                "SELECT id FROM question_banks WHERE id = %s AND created_by = %s",
                (bank_id, teacher["id"]),
            ).fetchone()
            if not bank:
                raise HTTPException(status_code=404, detail="找不到可管理的題庫。")
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM questions WHERE bank_id = %s",
                (bank_id,),
            ).fetchone()["count"]
            if count:
                raise HTTPException(status_code=409, detail="請先移動題庫中的題目，再刪除題庫。")
            conn.execute("DELETE FROM question_banks WHERE id = %s", (bank_id,))
            _audit(conn, teacher["id"], "delete_question_bank", "question_bank", bank_id)
    return {"ok": True}


@app.get("/api/teacher/questions", dependencies=[Depends(_require_teacher)])
def teacher_questions(search: str = "", bank_id: int | None = None):
    with database() as conn:
        rows = conn.execute(
            """SELECT * FROM questions
               WHERE (%s = '' OR prompt ILIKE %s OR category ILIKE %s OR scope ILIKE %s)
                 AND (%s IS NULL OR bank_id = %s)
               ORDER BY created_at DESC LIMIT 500""",
            (search, f"%{search}%", f"%{search}%", f"%{search}%", bank_id, bank_id),
        ).fetchall()
    return [_question_view(row, reveal=True) for row in rows]


@app.post("/api/teacher/pdf-text", dependencies=[Depends(_require_teacher)])
def extract_teacher_pdf(payload: dict[str, Any]):
    encoded = str(payload.get("content_base64", ""))
    if not encoded or len(encoded) > 4_200_000:
        raise HTTPException(status_code=413, detail="PDF 檔案上限為 3 MB。")
    try:
        content = b64decode(encoded, validate=True)
    except Base64Error as exc:
        raise HTTPException(status_code=400, detail="PDF 上傳資料格式無效。") from exc
    if len(content) > 3_000_000:
        raise HTTPException(status_code=413, detail="PDF 檔案上限為 3 MB。")
    try:
        import pymupdf
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="PDF 套件尚未安裝。") from exc
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
        raise HTTPException(status_code=400, detail="無法讀取 PDF，請確認檔案未損毀或加密。") from exc
    try:
        if document.needs_pass:
            raise HTTPException(status_code=400, detail="目前不支援有密碼保護的 PDF。")
        if not 1 <= document.page_count <= 20:
            raise HTTPException(status_code=400, detail="PDF 頁數需介於 1 至 20 頁。")
        pages = [
            f"--- 第 {index} 頁 ---\n{page.get_text('text').strip()}"
            for index, page in enumerate(document, start=1)
        ]
    finally:
        document.close()
    extracted = "\n\n".join(pages).strip()
    if not extracted:
        raise HTTPException(
            status_code=422,
            detail="PDF 中沒有可抽取的文字。掃描影像需先使用 OCR 軟體辨識，再貼入題目編輯器。",
        )
    return {"text": extracted[:100_000], "characters": len(extracted)}


def _validate_question(raw: dict[str, Any]) -> dict[str, Any]:
    question_type = raw.get("question_type", raw.get("type", "choice"))
    question_type = {
        "single_choice": "choice",
        "multiple_choice": "choice",
        "fill_blank": "fill_blank",
    }.get(question_type, question_type)
    category = str(raw.get("category", "General")).strip()
    difficulty = str(raw.get("difficulty", raw.get("level", "中級"))).strip()
    if difficulty in {"easy", "medium", "hard"}:
        difficulty = {"easy": "初級", "medium": "中級", "hard": "高級"}[difficulty]
    elif difficulty.casefold() == "intermediate":
        difficulty = "中高級"
    elif difficulty.casefold() == "beginner":
        difficulty = "初級"
    elif difficulty.casefold() == "advanced":
        difficulty = "高級"
    prompt = str(raw.get("prompt", raw.get("question", ""))).strip()
    options = raw.get("options", [])
    if isinstance(options, str):
        options = [value.strip() for value in options.split("|") if value.strip()]
    if not isinstance(options, list):
        options = []
    accepted = raw.get("accepted_answers", [])
    if isinstance(accepted, str):
        accepted = [value.strip() for value in accepted.split("|") if value.strip()]
    if not isinstance(accepted, list):
        accepted = []
    answer = str(raw.get("answer", "")).strip()
    if isinstance(raw.get("answer"), list):
        answer = str(raw["answer"][0]).strip() if raw["answer"] else ""
    points = raw.get("points", 5)
    if (
        not category
        or difficulty not in DIFFICULTIES
        or question_type not in {"choice", "fill_blank", "essay"}
        or not prompt
        or len(prompt) > 20_000
        or not isinstance(points, int)
        or not 1 <= points <= 1000
    ):
        raise HTTPException(status_code=400, detail="題目欄位、難度或配分格式錯誤。")
    if question_type == "choice" and (len(options) < 2 or answer not in options):
        raise HTTPException(status_code=400, detail="選擇題需至少有兩個選項且正確答案須在選項中。")
    if question_type == "fill_blank" and not (accepted or answer):
        raise HTTPException(status_code=400, detail="填空題需設定 accepted_answers 或 answer。")
    try:
        min_word_count = int(raw.get("min_word_count", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="作文最低字數格式錯誤。") from exc
    bank_id = raw.get("bank_id") or None
    try:
        bank_id = int(bank_id) if bank_id is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="題庫格式無效。") from exc
    if bank_id is not None and bank_id <= 0:
        raise HTTPException(status_code=400, detail="題庫格式無效。")
    if not 0 <= min_word_count <= 100_000:
        raise HTTPException(status_code=400, detail="作文最低字數超出範圍。")
    return {
        "bank_id": bank_id,
        "category": category,
        "scope": str(raw.get("scope", "")),
        "grade_level": str(raw.get("grade_level", "")),
        "difficulty": difficulty,
        "question_type": question_type,
        "prompt": prompt,
        "options": options if question_type == "choice" else [],
        "answer": answer,
        "accepted_answers": accepted or ([answer] if answer else []),
        "explanation": str(raw.get("explanation", "")),
        "rubric": str(raw.get("rubric", "")),
        "min_word_count": min_word_count,
        "points": points,
        "is_published": bool(raw.get("is_published", True)),
    }


def _parse_question_import_file(filename: str, content: bytes) -> Any:
    suffix = filename.lower().rsplit(".", maxsplit=1)[-1] if "." in filename else ""
    if suffix == "json":
        try:
            return json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="JSON 檔案格式或編碼無效。") from exc
    if suffix == "csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="CSV 必須使用 UTF-8 編碼。") from exc
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {str(key or "").strip(): value for key, value in row.items() if key}
            for row in reader
        ]
        return {"questions": _normalize_tabular_questions(rows)}
    if suffix != "xlsx":
        raise HTTPException(status_code=400, detail="僅支援 JSON、CSV 或 XLSX 檔案。")

    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if sum(item.file_size for item in archive.infolist()) > 10_000_000:
                raise HTTPException(status_code=413, detail="XLSX 解壓後內容超過安全上限。")
            if "xl/worksheets/sheet1.xml" not in names:
                raise HTTPException(status_code=400, detail="Excel 活頁簿找不到第一張工作表。")
            shared_strings = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared_strings = [
                    "".join(node.text or "" for node in item.findall(".//main:t", namespace))
                    for item in root.findall("main:si", namespace)
                ]
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except HTTPException:
        raise
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="XLSX 活頁簿格式無效。") from exc

    rows = []
    for row in sheet.findall(".//main:sheetData/main:row", namespace):
        values = []
        for cell in row.findall("main:c", namespace):
            column = "".join(char for char in cell.attrib.get("r", "") if char.isalpha())
            column_index = 0
            for char in column:
                column_index = column_index * 26 + ord(char.upper()) - ord("A") + 1
            if column_index == 0:
                continue
            while len(values) < column_index:
                values.append("")
            value_node = cell.find("main:v", namespace)
            value = value_node.text if value_node is not None else ""
            if cell.attrib.get("t") == "s" and value:
                try:
                    value = shared_strings[int(value)]
                except (ValueError, IndexError) as exc:
                    raise HTTPException(status_code=400, detail="XLSX 共用字串索引無效。") from exc
            elif cell.attrib.get("t") == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.findall(".//main:t", namespace)
                )
            values[column_index - 1] = value
        rows.append(values)
    if not rows:
        raise HTTPException(status_code=400, detail="Excel 工作表沒有內容。")
    headings = [str(value).strip() for value in rows[0]]
    records = [
        dict(zip(headings, row + [""] * max(0, len(headings) - len(row))))
        for row in rows[1:]
        if any(str(value).strip() for value in row)
    ]
    return {"questions": _normalize_tabular_questions(records)}


def _normalize_tabular_questions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = []
    for row in rows:
        question = dict(row)
        question["options"] = [
            value.strip()
            for value in str(row.get("options", "")).split("|")
            if value.strip()
        ]
        question["accepted_answers"] = [
            value.strip()
            for value in str(row.get("accepted_answers", "")).split("|")
            if value.strip()
        ] or ([str(row.get("answer", "")).strip()] if row.get("answer") else [])
        for field, default in (("points", 5), ("min_word_count", 0)):
            value = str(row.get(field, "")).strip()
            question[field] = int(value) if value.isdigit() else default if not value else value
        questions.append(question)
    return questions


def _prepare_question_import(payload: Any, selected_bank_id=None):
    data = payload.get("questions", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list) or not 1 <= len(data) <= 500:
        raise HTTPException(status_code=400, detail="匯入內容需包含 1 至 500 題。")
    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="匯入內容無法解析。") from exc
    if len(encoded) > 2_000_000:
        raise HTTPException(status_code=413, detail="題目匯入內容上限為 2 MB。")

    inherited_category = payload.get("category") if isinstance(payload, dict) else None
    inherited_level = payload.get("level") if isinstance(payload, dict) else None
    validated = []
    errors = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 題：題目資料必須是物件。")
            continue
        normalized = dict(item)
        normalized.setdefault("category", inherited_category or "General")
        if not normalized.get("difficulty"):
            normalized["difficulty"] = inherited_level or "中級"
        if selected_bank_id is not None:
            normalized["bank_id"] = selected_bank_id
        try:
            validated.append(_validate_question(normalized))
        except HTTPException as exc:
            errors.append(f"第 {index} 題：{exc.detail}")
    if errors:
        raise HTTPException(status_code=400, detail={"message": "題目驗證失敗。", "errors": errors})
    title = str(payload.get("title", "")).strip() if isinstance(payload, dict) else ""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return validated, title, hashlib.sha256(canonical).hexdigest()


def _insert_question(conn, teacher_id, question):
    if question["bank_id"] is not None:
        bank = conn.execute(
            "SELECT 1 FROM question_banks WHERE id = %s AND created_by = %s",
            (question["bank_id"], teacher_id),
        ).fetchone()
        if not bank:
            raise HTTPException(status_code=403, detail="只能將題目存入自己建立的題庫。")
    return conn.execute(
        """INSERT INTO questions
           (bank_id, category, scope, grade_level, difficulty, question_type, prompt,
            options, answer, accepted_answers, explanation, rubric, min_word_count,
            points, is_published, created_by)
           VALUES (%(bank_id)s, %(category)s, %(scope)s, %(grade_level)s, %(difficulty)s,
            %(question_type)s, %(prompt)s, %(options)s, %(answer)s, %(accepted_answers)s,
            %(explanation)s, %(rubric)s, %(min_word_count)s, %(points)s, %(is_published)s,
            %(created_by)s) RETURNING id""",
        {**question, "options": Jsonb(question["options"]),
         "accepted_answers": Jsonb(question["accepted_answers"]),
         "created_by": teacher_id},
    ).fetchone()["id"]


@app.post("/api/teacher/questions")
def add_teacher_question(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    question = _validate_question(payload)
    with database() as conn:
        with conn.transaction():
            question_id = _insert_question(conn, teacher["id"], question)
            _audit(conn, teacher["id"], "create_question", "question", question_id)
    return {"id": question_id}


@app.post("/api/teacher/questions/import")
def import_questions(payload: Any, teacher=Depends(_require_teacher)):
    selected_bank_id = payload.get("bank_id") if isinstance(payload, dict) else None
    validated, title, content_hash = _prepare_question_import(
        payload, selected_bank_id
    )
    with database() as conn:
        with conn.transaction():
            duplicate = conn.execute(
                "SELECT 1 FROM import_batches WHERE content_hash = %s", (content_hash,)
            ).fetchone()
            if duplicate:
                raise HTTPException(status_code=409, detail="此份考卷已匯入過，避免重複建立。")
            bank_id = selected_bank_id
            if not bank_id and title:
                bank = conn.execute(
                    """INSERT INTO question_banks(name, created_by)
                       VALUES (%s, %s) ON CONFLICT(name) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                       RETURNING id, created_by""",
                    (title[:120], teacher["id"]),
                ).fetchone()
                if bank["created_by"] != teacher["id"]:
                    raise HTTPException(status_code=409, detail="同名題庫已由其他教師建立，請選擇自己的題庫。")
                bank_id = bank["id"]
            ids = []
            for question in validated:
                question["bank_id"] = bank_id or question["bank_id"]
                ids.append(_insert_question(conn, teacher["id"], question))
            conn.execute(
                """INSERT INTO import_batches(teacher_id, content_hash, imported_count)
                   VALUES (%s, %s, %s)""",
                (teacher["id"], content_hash, len(ids)),
            )
            _audit(conn, teacher["id"], "import_questions", "question_bank", bank_id or "", {"count": len(ids)})
    return {"imported": len(ids), "question_ids": ids, "bank_id": bank_id}


@app.post("/api/teacher/questions/import-preview")
def preview_question_import(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    filename = str(payload.get("filename", ""))
    encoded = str(payload.get("content_base64", ""))
    if not filename or not encoded or len(encoded) > 2_800_000:
        raise HTTPException(status_code=413, detail="匯入檔案不可超過 2 MB。")
    try:
        content = b64decode(encoded, validate=True)
    except Base64Error as exc:
        raise HTTPException(status_code=400, detail="檔案上傳資料格式無效。") from exc
    if len(content) > 2_000_000:
        raise HTTPException(status_code=413, detail="匯入檔案不可超過 2 MB。")
    parsed = _parse_question_import_file(filename, content)
    selected_bank_id = payload.get("bank_id") or None
    validated, title, _ = _prepare_question_import(parsed, selected_bank_id)
    return {
        "payload": {
            **(parsed if isinstance(parsed, dict) else {}),
            "title": title,
            "bank_id": selected_bank_id,
            "questions": validated,
        },
        "questions": [
            {
                "category": question["category"],
                "scope": question["scope"],
                "grade_level": question["grade_level"],
                "difficulty": question["difficulty"],
                "type": question["question_type"],
                "prompt": question["prompt"],
                "points": question["points"],
            }
            for question in validated
        ],
    }


@app.patch("/api/teacher/questions/{question_id}")
def update_teacher_question(
    question_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    question = _validate_question(payload)
    with database() as conn:
        with conn.transaction():
            owned = conn.execute(
                "SELECT id FROM questions WHERE id = %s AND (created_by = %s OR created_by IS NULL)",
                (question_id, teacher["id"]),
            ).fetchone()
            if not owned:
                raise HTTPException(status_code=404, detail="找不到可管理的題目。")
            if question["bank_id"] is not None:
                _insert_question_bank_check(conn, question["bank_id"], teacher["id"])
            conn.execute(
                """UPDATE questions SET bank_id = %s, category = %s, scope = %s,
                   grade_level = %s, difficulty = %s, question_type = %s, prompt = %s,
                   options = %s, answer = %s, accepted_answers = %s, explanation = %s,
                   rubric = %s, min_word_count = %s, points = %s, is_published = %s
                   WHERE id = %s""",
                (question["bank_id"], question["category"], question["scope"],
                 question["grade_level"], question["difficulty"], question["question_type"],
                 question["prompt"], Jsonb(question["options"]), question["answer"],
                 Jsonb(question["accepted_answers"]), question["explanation"],
                 question["rubric"], question["min_word_count"], question["points"],
                 question["is_published"], question_id),
            )
            _audit(conn, teacher["id"], "update_question", "question", question_id)
    return {"ok": True}


def _insert_question_bank_check(conn, bank_id, teacher_id):
    if not conn.execute(
        "SELECT 1 FROM question_banks WHERE id = %s AND created_by = %s",
        (bank_id, teacher_id),
    ).fetchone():
        raise HTTPException(status_code=403, detail="只能管理自己建立的題庫。")


@app.delete("/api/teacher/questions/{question_id}")
def delete_teacher_question(question_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        with conn.transaction():
            question = conn.execute(
                "SELECT prompt FROM questions WHERE id = %s AND (created_by = %s OR created_by IS NULL)",
                (question_id, teacher["id"]),
            ).fetchone()
            if not question:
                raise HTTPException(status_code=404, detail="找不到可管理的題目。")
            used = conn.execute(
                "SELECT 1 FROM attempts WHERE question_id = %s LIMIT 1", (question_id,)
            ).fetchone()
            if used:
                conn.execute(
                    "UPDATE questions SET is_published = FALSE WHERE id = %s", (question_id,)
                )
                action = "archive_question"
            else:
                conn.execute("DELETE FROM questions WHERE id = %s", (question_id,))
                action = "delete_question"
            _audit(conn, teacher["id"], action, "question", question_id)
    return {"ok": True}


@app.get("/api/teacher/banks")
def teacher_banks(teacher=Depends(_require_teacher)):
    with database() as conn:
        return conn.execute(
            """SELECT b.id, b.name, b.description, COUNT(q.id) AS question_count
               FROM question_banks b LEFT JOIN questions q ON q.bank_id = b.id
               WHERE b.created_by = %s GROUP BY b.id ORDER BY b.name""",
            (teacher["id"],),
        ).fetchall()



@app.post("/api/teacher/assignments")
def create_assignment(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    title = str(payload.get("title", "")).strip()
    question_ids = payload.get("question_ids", [])
    student_ids = payload.get("student_ids", [])
    if (
        not 1 <= len(title) <= 160
        or not isinstance(question_ids, list)
        or not question_ids
        or not isinstance(student_ids, list)
        or not student_ids
    ):
        raise HTTPException(status_code=400, detail="作業名稱、題目與學生皆為必填。")
    status = str(payload.get("status", "published"))
    if status not in {"draft", "published", "closed"}:
        raise HTTPException(status_code=400, detail="作業狀態無效。")
    try:
        start_value = datetime.fromisoformat(payload["starts_at"]) if payload.get("starts_at") else None
        due_value = datetime.fromisoformat(payload["due_at"]) if payload.get("due_at") else None
        time_limit = int(payload.get("time_limit_minutes", 0))
        question_ids = {int(value) for value in question_ids}
        student_ids = {int(value) for value in student_ids}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="作業時間、題目或學生清單格式無效。") from exc
    if start_value and due_value and start_value >= due_value:
        raise HTTPException(status_code=400, detail="截止時間必須晚於開始時間。")
    if not 0 <= time_limit <= 600:
        raise HTTPException(status_code=400, detail="作答時間限制需介於 0 至 600 分鐘。")
    with database() as conn:
        with conn.transaction():
            valid_questions = conn.execute(
                """SELECT COUNT(*) AS count FROM questions
                   WHERE id = ANY(%s) AND (created_by = %s OR created_by IS NULL)""",
                (list(question_ids), teacher["id"]),
            ).fetchone()["count"]
            if valid_questions != len(question_ids):
                raise HTTPException(status_code=400, detail="題目清單包含無效或無權管理的題目。")
            assignment = conn.execute(
                """INSERT INTO assignment_sets
                   (teacher_id, title, description, starts_at, due_at, time_limit_minutes,
                    allow_retake, show_answers, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (teacher["id"], title, str(payload.get("description", ""))[:2000],
                 payload.get("starts_at"), payload.get("due_at"),
                 time_limit,
                 bool(payload.get("allow_retake", False)),
                 bool(payload.get("show_answers", True)), status),
            ).fetchone()
            for question_id in question_ids:
                conn.execute(
                    "INSERT INTO assignment_questions(assignment_id, question_id) VALUES (%s, %s)",
                    (assignment["id"], question_id),
                )
            for student_id in student_ids:
                valid = conn.execute(
                    "SELECT 1 FROM users WHERE id = %s AND role = 'student' AND is_active",
                    (student_id,),
                ).fetchone()
                if not valid:
                    raise HTTPException(status_code=400, detail="指派學生清單無效。")
                conn.execute(
                    "INSERT INTO assignment_students(assignment_id, student_id) VALUES (%s, %s)",
                    (assignment["id"], student_id),
                )
            _audit(conn, teacher["id"], "create_assignment", "assignment", assignment["id"])
    return {"id": assignment["id"]}


@app.patch("/api/teacher/assignments/{assignment_id}/schedule")
def update_assignment_schedule(
    assignment_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    starts_at = payload.get("starts_at") or None
    due_at = payload.get("due_at") or None
    try:
        start_value = datetime.fromisoformat(starts_at) if starts_at else None
        due_value = datetime.fromisoformat(due_at) if due_at else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="作業開始或截止時間格式無效。") from exc
    if start_value and due_value and start_value >= due_value:
        raise HTTPException(status_code=400, detail="截止時間必須晚於開始時間。")
    with database() as conn:
        with conn.transaction():
            updated = conn.execute(
                """UPDATE assignment_sets SET starts_at = %s, due_at = %s
                   WHERE id = %s AND teacher_id = %s RETURNING id""",
                (starts_at, due_at, assignment_id, teacher["id"]),
            ).fetchone()
            if not updated:
                raise HTTPException(status_code=404, detail="找不到可管理的作業。")
            _audit(
                conn, teacher["id"], "update_assignment_schedule", "assignment",
                assignment_id, {"starts_at": starts_at, "due_at": due_at},
            )
    return {"ok": True}


@app.patch("/api/teacher/assignments/{assignment_id}/status")
def update_assignment_status(
    assignment_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    status = str(payload.get("status", ""))
    if status not in {"draft", "published", "closed"}:
        raise HTTPException(status_code=400, detail="作業狀態無效。")
    with database() as conn:
        with conn.transaction():
            updated = conn.execute(
                """UPDATE assignment_sets SET status = %s
                   WHERE id = %s AND teacher_id = %s RETURNING id""",
                (status, assignment_id, teacher["id"]),
            ).fetchone()
            if not updated:
                raise HTTPException(status_code=404, detail="找不到可管理的作業。")
            _audit(
                conn, teacher["id"], "update_assignment_status", "assignment",
                assignment_id, {"status": status},
            )
    return {"ok": True}


@app.get("/api/teacher/assignments")
def teacher_assignments(teacher=Depends(_require_teacher)):
    with database() as conn:
        return conn.execute(
            """SELECT a.id, a.title, a.description, a.starts_at, a.due_at, a.status,
                      a.time_limit_minutes, a.allow_retake, a.show_answers,
                      COUNT(DISTINCT s.student_id) AS student_count,
                      COUNT(DISTINCT CASE WHEN t.quiz_id IS NOT NULL
                          THEN t.student_id END) AS submitted_students,
                      COUNT(DISTINCT q.question_id) AS question_count,
                      (SELECT AVG(z.score * 1.0 / NULLIF(z.max_score, 0))
                       FROM quiz_sessions z WHERE z.assignment_id = a.id
                         AND z.status = 'submitted') AS average_ratio
               FROM assignment_sets a
               LEFT JOIN assignment_students s ON s.assignment_id = a.id
               LEFT JOIN assignment_questions q ON q.assignment_id = a.id
               LEFT JOIN attempts t ON t.assignment_id = a.id
               WHERE a.teacher_id = %s GROUP BY a.id ORDER BY a.created_at DESC""",
            (teacher["id"],),
        ).fetchall()


@app.get("/api/teacher/assignments/{assignment_id}/statistics")
def assignment_statistics(assignment_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        owned = conn.execute(
            "SELECT 1 FROM assignment_sets WHERE id = %s AND teacher_id = %s",
            (assignment_id, teacher["id"]),
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="找不到可管理的作業。")
        question_rows = conn.execute(
            """SELECT q.id AS question_id, q.prompt, q.points, q.question_type,
                      COUNT(DISTINCT a.student_id) AS response_count,
                      COUNT(DISTINCT CASE
                          WHEN q.question_type IN ('choice', 'fill_blank')
                           AND a.earned_points = q.points THEN a.student_id END)
                          AS correct_count
               FROM assignment_questions aq
               JOIN questions q ON q.id = aq.question_id
               LEFT JOIN attempts a ON a.assignment_id = aq.assignment_id
                                   AND a.question_id = q.id
               WHERE aq.assignment_id = %s
               GROUP BY q.id ORDER BY q.id""",
            (assignment_id,),
        ).fetchall()
        submissions = conn.execute(
            """SELECT u.id AS student_id,
                      (SELECT COUNT(DISTINCT a.question_id) FROM attempts a
                       WHERE a.assignment_id = s.assignment_id
                         AND a.student_id = s.student_id) AS answered_count,
                      COALESCE((SELECT SUM(best.score) FROM (
                          SELECT MAX(a.earned_points) AS score FROM attempts a
                          WHERE a.assignment_id = s.assignment_id
                            AND a.student_id = s.student_id
                            AND a.earned_points IS NOT NULL
                          GROUP BY a.question_id
                      ) best), 0) AS score,
                      COALESCE((SELECT SUM(q.points) FROM assignment_questions aq
                                JOIN questions q ON q.id = aq.question_id
                                WHERE aq.assignment_id = s.assignment_id), 0) AS max_score,
                      (SELECT MAX(a.submitted_at) FROM attempts a
                       WHERE a.assignment_id = s.assignment_id
                         AND a.student_id = s.student_id) AS submitted_at
               FROM assignment_students s JOIN users u ON u.id = s.student_id
               WHERE s.assignment_id = %s ORDER BY u.display_name""",
            (assignment_id,),
        ).fetchall()
    completed_scores = [
        row["score"] for row in submissions if row["submitted_at"] is not None
    ]
    for question in question_rows:
        question["correct_rate"] = (
            question["correct_count"] / question["response_count"]
            if question["response_count"]
            and question["question_type"] in {"choice", "fill_blank"}
            else None
        )
    return {
        "submission_count": len(completed_scores),
        "average_score": (
            sum(completed_scores) / len(completed_scores) if completed_scores else None
        ),
        "highest_score": max(completed_scores) if completed_scores else None,
        "lowest_score": min(completed_scores) if completed_scores else None,
        "questions": question_rows,
    }


@app.get("/api/teacher/assignments/{assignment_id}/submissions")
def assignment_submissions(assignment_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        owned = conn.execute(
            "SELECT 1 FROM assignment_sets WHERE id = %s AND teacher_id = %s",
            (assignment_id, teacher["id"]),
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="找不到可管理的作業。")
        return conn.execute(
            """SELECT u.id AS student_id, u.username, u.display_name,
                      (SELECT COUNT(DISTINCT a.question_id) FROM attempts a
                       WHERE a.assignment_id = s.assignment_id
                         AND a.student_id = s.student_id) AS answered_count,
                      COALESCE((SELECT SUM(best.score) FROM (
                          SELECT MAX(a.earned_points) AS score FROM attempts a
                          WHERE a.assignment_id = s.assignment_id
                            AND a.student_id = s.student_id
                            AND a.earned_points IS NOT NULL
                          GROUP BY a.question_id
                      ) best), 0) AS score,
                      COALESCE((SELECT SUM(q.points) FROM assignment_questions aq
                                JOIN questions q ON q.id = aq.question_id
                                WHERE aq.assignment_id = s.assignment_id), 0) AS max_score,
                      (SELECT MAX(a.submitted_at) FROM attempts a
                       WHERE a.assignment_id = s.assignment_id
                         AND a.student_id = s.student_id) AS submitted_at
               FROM assignment_students s JOIN users u ON u.id = s.student_id
               WHERE s.assignment_id = %s ORDER BY u.display_name""",
            (assignment_id,),
        ).fetchall()


@app.get("/api/teacher/assignments/{assignment_id}/students/{student_id}/attempts")
def assignment_student_attempts(
    assignment_id: int, student_id: int, teacher=Depends(_require_teacher)
):
    with database() as conn:
        permitted = conn.execute(
            """SELECT 1 FROM assignment_students s
               JOIN assignment_sets a ON a.id = s.assignment_id
               WHERE s.assignment_id = %s AND s.student_id = %s AND a.teacher_id = %s""",
            (assignment_id, student_id, teacher["id"]),
        ).fetchone()
        if not permitted:
            raise HTTPException(status_code=404, detail="找不到此作業中的學生。")
        return conn.execute(
            """SELECT q.id AS question_id, latest.id AS attempt_id, q.prompt, q.points,
                      q.answer, q.accepted_answers, q.question_type,
                      latest.response, latest.earned_points, latest.feedback,
                      latest.submitted_at
               FROM assignment_questions aq
               JOIN questions q ON q.id = aq.question_id
               LEFT JOIN LATERAL (
                   SELECT a.id, a.response, a.earned_points, a.feedback, a.submitted_at
                   FROM attempts a
                   WHERE a.assignment_id = aq.assignment_id
                     AND a.student_id = %s AND a.question_id = aq.question_id
                   ORDER BY a.id DESC LIMIT 1
               ) latest ON TRUE
               WHERE aq.assignment_id = %s ORDER BY q.id""",
            (student_id, assignment_id),
        ).fetchall()


@app.post("/api/teacher/attempts/{attempt_id}/adjust-score")
def adjust_attempt_score(
    attempt_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    score = payload.get("score")
    reason = str(payload.get("reason", "")).strip()
    if isinstance(score, bool) or not isinstance(score, int):
        raise HTTPException(status_code=400, detail="分數必須是整數。")
    if not 1 <= len(reason) <= 500:
        raise HTTPException(status_code=400, detail="請填寫 1 至 500 個字元的評分調整原因。")
    with database() as conn:
        with conn.transaction():
            attempt = conn.execute(
                """SELECT a.id, a.quiz_id, a.earned_points, q.points
                   FROM attempts a JOIN questions q ON q.id = a.question_id
                   JOIN assignment_sets s ON s.id = a.assignment_id
                   WHERE a.id = %s AND s.teacher_id = %s FOR UPDATE OF a""",
                (attempt_id, teacher["id"]),
            ).fetchone()
            if not attempt:
                raise HTTPException(status_code=404, detail="找不到此教師作業的作答紀錄。")
            if not 0 <= score <= attempt["points"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"分數需介於 0 至 {attempt['points']}。",
                )
            conn.execute(
                "UPDATE attempts SET earned_points = %s WHERE id = %s",
                (score, attempt_id),
            )
            if attempt["quiz_id"]:
                conn.execute(
                    """UPDATE quiz_sessions SET score = (
                           SELECT COALESCE(SUM(earned_points), 0)
                           FROM attempts WHERE quiz_id = %s
                       ) WHERE id = %s""",
                    (attempt["quiz_id"], attempt["quiz_id"]),
                )
            _audit(
                conn, teacher["id"], "adjust_attempt_score", "attempt", attempt_id,
                {"old_score": attempt["earned_points"], "new_score": score, "reason": reason},
            )
    return {"ok": True}


@app.get("/api/teacher/assignments/{assignment_id}/scores.csv")
def export_assignment_scores(assignment_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        owned = conn.execute(
            "SELECT 1 FROM assignment_sets WHERE id = %s AND teacher_id = %s",
            (assignment_id, teacher["id"]),
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="找不到可管理的作業。")
    students = assignment_submissions(assignment_id, teacher)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "student_id", "username", "display_name", "answered_count",
            "score", "max_score", "submitted_at",
        ],
    )
    writer.writeheader()
    for row in students:
        safe_row = dict(row)
        for field in ("username", "display_name"):
            value = str(safe_row[field] or "")
            if value.startswith(("=", "+", "-", "@", "\t", "\r")):
                safe_row[field] = "'" + value
        writer.writerow(safe_row)
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ccss-assignment-{assignment_id}-scores.csv"'},
    )


@app.get("/api/teacher/essays")
def teacher_essays(teacher=Depends(_require_teacher)):
    with database() as conn:
        return conn.execute(
            """SELECT a.id AS attempt_id, a.student_id, u.display_name AS student_name,
                      q.prompt, q.points AS max_points, q.rubric, a.response, a.earned_points,
                      a.feedback, a.submitted_at
               FROM attempts a JOIN questions q ON q.id = a.question_id
               JOIN users u ON u.id = a.student_id
               WHERE q.question_type = 'essay'
                 AND (a.assignment_id IS NULL OR EXISTS (
                     SELECT 1 FROM assignment_sets s
                     WHERE s.id = a.assignment_id AND s.teacher_id = %s
                 ))
               ORDER BY a.submitted_at DESC LIMIT 200""",
            (teacher["id"],),
        ).fetchall()


@app.post("/api/teacher/essays/{attempt_id}/grade")
def grade_essay(attempt_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)):
    score = payload.get("score")
    feedback = str(payload.get("feedback", "")).strip()
    comments = payload.get("comments", [])
    with database() as conn:
        with conn.transaction():
            attempt = conn.execute(
                """SELECT a.*, q.points, q.question_type, q.prompt
                   FROM attempts a JOIN questions q ON q.id = a.question_id
                   WHERE a.id = %s AND q.question_type = 'essay'
                     AND (a.assignment_id IS NULL OR EXISTS (
                         SELECT 1 FROM assignment_sets s
                         WHERE s.id = a.assignment_id AND s.teacher_id = %s
                     )) FOR UPDATE""",
                (attempt_id, teacher["id"]),
            ).fetchone()
            if not attempt:
                raise HTTPException(status_code=404, detail="找不到可批改作文。")
            if not isinstance(score, int) or not 0 <= score <= attempt["points"]:
                raise HTTPException(status_code=400, detail="作文分數超出範圍。")
            conn.execute(
                "UPDATE attempts SET earned_points = %s, feedback = %s WHERE id = %s",
                (score, feedback, attempt_id),
            )
            conn.execute(
                """UPDATE essay_versions SET status = 'graded', score = %s, feedback = %s
                   WHERE attempt_id = %s AND version_number = (
                       SELECT MAX(version_number) FROM essay_versions WHERE attempt_id = %s
                   )""",
                (score, feedback, attempt_id, attempt_id),
            )
            version_number = conn.execute(
                "SELECT COALESCE(MAX(version_number), 1) AS version FROM essay_versions WHERE attempt_id = %s",
                (attempt_id,),
            ).fetchone()["version"]
            for comment in comments:
                quote = str(comment.get("quoted_text", "")).strip()
                message = str(comment.get("comment", "")).strip()
                if quote and message and quote in attempt["response"]:
                    conn.execute(
                        """INSERT INTO essay_comments
                           (attempt_id, teacher_id, version_number, quoted_text, comment)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (attempt_id, teacher["id"], version_number, quote, message),
                    )
            _audit(conn, teacher["id"], "grade_essay", "attempt", attempt_id, {"score": score})
    return {"ok": True}


@app.post("/api/teacher/essays/{attempt_id}/return")
def return_essay_for_revision(
    attempt_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    feedback = str(payload.get("feedback", "")).strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="請填寫要求修改的內容。")
    with database() as conn:
        with conn.transaction():
            attempt = conn.execute(
                """SELECT a.id FROM attempts a JOIN questions q ON q.id = a.question_id
                   WHERE a.id = %s AND q.question_type = 'essay'
                     AND (a.assignment_id IS NULL OR EXISTS (
                         SELECT 1 FROM assignment_sets s
                         WHERE s.id = a.assignment_id AND s.teacher_id = %s
                     )) FOR UPDATE""",
                (attempt_id, teacher["id"]),
            ).fetchone()
            if not attempt:
                raise HTTPException(status_code=404, detail="找不到可退回的作文。")
            conn.execute(
                """UPDATE attempts SET feedback = %s, revision_requested = TRUE
                   WHERE id = %s""",
                (feedback, attempt_id),
            )
            conn.execute(
                """UPDATE essay_versions SET status = 'revision_requested', feedback = %s
                   WHERE attempt_id = %s AND version_number = (
                       SELECT MAX(version_number) FROM essay_versions WHERE attempt_id = %s
                   )""",
                (feedback, attempt_id, attempt_id),
            )
            _audit(conn, teacher["id"], "request_essay_revision", "attempt", attempt_id)
    return {"ok": True}


@app.post("/api/teacher/announcements")
def create_announcement(payload: dict[str, Any], teacher=Depends(_require_teacher)):
    title = str(payload.get("title", "")).strip()
    content = str(payload.get("content", "")).strip()
    if not title or not content or len(title) > 160 or len(content) > 5000:
        raise HTTPException(status_code=400, detail="公告標題與內容不可空白或過長。")
    with database() as conn:
        return conn.execute(
            """INSERT INTO announcements(teacher_id, title, content)
               VALUES (%s, %s, %s) RETURNING id, title, content, published_at""",
            (teacher["id"], title, content),
        ).fetchone()


@app.get("/api/teacher/announcements")
def teacher_announcements(teacher=Depends(_require_teacher)):
    with database() as conn:
        return conn.execute(
            """SELECT id, title, content, published_at FROM announcements
               WHERE teacher_id = %s ORDER BY published_at DESC LIMIT 100""",
            (teacher["id"],),
        ).fetchall()


@app.patch("/api/teacher/announcements/{announcement_id}")
def update_announcement(
    announcement_id: int, payload: dict[str, Any], teacher=Depends(_require_teacher)
):
    title = str(payload.get("title", "")).strip()
    content = str(payload.get("content", "")).strip()
    if not title or not content or len(title) > 160 or len(content) > 5000:
        raise HTTPException(status_code=400, detail="公告標題與內容不可空白或過長。")
    with database() as conn:
        updated = conn.execute(
            """UPDATE announcements SET title = %s, content = %s
               WHERE id = %s AND teacher_id = %s RETURNING id""",
            (title, content, announcement_id, teacher["id"]),
        ).fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="找不到可編輯的公告。")
    return {"ok": True}


@app.delete("/api/teacher/announcements/{announcement_id}")
def delete_announcement(announcement_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        deleted = conn.execute(
            "DELETE FROM announcements WHERE id = %s AND teacher_id = %s RETURNING id",
            (announcement_id, teacher["id"]),
        ).fetchone()
    if not deleted:
        raise HTTPException(status_code=404, detail="找不到可刪除的公告。")
    return {"ok": True}


@app.get("/api/teacher/reports", dependencies=[Depends(_require_teacher)])
def teacher_reports():
    with database() as conn:
        return conn.execute(
            """SELECT r.id, r.reason, r.status, r.created_at, n.id AS note_id,
                      n.content, u.display_name AS reporter
               FROM note_reports r JOIN notes n ON n.id = r.note_id
               JOIN users u ON u.id = r.reporter_id
               WHERE r.status = 'open' ORDER BY r.created_at"""
        ).fetchall()


@app.get("/api/teacher/question-reports", dependencies=[Depends(_require_teacher)])
def teacher_question_reports():
    with database() as conn:
        return conn.execute(
            """SELECT r.id, r.reason, r.status, r.created_at, q.prompt,
                      u.display_name AS reporter
               FROM question_reports r JOIN questions q ON q.id = r.question_id
               JOIN users u ON u.id = r.reporter_id
               WHERE r.status = 'pending' ORDER BY r.created_at"""
        ).fetchall()


@app.post("/api/teacher/question-reports/{report_id}/resolve")
def resolve_question_report(report_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        with conn.transaction():
            report = conn.execute(
                """UPDATE question_reports SET status = 'resolved' WHERE id = %s
                   RETURNING id""",
                (report_id,),
            ).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="找不到題目回報。")
            _audit(conn, teacher["id"], "resolve_question_report", "question_report", report_id)
    return {"ok": True}


@app.post("/api/teacher/reports/{report_id}/resolve")
def resolve_report(report_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        with conn.transaction():
            report = conn.execute(
                """UPDATE note_reports SET status = 'resolved' WHERE id = %s
                   RETURNING note_id""",
                (report_id,),
            ).fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="找不到檢舉紀錄。")
            _audit(conn, teacher["id"], "resolve_note_report", "note_report", report_id)
    return {"ok": True}


@app.delete("/api/teacher/notes/{note_id}")
def delete_reported_note(note_id: int, teacher=Depends(_require_teacher)):
    with database() as conn:
        with conn.transaction():
            deleted = conn.execute(
                "DELETE FROM notes WHERE id = %s RETURNING id", (note_id,)
            ).fetchone()
            if not deleted:
                raise HTTPException(status_code=404, detail="找不到便利貼。")
            _audit(conn, teacher["id"], "delete_note", "note", note_id)
    return {"ok": True}


@app.post("/api/student/wheel")
def spin_wheel(payload: dict[str, Any], user=Depends(_require_student)):
    request_id = str(payload.get("request_id", ""))
    if not 16 <= len(request_id) <= 128:
        raise HTTPException(status_code=400, detail="轉盤請求識別碼無效，請重試。")
    with database() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (user["id"],))
            previous = conn.execute(
                "SELECT * FROM wheel_spins WHERE student_id = %s AND request_id = %s",
                (user["id"], request_id),
            ).fetchone()
            if previous:
                previous["score"] = _student_score(conn, user["id"])
                return previous
            score = _student_score(conn, user["id"])
            free = conn.execute(
                """SELECT r.streak_day FROM daily_login_rewards r
                   WHERE r.student_id = %s AND r.free_wheel_awarded
                     AND NOT EXISTS (SELECT 1 FROM wheel_spins w
                         WHERE w.student_id = r.student_id AND w.wheel_type = 'streak'
                           AND w.streak_day = r.streak_day)
                   ORDER BY r.login_date LIMIT 1 FOR UPDATE""",
                (user["id"],),
            ).fetchone()
            earned = conn.execute(
                """SELECT COALESCE(SUM(points), 0) AS score FROM (
                       SELECT MAX(COALESCE(earned_points, 0)) AS points
                       FROM attempts WHERE student_id = %s GROUP BY question_id
                   ) best""",
                (user["id"],),
            ).fetchone()["score"]
            milestones_used = conn.execute(
                "SELECT COUNT(*) AS count FROM wheel_spins WHERE student_id = %s AND wheel_type = 'milestone'",
                (user["id"],),
            ).fetchone()["count"]
            milestone = (milestones_used + 1) * 1000
            if earned >= milestone:
                wheel_type, spent = "milestone", 150
                points = secrets.choice([30, 40, 50, 60, 70])
            elif free:
                wheel_type, spent = "streak", 0
                points = secrets.choice([10, 20, 30, 40, 50])
            elif score >= 150:
                wheel_type, spent = "standard", 150
                points = secrets.choice([10, 20, 30, 40, 50])
            else:
                raise HTTPException(status_code=400, detail="積分不足 150 分，且目前沒有免費轉盤。")
            if spent and score < 150:
                raise HTTPException(status_code=400, detail="積分不足 150 分。")
            spin = conn.execute(
                """INSERT INTO wheel_spins
                   (student_id, request_id, wheel_type, score_milestone, streak_day,
                    points_spent, points_awarded)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING wheel_type, points_spent, points_awarded""",
                (
                    user["id"], request_id, wheel_type,
                    milestone if wheel_type == "milestone" else None,
                    free["streak_day"] if wheel_type == "streak" else None,
                    spent, points,
                ),
            ).fetchone()
            spin["score"] = _student_score(conn, user["id"])
    return spin


@app.get("/api/teacher/audit", dependencies=[Depends(_require_teacher)])
def teacher_audit():
    with database() as conn:
        return conn.execute(
            """SELECT l.id, l.action, l.entity_type, l.entity_id, l.details, l.created_at,
                      u.username AS actor
               FROM audit_logs l LEFT JOIN users u ON u.id = l.actor_id
               ORDER BY l.created_at DESC LIMIT 200"""
        ).fetchall()
