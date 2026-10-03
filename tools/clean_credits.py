"""
tools/clean_credits.py — replace harvest-artefact `credit_line` values in the served catalog with a real
courtesy credit (maintainer tool — NOT part of the runtime image). ADR-144: `display_credit()` hides
URL-led credits on /art; this is the data fix.

Junk classes (every row is classified):
  url_led        credit starts with http(s):// or www.
  url_inside     credit contains a URL anywhere
  google_id      Google Arts & Culture / Cultural Institute ID credits
  colon_db       "<Institution>: online database: entry 16776"  → the part before the first colon
  html_entity    `&amp;` etc. in an otherwise good credit       → html.unescape only (text kept as-is;
                 a leading list marker like '1.' is treated as junk instead)
Everything else is left untouched. CC BY rows are NEVER touched (ADR-142: credit stays exactly as given).

Resolution order for a junk row (first hit wins, the path is recorded):
  cosmos        (cosmos.json) NASA credit from the reground packet (`nasa.credit`), else needs_review
  colon_prefix  class colon_db
  repository    row's `current_repository`
  known_inst    a known institution name appears in the junk text
  host_map      URL host (credit, then source_url) → institution
  commons       Wikimedia Commons file page `{{Institution:X}}` / `institution =` (--offline: row left unchanged, status skipped_offline)
  fallback      "Wikimedia Commons" (Commons rows) / `source` when it is a known institution
  (else)        unresolved — row unchanged, listed

A failed Commons fetch is `lookup_error` and leaves the row unchanged — never baked as a verdict.

    python tools/clean_credits.py --report r.json              # dry run (default) incl. Commons lookup
    python tools/clean_credits.py --report r.json --offline    # skip the Commons lookup
    python tools/clean_credits.py --report r.json --write      # apply (only `credit_line` changes)
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "static" / "catalog"
PACKETS_DIR = Path.home() / "pieria-img" / "reground" / "packets" / "cosmos"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
UA = "Pieria-clean-credits/1.0 (art appliance maintainer tool; jmyost@gmail.com)"
BATCH = 40

# host (suffix match) or host/path-prefix → institution. No auction houses, image hosts, archive.org,
# flickr or Google here — those are not institutions.
HOST_MAP: dict[str, str] = {
    "artic.edu": "Art Institute of Chicago",
    "clevelandart.org": "Cleveland Museum of Art",
    "collection.barnesfoundation.org": "Barnes Foundation",
    "hdl.handle.net/10934": "Rijksmuseum",
    "nga.gov": "National Gallery of Art",
    "loc.gov": "Library of Congress",
    "nationalgallery.org.uk": "The National Gallery, London",
    "masp.org.br": "MASP",
    "artgallery.yale.edu": "Yale University Art Gallery",
    "media.iwm.org.uk": "Imperial War Museums",
    "skd-online-collection.skd.museum": "Staatliche Kunstsammlungen Dresden",
    "metmuseum.org": "The Metropolitan Museum of Art",
    "dia.org": "Detroit Institute of Arts",
    "rijksmuseum.nl": "Rijksmuseum",
    "getty.edu": "J. Paul Getty Museum",
    "mauritshuis.nl": "Mauritshuis",
    "clarkart.edu": "Clark Art Institute",
    "britishmuseum.org": "British Museum",
    "altemeister.museum-kassel.de": "Museumslandschaft Hessen Kassel",
    "moma.org": "Museum of Modern Art",
    "museodelprado.es": "Museo del Prado",
    "museothyssen.org": "Museo Thyssen-Bornemisza",
    "philamuseum.org": "Philadelphia Museum of Art",
    "collectionsonline.lacma.org": "Los Angeles County Museum of Art",
    "collections.artsmia.org": "Minneapolis Institute of Art",
    "sammlung.staedelmuseum.de": "Städel Museum",
    "cartelfr.louvre.fr": "Musée du Louvre",
    "timkenmuseum.org": "Timken Museum of Art",
    "ngv.vic.gov.au": "National Gallery of Victoria",
    "nationalgalleries.org": "National Galleries of Scotland",
    "collection.imamuseum.org": "Indianapolis Museum of Art at Newfields",
    "slam.org": "Saint Louis Art Museum",
    "dams.birminghammuseums.org.uk": "Birmingham Museums Trust",
    "collections.britishart.yale.edu": "Yale Center for British Art",
    "digitalcollections.folger.edu": "Folger Shakespeare Library",
    "dl.ndl.go.jp": "National Diet Library",
    "nla.gov.au": "National Library of Australia",
    "parismuseescollections.paris.fr": "Paris Musées",
    "eresources.nlb.gov.sg": "National Library Board, Singapore",
    "digital.library.pitt.edu": "University of Pittsburgh",
    "jpl.nasa.gov": "NASA/JPL-Caltech",
}

INSTITUTIONS = sorted({v for v in HOST_MAP.values() if len(v) >= 8}, key=len, reverse=True)
COMMONS_SOURCE = "Wikimedia Commons"

URL_LED = re.compile(r"(?i)^\s*(https?://|www\.)")
URL_ANY = re.compile(r"(?i)(https?://|www\.)")
URL_FIND = re.compile(r"(?i)(?:https?://|www\.)[^\s)\]>\"']+")
GOOGLE = re.compile(r"(?i)google\s+(arts\s*(&amp;|&|and)\s*culture|cultural\s+institute)|at\s+google\s+cultural")
COLON_DB = re.compile(r"(?i)^(.+?)\s*:\s*online database")
ENTITY = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")

CLASSES = ("url_led", "url_inside", "google_id", "colon_db", "html_entity")


def classify(credit: str | None) -> str | None:
    """Junk class of a credit_line, or None when it is clean."""
    if not credit or not isinstance(credit, str):
        return None
    if URL_LED.match(credit):
        return "url_led"
    if URL_ANY.search(credit):
        return "url_inside"
    if GOOGLE.search(credit):
        return "google_id"
    if COLON_DB.match(credit):
        return "colon_db"
    if ENTITY.search(credit) and html.unescape(credit) != credit:
        return "html_entity"
    return None


def is_cc_by(license_text: str | None) -> bool:
    s = re.sub(r"[\s_]+", "-", (license_text or "").lower())
    return "cc-by" in s


# one museum, one spelling (key: lowercase, ASCII apostrophe → canonical).
_ALIAS_SRC: dict[str, str] = {
    "national gallery": "The National Gallery, London",
    "the national gallery": "The National Gallery, London",
    "national gallery, london": "The National Gallery, London",
    "metropolitan museum of art": "The Metropolitan Museum of Art",
    "national gallery of art, washington dc": "National Gallery of Art",
    "national gallery of art, washington, d.c.": "National Gallery of Art",
    "national gallery of art, washington": "National Gallery of Art",
    "museum of fine arts, boston": "Museum of Fine Arts Boston",
    "musée d'orsay, paris": "Musée d'Orsay",
    "kunsthistorisches museum vienna (museum of fine arts)": "Kunsthistorisches Museum",
    "gemäldegalerie berlin": "Gemäldegalerie, Staatliche Museen zu Berlin",
    "gemäldegalerie, berlin": "Gemäldegalerie, Staatliche Museen zu Berlin",
    "gemäldegalerie alte meister (dresden)": "Gemäldegalerie Alte Meister, Dresden",
    "indianapolis museum of art at newfields": "Indianapolis Museum of Art",
    "the nelson-atkins museum of art": "Nelson-Atkins Museum of Art",
    "the museum of fine arts, houston": "Museum of Fine Arts, Houston",
    "j. paul getty museum, los angeles": "J. Paul Getty Museum",
    "van gogh museum, amsterdam": "Van Gogh Museum",
    "the kröller-müller museum": "Kröller-Müller Museum",
    "freer gallery of art": "Freer Gallery of Art, Smithsonian",
    "amon carter museum": "Amon Carter Museum of American Art",
    "städel": "Städel Museum",
    "uffizi": "Uffizi Gallery",
    "são paulo museum of art": "MASP",
    "museo thyssen-bornemisza": "Thyssen-Bornemisza Museum",
    "nasjonalgalleriet": "National Museum of Art, Architecture and Design",
    "the museum of modern art": "Museum of Modern Art",
    "barnes foundation, philadelphia": "Barnes Foundation",
    "the museum of modern art, new york": "Museum of Modern Art",
    "the national museum of western art": "National Museum of Western Art",
    "the toledo museum of art": "Toledo Museum of Art",
    "the art institute of chicago": "Art Institute of Chicago",
    "national gallery of scotland": "National Galleries of Scotland",
    # NGA donor collections (Commons source of each row confirmed to be the National Gallery of Art)
    "widener collection": "National Gallery of Art",
    "andrew w. mellon collection": "National Gallery of Art",
}
ALIASES = {k: v for k, v in _ALIAS_SRC.items()}
# per-row corrections the data cannot make, keyed (file, title); applied last.
OVERRIDES: dict[tuple[str, str], str] = {
    ("post-impressionism.json", "Still Life with Blue Pot"): "J. Paul Getty Museum",  # Getty Center object 103QVS
    ("romanticism.json", "Time (Time and the Old Women)"): "Palais des Beaux-Arts de Lille",  # Google slug: lille-palais-des-beaux-arts
}
# not a courtesy credit: private owners, generic fragments, mis-parsed contributor lines
NOT_A_CREDIT = {"private collection", "museum of art", "information technology university of minnesota"}
_FILLER = re.compile(r"^(?:drawings in the|museum collection of the|the)\s+")
_LIST_MARK = re.compile(r"\s*\d+\.(?:\s*[/&]\s*\d+\.)*\s*")
_JUNK_VALUE = re.compile(r"\d|[\[\]]|Object|PaintingDb")


def valid_credit(name: str | None) -> bool:
    """False for values that read as a citation / harvest fragment (digits, brackets, list markers)."""
    return bool(name) and not _JUNK_VALUE.search(name)


def _akey(n: str) -> str:
    return n.replace("\u2019", "'").lower()


def norm_inst(name: str | None) -> str | None:
    """Canonical spelling of an institution, or None when it is not a courtesy credit."""
    if not name:
        return None
    n = html.unescape(name).replace("_", " ").strip()
    n = _FILLER.sub("", n).strip()
    if not valid_credit(n) or _akey(n) in NOT_A_CREDIT:
        return None
    return ALIASES.get(_akey(n), n)


def list_segment(text: str) -> str | None:
    """First clean segment of a '1. X2. Y, Object 12' style credit (e.g. 'Addison Gallery of American Art')."""
    for seg in _LIST_MARK.split(text):
        n = norm_inst(seg.strip(" ,;"))
        if n and len(n) >= 6:
            return n
    return None


def _host_of(url: str) -> tuple[str, str]:
    u = url if re.match(r"(?i)https?://", url) else "http://" + url
    p = urlparse(u)
    return (p.hostname or "").lower(), p.path


def map_host(url: str) -> str | None:
    host, path = _host_of(url)
    for key, inst in HOST_MAP.items():
        kh, _, kp = key.partition("/")
        if host == kh or host.endswith("." + kh):
            if not kp or path.lstrip("/").startswith(kp):
                return inst
    return None


def credit_urls(credit: str | None) -> list[str]:
    return URL_FIND.findall(credit or "")


# ---------------------------------------------------------------------------- Commons wikitext parsing
_INST_TPL = re.compile(r"\{\{\s*Institution\s*:\s*([^{}|]+?)\s*(?:\|[^{}]*)?\}\}", re.I)
_COLL_PARAM = re.compile(r"(?is)\|\s*collection_display_name\s*=[ \t]*(.*?)(?=\n\s*\||\n\s*\}\}|\Z)")
_COMMONS_INST_PARAM = re.compile(r"(?is)\|\s*commons_institution\s*=[ \t]*(.*?)(?=\n\s*\||\n\s*\}\}|\Z)")
_GAP_CAT = re.compile(r"\[\[\s*Category\s*:\s*Google Art Project works in ([^\]|]+?)\s*(?:\|[^\]]*)?\]\]", re.I)
_INST_PARAM = re.compile(r"(?is)\|\s*institution\s*=[ \t]*(.*?)(?=\n\s*\||\n\s*\}\}|\Z)")


def strip_markup(text: str) -> str:
    t = re.sub(r"(?s)<!--.*?-->", "", text)
    t = re.sub(r"(?is)<ref[^>]*>.*?</ref>", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", t)
    for _ in range(3):
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = t.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip(" ,;")


def institution_from_wikitext(wikitext: str | None) -> str | None:
    """Institution named by a Commons file page: the `institution =` parameter first, else any
    `{{Institution:X}}` template. None when nothing sensible is found."""
    if not wikitext:
        return None
    def ok(c):
        c = strip_markup(c or "")
        return c if c and "=" not in c and len(c) <= 120 and not URL_ANY.search(c) else None

    cand = None
    m = _INST_PARAM.search(wikitext)
    if m:
        t = _INST_TPL.search(m.group(1))
        cand = ok(t.group(1) if t else m.group(1))
    if not cand:
        t = _INST_TPL.search(wikitext)
        cand = ok(t.group(1)) if t else None
    for rx in (_COMMONS_INST_PARAM, _COLL_PARAM):
        if cand:
            break
        m = rx.search(wikitext)
        if m:
            t = _INST_TPL.search(m.group(1))
            cand = ok(t.group(1) if t else m.group(1))
    if not cand:
        m = _GAP_CAT.search(wikitext)
        cand = ok(m.group(1)) if m else None
    return cand


def commons_title(row: dict) -> str | None:
    """`File:<name>` from source_url / thumbnail_url (Special:FilePath/<name> or upload.wikimedia.org)."""
    for key in ("source_url", "thumbnail_url"):
        u = row.get(key) or ""
        if not u:
            continue
        p = urlparse(u)
        path = p.path
        name = None
        if "commons.wikimedia.org" in (p.hostname or ""):
            m = re.search(r"/wiki/Special:(?:FilePath|Redirect/file)/(.+)$", path)
            if m:
                name = m.group(1)
            else:
                m = re.search(r"/wiki/(File:.+)$", path)
                if m:
                    return unquote(m.group(1)).replace("_", " ")
        elif (p.hostname or "") == "upload.wikimedia.org":
            parts = path.split("/")
            if "thumb" in parts:
                i = parts.index("thumb")
                name = parts[i + 3] if len(parts) > i + 3 else None
            elif len(parts) >= 2:
                name = parts[-1]
        if name:
            return "File:" + unquote(name).replace("_", " ")
    return None


class LookupError_(Exception):
    pass


def commons_fetch(titles: list[str]) -> dict[str, str | None]:
    """POST (GET 414s on long titles) → {title: wikitext}. Raises LookupError_ on any failure; a title
    that comes back missing maps to None (caller treats as lookup_error)."""
    import httpx

    out: dict[str, str | None] = {}
    for i in range(0, len(titles), BATCH):
        chunk = titles[i:i + BATCH]
        data = {"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                "format": "json", "formatversion": "2", "redirects": "1", "titles": "|".join(chunk)}
        last = None
        for attempt in range(3):
            try:
                r = httpx.post(COMMONS_API, data=data, headers={"User-Agent": UA}, timeout=60)
                r.raise_for_status()
                j = r.json()
                break
            except Exception as e:  # transient or hard — both are lookup_error, never a verdict
                last = e
                time.sleep(2 * (attempt + 1))
        else:
            raise LookupError_(f"{type(last).__name__}: {last}")
        q = j.get("query") or {}
        alias = {}
        for lst in (q.get("normalized") or [], q.get("redirects") or []):
            for m in lst:
                alias[m["from"]] = m["to"]
        pages = {}
        for pg in q.get("pages") or []:
            if pg.get("missing") or "revisions" not in pg:
                pages[pg.get("title")] = None
            else:
                pages[pg["title"]] = pg["revisions"][0]["slots"]["main"]["content"]
        for t in chunk:
            cur, seen = t, 0
            while cur in alias and seen < 5:
                cur, seen = alias[cur], seen + 1
            out[t] = pages.get(cur)
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------------------- resolution
def load_packets(packets_dir: Path) -> dict[str, str]:
    """cosmos catalog title → NASA credit (`nasa.credit` fact). Join key: catalog title (== packet
    `title`; all 104 match 1:1, and `catalog.source_url` matches too)."""
    out: dict[str, str] = {}
    if not packets_dir.is_dir():
        return out
    for f in sorted(packets_dir.glob("*.json")):
        try:
            p = json.loads(f.read_text())
        except Exception:
            continue
        for fa in p.get("facts") or []:
            if fa.get("key") == "nasa.credit" and (fa.get("value") or "").strip():
                out[p.get("title")] = fa["value"].strip()
                break
    return out


def resolve_static(row: dict, cls: str, filename: str, packets: dict[str, str]):
    """Steps that need no network. Returns (new, path) | ("NEED_COMMONS", None) | ("NEEDS_REVIEW", None)
    | (None, None) unresolved."""
    credit = row.get("credit_line") or ""
    if cls == "html_entity":
        un = html.unescape(credit)
        if not re.match(r"\s*\d+\.", un):
            return un, "unescape"
        seg = list_segment(un)
        if seg:
            return seg, "list_segment"
    if filename == "cosmos.json":
        nasa = packets.get(row.get("title"))
        return (html.unescape(nasa), "cosmos_packet") if nasa else ("NEEDS_REVIEW", None)
    if cls == "colon_db":
        pre = norm_inst(COLON_DB.match(credit).group(1))
        if pre:
            return pre, "colon_prefix"
    repo = norm_inst((row.get("current_repository") or "").strip())
    if repo:
        return repo, "repository"
    text = html.unescape(credit)
    for inst in INSTITUTIONS:
        # whole-name match: "Rijksmuseum Twenthe" must not collapse into "Rijksmuseum"
        if re.search(r"(?<!\w)" + re.escape(inst) + r"(?!\w)(?!\s+[A-Z])", text, re.I):
            return norm_inst(inst), "known_institution"
    for u in credit_urls(credit) + [row.get("source_url") or ""]:
        if u:
            inst = map_host(u)
            if inst:
                return norm_inst(inst), "host_map"
    if row.get("source") == COMMONS_SOURCE:
        return "NEED_COMMONS", None
    return resolve_fallback(row)


def resolve_fallback(row: dict):
    if row.get("source") == COMMONS_SOURCE:
        return COMMONS_SOURCE, "fallback"
    if row.get("source") in set(HOST_MAP.values()):
        return row["source"], "fallback"
    return None, None


def iter_catalog(catalog_dir: Path):
    for f in sorted(catalog_dir.glob("*.json")):
        if f.name.startswith("_") or f.name == "index.json":
            continue
        yield f


def dump(d: dict) -> str:
    """Byte-for-byte how the catalog files are written: indent=1, ensure_ascii=False, no trailing newline."""
    return json.dumps(d, indent=1, ensure_ascii=False)


def run(catalog_dir: Path, packets_dir: Path, fetch=None, offline: bool = False, write: bool = False):
    """Returns (report_rows, summary). `fetch(titles)->{title: wikitext|None}` is injectable."""
    packets = load_packets(packets_dir)
    results: list[dict] = []
    pending: list[dict] = []
    docs: dict[Path, tuple[str, dict]] = {}
    counts = {"rows": 0, "junk": 0, "cc_by_skipped": 0}
    status = Counter()
    unmapped = Counter()
    for f in iter_catalog(catalog_dir):
        raw = f.read_text(encoding="utf-8")
        d = json.loads(raw)
        docs[f] = (raw, d)
        for idx, row in enumerate(d.get("items") or []):
            counts["rows"] += 1
            cls = classify(row.get("credit_line"))
            if cls is None:
                continue
            counts["junk"] += 1
            res = {"file": f.name, "index": idx, "title": row.get("title"), "old": row.get("credit_line"),
                   "new": None, "class": cls, "path": None, "status": None}
            results.append(res)
            if is_cc_by(row.get("license")):
                counts["cc_by_skipped"] += 1
                res["status"] = "cc_by_skipped"
                continue
            new, path = resolve_static(row, cls, f.name, packets)
            if new == "NEED_COMMONS":
                if offline:
                    res["status"] = "skipped_offline"
                    continue
                else:
                    t = commons_title(row)
                    if t:
                        pending.append({"res": res, "row": row, "title": t})
                        continue
                    new, path = resolve_fallback(row)
            _settle(res, row, new, path)
    if pending:
        fetch = fetch or commons_fetch
        titles = sorted({p["title"] for p in pending})
        try:
            wt = fetch(titles)
            err = None
        except LookupError_ as e:
            wt, err = {}, str(e)
        for p in pending:
            res = p["res"]
            if err is not None or p["title"] not in wt or wt[p["title"]] is None:
                res["status"] = "lookup_error"
                res["error"] = err or "page missing / no wikitext"
                continue
            inst = norm_inst(institution_from_wikitext(wt[p["title"]]))
            if inst:
                _settle(res, p["row"], norm_inst(inst), "commons")
            else:
                new, path = resolve_fallback(p["row"])
                _settle(res, p["row"], new, path)
    for res in results:
        ov = OVERRIDES.get((res["file"], res["title"]))
        if ov and res["status"] not in ("cc_by_skipped", "lookup_error", "skipped_offline"):
            res.update(new=ov, path="override", status="changed" if ov != res["old"] else "unchanged")
        status[res["status"]] += 1
    # unmapped hosts across all junk rows that did not resolve through the host map
    for res in results:
        if res["path"] == "host_map":
            continue
        row = docs[catalog_dir / res["file"]][1]["items"][res["index"]]
        for u in credit_urls(res["old"]) + [row.get("source_url") or ""]:
            if not u:
                continue
            h, _ = _host_of(u)
            if h and h != "commons.wikimedia.org" and not map_host(u):
                unmapped[h] += 1
    summary = {
        **counts,
        "by_class": dict(Counter(r["class"] for r in results)),
        "by_path": dict(Counter(r["path"] for r in results if r["status"] == "changed")),
        "status": dict(status),
        "unmapped_hosts": dict(unmapped.most_common()),
        "values": dict(Counter(r["new"] for r in results if r["status"] == "changed").most_common()),
    }
    if write:
        changed_files = sorted({r["file"] for r in results if r["status"] == "changed"})
        for f in changed_files:  # validate EVERY file before writing ANY
            raw, d = docs[catalog_dir / f]
            if dump(d) != raw:
                raise SystemExit(f"{f}: round-trip dump != file bytes; nothing written")
        for f in changed_files:
            fp = catalog_dir / f
            raw, d = docs[fp]
            for r in results:
                if r["file"] == f and r["status"] == "changed":
                    d["items"][r["index"]]["credit_line"] = r["new"]
            fp.write_text(dump(d), encoding="utf-8")
    return results, summary


def _settle(res: dict, row: dict, new: str | None, path: str | None):
    if new == "NEEDS_REVIEW":
        res["status"] = "needs_review"
    elif not new:
        res["status"] = "unresolved"
    elif new == res["old"]:
        res["status"] = "unchanged"
    else:
        res.update(new=new, path=path, status="changed")
        return
    if path and new and new != "NEEDS_REVIEW":
        res["path"] = path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", required=True, help="path for the JSON report (required)")
    ap.add_argument("--write", action="store_true", help="apply changes to the catalog (default: dry run)")
    ap.add_argument("--offline", action="store_true", help="skip the Wikimedia Commons lookup")
    ap.add_argument("--dir", default=str(CATALOG_DIR))
    ap.add_argument("--packets", default=str(PACKETS_DIR))
    a = ap.parse_args(argv)
    results, summary = run(Path(a.dir), Path(a.packets), offline=a.offline, write=a.write)
    Path(a.report).write_text(json.dumps({"summary": summary, "rows": results}, indent=1, ensure_ascii=False))
    vals = Path(a.report).with_suffix(".values.json")
    vals.write_text(json.dumps(summary["values"], indent=1, ensure_ascii=False))
    print(json.dumps({k: v for k, v in summary.items() if k != "values"}, indent=1))
    print(f"distinct output values: {len(summary['values'])} -> {vals}")
    print(f"report: {a.report}  ({'WRITTEN' if a.write else 'dry run — no catalog writes'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
