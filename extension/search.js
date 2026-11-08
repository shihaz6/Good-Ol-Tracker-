// Search detection from a URL alone (no page access needed).
// Loaded by background.js via importScripts; also loaded by tests/test_search.js.

// Known search pages: exact host + path + the parameter that holds the query.
const SEARCH_SITES = [
  { name: "Google", host: /(^|\.)google\.[a-z.]+$/, path: /^\/search/, param: "q" },
  { name: "Brave Search", host: /^search\.brave\.com$/, path: /^\/(search|images|videos|news)/, param: "q" },
  { name: "Bing", host: /(^|\.)bing\.com$/, path: /^\/(search|images|videos|news)/, param: "q" },
  { name: "DuckDuckGo", host: /(^|\.)duckduckgo\.com$/, path: /^\/$/, param: "q" },
  { name: "Ecosia", host: /(^|\.)ecosia\.org$/, path: /^\/(search|images|videos|news)/, param: "q" },
  { name: "Yahoo", host: /(^|\.)search\.yahoo\.com$/, path: /^\/search/, param: "p" },
  { name: "YouTube", host: /(^|\.)youtube\.com$/, path: /^\/results/, param: "search_query" },
  { name: "GitHub", host: /^github\.com$/, path: /\/search$/, param: "q" },
  { name: "Stack Overflow", host: /(^|\.)stackoverflow\.com$/, path: /^\/search/, param: "q" },
  { name: "Reddit", host: /(^|\.)reddit\.com$/, path: /\/search\/?$/, param: "q" },
  { name: "Wikipedia", host: /(^|\.)wikipedia\.org$/, path: /^\/(w\/index\.php|wiki\/Special:Search)/, param: "search" },
  { name: "Amazon", host: /(^|\.)amazon\.[a-z.]+$/, path: /^\/s\/?$/, param: "k" },
  { name: "Daraz", host: /(^|\.)daraz\.(lk|pk|com\.bd|com\.np)$/, path: /^\/catalog/, param: "q" },
  { name: "LinkedIn", host: /(^|\.)linkedin\.com$/, path: /^\/search/, param: "keywords" },
  { name: "X", host: /^(x|twitter)\.com$/, path: /^\/search/, param: "q" },
];

// Any other site: a query parameter with one of these names counts as a search.
const GENERIC_SEARCH_PARAMS = new Set([
  "q", "query", "search", "search_query", "searchterm", "search_term", "keyword", "keywords",
]);

// Returns { engine, query } or null.
function detectSearch(rawUrl) {
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  if (!/^https?:$/.test(u.protocol)) return null;

  const clean = (v) => {
    const q = (v || "").replace(/\s+/g, " ").trim();
    // Ignore empty, absurdly long, or purely numeric values (page ids, not searches).
    return q && q.length <= 500 && !/^\d+$/.test(q) ? q : null;
  };

  const host = u.hostname.toLowerCase();
  const known = SEARCH_SITES.find((s) => s.host.test(host));
  if (known) {
    if (!known.path.test(u.pathname)) return null; // e.g. a YouTube video, not a search
    const query = clean(u.searchParams.get(known.param));
    return query ? { engine: known.name, query } : null;
  }

  for (const [key, value] of u.searchParams) {
    if (GENERIC_SEARCH_PARAMS.has(key.toLowerCase())) {
      const query = clean(value);
      if (query) return { engine: host.replace(/^www\./, ""), query };
    }
  }
  return null;
}

if (typeof module !== "undefined") module.exports = { detectSearch };
