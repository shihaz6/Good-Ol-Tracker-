// node tests/test_adult.js - which hosts the adult filter blocks (and must not block).
const { isAdultHost } = require("../extension/adult.js");

const blocked = [
  "pornhub.com", "www.pornhub.com", "de.pornhub.com", "xvideos.com", "onlyfans.com", "nhentai.net",
  "freepornvideos.net", "my-xxx-site.org", "best-hentai.io", "nsfwgifs.com", "rule34.xxx", "anything.xxx",
  "something.porn", "chaturbate.com", "PORNHUB.COM", "www.xnxx.com.",
];
const allowed = [
  "youtube.com", "google.com", "essex.ac.uk", "sussex.ac.uk", "middlesex.edu", "github.com", "sliit.lk",
  "wikipedia.org", "sextant.dev", "reddit.com", "x.com", "", "localhost", "expressjs.com", "xbox.com",
];

let fails = 0;
for (const h of blocked) if (!isAdultHost(h)) { fails++; console.log("NOT blocked:", h); }
for (const h of allowed) if (isAdultHost(h)) { fails++; console.log("wrongly blocked:", h); }
const total = blocked.length + allowed.length;
console.log(`${total - fails}/${total} adult filter cases passed`);
process.exit(fails ? 1 : 0);
