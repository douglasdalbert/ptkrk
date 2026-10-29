import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../app-singer/app.js", import.meta.url), "utf8");
const selectionFunctions = source.slice(source.indexOf("async function selectVideo("), source.indexOf("async function runSearch("));

test("search selection sends forceKaraoke only for the karaoke list", async () => {
  for (const kind of ["captioned", "karaoke"]) {
    let payload;
    let select;
    const element = () => ({
      dataset: {},
      append() {},
      setAttribute() {},
      replaceChildren() {},
      addEventListener(event, handler) {
        if (event === "click" && this.className === "search-select") select = handler;
      },
    });
    const context = {
      document: {
        querySelector: element,
        createDocumentFragment: element,
        createElement: element,
      },
      knownVideoTitles: new Map(),
      sendingVideos: new Map(),
      myRequestFor: () => null,
      formatDuration: () => "",
      renderSearchButton() {},
      renderSearchButtons() {},
      clearError() {},
      setTimeout() {},
      api: async (url, options) => {
        assert.equal(url, "/api/requests");
        payload = JSON.parse(options.body);
        return { id: "request" };
      },
    };
    runInNewContext(selectionFunctions, context);
    context.renderSearchResults(kind, [{ video_id: "abcdefghijk", title: "Song" }]);
    await select();
    assert.deepEqual(payload, { youtubeCode: "abcdefghijk", forceKaraoke: kind === "karaoke" });
  }
});
