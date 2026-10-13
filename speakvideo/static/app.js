'use strict';

/* SpeakVideo 前端。所有接口与原版一致，只重构界面与交互。 */

const $ = id => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let current = '', data = null, config = {}, voices = {}, referenceId = '', dirty = false;
let booted = false, projectsCache = [], dragged = null, taskStatus = new Map(), activeTab = 'script', lastTasks = [];
let voiceAssets = [], selectedAsset = null, llmDraft = null, connectionBusy = false;
const previewTasks = new Map();
const previewAudio = new Map();
let recorder = null, micStream = null, recordingTimer = null, recordingDiscard = false, recordingStarted = 0;

const TABS = ['script', 'binding', 'production'];
const TASK_LABELS = {brief: '生成阐述', generate: '生成剧本', synth: '合成配音', retry: '重试单行', merge: '合并导出', run: '完整工作流', design: '试音', create: '固化音色', sync: '同步音色', preview: '音色试听', llm_test: 'LLM 连接测试'};
const TASK_STATES = {queued: '排队中', running: '执行中', succeeded: '完成', failed: '失败', paused: '已暂停', interrupted: '被中断'};
const LINE_STATES = {succeeded: '已合成', failed: '失败', running: '合成中', pending: '待合成'};

const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
const fmtBytes = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? Math.round(n / 1024) + ' KB' : (n || 0) + ' B';
const fmtTime = s => { s = Number(s) || 0; const m = Math.floor(s / 60); return m ? `${m} 分 ${Math.round(s % 60)} 秒` : `${s.toFixed(1)} 秒`; };

/* ───────── 基础 ───────── */
function toast(message, isError = false) {
  const el = $('toast');
  el.textContent = message; el.classList.toggle('error', isError); el.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.hidden = true, isError ? 8000 : 4000);
}
async function api(path, method = 'GET', body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body instanceof FormData ? {} : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch { throw Error('无法连接本机工作台。请双击 start.command 启动服务，然后刷新页面。'); }
  const result = await response.json().catch(() => ({detail: `本机服务返回 HTTP ${response.status}，请重试或检查工作台服务。`}));
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
  return result;
}
async function safe(fn) { try { return await fn(); } catch (error) { toast(error.message, true); } }
async function queue(path, body) { const task = await api(path, 'POST', body); taskStatus.set(task.id, 'queued'); await pollTasks(); return task; }
const url = suffix => `/api/projects/${encodeURIComponent(current)}${suffix}`;
const speakerIndex = slot => Math.max(0, data.script.cast.findIndex(c => c.slot === slot)) % 4 + 1;
const speakerVar = slot => `--speaker: var(--s${speakerIndex(slot)})`;

function setSaveState(text, isDirty = false) { $('saveState').textContent = text; $('saveState').classList.toggle('dirty', isDirty); }
function markDirty() { dirty = true; setSaveState('未保存', true); renderSteps(); }

/* ───────── 左栏：工程与音色 ───────── */
function renderProjects() {
  const q = $('projectFilter').value.trim().toLowerCase();
  const list = projectsCache.filter(p => !q || p.name.toLowerCase().includes(q) || (p.topic || '').toLowerCase().includes(q));
  $('projectFilter').hidden = projectsCache.length < 6;
  $('projects').innerHTML = list.map(p => `<button class="project ${p.name === current ? 'selected' : ''}" data-project="${escape(p.name)}"><b>${escape(p.name)}</b><small>${escape(p.topic || '未填写话题')}</small></button>`).join('')
    || `<div class="empty">${projectsCache.length ? '没有匹配的工程' : '还没有工程'}</div>`;
  $$('[data-project]', $('projects')).forEach(b => b.onclick = () => safe(() => selectProject(b.dataset.project)));
}
async function inventory() {
  voices = await api('/api/voices');
  $('voiceInventory').innerHTML = Object.entries(voices).map(([role, emotions]) =>
    `<button class="voice" data-voice-role="${escape(role)}" title="点击管理 ${escape(role)}"><span>${escape(role)}</span><small>${emotions.map(escape).join(' · ')}</small></button>`).join('')
    || '<div class="empty">还没有音色，点 ＋ 去音色工厂创建。</div>';
  $$('[data-voice-role]', $('voiceInventory')).forEach(b => b.onclick = () => safe(() => openLibrary(b.dataset.voiceRole)));
  projectsCache = await api('/api/projects');
  renderProjects();
}

/* ───────── 工程加载 ───────── */
function defaultTab() {
  const a = data.workflow?.approvals || {};
  if (!a.script) return 'script';
  if (!a.binding) return 'binding';
  return 'production';
}
async function selectProject(name, {keepTab = false} = {}) {
  if (name !== current && dirty && !confirm('有未保存修改，仍要切换工程吗？')) return;
  const switching = name !== current;
  current = name; data = await api(url('')); dirty = false;
  data.workflow ||= {}; data.workflow.approvals ||= {};

  $('welcome').hidden = true; $('workspace').hidden = false; $('steps').hidden = false; $('save').hidden = false;
  $('projectName').textContent = current;
  $('title').textContent = data.script.meta.topic || '未命名对话';
  $('topic').value = data.script.meta.topic; $('keywords').value = data.script.meta.keywords.join(', ');
  $('guidance').value = data.script.meta.guidance || ''; $('brief').value = data.script.meta.brief;
  setSaveState('已保存');

  $('personas').innerHTML = data.script.cast.map(m =>
    `<label class="field" style="${speakerVar(m.slot)}"><span><span class="dot" style="background:var(--speaker)"></span>${escape(m.slot)} 人设</span><input data-persona="${escape(m.slot)}" value="${escape(m.persona)}"></label>`).join('');
  $$('input', $('personas')).forEach(input => input.oninput = () => { data.script.cast.find(m => m.slot === input.dataset.persona).persona = input.value; markDirty(); });

  renderLines(); renderBindings(); renderReport(data.preflight); renderMedia(); updateExports();
  const requested = new URLSearchParams(location.search).get('tab');
  let tab = defaultTab();
  if (keepTab && !switching) tab = activeTab;
  else if (!booted && TABS.includes(requested)) tab = requested;
  booted = true;
  switchTab(tab);
  await inventory(); renderTasks();
}

/* ───────── 步骤 ───────── */
function switchTab(id) {
  activeTab = id;
  $$('.tab').forEach(e => e.hidden = e.id !== id);
  renderSteps();
  const link = new URL(location.href); link.searchParams.set('project', current); link.searchParams.set('tab', id); history.replaceState(null, '', link);
  $('workspace').parentElement.scrollTop = 0;
  growAll();
}
function renderSteps() {
  const a = data?.workflow?.approvals || {};
  const doneMap = {script: a.script, binding: a.binding, production: a.audio};
  $$('[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === activeTab);
    b.classList.toggle('done', !!doneMap[b.dataset.tab] && !dirty);
  });
}

/* ───────── 台词 ───────── */
function options(values, selected, placeholder, labelled = false) {
  return (placeholder ? `<option value="">${escape(placeholder)}</option>` : '') +
    values.map(v => `<option value="${escape(v)}" ${v === selected ? 'selected' : ''}>${escape(labelled && config.moods?.[v] ? config.moods[v] : v)}</option>`).join('');
}
function growAll() { requestAnimationFrame(() => $$('.line textarea').forEach(autoGrow)); }
function autoGrow(el) {
  if (!el.offsetParent) return; el.style.height = 'auto'; el.style.height = el.scrollHeight + 2 + 'px'; }
function lineStatus(id) { return data.manifest?.lines?.[id]?.status || 'pending'; }

function renderLineStats() {
  const lines = data.script.lines;
  const done = lines.filter(l => lineStatus(l.id) === 'succeeded').length;
  const chars = lines.reduce((n, l) => n + l.words.length, 0);
  $('lineStats').textContent = lines.length ? `${lines.length} 行 · ${chars} 字 · 已合成 ${done}` : '';
}
function renderLines() {
  const lines = data.script.lines;
  const slots = data.script.cast.map(c => c.slot);
  renderLineStats();
  if (!lines.length) {
    $('lines').innerHTML = '<div class="empty-lines">还没有台词。按左侧阐述生成剧本，或手动添加一行。</div>';
    return;
  }
  $('lines').innerHTML = lines.map(line => {
    const status = lineStatus(line.id);
    const missing = !data.cast[line.slot]?.mood_map[line.mood];
    return `<div class="line" data-id="${escape(line.id)}" style="${speakerVar(line.slot)}">
      <span class="handle" draggable="true" title="拖动排序">⠿</span>
      <div class="line-body">
        <div class="line-bar">
          <select data-field="slot" class="slot" aria-label="角色">${options(slots, line.slot)}</select>
          <select data-field="mood" class="mood ${missing ? 'bad' : ''}" title="${missing ? '该情绪尚未映射到音色' : '情绪'}" aria-label="情绪">${options(Object.keys(config.moods || {}), line.mood, '', true)}</select>
          <span class="pause" title="句后停顿（毫秒）"><input data-field="pause_after_ms" type="number" min="0" max="10000" step="50" value="${line.pause_after_ms}" aria-label="句后停顿"></span>
          <span class="meta"><span class="status ${status}">${escape(LINE_STATES[status] || status)}</span><span>${escape(line.id)}</span></span>
          <span class="tools"><button class="icon-btn" data-retry title="重新合成这一行" aria-label="重试">↻</button><button class="icon-btn danger" data-delete title="删除这一行" aria-label="删除">✕</button></span>
        </div>
        <textarea data-field="words" rows="1" aria-label="台词" placeholder="输入台词">${escape(line.words)}</textarea>
      </div>
    </div>`;
  }).join('');

  $$('.line', $('lines')).forEach(row => {
    const id = row.dataset.id;
    const find = () => data.script.lines.find(l => l.id === id);
    $$('[data-field]', row).forEach(input => {
      input.oninput = () => {
        const field = input.dataset.field;
        find()[field] = field === 'pause_after_ms' ? Number(input.value) : input.value;
        if (field === 'words') autoGrow(input);
        markDirty();
        if (field === 'slot') { row.setAttribute('style', speakerVar(input.value)); renderBindings(); }
        if (field === 'mood') { input.classList.toggle('bad', !data.cast[find().slot]?.mood_map[input.value]); renderBindings(); }
        if (field === 'words') renderLineStats();
      };
    });
    autoGrow(row.querySelector('textarea'));
    row.querySelector('[data-delete]').onclick = () => { data.script.lines = data.script.lines.filter(l => l.id !== id); markDirty(); renderLines(); renderBindings(); };
    row.querySelector('[data-retry]').onclick = () => safe(() => action('retry', id));

    const handle = row.querySelector('.handle');
    handle.ondragstart = event => { dragged = id; event.dataTransfer.setData('text/plain', id); event.dataTransfer.setDragImage(row, 20, 20); row.classList.add('dragging'); };
    handle.ondragend = () => { row.classList.remove('dragging'); $$('.drop-target').forEach(e => e.classList.remove('drop-target')); };
    row.ondragover = event => { if (!dragged) return; event.preventDefault(); row.classList.add('drop-target'); };
    row.ondragleave = () => row.classList.remove('drop-target');
    row.ondrop = event => {
      event.preventDefault(); row.classList.remove('drop-target');
      if (!dragged || dragged === id) return;
      const from = data.script.lines.findIndex(l => l.id === dragged);
      const [moved] = data.script.lines.splice(from, 1);
      const to = data.script.lines.findIndex(l => l.id === id);
      data.script.lines.splice(from <= to ? to + 1 : to, 0, moved);
      dragged = null; markDirty(); renderLines();
    };
  });
}

/* ───────── 音色绑定 ───────── */
function renderBindings() {
  $('bindings').innerHTML = data.script.cast.map(member => {
    const binding = data.cast[member.slot] ||= {role: '', mood_map: {}, default_emotion: '平静'};
    const emotions = voices[binding.role] || [];
    const own = data.script.lines.filter(l => l.slot === member.slot);
    const moods = [...new Set(own.map(l => l.mood))];
    const mapped = moods.filter(m => emotions.includes(binding.mood_map[m])).length;
    return `<div class="binding" data-slot="${escape(member.slot)}" style="${speakerVar(member.slot)}">
      <div class="binding-head"><h3>${escape(member.slot.toUpperCase())}</h3><small>${escape(member.persona)}</small><span class="count ${mapped < moods.length ? 'bad' : 'good'}">${own.length} 行 · 已映射 ${mapped}/${moods.length}</span></div>
      <label class="field ${binding.role ? '' : 'bad'}">角色音色<select data-role>${options(Object.keys(voices), binding.role, '请选择角色')}</select></label>
      ${moods.length ? `<div class="moodgrid">${moods.map(m => `<label class="field ${emotions.includes(binding.mood_map[m]) ? '' : 'bad'}"><span>${escape(config.moods?.[m] || m)} <span class="muted">${escape(m)}</span></span><select data-mood="${escape(m)}">${options(emotions, binding.mood_map[m], '未映射')}</select></label>`).join('')}</div>` : '<p class="hint">这个角色还没有台词。</p>'}
    </div>`;
  }).join('');
  $$('.binding', $('bindings')).forEach(div => {
    const binding = data.cast[div.dataset.slot];
    div.querySelector('[data-role]').onchange = event => {
      binding.role = event.target.value; binding.mood_map = {};
      const emotions = voices[binding.role] || [];
      // 自动映射：情绪中文名与音色情感同名时直接绑定
      for (const l of data.script.lines.filter(l => l.slot === div.dataset.slot)) {
        const label = config.moods?.[l.mood];
        if (emotions.includes(label)) binding.mood_map[l.mood] = label;
      }
      markDirty(); renderBindings(); renderLines();
    };
    $$('[data-mood]', div).forEach(select => select.onchange = () => {
      if (select.value) binding.mood_map[select.dataset.mood] = select.value; else delete binding.mood_map[select.dataset.mood];
      markDirty(); renderBindings(); renderLines();
    });
  });
}

/* ───────── 保存 ───────── */
function collect() {
  data.script.meta.topic = $('topic').value;
  data.script.meta.keywords = $('keywords').value.split(/[,，]/).map(v => v.trim()).filter(Boolean);
  data.script.meta.guidance = $('guidance').value; data.script.meta.brief = $('brief').value;
}
async function save() {
  if (!data) return;
  collect(); setSaveState('保存中…');
  const result = await api(url(''), 'PUT', {script: data.script, cast: data.cast});
  dirty = false; setSaveState('已保存'); renderSteps();
  $('title').textContent = data.script.meta.topic || '未命名对话';
  renderReport(result.preflight);
}

/* ───────── 生成 ───────── */
function renderReport(report) {
  const issues = report.errors.map(v => `<div class="issue error">${escape(v)}</div>`).join('') + report.warnings.map(v => `<div class="issue warning">${escape(v)}</div>`).join('');
  $('report').innerHTML = `<div class="report-summary">
      <div><b>${report.line_count}</b><span>台词行</span></div>
      <div><b>${report.reusable}</b><span>可复用</span></div>
      <div class="verdict"><b class="${report.ok ? 'good' : 'bad'}">${report.ok ? '✓ 本地检查通过' : `✕ ${report.errors.length} 个问题待处理`}</b><span>${report.ok ? '可以开始合成' : '处理后再合成'}</span></div>
    </div>${issues}`;
  renderLineMap();
}
function renderLineMap() {
  if (!data) return;
  $('lineMap').innerHTML = data.script.lines.map(l => { const s = lineStatus(l.id); return `<i class="${s}" title="${escape(l.id)} · ${escape(LINE_STATES[s] || s)}：${escape(l.words.slice(0, 30))}"></i>`; }).join('');
}
function renderMedia() {
  const has = !!data.media.bgm;
  $('mediaState').textContent = has ? '已选择音乐' : '未选择';
  $('mediaState').classList.toggle('on', has);
  $('removeBgm').hidden = !has;
  $('bgmVolume').value = data.media.bgm_volume ?? 0.18;
  $('bgmVolumeOut').textContent = Math.round($('bgmVolume').value * 100) + '%';
}
function updateExports() {
  const meta = data.audio_export || {};
  const has = !!meta.files?.['master.mp3'];
  $('masterAudio').hidden = !has;
  $('audioState').innerHTML = has
    ? `${meta.mode === 'demo' ? '测试音频 · ' : ''}${fmtTime(meta.duration)} · ${data.audio_current ? '<span class="good">与当前工程一致</span>' : '<span class="stale">工程、音色或混音已变化，请重新导出</span>'}`
    : '';
  if (has) {
    const source = url('/files/exports/' + meta.files['master.mp3'].path);
    if ($('masterAudio').getAttribute('src') !== source) $('masterAudio').src = source;
    $('exports').innerHTML = Object.entries(meta.files).map(([name, file]) => `<a href="${url('/files/exports/' + file.path)}" download>↓ ${escape(name)} <small>${fmtBytes(file.bytes)}</small></a>`).join('');
  } else {
    $('exports').innerHTML = '<p class="hint">还没有导出音频。先合成配音，再点“合并并导出”。</p>';
  }
}
async function action(type, lineId) {
  if (type === 'generate' && data.script.lines.length && !confirm('重新生成将替换全部台词并清空复用记录，继续吗？')) return;
  await save();
  const task = await queue(url('/actions'), {action: type, line_id: lineId || null});
  toast(`已加入队列：${TASK_LABELS[task.label] || task.label}`);
}
async function approval(gate) {
  await save();
  await api(url(`/approve/${gate}`), 'POST', {});
  data.workflow.approvals[gate] = true;
  toast({script: '剧本已定稿', binding: '音色绑定已确认', audio: '完整音频已确认'}[gate]);
  if (gate === 'script') switchTab('binding'); else if (gate === 'binding') switchTab('production'); else renderSteps();
}

/* ───────── 任务 ───────── */
function renderTasks() {
  const tasks = [...lastTasks].reverse();
  tasks.sort((a, b) => (b.project === current) - (a.project === current));
  const active = lastTasks.filter(t => ['queued', 'running'].includes(t.status));
  $('taskBadge').textContent = active.length ? `(${active.length})` : '';
  const busy = active.some(t => t.project === current);
  $$('[data-action]').forEach(b => b.disabled = busy);

  $('tasks').innerHTML = tasks.slice(0, 20).map(t => {
    const p = t.progress;
    const pct = p && p.total ? Math.round(p.completed / p.total * 100) : 0;
    const msg = t.error || t.result || '';
    return `<div class="task ${t.project && t.project !== current ? 'other' : ''}">
      <div class="task-top"><b>${escape(TASK_LABELS[t.label] || t.label)}</b><span class="task-state ${t.status}">${escape(TASK_STATES[t.status] || t.status)}</span></div>
      <div class="task-sub">${escape(t.project || '音色工厂')}${p ? ` · ${p.completed}/${p.total} 行${p.current_line ? ' · 正在生成 ' + escape(p.current_line) : ''}` : ''}</div>
      ${p && t.status === 'running' ? `<div class="progress"><i style="width:${pct}%"></i></div>` : ''}
      ${msg ? `<div class="task-msg ${t.status === 'failed' ? 'bad' : ''}">${escape(msg)}</div>` : ''}
      ${['queued', 'running'].includes(t.status) ? `<button class="btn sm" data-pause="${escape(t.id)}">暂停</button>` : ''}
    </div>`;
  }).join('') || '<div class="empty">暂无任务。生成剧本、合成或导出时会显示在这里。</div>';
  $$('[data-pause]', $('tasks')).forEach(b => b.onclick = () => safe(async () => {
    await api(`/api/tasks/${b.dataset.pause}/pause`, 'POST', {});
    toast('已请求暂停；正在进行的请求或 ffmpeg 会在当前步骤完成后停止。');
  }));
}
async function pollTasks() {
  const tasks = await api('/api/tasks');
  lastTasks = tasks;
  renderTasks();
  let refresh = false;
  for (const t of tasks) {
    const prev = taskStatus.get(t.id);
    if (taskStatus.has(t.id) && prev !== t.status) {
      if (t.project === current && ['succeeded', 'failed', 'paused'].includes(t.status)) refresh = true;
      if (t.status === 'failed') toast(`${TASK_LABELS[t.label] || t.label}失败：${t.error || ''}`, true);
      if (t.status === 'succeeded' && !t.project && t.label === 'design') {
        referenceId = t.result;
        $('voicePreview').src = `/api/previews/${referenceId}`; $('voicePreview').hidden = false;
        setRef('试音已生成。满意的话点“固化为 pt 音色”，台词须与试音一致。', true);
      }
      if (t.status === 'succeeded' && !t.project && t.label === 'create') { await inventory(); if (data) renderBindings(); toast('音色已保存到 Mac voices/'); }
      if (t.label === 'preview' && t.status === 'succeeded') {
        const target = previewTasks.get(t.id);
        if (target) previewAudio.set(JSON.stringify([target.role,target.emotion]), {sha256: target.sha256, url: `/api/previews/${t.result}`});
        if (target && selectedAsset?.role === target.role && selectedAsset?.emotion === target.emotion && selectedAsset.sha256 === target.sha256) { $('assetPreview').src = `/api/previews/${t.result}`; $('assetPreview').hidden = false; }
        else if (target) toast(`${target.role}/${target.emotion} 试听已完成；请回到该条目试听。`);
      }
      if (t.label === 'llm_test' && ['succeeded', 'failed'].includes(t.status)) { $('llmTestState').textContent = t.result || t.error; $('llmTestState').classList.toggle('ready', t.status === 'succeeded'); }
    }
    taskStatus.set(t.id, t.status);
  }
  // 运行中也同步行状态，让进度点实时变化
  const running = tasks.some(t => t.project === current && t.status === 'running');
  if ((refresh || running) && !dirty && current) {
    if (refresh) await selectProject(current, {keepTab: true});
    else { data = {...data, ...(await api(url('')))}; data.workflow.approvals ||= {}; renderLineMap(); renderLineStats(); $$('.line').forEach(row => { const s = lineStatus(row.dataset.id); const el = row.querySelector('.status'); el.className = `status ${s}`; el.textContent = LINE_STATES[s] || s; }); }
  }
}

/* ───────── 音色工厂 ───────── */
function setRef(text, ready = false) { $('referenceState').textContent = text; $('referenceState').classList.toggle('ready', ready); }
function voiceBody(kind) { return {action: kind, role: $('voiceRole').value, emotion: $('voiceEmotion').value, text: $('voiceText').value, instruct: $('voiceDescription').value, reference_id: referenceId, overwrite: $('voiceOverwrite').checked}; }
function openFactory() { $('factoryDialog').showModal(); }

async function useReference(file, label) {
  const f = new FormData(); f.append('file', file);
  const result = await api('/api/voices/reference', 'POST', f);
  referenceId = result.reference_id;
  $('voicePreview').src = result.preview_url; $('voicePreview').hidden = false;
  setRef(`${label} · ${result.duration} 秒。请填写音频中实际说出的台词。`, true);
}
function releaseMic() {
  clearInterval(recordingTimer); recordingTimer = null;
  micStream?.getTracks().forEach(t => t.stop()); micStream = null;
  $('recordVoice').disabled = false; $('recordVoice').hidden = false; $('stopRecording').hidden = true;
  $('voiceFile').disabled = false; $('designVoice').disabled = false; $('createVoice').disabled = false;
}
async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) throw Error('当前浏览器不支持直接录音，请在 Edge / Chrome / Safari 中打开 localhost 工作台。');
  $('recordVoice').disabled = true;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({audio: true});
    const mime = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(t => MediaRecorder.isTypeSupported(t));
    if (!$('factoryDialog').open) { releaseMic(); return; }
    const activeRecorder = new MediaRecorder(micStream, mime ? {mimeType: mime} : {});
    recorder = activeRecorder;
    const chunks = []; recordingDiscard = false;
    recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    recorder.onerror = () => { recordingDiscard = true; releaseMic(); toast('录音失败，原参考音频仍保留。', true); };
    recorder.onstop = () => {
      const type = activeRecorder.mimeType; releaseMic(); recorder = null;
      if (recordingDiscard) return;
      const ext = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm';
      $('recordingTime').textContent = '正在处理录音…';
      safe(async () => { await useReference(new File(chunks, `recording.${ext}`, {type}), '直接录音已就绪'); $('recordingTime').textContent = '录音已保存'; });
    };
    recorder.start(); recordingStarted = Date.now();
    $('recordVoice').hidden = true; $('stopRecording').hidden = false;
    $('voiceFile').disabled = true; $('designVoice').disabled = true; $('createVoice').disabled = true;
    $('recordingTime').textContent = '录音中 0 秒';
    recordingTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - recordingStarted) / 1000);
      $('recordingTime').textContent = `录音中 ${seconds} 秒 / 120 秒`;
      if (seconds >= 119 && recorder?.state === 'recording') recorder.stop();
    }, 250);
  } catch (error) {
    releaseMic();
    throw Error(error.name === 'NotAllowedError' ? '麦克风权限被拒绝，请在浏览器地址栏允许本站使用麦克风后重试。' : error.name === 'NotFoundError' ? '未找到可用麦克风。' : `无法开始录音：${error.message}`);
  }
}
function cancelRecording() {
  recordingDiscard = true;
  if (recorder?.state === 'recording') recorder.stop();
  else releaseMic();
}

/* ───────── PT 库 ───────── */
const assetURL = a => `/api/voices/assets/${encodeURIComponent(a.role)}/${encodeURIComponent(a.emotion)}`;
async function refreshLibrary() {
  voiceAssets = await api('/api/voices/assets'); renderVoiceAssets();
  await inventory(); if (data) { renderBindings(); renderLines(); }
}
async function openLibrary(role = '') {
  await refreshLibrary(); $('voiceSearch').value = role; renderVoiceAssets();
  selectVoiceAsset(voiceAssets.find(v => !role || v.role === role) || null);
  $('libraryDialog').showModal();
}
function renderVoiceAssets() {
  const q = $('voiceSearch').value.trim().toLowerCase();
  $('voiceAssets').innerHTML = voiceAssets.filter(a => `${a.role} ${a.emotion} ${a.notes}`.toLowerCase().includes(q)).map(a => `<button class="asset-item ${selectedAsset?.role === a.role && selectedAsset?.emotion === a.emotion ? 'selected' : ''}" data-asset-role="${escape(a.role)}" data-asset-emotion="${escape(a.emotion)}"><b>${escape(a.role)} <span class="muted">/ ${escape(a.emotion)}</span></b><small>${fmtBytes(a.bytes)} · ${a.usage.length ? `${a.usage.length} 处绑定` : '未绑定'}</small></button>`).join('') || '<p class="empty">没有匹配的 PT 文件。</p>';
  $$('[data-asset-role]', $('voiceAssets')).forEach(b => b.onclick = () => selectVoiceAsset(voiceAssets.find(a => a.role === b.dataset.assetRole && a.emotion === b.dataset.assetEmotion)));
}
function selectVoiceAsset(asset) {
  selectedAsset = asset; renderVoiceAssets();
  $('voiceDetailTitle').textContent = asset ? asset.filename : '导入新 PT';
  $('assetRole').value = asset?.role || ''; $('assetEmotion').value = asset?.emotion || '平静';
  $('assetRefText').value = asset?.ref_text || ''; $('assetNotes').value = asset?.notes || '';
  $('assetInfo').textContent = asset ? `${fmtBytes(asset.bytes)} · SHA256 ${asset.sha256}` : '填写角色和情感，然后选择 PT 文件。';
  $('assetUsage').textContent = asset?.usage.length ? `被 ${asset.usage.map(u => `${u.project}/${u.slot}`).join('、')} 绑定，先解除绑定才能删除或重命名。` : '参考台词和备注仅用于记录；修改它们不会重新训练 PT。';
  $('assetExisting').hidden = !asset; $('saveVoiceAsset').disabled = !asset;
  $('ptUploadLabel').textContent = asset ? '替换 PT 文件' : '导入 PT 文件';
  $('assetPreview').hidden = true;
  const cached = asset && previewAudio.get(JSON.stringify([asset.role,asset.emotion]));
  if (cached?.sha256 === asset?.sha256 && cached) { $('assetPreview').src = cached.url; $('assetPreview').hidden = false; }
  if (asset) $('downloadPT').href = assetURL(asset) + '/download';
}

/* ───────── 多供应商 LLM ───────── */
function renderLLM() {
  $('llmFailover').checked = llmDraft.failover;
  $('llmProviders').innerHTML = llmDraft.providers.map(p => `<section class="provider-card" data-provider="${escape(p.id)}"><div class="row"><label class="check"><input type="radio" name="llmActive" value="${escape(p.id)}" ${p.id === llmDraft.active_provider ? 'checked' : ''}>默认模型</label><label class="check"><input type="checkbox" data-llm-field="enabled" ${p.enabled ? 'checked' : ''}>启用</label><button class="btn ghost sm danger" data-remove-provider>移除供应商</button></div><div class="grid-2"><label class="field">供应商名称<input data-llm-field="name" value="${escape(p.name)}"></label><label class="field">模型<input data-llm-field="model" value="${escape(p.model)}" placeholder="step-5-preview"></label></div><label class="field">Base URL<input data-llm-field="base_url" value="${escape(p.base_url)}" placeholder="https://api.stepfun.com/step_plan/v1"></label><label class="field">API Key（每行一个）<textarea data-llm-field="new_keys" rows="2" autocomplete="off" spellcheck="false" placeholder="${p.key_count ? `已有 ${p.key_count} 个 Key；留空保留，填写则全部替换` : '填写 API Key；本地免认证服务可留空'}">${escape(p.new_keys || '')}</textarea></label><label class="check"><input type="checkbox" data-llm-field="clear_keys" ${p.clear_keys ? 'checked' : ''}>清除已保存 Key</label><div class="row"><label class="check"><input type="checkbox" data-llm-field="json_mode" ${p.json_mode ? 'checked' : ''}>支持 JSON Mode</label><label class="field inline-field">超时（秒）<input type="number" min="1" max="3600" data-llm-field="timeout_seconds" value="${p.timeout_seconds}"></label><label class="field inline-field">输出 Token 上限<input type="number" min="32" max="131072" data-llm-field="max_tokens" value="${p.max_tokens}"></label></div></section>`).join('');
  $$('[data-provider]').forEach(row => {
    const p = llmDraft.providers.find(p => p.id === row.dataset.provider);
    $$('[data-llm-field]', row).forEach(el => el.oninput = () => p[el.dataset.llmField] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value);
    row.querySelector('[name="llmActive"]').onchange = () => llmDraft.active_provider = p.id;
    row.querySelector('[data-remove-provider]').onclick = () => { llmDraft.providers = llmDraft.providers.filter(x => x.id !== p.id); if (llmDraft.active_provider === p.id) llmDraft.active_provider = llmDraft.providers[0]?.id || ''; renderLLM(); };
  });
}
async function saveLLM() {
  const body = {active_provider: llmDraft.active_provider, failover: $('llmFailover').checked, providers: llmDraft.providers.map(({key_count, key_labels, new_keys, clear_keys, ...p}) => ({...p, api_keys: clear_keys ? [] : new_keys?.trim() ? new_keys.split(/\r?\n/).map(k => k.trim()).filter(Boolean) : null}))};
  llmDraft = await api('/api/llm/config', 'PUT', body); renderLLM();
  config = await api('/api/config'); $('llmLabel').textContent = config.llm_configured ? '已配置' : '未配置';
  toast('LLM 配置已保存，即时生效');
}

/* ───────── 连接 ───────── */
async function checkConnections() {
  if (connectionBusy) return;
  connectionBusy = true; $('checkConnections').disabled = true;
  try {
    const c = await api('/api/connections');
    $('connectionDot').textContent = c.mode === 'demo' ? '测试音' : c.ready ? '已连接' : '未就绪';
    $('connectionDot').className = c.ready && c.mode !== 'demo' ? 'good' : 'bad';
    $('mode').classList.toggle('offline', !c.ready);
    $('connectionDetails').innerHTML = `<p class="${c.ready ? 'good' : 'bad'}">${escape(c.message)}</p><dl><dt>服务地址</dt><dd>${escape(c.url)}</dd><dt>Gateway</dt><dd>${escape(c.gateway)}</dd><dt>Windows Agent</dt><dd>${escape(c.worker_id)} · ${escape(c.worker)}</dd><dt>GPU 模型</dt><dd>${escape(c.model || '尚未就绪')} ${escape(c.device || '')}</dd><dt>检测耗时</dt><dd>${c.latency_ms} ms</dd></dl><p class="hint">自动每 30 秒检测。Windows 关机或模型加载时会显示未就绪，恢复后自动更新。</p>`;
  } catch (error) {
    $('connectionDot').textContent = '检测失败'; $('connectionDot').className = 'bad';
    $('mode').classList.add('offline'); $('connectionDetails').textContent = `Mac 服务连接异常：${error.message}`;
  } finally { connectionBusy = false; $('checkConnections').disabled = false; }
}

/* ───────── 事件绑定 ───────── */
$('new').onclick = $('welcomeNew').onclick = () => $('newDialog').showModal();
$('closeDialog').onclick = () => $('newDialog').close();
$('newForm').onsubmit = event => {
  event.preventDefault();
  safe(async () => {
    const fields = new FormData(event.target);
    const p = await api('/api/projects', 'POST', {name: fields.get('name'), topic: fields.get('topic'), keywords: fields.get('keywords').split(/[,，]/).map(v => v.trim()).filter(Boolean)});
    $('newDialog').close(); event.target.reset();
    await selectProject(p.name);
  });
};
$('projectFilter').oninput = renderProjects;
$('save').onclick = () => safe(async () => { await save(); toast('修改已保存'); });
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (data) safe(async () => { await save(); toast('修改已保存'); }); }
});
['topic', 'keywords', 'guidance', 'brief'].forEach(id => $(id).oninput = markDirty);
$('addLine').onclick = () => {
  const last = data.script.lines.at(-1);
  const slots = data.script.cast.map(c => c.slot);
  const slot = last ? slots[(slots.indexOf(last.slot) + 1) % slots.length] : slots[0];
  const id = 'L' + crypto.randomUUID().replaceAll('-', '').slice(0, 12);
  data.script.lines.push({id, slot, words: '', mood: 'normal', pause_after_ms: 300});
  markDirty(); renderLines(); renderBindings();
  const ta = document.querySelector(`.line[data-id="${id}"] textarea`); ta?.focus(); ta?.scrollIntoView({block: 'center'});
};
$$('[data-tab]').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
$$('[data-action]').forEach(b => b.onclick = () => safe(() => action(b.dataset.action)));
$$('[data-approve]').forEach(b => b.onclick = () => safe(() => approval(b.dataset.approve)));
$('preflight').onclick = () => safe(async () => { await save(); renderReport(await api(url('/preflight'), 'POST', {})); toast('远端检查完成'); });
$('railToggle').onclick = () => document.querySelector('.shell').classList.toggle('show-rail');

$('bgmFile').onchange = () => safe(async () => {
  for (const file of $('bgmFile').files) { const f = new FormData(); f.append('file', file); data.media = await api(url('/assets/bgm'), 'POST', f); }
  $('bgmFile').value = ''; renderMedia(); toast('音乐已保存，重新导出后生效');
});
$('bgmVolume').oninput = () => $('bgmVolumeOut').textContent = Math.round($('bgmVolume').value * 100) + '%';
$('saveMix').onclick = () => safe(async () => { data.media = await api(url('/mix'), 'PUT', {bgm_volume: Number($('bgmVolume').value)}); renderMedia(); toast('混音设置已保存，重新导出后生效'); });
$('removeBgm').onclick = () => safe(async () => { data.media = await api(url('/mix'), 'PUT', {bgm_volume: Number($('bgmVolume').value), remove_bgm: true}); renderMedia(); toast('已移除音乐，重新导出后生效'); });

$('openFactory').onclick = openFactory;
$$('[data-open-factory]').forEach(b => b.onclick = openFactory);
$('closeFactory').onclick = () => { cancelRecording(); $('factoryDialog').close(); };
$('factoryDialog').addEventListener('close', cancelRecording);
$('recordVoice').onclick = () => safe(startRecording);
$('stopRecording').onclick = () => { if (recorder?.state === 'recording') recorder.stop(); };
$('voiceFile').onchange = () => safe(async () => {
  const file = $('voiceFile').files[0]; if (!file) return;
  await useReference(file, `已上传 ${file.name}`); $('voiceFile').value = '';
});
$('designVoice').onclick = () => safe(async () => { await queue('/api/voices/actions', voiceBody('design')); setRef('试音生成中，完成后会在这里播放…'); });
$('createVoice').onclick = () => safe(async () => { if (!referenceId) throw Error('请先上传参考音频或生成试音'); await queue('/api/voices/actions', voiceBody('create')); toast('音色固化任务已加入队列'); });
$('syncVoices').onclick = () => safe(async () => { await queue('/api/voices/actions', {action: 'sync'}); toast('同步任务已加入队列'); });

$('openLibrary').onclick = () => safe(() => openLibrary());
$('closeLibrary').onclick = () => $('libraryDialog').close();
$('voiceSearch').oninput = renderVoiceAssets;
$('newVoiceAsset').onclick = () => selectVoiceAsset(null);
$('libraryCreate').onclick = () => { $('libraryDialog').close(); openFactory(); };
$('voiceEditForm').onsubmit = event => { event.preventDefault(); safe(async () => {
  if (!selectedAsset) return;
  const updated = await api(assetURL(selectedAsset), 'PUT', {role: $('assetRole').value, emotion: $('assetEmotion').value, ref_text: $('assetRefText').value, notes: $('assetNotes').value});
  await refreshLibrary(); selectVoiceAsset(updated); toast('音色信息已更新');
}); };
$('ptFile').onchange = () => safe(async () => {
  const file = $('ptFile').files[0]; if (!file) return;
  try {
    const f = new FormData(); f.append('file', file); f.append('role', selectedAsset?.role || $('assetRole').value); f.append('emotion', selectedAsset?.emotion || $('assetEmotion').value); f.append('overwrite', String(!!selectedAsset));
    f.append('ref_text', $('assetRefText').value); f.append('notes', $('assetNotes').value);
    const result = await api('/api/voices/import', 'POST', f); await refreshLibrary(); selectVoiceAsset(result); toast('PT 已保存；配音前会自动同步到 Windows');
  } finally { $('ptFile').value = ''; }
});
$('deletePT').onclick = () => safe(async () => {
  if (!selectedAsset) return;
  await api(assetURL(selectedAsset), 'DELETE'); await refreshLibrary(); selectVoiceAsset(null); toast('已删除，可从备份恢复');
});
$('previewPT').onclick = () => safe(async () => {
  if (!selectedAsset) return;
  const target = {role: selectedAsset.role, emotion: selectedAsset.emotion, sha256: selectedAsset.sha256};
  const task = await api('/api/voices/actions', 'POST', {action: 'preview', role: target.role, emotion: target.emotion, text: $('assetPreviewText').value});
  previewTasks.set(task.id, target); taskStatus.set(task.id, 'queued');
  await pollTasks(); toast('试听任务已排队');
});
$('showVoiceTrash').onclick = () => safe(async () => {
  const items = await api('/api/voices/trash'); $('voiceTrash').hidden = !$('voiceTrash').hidden;
  $('voiceTrash').innerHTML = '<h3>删除与替换备份</h3>' + (items.map(i => `<div class="trash-row"><span>${escape(i.role)} / ${escape(i.emotion)} · ${i.reason === 'deleted' ? '删除' : '替换'}${i.created_at ? ` · ${escape(new Date(i.created_at).toLocaleString())}` : ''}</span><button class="btn sm" data-restore="${escape(i.id)}">恢复</button></div>`).join('') || '<p class="hint">暂无备份。</p>');
  $$('[data-restore]').forEach(b => b.onclick = () => safe(async () => { const asset = await api(`/api/voices/trash/${b.dataset.restore}/restore`, 'POST', {}); await refreshLibrary(); selectVoiceAsset(asset); toast('已恢复。'); }));
});
$('openLLM').onclick = () => safe(async () => { llmDraft = await api('/api/llm/config'); renderLLM(); $('llmDialog').showModal(); });
$('closeLLM').onclick = () => $('llmDialog').close();
$('addLLMProvider').onclick = () => {
  const p = {id: 'provider-' + crypto.randomUUID().slice(0, 8), name: '', base_url: '', model: '', enabled: true, json_mode: true, timeout_seconds: 180, max_tokens: 8192, key_count: 0};
  llmDraft.failover = $('llmFailover').checked; llmDraft.providers.push(p); llmDraft.active_provider ||= p.id; renderLLM();
};
$('saveLLM').onclick = () => safe(saveLLM);
$('testLLM').onclick = () => safe(async () => { await saveLLM(); await queue('/api/llm/test', {provider_id: llmDraft.active_provider}); $('llmTestState').textContent = '模型连接测试中…'; });
$('openConnections').onclick = () => { $('connectionDialog').showModal(); checkConnections(); };
$('closeConnections').onclick = () => $('connectionDialog').close();
$('checkConnections').onclick = checkConnections;

window.addEventListener('resize', () => { clearTimeout(growAll.t); growAll.t = setTimeout(growAll, 100); });
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });

/* ───────── 启动 ───────── */
safe(async () => {
  config = await api('/api/config');
  $('mode').textContent = config.tts_mode === 'demo' ? '测试模式 · 非真实配音' : config.tts_mode === 'gateway' ? '网关模式' : 'Qwen 直连模式';
  $('mode').classList.toggle('demo', config.tts_mode === 'demo');
  $('llmLabel').textContent = config.llm_configured ? '已配置' : '未配置';
  checkConnections();
  await inventory(); await pollTasks();
  const requested = new URLSearchParams(location.search).get('project');
  if (projectsCache.length) await selectProject(projectsCache.find(p => p.name === requested)?.name || projectsCache[0].name);
  else $('welcome').hidden = false;
  setInterval(() => safe(pollTasks), 2000);
  setInterval(() => { if (!document.hidden) checkConnections(); }, 30000);
});
