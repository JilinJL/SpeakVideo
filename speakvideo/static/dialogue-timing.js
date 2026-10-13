'use strict';

// Calibration: 1,821 characters, 65 × 350 ms pauses, 382 seconds total.
// This is playback time, not generation time; speech speed varies by voice/mood.
globalThis.SpeakVideoTiming = Object.freeze({
  charsPerSecond: 1821 / (382 - 65 * 0.35),
  characterCount: text => Array.from(String(text).trim()).length,
  estimate(lines) {
    const chars = lines.reduce((sum, line) => sum + this.characterCount(line.words), 0);
    const pauses = lines.reduce((sum, line) => sum + Math.max(0, Number(line.pause_after_ms) || 0) / 1000, 0);
    return {chars, seconds: chars / this.charsPerSecond + pauses};
  },
  format(seconds) {
    const total = Math.round(Math.max(0, seconds));
    return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  },
});
