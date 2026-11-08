// Run:  node tests/test_blocklist.js
const assert = require("node:assert/strict");
const path = require("node:path");
const { isBlocked, normalizeDomain } = require(path.join(__dirname, "..", "extension", "blocklist.js"));

// Default policy: only WhatsApp, banks and password managers (+ this app's own localhost pages).
const blocked = [
  "web.whatsapp.com", "www.whatsapp.com",
  "online.combank.lk", "www.peoplesbank.lk", "ndbbank.com", "www.sampath.lk", "www.hnb.lk",
  "www.boc.lk", "bank.example.com", "my-bank.io",
  "passwords.google.com", "vault.bitwarden.com", "my.1password.com", "www.lastpass.com",
  "localhost", "127.0.0.1",
  "", "not a host!",
];
const allowed = [
  "mail.google.com", "claude.ai", "chatgpt.com", "docs.google.com", "drive.google.com",
  "discord.com", "web.telegram.org", "www.paypal.com", "login.example.com",
  "accounts.google.com", "checkout.shop.com", "immigration.gov.lk",
  "www.youtube.com", "github.com", "stackoverflow.com", "en.wikipedia.org",
  "medium.com", "www.daraz.lk", "google.com",
];

let failed = 0;
for (const h of blocked) if (!isBlocked(h)) { failed++; console.log(`FAIL should block: ${h}`); }
for (const h of allowed) if (isBlocked(h)) { failed++; console.log(`FAIL should allow: ${h}`); }

// user list overrides: custom entry blocks subdomains too
if (!isBlocked("api.mysite.dev", ["mysite.dev"])) { failed++; console.log("FAIL custom list subdomain"); }

assert.equal(normalizeDomain("https://www.Example.com/path?q=1"), "example.com");
assert.equal(normalizeDomain("  sub.site.io  "), "sub.site.io");
assert.equal(normalizeDomain("http://localhost:8080/x"), "localhost");
assert.equal(normalizeDomain("bad domain"), "");

const total = blocked.length + allowed.length + 1;
console.log(`${total - failed}/${total} blocklist cases passed (+ normalizeDomain checks)`);
process.exit(failed ? 1 : 0);
