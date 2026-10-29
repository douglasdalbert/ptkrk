import assert from "node:assert/strict";
import { test } from "node:test";
import { detectPitch, VocalSampler } from "../app-singer/vocal-analysis.js";
import { renderVocalFeedback, setVocalDetectionFeedback, vocalMessages } from "../app-singer/vocal-feedback.js";

function tone(frequency, amplitude = 0.2, rate = 48000) {
  return Float32Array.from({ length: 2048 }, (_, index) =>
    amplitude * Math.sin(2 * Math.PI * frequency * index / rate));
}

test("YIN finds vocal pitches at phone sample rates, not silence or noise", () => {
  for (const rate of [44100, 48000]) {
    for (const frequency of [80, 110, 220, 440, 880]) {
      const pitch = detectPitch(tone(frequency, 0.2, rate), rate);
      assert.ok(pitch && pitch.confidence > 0.85);
      assert.ok(Math.abs(1200 * Math.log2(pitch.frequency / frequency)) < 10);
    }
  }
  assert.equal(detectPitch(new Float32Array(2048), 48000), null);
  let seed = 42;
  const noise = Float32Array.from({ length: 2048 }, () => {
    seed = (1664525 * seed + 1013904223) >>> 0;
    return seed / 2 ** 32 - 0.5;
  });
  assert.equal(detectPitch(noise, 48000), null);
});

test("reliable sustained voice yields a one-second summary; silence stays neutral", () => {
  const sampler = new VocalSampler();
  const samples = tone(220);
  let result;
  for (let now = 0; now <= 1000; now += 50) result = sampler.sample(samples, 48000, 0.14, 0.04, now);
  assert.equal(result.voiced_ms, 1000);
  assert.ok(result.jitter < 1);
  assert.ok(Math.abs(result.loudness - 0.14) < 0.001);
  for (let now = 1100; now <= 2100; now += 50) {
    assert.equal(sampler.sample(new Float32Array(2048), 48000, 0, 0.04, now), null);
  }
});

test("melodic transitions do not count as instability; irregular sustained voice does", () => {
  for (const transition of [true, false]) {
    const sampler = new VocalSampler();
    let result;
    for (let now = 0; now <= 1000; now += 50) {
      const frequency = transition ? (now < 500 ? 220 : 330) :
        220 * 2 ** ((now % 100 ? 100 : 0) / 1200);
      result = sampler.sample(tone(frequency), 48000, 0.14, 0.04, now);
    }
    assert.ok(transition ? result.jitter < 1 : result.jitter > 80);
  }
});

test("background suspension cannot count elapsed time as singing", () => {
  const sampler = new VocalSampler();
  sampler.sample(tone(220), 48000, 0.14, 0.04, 0);
  assert.equal(sampler.sample(tone(220), 48000, 0.14, 0.04, 20000), null);
});

test("short sung fragments accumulate across pauses rather than being discarded", () => {
  const sampler = new VocalSampler();
  const summaries = [];
  for (let now = 0; now <= 4000; now += 50) {
    const singing = now % 1000 < 400;
    const result = sampler.sample(singing ? tone(220) : new Float32Array(2048),
      48000, singing ? 0.14 : 0, 0.04, now);
    if (result) summaries.push(result);
  }
  assert.equal(summaries.length, 1);
  assert.equal(summaries[0].voiced_ms, 1000);
  assert.ok(summaries[0].jitter < 1);
});

test("diagnostics distinguish insufficient volume from unpitched sound", () => {
  const sampler = new VocalSampler();
  sampler.sample(tone(220), 48000, 0.01, 0.04, 0);
  assert.equal(sampler.status, "quiet");
  sampler.sample(new Float32Array(2048), 48000, 0.1, 0.04, 50);
  assert.equal(sampler.status, "uncertain");
  sampler.sample(tone(220), 48000, 0.14, 0.04, 100);
  assert.equal(sampler.status, "voiced");
});

function feedbackFixture() {
  const nodes = new Map(["use", ".vocal-label", ".vocal-message", "progress", ".vocal-points"].map(key => [key, {
    dataset: {},
    attributes: {}, setAttribute(name, value) { this.attributes[name] = value; },
  }]));
  const element = { dataset: {}, querySelector: key => nodes.get(key) };
  return { nodes, element };
}

test("pug feedback keeps calibration progress but never displays score progress", t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { nodes, element } = feedbackFixture();
  renderVocalFeedback(element, null, 20000);
  assert.equal(element.dataset.state, "waiting");
  assert.equal(nodes.get("progress").value, 0);
  assert.equal(nodes.get("progress").hidden, false);
  assert.equal(nodes.get(".vocal-points").hidden, true);
  assert.equal(nodes.get("progress").attributes["aria-label"], "Calibração da voz");
  renderVocalFeedback(element, { level: -1, calibration_ms: 10000 }, 20000);
  assert.equal(nodes.get("progress").value, 50);
  for (const [level, state] of ["bad", "falling", "good", "excellent"].entries()) {
    renderVocalFeedback(element, { level, points: 1250, ranking_max: 1000, percent: 125 }, 20000, true);
    assert.equal(nodes.get(".vocal-points").hidden, false);
    assert.equal(element.dataset.state, state);
    assert.equal(nodes.get("progress").value, 0);
    assert.equal(nodes.get("progress").hidden, true);
    assert.match(nodes.get(".vocal-points").textContent, /1\.250/);
    assert.equal(nodes.get("use").attributes.href, `/assets/vocal-pugs.svg#${state}`);
    assert.equal(nodes.get(".vocal-label").textContent,
      ["Vamos recuperar!", "Está se engasgando?!?", "Está indo bem...", "Dando show!"][level]);
    assert.equal(nodes.get("progress").attributes["aria-label"], "Calibração da voz");
  }
  renderVocalFeedback(element, {level: 2, points: 750, ranking_max: 1000, percent: 75}, 20000, false);
  assert.equal(nodes.get(".vocal-points").hidden, true);
});

test("twenty contextual messages rotate with a three-second minimum and expire neutrally", t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const advance = ms => { now += ms; t.mock.timers.tick(ms); };
  const { nodes, element } = feedbackFixture();
  const message = nodes.get(".vocal-message");
  assert.equal(Object.values(vocalMessages).flat().length, 20);
  const score = { request_id: "r", singer_id: "s", level: 2, calibration_ms: 5000,
    evaluated_ms: 1000, points: 33, ranking_max: 1000, feedback: "steady" };
  renderVocalFeedback(element, score);
  assert.equal(message.textContent, vocalMessages.steady[0]);
  renderVocalFeedback(element, { ...score, evaluated_ms: 2000, feedback: "quiet" });
  assert.equal(message.textContent, vocalMessages.steady[0]);
  advance(3000);
  assert.equal(message.textContent, vocalMessages.quiet[0]);
  renderVocalFeedback(element, { ...score, evaluated_ms: 3000, feedback: "recovering" });
  advance(3000);
  assert.equal(message.textContent, vocalMessages.recovering[0]);
  advance(3000);
  assert.equal(message.textContent, vocalMessages.waiting[0]);
  setVocalDetectionFeedback(element, "uncertain");
  advance(3000);
  assert.equal(message.textContent, vocalMessages.uncertain[0]);
  renderVocalFeedback(element, { ...score, evaluated_ms: 4000 });
  advance(3000);
  assert.equal(message.textContent, vocalMessages.steady[1]);
  renderVocalFeedback(element, { ...score, request_id: "next", level: -1, calibration_ms: 0 });
  assert.equal(message.textContent, vocalMessages.calibrating[0]);
  setVocalDetectionFeedback(element, "quiet");
  advance(3000);
  assert.equal(message.textContent, vocalMessages.calibrating[0]);
});

test("snapshots do not overwrite recent feedback and a late detailed update is accepted", t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let now = 0;
  t.mock.method(performance, "now", () => now);
  const { nodes, element } = feedbackFixture();
  const score = { request_id: "r", singer_id: "s", level: 3, calibration_ms: 5000,
    evaluated_ms: 3000, points: 100, ranking_max: 1000 };
  renderVocalFeedback(element, score);
  renderVocalFeedback(element, { ...score, feedback: "streak" });
  now = 3000;
  t.mock.timers.tick(3000);
  assert.equal(nodes.get(".vocal-message").textContent, vocalMessages.streak[0]);
  renderVocalFeedback(element, score);
  assert.equal(nodes.get(".vocal-message").textContent, vocalMessages.streak[0]);
});
