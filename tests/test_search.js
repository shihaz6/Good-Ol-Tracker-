// Run:  node tests/test_search.js
const assert = require("node:assert/strict");
const path = require("node:path");
const { detectSearch } = require(path.join(__dirname, "..", "extension", "search.js"));

const cases = [
  // [url, expected {engine, query} or null]
  ["https://search.brave.com/search?q=java+oop+inheritance&source=desktop", { engine: "Brave Search", query: "java oop inheritance" }],
  ["https://www.google.com/search?q=sql+normalization&oq=sql", { engine: "Google", query: "sql normalization" }],
  ["https://www.google.lk/search?q=sliit+timetable", { engine: "Google", query: "sliit timetable" }],
  ["https://www.bing.com/search?q=c%23+list+sort", { engine: "Bing", query: "c# list sort" }],
  ["https://duckduckgo.com/?q=privacy+browser&ia=web", { engine: "DuckDuckGo", query: "privacy browser" }],
  ["https://www.youtube.com/results?search_query=dbms+lecture", { engine: "YouTube", query: "dbms lecture" }],
  ["https://github.com/search?q=time+tracker&type=repositories", { engine: "GitHub", query: "time tracker" }],
  ["https://github.com/shihaz6/repo/search?q=flush", { engine: "GitHub", query: "flush" }],
  ["https://stackoverflow.com/search?q=sqlite+wal", { engine: "Stack Overflow", query: "sqlite wal" }],
  ["https://www.reddit.com/search/?q=best+laptop", { engine: "Reddit", query: "best laptop" }],
  ["https://en.wikipedia.org/w/index.php?search=Normal+form", { engine: "Wikipedia", query: "Normal form" }],
  ["https://www.amazon.com/s?k=mechanical+keyboard", { engine: "Amazon", query: "mechanical keyboard" }],
  ["https://www.daraz.lk/catalog/?q=headphones", { engine: "Daraz", query: "headphones" }],
  ["https://www.linkedin.com/search/results/all/?keywords=intern", { engine: "LinkedIn", query: "intern" }],
  ["https://x.com/search?q=python&src=typed_query", { engine: "X", query: "python" }],
  // generic: any site with a search-style parameter
  ["https://docs.python.org/3/search.html?q=asyncio", { engine: "docs.python.org", query: "asyncio" }],
  ["https://shop.example.com/find?keyword=usb+hub", { engine: "shop.example.com", query: "usb hub" }],
  // not searches
  ["https://www.youtube.com/watch?v=abc123", null],
  ["https://www.google.com/", null],
  ["https://github.com/shihaz6/Good-Ol-Tracker-", null],
  ["https://example.com/list?page=2", null],
  ["https://example.com/item?q=12345", null],          // numeric id, not a search
  ["https://www.google.com/search?q=+++", null],       // empty after trimming
  ["brave://settings/?search=cookies", null],          // browser pages ignored
];

let failed = 0;
for (const [url, expected] of cases) {
  try {
    assert.deepEqual(detectSearch(url), expected);
  } catch {
    failed++;
    console.log(`FAIL ${url}\n     got ${JSON.stringify(detectSearch(url))}, want ${JSON.stringify(expected)}`);
  }
}
console.log(`${cases.length - failed}/${cases.length} search detection cases passed`);
process.exit(failed ? 1 : 0);
