// Sites whose page content is never read, even with "scan all sites" on.
// Time on these sites (URL + tab title) is still tracked; only content is skipped.
// Shared by background.js (importScripts), options/popup pages (<script>) and tests (require).

const DEFAULT_BLOCKLIST = [
  // WhatsApp
  "whatsapp.com",
  // banks whose domain doesn't contain "bank" (those that do are caught automatically)
  "sampath.lk", "hnb.lk", "hnb.net", "boc.lk", "nsb.lk", "seylan.lk", "dfcc.lk",
  "nationstrust.com", "hsbc.lk", "hsbc.com", "sc.com", "citi.com",
  // password managers
  "passwords.google.com", "bitwarden.com", "1password.com", "lastpass.com",
  "dashlane.com", "keepersecurity.com", "nordpass.com",
  // not privacy: stops the tracker reading its own local pages (dashboard)
  "localhost", "127.0.0.1",
];

// "https://www.Example.com/path" -> "example.com"; "" if it isn't a usable domain.
function normalizeDomain(input) {
  let s = String(input || "").trim().toLowerCase();
  if (!s) return "";
  s = s.replace(/^[a-z]+:\/\//, "").split(/[/?#:]/)[0].replace(/^www\./, "");
  return /^[a-z0-9.-]+$/.test(s) && !s.startsWith(".") && !s.endsWith(".") ? s : "";
}

function isBlocked(hostname, list) {
  const host = normalizeDomain(hostname);
  if (!host) return true;
  if ((list || DEFAULT_BLOCKLIST).some((d) => host === d || host.endsWith("." + d))) return true;
  // Any bank: a hostname part containing "bank" (combank.lk, peoplesbank.lk, ndbbank.com, ...).
  return host.split(/[.-]/).some((part) => part.includes("bank"));
}

if (typeof module !== "undefined") module.exports = { DEFAULT_BLOCKLIST, isBlocked, normalizeDomain };
