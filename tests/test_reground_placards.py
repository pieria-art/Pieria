"""Unit tests for tools/reground_placards.py — no network, no model (both mocked)."""

import asyncio
import json

import ai_client
from tools import reground_placards as rg


class _FakeFetcher:
    """Records every get_json call and answers from a canned {url: body} map, keyed loosely by a
    substring of the params (good enough for these narrow unit tests)."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get_json(self, url, params=None):
        self.calls.append((url, params))
        for match, body in self.responses:
            if match in json.dumps(params or {}):
                return body, None
        return None, "no_fixture"


# --------------------------------------------------------------------------- accession-year guard
def test_accession_number_shapes_are_detected():
    assert rg.looks_like_accession_number("1940.116")
    assert rg.looks_like_accession_number("2019.34.1")
    assert rg.looks_like_accession_number(" 1940 . 116 ")


def test_plain_dates_are_not_accession_shaped():
    assert not rg.looks_like_accession_number("1874")
    assert not rg.looks_like_accession_number("c. 1665")
    assert not rg.looks_like_accession_number("1874-1876")
    assert not rg.looks_like_accession_number("")
    assert not rg.looks_like_accession_number(None)


def test_structured_fields_drop_accession_shaped_date_and_fall_back():
    # Yale's Homer "Young Girl" case (audit n=33): only date candidate is the 1940.116 accession
    # number; there's no other date source, and the existing catalog value is the real 1874.
    bundle = {
        "facts": [
            rg._fact("museum.objectDate", "1940.116", "Yale Object API", "https://yale/x", "CC0"),
        ],
        "conflicts": [],
        "is_version_of": None,
    }
    existing = {"creation_date": "1874", "date_display": "1874"}
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, existing)
    assert fields.get("date_display") == "1874"
    assert fields.get("date_source") == "existing_catalog_value"
    assert any("accession" in n for n in notes)


# --------------------------------------------------------------------------- precedence + conflicts
def test_precedence_prefers_museum_over_wikidata_over_commons():
    bundle = {
        "facts": [
            rg._fact("wikidata.made_from_material", ["oil paint"], "Wikidata", "https://wd/Q1", "CC0"),
            rg._fact("museum.medium", "Watercolor and gouache over graphite", "NGA API", "https://nga/x", "CC0"),
            rg._fact("commons.Medium", "oil on canvas", "Wikimedia Commons", "https://commons/x", "CC BY-SA"),
        ],
        "conflicts": [{"field": "medium", "detail": "conflicting medium classes: ['oil', 'watercolor']"}],
        "is_version_of": None,
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert fields["medium"] == "Watercolor and gouache over graphite"
    assert fields["medium_source"] == "NGA API"
    assert needs_review is True


# --------------------------------------------------------------------------- agent_name (round 5)
def _bundle(facts, conflicts=None):
    return {"facts": facts, "conflicts": conflicts or [], "is_version_of": None}


def test_agent_name_never_taken_from_commons_artist_photographer_credit():
    # audit finding: commons.Artist is routinely the PHOTOGRAPHER/uploader, not the painter.
    bundle = _bundle([rg._fact("commons.Artist", "Didier Descouens", "Wikimedia Commons", "u", "CC BY-SA")])
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Gustav Klimt"})
    assert fields.get("agent_name_confirmed") is None
    assert "agent_name_disagreement" not in fields
    assert needs_review is False


def test_agent_name_never_taken_from_commons_artist_institution_credit():
    # audit finding: commons.Artist can even be the holding INSTITUTION (Rijksmuseum on 100+ Hiroshige
    # prints), not a person at all.
    bundle = _bundle([rg._fact("commons.Artist", "Rijksmuseum", "Wikimedia Commons", "u", "CC BY-SA")])
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Utagawa Hiroshige"})
    assert fields.get("agent_name_confirmed") is None
    assert needs_review is False


def test_agent_name_filled_in_from_museum_record_when_catalog_has_none():
    bundle = _bundle([rg._fact("museum.creators", ["Winslow Homer"], "Met API", "u", "CC0")])
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Unknown Artist"})
    assert fields["agent_name_confirmed"] == "Winslow Homer"
    assert fields["agent_name_source"] == "Met API"
    assert needs_review is False


def test_agent_name_confirmed_keeps_catalogs_own_spelling_on_agreement():
    bundle = _bundle([rg._fact("wikidata.creator", ["Rembrandt"], "Wikidata", "u", "CC0")])
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Rembrandt van Rijn"})
    assert fields["agent_name_confirmed"] == "Rembrandt van Rijn"  # not overwritten with the mononym
    assert needs_review is False


def test_agent_name_disagreement_flags_needs_review_and_keeps_catalog_value():
    bundle = _bundle([rg._fact("wikidata.creator", ["Jacopo Amigoni"], "Wikidata", "u", "CC0")])
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Gustav Klimt"})
    assert fields.get("agent_name_confirmed") is None
    assert fields.get("agent_name_disagreement") == "Jacopo Amigoni"
    assert needs_review is True
    assert any("Jacopo Amigoni" in n for n in notes)


def test_run_import_writes_agent_name_confirmed_into_catalog(tmp_path, monkeypatch):
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    (tmp_path / "written").mkdir()
    packet = {"title": "Mystery Portrait", "facts": [],
              "structured": {"agent_name_confirmed": "Frans Hals"}}
    (packets_dir / "mystery-portrait-0000.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", tmp_path / "written")
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", tmp_path / "catalog")
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"mystery-portrait-0000": "demo"}))
    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [dict(title="Mystery Portrait", agent_name="Unknown Artist")]})

    rg.run_import()
    out = json.loads((tmp_path / "catalog" / "demo.json").read_text())["items"][0]
    assert out["agent_name"] == "Frans Hals"


def test_conflict_detection_flags_medium_class_mismatch_not_average():
    facts = [
        rg._fact("museum.medium", "oil on canvas", "Met API", "u", "CC0"),
        rg._fact("wikidata.made_from_material", ["watercolor"], "Wikidata", "u", "CC0"),
    ]
    kept, conflicts = rg._detect_conflicts(facts)
    assert conflicts and conflicts[0]["field"] == "medium"
    # kept facts unchanged (no averaging — resolve_structured_fields does the precedence pick)
    assert kept == facts


def test_conflict_detection_flags_date_spread_over_25_years():
    facts = [
        rg._fact("museum.objectDate", "1827", "Louvre API", "u", "CC0"),
        rg._fact("wikidata.inception", ["1844"], "Wikidata", "u", "CC0"),
        rg._fact("commons.DateTimeOriginal", "1900", "Wikimedia Commons", "u", "CC BY-SA"),
    ]
    _, conflicts = rg._detect_conflicts(facts)
    assert any(c["field"] == "date" for c in conflicts)


def test_conflict_detection_ignores_accession_shaped_date_candidates():
    facts = [
        rg._fact("museum.objectDate", "1940.116", "Yale API", "u", "CC0"),
        rg._fact("wikidata.inception", ["1874"], "Wikidata", "u", "CC0"),
    ]
    _, conflicts = rg._detect_conflicts(facts)
    assert not any(c["field"] == "date" for c in conflicts)


# --------------------------------------------------------------------------- claim check
def _bundle_with_facts(facts):
    return {"facts": facts, "conflicts": [], "is_version_of": None, "check_only_texts": []}


def test_claim_check_rejects_claim_with_no_fact_keys():
    bundle = _bundle_with_facts([rg._fact("wikidata.inception", ["1889"], "Wikidata", "u", "CC0")])
    claims = [{"text": "Painted in 1889.", "fact_keys": []}]
    ok, bad = rg.check_claims(claims, bundle)
    assert not ok and bad["text"] == "Painted in 1889."


def test_claim_check_rejects_claim_citing_nonexistent_fact_key():
    bundle = _bundle_with_facts([rg._fact("wikidata.inception", ["1889"], "Wikidata", "u", "CC0")])
    claims = [{"text": "Painted in 1889.", "fact_keys": ["museum.objectDate"]}]
    ok, bad = rg.check_claims(claims, bundle)
    assert not ok


def test_claim_check_rejects_year_not_present_in_cited_facts():
    # facts say 1889, claim asserts 1940 while citing the 1889 fact — containment check must catch it.
    bundle = _bundle_with_facts([rg._fact("wikidata.inception", ["1889"], "Wikidata", "u", "CC0")])
    claims = [{"text": "Painted in 1940 in Paris.", "fact_keys": ["wikidata.inception"]}]
    ok, bad = rg.check_claims(claims, bundle)
    assert not ok and bad["text"].startswith("Painted in 1940")


def test_claim_check_accepts_claim_backed_by_its_cited_fact():
    bundle = _bundle_with_facts([rg._fact("wikidata.inception", ["1889"], "Wikidata", "u", "CC0")])
    claims = [{"text": "Painted in 1889.", "fact_keys": ["wikidata.inception"]}]
    ok, bad = rg.check_claims(claims, bundle)
    assert ok and bad is None


def test_claim_check_accepts_claim_with_no_numeric_specifics():
    bundle = _bundle_with_facts([rg._fact("wikidata.depicts", ["a fishing boat"], "Wikidata", "u", "CC0")])
    claims = [{"text": "The scene shows a fishing boat at sea.", "fact_keys": ["wikidata.depicts"]}]
    ok, bad = rg.check_claims(claims, bundle)
    assert ok


# --------------------------------------------------------------------------- version detection
def test_version_detection_path_present_when_wikidata_states_after_a_work_by():
    bundle = {
        "facts": [], "conflicts": [],
        "is_version_of": {"relation": "after a work by", "of_qid": "Q123", "of_label": "The Death of Sardanapalus (1827)", "property": "P1877"},
        "check_only_texts": [],
    }
    fields = {"medium": "Oil on canvas", "date_display": "1844"}
    prompt = rg.build_narrative_prompt(bundle, fields)
    assert "after a work by" in prompt
    assert "The Death of Sardanapalus (1827)" in prompt
    assert "copy/cast/replica/version" in prompt


def test_template_fallback_states_version_relation_plainly():
    item = {"title": "The Death of Sardanapalus", "agent_name": "Eugène Delacroix"}
    fields = {"medium": "Oil on canvas", "date_display": "1844", "current_repository": "Philadelphia Museum of Art"}
    bundle = {"is_version_of": {"relation": "after a work by", "of_qid": "Q1", "of_label": "The Death of Sardanapalus (1827, Louvre)"}}
    out = rg._template_placard_grounded(item, fields, bundle)
    assert "after a work by" in out["description_narrative"]
    assert "1827" in out["description_narrative"]


# --------------------------------------------------------------------------- template fallback (no version)
def test_template_fallback_uses_grounded_fields_never_invents():
    item = {"title": "Boys wading", "agent_name": "Winslow Homer", "source": "National Gallery of Art"}
    fields = {"medium": "Watercolor and gouache over graphite", "date_display": "1873", "current_repository": "National Gallery of Art"}
    out = rg._template_placard_grounded(item, fields, {"is_version_of": None})
    assert "Watercolor and gouache over graphite" in out["description_narrative"]
    assert "National Gallery of Art" in out["description_narrative"]
    assert "oil" not in out["description_narrative"].lower()


# --------------------------------------------------------------------------- boilerplate-reuse regression
def test_prompts_differ_for_two_items_sharing_cultural_context_when_facts_differ():
    """Regression for the audit's n=29/31 and n=30/34 Homer pairs: the OLD build_catalog prompt fed
    only title/artist/date/medium/source/cultural_context, so two works by the same artist with the
    same cultural_context text converged on nearly identical boilerplate. The re-grounded prompt is
    built from each item's OWN retrieved facts bundle, so two items sharing cultural_context but
    resolving to different Wikidata items must not produce the same prompt."""
    bundle_a = _bundle_with_facts([rg._fact("wikidata.depicts", ["sailboats off Prout's Neck"], "Wikidata", "u", "CC0")])
    bundle_b = _bundle_with_facts([rg._fact("wikidata.depicts", ["sunset over Gloucester harbor"], "Wikidata", "u", "CC0")])
    fields = {"medium": "Oil on canvas", "date_display": "1880"}
    prompt_a = rg.build_narrative_prompt(bundle_a, fields)
    prompt_b = rg.build_narrative_prompt(bundle_b, fields)
    assert prompt_a != prompt_b
    assert "Prout's Neck" in prompt_a and "Gloucester" not in prompt_a
    assert "Gloucester" in prompt_b and "Prout's Neck" not in prompt_b


# --------------------------------------------------------------------------- generate_narrative (mocked model)
def test_generate_narrative_regenerates_once_on_failed_claim_check(monkeypatch):
    bundle = _bundle_with_facts([rg._fact("wikidata.inception", ["1889"], "Wikidata", "u", "CC0")])
    calls = []

    def fake_chat(role, messages, json_mode=False, **kwargs):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            return json.dumps({"description_narrative": "Painted in 1940.", "tags": "x",
                                "claims": [{"text": "Painted in 1940.", "fact_keys": ["wikidata.inception"]}]})
        return json.dumps({"description_narrative": "Painted in 1889.", "tags": "x",
                            "claims": [{"text": "Painted in 1889.", "fact_keys": ["wikidata.inception"]}]})

    monkeypatch.setattr(ai_client, "chat", fake_chat)
    data, fell_back = asyncio.run(rg.generate_narrative(bundle, {}, asyncio.Semaphore(8)))
    assert len(calls) == 2
    assert not fell_back
    assert data["description_narrative"] == "Painted in 1889."


def test_generate_narrative_falls_back_to_template_after_two_bad_attempts(monkeypatch):
    bundle = _bundle_with_facts([rg._fact("wikidata.inception", ["1889"], "Wikidata", "u", "CC0")])

    def fake_chat(role, messages, json_mode=False, **kwargs):
        return json.dumps({"description_narrative": "Painted in 1940.", "tags": "x",
                            "claims": [{"text": "Painted in 1940.", "fact_keys": ["wikidata.inception"]}]})

    monkeypatch.setattr(ai_client, "chat", fake_chat)
    data, fell_back = asyncio.run(rg.generate_narrative(bundle, {}, asyncio.Semaphore(8)))
    assert fell_back is True


def test_generate_narrative_falls_back_on_model_error(monkeypatch):
    bundle = _bundle_with_facts([])

    def boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(ai_client, "chat", boom)
    data, fell_back = asyncio.run(rg.generate_narrative(bundle, {}, asyncio.Semaphore(8)))
    assert data is None and fell_back is True


# --------------------------------------------------------------------------- batched label lookup
def test_labels_for_qids_makes_one_batched_call_not_one_per_id(monkeypatch):
    rg._LABEL_CACHE.clear()
    fx = _FakeFetcher([("Q1|Q2|Q3", {"entities": {
        "Q1": {"labels": {"en": {"value": "Alpha"}}},
        "Q2": {"labels": {"en": {"value": "Beta"}}},
        "Q3": {},  # no English label -> falls back to the QID itself
    }})])
    labels = asyncio.run(rg._labels_for_qids(fx, ["Q1", "Q2", "Q3"]))
    assert labels == {"Q1": "Alpha", "Q2": "Beta", "Q3": "Q3"}
    assert len(fx.calls) == 1  # one request for all three ids, not three


def test_labels_for_qids_uses_the_process_cache_on_a_repeat_id(monkeypatch):
    rg._LABEL_CACHE.clear()
    fx = _FakeFetcher([("Q9", {"entities": {"Q9": {"labels": {"en": {"value": "Once"}}}}})])
    asyncio.run(rg._labels_for_qids(fx, ["Q9"]))
    assert len(fx.calls) == 1
    # second lookup for the same id (as another item's creator, say) must not hit the network again
    labels = asyncio.run(rg._labels_for_qids(fx, ["Q9"]))
    assert labels == {"Q9": "Once"}
    assert len(fx.calls) == 1


# --------------------------------------------------------------------------- medium bucketing
# --------------------------------------------------------------------------- Commons QS-annotation cleanup
def test_commons_text_strips_quickstatement_annotation():
    # Live-run regression: Commons extmetadata DateTimeOriginal routinely embeds a hidden Wikidata
    # bot annotation right after the human-readable date; it must never reach a structured field.
    raw = "December 1888date QS:P571,+1888-12-00T00:00:00Z/10"
    assert rg._clean_commons_text(raw) == "December 1888"


def test_commons_text_strips_html_and_collapses_whitespace():
    assert rg._clean_commons_text("<i>1901</i>  ") == "1901"


def test_commons_text_strips_quickstatement_annotation_with_multiple_properties():
    raw = ("between 1650 and 1699date QS:P,+1650-00-00T00:00:00Z/7,P1319,"
           "+1650-00-00T00:00:00Z/9,P1326,+1699-00-00T00:00:00Z/9")
    assert rg._clean_commons_text(raw) == "between 1650 and 1699"


def test_commons_text_strips_label_quickstatement_annotation():
    raw = 'Fish and Rocks "label QS:Len,"Fish and Rocks ""'
    assert rg._clean_commons_text(raw) == "Fish and Rocks"


# --------------------------------------------------------------------------- Commons credit -> museum
def test_find_institution_and_accession_from_cleveland_credit():
    inst, acc = rg._find_institution_and_accession("https://clevelandart.org/art/1953.247")
    assert inst == "Cleveland Museum of Art" and acc == "1953.247"


def test_find_institution_and_accession_no_match_returns_none_none():
    assert rg._find_institution_and_accession("just some free text, no museum url") == (None, None)


# --------------------------------------------------------------------------- current_repository validity
def test_institution_label_accepted_for_museum_gallery_library_names():
    assert rg.looks_like_institution_label("Cleveland Museum of Art")
    assert rg.looks_like_institution_label("National Gallery")
    assert rg.looks_like_institution_label("Yale University Art Gallery")


def test_institution_label_accepts_center_and_centre():
    # regression: devils-bridge-st-gotthards-pass-0059 / tours-sunset-looking-backwards-0074's real
    # holder, "Yale Center for British Art", was rejected outright — "Center"/"Centre" wasn't in the
    # institution-keyword list at all.
    assert rg.looks_like_institution_label("Yale Center for British Art")
    assert rg.looks_like_institution_label("Pompidou Centre")


def test_institution_label_rejects_bare_qid_and_places():
    assert not rg.looks_like_institution_label("Q214867")
    assert not rg.looks_like_institution_label("Moon")
    assert not rg.looks_like_institution_label("Paris")
    assert not rg.looks_like_institution_label("")
    assert not rg.looks_like_institution_label(None)


def test_current_repository_never_a_bare_qid_or_a_wikidata_place():
    # audit n=93 regression: Apollo 11 bootprint photo's P276 "location" is the Moon — must never
    # land in current_repository, and a museum-API institution name is trusted directly.
    bundle = {
        "facts": [rg._fact("wikidata.collection", ["Moon"], "Wikidata", "u", "CC0")],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, _, notes = rg.resolve_structured_fields(bundle, {})
    assert "current_repository" not in fields
    assert any("rejected" in n for n in notes)


def test_current_repository_accepts_museum2_institution_from_commons_credit_lookup():
    bundle = {
        "facts": [rg._fact("museum2.institution", "Cleveland Museum of Art", "Cleveland Open Access API (by accession)", "u", "CC0")],
        "conflicts": [], "is_version_of": None,
    }
    fields, _, _ = rg.resolve_structured_fields(bundle, {})
    assert fields["current_repository"] == "Cleveland Museum of Art"


def test_medium_precedence_prefers_commons_credit_museum_record_over_wikidata():
    # audit n=45 regression: Cleveland's own record (fetched via the Commons Credit accession) says
    # handscroll; Wikidata's material claim must not win over it.
    bundle = {
        "facts": [
            rg._fact("wikidata.made_from_material", ["ink on paper"], "Wikidata", "u", "CC0"),
            rg._fact("museum2.technique", "Handscroll; ink on paper", "Cleveland Open Access API (by accession)", "u", "CC0"),
        ],
        "conflicts": [], "is_version_of": None,
    }
    fields, _, _ = rg.resolve_structured_fields(bundle, {})
    assert fields["medium"] == "Handscroll; ink on paper"


# --------------------------------------------------------------------------- item_key / batches
def test_item_key_is_slug_plus_index_and_stable():
    assert rg.item_key(45, "The Ray") == "ray-0045"  # _slug drops a leading article, like audit_placards
    assert rg.item_key(0, "") == "untitled-0000"


def test_build_batches_groups_collection_coherent_and_respects_size():
    entries = [{"key": f"k{i}", "collection": "a" if i < 5 else "b"} for i in range(10)]
    batches = rg.build_batches(entries, batch_size=4)
    assert sum(len(b) for b in batches) == 10
    assert len(batches) == 3
    # first batch is entirely collection "a" (sorted by collection first)
    assert all(e["collection"] == "a" for e in batches[0])


# --------------------------------------------------------------------------- visual claim validation
def test_visual_claim_accepts_generic_visible_description():
    ok, reason = rg._visual_claim_ok("The scene shows a fisherman hauling a net at sea.", "The Herring Net", [])
    assert ok and reason is None


def test_visual_claim_rejects_a_year():
    ok, reason = rg._visual_claim_ok("Painted around 1885 near the coast.", "The Herring Net", [])
    assert not ok and "number" in reason


def test_visual_claim_rejects_unbacked_proper_noun():
    ok, reason = rg._visual_claim_ok("The boat sails past Gloucester harbor.", "Sunset", [])
    assert not ok and "proper noun" in reason


def test_visual_claim_accepts_proper_noun_present_in_title():
    ok, reason = rg._visual_claim_ok("Gloucester harbor is calm at dusk.", "Sunset at Gloucester", [])
    assert ok


def test_visual_claim_accepts_proper_noun_present_in_facts():
    facts = [rg._fact("wikidata.depicts", ["Gloucester harbor"], "Wikidata", "u", "CC0")]
    ok, reason = rg._visual_claim_ok("Boats rest in Gloucester harbor.", "Untitled", facts)
    assert ok


# --------------------------------------------------------------------------- import validation
def _packet(facts):
    return {"title": "Two Sailboats", "facts": facts}


def test_validate_written_item_passes_grounded_claims():
    packet = _packet([rg._fact("wikidata.inception", ["1880"], "Wikidata", "u", "CC0")])
    written = {"description_narrative": "Two Sailboats, 1880.", "tags": "boats",
               "claims": [{"text": "Painted in 1880.", "fact_keys": ["wikidata.inception"], "visual": False}]}
    ok, reasons = rg.validate_written_item(written, packet)
    assert ok and not reasons


def test_validate_written_item_rejects_unbacked_year_claim():
    packet = _packet([rg._fact("wikidata.inception", ["1880"], "Wikidata", "u", "CC0")])
    written = {"description_narrative": "Two Sailboats, 1883.", "tags": "boats",
               "claims": [{"text": "Painted in 1883.", "fact_keys": ["wikidata.inception"], "visual": False}]}
    ok, reasons = rg.validate_written_item(written, packet)
    assert not ok and reasons


def test_validate_written_item_rejects_visual_claim_with_proper_noun():
    packet = _packet([])
    written = {"description_narrative": "A scene at Prout's Neck.", "tags": "boats",
               "claims": [{"text": "It shows Prout's Neck studio.", "fact_keys": [], "visual": True}]}
    ok, reasons = rg.validate_written_item(written, packet)
    assert not ok and reasons


def test_validate_written_item_rejects_empty_narrative():
    packet = _packet([])
    written = {"description_narrative": "", "tags": "", "claims": []}
    ok, reasons = rg.validate_written_item(written, packet)
    assert not ok and "empty description_narrative" in reasons


# --------------------------------------------------------------------------- date_display normalisation
def _time_claim(time_str, precision, circa=False):
    c = {"mainsnak": {"datavalue": {"value": {"time": time_str, "precision": precision}}}}
    if circa:
        c["qualifiers"] = {"P1480": [{}]}
    return c


def test_format_wikidata_date_year_precision():
    assert rg.format_wikidata_date(_time_claim("+1873-01-01T00:00:00Z", 9)) == "1873"


def test_format_wikidata_date_decade_precision():
    assert rg.format_wikidata_date(_time_claim("+1878-00-00T00:00:00Z", 8)) == "1870s"


def test_format_wikidata_date_century_precision():
    assert rg.format_wikidata_date(_time_claim("+1850-00-00T00:00:00Z", 7)) == "19th century"


def test_format_wikidata_date_circa_qualifier():
    assert rg.format_wikidata_date(_time_claim("+1873-00-00T00:00:00Z", 9, circa=True)) == "c. 1873"


def test_format_wikidata_date_day_precision_collapses_to_year_for_artworks():
    claim = _time_claim("+1885-03-13T00:00:00Z", 11)
    assert rg.format_wikidata_date(claim, allow_day_precision=False) == "1885"


def test_format_wikidata_date_day_precision_kept_for_photos_and_space():
    claim = _time_claim("+1969-07-20T00:00:00Z", 11)
    assert rg.format_wikidata_date(claim, allow_day_precision=True) == "20 July 1969"


# --------------------------------------------------------------------------- capture-timestamp rejection
def test_capture_timestamp_rejects_time_of_day():
    assert rg.looks_like_capture_timestamp("13 March 2008, 13:55:16")


def test_capture_timestamp_rejects_year_at_or_after_upload():
    assert rg.looks_like_capture_timestamp("2008", upload_year=2008)
    assert rg.looks_like_capture_timestamp("2010", upload_year=2008)


def test_capture_timestamp_rejects_year_after_death():
    assert rg.looks_like_capture_timestamp("1920", death_year=1909)


def test_capture_timestamp_accepts_plausible_creation_date():
    assert not rg.looks_like_capture_timestamp("1885", upload_year=2015, death_year=1910)


# --------------------------------------------------------------------------- garbled-date rejection
def test_garbled_date_rejects_style_word_and_unbalanced_paren():
    assert rg.looks_like_garbled_date("1632 Baroque (late 16th century")


def test_garbled_date_rejects_text_with_no_year_or_century():
    assert rg.looks_like_garbled_date("some description text")


def test_garbled_date_accepts_clean_year_and_century_expressions():
    assert not rg.looks_like_garbled_date("1873")
    assert not rg.looks_like_garbled_date("19th century")
    assert not rg.looks_like_garbled_date("between 1650 and 1699")


# --------------------------------------------------------------------------- institution phrase extraction
def test_extract_institution_phrase_finds_named_institution():
    assert rg._extract_institution_phrase("Collection of the National Gallery of Art, Washington") \
        == "National Gallery of Art"


def test_extract_institution_phrase_none_for_plain_text():
    assert rg._extract_institution_phrase("just a photo, no institution named") is None


# --------------------------------------------------------------------------- is_artwork_entity (P144/P1877 target)
def test_is_artwork_entity_true_for_a_painting(monkeypatch):
    rg._ARTWORK_ENTITY_CACHE.clear()
    rg._LABEL_CACHE.clear()
    fx = _FakeFetcher([
        ("\"ids\": \"Q1\"", {"entities": {"Q1": {"claims": {"P31": [
            {"mainsnak": {"datavalue": {"value": {"id": "Q3305213"}}}}
        ]}}}}),
        ("Q3305213", {"entities": {"Q3305213": {"labels": {"en": {"value": "painting"}}}}}),
    ])
    assert asyncio.run(rg._is_artwork_entity(fx, "Q1")) is True


def test_is_artwork_entity_false_for_a_theme(monkeypatch):
    rg._ARTWORK_ENTITY_CACHE.clear()
    rg._LABEL_CACHE.clear()
    fx = _FakeFetcher([
        ("\"ids\": \"Q2\"", {"entities": {"Q2": {"claims": {"P31": [
            {"mainsnak": {"datavalue": {"value": {"id": "Q9998"}}}}
        ]}}}}),
        ("Q9998", {"entities": {"Q9998": {"labels": {"en": {"value": "narrative motif"}}}}}),
    ])
    assert asyncio.run(rg._is_artwork_entity(fx, "Q2")) is False


# --------------------------------------------------------------------------- identity mismatch detection
def test_identity_mismatch_detects_different_creator():
    # wikidata.creator only — commons.Artist alone must NOT trigger this (it routinely names the
    # photographer of a 2D reproduction, not the painter; see the mononym test below).
    item = {"agent_name": "Gustav Klimt", "medium": "Oil on canvas"}
    facts = [rg._fact("wikidata.creator", "Jacopo Amigoni", "Wikidata", "u", "CC0")]
    result = rg.detect_identity_mismatch(item, facts)
    assert result and any("Amigoni" in e for e in result["evidence"])


def test_identity_mismatch_ignores_commons_artist_alone_photographer_credit():
    # audit finding: Commons "Artist" on a reproduction photo can be the PHOTOGRAPHER (e.g. Didier
    # Descouens), not the painter — must not false-positive when Wikidata isn't present to corroborate.
    item = {"agent_name": "Gustav Klimt", "medium": "Oil on canvas"}
    facts = [rg._fact("commons.Artist", "Didier Descouens", "Wikimedia Commons", "u", "CC BY-SA")]
    assert rg.detect_identity_mismatch(item, facts) is None


def test_identity_mismatch_tolerates_mononym_against_full_name():
    # Rembrandt van Rijn (catalog) vs "Rembrandt" (Wikidata mononym) must NOT be flagged.
    item = {"agent_name": "Rembrandt van Rijn", "medium": "Oil on canvas"}
    facts = [rg._fact("wikidata.creator", "Rembrandt", "Wikidata", "u", "CC0")]
    assert rg.detect_identity_mismatch(item, facts) is None


def test_identity_mismatch_detects_after_artist_pattern():
    item = {"agent_name": "J. M. W. Turner", "medium": "Oil on canvas"}
    facts = [rg._fact("commons.Credit", "William Miller after Turner", "Wikimedia Commons", "u", "CC BY-SA")]
    result = rg.detect_identity_mismatch(item, facts)
    assert result and any("after Turner" in e for e in result["evidence"])


def test_identity_mismatch_detects_print_technique_vs_oil_medium():
    item = {"agent_name": "J. M. W. Turner", "medium": "Oil on canvas"}
    facts = [rg._fact("commons.ObjectName", "Popular Graphic Arts engraving", "Wikimedia Commons", "u", "CC BY-SA")]
    result = rg.detect_identity_mismatch(item, facts)
    assert result and any("engraving" in e for e in result["evidence"])


def test_identity_mismatch_none_when_consistent():
    item = {"agent_name": "Winslow Homer", "medium": "Watercolor on paper"}
    facts = [rg._fact("commons.Artist", "Winslow Homer", "Wikimedia Commons", "u", "CC BY-SA")]
    assert rg.detect_identity_mismatch(item, facts) is None


# --------------------------------------------------------------------------- import flags (image_title_mismatch)
def test_run_import_collects_image_title_mismatch_flags(tmp_path, monkeypatch):
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    written_dir = tmp_path / "written"
    written_dir.mkdir()
    out_catalog_dir = tmp_path / "catalog"

    packet = {"title": "Campfire in the Adirondacks", "facts": [], "structured": {}}
    (packets_dir / "campfire-adirondacks-0015.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", written_dir)
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", out_catalog_dir)
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"campfire-adirondacks-0015": "demo"}))

    class _FakeCatalogItem(dict):
        pass

    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [dict(title="Campfire in the Adirondacks", agent_name="X")] * 16})

    written = {"key": "campfire-adirondacks-0015", "title": "Campfire in the Adirondacks",
               "description_narrative": "A hunter rests by tree roots.", "tags": "outdoors",
               "claims": [], "flags": ["image_title_mismatch"]}
    (written_dir / "batch_01.jsonl").write_text(json.dumps(written) + "\n")

    report = rg.run_import()
    assert report["flagged"] == [{"key": "campfire-adirondacks-0015", "collection": "demo",
                                   "flags": ["image_title_mismatch"], "title": "Campfire in the Adirondacks"}]
    assert report["passed"] == 1  # a flag never blocks the narrative import


# --------------------------------------------------------------------------- field corrections (round 4)
def test_validate_field_correction_accepts_grounded_value():
    packet = {"facts": [rg._fact("commons.Credit", "Drawing, Storm Coming", "Wikimedia Commons", "u", "CC BY-SA")]}
    ok, reason = rg.validate_field_correction({"value": "Charcoal on paper", "fact_keys": ["commons.Credit"]}, packet)
    assert ok


def test_validate_field_correction_rejects_unbacked_number():
    packet = {"facts": [rg._fact("wikidata.inception", ["1873"], "Wikidata", "u", "CC0")]}
    ok, reason = rg.validate_field_correction({"value": "1942", "fact_keys": ["wikidata.inception"]}, packet)
    assert not ok


def test_validate_field_correction_rejects_no_fact_keys():
    ok, reason = rg.validate_field_correction({"value": "Charcoal", "fact_keys": []}, {"facts": []})
    assert not ok and "no real fact_keys" in reason


def test_apply_field_corrections_overrides_existing_catalog_value_only():
    packet = {
        "facts": [rg._fact("commons.Credit", "Drawing, Storm Coming", "Wikimedia Commons", "u", "CC BY-SA")],
        "structured": {"medium": "Oil on canvas", "medium_source": "existing_catalog_value"},
    }
    written = {"field_corrections": {"medium": {"value": "Charcoal drawing", "fact_keys": ["commons.Credit"]}}}
    base = {"medium": "Oil on canvas"}
    applied, rejected = rg.apply_field_corrections(written, packet, base)
    assert applied == {"medium": "Charcoal drawing"} and base["medium"] == "Charcoal drawing"


def test_apply_field_corrections_never_overrides_a_verified_source():
    packet = {
        "facts": [rg._fact("museum.medium", "Oil on canvas", "Met API", "u", "CC0")],
        "structured": {"medium": "Oil on canvas", "medium_source": "Met Collection API"},
    }
    written = {"field_corrections": {"medium": {"value": "Watercolor", "fact_keys": ["museum.medium"]}}}
    base = {"medium": "Oil on canvas"}
    applied, rejected = rg.apply_field_corrections(written, packet, base)
    assert applied == {} and "medium" in rejected and base["medium"] == "Oil on canvas"


def test_run_import_applies_correction_and_blanks_uncorrected_field_on_identity_mismatch(tmp_path, monkeypatch):
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    written_dir = tmp_path / "written"
    written_dir.mkdir()

    packet = {
        "title": "Storm Coming", "facts": [
            {"key": "commons.Credit", "value": "Drawing, Storm Coming", "source": "Wikimedia Commons",
             "source_url": "u", "licence": "CC BY-SA"},
        ],
        "structured": {"medium": "Oil on canvas", "medium_source": "existing_catalog_value",
                        "date_display": "1925", "date_source": "existing_catalog_value"},
    }
    (packets_dir / "storm-coming-0125.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", written_dir)
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", tmp_path / "catalog")
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"storm-coming-0125": "demo"}))
    monkeypatch.setattr(rg, "load_catalog", lambda: {
        "demo": [dict(title="Storm Coming", agent_name="X", medium="Oil on canvas", date_display="1925")] * 126
    })

    written = {
        "key": "storm-coming-0125", "title": "Storm Coming",
        "description_narrative": "A charcoal drawing of a storm.", "tags": "storm",
        "claims": [], "flags": ["identity_mismatch"],
        "field_corrections": {"medium": {"value": "Charcoal on paper", "fact_keys": ["commons.Credit"]}},
    }
    (written_dir / "batch_01.jsonl").write_text(json.dumps(written) + "\n")

    report = rg.run_import()
    item = report["items"][0]
    assert item["corrections_applied"] == {"medium": "Charcoal on paper"}
    assert item["blanked_fields"] == ["date_display"]  # medium was corrected, date_display was not
    out = json.loads((tmp_path / "catalog" / "demo.json").read_text())["items"][125]
    assert out["medium"] == "Charcoal on paper"
    assert out["date_display"] == ""


def test_run_import_medium_doubtful_blanks_unverified_medium(tmp_path, monkeypatch):
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    (tmp_path / "written").mkdir()
    packet = {"title": "Sketch", "facts": [],
              "structured": {"medium": "Oil on canvas", "medium_source": "existing_catalog_value"}}
    (packets_dir / "sketch-0009.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", tmp_path / "written")
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", tmp_path / "catalog")
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"sketch-0009": "demo"}))
    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [dict(title="Sketch", agent_name="X", medium="Oil on canvas")] * 10})

    written = {"key": "sketch-0009", "title": "Sketch", "description_narrative": "A crayon sketch.",
               "tags": "sketch", "claims": [], "flags": ["medium_doubtful"]}
    (tmp_path / "written" / "batch_01.jsonl").write_text(json.dumps(written) + "\n")

    report = rg.run_import()
    assert report["medium_blanked"] == [{"key": "sketch-0009", "collection": "demo"}]
    out = json.loads((tmp_path / "catalog" / "demo.json").read_text())["items"][9]
    assert out["medium"] == ""


def test_run_import_medium_doubtful_ignored_when_medium_is_verified(tmp_path, monkeypatch):
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    (tmp_path / "written").mkdir()
    packet = {"title": "Sketch", "facts": [],
              "structured": {"medium": "Oil on canvas", "medium_source": "Met Collection API"}}
    (packets_dir / "sketch-0009.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", tmp_path / "written")
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", tmp_path / "catalog")
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"sketch-0009": "demo"}))
    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [dict(title="Sketch", agent_name="X", medium="Oil on canvas")] * 10})

    written = {"key": "sketch-0009", "title": "Sketch", "description_narrative": "A crayon sketch.",
               "tags": "sketch", "claims": [], "flags": ["medium_doubtful"]}
    (tmp_path / "written" / "batch_01.jsonl").write_text(json.dumps(written) + "\n")

    report = rg.run_import()
    assert report["medium_blanked"] == []
    out = json.loads((tmp_path / "catalog" / "demo.json").read_text())["items"][9]
    assert out["medium"] == "Oil on canvas"


def test_run_import_identity_mismatch_drops_museum_sourced_fields_too(tmp_path, monkeypatch):
    # addendum 2: hermit-thrush-0004 — the MATCH itself (Q64582791) was wrong, so every structured
    # field it backed (even a "verified" museum-sourced one) must be dropped, not just the
    # existing_catalog_value ones. Title + catalog artist are kept.
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    written_dir = tmp_path / "written"
    written_dir.mkdir()

    packet = {
        "title": "Hermit Thrush", "facts": [
            {"key": "wikidata.inception", "value": ["1820"], "source": "Wikidata",
             "source_url": "https://www.wikidata.org/wiki/Q64582791", "licence": "CC0"},
        ],
        "structured": {"medium": "Black chalk drawing", "medium_source": "National Gallery of Art API",
                        "date_display": "1820", "date_source": "National Gallery of Art API",
                        "current_repository": "National Gallery of Art"},
    }
    (packets_dir / "hermit-thrush-0004.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", written_dir)
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", tmp_path / "catalog")
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"hermit-thrush-0004": "demo"}))
    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [dict(
        title="Hermit Thrush", agent_name="John James Audubon",
        medium="Black chalk drawing", date_display="1820", current_repository="National Gallery of Art",
    )] * 5})

    written = {
        "key": "hermit-thrush-0004", "title": "Hermit Thrush",
        "description_narrative": "A hand-coloured engraving of a hermit thrush.", "tags": "bird",
        "claims": [], "flags": ["identity_mismatch"],
    }
    (written_dir / "batch_01.jsonl").write_text(json.dumps(written) + "\n")

    report = rg.run_import()
    entry = report["identity_suspect_fields_dropped"][0]
    assert entry["key"] == "hermit-thrush-0004"
    assert set(entry["fields_dropped"]) == {"medium", "date_display", "current_repository"}
    assert entry["match_qids"] == ["Q64582791"]
    out = json.loads((tmp_path / "catalog" / "demo.json").read_text())["items"][4]
    assert out["medium"] == "" and out["date_display"] == "" and out["current_repository"] == ""
    assert out["title"] == "Hermit Thrush" and out["agent_name"] == "John James Audubon"


def test_run_import_reports_existing_catalog_value_counts(tmp_path, monkeypatch):
    packets_dir = tmp_path / "packets" / "demo"
    packets_dir.mkdir(parents=True)
    (tmp_path / "written").mkdir()
    packet = {"title": "X", "facts": [],
              "structured": {"medium": "Oil on canvas", "medium_source": "existing_catalog_value"}}
    (packets_dir / "x-0000.json").write_text(json.dumps(packet))

    monkeypatch.setattr(rg, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(rg, "WRITTEN_DIR", tmp_path / "written")
    monkeypatch.setattr(rg, "OUT_CATALOG_DIR", tmp_path / "catalog")
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({"x-0000": "demo"}))
    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [dict(title="X", agent_name="Y")]})

    report = rg.run_import()
    assert report["existing_catalog_value_counts"] == {"medium": 1}


# --------------------------------------------------------------------------- duplicate-image detection
def test_identity_suspect_keys_loaded_from_import_report(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "IMPORT_REPORT_PATH", tmp_path / "import_report.json")
    assert rg._identity_suspect_keys_from_import_report() == set()  # no file yet -> empty, not an error

    (tmp_path / "import_report.json").write_text(json.dumps({
        "identity_suspect_fields_dropped": [
            {"key": "hermit-thrush-0004", "fields_dropped": ["medium"]},
            {"key": "other-key-0001", "fields_dropped": ["date_display"]},
        ],
    }))
    assert rg._identity_suspect_keys_from_import_report() == {"hermit-thrush-0004", "other-key-0001"}


def test_extract_match_qids_dedupes_and_ignores_non_wikidata_facts():
    packet = {"facts": [
        {"key": "wikidata.inception", "value": ["1820"], "source": "Wikidata", "source_url": "https://www.wikidata.org/wiki/Q64582791"},
        {"key": "wikidata.creator", "value": ["X"], "source": "Wikidata", "source_url": "https://www.wikidata.org/wiki/Q64582791"},
        {"key": "museum.medium", "value": "Oil", "source": "Met API", "source_url": "https://example.com/123"},
    ]}
    assert rg._extract_match_qids(packet) == ["Q64582791"]


def test_run_duplicate_images_groups_shared_preview_hashes(tmp_path, monkeypatch):
    previews_a = tmp_path / "previews" / "demo"
    previews_a.mkdir(parents=True)
    (previews_a / "garden-wall-0052.jpg").write_bytes(b"same-bytes")
    (previews_a / "garden-wall-0160.jpg").write_bytes(b"same-bytes")
    (previews_a / "unique-0003.jpg").write_bytes(b"different-bytes")

    monkeypatch.setattr(rg, "PREVIEWS_DIR", tmp_path / "previews")
    monkeypatch.setattr(rg, "ART_PACK_LIBRARY", tmp_path / "no_such_library")  # forces preview fallback
    monkeypatch.setattr(rg, "DUPLICATE_IMAGES_PATH", tmp_path / "duplicate_images.json")
    monkeypatch.setattr(rg, "PACKET_INDEX_PATH", tmp_path / "packets_index.json")
    (tmp_path / "packets_index.json").write_text(json.dumps({
        "garden-wall-0052": "demo", "garden-wall-0160": "demo", "unique-0003": "demo",
    }))
    monkeypatch.setattr(rg, "load_catalog", lambda: {"demo": [
        dict(title="Garden Wall", agent_name="A"), dict(title="Garden Wall", agent_name="B"),
        dict(title="Something Else", agent_name="C"),
    ]})
    rg._manifest_cache.clear()

    result = rg.run_duplicate_images()
    assert result["n_duplicate_groups"] == 1
    keys = {e["key"] for e in result["groups"][0]}
    assert keys == {"garden-wall-0052", "garden-wall-0160"}


# --------------------------------------------------------------------------- museum-match verification
def test_museum_match_rejects_unrelated_title():
    # audit finding: mount-washington-0007 (Homer painting) matched AIC's "Royal Flemish Vase" glass
    # record on a fuzzy title search — must be rejected outright, regardless of creator.
    item = {"title": "Mount Washington", "agent_name": "Winslow Homer"}
    record = {"title": "Royal Flemish Vase", "artist_display": "Mount Washington Glass Company"}
    ok, reason = rg.museum_match_verified(item, record)
    assert not ok and "similarity" in reason


def test_museum_match_accepts_matching_title_and_creator():
    item = {"title": "The Herring Net", "agent_name": "Winslow Homer"}
    record = {"title": "The Herring Net", "artistDisplayName": "Winslow Homer"}
    ok, reason = rg.museum_match_verified(item, record)
    assert ok


def test_museum_match_rejects_matching_title_without_creator_corroboration():
    item = {"title": "Landscape", "agent_name": "Winslow Homer"}
    record = {"title": "Landscape", "artistDisplayName": "Someone Else Entirely"}
    ok, reason = rg.museum_match_verified(item, record)
    assert not ok and "creator" in reason


def test_museum_match_explicit_id_skips_creator_requirement_but_not_title():
    item = {"title": "Fish and Rocks", "agent_name": "Bada Shanren"}
    record = {"title": "Fish and Rocks", "artistDisplayName": ""}
    ok, reason = rg.museum_match_verified(item, record, explicit_id=True)
    assert ok
    record_wrong_title = {"title": "Completely Different Work", "artistDisplayName": ""}
    ok2, reason2 = rg.museum_match_verified(item, record_wrong_title, explicit_id=True)
    assert not ok2 and "similarity" in reason2


# --------------------------------------------------------------------------- dimension unit conversion (round 6)
def test_wikidata_quantity_to_cm_converts_millimetres():
    dv = {"amount": "+1272", "unit": "http://www.wikidata.org/entity/Q174789"}  # mm
    assert rg.wikidata_quantity_to_cm(dv) == 127.2


def test_wikidata_quantity_to_cm_converts_metres():
    dv = {"amount": "+1.2", "unit": "http://www.wikidata.org/entity/Q11573"}  # m
    assert rg.wikidata_quantity_to_cm(dv) == 120.0


def test_wikidata_quantity_to_cm_converts_inches():
    dv = {"amount": "+10", "unit": "http://www.wikidata.org/entity/Q218593"}  # in
    assert rg.wikidata_quantity_to_cm(dv) == 25.4


def test_wikidata_quantity_to_cm_defaults_to_cm_when_unit_missing():
    dv = {"amount": "+45.5"}
    assert rg.wikidata_quantity_to_cm(dv) == 45.5


def test_wikidata_quantity_to_cm_strips_sign():
    dv = {"amount": "-30", "unit": "http://www.wikidata.org/entity/Q174728"}  # cm
    assert rg.wikidata_quantity_to_cm(dv) == 30.0


def test_dimension_plausible_rejects_out_of_range_for_ordinary_collections():
    assert not rg._dimension_plausible(1600.0, "watercolours")
    assert not rg._dimension_plausible(0.2, "watercolours")
    assert rg._dimension_plausible(45.0, "watercolours")


def test_dimension_plausible_allows_large_objects_for_exempt_collections():
    assert rg._dimension_plausible(3000.0, "cities-architecture")
    assert rg._dimension_plausible(3000.0, "cartography")


def test_physical_dimensions_drops_implausible_wikidata_value():
    # regression: boy-on-a-ram-0031's "+1272 x +1121 cm" — a raw mm amount already leaked past
    # wikidata_quantity_to_cm somehow (e.g. a bad source value) must still be caught by the guard.
    bundle = {
        "facts": [
            rg._fact("wikidata.height", ["1600.0"], "Wikidata", "u", "CC0"),
            rg._fact("wikidata.width", ["1400.0"], "Wikidata", "u", "CC0"),
        ],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {}, "watercolours")
    assert "physical_dimensions" not in fields
    assert any("implausible" in n for n in notes)


def test_physical_dimensions_accepts_plausible_wikidata_value():
    bundle = {
        "facts": [
            rg._fact("wikidata.height", ["45.0"], "Wikidata", "u", "CC0"),
            rg._fact("wikidata.width", ["35.0"], "Wikidata", "u", "CC0"),
        ],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {}, "watercolours")
    assert fields["physical_dimensions"] == "45.0 x 35.0 cm"
    assert fields["physical_dimensions_source"] == "Wikidata"


def test_physical_dimensions_prefers_museum_record_over_wikidata():
    bundle = {
        "facts": [
            rg._fact("wikidata.height", ["45.0"], "Wikidata", "u", "CC0"),
            rg._fact("wikidata.width", ["35.0"], "Wikidata", "u", "CC0"),
            rg._fact("museum.dimensions", "29.6 x 158.4 cm (11 5/8 x 62 3/8 in.)", "Cleveland Open Access API", "u", "CC0"),
        ],
        "conflicts": [], "is_version_of": None,
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {}, "asian-art")
    assert fields["physical_dimensions"] == "29.6 x 158.4 cm (11 5/8 x 62 3/8 in.)"
    assert fields["physical_dimensions_source"] == "Cleveland Open Access API"


# --------------------------------------------------------------------------- header hygiene (round 7)
def test_normalize_credit_text_strips_commons_featured_picture_boilerplate():
    raw = ("NASA, ESA, and the Hubble Heritage Team\n\n"
           "This is a featured picture on Wikimedia Commons (Featured pictures) and is considered "
           "one of the finest images. If you have an image of similar or higher quality, please "
           "nominate it here.")
    assert rg.normalize_credit_text(raw) == "NASA, ESA, and the Hubble Heritage Team"


def test_normalize_credit_text_strips_image_prefix_and_url_parenthetical():
    raw = ("Image: \n\nNational Aeronautics and Space Administration "
           "(a U.S. federal government agency; https://www.nasa.gov)")
    assert rg.normalize_credit_text(raw) == "NASA"


def test_normalize_credit_text_shortens_long_credit_to_leading_orgs():
    raw = "NASA, ESA, CSA, and STScI; image processing by a very long team of many named individuals here"
    out = rg.normalize_credit_text(raw, max_len=40)
    assert out == "NASA, ESA, CSA, STScI"


def test_normalize_credit_text_title_never_shortened_to_orgs_or_truncated():
    # title/current_repository get the same boilerplate/URL stripping but are never truncated —
    # only agent_name may be shortened when still too long after cleaning.
    raw = "A perfectly ordinary but somewhat long title that exceeds the eighty character maximum length limit"
    out = rg.normalize_credit_text(raw, shorten_to_orgs=False)
    assert out == raw


def test_normalize_credit_text_never_eats_a_titles_trailing_period():
    # regression: "Mrs. John Nicholson (Hannah Duncan) and John Nicholson, Jr." lost its final period
    # to the credit-text trailing-punctuation cleanup, which must only apply to agent_name.
    title = "Mrs. John Nicholson (Hannah Duncan) and John Nicholson, Jr."
    assert rg.normalize_credit_text(title, shorten_to_orgs=False) == title
    assert rg.normalize_credit_text("Portrait of Aechje Claesdr.", shorten_to_orgs=False) == "Portrait of Aechje Claesdr."


def test_normalize_credit_text_passthrough_for_clean_value():
    assert rg.normalize_credit_text("Winslow Homer") == "Winslow Homer"
    assert rg.normalize_credit_text("") == ""
    assert rg.normalize_credit_text(None) is None


# --------------------------------------------------------------------------- raw ISO date normalisation (round 7)
def test_normalize_iso_date_string_reduces_artwork_date_to_year():
    assert rg._normalize_iso_date_string("1889-07-14", "impressionism") == "1889"


def test_normalize_iso_date_string_keeps_day_precision_for_space_and_photo():
    assert rg._normalize_iso_date_string("1969-07-20", "earth-and-spaceflight") == "20 July 1969"


def test_normalize_iso_date_string_passthrough_non_iso_value():
    assert rg._normalize_iso_date_string("c. 1870s", "impressionism") == "c. 1870s"


def test_death_year_by_artist_name_falls_back_when_no_work_match(monkeypatch):
    rg._LABEL_CACHE.clear()
    fx = _FakeFetcher([
        ("wbsearchentities", {"search": [{"id": "Q296"}]}),
        ("\"ids\": \"Q296\"", {"entities": {"Q296": {"claims": {"P570": [
            {"mainsnak": {"datavalue": {"value": {"time": "+1926-12-05T00:00:00Z"}}}}
        ]}}}}),
    ])
    year = asyncio.run(rg._death_year_by_artist_name(fx, "Claude Monet"))
    assert year == 1926


def test_death_year_by_artist_name_returns_none_for_unknown_artist():
    fx = _FakeFetcher([])
    assert asyncio.run(rg._death_year_by_artist_name(fx, "Unknown Artist")) is None


def test_date_display_drops_existing_catalog_value_after_artist_death():
    # regression: "Farmyard in Normandy" catalogued "2024-04-10" — really a Commons upload date on a
    # 19th-century painting.
    bundle = {"facts": [], "conflicts": [], "is_version_of": None, "creator_death_year": 1875,
              "commons_upload_year": None}
    fields, needs_review, notes = rg.resolve_structured_fields(
        bundle, {"creation_date": "2024-04-10", "date_display": "2024-04-10"}, "impressionism")
    assert "date_display" not in fields
    assert needs_review is True
    assert any("upload/post-mortem" in n for n in notes)


def test_date_display_drops_existing_catalog_value_at_or_after_upload_year():
    bundle = {"facts": [], "conflicts": [], "is_version_of": None, "creator_death_year": None,
              "commons_upload_year": 2015}
    fields, needs_review, notes = rg.resolve_structured_fields(
        bundle, {"creation_date": "2020-01-01", "date_display": "2020-01-01"}, "impressionism")
    assert "date_display" not in fields
    assert needs_review is True


def test_date_display_accepts_plausible_existing_catalog_value():
    bundle = {"facts": [], "conflicts": [], "is_version_of": None, "creator_death_year": 1890,
              "commons_upload_year": 2015}
    fields, needs_review, notes = rg.resolve_structured_fields(
        bundle, {"creation_date": "1885-01-01", "date_display": "1885-01-01"}, "impressionism")
    assert fields["date_display"] == "1885"
    assert fields["date_source"] == "existing_catalog_value"


# --------------------------------------------------------------------------- round 8: aggregator blocklist
def test_is_aggregator_or_agency_flags_known_names():
    for name in ("Google Cultural Institute", "Google Art Project", "Bridgeman Art Library",
                 "Bridgeman Images", "Wikimedia Commons", "Flickr", "Project Apollo Archive",
                 "Art Renewal Center", "WikiArt", "Web Gallery of Art", "Yorck Project"):
        assert rg.is_aggregator_or_agency(name), name


def test_is_aggregator_or_agency_false_for_real_institutions():
    assert not rg.is_aggregator_or_agency("Yale Center for British Art")
    assert not rg.is_aggregator_or_agency("The Phillips Collection")


def test_extract_institution_phrase_rejects_aggregator():
    assert rg._extract_institution_phrase("via Google Cultural Institute") is None


def test_extract_institution_phrase_cleans_trailing_junk():
    assert rg._extract_institution_phrase("Cleveland Museum of Art. See the full record.") \
        == "Cleveland Museum of Art"
    assert rg._extract_institution_phrase("Library of Congress Catalog") == "Library of Congress"


def test_clean_institution_text_strips_leading_junk_phrases():
    assert rg._clean_institution_text("drawings in the Yale Center for British Art") \
        == "Yale Center for British Art"
    assert rg._clean_institution_text("photographs in the National Archives") == "National Archives"


def test_looks_like_institution_label_rejects_aggregator():
    assert not rg.looks_like_institution_label("Google Cultural Institute")


# --------------------------------------------------------------------------- round 8: title-only match gating
def test_medium_ignores_wikidata_when_match_is_title_only():
    bundle = {
        "facts": [rg._fact("wikidata.made_from_material", ["pastel"], "Wikidata", "u", "CC0")],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "medium"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert "medium" not in fields
    assert any("title-only match" in n for n in notes)


def test_medium_uses_wikidata_when_match_is_high_confidence():
    bundle = {
        "facts": [rg._fact("wikidata.made_from_material", ["pastel"], "Wikidata", "u", "CC0")],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert fields["medium"] == "pastel"


def test_current_repository_ignores_wikidata_collection_when_title_only():
    bundle = {
        "facts": [rg._fact("wikidata.collection", ["Metropolitan Museum of Art"], "Wikidata", "u", "CC0")],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "medium"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert "current_repository" not in fields


def test_dimensions_ignore_wikidata_when_title_only():
    bundle = {
        "facts": [
            rg._fact("wikidata.height", ["105.4"], "Wikidata", "u", "CC0"),
            rg._fact("wikidata.width", ["72.4"], "Wikidata", "u", "CC0"),
        ],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "medium"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert "physical_dimensions" not in fields


# --------------------------------------------------------------------------- round 8: dimension floor by medium
def test_dimension_plausible_uses_higher_floor_for_paintings():
    assert not rg._dimension_plausible(3.2, "post-impressionism", "Oil on canvas")
    assert rg._dimension_plausible(3.2, "post-impressionism", "Ink on paper")  # non-painting floor is lower
    assert rg._dimension_plausible(45.0, "post-impressionism", "Oil on canvas")


# --------------------------------------------------------------------------- round 8: date conflict precedence
def test_date_conflict_excludes_wikidata_prefers_commons():
    # toilers-of-the-sea-0125: Wikidata says 1847, Commons+museum agree on 1873 -> must never ship 1847.
    bundle = {
        "facts": [
            rg._fact("wikidata.inception", ["1847"], "Wikidata", "u", "CC0"),
            rg._fact("commons.DateTimeOriginal", "1873", "Wikimedia Commons", "u", "CC BY-SA"),
        ],
        "conflicts": [{"field": "date", "detail": "date spread 26 yrs"}], "is_version_of": None,
        "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert fields["date_display"] == "1873"
    assert any("ignored wikidata.inception" in n for n in notes)


def test_date_conflict_blanks_when_only_wikidata_available():
    bundle = {
        "facts": [rg._fact("wikidata.inception", ["1847"], "Wikidata", "u", "CC0")],
        "conflicts": [{"field": "date", "detail": "date spread 26 yrs"}], "is_version_of": None,
        "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert "date_display" not in fields


# --------------------------------------------------------------------------- round 8: photo-archive guard
def test_photo_archive_guard_rejects_loc_for_nonphoto_object():
    assert rg._is_photo_archive_of_a_nonphoto_object("Library of Congress", "Fresco")
    assert rg._is_photo_archive_of_a_nonphoto_object("Library of Congress", None)


def test_photo_archive_guard_allows_loc_for_a_photograph():
    assert not rg._is_photo_archive_of_a_nonphoto_object("Library of Congress", "Gelatin silver print")


def test_photo_archive_guard_ignores_non_archive_institutions():
    assert not rg._is_photo_archive_of_a_nonphoto_object("Yale Center for British Art", "Oil on canvas")


def test_current_repository_rejects_loc_credit_text_for_a_mural():
    bundle = {
        "facts": [rg._fact("museum.medium", "Fresco", "Met API", "u", "CC0"),
                  rg._fact("commons.institution_credit", "Library of Congress", "Wikimedia Commons", "u", "CC BY-SA")],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {})
    assert "current_repository" not in fields
    assert any("archive of the PHOTOGRAPH" in n for n in notes)


# --------------------------------------------------------------------------- round 8: commons structured QID override
def test_commons_structured_data_overrides_a_search_match():
    # house-in-provence-0130: search matched a Barnes painting (Q_WRONG); the Commons file's own
    # structured data (P6243/P180) links the real Indianapolis one (Q5914606) -> that must win.
    fx = _FakeFetcher([
        ("commonswiki", {"entities": {"M1": {"claims": {"P6243": [
            {"mainsnak": {"datavalue": {"value": {"id": "Q5914606"}}}}
        ]}}}}),
    ])
    item = {"source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/House%20in%20Provence.jpg"}
    match = {"qid": "Q_WRONG", "method": "wbsearchentities + creator-verified", "confidence": "medium"}
    new_match = asyncio.run(rg._apply_commons_structured_override(fx, item, match))
    assert new_match["qid"] == "Q5914606"
    assert new_match["confidence"] == "high"


def test_commons_structured_override_keeps_high_confidence_match_untouched():
    fx = _FakeFetcher([])
    item = {"source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg"}
    match = {"qid": "Q1", "method": "P18 exact commons-file match", "confidence": "high"}
    new_match = asyncio.run(rg._apply_commons_structured_override(fx, item, match))
    assert new_match == match


def test_commons_structured_override_noop_when_no_match():
    fx = _FakeFetcher([])
    item = {"source_url": "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg"}
    assert asyncio.run(rg._apply_commons_structured_override(fx, item, None)) is None


# --------------------------------------------------------------------------- round 8: attribution qualifier
def test_attribution_qualifier_detected_school_of():
    facts_by_key_source = [rg._fact("commons.ObjectName", "School of Raphael, Portrait", "Wikimedia Commons", "u", "CC BY-SA")]
    bundle = {
        "facts": [rg._fact("wikidata.creator", ["Raphael"], "Wikidata", "u", "CC0")] + facts_by_key_source,
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Raphael"})
    assert fields["agent_name_confirmed"] == "School of Raphael"
    assert needs_review is True


def test_attribution_qualifier_detected_german_schule_convention():
    bundle = {
        "facts": [
            rg._fact("wikidata.creator", ["Raphael"], "Wikidata", "u", "CC0"),
            rg._fact("commons.ObjectName", "Schule, Raffael", "Wikimedia Commons", "u", "CC BY-SA"),
        ],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Raphael"})
    assert fields["agent_name_confirmed"] == "School of Raffael"
    assert needs_review is True


def test_no_attribution_qualifier_keeps_plain_confirmed_name():
    bundle = {
        "facts": [rg._fact("wikidata.creator", ["Winslow Homer"], "Wikidata", "u", "CC0")],
        "conflicts": [], "is_version_of": None, "match": {"confidence": "high"},
    }
    fields, needs_review, notes = rg.resolve_structured_fields(bundle, {"agent_name": "Winslow Homer"})
    assert fields["agent_name_confirmed"] == "Winslow Homer"
    assert needs_review is False


# --------------------------------------------------------------------------- round 8: agent_name garbage
def test_looks_like_garbage_agent_name_flags_short_and_stopword_only():
    assert rg._looks_like_garbage_agent_name("and")
    assert rg._looks_like_garbage_agent_name("of")
    assert rg._looks_like_garbage_agent_name("NA")
    assert rg._looks_like_garbage_agent_name(", and")


def test_looks_like_garbage_agent_name_false_for_real_name():
    assert not rg._looks_like_garbage_agent_name("NASA")
    assert not rg._looks_like_garbage_agent_name("Rembrandt")


def test_normalize_credit_text_keeps_trailing_abbreviation_period_in_agent_name():
    # regression: round 7's trailing-punctuation cleanup ate this; round 8 fixes it for good.
    assert rg.normalize_credit_text("NASA/JPL-Caltech/Univ. of Ariz.") == "NASA/JPL-Caltech/Univ. of Ariz."


def test_medium_bucket_distinguishes_oil_from_watercolor_and_print():
    assert rg.medium_bucket("Oil on canvas") == "oil"
    assert rg.medium_bucket("Watercolor and gouache over graphite") == "watercolor"
    assert rg.medium_bucket("Color woodblock print") == "print"
    assert rg.medium_bucket("Bronze") == "sculpture_bronze"
    assert rg.medium_bucket("Marble") == "sculpture_marble"
    assert rg.medium_bucket("") is None


def test_visual_claim_exempts_capital_after_sentence_break():
    # Claims can hold two sentences; "She"/"Deep" opening the second sentence aren't proper nouns.
    assert rg._visual_claim_ok("A woman reads. She smiles.", "X", [])[0]
    assert rg._visual_claim_ok("A town at dusk. Deep blue water.", "X", [])[0]
    assert not rg._visual_claim_ok("A woman in Venetian dress.", "X", [])[0]
