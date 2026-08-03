/*
  search-operators.js — Parse inline operators from the smart search bar

  Supported syntax:
    plain words              → free-text query
    tag:vanilla              → require tag
    -tag:yaoi                → exclude tag
    artist:foo               → artist filter
    pages:>30  pages:<50     → page-count range
    pages:20                 → exact page count
    sort:popular             → sort mode  (popular | popular-week | popular-today | date)
    lang:english             → language filter (defaults to english anyway)

  Add a new operator? Add a case in `parseOperator` below.  The backend
  /api/search endpoint accepts the returned object as query params, so a
  new operator only requires backend awareness on the same JSON key name.
*/

export function parseSearch(input) {
  const out = {
    q: "",
    include_tags: [],
    exclude_tags: [],
    artist: null,
    pages_min: null,
    pages_max: null,
    sort: null,
    lang: "english",
  };
  const tokens = tokenize(input || "");
  const free = [];
  for (const t of tokens) {
    if (!parseOperator(t, out)) free.push(t);
  }
  out.q = free.join(" ").trim();
  return out;
}

function tokenize(s) {
  // Split on whitespace but keep quoted strings together.
  const re = /"([^"]+)"|(\S+)/g;
  const out = [];
  let m;
  while ((m = re.exec(s)) !== null) out.push(m[1] || m[2]);
  return out;
}

function parseOperator(tok, out) {
  const negated = tok.startsWith("-");
  const body = negated ? tok.slice(1) : tok;
  const idx = body.indexOf(":");
  if (idx <= 0) return false;
  const key = body.slice(0, idx).toLowerCase();
  const val = body.slice(idx + 1);
  switch (key) {
    case "tag":
      (negated ? out.exclude_tags : out.include_tags).push(val.toLowerCase());
      return true;
    case "artist":
      out.artist = val.toLowerCase();
      return true;
    case "pages": {
      if (val.startsWith(">")) out.pages_min = parseInt(val.slice(1), 10) || null;
      else if (val.startsWith("<")) out.pages_max = parseInt(val.slice(1), 10) || null;
      else {
        const n = parseInt(val, 10);
        if (!isNaN(n)) { out.pages_min = n; out.pages_max = n; }
      }
      return true;
    }
    case "sort":
      out.sort = val.toLowerCase();
      return true;
    case "lang":
      out.lang = val.toLowerCase();
      return true;
    default:
      return false;
  }
}
