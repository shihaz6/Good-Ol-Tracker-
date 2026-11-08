// Adult-site filter used by focus mode (and "block all the time" if you choose it).
// Two layers: a list of the big sites, plus words in the site's address that are
// practically never innocent ("porn", "xxx", ...), so new or unknown sites are caught too.
// Shared by background.js (importScripts) and tests/test_adult.js (Node).

const ADULT_DOMAINS = [
  "pornhub.com", "xvideos.com", "xvideos2.com", "xnxx.com", "xhamster.com", "xhamster.desi", "xhamster2.com",
  "redtube.com", "youporn.com", "tube8.com", "spankbang.com", "eporner.com", "txxx.com", "hqporner.com",
  "porntrex.com", "pornone.com", "tnaflix.com", "drtuber.com", "sunporno.com", "4tube.com", "porn.com",
  "beeg.com", "motherless.com", "heavy-r.com", "thumbzilla.com", "youjizz.com", "fapster.xxx",
  "brazzers.com", "realitykings.com", "bangbros.com", "naughtyamerica.com", "mofos.com", "digitalplayground.com",
  "onlyfans.com", "fansly.com", "manyvids.com", "clips4sale.com", "iwantclips.com", "loyalfans.com",
  "chaturbate.com", "stripchat.com", "bongacams.com", "livejasmin.com", "cam4.com", "camsoda.com",
  "myfreecams.com", "flirt4free.com", "imlive.com", "jerkmate.com", "streamate.com",
  "rule34.xxx", "rule34.paheal.net", "e621.net", "gelbooru.com", "danbooru.donmai.us", "nhentai.net",
  "hanime.tv", "hentaihaven.xxx", "e-hentai.org", "exhentai.org", "hitomi.la", "f95zone.to",
  "literotica.com", "sex.com", "adultfriendfinder.com", "fetlife.com", "erome.com", "redgifs.com",
  "scrolller.com", "porngifs.com", "nudostar.com", "thothub.to", "fapello.com", "coomer.su", "kemono.su",
  "javhd.com", "javlibrary.com", "missav.com", "missav.ws", "supjav.com", "jable.tv",
];

// Whole words inside the host name (between dots/dashes or at the edges are not required:
// "freepornvideos.net" should match). Kept to words with no common innocent use.
const ADULT_WORDS = ["porn", "xxx", "hentai", "nsfw", "xvideo", "xnxx", "xhamster", "camgirl", "sexcam", "nudes"];

const ADULT_HOST_REGEX = new RegExp(ADULT_WORDS.join("|"));

function isAdultHost(hostname) {
  const h = String(hostname || "").toLowerCase().replace(/^www\./, "").replace(/\.$/, "");
  if (!h) return false;
  if (ADULT_DOMAINS.some((d) => h === d || h.endsWith("." + d))) return true;
  return ADULT_HOST_REGEX.test(h) || /\.(xxx|porn|sex|adult)$/.test(h); // adult-only top-level domains
}

if (typeof module !== "undefined") module.exports = { ADULT_DOMAINS, ADULT_WORDS, isAdultHost };
