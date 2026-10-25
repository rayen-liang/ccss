const app = document.querySelector("#app");
const state = { user: null, page: "首頁", activeQuiz: null, notice: "", noticeType: "", wheelRequest: null };
const studentPages = ["首頁", "練習題", "我的作答", "作文中心", "便利貼", "帳號設定"];
const teacherPages = ["學生管理", "題庫管理", "作業指派", "作文批改", "公告與檢舉", "操作紀錄", "帳號設定"];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const displayDate = (value) => value ? new Date(value).toLocaleString() : "—";
const notice = (text, type = "") => { state.notice = text; state.noticeType = type; };
const showNotice = () => state.notice ? `<div class="notice ${state.noticeType}">${esc(state.notice)}</div>` : "";

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || `請求失敗（${response.status}）`);
  return result;
}

function renderAuth(mode = "login") {
  const isRegister = mode === "register";
  const isReset = mode === "reset";
  const isForgot = mode === "forgot";
  app.innerHTML = `<main class="auth-wrap"><section class="auth-card">
    <div class="brand">📘 CCSS</div><p class="tagline">Connect, Challenge, Succeed in English</p>
    ${mode === "login" ? `<p class="muted">教師請使用教師帳號登入；學生請使用學生帳號登入。</p>` : ""}
    ${!isReset && !isForgot ? `<div class="switcher" role="tablist">
      <button data-auth="login" class="${mode === "login" ? "active" : ""}">登入</button>
      <button data-auth="register" class="${isRegister ? "active" : ""}">建立學生帳號</button>
    </div>` : ""}
    ${showNotice()}
    <form class="auth-pane" data-form="${mode}">
      ${isRegister ? `<label>電子郵件<input name="email" type="email" required autocomplete="email"></label>` : ""}
      ${mode === "login" || isRegister ? `<label>帳號<input name="username" required minlength="3" maxlength="32" autocomplete="username"></label>` : ""}
      ${isForgot ? `<label>註冊電子郵件<input name="email" type="email" required autocomplete="email"></label>` : ""}
      ${isReset ? `<input name="token" type="hidden" value="${esc(new URLSearchParams(location.search).get("reset"))}"><label>新密碼<input name="password" type="password" required minlength="8" autocomplete="new-password"></label><label>確認新密碼<input name="confirm" type="password" required minlength="8"></label>` : ""}
      ${mode === "login" || isRegister ? `<label>密碼<input name="password" type="password" required minlength="8" autocomplete="${mode === "login" ? "current-password" : "new-password"}"></label>` : ""}
      ${isRegister ? `<label>確認密碼<input name="confirm" type="password" required minlength="8" autocomplete="new-password"></label>` : ""}
      <button class="primary">${mode === "login" ? "登入" : isRegister ? "建立帳號並登入" : isForgot ? "寄送重設連結" : "重設密碼"}</button>
      ${mode === "login" ? `<button type="button" class="secondary" data-auth="forgot">忘記密碼</button>` : ""}
      ${isForgot ? `<button type="button" class="secondary" data-auth="login">返回登入</button>` : ""}
    </form></section></main>`;
}

function navPages() {
  if (state.user.must_change_password) return ["帳號設定"];
  return state.user.role === "teacher" ? teacherPages : studentPages;
}
function renderShell() {
  const pages = navPages();
  if (!pages.includes(state.page)) state.page = pages[0];
  app.innerHTML = `<div class="shell">
    <aside class="sidebar"><div class="brand">📘 CCSS</div>
      <div class="user-chip"><strong>${esc(state.user.display_name)}</strong><small>${state.user.role === "teacher" ? "教師" : "學生"} · @${esc(state.user.username)}</small></div>
      <nav class="nav">${pages.map(page => `<button data-page="${esc(page)}" class="${state.page === page ? "active" : ""}">${esc(page)}</button>`).join("")}</nav>
      <div class="sidebar-footer"><button class="secondary" data-action="logout">安全登出</button></div>
    </aside>
    <main class="content"><div id="page-content"></div></main>
  </div>`;
  if (state.user.must_change_password) {
    state.page = "帳號設定";
    renderPage();
    notice("首次登入請先修改密碼，才能使用其他功能。");
    return;
  }
  renderPage();
}

function title(text, action = "") {
  return `<div class="page-title"><h1>${esc(text)}</h1>${action}</div>`;
}
function card(content, className = "") { return `<section class="card ${className}">${content}</section>`; }
function table(headers, rows) {
  return rows.length ? `<div class="table-wrap"><table><thead><tr>${headers.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(x => `<td>${x}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="empty">目前沒有資料</div>`;
}
function studentSelection(students, name = "student_ids") {
  return `<div class="inline-checks">${students.map(student => `<label><input type="checkbox" name="${name}" value="${student.id}"> ${esc(student.display_name)} (@${esc(student.username)})</label>`).join("")}</div>`;
}

async function renderPage() {
  const content = document.querySelector("#page-content");
  if (!content) return;
  content.innerHTML = `${title(state.page)}${showNotice()}<div class="loading">載入中…</div>`;
  try {
    const html = state.user.role === "teacher" ? await renderTeacherPage() : await renderStudentPage();
    content.innerHTML = `${title(state.page)}${showNotice()}${html}`;
  } catch (error) {
    content.innerHTML = `${title(state.page)}<div class="notice error">${esc(error.message)}</div><button class="secondary" data-action="refresh">重新載入</button>`;
  }
  state.notice = "";
}

async function renderStudentPage() {
  if (state.page === "首頁") {
    const data = await api("/student/dashboard");
    const leaderRows = data.leaderboard.map(item => [
      `<strong>${item.rank <= 3 ? ["🥇","🥈","🥉"][item.rank - 1] : `#${item.rank}`}</strong>`,
      esc(item.display_name), `${item.score} 分`,
    ]);
    return `<div class="grid metrics">
      ${card(`<span class="muted">目前積分</span><div class="metric">${data.score}</div>`)}
      ${card(`<span class="muted">完成題數</span><div class="metric">${data.completed}</div>`)}
      ${card(`<span class="muted">答題正確率</span><div class="metric">${data.accuracy}%</div>`)}
      ${card(`<span class="muted">目前排名</span><div class="metric">${data.rank ? `#${data.rank}` : "—"}</div>`)}
    </div><div class="grid two" style="margin-top:1rem">
      ${card(`<h2>🏆 積分排行榜</h2>${table(["名次", "學生", "積分"], leaderRows)}`)}
      ${card(`<h2>📣 公佈欄</h2>${data.announcements.length ? data.announcements.map(item => `<article class="question"><h3>${esc(item.title)}</h3><p>${esc(item.content)}</p><small class="muted">${esc(item.teacher_name)} · ${displayDate(item.published_at)}</small></article>`).join("") : `<div class="empty">目前沒有公告</div>`}`)}
      ${card(`<h2>🕘 最近作答</h2>${table(["題目", "類別", "成績", "時間"], data.recent.map(item => [esc(item.prompt), esc(item.category), item.earned_points == null ? "作文待批改" : `${item.earned_points}/${item.points}`, displayDate(item.submitted_at)]))}`)}
      ${card(`<h2>快速開始</h2><p>完成新題目可以累積學習積分。已提交的題目會從一般練習中隱藏。</p><button class="primary" data-page="練習題">開始練習</button><div class="divider"></div><h3>🎡 積分轉盤</h3><p class="muted">一般轉盤每次 150 分；第 20 天起每 10 天獲得免費轉盤。</p><button class="secondary" data-action="wheel">轉一次</button><div id="wheel-result"></div>`)}
    </div>`;
  }
  if (state.page === "練習題") {
    if (state.activeQuiz?.submitted) return renderQuizResult(state.activeQuiz.result);
    if (state.activeQuiz) return renderQuiz();
    const [questions, assignments] = await Promise.all([api("/questions"), api("/student/assignments")]);
    const categories = [...new Set(questions.map(q => q.category))];
    const dueAssignments = assignments.filter(a => !a.submitted_count || a.allow_retake);
    return `<div class="grid two">
      ${card(`<h2>開始新練習</h2><form class="stack" data-form="start-practice">
        <label>題目類別<select name="category"><option value="">全部類別</option>${categories.map(x => `<option>${esc(x)}</option>`).join("")}</select></label>
        <label>難度<select name="difficulty"><option value="">全部難度</option>${["初級","中級","中高級","高級"].map(x => `<option>${x}</option>`).join("")}</select></label>
        <label>題數<select name="count">${[5,10,15,20].map(x => `<option value="${x}" ${x===10?"selected":""}>${x} 題</option>`).join("")}</select></label>
        <button class="primary">抽取題目</button><small class="muted">可練習新題目：${questions.length} 題</small>
      </form>`)}
      ${card(`<h2>教師指派作業</h2>${dueAssignments.length ? dueAssignments.map(item => `<article class="question"><h3>${esc(item.title)}</h3><p>${esc(item.description || "教師指派練習")}</p><small class="muted">${item.question_count} 題 · 截止 ${displayDate(item.due_at)}</small><div style="margin-top:.7rem"><button class="primary" data-assignment="${item.id}">開始作答</button></div></article>`).join("") : `<div class="empty">目前沒有待完成作業</div>`}`)}
    </div>`;
  }
  if (state.page === "我的作答") {
    const attempts = await api("/student/attempts");
    return table(["題目", "類別", "我的答案", "分數", "提交時間", "解析"], attempts.map(item => [
      esc(item.prompt), esc(item.category), esc(item.response), item.earned_points == null ? "待批改" : `${item.earned_points}/${item.points}`,
      displayDate(item.submitted_at),
      `${item.explanation ? `<details><summary>查看解析</summary><p>選項：${esc((item.options || []).join("、"))}</p><p>正確答案：${esc(item.answer || (item.accepted_answers || []).join(" / "))}</p><p>${esc(item.explanation)}</p></details>` : "教師尚未開放"} <button class="secondary" data-report-question="${item.question_id}">回報題目</button>`,
    ]));
  }
  if (state.page === "作文中心") {
    const [essays, questions] = await Promise.all([api("/student/essays"), api("/questions")]);
    const submittedIds = new Set(essays.map(e => e.question_id));
    const prompts = questions.filter(q => q.question_type === "essay" && !submittedIds.has(q.id));
    const drafts = await Promise.all(prompts.map(q => api(`/student/essays/${q.id}/draft`)));
    return `<div class="stack">${prompts.map((q,index) => `<article class="card"><h2>${esc(q.prompt)}</h2><p class="muted">${esc(q.category)} · ${q.points} 分 · 最低 ${q.min_word_count} 字</p>${q.rubric ? `<p><strong>評分規準：</strong>${esc(q.rubric)}</p>` : ""}
      <form class="stack" data-form="submit-essay" data-question="${q.id}">
      <textarea class="essay-draft" data-question="${q.id}" name="content" maxlength="20000" placeholder="在此撰寫英文作文…" required>${esc(drafts[index].content || "")}</textarea>
      <small class="muted" data-draft-status="${q.id}">草稿會自動儲存</small><button class="primary">提交作文</button></form></article>`).join("")}
      ${essays.map(e => `<article class="card"><h3>${e.revision_requested ? "教師要求修改" : "已提交作文"} · ${esc(e.prompt)}</h3><p>${esc(e.response)}</p><p>分數：${e.earned_points == null ? "等待批改" : `${e.earned_points}/${e.points}`}</p>${e.feedback ? `<p><strong>教師評語：</strong>${esc(e.feedback)}</p>` : ""}${(e.comments||[]).map(c=>`<blockquote>${esc(c.quoted_text)}<br>${esc(c.comment)}</blockquote>`).join("")}${(e.versions||[]).length>1?`<details><summary>作文版本歷史</summary>${e.versions.map(v=>`<article class="answer-box"><strong>第 ${v.version} 版 · ${esc(v.status)}</strong><p>${esc(v.content)}</p></article>`).join("")}</details>`:""}<small class="muted">${displayDate(e.submitted_at)}</small>${e.revision_requested ? `<form class="stack" data-form="submit-essay" data-question="${e.question_id}" data-assignment="${e.assignment_id || ""}"><textarea class="essay-draft" data-question="${e.question_id}" name="content" required>${esc(e.response)}</textarea><button class="primary">提交修訂稿</button></form>` : ""}</article>`).join("")}
      ${!prompts.length && !essays.length ? `<div class="empty">目前沒有作文題目或提交紀錄</div>` : ""}</div>`;
  }
  if (state.page === "便利貼") {
    const notes = await api("/student/notes");
    return `<div class="grid two"><section class="card"><h2>新增便利貼</h2><form class="stack" data-form="new-note"><textarea name="content" maxlength="2000" required placeholder="分享學習心得或提問…"></textarea><button class="primary">發布</button></form></section>
      <section class="stack">${notes.map(n => `<article class="card"><div class="spread"><strong>${esc(n.author_name)}</strong><small class="muted">${displayDate(n.created_at)}</small></div><p>${esc(n.content)}</p>${n.author_id===state.user.id?`<div class="row"><button class="secondary" data-edit-note="${n.id}" data-content="${esc(n.content)}">編輯</button><button class="danger-button" data-delete-own-note="${n.id}">刪除</button></div>`:""}<form class="row" data-form="reply-note" data-note="${n.id}"><input name="content" maxlength="1000" required placeholder="回覆這則便利貼"><button class="secondary">回覆</button></form><button class="danger-button" data-report="${n.id}">檢舉</button>${(n.replies||[]).map(r=>`<p class="answer-box"><strong>${esc(r.author_name)}：</strong>${esc(r.content)}</p>`).join("")}</article>`).join("") || `<div class="empty">還沒有便利貼，來分享第一則吧！</div>`}</section></div>`;
  }
  if (state.page === "帳號設定") return accountPage();
  return `<div class="empty">尚未開放此頁面</div>`;
}

function renderQuiz() {
  const quiz = state.activeQuiz;
  return `<section class="card"><div class="spread"><div><h2>本次練習</h2><p class="muted">提交後答案將鎖定，不能修改。</p></div><span class="badge">${quiz.questions.length} 題</span></div>
    <form data-form="submit-quiz">${quiz.questions.map((q, index) => `<article class="question" data-q="${q.id}"><h3>${index+1}. ${esc(q.prompt)} <span class="badge">${q.points} 分</span></h3>${q.question_type === "choice" ? (q.options||[]).map(option=>`<label class="option"><input type="radio" name="q-${q.id}" value="${esc(option)}" required><span>${esc(option)}</span></label>`).join("") : q.question_type === "essay" ? `<textarea name="q-${q.id}" required placeholder="撰寫作文…"></textarea>` : `<input name="q-${q.id}" required placeholder="輸入答案">`}</article>`).join("")}<button class="primary">確認並提交</button></form></section>`;
}
function renderQuizResult(result) {
  return `<section class="card"><h2>本次成績：${result.score} / ${result.max_score}</h2><p>答對 ${result.correct_count} 題，共 ${result.question_count} 題。答案已鎖定，請檢視解析。</p>
    ${result.results.map((item,index)=>`<article class="question"><h3>${index+1}. ${esc(item.question.prompt)}</h3><p>選項：${esc((item.question.options||[]).join("、") || (item.question.question_type==="essay"?"作文題":"填空題"))}</p><p>你的答案：<strong>${esc(item.response)}</strong></p>${Object.hasOwn(item.question,"answer")?`<p>正確答案：<strong>${esc(item.question.answer || (item.question.accepted_answers||[]).join(" / "))}</strong></p><div class="answer-box">${esc(item.question.explanation || "沒有提供解析。")}</div>`:`<p class="muted">教師尚未開放正確答案與解析。</p>`}<p>本題得分：${item.earned_points ?? "待批改"}${item.earned_points == null ? "" : `/${item.question.points}`}</p></article>`).join("")}
    <button class="primary" data-action="practice-done">回到練習題</button></section>`;
}
async function accountPage() {
  const history = state.user.must_change_password ? [] : await api("/me/login-history");
  return `<div class="grid two">${card(`<h2>個人資料</h2><form class="stack" data-form="profile"><label>顯示名稱<input name="display_name" value="${esc(state.user.display_name)}" required maxlength="60"></label><label>電子郵件（忘記密碼時使用）<input name="email" type="email" value="${esc(state.user.email || "")}"></label><button class="primary">儲存個人資料</button></form><p class="muted">帳號：${esc(state.user.username)} · 建立時間：${displayDate(state.user.created_at)}</p>${history.length ? `<h3>最近登入紀錄</h3>${table(["結果","時間"],history.map(item=>[item.succeeded?"成功":"失敗",displayDate(item.created_at)]))}` : ""}`)}
  ${card(`<h2>修改密碼</h2><form class="stack" data-form="password"><label>目前密碼<input name="current_password" type="password" required autocomplete="current-password"></label><label>新密碼<input name="new_password" type="password" minlength="8" required autocomplete="new-password"></label><label>確認新密碼<input name="confirm_password" type="password" minlength="8" required></label><button class="primary">更新密碼</button></form>`)}</div>`;
}

async function renderTeacherPage() {
  if (state.page === "帳號設定") return accountPage();
  if (state.page === "學生管理") {
    const [students, classes] = await Promise.all([api("/teacher/students"), api("/teacher/classes")]);
    return `<div class="grid two">${card(`<h2>學生帳號與成績</h2>${table(["帳號", "暱稱", "積分", "正確率", "狀態", "管理"], students.map(s=>[esc(s.username),esc(s.display_name),s.score,`${s.accuracy}%`,s.is_active?"啟用":"停用",`<button class="secondary" data-reset-student="${s.id}">重設密碼</button> <button class="secondary" data-status-student="${s.id}" data-active="${!s.is_active}">${s.is_active?"停用":"啟用"}</button>`]))}<div class="divider"></div><h3>建立學生帳號</h3><form class="stack" data-form="create-student"><label>帳號<input name="username" required minlength="3"></label><label>暱稱<input name="display_name"></label><label>電子郵件<input name="email" type="email"></label><label>初始密碼（至少 8 字元）<input name="password" type="password" minlength="8" required></label><button class="primary">建立帳號</button></form>`)}
      ${card(`<h2>建立班級</h2><form class="stack" data-form="create-class"><label>班級名稱<input name="name" required maxlength="80"></label><p>勾選加入的學生：</p>${studentSelection(students)}<button class="primary">建立班級</button></form><div class="divider"></div><h3>現有班級</h3>${table(["班級","學生數"],classes.map(c=>[esc(c.name),c.student_count]))}`)}</div>`;
  }
  if (state.page === "題庫管理") {
    const [questions, banks] = await Promise.all([api("/teacher/questions"), api("/teacher/banks")]);
    return `<div class="grid two">
      ${card(`<h2>新增／編輯單題</h2><form class="stack" data-form="add-question"><label>類別<input name="category" required value="Grammar"></label><label>難度<select name="difficulty">${["初級","中級","中高級","高級"].map(x=>`<option>${x}</option>`).join("")}</select></label><label>題目類型<select name="type"><option value="choice">單選題</option><option value="fill_blank">填空題</option><option value="essay">作文</option></select></label><label>題目<textarea name="prompt" required></textarea></label><label>選項（以 | 分隔）<input name="options"></label><label>正確答案<input name="answer"></label><label>填空接受答案（以 | 分隔）<input name="accepted_answers"></label><label>配分<input name="points" type="number" min="1" value="5"></label><label>作文最低字數<input name="min_word_count" type="number" min="0" value="0"></label><label>作文評分規準<textarea name="rubric"></textarea></label><label>解析<textarea name="explanation"></textarea></label><label class="inline-checks"><span><input name="is_published" type="checkbox" checked> 立即發布</span></label><button class="primary">新增至題庫</button></form>`)}
      ${card(`<h2>匯入考卷 JSON</h2><p class="muted">支援題庫格式或含 title/category/level/questions 的考卷格式。</p><form class="stack" data-form="import-json"><input name="file" type="file" accept=".json,application/json"><label>或貼上 JSON<textarea name="json" placeholder='{"title":"English Test","questions":[...]}'></textarea></label><button class="primary">驗證並匯入</button></form><div class="divider"></div><h3>PDF 文字擷取</h3><p class="muted">支援 20 頁、3 MB 以內的文字型 PDF。掃描影像請先用 OCR 轉成文字，再手動檢查和編題。</p><form class="stack" data-form="pdf-read"><input name="pdf" type="file" accept="application/pdf" required><button class="secondary">擷取 PDF 文字</button><textarea name="extracted" placeholder="抽取結果會顯示在此，可複製到題目編輯器校對。"></textarea></form><div class="divider"></div><h3>題庫列表</h3>${table(["題庫","題數"],banks.map(b=>[esc(b.name),b.question_count]))}`)}
      ${card(`<h2>現有題目（${questions.length}）</h2>${table(["類別","難度","題型","題目","配分","管理"],questions.map(q=>[esc(q.category),esc(q.difficulty),esc(q.question_type),esc(q.prompt),q.points,`<button class="secondary" data-edit-question="${q.id}">編輯</button> <button class="danger-button" data-archive-question="${q.id}">${q.is_published?"下架":"已下架"}</button>`]))}`)}
    </div>`;
  }
  if (state.page === "作業指派") {
    const [students, questions, assignments] = await Promise.all([api("/teacher/students"), api("/teacher/questions"), api("/teacher/assignments")]);
    return `<div class="grid two">${card(`<h2>建立作業</h2><form class="stack" data-form="create-assignment"><label>作業名稱<input name="title" required></label><label>說明<textarea name="description"></textarea></label><label>截止時間<input name="due_at" type="datetime-local"></label><label>作答時間（分鐘，0 為不限）<input name="time_limit_minutes" type="number" min="0" value="0"></label><p>選擇題目</p><div class="inline-checks">${questions.map(q=>`<label><input type="checkbox" name="question_ids" value="${q.id}">${esc(q.prompt.slice(0,80))}</label>`).join("")}</div><p>指派給學生</p>${studentSelection(students)}<div class="inline-checks"><label><input type="checkbox" name="show_answers" checked>交卷後顯示正解與解析</label><label><input type="checkbox" name="allow_retake">允許重做</label></div><button class="primary">發布作業</button></form>`)}
    ${card(`<h2>已發布作業</h2>${table(["名稱","學生","已提交","截止日期"],assignments.map(a=>[esc(a.title),a.student_count,a.submitted_students,displayDate(a.due_at)]))}`)}</div>`;
  }
  if (state.page === "作文批改") {
    const essays = await api("/teacher/essays");
    return `<div class="stack">${essays.map(e=>`<article class="card"><h2>${esc(e.student_name)} · ${esc(e.prompt)}</h2><p class="muted">提交於 ${displayDate(e.submitted_at)} · 滿分 ${e.max_points}</p><div class="answer-box">${esc(e.response)}</div><p>評分規準：${esc(e.rubric || "未設定")}</p><form class="stack" data-form="grade-essay" data-attempt="${e.attempt_id}"><label>分數<input name="score" type="number" min="0" max="${e.max_points}" value="${e.earned_points ?? ""}" required></label><label>整體評語<textarea name="feedback">${esc(e.feedback || "")}</textarea></label><label>逐句批註原文（需完整包含在作文內）<input name="quoted_text"></label><label>批註<textarea name="comment"></textarea></label><div class="row"><button class="primary">儲存批改</button><button type="button" class="secondary" data-return-essay="${e.attempt_id}">退回修改</button></div></form></article>`).join("") || `<div class="empty">目前沒有待批改作文</div>`}</div>`;
  }
  if (state.page === "公告與檢舉") {
    const [reports, questionReports, announcements] = await Promise.all([api("/teacher/reports"), api("/teacher/question-reports"), api("/teacher/announcements")]);
    return `<div class="grid two">${card(`<h2>發布公告</h2><form class="stack" data-form="announcement"><label>標題<input name="title" required maxlength="160"></label><label>內容<textarea name="content" required></textarea></label><button class="primary">發布公告</button></form><div class="divider"></div><h2>已發布公告</h2>${announcements.map(a=>`<article class="question"><h3>${esc(a.title)}</h3><p>${esc(a.content)}</p><div class="row"><button class="secondary" data-edit-announcement="${a.id}" data-title="${esc(a.title)}" data-content="${esc(a.content)}">編輯</button><button class="danger-button" data-delete-announcement="${a.id}">刪除</button></div></article>`).join("") || `<div class="empty">目前沒有公告</div>`}`)}
      ${card(`<h2>待處理便利貼檢舉</h2>${reports.map(r=>`<article class="question"><p><strong>${esc(r.reporter)}</strong> 檢舉：${esc(r.reason)}</p><p>${esc(r.content)}</p><div class="row"><button class="secondary" data-resolve="${r.id}">標記已處理</button><button class="danger-button" data-delete-note="${r.note_id}" data-report-id="${r.id}">刪除留言</button></div></article>`).join("") || `<div class="empty">目前沒有便利貼檢舉</div>`}<h2 style="margin-top:1.5rem">題目回報</h2>${questionReports.map(r=>`<article class="question"><p><strong>${esc(r.reporter)}</strong> 回報：${esc(r.reason)}</p><p>${esc(r.prompt)}</p><button class="secondary" data-resolve-question="${r.id}">標記已處理</button></article>`).join("") || `<div class="empty">目前沒有題目回報</div>`}`)}</div>`;
  }
  if (state.page === "操作紀錄") {
    const rows = await api("/teacher/audit");
    return table(["操作者","操作","項目","編號","時間"],rows.map(r=>[esc(r.actor||"系統"),esc(r.action),esc(r.entity_type),esc(r.entity_id),displayDate(r.created_at)]));
  }
  return `<div class="empty">尚未開放此頁面</div>`;
}

function values(form) { return Object.fromEntries(new FormData(form).entries()); }
function checked(form, name) { return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map(el=>el.value); }
async function handleSubmit(event) {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  const type = form.dataset.form;
  const data = values(form);
  try {
    if (type === "login" || type === "register") {
      if (type === "register" && data.password !== data.confirm) throw new Error("兩次輸入的密碼不一致。");
      const result = await api(`/auth/${type}`, { method: "POST", body: data });
      state.user = result.user;
      state.page = result.user.role === "teacher" ? teacherPages[0] : studentPages[0];
      if (result.daily_reward) notice(`連續登入第 ${result.daily_reward.streak_day} 天，獲得 ${result.daily_reward.points_awarded} 分！`, "success");
      renderShell();
      return;
    }
    if (type === "forgot") {
      const result = await api("/auth/request-reset", { method: "POST", body: data });
      notice(result.message, "success"); renderAuth("forgot"); return;
    }
    if (type === "reset") {
      if (data.password !== data.confirm) throw new Error("兩次輸入的新密碼不一致。");
      await api("/auth/reset", { method: "POST", body: { token: data.token, password: data.password } });
      history.replaceState(null, "", "/");
      notice("密碼已重設，請使用新密碼登入。", "success"); renderAuth("login"); return;
    }
    if (type === "pdf-read") {
      const file = form.querySelector('[name="pdf"]').files[0];
      if (!file || file.size > 3_000_000) throw new Error("PDF 檔案上限為 3 MB。");
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
      }
      const extracted = await api("/teacher/pdf-text", { method: "POST", body: { content_base64: btoa(binary) } });
      form.querySelector('[name="extracted"]').value = extracted.text;
      notice(`PDF 文字已擷取（${extracted.characters} 字元），請人工校對後出題。`, "success");
      return;
    }
    if (type === "start-practice") {
      const result = await api("/practice/start", { method: "POST", body: { category: data.category || null, difficulty: data.difficulty || null, count: Number(data.count) } });
      state.activeQuiz = { ...result, submitted: false };
      renderPage(); return;
    }
    if (type === "submit-quiz") {
      const responses = {};
      for (const question of state.activeQuiz.questions) {
        const input = form.querySelector(`[name="q-${question.id}"]:checked`) || form.querySelector(`[name="q-${question.id}"]`);
        if (!input?.value.trim()) throw new Error("請完成所有題目後再提交。");
        responses[String(question.id)] = input.value.trim();
      }
      if (!confirm("提交後不能修改答案，確定交卷嗎？")) return;
      const result = await api(`/practice/${state.activeQuiz.quiz_id}/submit`, { method: "POST", body: { responses } });
      state.activeQuiz.submitted = true; state.activeQuiz.result = result; renderPage(); return;
    }
    if (type === "profile") {
      await api("/me", { method: "PATCH", body: data });
      state.user.display_name = data.display_name; state.user.email = data.email; notice("個人資料已更新。", "success");
    } else if (type === "password") {
      if (data.new_password !== data.confirm_password) throw new Error("兩次輸入的新密碼不一致。");
      await api("/auth/password", { method: "POST", body: { current_password: data.current_password, new_password: data.new_password } });
      state.user.must_change_password = false; notice("密碼已更新。", "success");
    } else if (type === "new-note") {
      await api("/student/notes", { method: "POST", body: data }); notice("便利貼已發布。", "success");
    } else if (type === "reply-note") {
      await api(`/student/notes/${form.dataset.note}/replies`, { method: "POST", body: data }); notice("回覆已發布。", "success");
    } else if (type === "submit-essay") {
      if (!confirm("提交後將不能自行修改，確定提交作文嗎？")) return;
      await api(`/student/essays/${form.dataset.question}/submit`, { method: "POST", body: { content: data.content, assignment_id: form.dataset.assignment ? Number(form.dataset.assignment) : null } }); notice("作文已提交，提交後不能自行修改。", "success");
    } else if (type === "create-class") {
      await api("/teacher/classes", { method: "POST", body: { name: data.name, student_ids: checked(form, "student_ids") } }); notice("班級已建立。", "success");
    } else if (type === "create-student") {
      await api("/teacher/students", { method: "POST", body: data }); notice("學生帳號已建立，首次登入需修改密碼。", "success");
    } else if (type === "add-question") {
      const raw = {
        ...data,
        points: Number(data.points),
        min_word_count: Number(data.min_word_count || 0),
        bank_id: form.dataset.bankId ? Number(form.dataset.bankId) : null,
        is_published: Boolean(form.querySelector('[name="is_published"]:checked')),
        options: data.options ? data.options.split("|").map(x=>x.trim()).filter(Boolean) : [],
        accepted_answers: data.accepted_answers ? data.accepted_answers.split("|").map(x=>x.trim()).filter(Boolean) : [],
      };
      if (form.dataset.editId) {
        await api(`/teacher/questions/${form.dataset.editId}`, { method: "PATCH", body: raw });
        form.dataset.editId = "";
        notice("題目已更新。", "success");
      } else {
        await api("/teacher/questions", { method: "POST", body: raw }); notice("題目已新增。", "success");
      }
    } else if (type === "import-json") {
      let parsed;
      if (data.file) {
        const file = form.querySelector('[name="file"]').files[0];
        if (!file || file.size > 2_000_000) throw new Error("JSON 檔案上限為 2 MB。");
        parsed = JSON.parse(await file.text());
      } else parsed = JSON.parse(data.json);
      const imported = await api("/teacher/questions/import", { method: "POST", body: parsed });
      notice(`已成功匯入 ${imported.imported} 題。`, "success");
    } else if (type === "create-assignment") {
      await api("/teacher/assignments", { method: "POST", body: {
        title: data.title, description: data.description, due_at: data.due_at || null,
        time_limit_minutes: Number(data.time_limit_minutes), show_answers: Boolean(form.querySelector('[name="show_answers"]:checked')),
        allow_retake: Boolean(form.querySelector('[name="allow_retake"]:checked')),
        question_ids: checked(form, "question_ids"), student_ids: checked(form, "student_ids"),
      } }); notice("作業已發布。", "success");
    } else if (type === "grade-essay") {
      const comments = data.quoted_text && data.comment ? [{ quoted_text: data.quoted_text, comment: data.comment }] : [];
      await api(`/teacher/essays/${form.dataset.attempt}/grade`, { method: "POST", body: { score: Number(data.score), feedback: data.feedback, comments } });
      notice("作文批改已儲存。", "success");
    } else if (type === "announcement") {
      await api("/teacher/announcements", { method: "POST", body: data }); notice("公告已發布。", "success");
    }
    renderPage();
  } catch (error) {
    notice(error.message, "error");
    if (state.user) renderPage();
    else renderAuth(type === "register" ? "register" : type === "forgot" ? "forgot" : type === "reset" ? "reset" : "login");
  }
}

app.addEventListener("submit", handleSubmit);
let draftTimers = new Map();
app.addEventListener("input", event => {
  const editor = event.target.closest("textarea.essay-draft");
  if (!editor) return;
  const questionId = editor.dataset.question;
  clearTimeout(draftTimers.get(questionId));
  const status = document.querySelector(`[data-draft-status="${questionId}"]`);
  if (status) status.textContent = "儲存中…";
  draftTimers.set(questionId, setTimeout(async () => {
    try {
      await api(`/student/essays/${questionId}/draft`, { method: "PUT", body: { content: editor.value } });
      if (status) status.textContent = "草稿已自動儲存";
    } catch (error) {
      if (status) status.textContent = `自動儲存失敗：${error.message}`;
    }
  }, 900));
});
app.addEventListener("click", async event => {
  const authButton = event.target.closest("[data-auth]");
  if (authButton) { renderAuth(authButton.dataset.auth); return; }
  const pageButton = event.target.closest("[data-page]");
  if (pageButton) { state.page = pageButton.dataset.page; state.activeQuiz = null; renderShell(); return; }
  const action = event.target.closest("[data-action]")?.dataset.action;
  try {
    if (action === "logout") {
      await api("/auth/logout", { method: "POST" }); state.user = null; state.activeQuiz = null; renderAuth(); return;
    }
    if (action === "practice-done") { state.activeQuiz = null; renderPage(); return; }
    if (action === "refresh") { renderPage(); return; }
    if (action === "wheel") {
      const id = crypto.randomUUID() + crypto.randomUUID();
      const reward = await api("/student/wheel", { method: "POST", body: { request_id: id } });
      const result = document.querySelector("#wheel-result");
      if (result) {
        const options = reward.wheel_type === "milestone" ? [30,40,50,60,70] : [10,20,30,40,50];
        const index = options.indexOf(reward.points_awarded);
        result.innerHTML = `<div class="wheel-stage"><div class="pointer"></div><div class="wheel spin" style="--stop:${8*360+360-(index+.5)*72}deg"><div class="wheel-labels">${options.map((n,i)=>`<span style="--angle:${i*72+36}deg">+${n}</span>`).join("")}</div></div></div><div class="notice success">獲得 +${reward.points_awarded} 分，${reward.points_spent ? `已扣除 ${reward.points_spent} 分。` : "使用免費轉盤。"}目前 ${reward.score} 分。</div>`;
      }
      return;
    }
    const assignment = event.target.closest("[data-assignment]")?.dataset.assignment;
    if (assignment) {
      state.activeQuiz = await api("/practice/start", { method: "POST", body: { assignment_id: Number(assignment), count: 50 } });
      state.activeQuiz.submitted = false; state.page = "練習題"; renderShell(); return;
    }
    const report = event.target.closest("[data-report]")?.dataset.report;
    if (report) {
      const reason = prompt("請輸入檢舉原因：");
      if (reason) { await api(`/student/notes/${report}/report`, { method: "POST", body: { reason } }); notice("檢舉已送交教師。", "success"); renderPage(); }
      return;
    }
    const editNote = event.target.closest("[data-edit-note]");
    if (editNote) {
      const content = prompt("編輯便利貼內容：", editNote.dataset.content || "");
      if (content !== null) {
        await api(`/student/notes/${editNote.dataset.editNote}`, { method: "PATCH", body: { content } });
        notice("便利貼已更新。", "success"); renderPage();
      }
      return;
    }
    const deleteOwnNote = event.target.closest("[data-delete-own-note]")?.dataset.deleteOwnNote;
    if (deleteOwnNote && confirm("確定刪除這則便利貼？")) {
      await api(`/student/notes/${deleteOwnNote}`, { method: "DELETE" });
      notice("便利貼已刪除。", "success"); renderPage(); return;
    }
    const resolve = event.target.closest("[data-resolve]")?.dataset.resolve;
    if (resolve) { await api(`/teacher/reports/${resolve}/resolve`, { method: "POST" }); notice("檢舉已標記處理。", "success"); renderPage(); return; }
    const resolveQuestion = event.target.closest("[data-resolve-question]")?.dataset.resolveQuestion;
    if (resolveQuestion) { await api(`/teacher/question-reports/${resolveQuestion}/resolve`, { method: "POST" }); notice("題目回報已標記處理。", "success"); renderPage(); return; }
    const reportedQuestion = event.target.closest("[data-report-question]")?.dataset.reportQuestion;
    if (reportedQuestion) {
      const reason = prompt("請描述題目問題：");
      if (reason) { await api(`/student/questions/${reportedQuestion}/report`, { method: "POST", body: { reason } }); notice("題目回報已送出。", "success"); renderPage(); }
      return;
    }
    const resetStudent = event.target.closest("[data-reset-student]")?.dataset.resetStudent;
    if (resetStudent && confirm("確定重設此學生密碼？重設後請將臨時密碼安全交給學生。")) {
      const result = await api(`/teacher/students/${resetStudent}/reset-password`, { method: "POST" });
      alert(`學生臨時密碼：${result.temporary_password}`);
      notice("已重設密碼，學生首次登入時必須更改。", "success"); renderPage(); return;
    }
    const statusStudent = event.target.closest("[data-status-student]");
    if (statusStudent) {
      const active = statusStudent.dataset.active === "true";
      await api(`/teacher/students/${statusStudent.dataset.statusStudent}/status`, { method: "POST", body: { active } });
      notice(active ? "學生帳號已啟用。" : "學生帳號已停用。", "success"); renderPage(); return;
    }
    const archiveQuestion = event.target.closest("[data-archive-question]")?.dataset.archiveQuestion;
    if (archiveQuestion && confirm("確定下架此題目？下架後學生無法再看到。")) {
      await api(`/teacher/questions/${archiveQuestion}`, { method: "DELETE" });
      notice("題目已下架。", "success"); renderPage(); return;
    }
    const editQuestion = event.target.closest("[data-edit-question]")?.dataset.editQuestion;
    if (editQuestion) {
      const questions = await api("/teacher/questions");
      const question = questions.find(item => String(item.id) === editQuestion);
      if (!question) throw new Error("找不到題目。");
      const form = document.querySelector('[data-form="add-question"]');
      form.dataset.editId = question.id;
      form.dataset.bankId = question.bank_id || "";
      form.elements.category.value = question.category;
      form.elements.difficulty.value = question.difficulty;
      form.elements.type.value = question.question_type;
      form.elements.prompt.value = question.prompt;
      form.elements.options.value = (question.options || []).join(" | ");
      form.elements.answer.value = question.answer || "";
      form.elements.accepted_answers.value = (question.accepted_answers || []).join(" | ");
      form.elements.points.value = question.points;
      form.elements.min_word_count.value = question.min_word_count || 0;
      form.elements.rubric.value = question.rubric || "";
      form.elements.explanation.value = question.explanation || "";
      form.elements.is_published.checked = question.is_published;
      form.querySelector("button.primary").textContent = "儲存題目修改";
      form.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const returnEssay = event.target.closest("[data-return-essay]")?.dataset.returnEssay;
    if (returnEssay) {
      const feedback = prompt("請輸入要求學生修改的內容：");
      if (feedback) {
        await api(`/teacher/essays/${returnEssay}/return`, { method: "POST", body: { feedback } });
        notice("作文已退回修改。", "success"); renderPage();
      }
      return;
    }
    const editAnnouncement = event.target.closest("[data-edit-announcement]");
    if (editAnnouncement) {
      const title = prompt("公告標題：", editAnnouncement.dataset.title || "");
      if (title !== null) {
        const content = prompt("公告內容：", editAnnouncement.dataset.content || "");
        if (content !== null) {
          await api(`/teacher/announcements/${editAnnouncement.dataset.editAnnouncement}`, { method: "PATCH", body: { title, content } });
          notice("公告已更新。", "success"); renderPage();
        }
      }
      return;
    }
    const deleteAnnouncement = event.target.closest("[data-delete-announcement]")?.dataset.deleteAnnouncement;
    if (deleteAnnouncement && confirm("確定刪除這則公告？")) {
      await api(`/teacher/announcements/${deleteAnnouncement}`, { method: "DELETE" });
      notice("公告已刪除。", "success"); renderPage(); return;
    }
    const deleteNote = event.target.closest("[data-delete-note]")?.dataset.deleteNote;
    if (deleteNote && confirm("確定刪除此便利貼？")) {
      await api(`/teacher/notes/${deleteNote}`, { method: "DELETE" });
      const reportId = event.target.closest("[data-delete-note]").dataset.reportId;
      await api(`/teacher/reports/${reportId}/resolve`, { method: "POST" });
      notice("留言已刪除並完成檢舉處理。", "success"); renderPage();
    }
    const draft = event.target.closest("[data-save-draft]")?.dataset.saveDraft;
    if (draft) {
      const form = event.target.closest("article").querySelector("form");
      const content = form.querySelector("textarea").value;
      await api(`/student/essays/${draft}/draft`, { method: "PUT", body: { content } });
      notice("作文草稿已儲存。", "success"); renderPage();
    }
  } catch (error) { notice(error.message, "error"); renderPage(); }
});

async function init() {
  if (new URLSearchParams(location.search).has("reset")) {
    renderAuth("reset");
    return;
  }
  try {
    const result = await api("/me");
    state.user = result.user;
    state.page = result.user.role === "teacher" ? teacherPages[0] : studentPages[0];
    renderShell();
  } catch {
    renderAuth();
  }
}
init();
