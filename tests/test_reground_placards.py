"""Unit tests for tools/reground_placards.py — no network, no model (both mocked)."""

import asyncio
import json

import ai_client
from tools import reground_placards as rg


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


def test_medium_bucket_distinguishes_oil_from_watercolor_and_print():
    assert rg.medium_bucket("Oil on canvas") == "oil"
    assert rg.medium_bucket("Watercolor and gouache over graphite") == "watercolor"
    assert rg.medium_bucket("Color woodblock print") == "print"
    assert rg.medium_bucket("Bronze") == "sculpture_bronze"
    assert rg.medium_bucket("Marble") == "sculpture_marble"
    assert rg.medium_bucket("") is None
