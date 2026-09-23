(() => {
  const root = document.getElementById("lab-app");
  if (!root) return;

  const elements = {
    message: document.querySelector("[data-lab-message]"),
    messageBox: document.getElementById("lab-message"),
    courses: document.getElementById("lab-courses"),
    semester: document.getElementById("lab-semester"),
    currentSemester: document.getElementById("lab-current-semester"),
    courseTotal: document.getElementById("lab-course-total"),
    courseCount: document.getElementById("lab-course-count"),
    updated: document.getElementById("lab-updated"),
    search: document.getElementById("lab-search"),
    pagination: document.getElementById("lab-pagination"),
    page: document.getElementById("lab-page"),
    prev: document.getElementById("lab-prev"),
    next: document.getElementById("lab-next"),
    waitlist: document.getElementById("lab-waitlist"),
    waitlistMessage: document.getElementById("lab-waitlist-message"),
    waitlistCount: document.getElementById("lab-waitlist-count"),
    waitlistActive: document.getElementById("lab-waitlist-active"),
    waitlistSelected: document.getElementById("lab-waitlist-selected"),
    waitlistTotal: document.getElementById("lab-waitlist-total"),
    confirmation: document.getElementById("lab-confirm"),
  };
  let page = 1;
  let total = 0;
  let busy = false;
  let loadingWaitlist = false;
  let pendingSelection = null;

  const node = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const icon = (name, small = false) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon" + (small ? " small" : ""));
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "/static/icons.svg#" + name);
    svg.append(use);
    return svg;
  };
  const first = (object, keys, fallback = "") => {
    for (const key of keys) {
      const value = object && object[key];
      if (value !== undefined && value !== null && String(value).trim()) return String(value);
    }
    return fallback;
  };
  const courseName = (course) => first(course, ["courseName", "taskName", "name"], "未命名实验课程");
  const button = (text, handler, options = {}) => {
    const el = node("button", "btn small" + (options.primary ? " primary" : "") + (options.danger ? " danger" : ""));
    el.type = "button";
    el.disabled = !!options.disabled;
    if (options.icon) el.append(icon(options.icon, true));
    el.append(node("span", "", text));
    el.addEventListener("click", handler);
    return el;
  };
  const setMessage = (text, tone = "") => {
    elements.message.textContent = text;
    elements.messageBox.classList.remove("error", "success", "warning");
    if (tone) elements.messageBox.classList.add(tone);
  };
  const dateTime = (timestamp) => timestamp
    ? new Date(timestamp * 1000).toLocaleString("zh-CN", {hour12: false})
    : "—";

  async function api(path, params, body) {
    const response = await fetch("/api/lab-selection/" + path + (params ? "?" + new URLSearchParams(params) : ""), {
      method: body ? "POST" : "GET",
      headers: body ? {"Content-Type": "application/json"} : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请检查输入或重新登录本科教务");
    return data;
  }

  async function run(action, starting = "正在查询教务系统…") {
    if (busy) return;
    busy = true;
    root.setAttribute("aria-busy", "true");
    setMessage(starting);
    const controls = [...root.querySelectorAll("button, select")].map((el) => [el, el.disabled]);
    controls.forEach(([el]) => { el.disabled = true; });
    try {
      await action();
    } catch (error) {
      setMessage(error && error.message ? error.message : "请求失败", "error");
    } finally {
      busy = false;
      root.removeAttribute("aria-busy");
      controls.forEach(([el, disabled]) => { if (el.isConnected) el.disabled = disabled; });
      elements.prev.disabled = page <= 1;
      elements.next.disabled = page * 30 >= total;
    }
  }

  function capacity(selected, maximum) {
    const wrap = node("div", "capacity");
    const selectedNumber = Number(selected);
    const maximumNumber = Number(maximum);
    if (!Number.isFinite(selectedNumber) || !Number.isFinite(maximumNumber) || maximumNumber <= 0) {
      wrap.append(node("span", "muted", "未公布"));
      return wrap;
    }
    const percent = Math.max(0, Math.min(100, selectedNumber * 100 / maximumNumber));
    const track = node("div", "progress");
    const bar = node("span");
    bar.style.width = percent + "%";
    bar.style.background = selectedNumber >= maximumNumber ? "var(--red)" : (percent > 85 ? "var(--amber)" : "var(--green)");
    track.append(bar);
    wrap.append(track, node("span", "capacity-label", selectedNumber + "/" + maximumNumber));
    return wrap;
  }

  function status(label, color) {
    return node("span", "status " + color, label);
  }

  function openConfirmation(course, payload, description, waitlist = false) {
    if (busy) return;
    pendingSelection = {course, payload, description, waitlist};
    document.getElementById("lab-confirm-title").textContent = waitlist ? "确认加入自动候补" : "确认实验选课";
    document.getElementById("lab-confirm-submit").textContent = waitlist ? "加入候补" : "确认选课";
    document.getElementById("lab-waitlist-options").hidden = !waitlist;
    document.getElementById("lab-confirm-description").textContent = courseName(course) + " · " + description;
    elements.confirmation.showModal();
  }

  function actionButtons(course, payload, description, reason) {
    const actions = node("div", "lab-row-actions");
    actions.append(button(reason || "选课", () => openConfirmation(course, payload, description), {
      primary: !reason,
      disabled: !!reason,
    }));
    if (!reason || ["已满", "尚未开始", "实验时间冲突", "理论课时间冲突"].includes(reason)) {
      actions.append(button("候补", () => openConfirmation(course, payload, description, true), {icon: "clock-3"}));
    }
    return actions;
  }

  function renderGroupCourse(course, group) {
    const block = node("section", "lab-group-block");
    const info = group.groupInfo || {};
    const name = first(info, ["name", "groupName"], "实验分组");
    const head = node("div", "lab-group-head");
    const copy = node("div");
    copy.append(node("h3", "", name));
    copy.append(node("p", "", (info.selectBeginTime || "开始时间未公布") + " 至 " + (info.selectEndTime || "结束时间未公布")));
    head.append(copy);
    const reason = group.unavailable_reason || "";
    const headActions = node("div", "lab-group-actions");
    headActions.append(capacity(group.selectCount, group.maxCount));
    headActions.append(status(reason || "可选", reason === "已选" ? "green" : (reason ? "red" : "blue")));
    headActions.append(actionButtons(course, {group_id: String(info.id)}, name, reason));
    head.append(headActions);
    block.append(head);
    const items = Array.isArray(group.itemList) ? group.itemList : [];
    if (items.length) {
      const wrap = node("div", "table-wrap");
      const table = node("table", "lab-subject-table");
      const thead = node("thead");
      const header = node("tr");
      ["实验项目", "上课安排"].forEach((label) => header.append(node("th", "", label)));
      thead.append(header);
      const tbody = node("tbody");
      items.forEach((item) => {
        const row = node("tr");
        const title = node("td");
        title.append(node("div", "table-title", first(item, ["subjectName", "name"], "实验项目")));
        const schedule = node("td");
        schedule.append(node("div", "schedule", first(item, ["time", "schedule"], "时间未公布")));
        row.append(title, schedule);
        tbody.append(row);
      });
      table.append(thead, tbody);
      wrap.append(table);
      block.append(wrap);
    }
    return block;
  }

  async function loadSlots(course, subject, row, trigger) {
    if (row.nextElementSibling && row.nextElementSibling.classList.contains("lab-slot-row")) {
      row.nextElementSibling.remove();
      trigger.querySelector("span").textContent = "查看时间";
      return;
    }
    const slots = await api("slots", {task_id: course.id, subject_id: subject.id});
    const slotRow = node("tr", "lab-slot-row");
    const cell = node("td", "lab-slot-panel");
    cell.colSpan = 4;
    const wrap = node("div", "table-wrap");
    const table = node("table", "lab-slot-table");
    const thead = node("thead");
    const header = node("tr");
    ["实验时间", "教师", "状态", ""].forEach((label) => header.append(node("th", "", label)));
    thead.append(header);
    const tbody = node("tbody");
    slots.forEach((slot) => {
      const times = (slot.list || []).map((item) => `第${item.weekNum || "—"}周 ${item.weekAndDate || ""} ${item.lessonBegin || "—"}~${item.lessonEnd || "—"}节`).join("；") || "时间未公布";
      const teachers = [...new Set((slot.list || []).map((item) => item.tecName).filter(Boolean))].join("、") || "未公布";
      const reason = slot.unavailable_reason || "";
      const slotLine = node("tr");
      const schedule = node("td");
      schedule.append(node("div", "schedule", times));
      const teacher = node("td", "", teachers);
      const state = node("td");
      state.append(status(reason || "可选", reason ? "red" : "blue"));
      const actions = node("td", "actions-cell");
      actions.append(actionButtons(course, {subject_id: String(subject.id), item_ids: slot.item_ids}, `${first(subject, ["name"], "实验项目")} · ${times}`, reason));
      slotLine.append(schedule, teacher, state, actions);
      tbody.append(slotLine);
    });
    if (!slots.length) {
      const empty = node("td", "table-empty", "暂无已发布的实验时间");
      empty.colSpan = 4;
      const emptyRow = node("tr"); emptyRow.append(empty); tbody.append(emptyRow);
    }
    table.append(thead, tbody); wrap.append(table); cell.append(wrap); slotRow.append(cell);
    row.after(slotRow);
    trigger.querySelector("span").textContent = "收起时间";
    setMessage(slots.length ? "已加载可选时间；提交前会再次核对。" : "暂无已发布的实验时间。", slots.length ? "success" : "warning");
  }

  function renderSubjectGroup(course, group) {
    const block = node("section", "lab-group-block");
    const subjects = Array.isArray(group.list) ? group.list : [];
    const groupName = group.type || "实验项目";
    const head = node("div", "lab-group-head");
    const copy = node("div");
    copy.append(node("h3", "", groupName));
    copy.append(node("p", "", `需完成 ${group.needComplete ?? "—"} 项 · 已完成 ${group.selectCount ?? "—"} 项`));
    head.append(copy, status(`${subjects.length} 个项目`, "gray"));
    block.append(head);
    const wrap = node("div", "table-wrap");
    const table = node("table", "lab-subject-table");
    const thead = node("thead");
    const header = node("tr");
    ["实验项目", "已选时间", "状态", ""].forEach((label) => header.append(node("th", "", label)));
    thead.append(header);
    const tbody = node("tbody");
    subjects.forEach((subject) => {
      const row = node("tr");
      const name = first(subject, ["name", "subjectName"], "实验项目");
      const selectedText = first(subject, ["selectItemString"]);
      const eligible = Number(group.needComplete) > Number(group.selectCount);
      const title = node("td"); title.append(node("div", "table-title", name));
      const selected = node("td"); selected.append(node("div", "schedule", selectedText || "尚未选择"));
      const state = node("td"); state.append(status(selectedText ? "已选" : (eligible ? "待选择" : "已满足要求"), selectedText ? "green" : (eligible ? "blue" : "gray")));
      const actions = node("td", "actions-cell");
      actions.append(button("查看时间", (event) => run(() => loadSlots(course, subject, row, event.currentTarget), "正在读取可选时间…"), {
        disabled: !!selectedText || !eligible,
      }));
      row.append(title, selected, state, actions);
      tbody.append(row);
    });
    if (!subjects.length) {
      const empty = node("td", "table-empty", "该分类暂无实验项目"); empty.colSpan = 4;
      const emptyRow = node("tr"); emptyRow.append(empty); tbody.append(emptyRow);
    }
    table.append(thead, tbody); wrap.append(table); block.append(wrap);
    return block;
  }

  async function loadSubjects(course, details, body) {
    body.replaceChildren(node("div", "table-empty lab-course-loading", "正在读取实验项目和已选状态…"));
    try {
      const groups = await api("subjects", {task_id: course.id});
      body.replaceChildren();
      groups.forEach((group) => body.append(group.groupInfo ? renderGroupCourse(course, group) : renderSubjectGroup(course, group)));
      if (!groups.length) body.append(node("div", "table-empty", "尚未发布实验选课安排"));
      details.dataset.loaded = "true";
      setMessage(groups.length ? `已更新“${courseName(course)}”的实验与已选状态。` : "尚未发布实验选课安排。", groups.length ? "success" : "warning");
    } catch (error) {
      body.replaceChildren(node("div", "table-empty", error.message));
      throw error;
    }
  }

  function renderCourse(course, index) {
    const details = node("details", "selection-course-group lab-course-group");
    details.dataset.search = JSON.stringify(course).toLocaleLowerCase();
    const summary = node("summary", "selection-course-summary");
    const chevron = node("span", "selection-course-chevron"); chevron.append(icon("chevron-down", true));
    const copy = node("span", "selection-course-copy");
    copy.append(node("strong", "", courseName(course)));
    const metadata = [first(course, ["courseCode", "taskCode", "code"]), first(course, ["teacherName", "teacher", "teachers"]), first(course, ["className", "gradeName"])].filter(Boolean).join(" · ");
    copy.append(node("span", "", metadata || "展开查看实验项目、分组和可选时间"));
    summary.append(chevron, copy, node("span", "selection-course-count", "查看实验"));
    const body = node("div", "lab-course-body");
    body.append(node("div", "table-empty lab-course-loading", "展开后加载实验项目"));
    details.append(summary, body);
    details.dataset.taskId = String(course.id);
    details.addEventListener("toggle", () => {
      if (details.open && details.dataset.loaded !== "true" && !busy) run(() => loadSubjects(course, details, body), "正在读取实验项目和已选状态…");
    });
    return details;
  }

  function applySearch() {
    const term = elements.search.value.trim().toLocaleLowerCase();
    let shown = 0;
    elements.courses.querySelectorAll(".lab-course-group").forEach((course) => {
      course.hidden = !!term && !course.dataset.search.includes(term);
      if (!course.hidden) shown += 1;
    });
    elements.updated.textContent = term ? `当前页显示 ${shown} 门` : `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
  }

  async function loadCourses() {
    const requestedSemester = elements.semester.value;
    elements.courses.replaceChildren(node("div", "table-wrap"));
    elements.courses.firstElementChild.append(node("div", "table-empty", "正在读取实验课程…"));
    const data = await api("courses", {semester: requestedSemester, page});
    elements.semester.replaceChildren(...data.semesters.map((semester) => {
      const option = node("option", "", semester.text || semester.label || semester.value);
      option.value = semester.value;
      return option;
    }));
    elements.semester.value = data.semester;
    page = data.page;
    total = data.total;
    elements.courses.replaceChildren();
    data.courses.forEach((course, index) => elements.courses.append(renderCourse(course, index)));
    if (!data.courses.length) {
      const empty = node("div", "table-wrap"); empty.append(node("div", "table-empty", "该学期暂无实验课程，可切换学期查询")); elements.courses.append(empty);
    }
    const selectedOption = elements.semester.selectedOptions[0];
    elements.currentSemester.textContent = selectedOption ? selectedOption.textContent : "未公布";
    elements.courseTotal.textContent = `${data.total} 门`;
    elements.courseCount.textContent = String(data.total);
    elements.page.textContent = `第 ${page} 页，共 ${data.total} 门`;
    elements.pagination.hidden = data.total <= 30;
    elements.updated.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
    elements.search.value = "";
    setMessage(data.courses.length ? `已获取 ${data.total} 门实验课程，展开课程可查看实验安排。` : "该学期暂无实验课程，可切换学期查询。", data.courses.length ? "success" : "warning");
    if (data.courses.length) {
      const firstDetails = elements.courses.querySelector(".lab-course-group");
      firstDetails.open = true;
      await loadSubjects(data.courses[0], firstDetails, firstDetails.querySelector(".lab-course-body"));
    }
  }

  const waitlistColors = {
    waiting: "blue", waiting_session: "amber", checking: "blue", submitting: "blue",
    selected: "green", paused: "amber", cancelled: "gray", expired: "red",
  };
  async function loadWaitlist() {
    if (loadingWaitlist) return;
    loadingWaitlist = true;
    try {
      const jobs = await api("waitlist");
      elements.waitlist.replaceChildren();
      jobs.forEach((job) => {
        const row = node("tr");
        const title = node("td");
        title.append(node("div", "table-title", job.label || "实验课候补"));
        title.append(node("div", "table-subtitle", job.message || ""));
        const state = node("td"); state.append(status(job.status_label, waitlistColors[job.status] || "gray"));
        const counts = node("td");
        counts.append(node("div", "table-title", String(job.checks || 0)));
        counts.append(node("div", "table-subtitle", `已尝试 ${job.attempts || 0} 次`));
        const next = node("td", "", dateTime(job.next_check));
        const actions = node("td", "actions-cell");
        if (job.is_active || job.status === "paused") {
          actions.append(button("取消", async (event) => {
            event.currentTarget.disabled = true;
            try {
              await api(`waitlist/${encodeURIComponent(job.id)}/cancel`, null, {});
              await loadWaitlist();
            } catch (error) {
              elements.waitlistMessage.textContent = error.message;
              event.currentTarget.disabled = false;
            }
          }, {danger: true}));
        }
        row.append(title, state, counts, next, actions);
        elements.waitlist.append(row);
      });
      if (!jobs.length) {
        const empty = node("td", "table-empty", "暂无候补任务，可在实验分组或时间段旁点击“候补”");
        empty.colSpan = 5;
        const row = node("tr"); row.append(empty); elements.waitlist.append(row);
      }
      const active = jobs.filter((job) => job.is_active).length;
      const selected = jobs.filter((job) => job.status === "selected").length;
      elements.waitlistActive.textContent = String(active);
      elements.waitlistSelected.textContent = String(selected);
      elements.waitlistTotal.textContent = String(jobs.length);
      elements.waitlistCount.textContent = String(active);
      elements.waitlistMessage.textContent = jobs.length ? `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}` : "候补期间需保持应用运行，重启应用后会继续。";
    } catch (error) {
      elements.waitlistMessage.textContent = error.message;
    } finally {
      loadingWaitlist = false;
    }
  }

  document.querySelector("[data-lab-query-form]").addEventListener("submit", (event) => {
    event.preventDefault(); page = 1; run(loadCourses);
  });
  elements.semester.addEventListener("change", () => { page = 1; run(loadCourses); });
  elements.prev.addEventListener("click", () => { if (!busy && page > 1) { page -= 1; run(loadCourses); } });
  elements.next.addEventListener("click", () => { if (!busy && page * 30 < total) { page += 1; run(loadCourses); } });
  elements.search.addEventListener("input", applySearch);
  document.querySelectorAll("[data-lab-tab]").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll("[data-lab-tab]").forEach((item) => item.classList.toggle("active", item === tab));
    document.querySelectorAll("[data-lab-panel]").forEach((panel) => { panel.hidden = panel.dataset.labPanel !== tab.dataset.labTab; });
    if (tab.dataset.labTab === "waitlist") loadWaitlist();
  }));
  document.getElementById("lab-confirm-cancel").addEventListener("click", () => elements.confirmation.close());
  elements.confirmation.addEventListener("close", () => { pendingSelection = null; });
  document.getElementById("lab-confirm-submit").addEventListener("click", async () => {
    if (!pendingSelection || busy) return;
    const pending = pendingSelection;
    const interval = Number(document.getElementById("lab-waitlist-interval").value);
    if (pending.waitlist && (!Number.isInteger(interval) || interval < 15 || interval > 3600)) {
      document.getElementById("lab-waitlist-interval").reportValidity();
      return;
    }
    elements.confirmation.close();
    await run(async () => {
      if (pending.waitlist) {
        const result = await api("waitlist", null, {
          task_id: String(pending.course.id), ...pending.payload,
          label: `${courseName(pending.course)} · ${pending.description}`.slice(0, 250), interval,
        });
        await loadWaitlist();
        setMessage(result.created ? "已加入候补，保持应用运行即可自动检查和尝试选课。" : "已有相同候补任务，可在“候补任务”中查看。", "success");
        return;
      }
      const result = await api("select", null, {task_id: String(pending.course.id), ...pending.payload});
      const details = elements.courses.querySelector(`[data-task-id="${CSS.escape(String(pending.course.id))}"]`);
      if (details) await loadSubjects(pending.course, details, details.querySelector(".lab-course-body"));
      setMessage(result.message, result.verified ? "success" : "warning");
    }, pending.waitlist ? "正在创建候补任务…" : "正在核对名额和时间并提交，请勿重复操作…");
  });
  document.getElementById("lab-waitlist-refresh").addEventListener("click", loadWaitlist);

  loadWaitlist();
  window.setInterval(() => { if (!document.hidden) loadWaitlist(); }, 10000);
})();
