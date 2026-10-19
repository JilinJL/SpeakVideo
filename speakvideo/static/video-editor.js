'use strict';

let footageProject = '', footageAsset = '', footageClip = '';
const clipTime = seconds => {
  const ms = Math.round(Math.max(0, seconds) * 1000);
  return `${Math.floor(ms / 60000).toString().padStart(2, '0')}:${(Math.floor(ms / 1000) % 60).toString().padStart(2, '0')}.${(ms % 1000).toString().padStart(3, '0')}`;
};
function parseClipTime(text) {
  const value = text.trim();
  if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?$/.test(value)) throw Error('时间请填写秒数、分:秒或时:分:秒，例如 02:30.500');
  const parts = value.split(':').map(Number);
  if (parts.slice(1).some(n => n >= 60)) throw Error('时间中的分、秒需要小于 60');
  return parts.reduce((sum, n) => sum * 60 + n, 0);
}
function footageSequence() {
  const assets = new Map((data.video.assets || []).map(a => [a.id, a]));
  return (data.video.clips || []).map(clip => {
    const asset = assets.get(clip.asset_id);
    const end = clip.end ?? (asset.kind === 'image' ? Number($('videoImageSeconds').value) : asset.duration);
    return {...asset, ...clip, id: asset.id, length: (end - clip.start) / clip.speed};
  });
}
function footageChanged() {
  invalidateDesign(); renderFootage(); renderDesignScene(); renderTasks();
}
function renderFootage() {
  if (!data?.video) return;
  const assets = data.video.assets;
  data.video.clips ||= assets.map(a => ({id: a.id, asset_id: a.id, start: 0, end: null, speed: 1}));
  if (footageProject !== current) {
    footageProject = current; footageAsset = ''; footageClip = '';
    $('footagePlayer').pause(); $('footagePlayer').removeAttribute('src'); $('footagePlayer').load();
  }
  $('videoAssetCount').textContent = `${assets.length} 个原片 / 图片`;
  $('videoAssets').innerHTML = assets.map(a => `<div class="video-asset">
    ${a.kind === 'image' ? `<img src="${url('/files/' + a.path)}" alt="${escape(a.name)}">` : '<span class="footage-icon" aria-hidden="true">▶</span>'}
    <div class="video-asset-info"><b>${escape(a.name)}</b><small>${a.kind === 'image' ? '图片' : clipTime(a.duration)} · ${fmtBytes(a.bytes)}</small>
    <div class="row">${a.kind === 'video' ? `<button class="btn sm" data-source-open="${a.id}">播放 / 选段</button>` : `<button class="btn sm" data-clip-add="${a.id}">追加图片</button>`}<button class="btn ghost sm" data-source-remove="${a.id}">移除素材</button></div></div></div>`).join('') || '<p class="hint">添加视频后，可在线播放、拖动选段，或填写精确起止时间。</p>';
  $$('[data-source-open]').forEach(b => b.onclick = () => openFootage(b.dataset.sourceOpen));
  $$('[data-clip-add]').forEach(b => b.onclick = () => {
    if (data.video.clips.length >= 200) { toast('每个工程最多 200 个片段', true); return; }
    data.video.clips.push({id: crypto.randomUUID().replaceAll('-', ''), asset_id: b.dataset.clipAdd, start: 0, end: null, speed: 1}); footageChanged();
  });
  $$('[data-source-remove]').forEach(b => b.onclick = () => {
    const id = b.dataset.sourceRemove;
    data.video.assets = assets.filter(a => a.id !== id);
    data.video.clips = data.video.clips.filter(c => c.asset_id !== id);
    footageChanged();
  });
  const sequence = footageSequence();
  const total = sequence.reduce((sum, a) => sum + a.length, 0), audio = data.audio_export?.duration;
  $('clipCount').textContent = `${sequence.length} 个片段`;
  $('clipSummary').textContent = !sequence.length ? '当前使用纯色背景。原片保留在上方，可随时追加片段。' : `片段共 ${clipTime(total)}${audio ? ` · 完整音频 ${clipTime(audio)} · ${total < audio ? '片段将循环补齐音频' : '在音频结束处自动截断'}` : ' · 合成长度跟随完整音频'}。原片原声静音。`;
  $('videoClips').innerHTML = sequence.map((a, i) => `<div class="video-asset"><div class="video-asset-info"><b>${i + 1}. ${escape(a.name)}</b><small>${a.kind === 'image' ? '图片' : `${clipTime(a.start)} → ${clipTime(a.start + a.length * a.speed)} · ${a.speed}×`} · 成片 ${clipTime(a.length)}</small><div class="row">
    ${a.kind === 'video' ? `<button class="btn sm" data-clip-edit="${data.video.clips[i].id}">编辑片段</button>` : ''}
    <button class="btn ghost sm" data-video-move="${i}" data-delta="-1" data-boundary="${i === 0}" ${i === 0 ? 'disabled' : ''}>上移</button><button class="btn ghost sm" data-video-move="${i}" data-delta="1" data-boundary="${i === sequence.length - 1}" ${i === sequence.length - 1 ? 'disabled' : ''}>下移</button><button class="btn ghost sm" data-video-remove="${i}">移除片段</button></div></div></div>`).join('');
  $$('[data-clip-edit]').forEach(b => b.onclick = () => { const clip = data.video.clips.find(c => c.id === b.dataset.clipEdit); openFootage(clip.asset_id, clip.id); });
  $$('[data-video-move]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.videoMove), j = i + Number(b.dataset.delta), clips = data.video.clips;
    [clips[i], clips[j]] = [clips[j], clips[i]]; footageChanged();
  });
  $$('[data-video-remove]').forEach(b => b.onclick = () => { data.video.clips.splice(Number(b.dataset.videoRemove), 1); footageChanged(); });
  if (!assets.some(a => a.id === footageAsset)) {
    footageAsset = ''; footageClip = ''; $('footageEditor').hidden = true; $('footagePlayer').pause();
    $('footagePlayer').removeAttribute('src'); $('footagePlayer').load();
  }
  if (footageClip && !data.video.clips.some(c => c.id === footageClip)) footageClip = '';
  $('clipApply').textContent = footageClip ? '应用到当前片段' : '添加所选片段';
}
function openFootage(id, clipID) {
  const asset = data.video.assets.find(a => a.id === id);
  const clip = clipID ? data.video.clips.find(c => c.id === clipID) : data.video.clips.find(c => c.asset_id === id);
  footageAsset = id; footageClip = clip?.id || '';
  const player = $('footagePlayer'); player.pause(); player.playbackRate = 1;
  $('footageEditor').hidden = false; $('footageTitle').textContent = asset.name;
  $('clipStart').value = clipTime(clip?.start || 0); $('clipEnd').value = clipTime(clip?.end ?? asset.duration);
  const speed = String(clip?.speed || 1);
  if (![...$('clipSpeed').options].some(o => o.value === speed)) $('clipSpeed').add(new Option(`${speed}×`, speed));
  $('clipSpeed').value = speed;
  $('clipStartRange').max = $('clipEndRange').max = asset.duration;
  const source = url('/files/' + (asset.preview_path || asset.path));
  const seek = () => { player.currentTime = clip?.start || 0; };
  if (player.getAttribute('src') !== source) { $('footageError').hidden = true; player.onloadedmetadata = seek; player.src = source; } else seek();
  $('clipApply').textContent = footageClip ? '应用到当前片段' : '添加所选片段';
  updateClipSelection(); $('footageEditor').scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
function selectedFootage() {
  const asset = data.video.assets.find(a => a.id === footageAsset);
  const start = parseClipTime($('clipStart').value), end = parseClipTime($('clipEnd').value), speed = Number($('clipSpeed').value);
  if (!asset || end > asset.duration + .001 || end - start < .04) throw Error('请选择原片范围内的起止时间，片段至少 0.04 秒');
  return {asset_id: asset.id, start, end, speed};
}
function updateClipSelection() {
  try {
    const clip = selectedFootage();
    $('clipStartRange').value = clip.start; $('clipEndRange').value = clip.end;
    $('clipDuration').textContent = `所选 ${clipTime(clip.end - clip.start)} → 成片 ${clipTime((clip.end - clip.start) / clip.speed)}`;
  } catch (error) { $('clipDuration').textContent = error.message; }
}
function commitFootage(append) {
  const clip = selectedFootage();
  const existing = !append && data.video.clips.find(c => c.id === footageClip);
  if (existing) Object.assign(existing, clip);
  else {
    if (data.video.clips.length >= 200) throw Error('每个工程最多 200 个片段');
    const id = crypto.randomUUID().replaceAll('-', ''); data.video.clips.push({id, ...clip}); footageClip = id;
  }
  footageChanged(); toast(existing ? '片段已更新，合成时使用所选范围' : '片段已追加，可继续从原片选段');
}
$('clipApply').onclick = () => safe(() => commitFootage(false));
$('clipAppend').onclick = () => safe(() => commitFootage(true));
for (const id of ['clipStart', 'clipEnd', 'clipSpeed']) $(id).oninput = () => updateClipSelection();
for (const [range, field] of [['clipStartRange', 'clipStart'], ['clipEndRange', 'clipEnd']]) $(range).oninput = () => {
  $('footagePlayer').pause();
  $(field).value = clipTime(Number($(range).value));
  if (range === 'clipStartRange') $('footagePlayer').currentTime = Number($(range).value);
  else $('footagePlayer').currentTime = Math.max(0, Number($(range).value) - .04);
  updateClipSelection();
};
for (const [button, field] of [['clipMarkStart', 'clipStart'], ['clipMarkEnd', 'clipEnd']]) $(button).onclick = () => {
  $(field).value = clipTime($('footagePlayer').currentTime); updateClipSelection();
};
$('footagePlayer').ontimeupdate = () => {
  const player = $('footagePlayer'); $('footagePosition').textContent = clipTime(player.currentTime);
  if (player.currentTime >= parseClipTime($('clipEnd').value)) { player.pause(); player.currentTime = parseClipTime($('clipEnd').value); }
};
$('footagePlayer').onerror = () => { if (footageAsset) $('footageError').hidden = false; };
function uploadFootage(name, form, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open('POST', `/api/projects/${encodeURIComponent(name)}/assets/video`);
    xhr.responseType = 'json'; xhr.upload.onprogress = e => progress(e.loaded, e.lengthComputable ? e.total : 0);
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response) : reject(Error(typeof xhr.response?.detail === 'string' ? xhr.response.detail : `上传失败（${xhr.status}）`));
    xhr.onerror = () => reject(Error('上传中断，请检查本地服务是否运行后重试'));
    xhr.send(form);
  });
}
