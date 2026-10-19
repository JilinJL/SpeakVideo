'use strict';

let designMode = 'overlay', selectedOverlay = '', designProject = '';
let coverPreviewTimer = null, coverPreviewAbort = null, coverPreviewURL = '', coverPreviewRevision = 0;
const overlayKeys = ['id', 'x', 'y', 'width', 'opacity', 'flip', 'start', 'end', 'speaker_slot', 'speaking_glow', 'glow_strength'];
let designIntervals = {};
const clampDesign = (value, min, max) => Math.max(min, Math.min(max, value));

function coverFields() {
  return {
    enabled: true,
    background: $('coverBackground').value,
    frame_second: 0,
    title: $('coverTitle').value,
    subtitle: $('coverSubtitle').value,
    font_size: Number($('coverFontSize').value),
    title_y: Number($('coverY').value) / 100,
    color: $('coverColor').value,
    shade: Number($('coverShade').value) / 100,
    show_overlays: $('coverShowOverlays').checked,
  };
}

function videoBody() {
  const [width, height] = $('videoSize').value.split('x').map(Number);
  return {
    width, height, fps: Number($('videoFps').value),
    font_size: Number($('videoFontSize').value),
    image_seconds: Number($('videoImageSeconds').value),
    burn_subtitles: $('videoBurnSubtitles').checked,
    asset_ids: data.video.assets.map(a => a.id),
    clips: data.video.clips,
    overlays: (data.video.overlays || []).map(layer => Object.fromEntries(overlayKeys.map(key => [key, layer[key]]))),
    cover: coverFields(),
  };
}

function invalidateDesign() {
  videoDirty = true; markDirty();
  $('coverState').textContent = '修改未导出';
  $('approveVideo').disabled = true;
  scheduleCoverPreview();
}

function renderDesign() {
  data.video.overlays ||= [];
  if (designProject !== current) {
    designProject = current; selectedOverlay = ''; designMode = 'overlay';
    coverPreviewAbort?.abort(); coverPreviewRevision++;
    if (coverPreviewURL) URL.revokeObjectURL(coverPreviewURL);
    coverPreviewURL = ''; $('coverDesignPreview').removeAttribute('src');
  }
  designIntervals = {};
  for (const slot of ['p1', 'p2']) {
    const merged = [];
    for (const row of (data.video_timeline?.lines || []).filter(row => row.slot === slot).sort((a, b) => a.start - b.start)) {
      if (merged.length && row.start <= merged.at(-1)[1] + 1e-6) merged.at(-1)[1] = Math.max(merged.at(-1)[1], row.end);
      else merged.push([row.start, row.end]);
    }
    designIntervals[slot] = merged;
  }
  const master = data.audio_current && data.audio_export?.files?.['master.wav'];
  const source = master ? url('/files/exports/' + master.path) : '';
  const options = data.video.cover.options;
  const fields = {coverBackground: 'background', coverTitle: 'title', coverSubtitle: 'subtitle', coverFontSize: 'font_size', coverColor: 'color'};
  for (const [id, key] of Object.entries(fields)) $(id).value = options[key];
  $('coverShowOverlays').checked = options.show_overlays;
  $('coverY').value = options.title_y * 100;
  $('coverShade').value = options.shade * 100;
  $('coverImageName').textContent = data.video.cover.image?.name || '未上传底图';
  updateCoverOutputs();
  const files = data.cover_export?.files || {};
  $('coverState').textContent = Object.keys(files).length ? (data.cover_current ? '封面已导出' : '设置已变化，请重新导出') : '尚未导出';
  $('coverExports').innerHTML = Object.entries(files).map(([name, file]) => `<a href="${url('/files/exports/' + file.path)}" download>↓ ${escape(name)} <small>${fmtBytes(file.bytes)}</small></a>`).join('');
  if (!data.video.overlays.some(layer => layer.id === selectedOverlay)) selectedOverlay = data.video.overlays[0]?.id || '';
  renderOverlayList(); renderOverlayControls(); setDesignMode(designMode);
}

function updateCoverOutputs() {
  $('coverYOut').textContent = `${$('coverY').value}%`;
  $('coverShadeOut').textContent = `${$('coverShade').value}%`;
}

function setDesignMode(mode) {
  designMode = mode;
  $('editOverlayTab').setAttribute('aria-pressed', String(mode === 'overlay'));
  $('editCoverTab').setAttribute('aria-pressed', String(mode === 'cover'));
  $('coverDesignPreview').hidden = mode !== 'cover';
  $('designLayers').hidden = mode === 'cover';
  renderDesignScene();
  if (mode === 'cover') scheduleCoverPreview();
}

function overlayGeometry(layer) {
  const [canvasWidth, canvasHeight] = $('videoSize').value.split('x').map(Number);
  let width = Math.max(1, Math.round(canvasWidth * layer.width));
  let height = Math.max(1, Math.round(width * layer.source_height / layer.source_width));
  if (height > canvasHeight) { width = Math.max(1, Math.round(width * canvasHeight / height)); height = canvasHeight; }
  return {width: width / canvasWidth, height: height / canvasHeight,
    x: Math.min(layer.x, 1 - width / canvasWidth), y: Math.min(layer.y, 1 - height / canvasHeight)};
}

function selectedLayer() { return data?.video.overlays.find(layer => layer.id === selectedOverlay); }

function paintLayer(element, layer) {
  const geometry = overlayGeometry(layer), second = 0;
  Object.assign(element.style, {
    left: `${geometry.x * 100}%`, top: `${geometry.y * 100}%`,
    width: `${geometry.width * 100}%`, height: `${geometry.height * 100}%`,
    opacity: layer.opacity,
  });
  element.hidden = second < layer.start || (layer.end !== null && second >= layer.end);
  element.classList.toggle('selected', layer.id === selectedOverlay);
  const image = element.querySelector('img');
  image.style.transform = layer.flip ? 'scaleX(-1)' : '';
  let amount = 0;
  if (layer.speaker_slot && layer.speaking_glow) {
    for (const interval of designIntervals[layer.speaker_slot] || []) {
      const start = Math.max(interval[0], layer.start), end = Math.min(interval[1], layer.end ?? Infinity);
      if (second >= start && second < end) {
        const fade = Math.min(.12, (end - start) / 2);
        amount = Math.min(1, (second - start) / fade, (end - second) / fade) * layer.glow_strength;
        break;
      }
    }
  }
  const color = config.subtitle_colors?.[layer.speaker_slot] || (layer.speaker_slot === 'p1' ? '#FFB266' : '#70BAFF');
  const rgb = [1, 3, 5].map(start => parseInt(color.slice(start, start + 2), 16)).join(',');
  const radius = Math.max(2, Math.min(24, Math.round(Math.min(element.offsetWidth, element.offsetHeight) * .04)));
  image.style.filter = amount > 0 ? `drop-shadow(0 0 ${radius / 3}px rgba(${rgb},${amount})) drop-shadow(0 0 ${radius}px rgba(${rgb},${amount}))` : '';
}

function syncDesignBackground() {
  const assets = footageSequence();
  const duration = data.audio_export?.duration || 86400;
  const lengths = assets.map(a => Math.min(duration, a.length));
  let asset = assets[0] || null;
  for (const [i, length] of lengths.entries()) if (length > 0) { asset = assets[i]; break; }
  const image = $('designBackgroundImage'), video = $('designBackgroundVideo');
  image.hidden = asset?.kind !== 'image'; video.hidden = asset?.kind !== 'video';
  video.pause();
  if (asset) {
    const source = url('/files/' + (asset.preview_path || asset.path));
    if (asset.kind === 'image') { if (image.getAttribute('src') !== source) image.src = source; }
    else {
      const seek = () => {
        video.playbackRate = asset.speed;
        const target = Math.min(asset.start + cursor * asset.speed, Math.max(0, video.duration - .04));
        if (Number.isFinite(video.duration) && Math.abs(video.currentTime - target) > .15) video.currentTime = target;
      };
      if (video.getAttribute('src') !== source) { video.onloadedmetadata = seek; video.src = source; }
      else seek();
    }
  } else { image.removeAttribute('src'); video.removeAttribute('src'); }
}

function renderDesignScene() {
  if (!data?.video) return;
  const [width, height] = $('videoSize').value.split('x').map(Number);
  $('designStage').style.aspectRatio = `${width}/${height}`;
  $('designStage').style.maxWidth = height > width ? '350px' : '640px';
  syncDesignBackground();
  $('designLayers').innerHTML = data.video.overlays.map(layer => `<div class="design-layer" data-layer="${layer.id}" role="button" tabindex="0" aria-label="拖动贴图 ${escape(layer.name)}"><img src="${url('/files/' + layer.path)}" alt="${escape(layer.name)}" draggable="false"><span class="design-resize" aria-label="调整贴图大小"></span></div>`).join('');
  $$('[data-layer]').forEach(element => {
    const layer = data.video.overlays.find(item => item.id === element.dataset.layer);
    paintLayer(element, layer);
    element.onpointerdown = event => startDesignDrag(event, element, layer);
    element.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key) || $('video').classList.contains('design-busy')) return;
      event.preventDefault(); selectedOverlay = layer.id;
      const step = event.shiftKey ? .05 : .005;
      layer.x = clampDesign(layer.x + ({ArrowLeft: -step, ArrowRight: step}[event.key] || 0), 0, 1);
      layer.y = clampDesign(layer.y + ({ArrowUp: -step, ArrowDown: step}[event.key] || 0), 0, 1);
      const geometry = overlayGeometry(layer); layer.x = geometry.x; layer.y = geometry.y;
      paintLayer(element, layer); renderOverlayList(); renderOverlayControls(); invalidateDesign();
    };
  });
}


function startDesignDrag(event, element, layer) {
  if (event.button !== 0 || $('video').classList.contains('design-busy')) return;
  event.preventDefault(); selectedOverlay = layer.id;
  $$('[data-layer]').forEach(el => el.classList.toggle('selected', el === element));
  renderOverlayList(); renderOverlayControls();
  const rect = $('designStage').getBoundingClientRect(), geometry = overlayGeometry(layer);
  const resize = event.target.classList.contains('design-resize');
  const start = {x: event.clientX, y: event.clientY, layerX: geometry.x, layerY: geometry.y, width: layer.width};
  element.setPointerCapture(event.pointerId);
  element.onpointermove = move => {
    if (resize) layer.width = clampDesign(start.width + (move.clientX - start.x) / rect.width, .02, 1);
    else {
      layer.x = clampDesign(start.layerX + (move.clientX - start.x) / rect.width, 0, 1);
      layer.y = clampDesign(start.layerY + (move.clientY - start.y) / rect.height, 0, 1);
    }
    const fitted = overlayGeometry(layer); layer.x = fitted.x; layer.y = fitted.y;
    paintLayer(element, layer); renderOverlayControls(); invalidateDesign();
  };
  element.onpointerup = element.onpointercancel = () => {
    element.onpointermove = element.onpointerup = element.onpointercancel = null;
  };
}

function renderOverlayList() {
  $('overlayList').innerHTML = data.video.overlays.map((layer, i) => `<button class="overlay-choice" data-select-overlay="${layer.id}" aria-pressed="${layer.id === selectedOverlay}"><img src="${url('/files/' + layer.path)}" alt=""><span>${i + 1}. ${escape(layer.name)}</span></button>`).join('') || '<p class="hint">还没有贴图。背景素材不会被替换；贴图按列表顺序从下向上叠放。</p>';
  $$('[data-select-overlay]').forEach(button => button.onclick = () => {
    selectedOverlay = button.dataset.selectOverlay; setDesignMode('overlay'); renderOverlayList(); renderOverlayControls();
  });
}

function renderOverlayControls() {
  const layer = selectedLayer(); $('overlayControls').hidden = !layer;
  if (!layer) return;
  $('overlaySelectedName').textContent = layer.name;
  for (const [id, value] of Object.entries({overlayX: layer.x * 100, overlayY: layer.y * 100,
    overlayWidth: layer.width * 100, overlayOpacity: layer.opacity * 100,
    overlayStart: layer.start, overlayEnd: layer.end ?? ''})) $(id).value = typeof value === 'number' ? Math.round(value * 10) / 10 : value;
  $('overlayFlip').checked = layer.flip;
  $('overlaySpeaker').value = layer.speaker_slot;
  for (const slot of ['p1', 'p2']) $('overlaySpeaker').querySelector(`[value="${slot}"]`).textContent = `${data.cast[slot]?.role || slot} · ${slot === 'p1' ? '橙色' : '蓝色'}高光`;
  $('overlaySpeakingGlow').checked = layer.speaking_glow;
  $('overlayGlowStrength').value = Math.round(layer.glow_strength * 100);
  $('overlayGlowOut').textContent = `${Math.round(layer.glow_strength * 100)}%`;
  $('overlayWidthOut').textContent = `${Math.round(layer.width * 100)}%`;
  $('overlayOpacityOut').textContent = `${Math.round(layer.opacity * 100)}%`;
}

function scheduleCoverPreview() {
  clearTimeout(coverPreviewTimer);
  coverPreviewAbort?.abort(); coverPreviewRevision++;
  if (designMode !== 'cover' || !data) return;
  coverPreviewTimer = setTimeout(() => safe(refreshCoverPreview), 400);
}

async function refreshCoverPreview() {
  if (!data) return;
  coverPreviewAbort?.abort(); const controller = coverPreviewAbort = new AbortController();
  const revision = ++coverPreviewRevision, name = current;
  $('designPreviewState').textContent = '正在更新封面…';
  try {
    const response = await fetch(url('/cover-preview'), {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(videoBody()), signal: controller.signal});
    if (!response.ok) { const error = await response.json(); throw Error(typeof error.detail === 'string' ? error.detail : '封面设置无效，请检查输入'); }
    const blob = await response.blob();
    if (revision !== coverPreviewRevision || name !== current) return;
    if (coverPreviewURL) URL.revokeObjectURL(coverPreviewURL);
    coverPreviewURL = URL.createObjectURL(blob); $('coverDesignPreview').src = coverPreviewURL;
    $('designPreviewState').textContent = '封面预览 · 与导出一致';
  } catch (error) {
    if (error.name !== 'AbortError' && revision === coverPreviewRevision) {
      $('designPreviewState').textContent = error.message;
    }
  }
}

async function uploadDesign(kind, input) {
  const files = [...input.files], name = current;
  if (!files.length) return;
  const previousBackground = $('coverBackground').value;
  if (kind === 'cover' && !data.video.cover.image) $('coverBackground').value = 'frame';
  try { await save(); await saveVideo(); }
  catch (error) { $('coverBackground').value = previousBackground; throw error; }
  videoUploading = true; renderTasks();
  try {
    for (let i = 0; i < files.length; i++) {
      const form = new FormData(); form.append('file', files[i]);
      const result = await api(`/api/projects/${encodeURIComponent(name)}/assets/${kind}`, 'POST', form);
      if (name === current) { data.video = result; if (kind === 'overlay') selectedOverlay = result.overlays.at(-1).id; }
    }
    if (name === current) { await selectProject(current, {keepTab: true}); setDesignMode(kind === 'cover' ? 'cover' : 'overlay'); }
    toast(kind === 'cover' ? '封面底图已上传' : `已添加 ${files.length} 张贴图`);
  } finally { videoUploading = false; input.value = ''; renderTasks(); }
}

function initVideoDesign() {
  const left = document.createElement('div'), right = document.createElement('div');
  left.className = 'video-column video-left'; right.className = 'video-column video-right';
  for (const selector of ['.video-assets-card', '.video-settings-card', '.video-cover-card']) left.append(document.querySelector(selector));
  for (const selector of ['.video-design-card', '.video-preview-card']) right.append(document.querySelector(selector));
  $('video').replaceChildren(left, right);
  $('editOverlayTab').onclick = () => setDesignMode('overlay');
  $('editCoverTab').onclick = $('previewCover').onclick = () => setDesignMode('cover');
  $('overlayFiles').onchange = () => safe(() => uploadDesign('overlay', $('overlayFiles')));
  $('coverFile').onchange = () => safe(() => uploadDesign('cover', $('coverFile')));
  for (const id of ['coverBackground', 'coverTitle', 'coverSubtitle', 'coverFontSize', 'coverColor', 'coverY', 'coverShade', 'coverShowOverlays']) {
    $(id).oninput = () => { updateCoverOutputs(); invalidateDesign(); };
  }
  for (const id of ['overlayX', 'overlayY', 'overlayWidth', 'overlayOpacity', 'overlayStart', 'overlayEnd', 'overlayFlip', 'overlaySpeaker', 'overlaySpeakingGlow', 'overlayGlowStrength']) {
    $(id).oninput = () => {
      const layer = selectedLayer(); if (!layer) return;
      Object.assign(layer, {x: Number($('overlayX').value) / 100, y: Number($('overlayY').value) / 100,
        width: Number($('overlayWidth').value) / 100, opacity: Number($('overlayOpacity').value) / 100,
        start: Number($('overlayStart').value), end: $('overlayEnd').value === '' ? null : Number($('overlayEnd').value), flip: $('overlayFlip').checked,
        speaker_slot: $('overlaySpeaker').value, speaking_glow: $('overlaySpeakingGlow').checked, glow_strength: Number($('overlayGlowStrength').value) / 100});
      $('overlayWidthOut').textContent = `${$('overlayWidth').value}%`;
      $('overlayOpacityOut').textContent = `${$('overlayOpacity').value}%`;
      $('overlayGlowOut').textContent = `${$('overlayGlowStrength').value}%`;
      renderDesignScene(); invalidateDesign();
    };
  }
  $('overlayFullTime').onclick = () => {
    const layer = selectedLayer(); if (!layer) return;
    layer.start = 0; layer.end = null;
    renderOverlayControls(); renderDesignScene(); invalidateDesign();
  };
  $('removeOverlay').onclick = () => {
    data.video.overlays = data.video.overlays.filter(layer => layer.id !== selectedOverlay); selectedOverlay = data.video.overlays[0]?.id || '';
    renderOverlayList(); renderOverlayControls(); renderDesignScene(); invalidateDesign();
  };
  for (const [id, delta] of [['overlayBackward', -1], ['overlayForward', 1]]) $(id).onclick = () => {
    const layers = data.video.overlays, i = layers.findIndex(layer => layer.id === selectedOverlay), j = i + delta;
    if (i < 0 || j < 0 || j >= layers.length) return;
    [layers[i], layers[j]] = [layers[j], layers[i]];
    renderOverlayList(); renderDesignScene(); invalidateDesign();
  };
}
