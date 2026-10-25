import json
import hashlib
import importlib
import os
import secrets
import smtplib
import ssl
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from email.message import EmailMessage

import streamlit as st

import ccss_db as db


st.set_page_config(page_title="CCSS | English Learning", page_icon="📘", layout="wide")
question_report_api = (
    "report_question",
    "list_question_reports",
    "resolve_question_report",
)
missing_question_report_api = [
    name for name in question_report_api if not callable(getattr(db, name, None))
]
if missing_question_report_api:
    db = importlib.reload(db)
    missing_question_report_api = [
        name for name in question_report_api if not callable(getattr(db, name, None))
    ]
if missing_question_report_api:
    st.error(
        "目前載入的 ccss_db 模組缺少題目回報功能："
        f"{', '.join(missing_question_report_api)}。"
        f"模組路徑：{getattr(db, '__file__', '未知')}。"
        "請確認 streamlit_app.py 與 ccss_db.py 已同步更新。"
    )
    st.stop()
db.initialize_database()
if callable(getattr(db, "daily_backup", None)):
    db.daily_backup()
else:
    st.warning(
        "目前載入的 ccss_db 模組不含每日備份功能。請更新 streamlit_app.py 與 ccss_db.py，"
        "並重新啟動 Streamlit；網站可繼續使用，但本次啟動不會建立備份。"
    )

st.markdown(
    """
    <style>
    .block-container {max-width: 1180px; padding-top: 2rem;}
    .ccss-title {font-size: 2.3rem; font-weight: 800; letter-spacing: .02em;}
    .ccss-subtitle {color: #7d8494; margin-top: -.7rem;}
    div[data-testid="stMetric"] {background: rgba(124,131,253,.10); padding: 1rem; border-radius: 14px;}
    .ccss-wheel-result {animation: ccss-wheel-arrive .9s cubic-bezier(.2,.8,.2,1);}
    .ccss-wheel-stage {position:relative;width:260px;height:260px;margin:1.2rem auto;}
    .ccss-wheel {
      position:absolute;inset:0;border:8px solid #fff;border-radius:50%;
      background:conic-gradient(#7C83FD 0 72deg,#FFB84D 72deg 144deg,
        #37C9A5 144deg 216deg,#F2778C 216deg 288deg,#8D77D8 288deg 360deg);
      box-shadow:0 8px 24px rgba(0,0,0,.25);
      animation:ccss-wheel-spin 4.2s cubic-bezier(.12,.72,.08,1) forwards;
    }
    .ccss-wheel::after {
      content:"";position:absolute;left:50%;top:50%;width:26px;height:26px;
      border-radius:50%;background:#fff;transform:translate(-50%,-50%);
      box-shadow:0 1px 5px rgba(0,0,0,.25);
    }
    .ccss-wheel-pointer {
      position:absolute;z-index:2;left:50%;top:-10px;transform:translateX(-50%);
      width:0;height:0;border-left:17px solid transparent;
      border-right:17px solid transparent;border-top:32px solid #f5d547;
      filter:drop-shadow(0 2px 2px rgba(0,0,0,.35));
    }
    .ccss-wheel-label {
      position:absolute;left:50%;top:50%;z-index:1;
      color:#fff;font-size:1rem;font-weight:800;text-shadow:0 1px 3px #222;
    }
    @keyframes ccss-wheel-spin {
      from {transform:rotate(0deg);}
      to {transform:rotate(var(--wheel-stop));}
    }
    @keyframes ccss-wheel-arrive {
      from {opacity: 0; transform: rotate(-12deg) scale(.75);}
      to {opacity: 1; transform: rotate(0) scale(1);}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def show_header():
    st.markdown('<div class="ccss-title">📘 CCSS</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ccss-subtitle">Connect, Challenge, Succeed in English</div>',
        unsafe_allow_html=True,
    )


def show_login():
    show_header()
    st.write("")
    login_tab, register_tab, recovery_tab = st.tabs(
        ["登入", "建立學生帳號", "忘記密碼"]
    )
    with login_tab:
        st.caption("教師請使用教師帳號登入；學生請使用學生帳號登入。")
        with st.form("login_form"):
            username = st.text_input("帳號")
            password = st.text_input("密碼", type="password")
            submitted = st.form_submit_button("登入", type="primary", width="stretch")
        if submitted:
            user = db.login(username, password)
            if user is None:
                st.error("帳號或密碼錯誤。")
            else:
                st.session_state.user = user
                st.session_state.pending_daily_reward = user.get("daily_reward")
                st.rerun()
    with register_tab:
        with st.form("register_form"):
            username = st.text_input("建立帳號", key="register_username")
            email = st.text_input("電子郵件（用於重設密碼）", key="register_email")
            password = st.text_input("設定密碼（至少 8 個字元）", type="password")
            confirm = st.text_input("再次輸入密碼", type="password")
            submitted = st.form_submit_button(
                "建立帳號並登入", type="primary", width="stretch"
            )
        if submitted:
            try:
                if password != confirm:
                    raise ValueError("兩次輸入的密碼不一致。")
                user_id = db.register_student(username, password, email)
                st.session_state.pending_daily_reward = db.claim_daily_login_reward(
                    user_id
                )
                st.session_state.user = db.get_user(user_id)
                st.success("帳號建立成功。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with recovery_tab:
        st.write("輸入註冊時使用的電子郵件，我們會寄送限時重設連結。")
        with st.form("request_password_reset_form"):
            email = st.text_input("電子郵件")
            submitted = st.form_submit_button("寄送重設連結", type="primary")
        if submitted:
            try:
                send_password_reset(email)
                st.success(
                    "若此郵件已註冊且符合寄送限制，重設連結將會寄出。請查看收件匣與垃圾郵件匣。"
                )
            except (OSError, smtplib.SMTPException, RuntimeError) as exc:
                st.error(f"目前無法寄送重設郵件：{exc}")


def send_password_reset(email):
    host = os.environ.get("CCSS_SMTP_HOST")
    sender = os.environ.get("CCSS_SMTP_FROM")
    public_url = os.environ.get("CCSS_PUBLIC_URL")
    if not host or not sender or not public_url:
        raise RuntimeError(
            "管理員尚未設定 CCSS_SMTP_HOST、CCSS_SMTP_FROM 與 CCSS_PUBLIC_URL。"
        )
    try:
        port = int(os.environ.get("CCSS_SMTP_PORT", "587"))
    except ValueError as exc:
        raise RuntimeError("CCSS_SMTP_PORT 必須是有效的連接埠數字。") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("CCSS_SMTP_PORT 必須介於 1 至 65535。")
    token = db.create_password_reset(email)
    if token is None:
        return
    public_url = public_url.rstrip("/")
    link = f"{public_url}/?reset={token}"
    message = EmailMessage()
    message["Subject"] = "CCSS 密碼重設"
    message["From"] = sender
    message["To"] = email.strip()
    message.set_content(
        f"請於 {db.RESET_TOKEN_TTL_MINUTES} 分鐘內使用以下連結重設密碼：\n\n{link}\n\n"
        "若你沒有提出申請，請忽略這封郵件。"
    )
    username = os.environ.get("CCSS_SMTP_USER")
    password = os.environ.get("CCSS_SMTP_PASSWORD")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(
                host, port, context=ssl.create_default_context(), timeout=20
            ) as server:
                if username:
                    server.login(username, password or "")
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                if username:
                    server.login(username, password or "")
                server.send_message(message)
    except (OSError, smtplib.SMTPException):
        db.cancel_password_reset(token)
        raise


def show_password_reset(token):
    show_header()
    st.subheader("重設密碼")
    with st.form("reset_password_form"):
        password = st.text_input("新密碼（至少 8 個字元）", type="password")
        confirm = st.text_input("確認新密碼", type="password")
        submitted = st.form_submit_button("設定新密碼", type="primary")
    if submitted:
        try:
            if password != confirm:
                raise ValueError("兩次輸入的新密碼不一致。")
            db.reset_student_password(token, password)
            st.query_params.clear()
            st.success("密碼已重設，請使用新密碼登入。")
        except ValueError as exc:
            st.error(str(exc))


def show_account_settings(user):
    st.subheader("個人中心")
    st.write(f"**登入帳號：** {user['username']}")
    st.write(f"**電子郵件：** {user['email'] or '尚未設定'}")
    st.write(f"**帳號建立時間：** {user['created_at']}")
    with st.form("account_name_form"):
        display_name = st.text_input(
            "顯示名稱", value=user["display_name"]
        )
        email = st.text_input("電子郵件", value=user["email"])
        save_name = st.form_submit_button("儲存個人資料")
    if save_name:
        try:
            if user["role"] == "teacher":
                db.teacher_update_profile(user["id"], display_name, email)
            else:
                db.change_display_name(user["id"], display_name)
                if email.strip() != user["email"]:
                    db.set_user_email(user["id"], email)
            st.session_state.user = db.get_user(user["id"])
            st.success("個人資料已更新。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    with st.form("account_password_form"):
        current = st.text_input(
            "目前密碼",
            type="password",
            disabled=bool(user["must_change_password"]),
            help="首次登入更改初始密碼時不需要填寫。",
        )
        new_password = st.text_input("新密碼（至少 8 個字元）", type="password")
        confirm = st.text_input("確認新密碼", type="password")
        save_password = st.form_submit_button(
            "設定新密碼" if user["must_change_password"] else "更改密碼",
            type="primary",
        )
    if save_password:
        try:
            if not user["must_change_password"]:
                authenticated = db.authenticate(user["username"], current)
                if authenticated is None:
                    raise ValueError("目前密碼不正確。")
            if new_password != confirm:
                raise ValueError("兩次輸入的新密碼不一致。")
            db.change_password(user["id"], new_password)
            st.session_state.user = db.get_user(user["id"])
            st.success("密碼已更新。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    with st.expander("最近登入紀錄"):
        history = db.login_history(user["id"])
        if history:
            st.dataframe(
                [
                    {
                        "帳號": entry["username"],
                        "結果": "成功" if entry["succeeded"] else "失敗",
                        "時間": entry["created_at"],
                    }
                    for entry in history
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("尚無登入紀錄。")


def show_leaderboard():
    st.subheader("🏆 學生分數排行榜")
    st.caption("各題只計入個人最高得分；作文在老師批改後列入排名。")
    rows = _leaderboard_rows()
    if not rows:
        st.info("目前還沒有學生，建立學生帳號後就會出現在排行榜。")
        return
    st.dataframe(
        [
            {
                "排名": row["rank"],
                "學生": row["display_name"],
                "總分": row["score"],
                "正答率": f"{row['accuracy']:.1f}%" if row["accuracy"] is not None else "—",
            }
            for row in rows
        ],
        hide_index=True,
        width="stretch",
    )


def _leaderboard_rows():
    rows = db.leaderboard()
    previous_score = None
    rank = 0
    for position, row in enumerate(rows, start=1):
        score = row["score"]
        if score != previous_score:
            rank = position
            previous_score = score
        row.setdefault("rank", rank)
        if "accuracy" not in row:
            answered = row.get("answered_count", 0)
            row["accuracy"] = (
                row["correct_count"] / answered * 100
                if answered
                else None
            )
    return rows


def show_student_dashboard(user):
    progress = db.student_progress(user["id"])
    ranking = _leaderboard_rows()
    daily_reward = st.session_state.pop("pending_daily_reward", None)
    if daily_reward:
        st.balloons()
        reward_text = (
            f"連續登入第 {daily_reward['streak_day']} 天，"
            if daily_reward["streak_day"] > 1
            else "今天登入"
        )
        reward_text += f"獲得 {daily_reward['points_awarded']} 分！"
        if daily_reward["free_wheel_awarded"]:
            reward_text += " 並解鎖一次免費轉盤。"
        st.success(reward_text)
    own_rank = next(
        (row["rank"] for row in ranking if row["id"] == user["id"]),
        None,
    )
    st.subheader(f"歡迎回來，{user['display_name']}！")
    st.caption("準備好今天的練習題了嗎？持續作答，讓自己的名字出現在排行榜上。")
    metrics = st.columns(4)
    metrics[0].metric("目前總分", f"{progress['total_score']} 分")
    metrics[1].metric("排行榜名次", f"第 {own_rank} 名" if own_rank else "—")
    metrics[2].metric("練習題數", f"{progress['questions_attempted']} 題")
    metrics[3].metric("待批改作文", f"{progress['essays_pending']} 篇")
    show_student_wheel(user)
    accuracy_reader = getattr(db, "student_accuracy", None)
    accuracy = (
        accuracy_reader(user["id"])
        if callable(accuracy_reader)
        else next(
            (
                row["accuracy"]
                for row in ranking
                if row["id"] == user["id"]
            ),
            None,
        )
    )
    st.metric("正答率", f"{accuracy:.1f}%" if accuracy is not None else "尚無自動批改作答")

    assignments = db.list_assignments(user["id"])
    if assignments:
        completed = sum(question["attempted"] for question in assignments)
        st.progress(
            completed / len(assignments),
            text=f"老師指派進度：{completed} / {len(assignments)} 題",
        )
        pending = [question for question in assignments if not question["attempted"]]
        if pending:
            st.markdown("#### 📌 待完成作業")
            for question in pending:
                due = f" · 截止 {question['due_at']}" if question["due_at"] else ""
                st.write(
                    f"- **{question['category']} · {question['difficulty']} · "
                    f"{question['points']} 分**{due} — {question['prompt'][:100]}"
                )

    announcements = db.list_announcements()
    if announcements:
        st.markdown("#### 📣 教師公告")
        for announcement in announcements:
            with st.container(border=True):
                st.markdown(f"**{announcement['title']}**")
                st.caption(
                    f"{announcement['teacher_name']} · {announcement['published_at']}"
                )
                st.write(announcement["content"])

    history = db.student_quiz_history(user["id"], limit=5)
    st.markdown("#### 最近測驗成績")
    if history:
        st.dataframe(
            [
                {
                    "完成時間": item["submitted_at"],
                    "得分": f"{item['score']} / {item['max_score']}",
                    "自動批改題答對": f"{item['correct_count']} / {item['choice_count']}",
                    "待批改作文": item["essays_pending"],
                }
                for item in history
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("完成第一次練習後，測驗成績會顯示在這裡。")

    st.write("")
    center = st.columns([1, 3, 1])[1]
    with center:
        st.subheader("🏆 學生分數排行榜")
        st.caption("只依各題最高得分排名，分數相同者並列；作文由老師批改後計入。")
        if ranking:
            st.dataframe(
                [
                    {
                        "排名": row["rank"],
                        "學生": row["display_name"],
                        "總分": f"{row['score']} 分",
                        "正答率": (
                            f"{row['accuracy']:.1f}%"
                            if row["accuracy"] is not None
                            else "—"
                        ),
                    }
                    for row in ranking
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("排行榜目前還沒有學生。")


def show_student_wheel(user):
    st.markdown("#### 🎡 積分轉盤")
    state = db.student_wheel_state(user["id"])
    st.caption(
        "每次抽獎花費 150 分。累積答題得分每達 1000 分，"
        "下一次抽獎改用 +30 至 +70 分的特殊轉盤。"
    )
    if state["special_ready"]:
        st.info(
            f"已解鎖第 {state['next_special_milestone']:,} 分里程碑特殊轉盤。"
        )
    elif state["earned_score"] < state["next_special_milestone"]:
        st.caption(
            f"距離下一個特殊轉盤還差 "
            f"{state['next_special_milestone'] - state['earned_score']} "
            "答題積分。"
        )

    result = st.session_state.pop("last_wheel_result", None)
    if result:
        reward_options = (
            (30, 40, 50, 60, 70)
            if result["wheel_type"] == "milestone"
            else (10, 20, 30, 40, 50)
        )
        winning_index = reward_options.index(result["points_awarded"])
        rotation = 8 * 360 + 360 - (winning_index + 0.5) * 72
        wheel_labels = "".join(
            f'<span class="ccss-wheel-label" style="transform:translate(-50%,-50%) rotate({index * 72 + 36}deg) translateY(-82px) rotate(-{index * 72 + 36}deg)">+{points}</span>'
            for index, points in enumerate(reward_options)
        )
        wheel_type_label = {
            "standard": "一般轉盤",
            "milestone": "千分里程碑特殊轉盤",
            "streak": "連續登入免費轉盤",
        }[result["wheel_type"]]
        st.markdown(
            f'<div class="ccss-wheel-stage">'
            f'<div class="ccss-wheel-pointer"></div>'
            f'<div class="ccss-wheel" style="--wheel-stop:{rotation:.1f}deg">'
            f"{wheel_labels}</div></div>"
            f'<div class="ccss-wheel-result">🎉 {wheel_type_label}：'
            f'獲得 +{result["points_awarded"]} 分！</div>',
            unsafe_allow_html=True,
        )
        spent_text = (
            f"已扣除 {result['points_spent']} 分。"
            if result["points_spent"]
            else "本次使用免費轉盤。"
        )
        st.success(f"{spent_text}目前積分：{result['score']} 分。")

    st.session_state.setdefault("wheel_request_id", secrets.token_urlsafe(24))
    spin_label = (
        f"🎡 使用免費轉盤（剩 {state['free_spins']} 次）"
        if state["free_spins"]
        else "🎡 轉一次（150 分）"
    )
    if st.button(
        spin_label,
        type="primary",
        disabled=not state["can_spin"],
        key="student_wheel_spin",
    ):
        try:
            result = db.spin_student_wheel(
                user["id"], st.session_state["wheel_request_id"]
            )
            st.session_state["last_wheel_result"] = result
            st.session_state["wheel_request_id"] = secrets.token_urlsafe(24)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if not state["can_spin"]:
        st.caption(f"目前有 {state['score']} 分，需至少 150 分才能轉一次。")


def show_sidebar(user):
    sidebar = st.sidebar
    sidebar.write(f"登入：**{user['display_name']}**")
    sidebar.markdown("### 📣 公佈欄")
    announcements = db.list_announcements(5)
    if announcements:
        for announcement in announcements:
            title = announcement["title"]
            with sidebar.expander(title[:45], expanded=False):
                sidebar.caption(
                    f"{announcement['teacher_name']} · "
                    f"{announcement['published_at']}"
                )
                sidebar.write(announcement["content"])
    else:
        sidebar.caption("目前沒有新公告。")

    sidebar.markdown("### 🏆 排名表")
    ranking = _leaderboard_rows()
    if ranking:
        ranks = {
            student["id"]: student["rank"]
            for student in ranking
        }
        visible_ranking = ranking[:5]
        student_row = next(
            (student for student in ranking if student["id"] == user["id"]),
            None,
        )
        if student_row and user["id"] not in {
            student["id"] for student in visible_ranking
        }:
            visible_ranking.append(student_row)
        sidebar.dataframe(
            [
                {
                    "名次": ranks[row["id"]],
                    "學生": row["display_name"],
                    "積分": row["score"],
                    "正答率": (
                        f"{row['accuracy']:.1f}%"
                        if row["accuracy"] is not None
                        else "—"
                    ),
                }
                for row in visible_ranking
            ],
            hide_index=True,
            width="stretch",
            height=min(260, 38 + 35 * len(visible_ranking)),
        )
    else:
        sidebar.caption("目前還沒有學生排名。")

    sidebar.divider()
    if sidebar.button("登出", width="stretch"):
        st.session_state.pop("user", None)
        st.rerun()


def show_quiz(user):
    st.subheader("練習題")
    assignments = db.list_assignments(user["id"])
    if assignments:
        with st.expander(f"老師指派的題目（{len(assignments)}）", expanded=True):
            for question in assignments:
                status = "已作答" if question["attempted"] else "待完成"
                due = f" · 截止 {question['due_at']}" if question["due_at"] else ""
                st.write(
                    f"**{question['category']} · {question['difficulty']} · "
                    f"{question['points']} 分**{due} — {question['prompt'][:120]}　`{status}`"
                )
    question_categories = db.list_questions()
    if not question_categories:
        st.info("題庫尚無題目，請等待老師匯入題庫。")
        return
    grouped_assignments = {
        item["assignment_set_id"]: item
        for item in assignments
        if item.get("assignment_set_id") is not None
    }
    source_options = ["全部題庫", "教師作業"]
    source = st.radio(
        "練習範圍",
        source_options if assignments else source_options[:1],
        horizontal=True,
        key="quiz_source",
    )
    assigned_only = source == "教師作業"
    assignment_id = None
    if assigned_only and grouped_assignments:
        assignment_ids = list(grouped_assignments)
        assignment_id = st.selectbox(
            "選擇作業",
            assignment_ids,
            format_func=lambda value: (
                grouped_assignments[value].get("assignment_title") or f"作業 #{value}"
            ),
            key="student_assignment_id",
        )
        selected_assignment = grouped_assignments[assignment_id]
        st.caption(
            f"{selected_assignment.get('assignment_description', '')} · "
            f"截止：{selected_assignment.get('due_at') or '未設定'} · "
            f"{selected_assignment.get('time_limit_minutes') or '不限時'} 分鐘"
        )
        if not selected_assignment.get("allow_retake") and selected_assignment.get("attempted"):
            st.info("此作業已完成，教師未開放重做。")
            return
    elif assigned_only:
        st.info("目前沒有教師指派的作業。")
        return
    answered_question_ids = {
        attempt["question_id"] for attempt in db.student_attempts(user["id"])
    }
    available_source = (
        [
            question
            for question in question_categories
            if question["id"]
            in {
                q["id"]
                for q in assignments
                if q.get("assignment_set_id") == assignment_id
                if assignment_id is not None
            }
        ]
        if assignment_id is not None
        else [question for question in question_categories if question["id"] in {q["id"] for q in assignments if q.get("assignment_set_id") is None}]
        if assigned_only
        else [
            question
            for question in question_categories
            if question["id"] not in answered_question_ids
        ]
    )
    if not available_source:
        if assigned_only:
            st.info("目前沒有指派題目。")
        else:
            st.success("你已完成所有可練習題目，目前沒有尚未作答的題目。")
        return
    active_quiz_id = st.session_state.get("active_quiz_id")
    active_quiz = db.get_quiz(active_quiz_id, user["id"]) if active_quiz_id else None
    if active_quiz and active_quiz["status"] == "in_progress":
        st.info(f"本次練習共 {len(active_quiz['questions'])} 題，請完成後一次交卷。")
        with st.form(f"quiz_form_{active_quiz_id}"):
            responses = {}
            for number, question in enumerate(active_quiz["questions"], start=1):
                st.markdown(f"#### 第 {number} 題 · {question['points']} 分")
                st.write(question["prompt"])
                question_type_label = {
                    "choice": "選擇題",
                    "fill_blank": "填空題",
                    "essay": "作文",
                }[question["question_type"]]
                st.caption(
                    f"{question['category']} · {question['difficulty']} · "
                    f"{question_type_label}"
                )
                if question["question_type"] == "choice":
                    responses[str(question["id"])] = st.radio(
                        "請選擇答案",
                        question["options"],
                        index=None,
                        key=f"answer_{active_quiz_id}_{question['id']}",
                    )
                elif question["question_type"] == "fill_blank":
                    responses[str(question["id"])] = st.text_input(
                        "請填寫答案",
                        max_chars=500,
                        key=f"answer_{active_quiz_id}_{question['id']}",
                    )
                else:
                    responses[str(question["id"])] = st.text_area(
                        "請以英文作答",
                        height=160,
                        max_chars=20_000,
                        placeholder="Write your answer here...",
                        key=f"answer_{active_quiz_id}_{question['id']}",
                    )
                st.divider()
            confirmed = st.checkbox("我已完成作答，確認提交本次練習")
            submitted = st.form_submit_button("提交並查看成績", type="primary")
        if submitted:
            if not confirmed:
                st.error("請先勾選確認交卷。")
            elif any(not response for response in responses.values()):
                st.error("尚有題目未作答，請完成所有題目再交卷。")
            else:
                try:
                    db.submit_quiz(active_quiz_id, user["id"], responses)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        return

    if active_quiz and active_quiz["status"] == "submitted":
        essay_count = sum(
            question["question_type"] == "essay"
            for question in active_quiz["questions"]
        )
        auto_graded_count = len(active_quiz["questions"]) - essay_count
        feedback_line = (
            f"自動批改題答對 {active_quiz['correct_count']} / {auto_graded_count} 題"
        )
        if essay_count:
            feedback_line += f" · {essay_count} 篇作文待老師批改"
        st.success(
            f"本次得分：{active_quiz['score']} / {active_quiz['max_score']} 分　"
            f"{feedback_line}"
        )
        for number, result in enumerate(
            db.quiz_results(active_quiz_id, user["id"]), start=1
        ):
            if result["earned_points"] is None:
                result_score = "待批改"
            else:
                outcome = (
                    "答對"
                    if result["earned_points"] == result["points"]
                    else "答錯"
                )
                result_score = (
                    f"{outcome} · {result['earned_points']} / {result['points']} 分"
                )
            with st.expander(f"第 {number} 題 · {result_score}"):
                st.write(result["prompt"])
                if result["question_type"] in ("choice", "fill_blank"):
                    if result["question_type"] == "choice":
                        st.markdown("**選項：**")
                        for option_number, option in enumerate(
                            result["options"], start=1
                        ):
                            st.write(f"{option_number}. {option}")
                        st.write(f"你的選擇：{result['response']}")
                    else:
                        st.write(f"你的答案：{result['response']}")
                    if result["answer"]:
                        st.write(f"正確答案：{result['answer']}")
                    if result["explanation"]:
                        st.info(f"解析：{result['explanation']}")
                else:
                    st.info("作文已送出，等待老師批改。")
        if st.button("開始新的練習", type="primary"):
            st.session_state.pop("active_quiz_id", None)
            st.rerun()
        return

    category_options = sorted({question["category"] for question in available_source})
    col1, col2, col3 = st.columns(3)
    category = col1.selectbox("題目類別／範圍", category_options, key="quiz_category")
    difficulty_options = ["全部難度", *db.DIFFICULTIES]
    difficulty = col2.selectbox("難度", difficulty_options, key="quiz_difficulty")
    matching = [
        question
        for question in available_source
        if question["category"] == category
        and (difficulty == "全部難度" or question["difficulty"] == difficulty)
    ]
    question_count = col3.number_input(
        "本次題數",
        min_value=1,
        max_value=max(
            1,
            min(200 if assignment_id is not None else 50, len(matching)),
        ),
        value=max(1, min(5, len(matching))),
        step=1,
        key=f"quiz_question_count_{category}_{difficulty}_{assignment_id}_{len(matching)}",
    )
    if not matching:
        st.info("這個條件目前沒有題目。")
        return
    st.caption(f"符合條件的題目：{len(matching)} 題")
    if st.button("開始練習", type="primary"):
        try:
            st.session_state.active_quiz_id = db.begin_filtered_quiz(
                user["id"],
                category,
                None if difficulty == "全部難度" else difficulty,
                int(question_count),
                assigned_only=assigned_only,
                assignment_id=assignment_id,
            )
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def show_student_attempts(user):
    st.subheader("我的作答紀錄")
    attempts = db.student_attempts(user["id"])
    if not attempts:
        st.info("你還沒有作答紀錄，前往「練習題」開始挑戰吧！")
        return
    for attempt in attempts:
        if attempt["earned_points"] is None:
            score = "待老師批改"
        else:
            score = f"{attempt['earned_points']} / {attempt['points']} 分"
        question_type = {
            "choice": "選擇題",
            "fill_blank": "填空題",
            "essay": "作文",
        }[attempt["question_type"]]
        with st.expander(
            f"{question_type} · {attempt['category']} · {attempt['difficulty']} · "
            f"{score} · {attempt['submitted_at']}"
        ):
            st.markdown(f"**題目：** {attempt['prompt']}")
            if attempt["question_type"] in ("choice", "fill_blank"):
                if attempt["question_type"] == "choice":
                    st.markdown("**選項：**")
                    for option_number, option in enumerate(attempt["options"], start=1):
                        st.write(f"{option_number}. {option}")
                    st.write(f"你的選擇：{attempt['response']}")
                else:
                    st.write(f"你的答案：{attempt['response']}")
                is_wrong = (
                    attempt["earned_points"] is not None
                    and attempt["earned_points"] < attempt["points"]
                )
                outcome = (
                    "答對"
                    if attempt["earned_points"] == attempt["points"]
                    else "答錯"
                )
                st.write(f"作答結果：{outcome}（{score}）")
                if attempt["answer"]:
                    st.write(f"正確答案：{attempt['answer']}")
                if attempt["explanation"]:
                    st.info(f"解析：{attempt['explanation']}")
                if is_wrong:
                    with st.form(f"report_question_{attempt['attempt_id']}"):
                        reason = st.text_area(
                            "回報題目問題（例如答案或選項有誤）",
                            max_chars=500,
                            key=f"question_report_reason_{attempt['attempt_id']}",
                        )
                        report_submitted = st.form_submit_button("回報此題")
                    if report_submitted:
                        try:
                            db.report_question(
                                attempt["question_id"], user["id"], reason
                            )
                            st.success("已回報此題，老師會進行檢查。")
                        except ValueError as exc:
                            st.error(str(exc))
            else:
                st.write(f"作文內容：{attempt['response']}")
                if attempt["rubric"]:
                    st.info(f"評分規準：{attempt['rubric']}")
                if attempt["earned_points"] is None:
                    status_text = {
                        "revision_requested": "老師已退回作文，請到作文中心修改後重新提交。",
                        "submitted": "作文已送出，等待老師批改。",
                    }.get(attempt["essay_status"], "作文已送出，等待老師批改。")
                    st.info(status_text)
                if attempt["feedback"]:
                    st.markdown("**老師評語：**")
                    st.write(attempt["feedback"])
                if attempt["attempt_id"] and attempt["version_number"]:
                    comments = db.essay_comments(
                        attempt["attempt_id"], attempt["version_number"], user["id"]
                    )
                    for comment in comments:
                        st.warning(
                            f"原文：{comment['quoted_text']}\n\n"
                            f"批註：{comment['comment']}"
                        )


def show_notes(user):
    st.subheader("📝 學生便利貼")
    st.caption("留下想說的話，所有學生都能閱讀並回覆。請勿在公開便利貼中分享個人敏感資訊。")
    with st.form("new_note_form", clear_on_submit=True):
        content = st.text_area("寫一張便利貼", max_chars=2000)
        submitted = st.form_submit_button("張貼")
    if submitted:
        try:
            db.create_note(user["id"], content)
            st.success("便利貼已發布。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    for note in db.list_notes():
        with st.container(border=True):
            st.markdown(f"**{note['display_name']}** · {note['created_at']}")
            st.write(note["content"])
            if note["author_id"] == user["id"]:
                with st.expander("編輯或刪除我的便利貼"):
                    with st.form(f"edit_note_{note['id']}"):
                        updated_content = st.text_area(
                            "便利貼內容",
                            value=note["content"],
                            max_chars=2000,
                            key=f"note_content_{note['id']}",
                        )
                        update_submitted = st.form_submit_button("儲存修改")
                    if update_submitted:
                        try:
                            db.update_note(note["id"], user["id"], updated_content)
                            st.success("便利貼已更新。")
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
                    if st.button("刪除便利貼", key=f"delete_note_{note['id']}"):
                        try:
                            db.delete_note(note["id"], user["id"])
                            st.success("便利貼已刪除。")
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
            else:
                with st.expander("檢舉這張便利貼"):
                    with st.form(f"report_note_{note['id']}"):
                        reason = st.text_input(
                            "檢舉原因",
                            max_chars=500,
                            key=f"report_reason_{note['id']}",
                        )
                        report_submitted = st.form_submit_button("送出檢舉")
                    if report_submitted:
                        try:
                            db.report_note(note["id"], user["id"], reason)
                            st.success("已送出檢舉，老師將會檢視。")
                        except ValueError as exc:
                            st.error(str(exc))
            for reply in note["replies"]:
                st.caption(
                    f"↳ {reply['display_name']} · {reply['created_at']}: {reply['content']}"
                )
            with st.form(f"reply_{note['id']}", clear_on_submit=True):
                reply = st.text_input("回覆這張便利貼", max_chars=1000)
                submitted = st.form_submit_button("回覆")
            if submitted:
                try:
                    db.reply_to_note(note["id"], user["id"], reply)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def _save_essay_draft_callback(student_id, question_id, widget_key):
    try:
        db.save_essay_draft(
            student_id, question_id, st.session_state.get(widget_key, "")
        )
        st.session_state.pop(f"{widget_key}_error", None)
    except ValueError as exc:
        st.session_state[f"{widget_key}_error"] = str(exc)


def show_student_essays(user):
    st.subheader("作文中心")
    st.caption("草稿會在修改時自動儲存；繳交後需等待老師批改或退回修改。")
    essays = db.list_student_essays(user["id"])
    if not essays:
        st.info("目前沒有可使用的作文題目。")
        return
    assignments_by_question = {}
    for assignment in db.list_assignments(user["id"]):
        if assignment.get("question_type") == "essay":
            assignments_by_question.setdefault(assignment["id"], assignment)

    for essay in essays:
        with st.expander(
            f"{essay['category']} · {essay['difficulty']} · {essay['points']} 分 · "
            f"{essay['prompt'][:70]}"
        ):
            st.write(essay["prompt"])
            if essay["rubric"]:
                st.info(f"評分規準：{essay['rubric']}")
            if essay["min_word_count"]:
                st.caption(f"最低字數：{essay['min_word_count']} 個英文單字")

            if essay["attempt_id"]:
                status_labels = {
                    "submitted": "等待老師批改",
                    "revision_requested": "老師要求修改",
                    "graded": "已批改",
                }
                st.write(
                    f"目前狀態：{status_labels.get(essay['essay_status'], '已提交')} · "
                    f"版本 {essay['version_number']} · "
                    f"{essay['earned_points'] if essay['earned_points'] is not None else '待批改'}"
                    f"{' / ' + str(essay['points']) + ' 分' if essay['earned_points'] is not None else ''}"
                )
                versions = db.student_essay_versions(
                    user["id"], essay["attempt_id"]
                )
                for version in versions:
                    with st.expander(
                        f"作文第 {version['version_number']} 版 · {version['submitted_at']}"
                    ):
                        st.write(version["content"])
                        if version["feedback"]:
                            st.info(f"老師評語／修改建議：{version['feedback']}")
                        for comment in db.essay_comments(
                            essay["attempt_id"],
                            version["version_number"],
                            user["id"],
                        ):
                            st.warning(
                                f"原文：{comment['quoted_text']}\n\n"
                                f"批註：{comment['comment']}"
                            )

            can_edit = (
                not essay["attempt_id"]
                or essay["essay_status"] == "revision_requested"
            )
            if not can_edit:
                continue

            draft_key = f"essay_draft_{user['id']}_{essay['question_id']}"
            if draft_key not in st.session_state:
                st.session_state[draft_key] = essay["draft_content"] or ""
            assignment = assignments_by_question.get(essay["question_id"])
            if assignment:
                st.caption(
                    f"教師作業：{assignment.get('assignment_title', '作文作業')} · "
                    f"截止：{assignment.get('due_at') or '未設定'}"
                )
            draft = st.text_area(
                "作文草稿（自動儲存）",
                height=220,
                max_chars=20_000,
                key=draft_key,
                on_change=_save_essay_draft_callback,
                args=(user["id"], essay["question_id"], draft_key),
            )
            if f"{draft_key}_error" in st.session_state:
                st.error(st.session_state[f"{draft_key}_error"])
            word_count = len(re.findall(r"\b[\w'-]+\b", draft))
            st.caption(f"目前字數：{word_count} 個英文單字")
            if st.button(
                "提交作文" if not essay["attempt_id"] else "提交修改版本",
                type="primary",
                key=f"submit_essay_{user['id']}_{essay['question_id']}",
            ):
                try:
                    result = db.submit_essay(
                        user["id"],
                        essay["question_id"],
                        draft,
                        assignment_id=(
                            assignment.get("assignment_set_id")
                            if assignment
                            else None
                        ),
                    )
                    st.success(
                        f"作文第 {result['version_number']} 版已提交給老師。"
                    )
                    st.session_state.pop(draft_key, None)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def question_import_example():
    return {
        "title": "English Grammar Test 01",
        "category": "grammar",
        "level": "intermediate",
        "questions": [
            {
                "id": "G001",
                "type": "single_choice",
                "difficulty": "easy",
                "points": 5,
                "question": "She _____ to school every day.",
                "options": ["go", "goes", "going", "gone"],
                "answer": "goes",
                "explanation": "Use the third-person singular form in the present simple tense.",
            },
            {
                "id": "G002",
                "type": "fill_blank",
                "difficulty": "medium",
                "points": 10,
                "question": "They have _____ their homework.",
                "accepted_answers": ["finished", "completed"],
                "explanation": "Use the past participle after have.",
            },
            {
                "id": "W001",
                "type": "essay",
                "difficulty": "upper_intermediate",
                "points": 20,
                "category": "writing",
                "question": "Write about a memorable day.",
                "min_word_count": 80,
                "rubric": "Content: 10 points; grammar and vocabulary: 10 points.",
            },
        ],
    }


def read_xlsx_rows(content):
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", namespace):
                shared_strings.append(
                    "".join(text.text or "" for text in item.findall(".//main:t", namespace))
                )
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in archive.namelist():
            raise ValueError("Excel 活頁簿找不到第一張工作表。")
        sheet = ET.fromstring(archive.read(sheet_name))
        rows = []
        for row in sheet.findall(".//main:sheetData/main:row", namespace):
            values = []
            for cell in row.findall("main:c", namespace):
                reference = cell.attrib.get("r", "")
                column = "".join(char for char in reference if char.isalpha())
                column_index = 0
                for char in column:
                    column_index = column_index * 26 + ord(char.upper()) - ord("A") + 1
                while len(values) < column_index:
                    values.append("")
                value_node = cell.find("main:v", namespace)
                value = value_node.text if value_node is not None else ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared_strings[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(
                        text.text or ""
                        for text in cell.findall(".//main:t", namespace)
                    )
                values[column_index - 1] = value
            rows.append(values)
    return rows


def parse_question_import(filename, content):
    suffix = filename.lower().rsplit(".", maxsplit=1)[-1]
    if suffix == "json":
        payload = json.loads(content.decode("utf-8-sig"))
        if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
            raise ValueError("JSON 檔案最上層必須包含 questions 陣列。")
        return payload
    if suffix == "csv":
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    elif suffix == "xlsx":
        rows = read_xlsx_rows(content)
        if not rows:
            raise ValueError("Excel 工作表沒有內容。")
        headings = [str(value).strip() for value in rows[0]]
        rows = [
            dict(zip(headings, row + [""] * max(0, len(headings) - len(row))))
            for row in rows[1:]
            if any(str(value).strip() for value in row)
        ]
    else:
        raise ValueError("僅支援 JSON、CSV 或 XLSX 檔案。")
    questions = []
    for row in rows:
        options = row.get("options", "")
        accepted_answers = row.get("accepted_answers", "")
        questions.append(
            {
                **row,
                "options": [
                    option.strip() for option in str(options).split("|") if option.strip()
                ],
                "accepted_answers": [
                    answer.strip()
                    for answer in str(accepted_answers).split("|")
                    if answer.strip()
                ]
                or [str(row.get("answer", "")).strip()],
                "points": int(row["points"]) if str(row.get("points", "")).isdigit() else row.get("points"),
                "min_word_count": (
                    int(row["min_word_count"])
                    if str(row.get("min_word_count", "")).isdigit()
                    else row.get("min_word_count", 0)
                ),
            }
        )
    return {"questions": questions}


def extract_pdf_text(content):
    if not content or len(content) > 15_000_000:
        raise ValueError("PDF 檔案大小需介於 1 byte 至 15 MB。")
    try:
        import pymupdf as fitz
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "PDF 文字與 OCR 套件尚未安裝，請安裝 requirements.txt 中的套件後重啟。"
        ) from exc

    try:
        document = fitz.open(stream=content, filetype="pdf")
    except (fitz.FileDataError, fitz.EmptyFileError) as exc:
        raise ValueError("無法讀取 PDF；請確認檔案未損毀且未加密。") from exc
    if document.needs_pass:
        document.close()
        raise ValueError("目前不支援有密碼保護的 PDF。")
    if not 1 <= document.page_count <= 20:
        document.close()
        raise ValueError("PDF 頁數需介於 1 至 20 頁。")

    language = os.environ.get("CCSS_TESSERACT_LANG", "eng")
    pages = []
    try:
        for page_number, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            if len(text) < 20:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                    try:
                        text = pytesseract.image_to_string(
                            image, lang=language, timeout=20
                        ).strip()
                    except pytesseract.TesseractNotFoundError as exc:
                        raise RuntimeError(
                            "伺服器找不到 Tesseract OCR。請安裝 tesseract-ocr 和 "
                            "tesseract-ocr-eng，並確認 tesseract 在 PATH 中。"
                        ) from exc
                    except pytesseract.TesseractError as exc:
                        raise RuntimeError(
                            "PDF OCR 執行失敗。請確認已安裝 CCSS_TESSERACT_LANG "
                            f"指定的語言資料（目前：{language}）。"
                        ) from exc
                    except RuntimeError as exc:
                        raise RuntimeError(
                            f"PDF 第 {page_number} 頁 OCR 逾時或失敗：{exc}"
                        ) from exc
            pages.append(f"--- 第 {page_number} 頁 ---\n{text}")
    finally:
        document.close()
    extracted = "\n\n".join(pages).strip()
    if not extracted:
        raise ValueError("PDF 沒有可辨識的文字內容。")
    return extracted[:100_000]


def show_teacher_students(teacher):
    st.subheader("學生管理")
    search = st.text_input("搜尋學生帳號、暱稱、電子郵件或班級", key="student_search")
    students = db.student_overview(teacher["id"], search)
    if students:
        st.dataframe(
            [
                {
                    "帳號": student["username"],
                    "暱稱": student["display_name"],
                    "班級": student["class_name"],
                    "狀態": "啟用" if student["is_active"] else "停用",
                    "積分": student["score"],
                    "正答率": (
                        f"{student['accuracy']:.1f}%"
                        if student.get("accuracy") is not None
                        else "—"
                    ),
                    "排名": student.get("rank") or "—",
                }
                for student in students
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("找不到學生，或尚未建立任何學生帳號。")

    create_tab, classes_tab, detail_tab = st.tabs(
        ["建立學生帳號", "班級管理", "學生個人資料與成績"]
    )
    with create_tab:
        with st.form("teacher_create_student"):
            username = st.text_input("登入帳號")
            display_name = st.text_input("學生暱稱")
            email = st.text_input("電子郵件（可留空）")
            class_name = st.text_input("班級（可留空）")
            temporary_password = st.text_input(
                "初始密碼（至少 8 個字元）", type="password"
            )
            submitted = st.form_submit_button("建立學生帳號", type="primary")
        if submitted:
            try:
                student_id = db.create_student(
                    teacher["id"],
                    username,
                    display_name or username,
                    temporary_password,
                    email,
                    class_name,
                )
                st.success(f"學生帳號已建立，學生編號：{student_id}")
            except ValueError as exc:
                st.error(str(exc))

    with classes_tab:
        st.markdown("#### 建立新班級")
        with st.form("create_class_form", clear_on_submit=True):
            new_class_name = st.text_input(
                "班級名稱", placeholder="例如：Grade 7A"
            )
            create_class_submitted = st.form_submit_button(
                "建立班級", type="primary"
            )
        if create_class_submitted:
            try:
                db.create_class(teacher["id"], new_class_name)
                st.success("班級已建立。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

        classes = db.list_classes(teacher["id"])
        all_students = db.student_overview(teacher["id"])
        active_students = [
            student for student in all_students if student["is_active"]
        ]
        if classes:
            st.markdown("#### 勾選學生加入班級")
            selected_class_id = st.selectbox(
                "選擇班級",
                [class_item["id"] for class_item in classes],
                format_func=lambda value: next(
                    f"{class_item['name']}（{class_item['student_count']} 人）"
                    for class_item in classes
                    if class_item["id"] == value
                ),
                key="manage_class_id",
            )
            current_members = set(
                db.class_student_ids(teacher["id"], selected_class_id)
            )
            selected_student_ids = st.multiselect(
                "學生名單",
                [student["id"] for student in active_students],
                default=[
                    student_id
                    for student_id in current_members
                    if any(
                        student["id"] == student_id
                        for student in active_students
                    )
                ],
                format_func=lambda value: next(
                    f"{student['username']} · {student['display_name']}"
                    for student in active_students
                    if student["id"] == value
                ),
                key=f"class_students_{selected_class_id}",
            )
            if st.button(
                "儲存班級學生",
                type="primary",
                key=f"save_class_students_{selected_class_id}",
            ):
                try:
                    db.set_class_students(
                        teacher["id"], selected_class_id, selected_student_ids
                    )
                    st.success("班級學生名單已更新。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            st.dataframe(
                [
                    {
                        "班級": class_item["name"],
                        "學生人數": class_item["student_count"],
                    }
                    for class_item in classes
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("建立班級後，即可勾選學生加入。")

    with detail_tab:
        if not students:
            st.info("請先建立學生帳號，或調整搜尋條件。")
            return
        selected_id = st.selectbox(
            "選擇學生",
            [student["id"] for student in students],
            format_func=lambda value: next(
                f"{student['username']} · {student['display_name']}"
                for student in students
                if student["id"] == value
            ),
        )
        detail = db.get_student_detail(teacher["id"], selected_id)
        detail_cols = st.columns(5)
        detail_cols[0].metric("累積積分", detail["progress"]["total_score"])
        detail_cols[1].metric("排行榜", detail["rank"] or "—")
        detail_cols[2].metric("作答題數", detail["progress"]["questions_attempted"])
        detail_cols[3].metric(
            "正答率",
            f"{detail['accuracy']:.1f}%"
            if detail.get("accuracy") is not None
            else "—",
        )
        detail_cols[4].metric("帳號狀態", "啟用" if detail["is_active"] else "停用")
        with st.form(f"edit_student_{selected_id}"):
            updated_name = st.text_input("暱稱", value=detail["display_name"])
            updated_email = st.text_input("電子郵件", value=detail["email"])
            updated_class = st.text_input("班級", value=detail["class_name"])
            save_student = st.form_submit_button("儲存學生資料")
        if save_student:
            try:
                db.update_student(
                    teacher["id"], selected_id, updated_name, updated_email, updated_class
                )
                st.success("學生資料已更新。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        active_col, reset_col = st.columns(2)
        with active_col:
            action_label = "停用帳號" if detail["is_active"] else "重新啟用帳號"
            if st.button(action_label, key=f"toggle_student_{selected_id}"):
                db.set_student_active(teacher["id"], selected_id, not detail["is_active"])
                st.rerun()
        with reset_col:
            with st.form(f"reset_student_password_{selected_id}"):
                new_password = st.text_input("重設登入密碼", type="password")
                reset_submitted = st.form_submit_button("重設密碼")
            if reset_submitted:
                try:
                    db.reset_student_password_by_teacher(
                        teacher["id"], selected_id, new_password
                    )
                    st.success("密碼已重設；學生下次登入需更改密碼。")
                except ValueError as exc:
                    st.error(str(exc))
        st.markdown("#### 歷次測驗")
        if detail["quizzes"]:
            st.dataframe(
                [
                    {
                        "提交時間": quiz["submitted_at"],
                        "分數": f"{quiz['score']} / {quiz['max_score']}",
                        "答對自動批改題": f"{quiz['correct_count']} / {quiz['choice_count']}",
                    }
                    for quiz in detail["quizzes"]
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.caption("尚無已提交測驗。")
        with st.expander("個別作答紀錄"):
            st.dataframe(
                [
                    {
                        "作答時間": attempt["submitted_at"],
                        "類別": attempt["category"],
                        "題目": attempt["prompt"],
                        "回答": attempt["response"],
                        "得分": (
                            "待批改"
                            if attempt["earned_points"] is None
                            else f"{attempt['earned_points']} / {attempt['points']}"
                        ),
                    }
                    for attempt in detail["attempts"]
                ],
                hide_index=True,
                width="stretch",
            )

def show_question_bank():
    st.subheader("題庫管理")
    teacher_id = st.session_state.user["id"]
    banks = db.list_question_banks(teacher_id)
    bank_tab, add_tab, import_tab, browse_tab = st.tabs(
        ["題庫", "新增／編輯題目", "整份考卷匯入", "搜尋與預覽"]
    )
    with bank_tab:
        with st.form("create_bank_form", clear_on_submit=True):
            new_bank_name = st.text_input("新題庫名稱")
            new_bank_description = st.text_area("題庫說明")
            create_bank = st.form_submit_button("建立題庫", type="primary")
        if create_bank:
            try:
                db.create_question_bank(
                    teacher_id, new_bank_name, new_bank_description
                )
                st.success("題庫已建立。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        if banks:
            st.dataframe(
                [
                    {
                        "題庫": bank["name"],
                        "說明": bank["description"],
                        "題目數": bank["question_count"],
                    }
                    for bank in banks
                ],
                hide_index=True,
                width="stretch",
            )
            selected_bank = st.selectbox(
                "編輯題庫",
                [bank["id"] for bank in banks],
                format_func=lambda value: next(
                    bank["name"] for bank in banks if bank["id"] == value
                ),
            )
            bank = next(item for item in banks if item["id"] == selected_bank)
            with st.form("edit_bank_form"):
                bank_name = st.text_input("名稱", value=bank["name"])
                bank_description = st.text_area("說明", value=bank["description"])
                save_bank = st.form_submit_button("儲存題庫修改")
            if save_bank:
                try:
                    db.update_question_bank(
                        teacher_id, selected_bank, bank_name, bank_description
                    )
                    st.success("題庫已更新。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            if bank["question_count"] == 0 and st.button(
                "刪除此空題庫", key=f"delete_bank_{selected_bank}"
            ):
                try:
                    db.delete_question_bank(teacher_id, selected_bank)
                    st.success("題庫已刪除。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with add_tab:
        if not banks:
            st.warning("請先建立題庫，再新增或匯入題目。")
        bank_options = [bank["id"] for bank in banks]
        bank_names = {bank["id"]: bank["name"] for bank in banks}
        existing_questions = db.list_questions(
            include_drafts=True, teacher_id=teacher_id
        )
        selected_question_id = st.selectbox(
            "選擇既有題目進行編輯（或選新增題目）",
            [None, *[question["id"] for question in existing_questions]],
            format_func=lambda value: (
                "＋ 新增題目"
                if value is None
                else next(
                    f"{question['category']} · {question['prompt'][:60]}"
                    for question in existing_questions
                    if question["id"] == value
                )
            ),
        )
        selected_question = next(
            (
                question
                for question in existing_questions
                if question["id"] == selected_question_id
            ),
            None,
        )
        with st.form("add_question_form"):
            question_bank_id = st.selectbox(
                "題庫",
                [None, *bank_options],
                index=(
                    [None, *bank_options].index(selected_question["bank_id"])
                    if selected_question
                    and selected_question["bank_id"] in bank_options
                    else 0
                ),
                format_func=lambda value: (
                    "未分類"
                    if value is None
                    else bank_names[value]
                ),
                disabled=not banks,
            ) if banks else None
            category = st.text_input(
                "類別",
                value=selected_question["category"] if selected_question else "",
                placeholder="Grammar",
            )
            scope = st.text_input(
                "範圍／知識點",
                value=selected_question["scope"] if selected_question else "",
                placeholder="Present simple",
            )
            grade_level = st.text_input(
                "年級／程度",
                value=selected_question["grade_level"] if selected_question else "",
                placeholder="Grade 7",
            )
            difficulty = st.selectbox(
                "難度",
                db.DIFFICULTIES,
                index=(
                    db.DIFFICULTIES.index(selected_question["difficulty"])
                    if selected_question
                    else 0
                ),
            )
            question_type = st.selectbox(
                "題型",
                ["choice", "fill_blank", "essay"],
                index=(
                    ["choice", "fill_blank", "essay"].index(
                        selected_question["question_type"]
                    )
                    if selected_question
                    else 0
                ),
                format_func=lambda value: {
                    "choice": "選擇題",
                    "fill_blank": "填空題",
                    "essay": "作文",
                }[value],
            )
            prompt = st.text_area(
                "題目內容",
                value=selected_question["prompt"] if selected_question else "",
            )
            options_text = st.text_area(
                "選項（選擇題每行一個選項）",
                value="\n".join(selected_question["options"])
                if selected_question
                else "",
            )
            answer_default = (
                "|".join(selected_question["accepted_answers"])
                if selected_question
                and selected_question["question_type"] == "fill_blank"
                else selected_question["answer"]
                if selected_question
                else ""
            )
            answer = st.text_input(
                "正確答案／填空接受答案（多個答案以 | 分隔）",
                value=answer_default,
            )
            explanation = st.text_area(
                "解說（選填）",
                value=selected_question["explanation"] if selected_question else "",
            )
            rubric = st.text_area(
                "作文評分規準（作文題使用）",
                value=selected_question["rubric"] if selected_question else "",
                max_chars=5000,
                help="可列出內容、文法、組織等評分重點。",
            )
            min_word_count = st.number_input(
                "作文最低英文單字數（作文題使用）",
                min_value=0,
                max_value=10_000,
                value=(
                    selected_question["min_word_count"]
                    if selected_question
                    else 0
                ),
                step=10,
            )
            points = st.number_input(
                "分數",
                min_value=1,
                max_value=100,
                value=selected_question["points"] if selected_question else 5,
            )
            published = st.checkbox(
                "發布後供學生練習",
                value=bool(selected_question["is_published"])
                if selected_question
                else True,
            )
            if selected_question:
                st.caption(
                    f"目前編輯：{selected_question['prompt']} "
                    f"（題庫：{selected_question['bank_id'] or '未分類'}）"
                )
            submitted = st.form_submit_button(
                "儲存題目修改" if selected_question else "新增題目",
                type="primary",
            )
        if submitted:
            try:
                data = {
                    "bank_id": question_bank_id,
                    "category": category,
                    "scope": scope,
                    "grade_level": grade_level,
                    "difficulty": difficulty,
                    "type": question_type,
                    "prompt": prompt,
                    "options": options_text.splitlines()
                    if question_type == "choice"
                    else [],
                    "answer": answer.strip()
                    if question_type != "fill_blank"
                    else answer.split("|")[0].strip(),
                    "accepted_answers": answer,
                    "explanation": explanation,
                    "rubric": rubric,
                    "min_word_count": int(min_word_count),
                    "points": int(points),
                    "is_published": published,
                }
                if selected_question:
                    db.update_question(
                        teacher_id, selected_question_id, data
                    )
                    st.success("題目已更新。")
                else:
                    db.add_question(teacher_id, data)
                    st.success("題目已新增。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with import_tab:
        st.write(
            "支援 PDF 文字抽取與掃描檔 OCR，以及 JSON、CSV、XLSX 匯入。PDF 辨識文字可供參考；請在「新增／編輯題目」手動整理，或依下方 JSON 範例建立題目後匯入。"
        )
        with st.expander("PDF 考卷文字抽取／OCR"):
            st.caption(
                "支援 1–20 頁、15 MB 以內的 PDF。掃描檔以 Tesseract 英文 OCR "
                "辨識；辨識結果可能有誤，請老師校對後再出題。"
            )
            pdf_upload = st.file_uploader(
                "上傳 PDF 考卷",
                type=["pdf"],
                key="question_source_pdf",
            )
            if pdf_upload is not None:
                pdf_content = pdf_upload.getvalue()
                pdf_digest = hashlib.sha256(pdf_content).hexdigest()
                if st.session_state.get("question_pdf_digest") != pdf_digest:
                    try:
                        extracted_text = extract_pdf_text(pdf_content)
                        st.session_state.question_pdf_digest = pdf_digest
                        st.session_state.question_pdf_text = extracted_text
                        st.session_state.pop("question_pdf_error", None)
                    except (ValueError, RuntimeError) as exc:
                        st.session_state.question_pdf_error = str(exc)
                        st.session_state.pop("question_pdf_text", None)
                        st.session_state.question_pdf_digest = pdf_digest
                if "question_pdf_error" in st.session_state:
                    st.error(st.session_state.question_pdf_error)
                elif "question_pdf_text" in st.session_state:
                    st.text_area(
                        "已抽取／辨識的題目文字（可選取複製至手動出題）",
                        value=st.session_state.question_pdf_text,
                        height=260,
                        key="question_pdf_text_editor",
                    )
                    st.download_button(
                        "下載抽取文字",
                        st.session_state.question_pdf_text,
                        file_name="ccss-pdf-extracted.txt",
                        mime="text/plain",
                    )
        with st.expander("完整 JSON 匯入範例", expanded=True):
            sample = question_import_example()
            st.code(json.dumps(sample, ensure_ascii=False, indent=2), language="json")
            st.caption(
                "JSON 最上層需有 questions 陣列；單選題使用 single_choice，"
                "填空題使用 fill_blank，作文使用 essay。"
            )
        import_bank = st.selectbox(
            "匯入至題庫",
            [None, *[bank["id"] for bank in banks]],
            format_func=lambda value: (
                "不指定題庫"
                if value is None
                else bank_names.get(value, str(value))
            ),
            key="import_bank",
        )
        st.download_button(
            "下載 JSON 範例",
            json.dumps(question_import_example(), ensure_ascii=False, indent=2),
            file_name="ccss-question-bank-example.json",
            mime="application/json",
        )
        st.download_button(
            "下載 CSV 範例",
            "category,scope,grade_level,difficulty,type,prompt,options,answer,accepted_answers,explanation,rubric,min_word_count,points\nGrammar,Present simple,Grade 7,初級,choice,Choose the correct form.,go|goes,goes,,Third-person singular.,,,5\n",
            file_name="ccss-question-bank-example.csv",
            mime="text/csv",
        )
        upload = st.file_uploader(
            "選擇題目匯入檔案",
            type=["json", "csv", "xlsx"],
            key="question_import_file",
        )
        if upload is not None:
            try:
                if upload.size > 2_000_000:
                    raise ValueError("考卷檔案不可超過 2 MB。")
                import_payload = db.normalize_question_import(
                    parse_question_import(
                        upload.name, upload.getvalue()
                    )
                )
                imported_questions = import_payload["questions"]
                if not 1 <= len(imported_questions) <= 500:
                    raise ValueError("每份考卷需包含 1 至 500 題。")
                if import_payload["title"]:
                    st.markdown(f"**考卷：** {import_payload['title']}")
                if import_payload["category"] or import_payload["level"]:
                    st.caption(
                        f"分類：{import_payload['category'] or '未指定'} · "
                        f"程度：{import_payload['level'] or '未指定'}"
                    )
                validation_errors = []
                for index, question in enumerate(imported_questions, 1):
                    try:
                        db.validate_question(question)
                    except (TypeError, ValueError, AttributeError) as exc:
                        validation_errors.append(f"第 {index} 題：{exc}")
                if validation_errors:
                    st.error("考卷預覽發現格式錯誤，請修正後再匯入。")
                    st.dataframe(
                        [{"錯誤": error} for error in validation_errors],
                        hide_index=True,
                        width="stretch",
                    )
                else:
                    st.success(f"預覽成功：共 {len(imported_questions)} 題，格式均正確。")
                    st.dataframe(
                        [
                            {
                                "題號": question.get("id", index),
                                "類別": question["category"],
                                "範圍": question.get("scope", ""),
                                "年級": question.get("grade_level", ""),
                                "難度": question["difficulty"],
                                "題型": question.get("type", "choice"),
                                "題目": question["prompt"],
                                "配分": question["points"],
                            }
                            for index, question in enumerate(imported_questions, 1)
                        ],
                        hide_index=True,
                        width="stretch",
                    )
                    if st.button("確認匯入整份考卷", type="primary"):
                        destination_bank = import_bank
                        if destination_bank is None and import_payload["title"]:
                            matching_bank = next(
                                (
                                    bank
                                    for bank in banks
                                    if bank["name"].casefold()
                                    == import_payload["title"].casefold()
                                ),
                                None,
                            )
                            if matching_bank:
                                destination_bank = matching_bank["id"]
                            else:
                                destination_bank = db.create_question_bank(
                                    teacher_id,
                                    import_payload["title"],
                                    "由考卷 JSON 匯入建立",
                                )
                        count = db.import_questions(
                            teacher_id,
                            {
                                **import_payload,
                                "questions": imported_questions,
                            },
                            bank_id=destination_bank,
                        )
                        st.success(f"成功匯入 {count} 題。")
                        st.rerun()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
                st.error(f"考卷讀取或匯入失敗：{exc}")
    with browse_tab:
        search = st.text_input("搜尋題幹、類別或範圍", key="teacher_question_search")
        bank_filter = st.selectbox(
            "篩選題庫",
            [None, *[bank["id"] for bank in banks]],
            format_func=lambda value: (
                "全部題庫"
                if value is None
                else bank_names.get(value, str(value))
            ),
            key="teacher_question_bank_filter",
        )
        questions = db.list_questions(
            bank_id=bank_filter,
            include_drafts=True,
            teacher_id=teacher_id,
            search=search,
        )
        if questions:
            st.dataframe(
                [
                    {
                        "類別": question["category"],
                        "範圍": question["scope"],
                        "年級": question["grade_level"],
                        "題庫": bank_names.get(question["bank_id"], "未分類"),
                        "難度": question["difficulty"],
                        "題型": question["question_type"],
                        "題目": question["prompt"],
                        "分數": question["points"],
                        "狀態": "已發布" if question["is_published"] else "草稿",
                    }
                    for question in questions
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("題庫目前是空的。")
        if questions:
            question_id = st.selectbox(
                "預覽／管理題目",
                [question["id"] for question in questions],
                format_func=lambda value: next(
                    question["prompt"][:90]
                    for question in questions
                    if question["id"] == value
                ),
            )
            preview = next(q for q in questions if q["id"] == question_id)
            with st.container(border=True):
                st.caption(
                    f"{preview['category']} · {preview['scope']} · "
                    f"{preview['grade_level']} · {preview['difficulty']} · "
                    f"{preview['points']} 分"
                )
                st.markdown(f"### {preview['prompt']}")
                if preview["question_type"] == "choice":
                    for option in preview["options"]:
                        st.write(f"○ {option}")
                elif preview["question_type"] == "fill_blank":
                    st.text_input(
                        "學生作答預覽",
                        key=f"preview_question_{question_id}",
                        disabled=True,
                    )
                if st.button(
                    "發布題目" if not preview["is_published"] else "設為草稿",
                    key=f"toggle_question_{question_id}",
                ):
                    db.update_question(
                        teacher_id,
                        question_id,
                        {**preview, "is_published": not preview["is_published"]},
                    )
                    st.rerun()
                if st.button("刪除／封存題目", key=f"delete_question_{question_id}"):
                    db.delete_question(teacher_id, question_id)
                    st.success("題目已刪除；若曾有作答紀錄則會封存保留成績。")
                    st.rerun()


def show_essay_grading():
    st.subheader("作文批改")
    attempts = db.list_essay_attempts(st.session_state.user["id"])
    if not attempts:
        st.info("目前沒有待批改的作文。")
        return
    for attempt in attempts:
        status = (
            f"{attempt['earned_points']} / {attempt['points']} 分"
            if attempt["earned_points"] is not None
            else {
                "revision_requested": "等待學生修改",
                "submitted": "待批改",
            }.get(attempt["essay_status"], "待批改")
        )
        with st.expander(
            f"{attempt['display_name']} · {attempt['category']} · {status} · {attempt['submitted_at']}"
        ):
            st.markdown(f"**題目：** {attempt['prompt']}")
            if attempt["rubric"]:
                st.info(f"評分規準：{attempt['rubric']}")
            word_count = len(re.findall(r"\b[\w'-]+\b", attempt["response"]))
            st.caption(
                f"版本 {attempt['version_number']} · {word_count} 個英文單字 · "
                f"最低要求 {attempt['min_word_count']} 字"
            )
            st.write(attempt["response"])
            for comment in db.essay_comments(
                attempt["attempt_id"],
                attempt["version_number"],
                st.session_state.user["id"],
            ):
                st.warning(
                    f"原文：{comment['quoted_text']}\n\n批註：{comment['comment']}"
                )
            with st.form(f"grade_{attempt['attempt_id']}"):
                score = st.number_input(
                    f"分數（0–{attempt['points']}）",
                    min_value=0,
                    max_value=attempt["points"],
                    value=attempt["earned_points"] or 0,
                    key=f"grade_score_{attempt['attempt_id']}",
                )
                feedback = st.text_area(
                    "評語",
                    value=attempt["feedback"],
                    key=f"grade_feedback_{attempt['attempt_id']}",
                )
                submitted = st.form_submit_button("儲存批改", type="primary")
            if submitted:
                try:
                    db.grade_essay(
                        st.session_state.user["id"],
                        attempt["attempt_id"],
                        int(score),
                        feedback,
                    )
                    st.success("批改已儲存。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            with st.form(f"essay_comment_{attempt['attempt_id']}"):
                quoted_text = st.text_area(
                    "逐句批註：貼上作文中的完整句子",
                    max_chars=1000,
                    key=f"essay_quote_{attempt['attempt_id']}",
                )
                comment = st.text_area(
                    "修改建議",
                    max_chars=2000,
                    key=f"essay_comment_text_{attempt['attempt_id']}",
                )
                comment_submitted = st.form_submit_button("新增逐句批註")
            if comment_submitted:
                try:
                    db.add_essay_comment(
                        st.session_state.user["id"],
                        attempt["attempt_id"],
                        attempt["version_number"],
                        quoted_text,
                        comment,
                    )
                    st.success("逐句批註已儲存。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            with st.form(f"essay_revision_{attempt['attempt_id']}"):
                revision_feedback = st.text_area(
                    "要求修改的整體建議",
                    max_chars=5000,
                    key=f"essay_revision_feedback_{attempt['attempt_id']}",
                )
                revision_submitted = st.form_submit_button("退回修改")
            if revision_submitted:
                try:
                    db.request_essay_revision(
                        st.session_state.user["id"],
                        attempt["attempt_id"],
                        revision_feedback,
                    )
                    st.success("作文已退回學生修改。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def show_teacher_announcements(user):
    st.subheader("教師公告")
    with st.form("teacher_announcement_form", clear_on_submit=True):
        title = st.text_input("公告標題", max_chars=120)
        content = st.text_area("公告內容", max_chars=5000)
        submitted = st.form_submit_button("發布公告", type="primary")
    if submitted:
        try:
            db.create_announcement(user["id"], title, content)
            st.success("公告已發布。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    announcements = db.list_announcements(20)
    for announcement in announcements:
        with st.container(border=True):
            st.markdown(f"**{announcement['title']}** · {announcement['published_at']}")
            st.write(announcement["content"])
            with st.expander("編輯或刪除此公告"):
                with st.form(f"edit_announcement_{announcement['id']}"):
                    updated_title = st.text_input(
                        "標題",
                        value=announcement["title"],
                        key=f"announcement_title_{announcement['id']}",
                    )
                    updated_content = st.text_area(
                        "內容",
                        value=announcement["content"],
                        key=f"announcement_content_{announcement['id']}",
                    )
                    save_announcement = st.form_submit_button("儲存修改")
                if save_announcement:
                    try:
                        db.update_announcement(
                            user["id"],
                            announcement["id"],
                            updated_title,
                            updated_content,
                        )
                        st.success("公告已更新。")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
                if st.button(
                    "刪除公告", key=f"delete_announcement_{announcement['id']}"
                ):
                    db.delete_announcement(user["id"], announcement["id"])
                    st.rerun()


def show_teacher_assignments(user):
    st.subheader("作業指派與成績")
    students = db.student_overview(user["id"])
    questions = db.list_questions(include_drafts=True, teacher_id=user["id"])
    if not students:
        st.info("請先建立啟用中的學生帳號。")
    if not questions:
        st.info("請先新增題目。")
    if students and questions:
        with st.form("create_assignment_set"):
            title = st.text_input("作業名稱", placeholder="Unit 1 Grammar Quiz")
            description = st.text_area("作業說明")
            all_students = st.checkbox("指派給所有啟用中的學生")
            student_ids = st.multiselect(
                "指定學生",
                [student["id"] for student in students if student["is_active"]],
                format_func=lambda value: next(
                    f"{student['username']} · {student['display_name']}"
                    for student in students
                    if student["id"] == value
                ),
                disabled=all_students,
            )
            question_ids = st.multiselect(
                "選擇作業題目",
                [question["id"] for question in questions],
                format_func=lambda value: next(
                    f"{question['category']} · {question['difficulty']} · "
                    f"{question['prompt'][:70]} · {question['points']} 分"
                    for question in questions
                    if question["id"] == value
                ),
            )
            schedule = st.checkbox("設定開始與截止時間")
            starts_at = due_at = None
            if schedule:
                starts_date = st.date_input("開始日期", value=date.today())
                due_date = st.date_input("截止日期", value=date.today())
                starts_time = st.time_input("開始時間", value=time(0, 0))
                due_time = st.time_input("截止時間", value=time(23, 59))
                starts_at = datetime.combine(starts_date, starts_time).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                due_at = datetime.combine(due_date, due_time).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            timed = st.checkbox("設定作答時間限制")
            time_limit = (
                st.number_input("作答時間（分鐘）", min_value=1, max_value=600, value=30)
                if timed
                else None
            )
            allow_retake = st.checkbox("允許學生重新作答")
            show_answers = st.checkbox("交卷後顯示答案與解析", value=True)
            publish = st.checkbox("立即發布作業", value=True)
            submitted = st.form_submit_button("建立作業", type="primary")
        if submitted:
            try:
                assignment_id = db.create_assignment(
                    user["id"],
                    title,
                    description,
                    question_ids,
                    student_ids=student_ids,
                    all_students=all_students,
                    starts_at=starts_at,
                    due_at=due_at,
                    time_limit_minutes=int(time_limit) if time_limit else None,
                    allow_retake=allow_retake,
                    show_answers=show_answers,
                    status="published" if publish else "draft",
                )
                st.success(f"作業已建立（編號 {assignment_id}）。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    assignments = db.list_assignments_for_teacher(user["id"])
    if not assignments:
        st.info("目前還沒有作業。")
        return
    st.markdown("#### 已建立作業")
    st.dataframe(
        [
            {
                "作業": assignment["title"],
                "狀態": assignment["status"],
                "學生": assignment["student_count"],
                "題目": assignment["question_count"],
                "已提交": assignment["submitted_count"],
                "平均比例": (
                    f"{assignment['average_ratio']:.0%}"
                    if assignment["average_ratio"] is not None
                    else "—"
                ),
                "截止": assignment["due_at"] or "未設定",
            }
            for assignment in assignments
        ],
        hide_index=True,
        width="stretch",
    )
    selected_assignment = st.selectbox(
        "檢視作業提交",
        [assignment["id"] for assignment in assignments],
        format_func=lambda value: next(
            assignment["title"]
            for assignment in assignments
            if assignment["id"] == value
        ),
    )
    selected = next(a for a in assignments if a["id"] == selected_assignment)
    with st.expander("延長或調整作業期限"):
        current_start = (
            datetime.fromisoformat(selected["starts_at"])
            if selected["starts_at"]
            else datetime.combine(date.today(), time(0, 0))
        )
        current_due = (
            datetime.fromisoformat(selected["due_at"])
            if selected["due_at"]
            else datetime.combine(date.today(), time(23, 59))
        )
        with st.form(f"assignment_schedule_{selected_assignment}"):
            has_start = st.checkbox(
                "設定開始時間",
                value=bool(selected["starts_at"]),
            )
            start_date = st.date_input(
                "開始日期",
                value=current_start.date(),
                key=f"assignment_start_date_{selected_assignment}",
            )
            start_time = st.time_input(
                "開始時間",
                value=current_start.time(),
                key=f"assignment_start_time_{selected_assignment}",
            )
            has_due = st.checkbox(
                "設定截止時間",
                value=bool(selected["due_at"]),
            )
            due_date = st.date_input(
                "截止日期",
                value=current_due.date(),
                key=f"assignment_due_date_{selected_assignment}",
            )
            due_time = st.time_input(
                "截止時間",
                value=current_due.time(),
                key=f"assignment_due_time_{selected_assignment}",
            )
            schedule_submitted = st.form_submit_button("儲存作業期限")
        if schedule_submitted:
            starts_at = (
                datetime.combine(start_date, start_time).strftime("%Y-%m-%d %H:%M:%S")
                if has_start
                else None
            )
            due_at = (
                datetime.combine(due_date, due_time).strftime("%Y-%m-%d %H:%M:%S")
                if has_due
                else None
            )
            try:
                db.update_assignment_deadline(
                    user["id"], selected_assignment, starts_at, due_at
                )
                st.success("作業期限已更新。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    status_options = ["published", "draft", "closed", "cancelled"]
    status_col, export_col = st.columns(2)
    new_status = status_col.selectbox(
        "更新作業狀態",
        status_options,
        index=status_options.index(selected["status"]),
        key=f"assignment_status_{selected_assignment}",
    )
    if status_col.button("儲存狀態", key=f"save_assignment_{selected_assignment}"):
        try:
            db.update_assignment_status(user["id"], selected_assignment, new_status)
            st.success("作業狀態已更新。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    export_col.download_button(
        "匯出作業成績 CSV",
        db.export_assignment_scores(user["id"], selected_assignment),
        file_name=f"ccss-assignment-{selected_assignment}-scores.csv",
        mime="text/csv",
    )
    submissions = db.assignment_submissions(user["id"], selected_assignment)
    statistics = db.assignment_statistics(user["id"], selected_assignment)
    if statistics["submission_count"]:
        metric_cols = st.columns(4)
        metric_cols[0].metric("已提交學生", statistics["submission_count"])
        metric_cols[1].metric(
            "班級平均分",
            f"{statistics['average_score']:.1f}",
        )
        metric_cols[2].metric("最高分", statistics["highest_score"])
        metric_cols[3].metric("最低分", statistics["lowest_score"])
    with st.expander("逐題答題統計"):
        st.dataframe(
            [
                {
                    "題目": question["prompt"],
                    "題型": question["question_type"],
                    "作答學生": question["response_count"],
                    "答對人數": (
                        question["correct_count"]
                        if question["correct_rate"] is not None
                        else "待批改／不適用"
                    ),
                    "自動批改題答對率": (
                        f"{question['correct_rate']:.0%}"
                        if question["correct_rate"] is not None
                        else "—"
                    ),
                }
                for question in statistics["questions"]
            ],
            hide_index=True,
            width="stretch",
        )
    if submissions:
        st.dataframe(
            [
                {
                    "學生": submission["display_name"],
                    "帳號": submission["username"],
                    "作答題數": submission["answered_count"],
                    "分數": (
                        f"{submission['score'] or 0} / {submission['max_score'] or 0}"
                    ),
                    "提交時間": submission["submitted_at"] or "尚未提交",
                }
                for submission in submissions
            ],
            hide_index=True,
            width="stretch",
        )
        selected_student = st.selectbox(
            "查看學生逐題作答",
            [submission["student_id"] for submission in submissions],
            format_func=lambda value: next(
                submission["display_name"]
                for submission in submissions
                if submission["student_id"] == value
            ),
        )
        attempts = db.assignment_question_attempts(
            user["id"], selected_assignment, selected_student
        )
        for attempt in attempts:
            with st.expander(
                f"{attempt['prompt'][:80]} · "
                f"{attempt['earned_points'] if attempt['earned_points'] is not None else '待批改'} / {attempt['points']}"
            ):
                st.write(f"學生答案：{attempt['response'] or '尚未作答'}")
                if attempt["response"] and attempt["question_type"] != "essay":
                    st.write(f"正確答案：{attempt['answer']}")
                if attempt["response"]:
                    with st.form(f"adjust_score_{selected_assignment}_{attempt['question_id']}"):
                        corrected_score = st.number_input(
                            "調整分數",
                            min_value=0,
                            max_value=attempt["points"],
                            value=attempt["earned_points"] or 0,
                        )
                        reason = st.text_input("評分調整原因")
                        adjust = st.form_submit_button("儲存評分調整")
                    if adjust:
                        try:
                            db.adjust_attempt_score(
                                user["id"],
                                attempt["attempt_id"],
                                int(corrected_score),
                                reason,
                            )
                            st.success("分數已更新，操作已記錄。")
                            st.rerun()
                        except ValueError as exc:
                            st.error(f"無法修改分數：{exc}")


def show_teacher_audit(user):
    st.subheader("重要操作紀錄")
    rows = db.audit_log(user["id"])
    if not rows:
        st.info("目前還沒有教師操作紀錄。")
        return
    st.dataframe(
        [
            {
                "操作者": row["actor"] or "已刪除帳號",
                "操作": row["action"],
                "資料類型": row["entity_type"],
                "資料編號": row["entity_id"],
                "詳細資料": row["details"],
                "時間": row["created_at"],
            }
            for row in rows
        ],
        hide_index=True,
        width="stretch",
    )


def show_note_reports(user):
    st.subheader("便利貼檢舉")
    reports = db.list_note_reports(user["id"])
    if not reports:
        st.info("目前沒有便利貼檢舉。")
        return
    for report in reports:
        with st.container(border=True):
            st.markdown(
                f"**{report['note_author']}** 的留言 · 檢舉人 {report['reporter_name']}"
            )
            st.write(report["note_content"])
            st.caption(f"原因：{report['reason']} · {report['created_at']} · {report['status']}")
            if report["status"] == "pending" and st.button(
                "標記已處理", key=f"resolve_report_{report['id']}"
            ):
                db.resolve_note_report(user["id"], report["id"])
                st.success("檢舉已標記為處理完成。")
                st.rerun()
            if st.button("刪除違規便利貼", key=f"remove_reported_note_{report['id']}"):
                db.delete_note_as_teacher(user["id"], report["note_id"])
                db.resolve_note_report(user["id"], report["id"])
                st.success("違規便利貼已刪除，檢舉已處理。")
                st.rerun()


def show_question_reports(user):
    st.subheader("學生題目回報")
    reports = db.list_question_reports(user["id"])
    if not reports:
        st.info("目前沒有題目回報。")
        return
    for report in reports:
        with st.container(border=True):
            st.markdown(f"**{report['category']} · {report['difficulty']}**")
            st.write(report["prompt"])
            if report["options"]:
                st.markdown("**選項：**")
                for option_number, option in enumerate(
                    report["options"], start=1
                ):
                    st.write(f"{option_number}. {option}")
            st.caption(
                f"回報人：{report['reporter_name']} · {report['created_at']} · "
                f"{report['status']}"
            )
            st.write(f"回報內容：{report['reason']}")
            if report["status"] == "pending" and st.button(
                "標記已處理", key=f"resolve_question_report_{report['id']}"
            ):
                db.resolve_question_report(user["id"], report["id"])
                st.success("題目回報已標記為處理完成。")
                st.rerun()


def show_teacher(user):
    tabs = st.tabs(
        ["學生管理", "題庫管理", "作業與成績", "作文批改", "公告",
         "留言檢舉", "題目回報", "操作紀錄", "個人資料"]
    )
    with tabs[0]:
        show_teacher_students(user)
    with tabs[1]:
        show_question_bank()
    with tabs[2]:
        show_teacher_assignments(user)
    with tabs[3]:
        show_essay_grading()
    with tabs[4]:
        show_teacher_announcements(user)
    with tabs[5]:
        show_note_reports(user)
    with tabs[6]:
        show_question_reports(user)
    with tabs[7]:
        show_teacher_audit(user)
    with tabs[8]:
        show_account_settings(user)


def main():
    reset_token = st.query_params.get("reset")
    if reset_token:
        show_password_reset(reset_token)
        return
    user = st.session_state.get("user")
    if user is None or db.get_user(user["id"]) is None:
        st.session_state.pop("user", None)
        show_login()
        return
    user = db.get_user(user["id"])
    st.session_state.user = user
    show_header()
    show_sidebar(user)
    if user["must_change_password"]:
        st.warning("為保護帳號安全，請先設定新密碼後再使用其他功能。")
        show_account_settings(user)
        return
    if user["role"] == "teacher":
        show_teacher(user)
    else:
        dashboard, practice, history, essays, notes, account = st.tabs(
            [
                "🏠 學生首頁",
                "✍️ 練習題",
                "📚 我的作答",
                "📝 作文中心",
                "💬 便利貼",
                "⚙️ 帳號設定",
            ]
        )
        with dashboard:
            show_student_dashboard(user)
        with practice:
            show_quiz(user)
        with history:
            show_student_attempts(user)
        with essays:
            show_student_essays(user)
        with notes:
            show_notes(user)
        with account:
            show_account_settings(user)


main()
