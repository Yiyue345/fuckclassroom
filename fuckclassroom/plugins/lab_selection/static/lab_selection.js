(() => {
  const root = document.getElementById('lab-app');
  if (!root) return;
  const message = document.getElementById('lab-message');
  const courses = document.getElementById('lab-courses');
  const subjects = document.getElementById('lab-subjects');
  const semester = document.getElementById('lab-semester');
  let page = 1;
  let total = 0;
  let busy = false;
  let pendingSelection = null;
  const confirmation = document.getElementById('lab-confirm');
  const node = (tag, text) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const button = (text, handler, disabled = false) => {
    const el = node('button', text);
    el.className = 'btn'; el.type = 'button'; el.disabled = disabled;
    el.dataset.unavailable = String(disabled);
    el.addEventListener('click', handler);
    return el;
  };
  async function api(path, params, body) {
    const response = await fetch('/api/lab-selection/' + path + (params ? '?' + new URLSearchParams(params) : ''), {
      method: body ? 'POST' : 'GET',
      headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: 'no-store',
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入或重新登录本科教务');
    return data;
  }
  async function run(action) {
    if (busy) return;
    busy = true; root.setAttribute('aria-busy', 'true'); message.textContent = '正在查询教务系统…';
    const controls = [...root.querySelectorAll('button, select')].map(el => [el, el.disabled]);
    controls.forEach(([el]) => {el.disabled = true;});
    try { await action(); } catch (error) { message.textContent = error.message; }
    finally {
      busy = false; root.removeAttribute('aria-busy');
      controls.forEach(([el, disabled]) => { if (el.isConnected) el.disabled = disabled; });
      document.getElementById('lab-prev').disabled = page <= 1;
      document.getElementById('lab-next').disabled = page * 30 >= total;
    }
  }
  async function loadCourses() {
    subjects.replaceChildren(); courses.replaceChildren();
    const data = await api('courses', {semester: semester.value, page});
    semester.replaceChildren(...data.semesters.map(s => {
      const option = node('option', s.text || s.label || s.value);
      option.value = s.value; return option;
    }));
    semester.value = data.semester; page = data.page;
    for (const course of data.courses) {
      const card = node('article'); card.className = 'card';
      card.append(node('h2', course.courseName || '实验课程'));
      card.append(button('查看实验与已选状态', () => run(() => loadSubjects(course))));
      courses.append(card);
    }
    document.getElementById('lab-pagination').hidden = data.total <= 30;
    total = data.total;
    document.getElementById('lab-page').textContent = `第 ${page} 页，共 ${data.total} 门`;
    message.textContent = data.courses.length ? `已获取 ${data.total} 门实验课程` : '该学期暂无实验课程，可切换学期查询。';
  }
  async function submit(course, payload, description, waitlist = false) {
    if (busy) return;
    pendingSelection = {course, payload, description, waitlist};
    document.getElementById('lab-confirm-title').textContent = waitlist ? '确认加入自动候补' : '确认实验选课';
    document.getElementById('lab-confirm-submit').textContent = waitlist ? '加入候补' : '确认选课';
    document.getElementById('lab-waitlist-options').hidden = !waitlist;
    document.getElementById('lab-confirm-description').textContent = `${course.courseName} · ${description}`;
    confirmation.showModal();
  }
  document.getElementById('lab-confirm-cancel').addEventListener('click', () => confirmation.close());
  confirmation.addEventListener('close', () => {pendingSelection = null;});
  document.getElementById('lab-confirm-submit').addEventListener('click', async () => {
    if (!pendingSelection || busy) return;
    const {course, payload, description, waitlist} = pendingSelection;
    const interval = Number(document.getElementById('lab-waitlist-interval').value);
    if (waitlist && (!Number.isInteger(interval) || interval < 15 || interval > 3600)) {
      document.getElementById('lab-waitlist-interval').reportValidity();
      return;
    }
    confirmation.close();
    await run(async () => {
      if (waitlist) {
        const result = await api('waitlist', null, {task_id: String(course.id), ...payload, label: `${course.courseName} · ${description}`.slice(0, 250), interval});
        message.textContent = result.created ? '已加入候补，保持应用运行即可自动检查和尝试选课。' : '已有相同候补任务，请在下方查看。';
        await loadWaitlist();
        return;
      }
      message.textContent = '正在核对名额和时间并提交，请勿重复操作…';
      const result = await api('select', null, {task_id: String(course.id), ...payload});
      try { await loadSubjects(course); } catch (_) { /* Preserve the submission outcome. */ }
      message.textContent = result.message;
    });
  });
  async function loadSubjects(course) {
    subjects.replaceChildren();
    const groups = await api('subjects', {task_id: course.id});
    subjects.append(node('h2', course.courseName));
    for (const group of groups) {
      const card = node('article'); card.className = 'card';
      if (group.groupInfo) {
        const info = group.groupInfo;
        card.append(node('h3', info.name));
        card.append(node('p', `已选 ${group.selectCount ?? '—'} / ${group.maxCount ?? '—'} · ${info.selectBeginTime || '—'} 至 ${info.selectEndTime || '—'}`));
        for (const item of group.itemList || []) card.append(node('p', `${item.subjectName || ''} · ${item.time || ''}`));
        const reason = group.unavailable_reason;
        card.append(button(reason || '选择整组', () => submit(course, {group_id: String(info.id)}, info.name), !!reason));
        if (!reason || ['已满', '尚未开始'].includes(reason)) {
          card.append(button('加入候补', () => submit(course, {group_id: String(info.id)}, info.name, true)));
        }
      } else {
        card.append(node('h3', group.type || '实验项目'));
        for (const subject of group.list || []) {
          const row = node('div'); row.append(node('p', `${subject.name || ''} ${subject.selectItemString || ''}`));
          const selected = !!subject.selectItemString;
          const eligible = Number(group.needComplete) > Number(group.selectCount);
          row.append(button(selected ? '已选' : eligible ? '查看可选时间' : '已满足要求', () => run(async () => {
            const slots = await api('slots', {task_id: course.id, subject_id: subject.id});
            let panel = row.querySelector('.lab-slots');
            if (!panel) {panel = node('div'); panel.className = 'lab-slots'; row.append(panel);}
            panel.replaceChildren();
            for (const slot of slots) {
              const times = (slot.list || []).map(i => `第${i.weekNum}周 ${i.weekAndDate || ''} ${i.lessonBegin}~${i.lessonEnd}节 ${i.tecName || ''}`).join('；');
              panel.append(node('p', times));
              panel.append(button(slot.unavailable_reason || '选择此时间', () => submit(course, {subject_id: String(subject.id), item_ids: slot.item_ids}, `${subject.name} · ${times}`), !!slot.unavailable_reason));
              if (!slot.unavailable_reason || ['已满', '尚未开始', '实验时间冲突', '理论课时间冲突'].includes(slot.unavailable_reason)) {
                panel.append(button('候补此时间', () => submit(course, {subject_id: String(subject.id), item_ids: slot.item_ids}, `${subject.name} · ${times}`, true)));
              }
            }
            message.textContent = slots.length ? '已加载可选时间；提交前会再次核对。' : '暂无已发布的实验时间。';
          }), selected || !eligible));
          card.append(row);
        }
      }
      subjects.append(card);
    }
    message.textContent = groups.length ? '已更新实验与已选状态。' : '尚未发布实验选课安排。';
  }
  document.getElementById('lab-refresh').addEventListener('click', () => {page = 1; run(loadCourses);});
  semester.addEventListener('change', () => {page = 1; run(loadCourses);});
  document.getElementById('lab-prev').addEventListener('click', () => {if (!busy && page > 1) {page--; run(loadCourses);}});
  document.getElementById('lab-next').addEventListener('click', () => {if (!busy) {page++; run(loadCourses);}});
  let loadingWaitlist = false;
  async function loadWaitlist() {
    if (loadingWaitlist) return;
    loadingWaitlist = true;
    const status = document.getElementById('lab-waitlist-message');
    try {
      const jobs = await api('waitlist');
      const container = document.getElementById('lab-waitlist');
      container.replaceChildren();
      for (const job of jobs) {
        const card = node('article'); card.className = 'card';
        card.append(node('h3', job.label || '实验课候补'));
        card.append(node('p', `${job.status_label} · 已检查 ${job.checks} 次 · 已尝试 ${job.attempts} 次`));
        card.append(node('p', job.message));
        if (job.next_check) card.append(node('p', `下次检查：${new Date(job.next_check * 1000).toLocaleString()}`));
        if (job.is_active || job.status === 'paused') card.append(button('取消候补', async (event) => {
          const cancelButton = event.currentTarget;
          cancelButton.disabled = true;
          try {
            await api(`waitlist/${encodeURIComponent(job.id)}/cancel`, null, {});
            await loadWaitlist();
          } catch (error) {status.textContent = error.message; cancelButton.disabled = false;}
        }));
        container.append(card);
      }
      status.textContent = jobs.length ? '任务状态已更新' : '暂无候补任务。在实验分组或时间段旁点击加入候补。';
    } catch (error) {status.textContent = error.message;}
    finally {loadingWaitlist = false;}
  }
  document.getElementById('lab-waitlist-refresh').addEventListener('click', loadWaitlist);
  loadWaitlist();
  setInterval(() => {if (!document.hidden) loadWaitlist();}, 10000);
})();
