export function detectPitch(samples, sampleRate) {
  const stride = Math.max(1, Math.floor(sampleRate / 12000));
  const rate = sampleRate / stride;
  const size = Math.floor(samples.length / stride / 2);
  const maxLag = Math.min(size - 1, Math.ceil(rate / 70));
  const minLag = Math.floor(rate / 1000);
  const differences = new Float32Array(maxLag + 1);
  let sum = 0;
  for (let lag = 1; lag <= maxLag; lag += 1) {
    let difference = 0;
    for (let index = 0; index < size; index += 1) {
      const delta = samples[index * stride] - samples[(index + lag) * stride];
      difference += delta * delta;
    }
    sum += difference;
    differences[lag] = sum ? difference * lag / sum : 1;
  }
  for (let lag = Math.max(2, minLag); lag < maxLag; lag += 1) {
    if (differences[lag] >= 0.15) continue;
    while (lag + 1 < maxLag && differences[lag + 1] < differences[lag]) lag += 1;
    const left = differences[lag - 1], center = differences[lag], right = differences[lag + 1];
    const denominator = 2 * (2 * center - right - left);
    const refined = lag + (denominator ? (right - left) / denominator : 0);
    return { frequency: rate / refined, confidence: 1 - center };
  }
  return null;
}

export class VocalSampler {
  constructor() {
    this.reset();
  }

  reset() {
    this.lastAt = null;
    this.previousPitch = null;
    this.voicedMs = 0;
    this.loudnessSum = 0;
    this.differences = [];
    this.status = "waiting";
  }

  sample(samples, sampleRate, rms, threshold, now) {
    if (this.lastAt !== null && now - this.lastAt < 50) return null;
    const elapsed = this.lastAt === null ? 0 : now - this.lastAt;
    const delta = elapsed <= 150 ? elapsed : 0;
    this.lastAt = now;
    const pitch = rms >= threshold ? detectPitch(samples, sampleRate) : null;
    this.status = rms < threshold ? "quiet" : pitch ? "voiced" : "uncertain";
    if (pitch && pitch.confidence >= 0.85 && delta > 0) {
      this.voicedMs += delta;
      this.loudnessSum += rms * delta;
      if (this.previousPitch !== null) {
        const change = Math.abs(1200 * Math.log2(pitch.frequency / this.previousPitch));
        // Ignore note transitions and octave-tracking errors rather than calling them bad singing.
        if (change <= 150) this.differences.push(change);
      }
      this.previousPitch = pitch.frequency;
    } else {
      this.previousPitch = null;
    }
    // Keep reliable fragments across syllables and pauses instead of discarding short windows.
    if (this.voicedMs < 1000 || this.differences.length < 4) return null;
    const result = {
      voiced_ms: Math.min(1000, Math.round(this.voicedMs)),
      loudness: this.loudnessSum / this.voicedMs,
      jitter: this.differences.reduce((sum, value) => sum + value, 0) / this.differences.length,
    };
    this.reset();
    return result;
  }
}
