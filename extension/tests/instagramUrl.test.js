import assert from "node:assert/strict";
import { test } from "node:test";

import { fromPageInfo, parseInstagramUrl } from "../src/lib/instagramUrl.js";

const cases = [
  ["https://www.instagram.com/p/ABCDE12345/", { kind: "post", shortcode: "ABCDE12345",
    canonicalUrl: "https://www.instagram.com/p/ABCDE12345/" }],
  ["https://www.instagram.com/reel/ABCDE12345/?igsh=abc", { kind: "reel", shortcode: "ABCDE12345",
    canonicalUrl: "https://www.instagram.com/reel/ABCDE12345/" }],
  ["https://instagram.com/reels/ABCDE12345", { kind: "reel", shortcode: "ABCDE12345",
    canonicalUrl: "https://www.instagram.com/reel/ABCDE12345/" }],
  ["https://www.instagram.com/tv/ABCDE12345/", { kind: "reel", shortcode: "ABCDE12345",
    canonicalUrl: "https://www.instagram.com/p/ABCDE12345/" }],
  ["https://www.instagram.com/Fulano/p/ABCDE12345/", { kind: "post", shortcode: "ABCDE12345", username: "fulano",
    canonicalUrl: "https://www.instagram.com/p/ABCDE12345/" }],
  ["https://www.instagram.com/Fulano.Silva/", { kind: "profile", username: "fulano.silva",
    canonicalUrl: "https://www.instagram.com/fulano.silva/" }],
  ["https://www.instagram.com/fulano/reels/", { kind: "profile", username: "fulano",
    canonicalUrl: "https://www.instagram.com/fulano/" }],
  ["https://www.instagram.com/", { kind: "other" }],
  ["https://www.instagram.com/explore/", { kind: "other" }],
  ["https://www.instagram.com/stories/fulano/123/", { kind: "other" }],
  ["https://www.instagram.com/p/abc/", { kind: "other" }],
  ["https://example.com/p/ABCDE12345/", { kind: "other" }],
  ["chrome://extensions/", { kind: "other" }],
  ["", { kind: "other" }],
  [undefined, { kind: "other" }],
];

for (const [url, expected] of cases) {
  test(`parseInstagramUrl(${JSON.stringify(url)})`, () => {
    assert.deepEqual(parseInstagramUrl(url), expected);
  });
}

test("fromPageInfo converte a resposta do backend", () => {
  assert.deepEqual(fromPageInfo({ kind: "reel", shortcode: "X1234", canonical_url: "https://www.instagram.com/reel/X1234/" }),
    { kind: "reel", shortcode: "X1234", canonicalUrl: "https://www.instagram.com/reel/X1234/" });
  assert.deepEqual(fromPageInfo({ kind: "weird" }), { kind: "other" });
  assert.deepEqual(fromPageInfo(null), { kind: "other" });
});
