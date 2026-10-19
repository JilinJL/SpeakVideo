'use strict';

/* SpeakVideo 前端。所有接口与原版一致，只重构界面与交互。 */

const $ = id => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let current = '', data = null, config = {}, voices = {}, referenceId = '', dirty = false;
let booted = false, projectsCache = [], dragged = null, taskStatus = new Map(), activeTab = 'script', lastTasks = [];
let voiceAssets = [], selectedAsset = null, llmDraft = null, connectionBusy = false;
const previewTasks = new Map();
const previewAudio = new Map();
let capture = null, recorderModule = null, currentDesignTask = '', runtimeBusy = false;
let videoDirty = false, videoUploading = false;
let libraryEditable = false, libraryMutation = false, libraryRefresh = null, factorySubmitting = '', factoryFinishing = '', currentCreateTask = '', referenceUploading = false, syncSubmitting = false;
const activeTask = status => ['submitting', 'queued', 'running'].includes(status);
let homeProjects = [];
let tasksPolling = false;

const TABS = ['script', 'binding', 'production', 'video'];
const TASK_LABELS = {brief: '写对话草稿', extend: '扩展对话', generate: '生成台词', synth: '合成配音', retry: '重试语音组', merge: '合并导出', render: '合成视频', run: '完整音频工作流', design: '试音', create: '固化音色', sync: '刷新音色库', preview: '音色试听', llm_test: 'LLM 连接测试'};
const TASK_STATES = {queued: '排队中', running: '执行中', succeeded: '完成', failed: '失败', paused: '已暂停', interrupted: '被中断'};
TASK_LABELS.cover = '导出封面';
TASK_LABELS.preview_video = '视频兼容预览';
TASK_LABELS.retry = '重新生成配音';
TASK_LABELS.run = '生成到整组试听';
const LINE_STATES = {succeeded: '可试听', failed: '失败', running: '生成中', queued: '排队中', pending: '待生成'};
const selectedLines = new Set(), retrySubmitting = new Set();
let playingLine = '', linePlayerLoading = false;

const icon = name => `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const store = {
  get(key, fallback) { try { const v = localStorage.getItem('speakvideo.' + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem('speakvideo.' + key, JSON.stringify(value)); } catch { /* 隐私模式下不记忆布局 */ } },
};
for (const [id, target] of store.get('voicePreviewTasks', [])) previewTasks.set(id, target);
const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
const fmtBytes = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? Math.round(n / 1024) + ' KB' : (n || 0) + ' B';
const fmtTime = s => { s = Number(s) || 0; const m = Math.floor(s / 60); return m ? `${m} 分 ${Math.round(s % 60)} 秒` : `${s.toFixed(1)} 秒`; };

/* ───────── 基础 ───────── */
function placeMessages() {
  const host = $('toast'), parent = $$('dialog[open]').at(-1) || document.body;
  // A popover outside a modal is visually above it but remains inert. Keep it
  // inside the active modal so its close button and hover timer are operable.
  if (host.parentElement !== parent) parent.append(host);
  if (!host.childElementCount) return;
  if (host.showPopover) { if (host.matches(':popover-open')) host.hidePopover(); host.showPopover(); }
  else host.style.display = 'flex';
}
$$('dialog').forEach(dialog => dialog.addEventListener('close', placeMessages));
function toast(message, isError = false) {
  const host = $('toast');
  if ([...host.children].some(el => el.dataset.message === String(message))) return;
  const item = document.createElement('div'); item.dataset.message = String(message);
  while (host.childElementCount >= 5) {
    const oldest = host.firstElementChild;
    if (oldest.dismiss) oldest.dismiss(); else oldest.remove();
  }
  item.className = `message ${isError ? 'error' : 'success'}`;
  item.setAttribute('role', isError ? 'alert' : 'status');
  item.tabIndex = 0; item.title = '点击关闭消息';
  const symbol = document.createElement('span'); symbol.className = 'message-symbol'; symbol.textContent = isError ? '!' : '✓';
  const body = document.createElement('div'); body.className = 'message-body';
  const content = document.createElement('span'); content.textContent = message;
  const countdown = document.createElement('span'); countdown.className = 'message-countdown'; countdown.setAttribute('aria-hidden', 'true');
  const close = document.createElement('button'); close.className = 'message-close'; close.textContent = '×'; close.setAttribute('aria-label', '关闭消息');
  body.append(content, countdown); item.append(symbol, body, close); host.append(item);
  // Popovers share the browser top layer with dialogs, so feedback stays visible.
  placeMessages();
  let timer, remaining = isError ? 12000 : 5000, deadline = 0, hovering = false, focused = false, dismissed = false;
  const dismiss = item.dismiss = () => {
    if (dismissed) return;
    dismissed = true; clearInterval(timer); item.remove();
    if (!host.childElementCount) { if (host.hidePopover) host.hidePopover(); else host.style.display = 'none'; }
  };
  const tick = () => {
    remaining = Math.max(0, deadline - performance.now());
    if (!remaining) return dismiss();
    countdown.textContent = `${Math.ceil(remaining / 1000)} 秒后关闭 · 点击关闭`;
  };
  const updateTimer = () => {
    if (dismissed) return;
    if (timer) { remaining = Math.max(0, deadline - performance.now()); clearInterval(timer); timer = null; }
    if (hovering || focused) countdown.textContent = `剩余 ${Math.ceil(remaining / 1000)} 秒 · 已暂停 · 点击关闭`;
    else { deadline = performance.now() + remaining; tick(); if (!dismissed) timer = setInterval(tick, 100); }
  };
  item.onclick = dismiss;
  item.onkeydown = event => { if (event.target === item && ['Enter', ' '].includes(event.key)) { event.preventDefault(); dismiss(); } };
  item.onpointerenter = () => { hovering = true; updateTimer(); };
  item.onpointerleave = () => { hovering = false; updateTimer(); };
  item.onfocusin = () => { focused = true; updateTimer(); };
  item.onfocusout = event => { if (!item.contains(event.relatedTarget)) { focused = false; updateTimer(); } };
  updateTimer();
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
const speakerIndex = slot => ({p1: 1, p2: 2}[slot] || 3);
const speakerVar = slot => `--speaker: var(--s${speakerIndex(slot)})`;

function setSaveState(text, isDirty = false) { $('saveState').textContent = text; $('saveState').classList.toggle('dirty', isDirty); }
function markDirty() { dirty = true; setSaveState('未保存', true); renderSteps(); renderDraftTimeline(); updateLineReview(); }

/* ───────── 左栏：工程与音色 ───────── */
function renderProjects() {
  const q = $('projectFilter').value.trim().toLowerCase();
  const list = projectsCache.filter(p => !q || p.name.toLowerCase().includes(q) || (p.topic || '').toLowerCase().includes(q));
  $('projectFilter').hidden = projectsCache.length < 6;
  $('projectCount').textContent = projectsCache.length || '';
  $('projects').innerHTML = list.map(p => `<button class="project ${p.name === current ? 'selected' : ''}" data-project="${escape(p.name)}"><b>${escape(p.name)}</b><small>${escape(p.topic || '未填写话题')}</small></button>`).join('')
    || `<div class="empty">${projectsCache.length ? '没有匹配的工程' : '还没有工程'}</div>`;
  $$('[data-project]', $('projects')).forEach(b => b.onclick = () => safe(() => selectProject(b.dataset.project)));
}
async function inventory({includeVoices = true} = {}) {
  projectsCache = await api('/api/projects');
  renderProjects();
  if (!includeVoices) return;
  voices = await api('/api/voices');
  $('voiceCount').textContent = Object.keys(voices).length || '';
  $('voiceRoleOptions').innerHTML = Object.keys(voices).map(role => `<option value="${escape(role)}">`).join('');
  const opened = new Set($$('details[open]', $('voiceInventory')).map(el => el.dataset.inventoryRole));
  $('voiceInventory').innerHTML = Object.entries(voices).map(([role, emotions]) =>
    `<details class="inventory-role" data-inventory-role="${escape(role)}" ${opened.has(role) ? 'open' : ''}><summary><span>${escape(role)}</span><small>${emotions.length} 个情绪</small></summary><div class="inventory-emotions">${emotions.map(emotion => `<button class="voice" data-voice-role="${escape(role)}" data-voice-emotion="${escape(emotion)}">${escape(emotion)}</button>`).join('')}</div></details>`).join('')
    || '<div class="empty">还没有音色。点上方 + 去音色工厂创建。</div>';
  $$('[data-inventory-role]', $('voiceInventory')).forEach(group => group.querySelector('summary').onclick = event => { event.preventDefault(); safe(() => openLibrary(group.dataset.inventoryRole)); });
  $$('[data-voice-role]', $('voiceInventory')).forEach(b => b.onclick = () => safe(async () => { await openLibrary(b.dataset.voiceRole); selectVoiceAsset(voiceAssets.find(a => a.role === b.dataset.voiceRole && a.emotion === b.dataset.voiceEmotion)); }));

}

/* ───────── 主页 ───────── */
function renderHomeProjects() {
  const query = $('homeProjectFilter').value.trim().toLowerCase();
  const projects = homeProjects.filter(p => !query || `${p.name} ${p.topic}`.toLowerCase().includes(query));
  $('homeProjectCount').textContent = `${homeProjects.length} 个 · 最近更新优先`;
  $('homeProjectFilter').hidden = !homeProjects.length;
  $('homeProjects').innerHTML = projects.map(p => {
    const href = `/?project=${encodeURIComponent(p.name)}&tab=${encodeURIComponent(p.tab)}`;
    const updated = new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai'}).format(new Date(p.updated_at * 1000));
    const active = lastTasks.find(t => t.project === p.name && ['queued', 'running'].includes(t.status));
    return `<a class="home-project" href="${href}" data-home-project="${escape(p.name)}" data-home-tab="${escape(p.tab)}">
      <span class="home-project-icon">${icon(p.tab === 'video' ? 'play' : 'folder')}</span>
      <div class="home-project-copy"><h3>${escape(p.topic || p.name)}</h3><p><span>${escape(p.name)}</span><span>${p.line_count} 行台词</span><span>更新于 ${escape(updated)}</span></p></div>
      <span class="home-stage ${active ? 'running' : p.tab}">${escape(active ? (active.status === 'running' ? '制作中' : '排队中') : p.stage)}</span><span class="home-project-arrow" aria-hidden="true">↗</span>
    </a>`;
  }).join('') || `<div class="home-empty"><span>${icon('folder')}</span><h3>${query ? '没有找到匹配的工程' : '你的第一段对话，从一个问题开始。'}</h3><p>${query ? '换个关键词，试试话题或工程名称。' : '点上方「新建对话」，写下你想聊的话题。'}</p></div>`;
  $$('[data-home-project]').forEach(link => link.onclick = event => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button) return;
    event.preventDefault();
    safe(() => selectProject(link.dataset.homeProject, {tab: link.dataset.homeTab}));
  });
}
async function refreshHome() {
  const response = await api('/api/home');
  homeProjects = response.projects;
  if (!current) renderHomeProjects();
}
async function showHome() {
  current = ''; data = null; dirty = false; videoDirty = false; booted = true;
  $('welcome').hidden = false; $('workspace').hidden = true; $('steps').hidden = true; $('save').hidden = true;
  $('saveState').hidden = true; $('projectName').hidden = true;
  $('title').textContent = '创作工作台'; document.title = 'SpeakVideo · 创作主页';
  document.querySelector('.main').classList.add('on-home');
  applyRail();
  $('homeSetup').hidden = !!config.llm_configured;
  const link = new URL(location.href); link.searchParams.delete('project'); link.searchParams.delete('tab');
  history.replaceState(null, '', link);
  renderProjects(); renderTasks();
  await refreshHome();
}

/* ───────── 工程加载 ───────── */
function defaultTab() {
  const a = data.workflow?.approvals || {};
  if (!a.script) return 'script';
  if (!a.binding) return 'binding';
  return a.audio ? 'video' : 'production';
}
async function selectProject(name, {keepTab = false, tab: preferredTab = null} = {}) {
  if (name !== current && dirty && !confirm('有未保存修改，仍要切换工程吗？')) return;
  const switching = name !== current;
  if (switching) { selectedLines.clear(); stopLineAudio(); }
  current = name;
  [data, config.script_prompts] = await Promise.all([api(url('')), api('/api/script-prompts')]);
  dirty = false;
  data.workflow ||= {}; data.workflow.approvals ||= {};

  $('welcome').hidden = true; $('workspace').hidden = false; $('steps').hidden = false; $('save').hidden = false;
  $('saveState').hidden = false; $('projectName').hidden = false;
  document.querySelector('.main').classList.remove('on-home');
  applyRail();
  $('projectName').textContent = current;
  $('title').textContent = data.script.meta.topic || '未命名对话';
  document.title = `${data.script.meta.topic || current} - SpeakVideo 音频工作台`;
  $('topic').value = data.script.meta.topic; $('keywords').value = data.script.meta.keywords.join(', ');
  $('guidance').value = data.script.meta.guidance || ''; $('brief').value = data.script.meta.brief;
  if ($('draftSettings').dataset.project !== current) {
    $('draftSettings').dataset.project = current;
    $('draftSettings').open = !$('brief').value.trim();
  }
  for (const key of ['brief', 'structure']) {
    $(`${key}Prompt`).value = config.script_prompts[key];
  }
  $('structureContract').value = config.script_json_contract;
  renderPromptState();
  setSaveState('已保存');

  $('personas').innerHTML = data.script.cast.map(m =>
    `<label class="field" style="${speakerVar(m.slot)}"><span><span class="dot" style="background:var(--speaker)"></span>${escape(m.slot)} 人设</span><textarea rows="3" maxlength="2000" data-persona="${escape(m.slot)}">${escape(m.persona)}</textarea></label>`).join('');
  $$('textarea', $('personas')).forEach(input => input.oninput = () => { data.script.cast.find(m => m.slot === input.dataset.persona).persona = input.value; markDirty(); });

  if (live && live.project !== current) stopFollowing();
  $('synthesisConcurrency').value = data.synthesis?.concurrency || 1;
  renderSynthesisConcurrency();
  renderLines(); renderBindings(); renderReport(data.preflight); renderMedia(); updateExports(); renderVideo(); renderLive();
  const requested = new URLSearchParams(location.search).get('tab');
  let tab = defaultTab();
  if (TABS.includes(preferredTab)) tab = preferredTab;
  else if (keepTab && !switching) tab = activeTab;
  else if (!booted && TABS.includes(requested)) tab = requested;
  booted = true;
  switchTab(tab);
  await inventory({includeVoices: false}); renderTasks();
}

/* ───────── 步骤 ───────── */
function switchTab(id) {
  if (id !== 'video') $('footagePlayer').pause();
  activeTab = id;
  $$('.tab').forEach(e => e.hidden = e.id !== id);
  $('workspace').dataset.tab = id;
  renderSteps();
  const link = new URL(location.href); link.searchParams.set('project', current); link.searchParams.set('tab', id); history.replaceState(null, '', link);
  $('workspace').scrollTop = 0;
  growAll();
}
function renderSteps() {
  const a = data?.workflow?.approvals || {};
  const doneMap = {script: a.script, binding: a.binding, production: a.audio, video: data?.video_current};
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
function lineStatus(id) {
  if (retrySubmitting.has(id)) return 'queued';
  const active = lastTasks.filter(t => t.project === current && activeTask(t.status) && t.line_ids?.includes(id));
  const retry = active.find(t => t.label === 'retry');
  const task = retry || active.find(t => t.label === 'synth');
  if (task) {
    if (task.status === 'queued') return 'queued';
    if (task.progress?.running_ids?.includes(id)) return 'running';
    if (task.progress?.completed_ids?.includes(id)) return 'succeeded';
    if (retry || !data.preflight?.reusable_ids?.includes(id)) return 'queued';
  }
  const status = data.manifest?.lines?.[id]?.status || 'pending';
  return status === 'succeeded' && data.preflight?.reusable_ids && !data.preflight.reusable_ids.includes(id) ? 'pending' : status;
}

function stopLineAudio() {
  const player = $('linePlayer');
  player.pause(); player.removeAttribute('src'); player.load();
  playingLine = ''; linePlayerLoading = false; $('linePlayerDock').hidden = true;
}
function reviewGroups() {
  if (!data) return [];
  if (!dirty && data.speech_groups) return data.speech_groups;
  const groups = [];
  const joined = lines => lines.map((l, i) => l.words.trim() + (i < lines.length - 1 && l.words.trim() && !/[，。！？；：、,.!?;:…"'”’）)]$/.test(l.words.trim()) ? '。' : '')).join('');
  const voice = line => { const binding = data.cast[line.slot]; return JSON.stringify(binding ? [line.slot, binding.role, binding.mood_map[line.mood] ?? null] : [line.slot, null, line.mood]); };
  for (const line of data.script.lines) {
    const group = groups.at(-1), previous = group?.lines.at(-1);
    if (previous && voice(previous) === voice(line) && previous.pause_after_ms < 1000 && [...joined([...group.lines, line])].length <= 200) group.lines.push(line);
    else groups.push({id: line.id, slot: line.slot, lines: [line]});
  }
  return groups.map(g => ({id: g.id, slot: g.slot, line_ids: g.lines.map(l => l.id), text: joined(g.lines)}));
}
function groupForLine(id) { return reviewGroups().find(g => g.line_ids.includes(id)); }
function groupState(group) {
  const states = group.line_ids.map(lineStatus);
  return ['running', 'queued', 'failed', 'pending'].find(s => states.includes(s)) || 'succeeded';
}
function groupLabel(group) { return group.line_ids.length > 1 ? `${group.id}–${group.line_ids.at(-1)} · ${group.line_ids.length} 行` : `${group.id} · 1 行`; }
function segmentSource(id) {
  const group = groupForLine(id), clip = data?.group_audio?.[group?.id];
  return clip && !dirty && group.line_ids.every(key => data.preflight?.reusable_ids?.includes(key)) ? url(`/groups/${encodeURIComponent(group.id)}/audio?version=${encodeURIComponent(clip.sha256)}`) : '';
}
async function playLine(id) {
  const group = groupForLine(id); id = group.id;
  const source = segmentSource(id), player = $('linePlayer');
  if (!source) throw Error('请先保存并生成本组配音，再试听');
  if (playingLine === id && !player.paused) { player.pause(); return; }
  $$('audio').forEach(audio => { if (audio !== player) audio.pause(); });
  if (player.getAttribute('src') !== source) { player.src = source; player.load(); }
  playingLine = id; linePlayerLoading = true;
  $('linePlayerDock').hidden = false;
  $('linePlayerTitle').textContent = `整组试听 ${groupLabel(group)} · ${group.text}`;
  updateLineReview();
  try { await player.play(); }
  catch (error) { linePlayerLoading = false; updateLineReview(); if (error.name !== 'AbortError') throw Error('试听加载失败，请刷新后重试'); }
}
for (const event of ['playing', 'pause', 'ended', 'waiting', 'error']) {
  $('linePlayer').addEventListener(event, () => {
    linePlayerLoading = event === 'waiting'; updateLineReview();
    if (event === 'error' && $('linePlayer').getAttribute('src')) toast('整组音频加载失败，请刷新后重试', true);
  });
}
function updateLineReview() {
  if (!data) return;
  const groups = reviewGroups(), ids = new Set(groups.map(g => g.id));
  for (const id of selectedLines) if (!ids.has(id)) selectedLines.delete(id);
  const busy = lastTasks.some(t => t.project === current && activeTask(t.status));
  const blocked = videoUploading || lastTasks.some(t => t.project === current && activeTask(t.status) && !['synth', 'retry'].includes(t.label));
  $('selectAllLines').checked = !!ids.size && selectedLines.size === ids.size;
  $('selectAllLines').indeterminate = selectedLines.size > 0 && selectedLines.size < ids.size;
  $('selectAllLines').disabled = !ids.size || dirty;
  $('lineSelectionCount').textContent = `已选 ${selectedLines.size} 组`;
  $('retrySelectedLines').disabled = !selectedLines.size || blocked || !!retrySubmitting.size || dirty;
  const running = groups.filter(g => groupState(g) === 'running').length;
  const queued = groups.filter(g => groupState(g) === 'queued').length;
  $('lineReviewState').textContent = running || queued ? `${running} 组生成中 · ${queued} 组排队中` : dirty ? '保存后更新语音分组' : '直接试听完整语音组，满意后再合并';
  $('lineReviewState').classList.toggle('running', !!running);
  $$('.speech-group', $('lines')).forEach(section => {
    const group = groups.find(g => g.id === section.dataset.groupId); if (!group) return;
    const id = group.id, state = groupState(group);
    section.dataset.state = state; section.setAttribute('aria-busy', String(state === 'running'));
    const status = section.querySelector('[data-group-status]');
    status.className = `status ${state}`; status.textContent = LINE_STATES[state] || state;
    section.querySelector('[data-group-select]').checked = selectedLines.has(id);
    section.querySelector('[data-group-select]').disabled = dirty;
    const retry = section.querySelector('[data-group-retry]');
    retry.disabled = blocked || ['queued', 'running'].includes(state) || dirty;
    retry.setAttribute('aria-busy', String(state === 'running'));
    const listen = section.querySelector('[data-group-listen]'), on = playingLine === id && !$('linePlayer').paused;
    listen.disabled = !segmentSource(id) || ['queued', 'running'].includes(state);
    listen.classList.toggle('loading', playingLine === id && linePlayerLoading);
    listen.setAttribute('aria-pressed', String(on));
    listen.setAttribute('aria-label', `${on ? '暂停整组试听' : '整组试听'} ${id}`);
    listen.innerHTML = `${icon(on ? 'stop' : 'play')}<span>${on ? '暂停' : '整组试听'}</span>`;
    const clip = data.group_audio?.[id];
    section.querySelector('[data-group-duration]').textContent = clip && !dirty ? `${clip.duration.toFixed(1)} 秒` : '';
  });
  $$('.line', $('lines')).forEach(row => {
    const state = lineStatus(row.dataset.id);
    row.dataset.state = state;
    const status = row.querySelector('.status');
    if (status) { status.className = `status ${state}`; status.textContent = LINE_STATES[state] || state; }
    $$('[data-field], [data-delete]', row).forEach(el => el.disabled = busy);
    const handle = row.querySelector('.handle'); if (handle) handle.draggable = !busy;
  });
  $('addLine').disabled = busy;
  $$('#bindings select, #personas textarea').forEach(el => el.disabled = busy);
  $$('[data-approve="script"], [data-approve="binding"]').forEach(el => el.disabled = busy);
}
let retrySubmission = Promise.resolve();
function retryLines(ids) {
  const name = current, requested = [...new Set(ids)];
  for (const id of requested) retrySubmitting.add(id);
  updateLineReview(); renderLineMap();
  const operation = retrySubmission.catch(() => {}).then(async () => {
    if (current !== name) return;
    if (dirty) await save();
    const task = await api(url('/actions'), 'POST', {action: 'retry', line_ids: requested, concurrency: Number($('synthesisConcurrency').value)});
    if (task.line_ids.includes(playingLine)) stopLineAudio();
    lastTasks = [...lastTasks.filter(t => t.id !== task.id), task]; taskStatus.set(task.id, task.status);
    toast(`已加入整组重生成队列 · ${task.line_ids.length} 行台词`);
    await pollTasks();
  }).finally(() => { for (const id of requested) retrySubmitting.delete(id); updateLineReview(); renderLineMap(); });
  retrySubmission = operation;
  return operation;
}
$('selectAllLines').onchange = () => { selectedLines.clear(); if ($('selectAllLines').checked) reviewGroups().forEach(group => selectedLines.add(group.id)); updateLineReview(); };
$('retrySelectedLines').onclick = () => safe(() => retryLines([...selectedLines]));
$('reviewLineAudio').onclick = () => { switchTab('script'); $('lines').scrollTop = 0; $('lines').closest('.lines-card').scrollIntoView({block: 'start'}); };

function renderLineStats() {
  const lines = data.script.lines;
  const done = lines.filter(l => lineStatus(l.id) === 'succeeded').length;
  const {chars, seconds} = SpeakVideoTiming.estimate(lines);
  $('lineStats').textContent = lines.length ? `${lines.length} 行 · ${chars} 字 · 预计 ${SpeakVideoTiming.format(seconds)} · 已合成 ${done}` : '';
  $('lineStats').title = '按约 5.07 字/秒加台词停顿估算播放时长；合并配音的组内停顿由模型自然朗读，实际时长会随音色和情绪变化。';
}
function validateLineInput(input) {
  const count = SpeakVideoTiming.characterCount(input.value);
  const valid = count >= 3 && count <= 200;
  input.classList.toggle('bad', !valid);
  input.setAttribute('aria-invalid', String(!valid));
  input.title = `${count} 字 · 每行须为 3–200 字`;
  input.setCustomValidity(valid ? '' : '每行台词须为 3–200 字');
}
function renderLines() {
  if (liveRunning() && live.kind === 'generate') return renderStreamingLines();
  delete $('lines').dataset.streaming;
  const lines = data.script.lines;
  const slots = data.script.cast.map(c => c.slot);
  renderLineStats();
  if (!lines.length) {
    $('lines').innerHTML = '<div class="empty-lines">还没有台词。点左侧“生成台词”，或手动添加一行。</div>';
    return;
  }
  $('lines').innerHTML = reviewGroups().map(group => `<section class="speech-group" data-group-id="${escape(group.id)}" style="${speakerVar(group.slot)}">
    <div class="speech-group-head"><label class="check"><input type="checkbox" data-group-select aria-label="选择语音组 ${escape(group.id)}">${escape(group.slot.toUpperCase())} · ${escape(groupLabel(group))}</label><span class="status" data-group-status></span><span class="group-tools"><button class="btn ghost sm line-listen" data-group-listen aria-label="整组试听 ${escape(group.id)}">整组试听</button><span class="muted small" data-group-duration></span><button class="btn ghost sm" data-group-retry aria-label="重新生成本组 ${escape(group.id)}">${icon('refresh')}重生成本组</button></span></div>
    ${group.line_ids.map(id => lines.find(line => line.id === id)).filter(Boolean).map(line => {
    const status = lineStatus(line.id);
    const missing = !data.cast[line.slot]?.mood_map[line.mood];
    return `<div class="line" data-id="${escape(line.id)}" style="${speakerVar(line.slot)}">
      <span class="handle" draggable="true" title="拖动排序">⠿</span>
      <div class="line-body">
        <div class="line-bar">
          <select data-field="slot" class="slot" aria-label="角色">${options(slots, line.slot)}</select>
          <select data-field="mood" class="mood ${missing ? 'bad' : ''}" title="${missing ? '该情绪尚未映射到音色' : '情绪'}" aria-label="情绪">${options(Object.keys(config.moods || {}), line.mood, '', true)}</select>
          <span class="pause" title="句后停顿（毫秒）；合并配音组内按标点自然停顿，组尾追加此停顿。至少 1000 ms 时分组。"><input data-field="pause_after_ms" type="number" min="0" max="10000" step="50" value="${line.pause_after_ms}" aria-label="句后停顿"></span>
          <span class="meta"><span class="status ${status}">${escape(LINE_STATES[status] || status)}</span><span>${escape(line.id)}</span></span>
          <span class="tools"><button class="icon-btn danger" data-delete title="删除这一行" aria-label="删除">${icon('x')}</button></span>
        </div>
        <textarea data-field="words" rows="1" aria-label="台词" placeholder="输入台词（3–200 字）">${escape(line.words)}</textarea>
      </div>
    </div>`;
  }).join('')}</section>`).join('');
  $$('.speech-group', $('lines')).forEach(section => {
    const id = section.dataset.groupId;
    section.querySelector('[data-group-retry]').onclick = () => safe(() => retryLines([id]));
    section.querySelector('[data-group-listen]').onclick = () => safe(() => playLine(id));
    section.querySelector('[data-group-select]').onchange = event => { if (event.target.checked) selectedLines.add(id); else selectedLines.delete(id); updateLineReview(); };
  });

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
        if (field === 'words') validateLineInput(input);
        if (['words', 'pause_after_ms', 'slot', 'mood'].includes(field)) renderLineStats();
      };
    });
    validateLineInput(row.querySelector('textarea'));
    autoGrow(row.querySelector('textarea'));
    row.querySelector('[data-delete]').onclick = () => { data.script.lines = data.script.lines.filter(l => l.id !== id); markDirty(); renderLines(); renderBindings(); };


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
  updateLineReview();
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
  const invalid = data.script.lines.find(line => {
    const count = SpeakVideoTiming.characterCount(line.words);
    return count < 3 || count > 200;
  });
  if (invalid) throw Error(`${invalid.id} 台词须为 3–200 字，请修改后保存。`);

  collect(); setSaveState('保存中…');
  const result = await api(url(''), 'PUT', {script: data.script, cast: data.cast});
  if (videoDirty) await saveVideo();
  dirty = false; const fresh = await api(url(''));
  for (const key of ['speech_groups', 'group_audio', 'manifest', 'preflight']) data[key] = fresh[key];
  setSaveState('已保存'); renderSteps(); renderLines(); updateLineReview();
  $('title').textContent = data.script.meta.topic || '未命名对话';
  document.title = `${data.script.meta.topic || current} - SpeakVideo 音频工作台`;
  renderPromptState(); renderReport(result.preflight);
}

/* ───────── 生成 ───────── */
function renderDraftTimeline() {
  if (!data) return;
  const hasBrief = !!$('brief').value.trim(), on = liveRunning();
  const drafting = on && ['brief', 'extend'].includes(live.kind);
  const generating = on && live.kind === 'generate';
  const busy = videoUploading || on || lastTasks.some(t => t.project === current && activeTask(t.status));
  $('draftSettings').classList.toggle('complete', hasBrief && !drafting);
  $('draftSettings').setAttribute('aria-current', !hasBrief || drafting ? 'step' : 'false');
  $('draftToLines').classList.toggle('active', hasBrief || drafting);
  $('draftToLines').setAttribute('aria-current', hasBrief && !drafting ? 'step' : 'false');
  $('draftStepState').textContent = drafting ? '正在生成草稿' : generating ? '正在生成台词' : hasBrief ? '编辑草稿，确认后生成台词' : '等待草稿';
  const draftButton = document.querySelector('[data-action="brief"]');
  draftButton.firstChild.textContent = hasBrief ? '重新生成草稿' : '生成草稿';
  draftButton.disabled = busy || !$('topic').value.trim();
  document.querySelector('[data-action="extend"]').disabled = busy || !hasBrief;
  document.querySelector('[data-action="generate"]').disabled = busy || !hasBrief;
  $('draftGenerateHint').textContent = hasBrief ? '按当前草稿生成；重新生成会替换全部台词，旧版自动保留。' : '先生成或填写草稿，再生成台词。';
  // A running request uses a saved snapshot; keep its draft stable until it finishes.
  $('brief').readOnly = on;
}
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
  const markup = reviewGroups().map(g => { const s = groupState(g); return `<button type="button" data-line-jump="${escape(g.id)}" class="${s}" aria-label="语音组 ${escape(g.id)} ${escape(LINE_STATES[s] || s)}" title="${escape(groupLabel(g))} · ${escape(LINE_STATES[s] || s)}：${escape(g.text.slice(0, 30))}"></button>`; }).join('');
  for (const host of [$('lineMap'), $('lineReviewMap')]) {
    host.innerHTML = markup;
    $$('[data-line-jump]', host).forEach(button => button.onclick = () => {
      switchTab('script');
      const row = $$('.line', $('lines')).find(row => row.dataset.id === button.dataset.lineJump);
      row?.scrollIntoView({block: 'center', behavior: 'smooth'}); row?.closest('.speech-group')?.querySelector('[data-group-listen]')?.focus();
    });
  }
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
    $('exports').innerHTML = '<p class="hint">先在台词页逐句试听，效果满意后再点“满意，合并完整音频”。</p>';
  }
}
async function action(type, lineId) {
  if (type === 'retry') return retryLines([lineId]);
  if (videoUploading) throw Error('请等待素材上传完成');
  if (type === 'brief' && !$('topic').value.trim()) throw Error('请先填写话题，再生成草稿');
  if (type === 'generate' && !$('brief').value.trim()) throw Error('请先生成或填写草稿，再生成台词');
  if (type === 'generate' && data.script.lines.length && !confirm('重新生成将替换全部台词并清空复用记录，继续吗？')) return;
  if (type === 'extend' && !$('brief').value.trim()) throw Error('请先填写或生成草稿，再扩展对话');
  if (dirty) await save();
  if (['synth', 'retry', 'run'].includes(type)) await saveSynthesisConcurrency();
  if (['render', 'cover'].includes(type)) await saveVideo();
  const task = await queue(url('/actions'), {action: type, line_id: lineId || null,
    ...(['synth', 'retry', 'run'].includes(type) ? {concurrency: Number($('synthesisConcurrency').value)} : {})});
  if (LLM_ACTIONS.has(type)) followLLM(task); else toast(`已加入队列：${TASK_LABELS[task.label] || task.label}`);
}

function renderSynthesisConcurrency() {
  $('synthesisConcurrencyOut').textContent = `${$('synthesisConcurrency').value} 组`;
}
let synthesisSaving = Promise.resolve();
function saveSynthesisConcurrency() {
  const name = current, concurrency = Number($('synthesisConcurrency').value);
  synthesisSaving = synthesisSaving.catch(() => {}).then(async () => {
    const saved = await api(`/api/projects/${encodeURIComponent(name)}/synthesis`, 'PUT', {concurrency});
    if (current === name) { data.synthesis = saved; renderSynthesisConcurrency(); }
  });
  return synthesisSaving;
}
$('synthesisConcurrency').oninput = renderSynthesisConcurrency;
$('synthesisConcurrency').onchange = () => safe(saveSynthesisConcurrency);

/* ───────── 流式生成：草稿逐字出现，台词逐条追加，可随时停止 ─────────
   服务端只推送增量；停止会关闭模型连接并放弃本次结果，工程内容保持生成前的样子。 */
const LLM_ACTIONS = new Set(['brief', 'extend', 'generate']);
let live = null; // {task, kind, source, before, lines, text}
function followLLM(task) {
  if (live?.task === task.id) return;
  stopFollowing();
  const kind = task.label === 'extend' ? 'extend' : task.label;
  if (['brief', 'extend'].includes(kind)) $('draftSettings').open = false;
  live = {task: task.id, project: current, kind, before: {brief: $('brief').value, lines: data.script.lines}, lines: [], text: '', phase: 0};
  renderLive();
  const source = live.source = new EventSource(`/api/tasks/${task.id}/stream`);
  source.onmessage = event => {
    if (!live || live.source !== source) return;
    const chunk = JSON.parse(event.data);
    // “完整工作流”会先写草稿再生成台词，阶段切换时换一个显示目标
    if (chunk.phase !== live.phase) { live.phase = chunk.phase; live.kind = chunk.kind; live.text = ''; live.lines = []; }
    live.reasoning = chunk.reasoning; live.chars = chunk.chars;
    // from 是这段增量的起点；断线重连时服务端从头补发，按起点对齐避免重复
    if (chunk.lines) live.lines = [...live.lines.slice(0, chunk.from), ...chunk.lines];
    if (chunk.text != null) live.text = live.text.slice(0, chunk.from) + chunk.text;
    renderLive();
  };
  source.addEventListener('end', () => { source.close(); if (live?.source === source) live.source = null; });
  source.onerror = () => { if (source.readyState === EventSource.CLOSED && live?.source === source) live.source = null; };
}
function stopFollowing(restore = false) {
  if (!live) return;
  live.source?.close();
  const was = live; live = null;
  // 停止或失败：撤回流式显示；成功：保留到重新载入工程为止，避免闪回旧内容
  if (restore && was.project === current && data) { $('brief').value = was.before.brief; delete $('lines').dataset.streaming; renderLines(); }
  renderLive();
}
function liveRunning() { return !!live && live.project === current; }
function renderLive() {
  const on = liveRunning(), kind = live?.kind;
  const lineKind = on && kind === 'generate', briefKind = on && (kind === 'brief' || kind === 'extend');
  $$('[data-stop-llm]').forEach(b => b.hidden = !(b.dataset.stopLlm === 'generate' ? lineKind : briefKind));
  $$('[data-discard-llm]').forEach(b => b.hidden = !(b.dataset.discardLlm === 'generate' ? lineKind : briefKind));
  document.querySelector('[data-action="brief"]').hidden = briefKind;
  $('addLine').hidden = lineKind; document.querySelector('[data-approve="script"]').hidden = lineKind;
  $('brief').readOnly = briefKind; $('brief').classList.toggle('streaming', briefKind);
  $('briefLive').hidden = !briefKind;
  if (briefKind) {
    const thinking = !live.text && live.reasoning;
    $('briefLive').textContent = thinking ? `思考中 · ${live.reasoning.toLocaleString()} 字` : live.text ? `生成中 · ${(live.chars || 0).toLocaleString()} 字` : '等待模型响应…';
    $('brief').value = kind === 'extend' ? `${live.before.brief}\n\n${live.text}` : live.text;
    if (live.text) $('brief').scrollTop = $('brief').scrollHeight;
  }
  if (lineKind) renderStreamingLines();
  renderDraftTimeline();
}
function renderStreamingLines() {
  const box = $('lines');
  if (!box.dataset.streaming) { box.dataset.streaming = 'true'; box.innerHTML = '<div class="stream-lines"></div><div class="stream-tail"></div>'; }
  const list = box.querySelector('.stream-lines');
  while (list.childElementCount > live.lines.length) list.lastElementChild.remove();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  for (const line of live.lines.slice(list.childElementCount)) {
    const slot = ['p1', 'p2'].includes(line.slot) ? line.slot : 'p1';
    const row = document.createElement('div');
    row.className = 'line streamed'; row.setAttribute('style', speakerVar(slot));
    row.innerHTML = `<span class="handle" aria-hidden="true"></span><div class="line-body"><div class="line-bar"><b class="stream-slot">${escape(line.slot || '?')}</b><span class="stream-mood">${escape(config.moods?.[line.mood] || line.mood || '')}</span><span class="stream-pause">${escape(line.pause_after_ms ?? '')} ms</span><span class="meta"><span>${escape(line.id || '')}</span></span></div><p class="stream-words">${escape(line.words || '')}</p></div>`;
    list.append(row);
  }
  const thinking = !live.lines.length && live.reasoning;
  box.querySelector('.stream-tail').innerHTML = `<span class="stream-dot"></span>${thinking ? `模型思考中 · 已推理 ${live.reasoning.toLocaleString()} 字` : live.lines.length ? `已生成 ${live.lines.length} 行，继续生成中…` : live.chars ? '正在输出台词…' : '等待模型响应…'}`;
  $('lineStats').textContent = `生成中 · ${live.lines.length} 行`;
  if (atBottom) box.scrollTop = box.scrollHeight;
}
// 停止：保留已生成的部分，作为未保存修改放进编辑区，确认后点保存即可；放弃：恢复生成前的内容
const stoppedTasks = new Set();
async function stopLLM({keep = true} = {}) {
  if (!live) return;
  const was = live, id = was.task;
  stoppedTasks.add(id); // 防止轮询在暂停请求返回前又接上这条流
  stopFollowing(!keep);
  if (keep && was.project === current && data) {
    if (was.kind === 'generate') {
      const slots = data.script.cast.map(c => c.slot), ids = new Set();
      const kept = was.lines.filter(l => typeof l.words === 'string' && l.words.trim()).map((l, i) => {
        let lineId = /^[A-Za-z0-9_-]{1,30}$/.test(l.id || '') && !ids.has(l.id) ? l.id : `L${String(i + 1).padStart(3, '0')}`;
        while (ids.has(lineId)) lineId += 'x';
        ids.add(lineId);
        return {id: lineId, slot: slots.includes(l.slot) ? l.slot : slots[i % slots.length], words: l.words.trim(),
          mood: config.moods?.[l.mood] ? l.mood : 'normal', pause_after_ms: Number.isInteger(l.pause_after_ms) ? Math.min(10000, Math.max(0, l.pause_after_ms)) : 300};
      });
      if (kept.length) { data.script.lines = kept; markDirty(); }
      delete $('lines').dataset.streaming; renderLines(); renderBindings();
      await api(`/api/tasks/${id}/pause`, 'POST', {});
      toast(kept.length ? `已停止生成，保留已生成的 ${kept.length} 行台词。确认无误后点“保存”` : '已停止生成，还没有完整的台词行，台词保持原样');
      return;
    }
    if (was.text.trim()) { $('brief').value = was.kind === 'extend' ? `${was.before.brief}\n\n${was.text.trim()}` : was.text.trim(); markDirty(); }
    else $('brief').value = was.before.brief;
    await api(`/api/tasks/${id}/pause`, 'POST', {});
    toast(was.text.trim() ? `已停止生成，保留已生成的 ${was.text.trim().length.toLocaleString()} 字草稿。确认无误后点“保存”` : '已停止生成，还没有输出内容，草稿保持原样');
    return;
  }
  await api(`/api/tasks/${id}/pause`, 'POST', {});
  toast('已放弃本次生成，草稿和台词恢复为生成前的样子');
}
$$('[data-stop-llm]').forEach(b => b.onclick = () => safe(() => stopLLM()));
$$('[data-discard-llm]').forEach(b => b.onclick = () => safe(() => stopLLM({keep: false})));

/* ───────── 视频工作台 ───────── */
function renderVideoAssets() { renderFootage(); }

function renderVideo() {
  data.video ||= {assets: [], options: {width: 1080, height: 1920, fps: 30, font_size: 48, image_seconds: 5, burn_subtitles: true}};
  videoDirty = false;
  const options = data.video.options, size = `${options.width}x${options.height}`;
  if (!$$('option', $('videoSize')).some(o => o.value === size)) $('videoSize').add(new Option(size.replace('x', ' × '), size));
  $('videoSize').value = size;
  if (!$$('option', $('videoFps')).some(o => Number(o.value) === options.fps)) $('videoFps').add(new Option(`${options.fps} fps`, String(options.fps)));
  $('videoFps').value = options.fps;
  $('videoImageSeconds').value = options.image_seconds;
  $('videoFontSize').value = options.font_size;
  $('videoBurnSubtitles').checked = options.burn_subtitles;
  $('videoSubtitleLegend').innerHTML = data.script.cast.map(member => `<span><i style="background:${escape(config.subtitle_colors?.[member.slot] || '#FFFFFF')}"></i>${escape(data.cast[member.slot]?.role || member.slot)} · 白字${member.slot === 'p1' ? '橙色' : '蓝色'}描边</span>`).join('');
  $('videoAudioState').textContent = data.audio_current ? `完整音频 · ${fmtTime(data.audio_export.duration)}` : '请先合并导出完整音频';
  renderVideoAssets();
  renderDesign();
  const meta = data.video_export;
  const file = Object.entries(meta?.files || {}).find(([name]) => name.endsWith('.mp4'))?.[1];
  $('approveVideo').disabled = !data.video_current || !data.workflow.approvals.audio;
  $('approveVideo').textContent = data.workflow.approvals.video ? '成片已确认' : '确认当前成片';
  $('finalVideo').hidden = !file; $('videoEmpty').hidden = !!file;
  if (file) {
    const source = url('/files/exports/' + file.path);
    if ($('finalVideo').getAttribute('src') !== source) $('finalVideo').src = source;
    $('videoState').textContent = `${meta.mode === 'demo' ? '测试音频 · ' : ''}${fmtTime(meta.duration)} · ${meta.options.width} × ${meta.options.height} · ${data.video_current ? '与当前工程一致' : '设置或工程已变化，请重新合成'}`;
    $('videoState').classList.toggle('stale', !data.video_current);
    $('videoExports').innerHTML = Object.entries(meta.files).map(([name, f]) => `<a href="${url('/files/exports/' + f.path)}" download="${escape(name)}">↓ ${escape(name)} <small>${fmtBytes(f.bytes)}</small></a>`).join('');
  } else {
    $('finalVideo').removeAttribute('src'); $('finalVideo').load();
    $('videoState').textContent = '';
    $('videoExports').innerHTML = '<p class="hint">合成完成后可在这里预览并下载带字幕的 MP4。</p>';
  }
}
async function saveVideo() {
  data.video = await api(url('/video'), 'PUT', videoBody());
  videoDirty = false;
}
for (const id of ['videoSize', 'videoFps', 'videoImageSeconds', 'videoFontSize', 'videoBurnSubtitles']) $(id).onchange = () => {
  videoDirty = true; markDirty(); renderDesignScene(); scheduleCoverPreview();
};
$('saveVideo').onclick = () => safe(async () => { await save(); await saveVideo(); await selectProject(current, {keepTab: true}); toast('视频设置已保存'); });
$('videoFiles').onchange = () => safe(async () => {
  const files = [...$('videoFiles').files], name = current;
  if (!files.length) return;
  await save(); await saveVideo();
  videoUploading = true; $('videoFiles').disabled = true; renderTasks();
  try {
    for (let i = 0; i < files.length; i++) {
      $('videoUploadState').textContent = `上传 ${i + 1}/${files.length}：${files[i].name}`;
      const form = new FormData(); form.append('file', files[i]);
      const config = await uploadFootage(name, form, (loaded, total) => { $('videoUploadState').textContent = `上传 ${i + 1}/${files.length} · ${total ? Math.round(loaded / total * 100) + '%' : fmtBytes(loaded)} · ${files[i].name}${total && loaded >= total ? ' · 正在读取素材信息…' : ''}`; });
      if (name === current) { data.video = config; renderVideoAssets(); renderDesignScene(); renderTasks(); }
    }
    if (name === current) await selectProject(current, {keepTab: true});
    toast(`已添加 ${files.length} 个素材`);
  } finally {
    videoUploading = false; $('videoFiles').value = ''; $('videoUploadState').textContent = ''; renderTasks();
  }
});
async function approval(gate) {
  await save();
  await api(url(`/approve/${gate}`), 'POST', {});
  data.workflow.approvals[gate] = true;
  toast({script: '剧本已定稿', binding: '音色绑定已确认', audio: '完整音频已确认', video: '当前成片已确认'}[gate]);
  if (gate === 'video') await selectProject(current, {keepTab: true});
  if (gate === 'script') switchTab('binding'); else if (gate === 'binding') switchTab('production'); else if (gate === 'audio') switchTab('video'); else renderSteps();
}

/* ───────── 任务 ───────── */
function taskDetail(t) {
  const p = t.progress, parts = [];
  if (p?.total > 0 && ['lines', 'assets'].includes(p.kind)) {
    parts.push(`${p.completed}/${p.total} ${p.kind === 'lines' ? '行' : '个素材'}`);
    if (p.failed) parts.push(`失败 ${p.failed} 行`);
    if (p.concurrency) parts.push(t.status === 'running' ? `并发 ${p.concurrency} 组 · 当前 ${p.active_groups || 0} 组` : `并发 ${p.concurrency} 组`);
    else if (p.current_line && t.status === 'running') parts.push(`正在合成 ${p.current_line}`);
  }
  if (t.status === 'queued') parts.push('等待前序任务完成');
  else if (p?.stage && t.status === 'running') parts.push(p.stage);
  else if (!parts.length) parts.push(t.result || t.error || TASK_STATES[t.status] || t.status);
  if (t.elapsed_seconds != null) parts.push(`${['running'].includes(t.status) ? '已用时' : '用时'} ${fmtTime(t.elapsed_seconds)}`);
  return parts.join(' · ');
}

function renderTasks() {
  const tasks = [...lastTasks].reverse();
  tasks.sort((a, b) => (b.project === current) - (a.project === current));
  const active = lastTasks.filter(t => ['queued', 'running'].includes(t.status));
  $('taskBadge').textContent = active.length || '';
  const busy = videoUploading || active.some(t => t.project === current);
  $('synthesisConcurrency').disabled = busy;
  $$('[data-action]').forEach(b => b.disabled = busy || (b.dataset.action === 'render' && !data?.audio_current) || (b.dataset.action === 'merge' && (!data?.script.lines.length || data.preflight?.reusable !== data.script.lines.length || dirty)));
  renderDraftTimeline();
  const coverReady = data?.cover_current;
  $('approveVideo').disabled = busy || videoDirty || !coverReady || !data?.video_current || !data?.workflow.approvals.audio;
  for (const id of ['videoFiles', 'saveVideo', 'overlayFiles', 'coverFile', 'previewCover']) $(id).disabled = busy;
  $$('input, select, textarea, button', $('overlayControls')).forEach(el => el.disabled = busy);
  $$('input, select, textarea', document.querySelector('.video-cover-card')).forEach(el => el.disabled = busy);
  $('video').classList.toggle('design-busy', busy);
  if (busy) $('designBackgroundVideo').pause();
  $$('[data-video-move], [data-video-remove], [data-source-open], [data-source-remove], [data-clip-edit], [data-clip-add]').forEach(b => b.disabled = busy || b.dataset.boundary === 'true');
  $$('button, input, select', $('footageEditor')).forEach(el => el.disabled = busy);
  updateLineReview();

  $('tasks').innerHTML = tasks.slice(0, 20).map(t => {
    const p = t.progress;
    const measurable = p && p.total > 0 && ['lines', 'assets'].includes(p.kind);
    const pct = measurable ? Math.round(p.completed / p.total * 100) : 0;
    const detail = taskDetail(t);
    const msg = t.error || (t.result && !detail.includes(t.result) ? t.result : '');
    return `<div class="task ${t.project && t.project !== current ? 'other' : ''}">
      <div class="task-top"><b>${escape(TASK_LABELS[t.label] || t.label)}</b><span class="task-state ${t.status}">${escape(TASK_STATES[t.status] || t.status)}</span></div>
      <div class="task-sub">${escape(t.project || '音色工厂')} · ${escape(detail)}</div>
      ${t.status === 'running' ? `<div class="progress ${measurable ? '' : 'indeterminate'}"><i style="width:${measurable ? pct : 35}%"></i></div>` : ''}
      ${msg ? `<div class="task-msg ${t.status === 'failed' ? 'bad' : ''}">${escape(msg)}</div>` : ''}
      ${['queued', 'running'].includes(t.status) ? `<button class="btn sm" data-pause="${escape(t.id)}">${LLM_ACTIONS.has(t.label) || t.label === 'llm_test' ? '停止' : '暂停'}</button>` : ''}
      ${t.llm || t.log?.length ? `<button class="btn ghost sm" data-task-log="${escape(t.id)}">查看日志</button>` : ''}
    </div>`;
  }).join('') || '<div class="empty">暂无任务。生成剧本、合成或导出时会显示在这里。</div>';
  $$('[data-pause]', $('tasks')).forEach(b => b.onclick = () => safe(async () => {
    const task = lastTasks.find(t => t.id === b.dataset.pause);
    if (live?.task === b.dataset.pause) return stopLLM();
    await api(`/api/tasks/${b.dataset.pause}/pause`, 'POST', {});
    toast(LLM_ACTIONS.has(task?.label) ? '已停止生成' : '已请求暂停；正在进行的请求或 ffmpeg 会在当前步骤完成后停止。');
  }));
  $$('[data-task-log]', $('tasks')).forEach(b => b.onclick = () => { terminalFocus = b.dataset.taskLog; setTerminal({hidden: false, open: true}); });
}
async function pollTasks() {
  if (tasksPolling) return;
  tasksPolling = true;
  try {
  const tasks = await api('/api/tasks');
  lastTasks = tasks;
  renderTasks();
  const followed = live && tasks.find(t => t.id === live.task);
  // 完整工作流写完台词后进入配音：退出流式视图，回到可编辑的台词列表
  if (live && followed?.label === 'run' && activeTask(followed.status) && !followed.streaming && live.phase) {
    stopFollowing(); if (!dirty && current) { delete $('lines').dataset.streaming; await selectProject(current, {keepTab: true}); }
  }
  else if (live && (!followed || !activeTask(followed.status))) {
    const ok = followed?.status === 'succeeded', mine = live.project === current;
    stopFollowing(!ok);
    // 成功后直接载入新结果；有未保存修改时只替换生成的部分，不动其他输入
    if (ok && mine && dirty) {
      const fresh = await api(url(''));
      if (followed.label === 'generate' || followed.label === 'run') { data.script.lines = fresh.script.lines; data.manifest = fresh.manifest; }
      if (followed.label !== 'generate') $('brief').value = fresh.script.meta.brief;
      delete $('lines').dataset.streaming; renderLines(); renderBindings();
    }
  }
  // 刷新页面或切回工程时，接上仍在进行的模型输出
  const resumable = current && !live && tasks.find(t => t.project === current && t.streaming && t.label !== 'llm_test' && !stoppedTasks.has(t.id));
  if (resumable) followLLM(resumable);
  let refresh = false, fullRefresh = false, homeChanged = false, historyChanged = false;
  for (const t of tasks) {
    const prev = taskStatus.get(t.id);
    if (prev !== t.status && t.project) homeChanged = true;
    if (taskStatus.has(t.id) && prev !== t.status) {
      if (t.project === current && ['succeeded', 'failed', 'paused'].includes(t.status)) {
        refresh = true; if (!['synth', 'retry'].includes(t.label)) fullRefresh = true;
      }
      if (t.status === 'succeeded' && ['design','preview','create'].includes(t.label)) historyChanged = true;
      if (t.status === 'succeeded' && t.label !== 'create') toast(`${TASK_LABELS[t.label] || t.label}已完成${t.label === 'llm_test' && t.result ? `：${t.result}` : ''}`);
      if (['paused','interrupted'].includes(t.status) && !(LLM_ACTIONS.has(t.label) && t.status === 'paused')) toast(`${TASK_LABELS[t.label] || t.label}${TASK_STATES[t.status]}`);
      if (t.status === 'failed') { toast(`${TASK_LABELS[t.label] || t.label}失败：${t.error || ''}${t.llm ? '\n完整日志和模型原始输出见终端。' : ''}`, true); if (t.llm && t.project === current) setTerminal({hidden: false, open: true}); }
      if (t.status === 'succeeded' && !t.project && t.label === 'design') {
        if (t.id === currentDesignTask) {
          factoryFinishing = 'design'; renderFactoryTasks(); setRef('试音已生成，正在准备波形与播放器…');
          try {
            const result = await api('/api/voices/reference', 'POST', await referenceForm(`/api/previews/${t.result}`, 'design.wav'));
            await referenceEditor.load(result, '描述生成试音');
            setRef('', true);
          } catch (error) { setRef(`试听音频加载失败：${error.message}，可重新生成试音。`); toast(error.message, true); }
          finally { factoryFinishing = ''; renderFactoryTasks(); }
        }
      }
      if (t.status === 'succeeded' && !t.project && t.label === 'create') { if (t.id === currentCreateTask) setRef('音色已保存，可在音色库中试听与管理。', true); await refreshLibrary(); toast(`音色已保存到${config.tts_mode === 'gateway' ? ' Windows 实际 PT 库' : '本机 Qwen PT 目录'}`); }
      if ([currentDesignTask,currentCreateTask].includes(t.id) && ['failed','paused','interrupted'].includes(t.status)) setRef(`${t.label === 'design' ? '试音' : '克隆'}${TASK_STATES[t.status]}${t.error ? `：${t.error}` : '，可重新提交。'}`);
      if (t.label === 'sync' && ['succeeded', 'failed'].includes(t.status)) await refreshLibrary();
      if (t.label === 'preview' && t.status === 'succeeded') {
        const target = previewTasks.get(t.id);
        if (target) previewAudio.set(JSON.stringify([target.role,target.emotion]), {sha256: target.sha256, url: `/api/previews/${t.result}`});
        if (target && selectedAsset?.role === target.role && selectedAsset?.emotion === target.emotion && selectedAsset.sha256 === target.sha256) { $('assetPreview').src = `/api/previews/${t.result}`; $('assetPreview').hidden = false; }

      }
      if (t.label === 'llm_test' && ['succeeded', 'failed'].includes(t.status)) { $('llmTestState').textContent = t.result || t.error; $('llmTestState').classList.toggle('ready', t.status === 'succeeded'); }
    }
    const preview = previewTasks.get(t.id);
    if (preview) {
      preview.status = t.status; preview.error = t.error || '';
      if (t.status === 'succeeded' && t.result) previewAudio.set(JSON.stringify([preview.role, preview.emotion]), {sha256: preview.sha256, url: `/api/previews/${t.result}`});
    }
    taskStatus.set(t.id, t.status);
  }
  if (!current) {
    if (homeChanged) await refreshHome();
  }
  store.set('voicePreviewTasks', [...previewTasks].filter(([id]) => !id.startsWith('submitting:')).slice(-50));
  renderPreviewState(); renderFactoryTasks(); if (historyChanged) await refreshVoiceHistory();
  // 运行中也同步行状态，让进度点实时变化
  const running = tasks.some(t => t.project === current && activeTask(t.status) && !LLM_ACTIONS.has(t.label));
  if ((refresh || running) && current) {
    if (fullRefresh && !dirty) await selectProject(current, {keepTab: true});
    else {
      const name = current, fresh = await api(url(''));
      if (name === current) {
        for (const key of ['manifest', 'preflight', 'group_audio', 'speech_groups', 'audio_export', 'audio_current', 'workflow', 'video_current', 'cover_current']) data[key] = fresh[key];
        renderReport(data.preflight); renderLineStats(); updateLineReview(); updateExports(); renderSteps();
      }
    }
  }
  } finally { tasksPolling = false; }
}

/* ───────── 音色工厂 ───────── */
function setRef(text, ready = false) { $('referenceState').textContent = text; $('referenceState').hidden = !/(正在|生成中|克隆|失败|提交中|处理中)/.test(text) || ready; }
function voiceBody(kind) { return {action: kind, role: $('voiceRole').value, emotion: $('voiceEmotion').value, text: $('voiceText').value, instruct: $('voiceDescription').value, reference_id: referenceId, overwrite: $('voiceOverwrite').checked}; }
function openFactory(target = {}) {
  if (typeof target.role === 'string') $('voiceRole').value = target.role;
  if (typeof target.emotion === 'string') $('voiceEmotion').value = target.emotion;
  $('factoryDialog').showModal(); referenceEditor.render(); refreshMicrophones(); renderFactoryTasks(); safe(refreshVoiceHistory);
}

const referenceEditor = new ReferenceEditor({request: api, onPlay: () => $$('audio').forEach(audio => audio.pause()), onError: error => toast(error.message, true), onChange: result => {
  referenceId = result?.reference_id || '';
  $('createVoice').disabled = !result || !!capture;
  if (result) setRef('', true);
  else setRef('');
  renderFactoryTasks();
}});
async function referenceForm(source, filename) {
  const response = await fetch(source); if (!response.ok) throw Error('角色原声无法读取');
  const form = new FormData(); form.append('file', await response.blob(), filename); return form;
}

async function useReference(file, label) {
  if (referenceUploading) return;
  referenceUploading = true; referenceEditor.pending = true; referenceEditor.render(); renderFactoryTasks(); setRef('正在上传并处理角色原声…');
  try {
    const f = new FormData(); f.append('file', file);
    const result = await api('/api/voices/reference', 'POST', f);
    currentDesignTask = ''; await referenceEditor.load(result, label);
    setRef('', true); toast('角色原声已加载');
  } catch (error) { setRef(`角色原声处理失败：${error.message}`); throw error; }
  finally { referenceUploading = false; referenceEditor.pending = false; referenceEditor.render(); renderFactoryTasks(); }
}
function recordingControls() {
  const busy = !!capture;
  referenceEditor.pending = busy; referenceEditor.render();
  $('recordVoice').disabled = busy; $('stopRecording').hidden = !capture?.recording; $('cancelRecording').hidden = !busy;
  for (const id of ['voiceFile', 'designVoice', 'microphone', 'refreshMicrophones']) $(id).disabled = busy;
  $('createVoice').disabled = busy || !referenceEditor.buffer;
  renderFactoryTasks();
}
async function refreshMicrophones(notify = false) {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const selected = $('microphone').value;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
    $('microphone').replaceChildren(new Option('系统默认麦克风', ''), ...devices.map((d, i) => new Option(d.label || `麦克风 ${i + 1}`, d.deviceId)));
    if (devices.some(d => d.deviceId === selected)) $('microphone').value = selected;
    if (notify) toast('麦克风列表已刷新');
  } catch (e) { $('recordingTime').textContent = `麦克风列表读取失败：${e.message}`; toast(e.message, true); }
}
function releaseCapture(session) {
  clearInterval(session.timer); clearTimeout(session.stopTimeout); cancelAnimationFrame(session.raf);
  session.source?.disconnect(); session.node?.disconnect(); session.mute?.disconnect(); session.node?.port.close();
  session.stream?.getTracks().forEach(track => track.stop()); session.chunks = [];
}
function drawLive(session) {
  if (capture !== session || !session.recording) return;
  const canvas = $('liveWave'), painter = canvas.getContext('2d'); canvas.width = canvas.clientWidth || 800;
  painter.clearRect(0, 0, canvas.width, canvas.height); painter.strokeStyle = '#2f6feb'; painter.beginPath();
  for (let x = 0; x < canvas.width; x++) {
    const y = canvas.height / 2 - (session.latest?.[Math.floor(x / canvas.width * session.latest.length)] || 0) * canvas.height * .45;
    if (x) painter.lineTo(x, y); else painter.moveTo(x, y);
  }
  painter.stroke(); session.raf = requestAnimationFrame(() => drawLive(session));
}
function pcmFile(session) {
  const bytes = new ArrayBuffer(44 + session.frames * 2), view = new DataView(bytes);
  const text = (at, value) => [...value].forEach((ch, i) => view.setUint8(at + i, ch.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); text(8, 'WAVE'); text(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, session.rate, true); view.setUint32(28, session.rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, session.frames * 2, true);
  let frame = 0;
  for (const chunk of session.chunks) for (const value of chunk) { const v = Math.max(-1, Math.min(1, value)); view.setInt16(44 + frame++ * 2, Math.round(v * (v < 0 ? 32768 : 32767)), true); }
  return new File([bytes], 'recording.wav', {type: 'audio/wav'});
}
function finishRecording(session) {
  if (capture !== session) return;
  let file;
  if (session.frames && session.peak >= .00001) file = pcmFile(session);
  releaseCapture(session); capture = null; $('liveWave').hidden = true; recordingControls();
  if (!file) { toast('未采集到声音，请检查麦克风、静音和系统权限后重录。', true); return; }
  safe(async () => { await useReference(file, '直接 PCM 录音'); $('recordingTime').textContent = session.peak < .001 ? '录音音量偏低，请先试听' : '录音已保存'; });
}
function stopRecording() {
  if (!capture?.recording || capture.stopping) return;
  const session = capture; session.stopping = true; clearInterval(session.timer);
  session.node.port.postMessage('stop'); $('recordingTime').textContent = '正在整理录音…';
  session.stopTimeout = setTimeout(() => { if (capture === session) { cancelRecording(); toast('录音采集被中断，请重试。', true); } }, 3000);
}
async function startRecording() {
  if (capture) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) throw Error('当前浏览器不支持 PCM 录音，请用 Chrome / Edge / Safari 打开 localhost。');
  referenceEditor.stop();
  const session = {chunks: [], frames: 0, peak: 0, lastSound: performance.now()}; capture = session; recordingControls();
  $('recordingTime').textContent = '等待麦克风权限…';
  try {
    const ctx = referenceEditor.context(); await ctx.resume();
    if (capture !== session) return;
    session.stream = await navigator.mediaDevices.getUserMedia({audio: {
      ...($('microphone').value ? {deviceId: {exact: $('microphone').value}} : {}),
      echoCancellation: false, noiseSuppression: false, autoGainControl: false,
    }, video: false});
    if (capture !== session || !$('factoryDialog').open) { releaseCapture(session); return; }
    await refreshMicrophones();
    recorderModule ||= ctx.audioWorklet.addModule('/recorder-worklet.js').catch(error => { recorderModule = null; throw error; });
    await recorderModule;
    if (capture !== session) { releaseCapture(session); return; }
    session.rate = ctx.sampleRate;
    session.node = new AudioWorkletNode(ctx, 'reference-recorder', {processorOptions: {maxSeconds: 120}});
    session.node.onprocessorerror = () => { if (capture === session) { cancelRecording(); toast('PCM 采集失败，请重试。', true); } };
    session.node.port.onmessage = ({data}) => {
      if (capture !== session) return;
      if (data.type === 'samples') {
        session.chunks.push(data.samples); session.frames += data.samples.length; session.latest = data.samples;
        const peak = data.samples.reduce((p, v) => Math.max(p, Math.abs(v)), 0); session.peak = Math.max(session.peak, peak);
        if (peak > .001) session.lastSound = performance.now();
      } else if (data.type === 'stopped' || data.type === 'limit') finishRecording(session);
    };
    session.source = ctx.createMediaStreamSource(session.stream); session.mute = ctx.createGain(); session.mute.gain.value = 0;
    session.source.connect(session.node).connect(session.mute).connect(ctx.destination);
    session.recording = true; session.started = performance.now(); $('liveWave').hidden = false; drawLive(session); recordingControls();
    session.stream.getAudioTracks().forEach(track => track.addEventListener('ended', () => { if (capture === session) stopRecording(); }));
    session.timer = setInterval(() => {
      const seconds = Math.floor((performance.now() - session.started) / 1000);
      $('recordingTime').textContent = performance.now() - session.lastSound > 3000 ? '未检测到声音，请检查静音或更换麦克风' : `PCM 录音中 ${seconds} 秒 / 120 秒`;
      if (ctx.state !== 'running') cancelRecording();
      else if (seconds >= 120) stopRecording();
    }, 200);
  } catch (error) {
    releaseCapture(session);
    if (capture !== session) return;
    capture = null; recordingControls(); $('liveWave').hidden = true;
    throw Error(error.name === 'NotAllowedError' ? '麦克风权限被拒绝，请在浏览器地址栏允许本站使用麦克风后重试。' : error.name === 'NotFoundError' ? '未找到可用麦克风。' : `无法开始录音：${error.message}`);
  }
}
function cancelRecording() {
  if (!capture) return;
  const session = capture; capture = null; releaseCapture(session); $('liveWave').hidden = true;
  $('recordingTime').textContent = '已取消录音'; recordingControls();
}

/* ───────── PT 库 ───────── */
const assetURL = a => `/api/voices/assets/${encodeURIComponent(a.role)}/${encodeURIComponent(a.emotion)}`;
function setButtonBusy(button, busy, label) {
  button.dataset.busy = String(busy); button.setAttribute('aria-busy', String(busy));
  button.disabled = busy; if (label) button.textContent = label;
}
async function libraryAction(button, label, fn) {
  if (libraryMutation) return;
  libraryMutation = true; const text = button.textContent;
  setButtonBusy(button, true, button.id === 'ptUploadControl' ? null : label);
  if (button.id === 'ptUploadControl') $('ptUploadState').textContent = '正在上传并校验文件，请稍候…';
  $('libraryGrid').inert = true; $('libraryGrid').dataset.locked = 'true';
  $('libraryNewMenu').inert = true;
  try { return await fn(); }
  finally {
    libraryMutation = false; setButtonBusy(button, false, button.id === 'ptUploadControl' ? null : text);
    if (button.id === 'ptUploadControl') $('ptUploadState').textContent = '仅支持 .pt，最大 32 MB。替换后无法恢复旧音色。';
    $('libraryGrid').inert = false; delete $('libraryGrid').dataset.locked; $('libraryNewMenu').inert = false;
    renderMetadataState(); renderPreviewState();
  }
}
function libraryView() { $('libraryNewMenu').open = false; $('voiceMoreMenu').open = false; }

async function refreshLibrary() {
  if (libraryRefresh) return libraryRefresh;
  libraryRefresh = (async () => {
    setButtonBusy($('refreshVoiceLibrary'), true, '刷新中');
    try {
      const [assets, status, deletions] = await Promise.all([api('/api/voices/assets'), api('/api/voices/status'), api('/api/voices/deletions')]);
      voiceAssets = assets; libraryEditable = status.editable; renderVoiceAssets();
      $('libraryCount').textContent = `${new Set(assets.map(a => a.role)).size} 个角色 · ${assets.length} 个音色`;
      $('voiceSource').textContent = `${config.tts_mode === 'gateway' ? 'Windows 音色库' : '本机音色库'} · ${status.message}`;
      $('voiceSource').title = config.voice_source || '';
      $('ptFile').disabled = !libraryEditable;
      $('libraryNotice').hidden = true;
      const pending = deletions.filter(i => i.status === 'pending');
      $('remoteDeletionState').hidden = !pending.length;
      $('remoteDeletionState').innerHTML = pending.length ? `${pending.length} 个 Windows 副本待删除：${pending.map(i => escape(`${i.role}/${i.emotion} ${i.error || ''}`)).join('；')} <button type="button" class="btn sm" id="retryRemoteDeletion">重试删除同步</button>` : '';
      if (pending.length) $('retryRemoteDeletion').onclick = () => safe(() => queue('/api/voices/deletions/retry', {}));
      await inventory(); await refreshVoiceHistory(); if (data) { renderBindings(); renderLines(); }
      renderMetadataState();
    } catch (error) {
      $('libraryNotice').hidden = false; $('libraryNotice').textContent = `加载失败：${error.message}。可点击上方「刷新」重试。`;
      throw error;
    } finally { setButtonBusy($('refreshVoiceLibrary'), false, '刷新'); }
  })();
  try { return await libraryRefresh; } finally { libraryRefresh = null; }
}
async function openLibrary(role = '') {
  libraryView(); $('voiceSearch').value = role;
  $('libraryDialog').showModal();
  $('libraryNotice').hidden = false; $('libraryNotice').textContent = '正在读取音色库…';
  $('libraryNotice').dataset.busy = 'true'; $('libraryGrid').setAttribute('aria-busy', 'true');
  try {
    await refreshLibrary();
    selectVoiceAsset(voiceAssets.find(v => !role || v.role === role) || null);
  } finally { $('libraryNotice').dataset.busy = 'false'; $('libraryGrid').setAttribute('aria-busy', 'false'); }
}
const expandedVoiceRoles = new Set();
function renderVoiceAssets() {
  const q = $('voiceSearch').value.trim().toLowerCase();
  const filtered = voiceAssets.filter(a => `${a.role} ${a.emotion} ${a.notes}`.toLowerCase().includes(q));
  const roles = [...new Set(filtered.map(a => a.role))];
  $('voiceAssets').innerHTML = roles.map(role => {
    const children = filtered.filter(a => a.role === role);
    const open = q || expandedVoiceRoles.has(role) || selectedAsset?.role === role;
    return `<details class="asset-role-group" data-role-group="${escape(role)}" ${open ? 'open' : ''}><summary class="asset-role-heading"><button type="button" class="role-chevron" data-role-toggle aria-label="展开或收起 ${escape(role)}">›</button><b>${escape(role)}</b><span>${children.length} 个情绪</span></summary><div class="asset-emotions">${children.map(a => `<button type="button" class="asset-item ${selectedAsset?.role === a.role && selectedAsset?.emotion === a.emotion ? 'selected' : ''}" aria-pressed="${selectedAsset?.role === a.role && selectedAsset?.emotion === a.emotion}" data-asset-role="${escape(a.role)}" data-asset-emotion="${escape(a.emotion)}"><b>${escape(a.emotion)}</b><small>${a.usage.length ? `${a.usage.length} 处绑定` : '未绑定'}</small></button>`).join('')}</div></details>`;
  }).join('') || `<p class="empty">${q ? '没有匹配的音色，试试其他关键词。' : '还没有音色。点击「新建音色」开始。'}</p>`;
  $$('[data-role-group]', $('voiceAssets')).forEach(group => group.querySelector('summary').onclick = event => {
    event.preventDefault(); const role = group.dataset.roleGroup;
    if (event.target.closest('[data-role-toggle]')) { group.open = !group.open; if (group.open) expandedVoiceRoles.add(role); else expandedVoiceRoles.delete(role); return; }
    expandedVoiceRoles.add(role); selectVoiceAsset(filtered.find(a => a.role === role));
  });
  $$('[data-role-group]', $('voiceAssets')).forEach(group => group.ontoggle = () => { if (group.open) expandedVoiceRoles.add(group.dataset.roleGroup); else expandedVoiceRoles.delete(group.dataset.roleGroup); });
  $$('[data-asset-role]', $('voiceAssets')).forEach(b => b.onclick = () => selectVoiceAsset(voiceAssets.find(a => a.role === b.dataset.assetRole && a.emotion === b.dataset.assetEmotion)));
}
function addEmotion(role) {
  if ([currentDesignTask,currentCreateTask].some(id => activeTask(lastTasks.find(t => t.id === id)?.status))) { toast('请等待当前音色生成完成后再添加情绪', true); return; }
  referenceEditor.close(); referenceEditor.buffer = null; referenceId = ''; $('referenceEditor').hidden = true; $('voicePreview').hidden = true; $('voiceText').value = ''; $('voiceDescription').value = ''; setRef('');
  $('libraryDialog').close(); $('voiceOverwrite').checked = false;
  openFactory({role, emotion: ''}); $('voiceEmotion').focus();
}
function renderMetadataState() {
  const changed = selectedAsset && ['role', 'emotion', 'ref_text', 'notes'].some((key, i) => $(['assetRole','assetEmotion','assetRefText','assetNotes'][i]).value !== (selectedAsset[key] || ''));
  $('saveVoiceAsset').hidden = !selectedAsset;
  $('saveVoiceAsset').disabled = libraryMutation || !changed || !libraryEditable || selectedAsset?.editable === false;
  $('deletePT').disabled = libraryMutation || !libraryEditable || !selectedAsset || selectedAsset.editable === false;
  $('ptFile').disabled = libraryMutation || !libraryEditable;
  $('ptUploadControl').classList.toggle('disabled', $('ptFile').disabled);
  $('ptUploadControl').setAttribute('aria-disabled', String($('ptFile').disabled));
}
function selectedPreview() {
  return selectedAsset && [...previewTasks.values()].reverse().find(t => t.role === selectedAsset.role && t.emotion === selectedAsset.emotion && t.sha256 === selectedAsset.sha256);
}
function renderPreviewState() {
  const state = selectedPreview(), busy = activeTask(state?.status);
  const label = state?.status === 'submitting' ? '提交中…' : state?.status === 'queued' ? '排队中…' : state?.status === 'running' ? '生成中…' : '生成试听';
  setButtonBusy($('previewPT'), busy, label);
  $('previewPT').disabled = busy || !selectedAsset || libraryMutation;
  $('assetPreviewText').disabled = busy;
  $('assetPreviewProgress').hidden = !busy;
  const cached = selectedAsset && previewAudio.get(JSON.stringify([selectedAsset.role,selectedAsset.emotion]));
  const valid = cached && cached.sha256 === selectedAsset.sha256;
  const el = $('assetPreviewState'); el.className = `preview-state ${busy ? 'running' : state?.status || ''}`;
  el.textContent = state?.status === 'submitting' ? '正在提交试听请求…' : state?.status === 'queued' ? '等待音色服务，可切换音色或关闭窗口' : state?.status === 'running' ? '正在生成声音，完成后会显示播放器' : state?.status === 'failed' ? `生成失败：${state.error || '请重试'}` : ['paused','interrupted'].includes(state?.status) ? '试听已停止，可以重新生成' : valid ? '试听已就绪' : '生成后可在这里播放';
  $('assetPreview').hidden = !valid || busy;
  if (valid && $('assetPreview').getAttribute('src') !== cached.url) $('assetPreview').src = cached.url;
}
function selectVoiceAsset(asset, draft = {}) {
  $('assetPreview').pause(); selectedAsset = asset; renderVoiceAssets();
  $('voiceDetailTitle').textContent = asset ? `${asset.role} · ${asset.emotion}` : '导入音色';
  $('voiceDetailSubtitle').textContent = asset ? 'VOICE / 音色详情' : 'NEW VOICE / 新建条目';
  $('assetRole').value = asset?.role || draft.role || ''; $('assetEmotion').value = asset?.emotion || draft.emotion || '';
  $('assetRefText').value = asset?.ref_text || ''; $('assetNotes').value = asset?.notes || '';
  $('assetInfo').textContent = asset ? `${asset.filename} · ${fmtBytes(asset.bytes)}
SHA256 ${asset.sha256}` : '';
  $('assetUsage').textContent = asset?.usage.length ? `已用于 ${asset.usage.map(u => `${u.project}/${u.slot}`).join('、')}。重命名前需解除绑定，替换文件会使原配音失效。` : asset ? '尚未绑定工程。修改名称和备注不会改变声音；不同情绪需要独立音色。' : '填写角色与情感后导入 .pt 文件，或从「新建音色」录音 / 描述克隆。';
  $('addRoleEmotion').hidden = !asset;
  for (const id of ['assetExisting','voiceMoreMenu','voiceFileInfo','voiceDanger']) $(id).hidden = !asset;
  $('voiceMetadata').open = !asset; $('voiceFileInfo').open = false; $('voiceMoreMenu').open = false;
  $('voiceMetadataTitle').textContent = asset ? '编辑音色信息' : '填写音色信息';
  $('ptUploadLabel').textContent = asset ? '替换 PT 文件' : '选择 PT 并导入';
  $('ptUploadState').textContent = asset ? '替换后无法恢复旧音色。仅支持 .pt，最大 32 MB。' : '仅支持 .pt，最大 32 MB。文件上传后自动保存。';
  $('assetPreviewText').value = selectedPreview()?.text || '你好，这是一段音色试听。';
  if (asset) $('downloadPT').href = assetURL(asset) + '/download';
  renderMetadataState(); renderPreviewState(); renderVoiceHistory();
}
function renderFactoryTasks() {
  for (const [kind, id, taskId, normal] of [['design','designVoice',currentDesignTask,'按描述生成试音'],['create','createVoice',currentCreateTask,'克隆为 PT 音色']]) {
    const status = factoryFinishing === kind ? 'processing' : factorySubmitting === kind ? 'submitting' : lastTasks.find(t => t.id === taskId)?.status;
    const busy = activeTask(status) || status === 'processing';
    setButtonBusy($(id), busy, busy ? status === 'processing' ? '处理音频中…' : status === 'running' ? '生成中…' : status === 'queued' ? '排队中…' : '提交中…' : normal);
    $(id).disabled = busy || !!capture || (kind === 'create' && !referenceEditor.buffer);
  }
  const busy = !!factorySubmitting || !!factoryFinishing || referenceUploading || [currentDesignTask,currentCreateTask].some(id => activeTask(lastTasks.find(t => t.id === id)?.status));
  for (const id of ['designVoice','createVoice']) $(id).disabled ||= busy;
  referenceEditor.locked = busy || !!capture; referenceEditor.render();
  for (const id of ['voiceFile','recordVoice']) $(id).disabled = busy || !!capture;
  const syncing = syncSubmitting || lastTasks.some(t => !t.project && t.label === 'sync' && activeTask(t.status));
  setButtonBusy($('syncVoices'), syncing, syncing ? '刷新中…' : '刷新音色库');
  $('voiceFileLabel').dataset.busy = String(referenceUploading);
  $('voiceFileLabel').classList.toggle('disabled', busy || !!capture);
  $('voiceFileLabel').setAttribute('aria-disabled', String(busy || !!capture));
  $('uploadReferenceLabel').textContent = referenceUploading ? '处理音频中…' : '上传角色原声';
}
async function submitFactory(kind) {
  if (factorySubmitting || factoryFinishing || referenceUploading || [currentDesignTask,currentCreateTask].some(id => activeTask(lastTasks.find(t => t.id === id)?.status))) return;
  if (!$('voiceRole').value.trim() || !$('voiceEmotion').value.trim()) throw Error('请填写角色名称与情绪名称');
  if (!$('voiceText').value.trim()) throw Error('请填写对应台词');
  if (kind === 'create' && !referenceEditor.buffer) throw Error('请先上传音频、录音或生成试音');
  factorySubmitting = kind; renderFactoryTasks();
  try {
    if (kind === 'create') referenceId = (await referenceEditor.prepare()).reference_id;
    const task = await api('/api/voices/actions', 'POST', voiceBody(kind));
    toast(kind === 'create' ? '音色克隆已加入队列' : '描述试音已加入队列');
    if (kind === 'design') { currentDesignTask = task.id; setRef('试音生成中，完成后可编辑波形…'); }
    else { currentCreateTask = task.id; setRef('正在克隆音色，完成后会保存到音色库…'); }
    taskStatus.set(task.id, 'queued'); lastTasks = [...lastTasks, task];
    await pollTasks();
  } catch (error) { setRef(`提交失败：${error.message}`); throw error; }
  finally { factorySubmitting = ''; recordingControls(); renderFactoryTasks(); }
}

/* Durable auditions retain every take, including voices later replaced. */
let voiceHistory = [], voiceHistoryRefresh = null;
const PREVIEW_KINDS = {preview: 'PT 试听', design: '描述试音', original: '角色原声'};
async function refreshVoiceHistory() {
  if (voiceHistoryRefresh) return voiceHistoryRefresh;
  voiceHistoryRefresh = (async () => {
    voiceHistory = await api('/api/voices/previews');
    const seen = new Set();
    for (const entry of voiceHistory) {
      if (entry.kind !== 'preview' || !entry.voice_sha256) continue;
      const key = JSON.stringify([entry.role,entry.emotion]);
      if (!seen.has(key)) { previewAudio.set(key, {sha256:entry.voice_sha256,url:entry.audio_url}); seen.add(key); }
    }
    renderVoiceHistory(); renderPreviewState();
  })();
  try { await voiceHistoryRefresh; } finally { voiceHistoryRefresh = null; }
}
function renderVoiceHistory() {
  const factoryRole = $('voiceRole').value.trim(), emotion = $('voiceEmotion').value.trim();
  const search = $('voiceHistorySearch').value.trim().toLowerCase();
  for (const [id, entries] of [
    ['assetHistory', voiceHistory.filter(p => p.role === selectedAsset?.role && p.emotion === selectedAsset?.emotion)],
    ['factoryHistory', voiceHistory.filter(p => !factoryRole || p.role === factoryRole && (!emotion || p.emotion === emotion))],
    ['allVoiceHistory', voiceHistory.filter(p => !search || `${p.role} ${p.emotion} ${p.text}`.toLowerCase().includes(search))],
  ]) {
    const host = $(id);
    // Keep an existing player alive during background refreshes.
    const signature = JSON.stringify([entries.map(p => p.id), selectedAsset?.role, selectedAsset?.emotion, selectedAsset?.sha256]);
    if (host.dataset.entries === signature) continue;
    host.dataset.entries = signature;
    host.querySelectorAll('audio').forEach(audio => audio.pause());
    host.innerHTML = entries.map(entry => {
      const outdated = entry.kind === 'preview' && selectedAsset?.role === entry.role && selectedAsset?.emotion === entry.emotion && entry.voice_sha256 && entry.voice_sha256 !== selectedAsset.sha256;
      return `<article class="preview-take"><div class="preview-take-head"><b>${escape(entry.role || '历史试听')}${entry.emotion ? ` · ${escape(entry.emotion)}` : ''}</b><span>${PREVIEW_KINDS[entry.kind] || '试听'}${outdated ? ' · 旧版音色' : ''}</span></div><time>${escape(new Date(entry.created_at).toLocaleString())} · ${fmtTime(entry.duration)}</time>${entry.text ? `<p class="preview-take-text">${escape(entry.text)}</p>` : ''}<audio controls preload="none" src="${escape(entry.audio_url)}" aria-label="${escape(entry.role || '历史试听')} ${escape(PREVIEW_KINDS[entry.kind] || '')}"></audio><div class="row"><a class="btn ghost sm" href="${escape(entry.audio_url)}" download="${escape(entry.role || '试听')}-${entry.id}.wav">下载</a><button type="button" class="btn ghost sm" data-use-preview="${escape(entry.id)}">用作角色原声</button><button type="button" class="btn ghost sm" data-record-preview="${escape(entry.id)}">重新录音</button></div></article>`;
    }).join('') || '<p class="empty">暂无试听记录</p>';
    $$('a[download]', host).forEach(link => link.onclick = () => toast('试听音频已开始下载'));
    $$('[data-record-preview]', host).forEach(button => button.onclick = () => safe(() => recordVoiceAgain(button.dataset.recordPreview)));
    $$('[data-use-preview]', host).forEach(button => button.onclick = () => safe(() => useVoicePreview(button.dataset.usePreview)));
  }
}
function recordVoiceAgain(id) {
  if (referenceUploading || capture || [currentDesignTask,currentCreateTask].some(task => activeTask(lastTasks.find(t => t.id === task)?.status))) throw Error('请等待当前录音或生成结束');
  const entry = voiceHistory.find(p => p.id === id); if (!entry) return;
  $('voiceHistoryDialog').close(); $('libraryDialog').close();
  referenceEditor.close(); referenceEditor.buffer = null; referenceId = ''; $('referenceEditor').hidden = true;
  $('voiceText').value = entry.text || ''; $('voiceOverwrite').checked = !!voices[entry.role]?.includes(entry.emotion);
  if (!$('factoryDialog').open) openFactory({role:entry.role,emotion:entry.emotion || '平静'});
  else { $('voiceRole').value = entry.role; $('voiceEmotion').value = entry.emotion || '平静'; }
  $('factoryDialog').querySelector('[data-reference-method="record"]').click(); renderFactoryTasks();
}
async function useVoicePreview(id) {
  if (referenceUploading || capture || [currentDesignTask,currentCreateTask].some(task => activeTask(lastTasks.find(t => t.id === task)?.status))) throw Error('请等待当前录音或生成结束');
  const entry = voiceHistory.find(p => p.id === id); if (!entry) return;
  const wasOpen = $('factoryDialog').open;
  $('voiceHistoryDialog').close(); $('libraryDialog').close();
  if (!wasOpen) { $('voiceOverwrite').checked = false; openFactory({role:entry.role,emotion:entry.emotion || '平静'}); }
  const form = await referenceForm(entry.audio_url, 'original.wav');
  await useReference(form.get('file'), '试听原声'); $('voiceText').value = entry.text || ''; renderFactoryTasks();
}
$('openVoiceHistory').onclick = () => safe(async () => { $('voiceHistoryDialog').showModal(); await refreshVoiceHistory(); });
$('closeVoiceHistory').onclick = () => $('voiceHistoryDialog').close();
$('voiceHistorySearch').oninput = renderVoiceHistory;
for (const id of ['voiceRole','voiceEmotion']) $(id).addEventListener('input', renderVoiceHistory);
for (const id of ['voiceHistoryDialog','libraryDialog','factoryDialog']) $(id).addEventListener('close', () => { $(id).querySelectorAll('audio').forEach(audio => audio.pause()); });
document.addEventListener('play', event => { if (event.target instanceof HTMLAudioElement) { $$('audio').forEach(audio => { if (audio !== event.target) audio.pause(); }); referenceEditor.stop(); } }, true);

/* ───────── 多供应商 LLM ───────── */
function renderLLM() {
  $('llmFailover').checked = llmDraft.failover;
  $('llmProviders').innerHTML = llmDraft.providers.map(p => `<section class="provider-card" data-provider="${escape(p.id)}"><div class="row"><label class="check"><input type="radio" name="llmActive" value="${escape(p.id)}" ${p.id === llmDraft.active_provider ? 'checked' : ''}>默认模型</label><label class="check"><input type="checkbox" data-llm-field="enabled" ${p.enabled ? 'checked' : ''}>启用</label><button class="btn ghost sm danger" data-remove-provider>移除供应商</button></div><div class="grid-2"><label class="field">供应商名称<input data-llm-field="name" value="${escape(p.name)}"></label><label class="field">模型<input data-llm-field="model" value="${escape(p.model)}" placeholder="step-5-preview"></label></div><label class="field">Base URL<input data-llm-field="base_url" value="${escape(p.base_url)}" placeholder="https://api.stepfun.com/step_plan/v1"></label><label class="field">API Key（每行一个）<textarea data-llm-field="new_keys" rows="2" autocomplete="off" spellcheck="false" placeholder="${p.key_count ? `已有 ${p.key_count} 个 Key；留空保留，填写则全部替换` : '填写 API Key；本地免认证服务可留空'}">${escape(p.new_keys || '')}</textarea></label><label class="check"><input type="checkbox" data-llm-field="clear_keys" ${p.clear_keys ? 'checked' : ''}>清除已保存 Key</label><div class="row"><label class="check"><input type="checkbox" data-llm-field="json_mode" ${p.json_mode ? 'checked' : ''}>支持 JSON Mode</label><label class="check" title="边生成边显示，可随时停止；供应商不支持时自动按普通请求处理"><input type="checkbox" data-llm-field="stream" ${p.stream !== false ? 'checked' : ''}>流式输出</label><label class="field inline-field" title="流式时为两段输出之间的最长等待">无响应超时（秒）<input type="number" min="1" max="3600" data-llm-field="timeout_seconds" value="${p.timeout_seconds}"></label><label class="field inline-field">输出上限（tokens）<input type="number" min="32" max="2000000" data-llm-field="max_tokens" value="${p.max_tokens ?? ''}" placeholder="不限"></label></div></section>`).join('');
  $$('[data-provider]').forEach(row => {
    const p = llmDraft.providers.find(p => p.id === row.dataset.provider);
    $$('[data-llm-field]', row).forEach(el => el.oninput = () => p[el.dataset.llmField] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? (el.value === '' ? null : Number(el.value)) : el.value);
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
async function checkConnections(notify = false) {
  if (connectionBusy) return;
  connectionBusy = true; $('checkConnections').disabled = true;
  try {
    const c = await api('/api/connections');
    if (notify) toast(c.message || (c.ready ? '连接检测完成' : '连接尚未就绪'), !c.ready);
    $('connectionDot').textContent = c.mode === 'demo' ? '测试音' : c.ready ? '已连接' : '未就绪';
    $('connectionDot').className = `foot-state ${c.ready && c.mode !== 'demo' ? 'good' : 'bad'}`;
    $('openConnections').dataset.state = c.mode === 'demo' ? 'demo' : c.ready ? 'ok' : 'bad';
    $('mode').classList.toggle('offline', !c.ready);
    $('connectionDetails').innerHTML = `<p class="${c.ready ? 'good' : 'bad'}">${escape(c.message)}</p><dl><dt>服务地址</dt><dd>${escape(c.url)}</dd><dt>Gateway</dt><dd>${escape(c.gateway)}</dd><dt>Windows Agent</dt><dd>${escape(c.worker_id)} · ${escape(c.worker)}</dd><dt>GPU 模型</dt><dd>${escape(c.model || '尚未就绪')} ${escape(c.device || '')}</dd><dt>检测耗时</dt><dd>${c.latency_ms} ms</dd></dl><p class="hint">自动每 30 秒检测。Windows 关机或模型加载时会显示未就绪，恢复后自动更新。</p>`;
    if (c.mode === 'gateway' && c.worker === 'connected') {
      try {
        const caps = await api('/api/worker/capabilities');
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = caps.available ? `Windows 接口文档 · 协议 ${caps.document.protocol} · ${caps.rpc.length} 个 RPC` : caps.message;
        details.append(summary);
        if (caps.available) {
          const hint = document.createElement('p'); hint.className = 'hint';
          hint.textContent = caps.gateway_capabilities ? '能力发现路由已连接。音色编辑以 PT 管理接口实际返回为准。' : '已通过健康接口读取文档；Gateway 能力发现路由仍需升级。';
          const pre = document.createElement('pre'); pre.textContent = JSON.stringify(caps.document, null, 2);
          pre.style.cssText = 'max-height:360px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:12px';
          details.append(hint, pre);
        }
        $('connectionDetails').append(details);
      } catch (error) {
        const hint = document.createElement('p'); hint.className = 'hint'; hint.textContent = `接口文档读取失败：${error.message}`;
        $('connectionDetails').append(hint);
      }
    }
  } catch (error) {
    $('connectionDot').textContent = '检测失败'; $('connectionDot').className = 'foot-state bad'; $('openConnections').dataset.state = 'bad';
    if (notify) toast(error.message, true);
    $('mode').classList.add('offline'); $('connectionDetails').textContent = `Mac 服务连接异常：${error.message}`;
  } finally { connectionBusy = false; $('checkConnections').disabled = false; }
}

/* ───────── 事件绑定 ───────── */
$('new').onclick = $('welcomeNew').onclick = $('miniNew').onclick = () => $('newDialog').showModal();
$('closeDialog').onclick = () => $('newDialog').close();
$('newForm').onsubmit = event => {
  event.preventDefault();
  safe(async () => {
    const fields = new FormData(event.target);
    const p = await api('/api/projects', 'POST', {name: fields.get('name'), topic: fields.get('topic'), keywords: fields.get('keywords').split(/[,，]/).map(v => v.trim()).filter(Boolean)});
    $('newDialog').close(); event.target.reset();
    await selectProject(p.name); toast('工程已创建');
  });
};
$('projectFilter').oninput = renderProjects;
$('homeProjectFilter').oninput = renderHomeProjects;
$('homeVoices').onclick = () => safe(() => openLibrary());
$('homeLLM').onclick = () => $('openLLM').click();
$('save').onclick = () => safe(async () => { await save(); toast('修改已保存'); });
document.addEventListener('keydown', event => {
  if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === 's') { event.preventDefault(); if ($('promptDialog').open) savePrompts(); else if (data) safe(async () => { await save(); toast('修改已保存'); }); }
  else if (key === 'b' && !document.querySelector('dialog[open]')) { event.preventDefault(); setSideCollapsed(!sideCollapsed); }
  else if (key === 'j') { event.preventDefault(); setTerminal({hidden: !terminal.hidden}); }
});
['topic', 'keywords', 'guidance', 'brief'].forEach(id => $(id).oninput = markDirty);

/* ───────── 脚本提示词 ─────────
   打开时读取全局配置；取消撤回编辑，保存只写全局文件。 */
const PROMPT_HINTS = {
  brief: '所有工程的草稿生成与扩展共用。',
  structure: '所有工程的台词生成共用。',
  contract: '全局固定规范，生成台词时自动附加。',
};
let promptTab = 'brief', promptSnapshot = null;
const promptCustom = key => $(`${key}Prompt`).value.trim() !== (config.script_prompt_defaults?.[key] || '').trim();
function renderPromptState() {
  for (const key of ['brief', 'structure']) {
    const custom = promptCustom(key);
    $(`${key}PromptState`).textContent = custom ? '自定义' : '全局';
    $(`${key}PromptState`).parentElement.classList.toggle('custom', custom);
    document.querySelector(`[data-prompt-tab="${key}"]`).classList.toggle('custom', custom);
  }
  $$('[data-prompt-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.promptTab === promptTab)));
  $$('[data-prompt-pane]').forEach(p => p.hidden = p.dataset.promptPane !== promptTab);
  $('promptHint').textContent = PROMPT_HINTS[promptTab];
  const fixed = promptTab === 'contract';
  const area = fixed ? $('structureContract') : $(`${promptTab}Prompt`);
  const custom = !fixed && promptCustom(promptTab);
  $('promptStatus').textContent = fixed ? '固定规范' : custom ? '已自定义' : '默认写法';
  $('promptStatus').classList.toggle('custom', custom);
  $('promptCount').textContent = fixed ? `${area.value.length.toLocaleString()} 字` : `${area.value.length.toLocaleString()} / 20,000 字`;
  $('resetPrompt').hidden = fixed; $('resetPrompt').disabled = !custom;
}
function showPromptTab(tab) {
  promptTab = tab; renderPromptState();
  const area = tab === 'contract' ? $('structureContract') : $(`${tab}Prompt`);
  area.scrollTop = 0; if ($('promptDialog').open) area.focus({preventScroll: true});
}
function openPrompts(tab = 'brief') {
  if ($('promptDialog').open) { showPromptTab(tab); return; }
  safe(async () => {
    config.script_prompts = await api('/api/script-prompts');
    for (const key of ['brief', 'structure']) $(`${key}Prompt`).value = config.script_prompts[key];
    promptSnapshot = {...config.script_prompts};
    $('promptDialog').showModal(); showPromptTab(tab);
  });
}
const promptsChanged = () => !!promptSnapshot && ['brief', 'structure'].some(k => $(`${k}Prompt`).value !== promptSnapshot[k]);
function cancelPrompts() {
  if (promptsChanged() && !confirm('放弃对提示词的修改吗？')) return;
  if (promptSnapshot) for (const k of ['brief', 'structure']) $(`${k}Prompt`).value = promptSnapshot[k];
  promptSnapshot = null; $('promptDialog').close(); renderPromptState();
}
function savePrompts() {
  safe(async () => {
    $('savePrompts').disabled = true;
    try {
      if (promptsChanged()) {
        config.script_prompts = await api('/api/script-prompts', 'PUT', {
          brief: $('briefPrompt').value, structure: $('structurePrompt').value,
        });
        for (const key of ['brief', 'structure']) $(`${key}Prompt`).value = config.script_prompts[key];
        toast('全局提示词已保存');
      }
      promptSnapshot = null; $('promptDialog').close(); renderPromptState();
    } finally { $('savePrompts').disabled = false; }
  });
}
$$('[data-open-prompt]').forEach(b => b.onclick = () => openPrompts(b.dataset.openPrompt));
$$('[data-prompt-tab]').forEach(b => b.onclick = () => showPromptTab(b.dataset.promptTab));
['briefPrompt', 'structurePrompt'].forEach(id => $(id).oninput = renderPromptState);
$('resetPrompt').onclick = () => { $(`${promptTab}Prompt`).value = config.script_prompt_defaults[promptTab]; renderPromptState(); $(`${promptTab}Prompt`).focus(); };
$('closePrompts').onclick = $('cancelPrompts').onclick = cancelPrompts;
$('savePrompts').onclick = savePrompts;
$('promptDialog').addEventListener('cancel', event => { event.preventDefault(); cancelPrompts(); });

/* ───────── 布局：侧栏、分组与任务栏的折叠状态，记在本机 ───────── */
const layout = store.get('layout', {side: false, rail: true, sections: {}});
layout.sections ||= {};
const narrowSide = matchMedia('(max-width: 860px)'), drawerRail = matchMedia('(max-width: 1180px)');
let sideCollapsed = false, homeRail = false; // 主页任务栏默认收起，工程布局独立保留
function setSideCollapsed(collapsed, remember = true) {
  sideCollapsed = collapsed;
  if (remember && !narrowSide.matches) layout.side = collapsed;
  $('shell').classList.toggle('side-collapsed', collapsed);
  $('sideToggle').setAttribute('aria-expanded', String(!collapsed));
  $('sideToggle').title = collapsed ? '展开侧栏（⌘B）' : '折叠侧栏（⌘B）';
  if (remember && !narrowSide.matches) store.set('layout', layout);
  clearTimeout(growAll.t); growAll.t = setTimeout(growAll, 30);
}
function setSection(name, open, remember = true) {
  const sec = document.querySelector(`.side-sec[data-sec="${name}"]`);
  sec.classList.toggle('closed', !open);
  sec.querySelector('.sec-toggle').setAttribute('aria-expanded', String(open));
  layout.sections[name] = open; if (remember) store.set('layout', layout);
}
function railVisible() { return !current ? homeRail : drawerRail.matches ? $('shell').classList.contains('show-rail') : layout.rail; }
function setRail(open) {
  if (!current) homeRail = open;
  else if (drawerRail.matches) $('shell').classList.toggle('show-rail', open);
  else { layout.rail = open; store.set('layout', layout); }
  applyRail();
}
function applyRail() {
  if (!drawerRail.matches) $('shell').classList.remove('show-rail');
  else if (!current) $('shell').classList.toggle('show-rail', homeRail);
  $('shell').classList.toggle('rail-off', !current ? !homeRail : !layout.rail);
  $('railToggle').setAttribute('aria-pressed', String(railVisible()));
  clearTimeout(growAll.t); growAll.t = setTimeout(growAll, 30);
}
$('sideToggle').onclick = () => setSideCollapsed(!sideCollapsed);
$$('.sec-toggle').forEach(b => b.onclick = () => { const sec = b.closest('.side-sec'); setSection(sec.dataset.sec, sec.classList.contains('closed')); });
$$('[data-expand]').forEach(b => b.onclick = () => { setSideCollapsed(false); setSection(b.dataset.expand, true); });
$('closeRail').onclick = () => setRail(false);
drawerRail.addEventListener('change', applyRail);
narrowSide.addEventListener('change', () => setSideCollapsed(narrowSide.matches || layout.side, false));
// 窄屏下展开的侧栏浮在内容上方：点内容区或选中工程后自动收起
document.querySelector('.main').addEventListener('pointerdown', () => { if (narrowSide.matches && !sideCollapsed) setSideCollapsed(true, false); });
$('projects').addEventListener('click', () => { if (narrowSide.matches) setSideCollapsed(true, false); });
setSideCollapsed(narrowSide.matches || layout.side, false);
for (const name of ['projects', 'voices']) setSection(name, layout.sections[name] !== false, false);
applyRail();

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
$('railToggle').onclick = () => setRail(!railVisible());

$('bgmFile').onchange = () => safe(async () => {
  for (const file of $('bgmFile').files) { const f = new FormData(); f.append('file', file); data.media = await api(url('/assets/bgm'), 'POST', f); }
  $('bgmFile').value = ''; renderMedia(); toast('音乐已保存，重新导出后生效');
});
$('bgmVolume').oninput = () => $('bgmVolumeOut').textContent = Math.round($('bgmVolume').value * 100) + '%';
$('saveMix').onclick = () => safe(async () => { data.media = await api(url('/mix'), 'PUT', {bgm_volume: Number($('bgmVolume').value)}); renderMedia(); toast('混音设置已保存，重新导出后生效'); });
$('removeBgm').onclick = () => safe(async () => { data.media = await api(url('/mix'), 'PUT', {bgm_volume: Number($('bgmVolume').value), remove_bgm: true}); renderMedia(); toast('已移除音乐，重新导出后生效'); });

$('openFactory').onclick = openFactory;
$$('[data-open-factory]').forEach(b => b.onclick = openFactory);
$('closeFactory').onclick = () => $('factoryDialog').close();
$('factoryDialog').addEventListener('close', () => { cancelRecording(); referenceEditor.close(); });
$('recordVoice').onclick = () => safe(startRecording);
$('stopRecording').onclick = stopRecording;
$('cancelRecording').onclick = cancelRecording;
$('refreshMicrophones').onclick = () => refreshMicrophones(true);
navigator.mediaDevices?.addEventListener('devicechange', () => refreshMicrophones());
window.addEventListener('pagehide', () => { cancelRecording(); referenceEditor.close(); });
$('voiceFile').onchange = () => safe(async () => {
  const file = $('voiceFile').files[0]; if (!file) return;
  await useReference(file, `已上传 ${file.name}`); $('voiceFile').value = '';
});
$$('[data-reference-method]').forEach(button => button.onclick = () => {
  if (capture || factorySubmitting || factoryFinishing) { toast('请先结束当前录音或等待音频处理完成', true); return; }
  const method = button.dataset.referenceMethod;
  $$('[data-reference-method]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  $$('[data-reference-panel]').forEach(panel => panel.hidden = panel.dataset.referencePanel !== method);
});
$('designVoice').onclick = () => safe(() => submitFactory('design'));
$('createVoice').onclick = () => safe(() => submitFactory('create'));
$('syncVoices').onclick = () => safe(async () => {
  if (syncSubmitting || lastTasks.some(t => !t.project && t.label === 'sync' && activeTask(t.status))) return;
  syncSubmitting = true; renderFactoryTasks();
  try { await queue('/api/voices/actions', {action: 'sync'}); toast('音色库刷新任务已加入队列'); }
  finally { syncSubmitting = false; renderFactoryTasks(); }
});

$('openLibrary').onclick = () => safe(() => openLibrary());
$('closeLibrary').onclick = () => $('libraryDialog').close();
$('voiceSearch').oninput = renderVoiceAssets;
$('newVoiceAsset').onclick = () => { libraryView(); selectVoiceAsset(null); $('assetRole').focus(); };
$('refreshVoiceLibrary').onclick = () => safe(async () => {
  if (libraryMutation) return;
  const previous = selectedAsset; await refreshLibrary();
  selectVoiceAsset(voiceAssets.find(a => a.role === previous?.role && a.emotion === previous?.emotion) || voiceAssets[0] || null);
  toast('音色库已刷新');
});
for (const id of ['ptUploadControl','voiceFileLabel']) {
  $(id).onkeydown = event => { if (['Enter',' '].includes(event.key)) { event.preventDefault(); const file = $(id).querySelector('input'); if (!file.disabled) file.click(); } };
}
$('downloadPT').onclick = () => toast('PT 文件已开始下载');
$('downloadReference').onclick = () => safe(async () => { await referenceEditor.download(); toast('选区音频已开始下载'); });
for (const id of ['assetRole','assetEmotion','assetRefText','assetNotes']) $(id).oninput = renderMetadataState;
$('libraryCreate').onclick = () => { const target = selectedAsset ? {role: '', emotion: '平静'} : {role: $('assetRole').value, emotion: $('assetEmotion').value || '平静'}; $('libraryNewMenu').open = false; $('voiceOverwrite').checked = false; $('libraryDialog').close(); openFactory(target); };
$('addRoleEmotion').onclick = () => addEmotion(selectedAsset.role);
$('editBaseAudio').onclick = () => safe(async () => {
  const asset = selectedAsset;
  $('libraryDialog').close(); openFactory({role: asset.role, emotion: asset.emotion});
  $('voiceText').value = asset.ref_text; $('voiceOverwrite').checked = true;
  referenceId = ''; $('createVoice').disabled = true;
  referenceEditor.close(); referenceEditor.buffer = null; $('referenceEditor').hidden = true;
  if (!asset.has_reference) {
    referenceEditor.close(); referenceEditor.buffer = null; $('referenceEditor').hidden = true;
    $('voicePreview').hidden = true; referenceId = ''; $('createVoice').disabled = true;
    setRef('此 PT 没有保存角色原声，请上传原始录音或直接录音，再裁剪克隆。'); return;
  }
  referenceUploading = true; renderFactoryTasks(); setRef('正在读取角色原声…');
  try {
    const result = await api('/api/voices/reference', 'POST', await referenceForm(assetURL(asset) + '/reference', 'reference.wav'));
    await referenceEditor.load(result, `${asset.role}/${asset.emotion} 角色原声`);
    setRef('', true);
  } catch (error) { setRef(`角色原声加载失败：${error.message}`); throw error; }
  finally { referenceUploading = false; renderFactoryTasks(); }
});
$('voiceEditForm').onsubmit = event => { event.preventDefault(); safe(() => libraryAction($('saveVoiceAsset'), '保存中…', async () => {
  if (!selectedAsset || !libraryEditable) return;
  const updated = await api(assetURL(selectedAsset), 'PUT', {role: $('assetRole').value, emotion: $('assetEmotion').value, ref_text: $('assetRefText').value, notes: $('assetNotes').value});
  await refreshLibrary(); selectVoiceAsset(updated); toast('音色信息已更新');
})); };
$('ptFile').onchange = () => safe(async () => {
  const file = $('ptFile').files[0]; if (!file) return;
  if (!$('voiceEditForm').reportValidity()) { $('ptFile').value = ''; return; }
  await libraryAction($('ptUploadControl'), '上传中…', async () => {
  try {
    const f = new FormData(); f.append('file', file); f.append('role', selectedAsset?.role || $('assetRole').value); f.append('emotion', selectedAsset?.emotion || $('assetEmotion').value); f.append('overwrite', String(!!selectedAsset));
    f.append('ref_text', $('assetRefText').value); f.append('notes', $('assetNotes').value);
    const result = await api('/api/voices/import', 'POST', f); await refreshLibrary(); selectVoiceAsset(result); toast(config.tts_mode === 'gateway' ? 'PT 已保存到 Windows，Mac 缓存已校验更新' : 'PT 已保存到本机 Qwen 音色库');
  } finally { $('ptFile').value = ''; }
  });
});
$('deletePT').onclick = () => safe(async () => {
  const asset = selectedAsset;
  if (!asset || libraryMutation) return;
  const usage = asset.usage.length ? `\n将同时解除 ${asset.usage.length} 处工程绑定。` : '';
  if (!confirm(`确认删除「${asset.role} · ${asset.emotion}」吗？删除后无法恢复。${usage}`)) return;
  await libraryAction($('deletePT'), '删除中…', async () => {
    if (dirty) await save();
    const result = await api(assetURL(asset) + '?detach=true', 'DELETE');
    await refreshLibrary(); selectVoiceAsset(voiceAssets.find(a => a.role === asset.role) || null);
    if (current && !dirty) await selectProject(current, {keepTab:true});
    toast(result.remote_status === 'pending' ? '音色已删除，Windows 副本仍待同步；请重试删除同步。' : '音色已删除');
  });
});
$('previewPT').onclick = () => safe(async () => {
  if (!selectedAsset || activeTask(selectedPreview()?.status)) return;
  const text = $('assetPreviewText').value.trim(); if (!text) throw Error('请填写试听台词');
  const target = {role: selectedAsset.role, emotion: selectedAsset.emotion, sha256: selectedAsset.sha256, text, status: 'submitting'};
  const pending = `submitting:${JSON.stringify([target.role,target.emotion,target.sha256])}`;
  previewTasks.set(pending, target); renderPreviewState();
  try {
    const task = await api('/api/voices/actions', 'POST', {action: 'preview', role: target.role, emotion: target.emotion, text});
    previewTasks.delete(pending); target.status = task.status; previewTasks.set(task.id, target);
    store.set('voicePreviewTasks', [...previewTasks]); taskStatus.set(task.id, 'queued');
    renderPreviewState(); toast('音色试听已加入队列'); await pollTasks();
  } catch (error) {
    if (previewTasks.has(pending)) { target.status = 'failed'; target.error = error.message; }
    renderPreviewState(); throw error;
  }
});
// 点击其他位置、Escape 或选择操作后收起操作菜单。
$('libraryDialog').addEventListener('click', event => {
  for (const id of ['libraryNewMenu','voiceMoreMenu']) if (!$(id).contains(event.target)) $(id).open = false;
});
$('libraryDialog').addEventListener('cancel', event => {
  const menu = ['libraryNewMenu','voiceMoreMenu'].map($).find(el => el.open);
  if (menu) { event.preventDefault(); menu.open = false; }
});
$('openLLM').onclick = () => safe(async () => { llmDraft = await api('/api/llm/config'); renderLLM(); $('llmDialog').showModal(); });
$('closeLLM').onclick = () => $('llmDialog').close();
$('addLLMProvider').onclick = () => {
  const p = {id: 'provider-' + crypto.randomUUID().slice(0, 8), name: '', base_url: '', model: '', enabled: true, json_mode: true, stream: true, timeout_seconds: 180, max_tokens: null, key_count: 0};
  llmDraft.failover = $('llmFailover').checked; llmDraft.providers.push(p); llmDraft.active_provider ||= p.id; renderLLM();
};
$('saveLLM').onclick = () => safe(saveLLM);
$('testLLM').onclick = () => safe(async () => { await saveLLM(); await queue('/api/llm/test', {provider_id: llmDraft.active_provider}); $('llmTestState').textContent = '模型连接测试中…'; });
$('openConnections').onclick = () => { $('ttsMode').value = config.tts_mode === 'direct' ? 'direct' : 'gateway'; $('connectionDialog').showModal(); checkConnections(); };
$('closeConnections').onclick = () => $('connectionDialog').close();
$('checkConnections').onclick = () => checkConnections(true);
$('saveTTSMode').onclick = () => safe(async () => {
  if (dirty) await save();
  config = await api('/api/tts/mode', 'POST', {mode: $('ttsMode').value, local_url: $('localQwenURL').value});
  $('mode').textContent = config.tts_mode === 'gateway' ? '远端 Windows' : '本机 Qwen';
  referenceEditor.close(); referenceEditor.buffer = null; referenceId = ''; $('referenceEditor').hidden = true; $('createVoice').disabled = true;
  await checkConnections(); await refreshLibrary();
  if (current) await selectProject(current, {keepTab:true});
  toast(`已切换：${config.voice_source}`);
});

/* ───────── 终端：可拖动、可折叠、可隐藏，位置记在本机 ───────── */
const terminal = store.get('terminal', {hidden: false, open: false, right: 16, bottom: 16});
let terminalFocus = '';
function clampTerminal() {
  const el = $('terminal'); if (el.hidden) return;
  const maxRight = Math.max(8, innerWidth - el.offsetWidth - 8), maxBottom = Math.max(8, innerHeight - el.offsetHeight - 8);
  terminal.right = Math.min(Math.max(8, terminal.right), maxRight);
  terminal.bottom = Math.min(Math.max(8, terminal.bottom), maxBottom);
  el.style.right = terminal.right + 'px'; el.style.bottom = terminal.bottom + 'px';
}
function setTerminal(patch = {}, remember = true) {
  Object.assign(terminal, patch);
  const el = $('terminal');
  el.hidden = terminal.hidden; el.classList.toggle('collapsed', !terminal.open);
  $('terminalBody').hidden = !terminal.open;
  $('toggleTerminal').setAttribute('aria-expanded', String(terminal.open));
  $('showTerminal').setAttribute('aria-pressed', String(!terminal.hidden));
  $('terminalState').textContent = terminal.hidden ? '已隐藏' : '';
  clampTerminal(); if (remember) store.set('terminal', terminal);
  if (!terminal.hidden && terminal.open) refreshRuntime().then(() => { terminalFocus = ''; });
}
async function refreshRuntime() {
  if (runtimeBusy) return;
  runtimeBusy = true;
  try {
    const r = await api('/api/runtime'), c = r.connection, m = r.metrics;
    $('terminalBadge').textContent = c.ready ? `${c.worker_id || '本机'} 已就绪` : '未就绪';
    $('terminal').dataset.state = c.ready ? 'ok' : 'bad';
    const lines = [`[${new Date().toLocaleTimeString()}] ${config.tts_mode === 'gateway' ? 'Windows / Gateway' : '本机 Qwen'} ${c.url}`, `连接: ${c.gateway} · Worker: ${c.worker} · ${c.message}`, `模型: ${c.model || '未加载'} · ${c.device || '未知设备'}`];
    if (m) {
      lines.push(`CPU ${m.cpu_percent ?? '未知'}% · RAM ${m.memory_used_mb ?? '未知'} / ${m.memory_total_mb ?? '未知'} MB`);
      for (const gpu of m.gpus || []) lines.push(`${gpu.name}: GPU ${gpu.utilization_percent}% · VRAM ${gpu.memory_used_mb}/${gpu.memory_total_mb} MB · ${gpu.temperature_c}°C`);
      lines.push(`Windows 队列: ${m.queue_depth ?? '未知'} · 运行任务: ${m.current_job_id || '空闲'}`);
      for (const log of m.logs || []) lines.push(`[Windows] ${log}`);
    } else lines.push(`资源监控: ${r.metrics_status}`);
    for (const t of r.tasks.slice(-8)) lines.push(`[${TASK_STATES[t.status] || t.status}] ${TASK_LABELS[t.label] || t.label} ${t.project || ''} · ${taskDetail(t)}${t.error && !taskDetail(t).includes(t.error) ? ` · ${t.error}` : ''}`);
    // 最近一次模型调用（或点「查看日志」选中的任务）展开完整过程和原始输出
    const focus = r.tasks.find(t => t.id === terminalFocus) || [...r.tasks].reverse().find(t => t.llm || t.log?.length);
    if (focus) {
      const stamp = at => new Date(at * 1000).toLocaleTimeString();
      lines.push('', `──── ${TASK_LABELS[focus.label] || focus.label} ${focus.project || ''} · ${TASK_STATES[focus.status] || focus.status} ────`);
      for (const entry of focus.log || []) lines.push(`[${stamp(entry.at)}] ${entry.message}`);
      const l = focus.llm;
      if (l) {
        lines.push(`供应商: ${l.endpoint || '未知'}${l.request_id ? ` · 请求 ID ${l.request_id}` : ''}`);
        lines.push(`结束原因: ${l.finish_reason || '无'} · 正文 ${l.content_chars.toLocaleString()} 字${l.reasoning_chars ? ` · 推理 ${l.reasoning_chars.toLocaleString()} 字` : ''}${l.usage_text ? ` · ${l.usage_text}` : ''}`);
        if (l.error && !(focus.log || []).some(entry => entry.message.includes(l.error))) lines.push(`错误: ${l.error}`);
        if (l.output_tail) lines.push('模型输出末尾:', l.output_tail);
        lines.push(`完整原始输出: ${l.file}`);
      }
    }
    const box = $('terminalOutput'), stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.textContent = lines.join('\n');
    if (stick || terminalFocus) box.scrollTop = box.scrollHeight;
  } catch (e) { $('terminalOutput').textContent = `状态读取失败: ${e.message}`; $('terminal').dataset.state = 'bad'; $('terminalBadge').textContent = '读取失败'; }
  finally { runtimeBusy = false; clampTerminal(); }
}
$('toggleTerminal').onclick = () => setTerminal({open: !terminal.open});
$('hideTerminal').onclick = () => setTerminal({hidden: true});
$('showTerminal').onclick = () => setTerminal({hidden: !terminal.hidden});
$('refreshTerminal').onclick = refreshRuntime;
$('terminalHead').ondblclick = event => { if (!event.target.closest('button')) setTerminal({open: !terminal.open}); };
$('terminalHead').onpointerdown = event => {
  if (event.button || event.target.closest('button')) return;
  const head = $('terminalHead'), el = $('terminal');
  const start = {x: event.clientX, y: event.clientY, right: terminal.right, bottom: terminal.bottom};
  head.setPointerCapture(event.pointerId);
  head.onpointermove = move => {
    el.classList.add('dragging');
    terminal.right = start.right - (move.clientX - start.x); terminal.bottom = start.bottom - (move.clientY - start.y);
    clampTerminal();
  };
  head.onpointerup = head.onpointercancel = () => { head.onpointermove = head.onpointerup = head.onpointercancel = null; el.classList.remove('dragging'); store.set('terminal', terminal); };
};
window.addEventListener('resize', clampTerminal);
setTerminal({}, false);

window.addEventListener('resize', () => { clearTimeout(growAll.t); growAll.t = setTimeout(growAll, 100); });
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });

/* ───────── 启动 ───────── */
initVideoDesign();
safe(async () => {
  config = await api('/api/config');
  $('mode').textContent = config.tts_mode === 'demo' ? '测试模式' : config.tts_mode === 'gateway' ? '网关模式' : 'Qwen 直连';
  $('mode').classList.toggle('demo', config.tts_mode === 'demo');
  $('openConnections').title = config.tts_mode === 'demo' ? '连接状态：测试模式，非真实配音' : '连接状态';
  $('llmLabel').textContent = config.llm_configured ? '已配置' : '未配置';
  checkConnections();
  await inventory({includeVoices: false}); await pollTasks();
  const requested = new URLSearchParams(location.search).get('project');
  if (requested && projectsCache.some(p => p.name === requested)) await selectProject(requested);
  else { await showHome(); if (requested) toast('这个工程已不存在，已返回主页。'); }
  $('voiceInventory').innerHTML = '<div class="empty">音色库加载中…</div>';
  safe(async () => {
    try { await inventory(); if (data) renderBindings(); }
    catch {
      $('voiceInventory').innerHTML = '<div class="empty">音色库暂不可用，可继续编辑脚本。</div>';
    }
  });
  setInterval(() => safe(pollTasks), 2000);
  setInterval(() => { if (!document.hidden) checkConnections(); }, 30000);
  refreshRuntime(); setInterval(() => { if (!document.hidden && !terminal.hidden && terminal.open) refreshRuntime(); }, live ? 1500 : 5000);
});
