"""tools/clean_credits.py — classifier, resolution order, Commons wikitext parsing. No network."""
import json

import pytest

from tools import clean_credits as cc


@pytest.mark.parametrize("credit,cls", [
    ("https://www.artic.edu/artworks/8971", "url_led"),
    ("  www.example.org/x", "url_led"),
    ("1. http://www.dia.org2. Bridgeman Art Library: Object 1146953. Detroit Institute of Arts", "url_inside"),
    ("Photo by X, see http://example.org/y", "url_inside"),
    ("ygF9ozok7GQIvA at Google Cultural Institute maximum zoom level", "google_id"),
    ("Google Arts &amp; Culture — IQE1CY9y_Rfy5A", "google_id"),
    ("Google Arts & Culture — the-priest-yoshida-kenko-utagawa-hiroshige/SgGYDNZb1pY2og", "google_id"),
    ("Art Institute of Chicago: online database: entry 16776", "colon_db"),
    ("Gift of Smith &amp; Sons", "html_entity"),
    ("Art Institute of Chicago", None),
    ("", None),
    (None, None),
])
def test_classify(credit, cls):
    assert cc.classify(credit) == cls


def test_url_in_middle_is_url_inside():
    assert cc.classify("Detroit Institute of Arts (http://www.dia.org)") == "url_inside"


def _row(**kw):
    base = {"title": "T", "credit_line": "https://unmapped.example/artworks/1", "license": "PDM-1.0",
            "source": "Wikimedia Commons",
            "source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/A%20b.jpg?width=3840"}
    base.update(kw)
    return base


def _catalog(tmp_path, rows, name="x.json"):
    d = {"id": "x", "title": "X", "items": rows}
    (tmp_path / name).write_text(json.dumps(d, indent=1, ensure_ascii=False))
    (tmp_path / "_pins.json").write_text("{}")
    return tmp_path


def _run(tmp_path, rows, **kw):
    kw.setdefault("offline", False)
    kw.setdefault("fetch", lambda titles: {t: "no institution here" for t in titles})
    res, summ = cc.run(_catalog(tmp_path, rows), tmp_path / "nopackets", **kw)
    return res, summ


def test_repository_beats_host_map(tmp_path):
    res, _ = _run(tmp_path, [_row(current_repository="Some Museum")])
    assert (res[0]["new"], res[0]["path"]) == ("Some Museum", "repository")


def test_colon_prefix_first(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="Art Institute of Chicago: online database: entry 6565",
                                  current_repository="Other")])
    assert (res[0]["new"], res[0]["path"]) == ("Art Institute of Chicago", "colon_prefix")


def test_known_institution_in_text(tmp_path):
    credit = "1. http://www.example.org2. Bridgeman: Object 1146953. Detroit Institute of Arts"
    res, _ = _run(tmp_path, [_row(credit_line=credit, source="Other")])
    assert (res[0]["new"], res[0]["path"]) == ("Detroit Institute of Arts", "known_institution")


def test_host_map_credit_then_source_url(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="https://www.clevelandart.org/art/1"),
                             _row(credit_line="https://hdl.handle.net/10934/RM0001-X"),
                             _row(credit_line="https://hdl.handle.net/2027/other")])
    assert [(r["new"], r["path"]) for r in res[:2]] == [("Cleveland Museum of Art", "host_map"),
                                                         ("Rijksmuseum", "host_map")]
    assert res[2]["path"] == "fallback" and res[2]["new"] == "Wikimedia Commons"


def test_auction_and_image_hosts_not_mapped(tmp_path):
    for u in ("https://www.sothebys.com/x", "https://archive.org/details/y", "https://www.flickr.com/p/1",
              "https://artsandculture.google.com/asset/z"):
        assert cc.map_host(u) is None


def test_commons_fallback_and_source_institution(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="http://unknown.example/1"),
                             _row(credit_line="http://unknown.example/2", source="Art Institute of Chicago"),
                             _row(credit_line="http://unknown.example/3", source="Random Blog")])
    assert (res[0]["new"], res[0]["path"]) == ("Wikimedia Commons", "fallback")
    assert (res[1]["new"], res[1]["path"]) == ("Art Institute of Chicago", "fallback")
    assert res[2]["status"] == "unresolved" and res[2]["new"] is None


def test_cc_by_never_touched(tmp_path):
    for lic in ("CC-BY-4.0", "cc-by"):
        res, summ = _run(tmp_path, [_row(license=lic, current_repository="X")])
        assert res[0]["status"] == "cc_by_skipped" and res[0]["new"] is None
    assert summ["cc_by_skipped"] == 1


def test_html_entity_unescape_only(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="Gift of A &amp; B", current_repository="Other")])
    assert (res[0]["new"], res[0]["path"]) == ("Gift of A & B", "unescape")


def test_lookup_error_leaves_row_unchanged(tmp_path):
    def boom(titles):
        raise cc.LookupError_("503")
    res, _ = _run(tmp_path, [_row()], offline=False, fetch=boom, write=True)
    assert res[0]["status"] == "lookup_error" and res[0]["new"] is None
    on_disk = json.loads((tmp_path / "x.json").read_text())["items"][0]
    assert on_disk["credit_line"] == "https://unmapped.example/artworks/1"


def test_missing_page_is_lookup_error(tmp_path):
    res, _ = _run(tmp_path, [_row()], offline=False, fetch=lambda t: {x: None for x in t})
    assert res[0]["status"] == "lookup_error"


def test_commons_lookup_hit_and_miss(tmp_path):
    seen = []

    def fetch(titles):
        seen.extend(titles)
        return {t: "{{Artwork\n|institution = {{Institution:Rijksmuseum}}\n}}" for t in titles}
    res, _ = _run(tmp_path, [_row()], offline=False, fetch=fetch)
    assert seen == ["File:A b.jpg"]
    assert (res[0]["new"], res[0]["path"]) == ("Rijksmuseum", "commons")
    res2, _ = _run(tmp_path, [_row()], offline=False, fetch=lambda t: {x: "no template" for x in t})
    assert (res2[0]["new"], res2[0]["path"]) == ("Wikimedia Commons", "fallback")


def test_cosmos_packet_and_needs_review(tmp_path):
    pk = tmp_path / "packets"
    pk.mkdir()
    (pk / "a.json").write_text(json.dumps({"title": "Known", "facts": [
        {"key": "nasa.release_text.1", "value": "x"}, {"key": "nasa.credit", "value": "NASA, ESA, STScI"}]}))
    cat = tmp_path / "cat"
    cat.mkdir()
    _catalog(cat, [_row(title="Known"), _row(title="Unknown")], name="cosmos.json")
    res, summ = cc.run(cat, pk, offline=True)
    assert (res[0]["new"], res[0]["path"]) == ("NASA, ESA, STScI", "cosmos_packet")
    assert res[1]["status"] == "needs_review" and res[1]["new"] is None
    assert summ["status"]["needs_review"] == 1


def test_write_only_changes_credit_line_and_preserves_format(tmp_path):
    rows = [_row(title="Café", current_repository="Museum"), {"title": "ok", "credit_line": "Fine"}]
    _catalog(tmp_path, rows)
    before = (tmp_path / "x.json").read_text()
    cc.run(tmp_path, tmp_path / "np", offline=True, write=False)
    assert (tmp_path / "x.json").read_text() == before
    cc.run(tmp_path, tmp_path / "np", offline=True, write=True)
    after = json.loads((tmp_path / "x.json").read_text())
    expected = json.loads(before)
    expected["items"][0]["credit_line"] = "Museum"
    assert after == expected
    assert (tmp_path / "x.json").read_text() == cc.dump(expected) and "Café" in (tmp_path / "x.json").read_text()


# ------------------------------------------------------------------ Commons wikitext / titles
@pytest.mark.parametrize("wt,want", [
    ("{{Artwork\n |artist = X\n |institution = {{Institution:Art Institute of Chicago}}\n |date=1900\n}}",
     "Art Institute of Chicago"),
    ("{{Painting\n| Institution = [[Rijksmuseum|Rijks]]\n| x = y\n}}", "Rijks"),
    ("|institution = [[Barnes Foundation]]<ref>x</ref>\n|a=b", "Barnes Foundation"),
    ("blah {{Institution:Yale University Art Gallery}} blah", "Yale University Art Gallery"),
    ("{{Artwork\n|institution =\n|date=1900}}", None),
    ("nothing here", None),
    (None, None),
    ("{{Artwork\n|institution =\n|collection_display_name = National Gallery of Art, Washington DC\n|x=y}}",
     "National Gallery of Art, Washington DC"),
    ("{{Artwork\n|institution=\n|commons_institution = Rijksmuseum\n|collection_display_name = Other\n}}",
     "Rijksmuseum"),
    ("{{Artwork\n|institution =\n}}\n[[Category:Google Art Project works in Smithsonian American Art Museum]]",
     "Smithsonian American Art Museum"),
])
def test_institution_from_wikitext(wt, want):
    assert cc.institution_from_wikitext(wt) == want


def test_commons_title_forms():
    assert cc.commons_title({"source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/Grant%20Wood%20-%20American%20Gothic%20%281930%29.jpg?width=1"}) == "File:Grant Wood - American Gothic (1930).jpg"
    assert cc.commons_title({"thumbnail_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Foo_bar.jpg/600px-Foo_bar.jpg"}) == "File:Foo bar.jpg"
    assert cc.commons_title({"source_url": "https://example.org/x"}) is None


def test_norm_inst_aliases_and_private():
    assert cc.norm_inst("National Gallery") == "The National Gallery, London"
    assert cc.norm_inst("Metropolitan Museum of Art") == "The Metropolitan Museum of Art"
    assert cc.norm_inst("National Gallery of Art, Washington DC") == "National Gallery of Art"
    assert cc.norm_inst("Private collection") is None
    assert cc.norm_inst("Tate Britain") == "Tate Britain"


def test_private_collection_goes_to_fallback(tmp_path):
    res, _ = _run(tmp_path, [_row(), _row(current_repository="Private collection")], offline=False,
                  fetch=lambda t: {x: "|institution = {{Institution:Private collection}}" for x in t})
    assert [(r["new"], r["path"]) for r in res] == [("Wikimedia Commons", "fallback")] * 2


def test_new_host_map_entries():
    assert cc.map_host("https://www.getty.edu/x") == "J. Paul Getty Museum"
    assert cc.map_host("http://altemeister.museum-kassel.de/x") == "Museumslandschaft Hessen Kassel"
    assert cc.map_host("https://www.tate-images.com/x") is None


@pytest.mark.parametrize("bad", [
    "1. & 3. Addison Gallery of American Art2. PaintingDb, Object 8552",
    "Geoffrey C. Warren (1925) Elixir of Life {Uisge Beatha}. Dublin, p. 11. [1]",
    "Private collection", "Museum of Art",
])
def test_norm_inst_rejects_junk(bad):
    assert cc.norm_inst(bad) is None


def test_list_segment_extracts_addison():
    assert cc.list_segment("1. & 3. Addison Gallery of American Art2. PaintingDb, Object 8552") \
        == "Addison Gallery of American Art"
    assert cc.list_segment("1. Object 12 2. PaintingDb") is None


def test_html_entity_junk_extracts_or_falls_back(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="1. &amp; 3. Addison Gallery of American Art2. PaintingDb, Object 8"),
                             _row(credit_line="1. &amp; 2. Object 8")])
    assert (res[0]["new"], res[0]["path"]) == ("Addison Gallery of American Art", "list_segment")
    assert (res[1]["new"], res[1]["path"]) == ("Wikimedia Commons", "fallback")


@pytest.mark.parametrize("raw,want", [
    ("the Musée du Louvre", "Musée du Louvre"),
    ("drawings in the National Gallery of Art", "National Gallery of Art"),
    ("museum collection of the Prague City Gallery", "Prague City Gallery"),
    ("English_Heritage", "English Heritage"),
    ("The Walters Art Museum", "The Walters Art Museum"),   # capital "The" is part of the name
])
def test_norm_inst_strips_filler(raw, want):
    assert cc.norm_inst(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("Musée d’Orsay, Paris", "Musée d'Orsay"),
    ("Gemäldegalerie, Berlin", "Gemäldegalerie, Staatliche Museen zu Berlin"),
    ("the Gemäldegalerie Alte Meister (Dresden)", "Gemäldegalerie Alte Meister, Dresden"),
    ("The Toledo Museum of Art", "Toledo Museum of Art"),
    ("Uffizi", "Uffizi Gallery"),
    ("Widener Collection", "National Gallery of Art"),
    ("National Gallery of Scotland", "National Galleries of Scotland"),
])
def test_norm_inst_aliases_one_spelling(raw, want):
    assert cc.norm_inst(raw) == want


def test_offline_skips_commons_rows_unchanged(tmp_path):
    res, summ = _run(tmp_path, [_row(), _row(current_repository="Museum X")], offline=True, write=True)
    assert (res[0]["status"], res[0]["new"]) == ("skipped_offline", None)
    assert res[1]["new"] == "Museum X"
    on_disk = json.loads((tmp_path / "x.json").read_text())["items"]
    assert on_disk[0]["credit_line"] == "https://unmapped.example/artworks/1"
    assert summ["status"]["skipped_offline"] == 1


def test_html_entity_unescape_only_and_list_marker(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="Gift of Mr. &amp; Mrs. Smith, 1952")])
    assert (res[0]["new"], res[0]["path"]) == ("Gift of Mr. & Mrs. Smith, 1952", "unescape")


def test_override_applied_last(tmp_path):
    res, _ = _run(tmp_path, [_row(title="Still Life with Blue Pot", current_repository="Getty Research Institute")],
                  )
    assert res[0]["new"] == "Getty Research Institute"   # file x.json: no override
    d = tmp_path / "ov"
    d.mkdir()
    _catalog(d, [_row(title="Still Life with Blue Pot", current_repository="Getty Research Institute")],
             name="post-impressionism.json")
    res, _ = cc.run(d, d / "np", offline=True)
    assert (res[0]["new"], res[0]["path"]) == ("J. Paul Getty Museum", "override")


def test_known_institution_is_whole_name(tmp_path):
    res, _ = _run(tmp_path, [_row(credit_line="http://x.example Rijksmuseum Twenthe, Enschede", source="Other"),
                             _row(credit_line="http://x.example Rijksmuseum, Amsterdam", source="Other")])
    assert res[0]["path"] != "known_institution"
    assert (res[1]["new"], res[1]["path"]) == ("Rijksmuseum", "known_institution")


def test_write_validates_all_before_writing_any(tmp_path):
    _catalog(tmp_path, [_row(current_repository="Museum X")], name="a.json")
    (tmp_path / "b.json").write_text(json.dumps({"items": [_row(current_repository="Y")]}, indent=4))
    a_before = (tmp_path / "a.json").read_text()
    with pytest.raises(SystemExit):
        cc.run(tmp_path, tmp_path / "np", offline=True, write=True)
    assert (tmp_path / "a.json").read_text() == a_before


@pytest.mark.parametrize("raw,want", [
    ("São Paulo Museum of Art", "MASP"), ("Museo Thyssen-Bornemisza", "Thyssen-Bornemisza Museum"),
    ("Nasjonalgalleriet", "National Museum of Art, Architecture and Design"),
    ("The Museum of Modern Art", "Museum of Modern Art")])
def test_more_aliases(raw, want):
    assert cc.norm_inst(raw) == want


def test_rkd_not_mapped():
    assert cc.map_host("https://rkd.nl/images/1") is None
