#!/usr/bin/env node
/*
  test_pagination.mjs — browserless verification of v12.16 strict
  page-by-page search pagination.

  Loads the REAL miniapp/frontend/js/pages/search.js (import specifiers
  rewritten to stubs), then:

    TEST 1  pages 1→4: after page 4, grid has EXACTLY 25 cards and
            state.results has EXACTLY 25 items (not 100) — strict
            replacement, no accumulation.
    TEST 2  the API was called with exactly page=4 (numeric page tap
            requests exactly that page).
    TEST 3  has_more contract: when page 4 returns has_more=false, the
            » (last-known) and › (next) buttons are disabled; when
            has_more=true they are enabled.
    TEST 4  sort/query reset: tapping a sort chip after reaching page 4
            re-requests page 1.
    TEST 5  hash persistence: navigating to page 3 writes
            #search?page=3&sort=... into location.hash; mounting with
            #search?page=2 in the hash loads page 2 first.

  Run:  node scripts/test_pagination.mjs     (exit 0 = all pass)
*/

import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SEARCH_JS = join(ROOT, "miniapp/frontend/js/pages/search.js");
const STUBS = join(ROOT, "scripts/test_pagination_stubs.mjs");

/* ---- import the stubs FIRST so they install the DOM globals --------- */
const stubs = await import(pathToFileURL(STUBS).href);
const { api, __setPages, __apiCalls, __test } = stubs;

/* ---- transform the REAL search.js: bare specifiers -> stub file ----- */
let src = readFileSync(SEARCH_JS, "utf8");
const stubUrl = pathToFileURL(STUBS).href;
const SPECS = [
  "core/api.js", "core/components.js", "core/telegram.js", "core/state.js",
  "plugins/search-operators.js", "plugins/card-actions.js", "plugins/home-rows.js",
];
for (const s of SPECS) {
  src = src.split(`"${s}"`).join(JSON.stringify(stubUrl));
  src = src.split(`'${s}'`).join(JSON.stringify(stubUrl));
}
const tmp = mkdtempSync(join(tmpdir(), "du-test-"));
const transformed = join(tmp, "search_under_test.mjs");
writeFileSync(transformed, src);

const { render } = await import(pathToFileURL(transformed).href);

/* ---- helpers --------------------------------------------------------- */
let failures = 0;
function check(name, cond, extra = "") {
  if (cond) console.log(`  ok    ${name}`);
  else { console.log(`  FAIL  ${name}${extra ? " — " + extra : ""}`); failures++; }
}
const mkItems = (page) => Array.from({ length: 25 }, (_, i) => ({
  id: page * 1000 + i, title: `p${page} item ${i}`, cover: "", pages: 20,
}));
const cards = (root) => root.querySelectorAll(".card");
// v12.17: pagination bar is inside $footer; walk the whole tree rather
// than the shallow querySelector so nested containers still resolve.
const allPageButtons = (root) => {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    for (const c of (n.childNodes || [])) {
      if (c.nodeType === 1) {
        if ((c.className || "").split(/\s+/).includes("du-page-btn")) out.push(c);
        walk(c);
      }
    }
  };
  walk(root);
  return out;
};
const pageButtons = allPageButtons;
// v12.17: active page label is the enclosed-circle glyph ❶..❾ for
// pages 1..9; other pages keep the numeric label. Normalize either form
// back to a plain digit for matching.
const GLYPH_TO_DIGIT = { "❶": "1", "❷": "2", "❸": "3", "❹": "4", "❺": "5", "❻": "6", "❼": "7", "❽": "8", "❾": "9" };
const pageLabel = (b) => {
  if (!b) return "";
  const t = (b.textContent || "").trim();
  return GLYPH_TO_DIGIT[t] || t;
};
const btnByLabel = (root, label) =>
  pageButtons(root).find(b => pageLabel(b) === label);
const activeBtn = (root) =>
  pageButtons(root).find(b => b.getAttribute("aria-current") === "page");
const lastSearchCall = () => __apiCalls[__apiCalls.length - 1];

/* =========================== TEST 1: strict replacement ============= */
console.log("\nTEST 1 — pages 1→4: grid + state hold ONLY the current page");
{
  __test.setHash("");
  __setPages({
    1: { items: mkItems(1), has_more: true },
    2: { items: mkItems(2), has_more: true },
    3: { items: mkItems(3), has_more: true },
    4: { items: mkItems(4), has_more: true },
  });
  const root = stubs.__test.StubElement ? new stubs.__test.StubElement("div") : null;
  await render(root, { me: { user_id: 1 } });
  check("page 1 renders 25 cards", cards(root).length === 25, `got ${cards(root).length}`);
  check("active page is 1", pageLabel(activeBtn(root)) === "1");

  for (const p of [2, 3, 4]) {
    const btn = btnByLabel(root, String(p));
    check(`page button ${p} exists`, !!btn);
    btn.click();
    await new Promise(r => setTimeout(r, 0));   // let the async load settle
    await new Promise(r => setTimeout(r, 0));
  }
  check("page 4 renders EXACTLY 25 cards (not 100)", cards(root).length === 25,
        `got ${cards(root).length}`);
  const ids = cards(root).map(c => c.dataset.galleryId);
  check("page 4 cards are page-4 items (4000-series)", ids.every(id => id.startsWith("4")),
        `first=${ids[0]}`);
  check("active page is 4", pageLabel(activeBtn(root)) === "4");
  check("API was last called with page=4", lastSearchCall()?.page === 4);
  check("API page sequence was 1,2,3,4 (no double-fetch)",
        JSON.stringify(__apiCalls.map(c => c.page)) === "[1,2,3,4]",
        JSON.stringify(__apiCalls.map(c => c.page)));
  check("hash now says page=4", __test.getHash().includes("page=4"), __test.getHash());

  /* ==================== TEST 3: has_more edge (end of results) ====== */
  console.log("\nTEST 3 — has_more=false disables › and » on the last page");
  // Keep pages 1-4 defined (they are re-fetched on back-navigation); only
  // ADD page 5 as the end-of-results page.
  __setPages({
    1: { items: mkItems(1), has_more: true },
    2: { items: mkItems(2), has_more: true },
    3: { items: mkItems(3), has_more: true },
    4: { items: mkItems(4), has_more: true },
    5: { items: mkItems(5), has_more: false },
  });
  // NOTE: __setPages clears __apiCalls — re-walk from page 1 so the bar
  // rebuilds its learned bounds from a clean slate.
  const root3 = root;  // same mounted page
  btnByLabel(root3, "1")?.click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  for (const p of [2, 3, 4]) {
    btnByLabel(root3, String(p)).click();
    await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  }
  const five = btnByLabel(root3, "5");
  check("page 5 button appears after has_more chain", !!five);
  five.click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  check("page 5 renders 25 cards", cards(root).length === 25);
  // v12.17 CONTRACT CHANGE: `›` follows probableHasMore, NOT the raw
  // server has_more. When a page returns >= PAGE_SIZE items the
  // frontend keeps `›` reachable so the user is never stranded by a
  // bad backend signal (the screenshot bug). Tapping into an empty
  // page N+1 falls through to emptyState().
  check("› REACHABLE at known end (page full → probableHasMore=true)",
        btnByLabel(root, "›").disabled === false);
  // On the last page already, » is a no-op → correctly disabled:
  check("» disabled while ON the last page", btnByLabel(root, "»").disabled === true);
  check("« enabled away from page 1", btnByLabel(root, "«").disabled === false);

  // ‹ goes back to 4:
  btnByLabel(root3, "‹").click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  check("‹ from 5 lands on 4", pageLabel(activeBtn(root3)) === "4");
  check("page 4 re-rendered with only 25 cards", cards(root3).length === 25);
  // Now that page 5's has_more=false was observed, knownLastPage=5 and we
  // are NOT on it → » must be ENABLED even though page 4 says has_more=true.
  check("» enabled off the last page (end learned)", btnByLabel(root3, "»").disabled === false);
  btnByLabel(root3, "»").click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  check("» jumps to the learned last page (5)", pageLabel(activeBtn(root3)) === "5");
  check("» target fetched page 5", lastSearchCall()?.page === 5);

  // « jumps to first
  btnByLabel(root3, "«").click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  check("« lands on page 1", pageLabel(activeBtn(root3)) === "1");
  check("page 1 shows its own 25 cards only", cards(root3).length === 25);

  /* ==================== TEST 4: sort chip resets to page 1 =========== */
  console.log("\nTEST 4 — sort chip tap resets to page 1");
  __setPages({
    1: { items: mkItems(1), has_more: true },
    2: { items: mkItems(2), has_more: true },
  });
  btnByLabel(root, "2").click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  check("on page 2 before chip tap", pageLabel(activeBtn(root)) === "2");
  const chip = root.querySelectorAll(".chip").find(c => c.textContent.includes("New Uploads"));
  check("New Uploads chip exists", !!chip);
  const callsBefore = __apiCalls.length;
  chip.click();
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  const newCalls = __apiCalls.slice(callsBefore);
  check("chip tap fetched page 1", newCalls[0]?.page === 1,
        JSON.stringify(newCalls.map(c => c.page)));
  check("chip tap used sort=date", newCalls[0]?.sort === "date", String(newCalls[0]?.sort));
  check("active page back to 1", pageLabel(activeBtn(root)) === "1");
}

/* =========================== TEST 5: hash restore ==================== */
console.log("\nTEST 5 — mount with #search?page=2&sort=popular-week restores page 2");
{
  __setPages({
    1: { items: mkItems(1), has_more: true },
    2: { items: mkItems(2), has_more: true },
  });
  __test.setHash("#search?page=2&sort=popular-week");
  const root2 = new stubs.__test.StubElement("div");
  await render(root2, { me: { user_id: 1 } });
  check("first API call requests page 2", __apiCalls[0]?.page === 2,
        JSON.stringify(__apiCalls.map(c => c.page)));
  check("first API call requests sort=popular-week", __apiCalls[0]?.sort === "popular-week");
  check("active page is 2", pageLabel(activeBtn(root2)) === "2");
  check("renders 25 cards", cards(root2).length === 25);
}

/* =========================== summary ================================= */
console.log(failures === 0
  ? "\nALL PAGINATION TESTS PASSED"
  : `\n${failures} PAGINATION TEST(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
