'use strict';

// Selections are immediately usable. Prepare exact PCM only when cloning/downloading.
class ReferenceEditor {
  constructor({request, onChange, onError}) {
    this.request = request; this.onChange = onChange; this.onError = onError;
    this.el = id => document.getElementById(id);
    this.version = 0; this.buffer = null; this.source = null; this.locked = false;
    this.ctx = null; this.pending = false;
    this.el('trimStart').oninput = () => this.setSelection(Number(this.el('trimStart').value), this.end);
    this.el('trimEnd').oninput = () => this.setSelection(this.start, Number(this.el('trimEnd').value));
    this.el('resetSelection').onclick = () => this.setSelection(0, this.buffer.duration);
    this.el('previewSelection').onclick = () => this.play().catch(this.onError);
    this.el('stopSelection').onclick = () => this.stop();
    this.el('downloadReference').onclick = () => this.download().catch(this.onError);
    const area = this.el('referenceWaveArea');
    area.onpointerdown = event => this.beginDrag(event);
    area.onpointermove = event => this.drag(event);
    area.onpointerup = area.onpointercancel = () => this.dragging = null;
    for (const [id, side] of [['trimStartHandle', 'start'], ['trimEndHandle', 'end']]) {
      this.el(id).onkeydown = event => {
        if (this.locked || this.pending || !this.buffer) return;
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let value = this[side] + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? .1 : .01);
        if (event.key === 'Home') value = 0;
        if (event.key === 'End') value = this.buffer.duration;
        this.setSelection(side === 'start' ? value : this.start, side === 'end' ? value : this.end);
      };
    }
    this.observer = new ResizeObserver(() => { if (this.buffer) this.draw(); });
    this.observer.observe(area);
  }
  context() { return this.ctx ||= new (window.AudioContext || window.webkitAudioContext)(); }
  async load(result, name) {
    const version = ++this.version;
    this.stop(); this.buffer = null; this.ready = null; this.el('referenceEditor').hidden = true; this.onChange(null); this.pending = true; this.render();
    try {
      const response = await fetch(result.preview_url);
      if (!response.ok) throw Error('参考音频无法读取，请重新上传');
      const buffer = await this.context().decodeAudioData(await response.arrayBuffer());
      if (version !== this.version) return;
      if (buffer.duration < .1) throw Error('参考音频至少需要 0.1 秒');
      this.buffer = buffer; this.original = result; this.start = 0; this.end = buffer.duration;
      this.locked = false; this.el('referenceName').textContent = `${name} · ${buffer.duration.toFixed(2)} 秒`;
      this.el('referenceEditor').hidden = false;
      this.el('voicePreview').hidden = true;
      this.ready = result; this.readySelection = [this.start, this.end];
      this.onChange(result); this.render(); this.draw();
    } finally {
      if (version === this.version) { this.pending = false; this.render(); }
    }
  }
  setSelection(start, end) {
    if (!this.buffer || this.locked || this.pending) return;
    this.stop();
    this.start = Math.max(0, Math.min(start, this.buffer.duration - .1));
    this.end = Math.max(this.start + .1, Math.min(end, this.buffer.duration));
    this.onChange({duration: this.end - this.start, reference_id: this.isPrepared() ? this.ready.reference_id : ''}); this.render();
  }
  beginDrag(event) {
    if (!this.buffer || this.locked || this.pending) return;
    event.preventDefault(); this.stop();
    const area = this.el('referenceWaveArea');
    const rect = area.getBoundingClientRect();
    const time = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * this.buffer.duration;
    const tolerance = 14 / rect.width * this.buffer.duration;
    const side = event.target.id === 'trimStartHandle' || Math.abs(time - this.start) < tolerance ? 'start'
      : event.target.id === 'trimEndHandle' || Math.abs(time - this.end) < tolerance ? 'end'
      : time > this.start && time < this.end ? 'move' : 'new';
    this.dragging = {side, anchor: time, start: this.start, end: this.end};
    area.setPointerCapture(event.pointerId);
    if (side === 'new') this.setSelection(time, time + .1);
  }
  drag(event) {
    if (!this.dragging || this.locked || this.pending) return;
    const rect = this.el('referenceWaveArea').getBoundingClientRect();
    const t = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * this.buffer.duration;
    const d = this.dragging;
    if (d.side === 'start') this.setSelection(Math.min(t, this.end - .1), this.end);
    else if (d.side === 'end') this.setSelection(this.start, t);
    else if (d.side === 'new') this.setSelection(Math.min(t, d.anchor), Math.max(t, d.anchor));
    else {
      const delta = Math.max(-d.start, Math.min(t - d.anchor, this.buffer.duration - d.end));
      this.setSelection(d.start + delta, d.end + delta);
    }
  }
  isPrepared() { return this.ready && this.readySelection?.[0] === this.start && this.readySelection?.[1] === this.end; }
  render() {
    for (const id of ['trimStart', 'trimEnd', 'resetSelection', 'downloadReference']) this.el(id).disabled = this.pending || this.locked || !this.buffer;
    this.el('previewSelection').disabled = this.pending || this.locked || !this.buffer;
    this.el('cropState').textContent = this.pending ? '处理中…' : '选区直接用于克隆';
    if (!this.buffer) return;
    if (!this.source) this.position(this.start);
    this.el('trimStart').value = this.start.toFixed(2); this.el('trimEnd').value = this.end.toFixed(2);
    this.el('trimDuration').textContent = `保留 ${(this.end - this.start).toFixed(2)} 秒 / ${this.buffer.duration.toFixed(2)} 秒`;
    const selection = this.el('referenceSelection');
    selection.style.left = `${this.start / this.buffer.duration * 100}%`;
    selection.style.width = `${(this.end - this.start) / this.buffer.duration * 100}%`;
    for (const [id, side] of [['trimStartHandle', 'start'], ['trimEndHandle', 'end']]) {
      const handle = this.el(id);
      handle.setAttribute('aria-valuemin', '0'); handle.setAttribute('aria-valuemax', this.buffer.duration.toFixed(2));
      handle.setAttribute('aria-valuenow', this[side].toFixed(2)); handle.setAttribute('aria-valuetext', `${this[side].toFixed(2)} 秒`);
    }
  }
  draw() {
    if (!this.buffer) return;
    const canvas = this.el('referenceWave'), ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, canvas.clientWidth), height = 140;
    canvas.width = Math.round(width * ratio); canvas.height = height * ratio;
    const painter = canvas.getContext('2d'); painter.scale(ratio, ratio);
    painter.clearRect(0, 0, width, height);
    const values = this.buffer.getChannelData(0), step = Math.max(1, Math.ceil(values.length / width));
    painter.strokeStyle = '#2f6feb'; painter.lineWidth = 1;
    painter.beginPath();
    for (let x = 0; x < width; x++) {
      let min = 0, max = 0;
      for (let i = x * step; i < Math.min((x + 1) * step, values.length); i++) { min = Math.min(min, values[i]); max = Math.max(max, values[i]); }
      painter.moveTo(x, height / 2 - max * height * .45); painter.lineTo(x, height / 2 - min * height * .45);
    }
    painter.stroke();
    this.el('referenceRuler').replaceChildren(...Array.from({length: 6}, (_, i) => {
      const label = document.createElement('span'); label.textContent = `${(this.buffer.duration * i / 5).toFixed(1)} s`; return label;
    }));
  }
  position(time) {
    const duration = this.buffer?.duration || 0;
    this.el('referencePosition').textContent = `${time.toFixed(2)} / ${duration.toFixed(2)} 秒`;
    this.el('referencePlayhead').hidden = !duration;
    this.el('referencePlayhead').style.left = `${duration ? Math.min(100, time / duration * 100) : 0}%`;
  }
  stop(reset = true) {
    cancelAnimationFrame(this.animation);
    if (this.source) { this.source.onended = null; try { this.source.stop(); } catch {} this.source.disconnect(); this.source = null; }
    this.el('stopSelection').disabled = true; this.el('previewSelection').textContent = '试听选区';
    if (reset) this.position(this.start || 0);
  }
  async play() {
    if (!this.buffer || this.pending) return;
    this.stop(); const version = this.version, ctx = this.context();
    await ctx.resume();
    if (version !== this.version || !this.el('factoryDialog').open) return;
    this.source = ctx.createBufferSource(); this.source.buffer = this.buffer;
    this.source.connect(ctx.destination);
    this.source.onended = () => { this.stop(false); this.position(this.end); };
    const started = ctx.currentTime, start = this.start, end = this.end;
    this.source.start(0, start, end - start);
    const tick = () => { this.position(Math.min(end, start + ctx.currentTime - started)); if (this.source) this.animation = requestAnimationFrame(tick); };
    tick(); this.el('stopSelection').disabled = false; this.el('previewSelection').textContent = '重新试听选区';
  }
  async prepare() {
    if (!this.buffer || this.pending) throw Error('请等待参考音频处理完成');
    if (this.isPrepared()) return this.ready;
    this.stop(); this.pending = true; this.render(); const version = this.version, selection = [this.start, this.end];
    try {
      const result = await this.request('/api/voices/reference/crop', 'POST', {
        reference_id: this.original.reference_id, start: selection[0],
        end: Math.min(selection[1], this.original.duration_precise || this.buffer.duration),
      });
      if (version !== this.version) throw Error('参考音频已切换，请重新提交');
      this.ready = result; this.readySelection = selection; this.onChange(result);
      return result;
    } finally { if (version === this.version) { this.pending = false; this.render(); } }
  }
  async download() {
    const result = await this.prepare();
    const link = document.createElement('a'); link.href = result.preview_url; link.download = 'reference.wav'; link.click();
  }
  close() { this.stop(); ++this.version; this.pending = false; this.render(); }
}
