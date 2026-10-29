import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const tvSource = readFileSync(new URL("../app-tv/app.js", import.meta.url), "utf8");
const singerSource = readFileSync(new URL("../app-singer/app.js", import.meta.url), "utf8");

function element() {
  return {
    hidden: false, disabled: false, dataset: {}, children: [], events: {},
    style: { setProperty() {} },
    classList: { add() {}, remove() {} },
    setAttribute() {},
    addEventListener(event, callback) { this.events[event] = callback; },
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; },
  };
}

function tvContext(scoringMode = "vocal") {
  const context = {
    scoringMode, activeId: "song", activeSingerId: "singer",
    boostCharge: 0, boostRemainingMs: 0, boostFrameAt: null, boostUsed: false,
    vocalBoostFillMs: 40000, boostDurationMs: 30000, boostMultiplier: 2, boostFillPercent: 25,
    boostedBlocks: new Set(), boostBar: element(), boostLabel: element(), boostStars: element(),
    show: element(), video: { paused: false, ended: false }, captionBars: Array(4),
    overlay: { clientWidth: 1000, clientHeight: 600 }, barPosition: 1,
    svgNamespace: "http://www.w3.org/2000/svg", starColors: ["#fff"],
    document: { createElementNS: () => element() },
    performance: { now: () => context.now }, now: 0,
    socket: { readyState: 1, send(data) { context.messages.push(JSON.parse(data)); } },
    WebSocket: { OPEN: 1 }, messages: [],
    formatNumber: value => String(value),
  };
  runInNewContext(tvSource.slice(tvSource.indexOf("function updateBoostBar("),
    tvSource.indexOf("function stopPlayback(")), context);
  return context;
}

test("karaoke fills at exactly 40000 ms, allows one special and hides its spent bar", () => {
  const tv = tvContext();
  tv.updateVocalBoost({ singing_ms: 39999 });
  assert.equal(tv.boostCharge, 39999 / 40000);
  tv.startBoost();
  assert.equal(tv.boostRemainingMs, 0);
  tv.updateVocalBoost({ singing_ms: 40000 });
  assert.equal(tv.boostCharge, 1);
  assert.equal(tv.messages.at(-1).ready, true);
  tv.startBoost();
  assert.equal(tv.boostUsed, true);
  assert.equal(tv.boostRemainingMs, 30000);
  assert.equal(tv.boostBar.style.visibility, "");
  tv.now = 30000;
  tv.tickBoost();
  assert.equal(tv.boostRemainingMs, 0);
  assert.equal(tv.boostBar.style.visibility, "hidden");
  tv.updateVocalBoost({ singing_ms: 100000 });
  tv.startBoost();
  assert.equal(tv.boostRemainingMs, 0);
  assert.equal(tv.boostCharge, 0);
});

test("playback can fill and activate more than one special", () => {
  const tv = tvContext("captions");
  for (let round = 0; round < 2; round += 1) {
    tv.creditBoostHit(round);
    assert.equal(tv.boostCharge, 1);
    tv.startBoost();
    assert.equal(tv.boostRemainingMs, 30000);
    assert.equal(tv.boostUsed, false);
    tv.endBoost();
    assert.equal(tv.boostBar.style.visibility, "");
  }
});

test("karaoke special pauses its countdown when the video is paused", () => {
  const tv = tvContext();
  tv.updateVocalBoost({ singing_ms: 40000 });
  tv.video.paused = true;
  tv.startBoost();
  tv.now = 50000;
  tv.tickBoost();
  assert.equal(tv.boostRemainingMs, 30000);
  tv.video.paused = false;
  tv.tickBoost();
  tv.now = 51000;
  tv.tickBoost();
  assert.equal(tv.boostRemainingMs, 29000);
});

test("each boosted vocal hit shows a multiplier star; ordinary hits do not", () => {
  const tv = tvContext();
  Object.assign(tv, {
    WebSocket: class {
      constructor() { this.readyState = 1; }
      send() {}
    },
    location: { protocol: "http:", host: "localhost" },
    setConnectionStatus() {}, renderCaptionBar() {}, updateVocalFeedback() {},
    currentScoreValue: element(), hitBlocks: new Set(), blockResults: new Map(),
  });
  runInNewContext(tvSource.slice(tvSource.indexOf("function connect("), tvSource.indexOf("async function setup(")), tv);
  tv.connect();
  for (const boosted of [false, true, true]) {
    tv.socket.onmessage({ data: JSON.stringify({
      type: "score_update", request_id: "song", singer_id: "singer",
      mode: "vocal", result: "hit", points: 50, singing_ms: 45000, boosted,
    }) });
  }
  assert.equal(tv.boostStars.children.length, 2);
  assert.equal(tv.boostStars.children[0].children[1].textContent, "2x");
});

test("live score numbers preserve points above the maximum, including after a late miss", () => {
  for (const mode of ["vocal", "captions"]) {
    const tv = tvContext(mode);
    Object.assign(tv, {
      WebSocket: class {
        constructor() { this.readyState = 1; }
        send() {}
      },
      location: { protocol: "http:", host: "localhost" },
      setConnectionStatus() {}, renderCaptionBar() {}, updateVocalFeedback() {},
      currentScoreValue: element(), hitBlocks: new Set(), blockResults: new Map(),
      rankingMax: 1000, activeScorePoints: 0,
    });
    runInNewContext(tvSource.slice(tvSource.indexOf("function connect("), tvSource.indexOf("async function setup(")), tv);
    tv.connect();
    for (const [result, points] of [["hit", 1250], ["miss", 1125]]) {
      tv.socket.onmessage({ data: JSON.stringify({
        type: "score_update", request_id: "song", singer_id: "singer",
        mode, result, points, singing_ms: 50000,
      }) });
      assert.equal(tv.activeScorePoints, points);
      assert.equal(tv.currentScoreValue.textContent, String(points));
    }
  }
});

function singerContext() {
  const context = {
    scoringMode: "vocal", singer: { singer_id: "singer" }, currentInvitation: { accepted: true },
    boostButton: element(), singerBoostButton: element(), skippingView: element(),
    skipButton: element(), acceptButton: element(), scoreStatus: element(),
    document: { querySelector: () => element() },
    clearError() {}, updateSkipClock() {}, updateFooter() {}, setTimeout() {},
    socket: { readyState: 1, send(data) { context.messages.push(JSON.parse(data)); } },
    WebSocket: { OPEN: 1 }, crypto: { randomUUID: () => "event" }, messages: [],
  };
  runInNewContext(singerSource.slice(singerSource.indexOf("function renderSkip("),
    singerSource.indexOf("function updateFooter(")), context);
  runInNewContext(singerSource.slice(singerSource.indexOf("function sendBoostRequest("),
    singerSource.indexOf("function sampleMicrophone(")), context);
  return context;
}

test("singer and donor buttons activate karaoke and disappear after the only special", () => {
  const singer = singerContext();
  const state = {
    items: [{ id: "song", singer_id: "singer", backvocals: [] }],
    invitation: { request_id: "song", accepted: true, lead_accepted: true },
    boost_ready: true, boost_active: false, boost_used: false,
  };
  singer.renderSkip(state);
  assert.equal(singer.singerBoostButton.hidden, false);
  assert.equal(singer.singerBoostButton.disabled, false);
  assert.equal(singer.singerBoostButton.textContent, "Usar energia acumulada");
  singer.singerBoostButton.events.click();
  assert.equal(singer.messages.at(-1).type, "boost_request");
  assert.equal(singer.messages.at(-1).request_id, "song");
  singer.singer = { singer_id: "donor" };
  singer.renderSkip(state);
  assert.equal(singer.boostButton.hidden, false);
  assert.equal(singer.boostButton.disabled, false);
  singer.boostButton.events.click();
  assert.equal(singer.messages.at(-1).type, "boost_request");
  state.boost_used = true;
  state.boost_active = true;
  state.boost_ready = false;
  singer.renderSkip(state);
  assert.equal(singer.boostButton.disabled, true);
  state.boost_active = false;
  singer.renderSkip(state);
  assert.equal(singer.boostButton.hidden, true);
  singer.singer = { singer_id: "singer" };
  singer.renderSkip(state);
  assert.equal(singer.singerBoostButton.hidden, true);
});

test("karaoke loudness surge uses the same activation request as playback", () => {
  const singer = singerContext();
  Object.assign(singer, {
    loudnessRequestId: "song", loudnessSum: 20, loudnessCount: 20, shortLoudness: 2,
    surgeFrames: 0, windowFrames: 0, windowSum: 0, windowStartedAt: 0, lastVoicedAt: 10000,
    loudnessBaselineWindows: 20, boostLoudnessPercent: 15, loudnessWindowMs: 250,
    boostActive: false, loudnessSurgeFrames: 2, lastBoostRequestAt: 0, boostRequestCooldownMs: 3000,
    performance: { now: () => 10000 },
  });
  runInNewContext(singerSource.slice(singerSource.indexOf("function trackLoudness("),
    singerSource.indexOf("function sendBoostRequest(")), singer);
  singer.trackLoudness(2, "song");
  singer.trackLoudness(2, "song");
  assert.equal(singer.messages.length, 1);
  assert.equal(singer.messages[0].type, "boost_request");
  assert.equal(singer.messages[0].request_id, "song");
  assert.match(singerSource, /if \(microphoneSpeaking && position !== null\) trackLoudness\(rms, requestId\);/);
});
