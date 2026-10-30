/* Dream settings: serialized, revision-bound autosaves; the backend owns cron. */
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'dream-dialog';
  dialog.setAttribute('aria-labelledby', 'dream-title');
  dialog.innerHTML = `<header><div><h2 id="dream-title">Dream</h2><p class="dream-muted">夜间记忆审校</p></div><button id="dream-close" aria-label="Close Dream">×</button></header>
    <p id="dream-message" role="status"></p>
    <div class="dream-scroll"><details id="dream-report" hidden><summary>运行结果 · 技术详情</summary><pre id="dream-report-text"></pre></details>
    <section id="dream-agents" aria-label="Dream Agents">
      <div class="dream-workspace"><div class="dream-table-wrap"><table><thead><tr><th>Agent</th><th>计划</th><th>每日</th><th>检查</th><th id="dream-run-heading"></th></tr></thead><tbody id="dream-rows"></tbody></table></div><section id="dream-detail" aria-label="Agent settings"><p class="dream-empty">选择一个 Agent<span>查看提示词模块与最终提示词</span></p></section></div>
    </section>
    </div>`;
  document.body.append(dialog);
  const $ = id => dialog.querySelector('#' + id);
  const text = (tag, value, cls) => { const e = document.createElement(tag); e.textContent = value ?? ''; if (cls) e.className = cls; return e; };
  // Unmodified Heroicons v2.2.0 outline/bolt path; MIT, notices/HEROICONS-LICENSE.
  const bolt = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" aria-hidden="true" data-slot="icon"><path stroke-linecap="round" stroke-linejoin="round" d="m3.75 13.5 10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75Z"/></svg>';
  const runAll = document.createElement('button'); runAll.id = 'dream-run-all'; runAll.innerHTML = bolt; runAll.setAttribute('aria-label','Run all enabled Dream tasks'); runAll.title = '立即运行所有已启用的 Dream 任务'; $('dream-run-heading').append(runAll);
  let snapshot, rows = [], modules = [], active = null, dirty = false, busy = false, previewEpoch = 0, triggering = false;
  const submitted = new Set(), pendingProfiles = new Set();
  const runnable = r => !!r?.enabled && !!r.job_id && !r.error && !r.problem && !submitted.has(r.profile);
  const savedRows = () => (snapshot?.rows || []).filter(runnable);
  const message = value => { $('dream-message').textContent = value; };
  const signature = row => JSON.stringify([!!row.enabled,row.time || null,row.modules || [],!!row.replace_prompt]);
  const saved = row => snapshot?.rows.find(r => r.profile === row.profile);
  const timeOK = value => /^([01]\d|2[0-3]):[0-5]\d$/.test(value || '');
  let saving = false;
  const states = new Map();
  const state = row => states.get(row.profile);
  const unsettled = () => saving || rows.some(r => r.draft || state(r)?.timer || state(r)?.error);
  const sameModules = (a,b) => JSON.stringify(a || []) === JSON.stringify(b || []);
  function status(row) {
    const s = state(row);
    return s.error ? '未保存 · 失败' : s.saving || s.timer || row.draft && validPayload(row) ? '保存中…' : row.draft ? '未保存' : '已保存';
  }
  function syncStatus(row) {
    const tr = [...$('dream-rows').children].find(e => e.dataset.profile === row.profile);
    if (tr) {
      const label = tr.querySelector('.dream-save-state'); label.textContent = status(row);
      label.classList.toggle('dream-draft',!!row.draft || !!state(row).error);
      tr.querySelector('.dream-retry').hidden = !state(row).error;
      tr.querySelector('.dream-save-error').textContent = state(row).error || '';
      const check = tr.querySelector('.dream-health'); check.title = health(row);
      check.textContent = row.error || row.problem ? '需检查' : !row.job_id ? '未创建' : row.heartbeat_age == null ? '未知' : row.heartbeat_age < 120 ? '有心跳' : '心跳较早';
    }
    if (active === row.profile && $('dream-draft-state')) $('dream-draft-state').textContent = status(row);
  }
  // Invalid/incomplete time remains a local draft, never poisoning a switch save.
  function payload(row) {
    const base = saved(row);
    return {...row, time:timeOK(row.time) ? row.time : base.time,
      replace_prompt:!sameModules(row.modules,base.modules)};
  }
  function validPayload(row) { return valid(payload(row)) && signature(payload(row)) !== signature(saved(row)); }
  function valid(row) {
    if (!row || row.error || (row.problem && row.enabled)) return false;
    const saved = snapshot?.rows.find(r => r.profile === row.profile);
    const sameModules = JSON.stringify(row.modules) === JSON.stringify(saved?.modules);
    return ((row.time === null && saved?.time === null) || /^([01]\d|2[0-3]):[0-5]\d$/.test(row.time || '')) &&
      (!row.job_id || row.replace_prompt || sameModules) &&
      ((row.job_id && !row.replace_prompt) || (row.modules?.length && row.modules.every(id => modules.some(m => m.id === id))));
  }
  function activate(row) {
    if (active === row.profile) return;
    active = row.profile; dialog.querySelectorAll('[data-profile]').forEach(tr => { tr.classList.toggle('is-active',tr.dataset.profile === active); tr.querySelector('button').setAttribute('aria-expanded',String(tr.dataset.profile === active)); });  renderDetail(); updateChrome();
  }
  const changed = row => {
    row.draft = signature(row) !== signature(snapshot.rows.find(r => r.profile === row.profile));
    dirty = rows.some(r => r.draft); previewEpoch++;
    if ($('dream-preview')) { $('dream-preview').open = false; $('dream-prompt').textContent = ''; }
    // Never remount editable controls during autosave or typing.
    syncStatus(row);
    updateChrome();
  };
  function schedule(row, delay = 0) {
    const s = state(row); clearTimeout(s.timer); s.timer = null;
    changed(row);
    if (!s.error && validPayload(row)) s.timer = setTimeout(() => { s.timer = null; s.ready = true; pump(); },delay);
    syncStatus(row); updateChrome();
  }
  async function pump() {
    if (saving || busy) return;
    const row = rows.find(r => state(r).ready && !state(r).error && validPayload(r));
    if (!row) return;
    const s = state(row), sent = payload(row);
    s.ready = false; s.saving = true; saving = true; syncStatus(row); updateChrome();
    try {
      const result = await post('apply',{library_version:snapshot.library_version,changes:[{profile:row.profile,version:row.version,enabled:sent.enabled,time:sent.time,modules:sent.modules,replace_prompt:sent.replace_prompt}]});
      const outcome = result.results.find(r => r.profile === row.profile);
      if (!outcome?.ok) throw new Error(outcome?.error || '保存结果无法核实');
      const actual = await api('/api/dream'), fresh = actual.rows.find(r => r.profile === row.profile);
      if (!fresh || fresh.error || fresh.enabled !== sent.enabled || fresh.time !== sent.time || !sameModules(fresh.modules,sent.modules)) throw new Error('保存后核对不一致，请重新打开核对实际状态');
      // Advance this revision only after readback; retain edits made in flight.
      const edits = {enabled:row.enabled,time:row.time,modules:row.modules};
      snapshot.rows = snapshot.rows.map(r => r.profile === row.profile ? fresh : r);
      Object.assign(row,fresh,edits);
      row.replace_prompt = !sameModules(row.modules,fresh.modules);
      changed(row);
    } catch(e) { s.error = e.message; s.ready = false; }
    finally {
      s.saving = false; saving = false; changed(row);
      if (!s.error && validPayload(row) && !s.timer) s.ready = true;
      syncStatus(row); updateChrome(); pump();
    }
  }
  async function post(action, data) { return api('/api/dream/' + action, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}); }
  function updateChrome() {
    runAll.disabled = busy || unsettled() || !savedRows().length;
    runAll.innerHTML = triggering ? '<span aria-hidden="true">…</span>' : bolt;
    runAll.setAttribute('aria-busy',String(triggering));
    runAll.title = unsettled() ? '请等待设置保存并核对；未保存或无效设置需先处理' : '立即运行所有已保存且启用的计划';
    dialog.querySelectorAll('.dream-run').forEach(button => {
      const row = rows.find(r => r.profile === button.dataset.runProfile);
      const saved = snapshot?.rows.find(r => r.profile === row.profile);
      button.disabled = busy || unsettled() || !runnable(saved);
      const pending = pendingProfiles.has(row.profile);
      button.setAttribute('aria-busy',String(pending)); button.innerHTML = pending ? '<span aria-hidden="true">…</span>' : bolt;
      button.title = unsettled() ? '请等待设置保存并核对；未保存或无效设置需先处理' : submitted.has(row.profile) ? '已提交或结果未知；请在 Hermes 中核对，勿重复运行' : runnable(saved) ? '立即运行已保存的计划' : '仅已保存且启用的计划可以运行';
    });
  }
  function setBusy(value) { busy = value; dialog.querySelectorAll('button,input,select,textarea').forEach(e => e.disabled = value || (!snapshot && e.id !== 'dream-close')); if (!value) { renderRows(); renderDetail(); } updateChrome(); }

  function plainProblem(value) {
    if (!value) return '';
    const files = ['USER','MEMORY','SOUL','AGENTS'].filter(n => value.includes(n));
    if (/Neither USER nor MEMORY exists/.test(value)) return '尚无 USER 或 MEMORY，无法启用';
    if (files.length && /cannot be read/.test(value)) return files.join('、') + ' 无法读取，请检查权限';
    if (/disabled/i.test(value)) return 'USER 或 MEMORY 已在配置中停用';
    if (files.length && /missing|not found|no such|unavailable|exist|read/i.test(value)) return files.join('、') + ' 文件缺失或无法读取';
    if (/binding|generation|canonical|symlink/i.test(value)) return '文件或工作区绑定需要核对';
    if (/adoption requires/i.test(value)) return '尚未绑定现有 Dream 计划';
    if (/job missing/i.test(value)) return '原计划已不存在，需要核对';
    return '设置需要检查，请查看技术详情';
  }
  function health(row) {
    if (row.error || row.problem) return plainProblem(row.error || row.problem);
    if (!row.job_id) return '尚未创建';
    if (row.heartbeat_age == null) return '调度器状态未知';
    return row.heartbeat_age < 120 ? '调度器有心跳' : '调度器心跳较早';
  }
  function renderRows() {
    $('dream-rows').replaceChildren(...rows.map(row => {
      const tr = document.createElement('tr'); tr.dataset.profile = row.profile; tr.classList.toggle('is-active', active === row.profile);
      tr.onclick = e => { if (!busy && !e.target.closest('button,input,select,textarea,label,a,summary')) activate(row); };
      const name = text('td',''), select = text('button',row.profile,'dream-agent-name');
      select.setAttribute('aria-label','Settings ' + row.profile); select.setAttribute('aria-expanded',String(active === row.profile)); select.disabled = busy;
      const label = text('small',status(row),'dream-save-state dream-saved'); label.setAttribute('role','status');
      const retry = text('button','重试','dream-retry'); retry.hidden = !state(row).error;
      retry.setAttribute('aria-label','Retry save ' + row.profile);
      retry.onclick = () => { if (!validPayload(row)) { state(row).error = '请先修正无效输入；若已恢复原设置，请关闭后重新打开核对实际状态。'; syncStatus(row); return; } state(row).error = ''; schedule(row); };
      select.onclick = () => activate(row); name.append(select,label,retry,text('small',state(row).error || '','dream-save-error'));
      const statusCell = text('td',''), enable = document.createElement('input'); enable.type = 'checkbox'; enable.setAttribute('role','switch'); enable.setAttribute('aria-label','Enable ' + row.profile); enable.checked = row.enabled; enable.disabled = busy || !!row.error || (!!row.problem && !row.enabled);
      enable.onchange = () => { activate(row); row.enabled = enable.checked; enable.disabled = !!row.problem && !row.enabled; schedule(row); }; statusCell.append(enable);
      const daily = text('td',''), time = document.createElement('input'); time.type = 'text'; time.className = 'dream-time'; time.inputMode = 'numeric'; time.maxLength = 5; time.placeholder = 'HH:MM'; time.value = row.time || ''; time.setAttribute('aria-label','Nightly time ' + row.profile); time.disabled = busy || !!row.error;
      const reason = text('small','','dream-time-error'); reason.id = 'dream-time-error-' + rows.indexOf(row); time.setAttribute('aria-describedby',reason.id);
      const validateTime = () => { const ok = (row.time === null && snapshot.rows.find(r => r.profile === row.profile)?.time === null) || /^([01]\d|2[0-3]):[0-5]\d$/.test(row.time || ''); time.setAttribute('aria-invalid',String(!ok)); reason.textContent = ok ? '' : '请输入 00:00–23:59'; };
      time.onfocus = () => activate(row);
      time.oninput = () => { row.time = time.value; validateTime(); schedule(row,500); };
      time.onblur = () => { if (timeOK(row.time)) schedule(row); };
      time.onkeydown = e => { if (e.key === 'Enter' && timeOK(row.time)) { e.preventDefault(); schedule(row); } };
      validateTime(); daily.append(time,reason);
      const check = text('td',row.error || row.problem ? '需检查' : !row.job_id ? '未创建' : row.heartbeat_age == null ? '未知' : row.heartbeat_age < 120 ? '有心跳' : '心跳较早','dream-health'); check.title = health(row);
      const run = text('td',''), button = text('button','','dream-run'); button.innerHTML = bolt; button.dataset.runProfile = row.profile; button.setAttribute('aria-label','Run Dream ' + row.profile); button.onclick = () => { if (!button.disabled) runNow([snapshot.rows.find(r => r.profile === row.profile)]); }; run.append(button);
      tr.append(name,statusCell,daily,check,run); return tr;
    }));
    updateChrome();
  }
  function moduleChecks(container, ids, onChange, prefix) {
    container.replaceChildren(...modules.map(m => {
      const label = text('label',''), input = document.createElement('input'); input.type = 'checkbox'; input.value = m.id; input.checked = ids.includes(m.id); input.setAttribute('aria-label',prefix + m.name); input.onchange = () => onChange([...container.querySelectorAll('input:checked')].map(e => e.value));
      const copy = text('span',''); copy.append(text('strong',m.name),text('small',m.text)); label.append(input,copy); return label;
    }));
  }
  function renderDetail() {
    const row = rows.find(r => r.profile === active), panel = $('dream-detail');
    previewEpoch++;
    if (!row) { panel.innerHTML = '<p class="dream-empty">选择一个 Agent<span>查看提示词模块与最终提示词</span></p>'; return; }
    panel.innerHTML = `<div class="dream-section-heading"><h3 id="dream-detail-title" tabindex="-1"></h3><span id="dream-draft-state" class="dream-muted"></span></div><div id="dream-detail-body"></div>`;
    $('dream-detail-title').textContent = row.profile;
    $('dream-draft-state').textContent = status(row);
    const body = $('dream-detail-body');
    const issue = row.error || row.problem;
    if (issue) { body.append(text('p',plainProblem(issue),'dream-issue')); body.append(text('p',issue,'dream-error-reason')); }
    if (!row.error) {
      const missing = ['user','memory'].filter(k => row.files?.[k]?.state === 'missing');
      if (missing.length === 1) body.append(text('p',missing[0].toUpperCase() + ' 缺失，将跳过；仅审校已有记忆。','dream-muted'));
      for (const key of ['soul','agents']) if (row.files?.[key]?.state === 'unreadable') body.append(text('p',key.toUpperCase() + ' 参考文件无法读取：' + row.files[key].error,'dream-muted'));
      if (row.prompt_notice) body.append(text('p',row.prompt_notice,'dream-muted'));
      body.append(text('h4','提示词模块'));
      const checks = text('div','','dream-checks'), warning = text('p','','dream-issue'); warning.id = 'dream-module-error';
      warning.textContent = row.modules?.length ? '' : '请至少选择一个提示词模块；尚未保存';
      moduleChecks(checks,row.modules || [],ids => { row.modules = ids; row.replace_prompt = !sameModules(ids,saved(row).modules); warning.textContent = ids.length ? '' : '请至少选择一个提示词模块；尚未保存'; schedule(row); },'Module ' + row.profile + ' '); body.append(checks,warning);
      if ((row.modules || []).some(id => !modules.some(m => m.id === id))) body.append(text('p','有引用的模块已删除。原提示词保留；重新选择模块后再替换。','dream-issue'));
      const details = document.createElement('details'); details.id = 'dream-preview'; details.append(text('summary','最终提示词'),text('pre','','dream-prompt')); details.lastChild.id = 'dream-prompt'; body.append(details);
      details.ontoggle = () => { if (!details.isConnected) return; if (details.open) showPreview(row); else { previewEpoch++; details.lastChild.textContent = ''; } };
    }
  }

  async function showPreview(row) {
    const epoch = ++previewEpoch; $('dream-prompt').textContent = '正在生成…';
    try { const result = (!row.job_id || row.replace_prompt) ? await post('preview',{profile:row.profile,modules:row.modules}) : {prompt:row.prompt}; if (epoch === previewEpoch && $('dream-preview')?.open) $('dream-prompt').textContent = result.prompt; } catch(e) { if (epoch === previewEpoch && $('dream-preview')?.open) $('dream-prompt').textContent = '无法生成提示词：' + e.message; }
  }
  async function runNow(targetRows) {
    if (busy || unsettled() || !targetRows.length || targetRows.some(r => !runnable(r))) return;
    if (!confirm('立即运行 ' + targetRows.length + ' 个已保存且启用的 Dream 任务？\n' + targetRows.map(r => '• ' + r.profile).join('\n') + '\n任务可能实际修改各自的 USER.md / MEMORY.md。\n未保存草稿不会应用，每日计划时间保持不变。\n收到提交确认不代表任务已完成。')) return;
    targetRows.forEach(r => pendingProfiles.add(r.profile));
    triggering = true; setBusy(true); message('正在提交 Dream 任务…');
    try {
      const result = await post('trigger',{targets:targetRows.map(r => ({profile:r.profile,job_id:r.job_id,version:r.version}))});
      for (const r of result.results) if (r.status !== 'failed') submitted.add(r.profile);
      message(result.results.map(r => r.profile + ': ' + (r.status === 'accepted' ? '已提交，尚未确认完成' : r.status === 'unknown' ? '提交结果未知，请核对，勿自动重试' : '未提交 — ' + r.error)).join('\n'));
      $('dream-report').hidden = false; $('dream-report').open = false; $('dream-report-text').textContent = JSON.stringify(result.results,null,2);
    } catch(e) { targetRows.forEach(r => submitted.add(r.profile)); message('提交结果无法核实：' + e.message + ' · 请在 Hermes 中核对，勿重复运行。'); }
    finally { pendingProfiles.clear(); triggering = false; setBusy(false); }
  }
  runAll.onclick = () => runNow(savedRows());
  async function refresh() { snapshot = await api('/api/dream'); rows = snapshot.rows.map(r => ({...r})); modules = snapshot.modules; states.clear(); rows.forEach(r => states.set(r.profile,{timer:null,ready:false,saving:false,error:''})); dirty = false; active = null; renderRows(); renderDetail(); updateChrome(); }
  function close() {
    if (busy) return;
    rows.forEach(row => { const s = state(row); if (s.timer) { clearTimeout(s.timer); s.timer = null; s.ready = true; } });
    pump();
    if (saving || rows.some(r => state(r).ready && !state(r).error && validPayload(r))) { message('正在保存并核对，请稍候再关闭。'); return; }
    if ((dirty || rows.some(r => state(r).error)) && !confirm('仍有未保存或保存失败的 Dream 修改。关闭将放弃这些修改；已保存的设置不会撤销。继续关闭？')) return;
    dirty = false; rows = []; states.clear(); message(''); dialog.close(); previewEpoch++;
  }
  window.addEventListener('beforeunload',e => { if (dirty || unsettled()) { e.preventDefault(); e.returnValue = ''; } });
  document.getElementById('dream-open').onclick = async () => { if (dialog.open) return; snapshot = null; rows = []; modules = []; active = null; dirty = false; renderRows(); renderDetail(); $('dream-report').hidden = true; $('dream-report').open = false; $('dream-report-text').textContent = ''; dialog.showModal(); setBusy(true); message('正在读取设置…'); try { await refresh(); message(''); } catch(e) { message(e.message); } finally { setBusy(false); } };
  $('dream-close').onclick = close; dialog.addEventListener('cancel',e => { e.preventDefault(); close(); });

})();
