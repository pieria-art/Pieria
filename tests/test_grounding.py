"""core/grounding.py (ADR-148 runtime grounding) + its curator.enrich_artwork wiring.
No real network (respx) and no real model (ai_client.chat is stubbed)."""

import json

import httpx
import pytest
import respx

import ai_client
import curator
from core import grounding
from models import ArtworkModel

WD = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
LEAD = ("Test Painting is a watercolor by Winslow Homer depicting a boy fishing at a quiet pond "
        "in the summer light.")

MET_HINTS = json.dumps({
    "accessionNumber": "1940.116", "title": "Test Painting", "artistDisplayName": "Winslow Homer",
    "objectDate": "1873", "medium": "Watercolor over graphite", "dimensions": "10 x 12 in.",
    "culture": "American", "objectURL": "https://www.metmuseum.org/art/collection/search/1",
})


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(grounding, "_SLEEP", _no_sleep)
    monkeypatch.setattr(grounding, "_MIN_INTERVAL_S", 0.0)


def _entity_claims():
    def item(qid):
        return {"mainsnak": {"datavalue": {"value": {"id": qid}}}}
    return {
        "P170": [item("Q2")], "P186": [item("Q3")], "P195": [item("Q4")],
        "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1873-00-00T00:00:00Z", "precision": 9}}}}],
    }


def _wikidata_router(accession_qid="Q1", down=False, lead=LEAD):
    """One handler for every Wikimedia endpoint the grounding code touches."""
    seen = {"sparql": [], "wp": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            return httpx.Response(503)
        url, q = str(request.url), request.url.params
        if url.startswith(SPARQL):
            seen["sparql"].append(q.get("query", ""))
            hit = 'P217 "1940.116"' in q.get("query", "")
            rows = [{"item": {"value": f"http://www.wikidata.org/entity/{accession_qid}"}}] if hit else []
            return httpx.Response(200, json={"results": {"bindings": rows}})
        if url.startswith(WD):
            ids = (q.get("ids") or "").split("|")
            if q.get("props") == "labels":
                labs = {"Q2": "Winslow Homer", "Q3": "watercolor", "Q4": "Metropolitan Museum of Art"}
                return httpx.Response(200, json={"entities": {
                    i: {"labels": {"en": {"value": labs.get(i, i)}}} for i in ids}})
            if q.get("props") == "claims|sitelinks":
                return httpx.Response(200, json={"entities": {ids[0]: {
                    "claims": _entity_claims(), "sitelinks": {"enwiki": {"title": "Test Painting"}}}}})
            return httpx.Response(200, json={"entities": {}})
        if "wikipedia.org" in url:
            seen["wp"] += 1
            return httpx.Response(200, json={"extract": lead})
        return httpx.Response(404)

    return handler, seen


def _chat_stub(replies):
    calls = []

    def chat(role, messages, json_mode=False, **kw):
        content = messages[0]["content"]
        calls.append(content[0]["text"] if isinstance(content, list) else content)
        return json.dumps(replies[min(len(calls) - 1, len(replies) - 1)])
    chat.calls = calls
    return chat


GOOD = {
    "description_narrative": "A watercolor by Winslow Homer, made in 1873.",
    "tags": "watercolor, homer",
    "claims": [{"text": "made in 1873", "fact_keys": ["wikidata.inception"]},
               {"text": "by Winslow Homer", "fact_keys": ["museum.creators"]}],
}


async def _ground(monkeypatch, replies, router=None, hints=MET_HINTS, **kw):
    handler, seen = router or _wikidata_router()
    chat = _chat_stub(replies)
    monkeypatch.setattr(ai_client, "chat", chat)
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            res = await grounding.ground_work(
                title="Test Painting", artist="Winslow Homer", hints=hints,
                source_api="The Metropolitan Museum of Art", client=client, **kw)
    return res, chat, seen


# ------------------------------------------------------------------ identity
@pytest.mark.asyncio
async def test_identity_resolves_museum_accession_to_wikidata_qid():
    handler, seen = _wikidata_router()
    rec = grounding.museum_record_from_hints(MET_HINTS, "The Metropolitan Museum of Art")
    assert rec["accession"] == "1940.116" and rec["institution"].startswith("The Metropolitan")
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(side_effect=handler)
        async with httpx.AsyncClient() as client:
            fx = grounding.RuntimeFetcher(client)
            match = await grounding.resolve_identity(fx, title="Test Painting", artist="Winslow Homer", rec=rec)
    assert match == {"qid": "Q1", "method": "museum accession -> Wikidata P217/P195", "confidence": "high"}
    assert 'P217 "1940.116"' in seen["sparql"][0] and "metropolitan" in seen["sparql"][0]


def test_museum_record_normalisation_for_aic_and_cleveland():
    aic = grounding.museum_record_from_hints({"id": 7, "main_reference_number": "1900.1", "title": "T",
                                              "artist_title": "A", "date_display": "1890",
                                              "medium_display": "Oil on canvas"}, "Art Institute of Chicago")
    assert (aic["accession"], aic["medium"], aic["inst_keyword"]) == ("1900.1", "Oil on canvas", "chicago")
    cma = grounding.museum_record_from_hints({"accession_number": "1916.1", "technique": "oil",
                                              "creators": [{"description": "Monet (French)"}]}, None)
    assert (cma["accession"], cma["artist"]) == ("1916.1", "Monet (French)")
    assert grounding.museum_record_from_hints("not json", None) is None


# ------------------------------------------------------------------ structured fields come from records
@pytest.mark.asyncio
async def test_structured_fields_come_from_records_not_the_model(monkeypatch):
    lying = dict(GOOD, medium="Oil on canvas", date_display="1999", current_repository="Louvre",
                 physical_dimensions="1 x 1 m", cultural_context="French", agent_name="Someone Else")
    res, chat, _ = await _ground(monkeypatch, [lying])
    assert res and not res["used_fallback"]
    f = res["fields"]
    assert f["medium"] == "Watercolor over graphite"          # museum record, not the model
    assert f["date_display"] == "1873" and f["current_repository"] == "The Metropolitan Museum of Art"
    assert f["physical_dimensions"] == "10 x 12 in." and f["cultural_context"] == "American"
    assert f["agent_name"] == "Winslow Homer"
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_accession_number_never_becomes_the_date(monkeypatch):
    hints = json.dumps(dict(json.loads(MET_HINTS), objectDate="1940.116"))
    res, _, _ = await _ground(monkeypatch, [GOOD], hints=hints)
    assert res["fields"]["date_display"] == "1873"            # fell through to Wikidata inception


# ------------------------------------------------------------------ prose validation, retry, fallback
@pytest.mark.asyncio
async def test_unsupported_claim_is_rejected_retried_then_falls_back_to_minimal_placard(monkeypatch):
    bad = {"description_narrative": "A watercolor by Winslow Homer, painted in 1999.",
           "tags": "x", "claims": [{"text": "painted in 1999", "fact_keys": ["wikidata.inception"]}]}
    res, chat, _ = await _ground(monkeypatch, [bad, bad])
    assert len(chat.calls) == 2                               # exactly one retry
    assert "REJECTED" in chat.calls[1] and "1999" in chat.calls[1]   # violations listed to the model
    assert res["used_fallback"] and res["violations"]
    text = res["description_narrative"]
    assert "1999" not in text and "Test Painting" in text and "Watercolor over graphite" in text
    assert "Metropolitan Museum of Art" in text               # built from records only


@pytest.mark.asyncio
async def test_retry_with_violations_can_recover(monkeypatch):
    bad = {"description_narrative": "A watercolor from 1899.", "claims": [
        {"text": "from 1899", "fact_keys": ["wikidata.inception"]}]}
    res, chat, _ = await _ground(monkeypatch, [bad, GOOD])
    assert len(chat.calls) == 2 and not res["used_fallback"]
    assert "1873" in res["description_narrative"]


@pytest.mark.asyncio
async def test_prose_naming_a_different_medium_than_the_record_is_rejected(monkeypatch):
    oil = {"description_narrative": "An oil painting by Winslow Homer.", "claims": [
        {"text": "by Winslow Homer", "fact_keys": ["museum.creators"]}]}
    res, chat, _ = await _ground(monkeypatch, [oil, oil])
    assert res["used_fallback"] and len(chat.calls) == 2


# ------------------------------------------------------------------ Wikipedia lead is check-only (CC BY-SA)
@pytest.mark.asyncio
async def test_wikipedia_lead_is_check_only_and_never_copied(monkeypatch):
    copied = {"description_narrative": "Test Painting is a watercolor by Winslow Homer depicting a boy "
                                       "fishing at a quiet pond.",
              "claims": [{"text": "by Winslow Homer", "fact_keys": ["museum.creators"]}]}
    res, chat, seen = await _ground(monkeypatch, [copied, copied])
    assert seen["wp"] == 1
    assert res["used_fallback"]                               # long verbatim run -> rejected twice
    assert any("check-only" in v for v in res["violations"])
    # the lead reached the model only as background context, never as a citable fact
    assert "Background text (context only" in chat.calls[0] and "a boy fishing at a quiet pond" in chat.calls[0]
    assert "[wikipedia" not in chat.calls[0].lower()
    paraphrase = dict(GOOD, description_narrative="A Homer watercolor of a young angler by a pond, made in 1873.")
    ok, _, _ = await _ground(monkeypatch, [paraphrase])
    assert not ok["used_fallback"]


# ------------------------------------------------------------------ degrade to today's behaviour
@pytest.mark.asyncio
async def test_wikidata_down_without_a_record_returns_none_for_the_legacy_path(monkeypatch):
    res, chat, _ = await _ground(monkeypatch, [GOOD], router=_wikidata_router(down=True), hints=None)
    assert res is None and chat.calls == []                   # nothing to ground on -> no model call


@pytest.mark.asyncio
async def test_wikidata_down_with_a_museum_record_still_grounds_on_the_record_alone(monkeypatch):
    good = {"description_narrative": "A watercolor by Winslow Homer.", "tags": "homer",
            "claims": [{"text": "by Winslow Homer", "fact_keys": ["museum.creators"]}]}
    res, _, _ = await _ground(monkeypatch, [good], router=_wikidata_router(down=True))
    assert res and res["match"] is None and res["fields"]["medium"] == "Watercolor over graphite"


@pytest.mark.asyncio
async def test_fetcher_backs_off_on_429_and_sends_a_descriptive_user_agent(monkeypatch):
    sleeps = []

    async def rec_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(grounding, "_SLEEP", rec_sleep)
    with respx.mock() as m:
        route = m.get("https://www.wikidata.org/x").mock(side_effect=[
            httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json={"ok": 1})])
        async with httpx.AsyncClient(headers={"User-Agent": grounding.SD_USER_AGENT}) as client:
            body, err = await grounding.RuntimeFetcher(client, min_interval=0).get_json(
                "https://www.wikidata.org/x")
        ua = route.calls[0].request.headers["user-agent"]
    assert body == {"ok": 1} and err is None and 3.0 in sleeps and "Pieria" in ua


# ------------------------------------------------------------------ curator wiring
def _artwork(db, **kw):
    a = ArtworkModel(filename="", title="Test Painting", agent_name="Winslow Homer",
                     source_url="https://example.org/img.jpg", status="processing", **kw)
    db.add(a)
    db.commit()
    return a


@pytest.mark.asyncio
async def test_curator_applies_grounded_fields_and_respects_user_edits(testing_session, monkeypatch):
    handler, _ = _wikidata_router()
    monkeypatch.setattr(ai_client, "chat", _chat_stub([dict(GOOD, medium="Oil on canvas")]))
    art = _artwork(testing_session, user_edited_fields=json.dumps(["cultural_context"]),
                   cultural_context="Mine")
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(side_effect=handler)
        out = await curator.enrich_artwork(art.id, testing_session, context_hints=MET_HINTS,
                                           source_api="The Metropolitan Museum of Art")
    assert out.medium == "Watercolor over graphite"           # record beat the model's "Oil on canvas"
    assert out.cultural_context == "Mine"                     # F8: the user's edit wins
    assert out.description_narrative.startswith("A watercolor by Winslow Homer")
    assert out.status == "pending_review"


@pytest.mark.asyncio
async def test_curator_falls_back_to_todays_path_when_nothing_to_ground_on(testing_session, monkeypatch):
    handler, _ = _wikidata_router(down=True)
    monkeypatch.setattr(curator.wikipedia, "summary", lambda *a, **k: "Some summary.")
    legacy = {"title": "Test Painting", "description_narrative": "Legacy blurb.", "tags": ["a"]}
    monkeypatch.setattr(ai_client, "chat", _chat_stub([legacy]))
    art = _artwork(testing_session)
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(side_effect=handler)
        out = await curator.enrich_artwork(art.id, testing_session, context_hints=None)
    assert out.description_narrative == "Legacy blurb."


@pytest.mark.asyncio
async def test_personal_photos_skip_grounding_and_the_museum_pipeline(testing_session, monkeypatch):
    async def boom(**kw):
        raise AssertionError("personal photo reached the grounding pipeline")

    def no_wiki(*a, **k):
        raise AssertionError("personal photo reached the Wikipedia legacy path")
    monkeypatch.setattr(grounding, "ground_work", boom)
    monkeypatch.setattr(curator.wikipedia, "summary", no_wiki)
    art = _artwork(testing_session, is_personal=True, description_narrative="My dog.")
    out = await curator.enrich_artwork(art.id, testing_session, context_hints=MET_HINTS)
    assert out.description_narrative == "My dog." and out.is_personal


@pytest.mark.asyncio
async def test_image_is_decoded_once_and_off_the_event_loop(testing_session, monkeypatch, tmp_path):
    import asyncio
    import threading

    import config
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path)
    (tmp_path / "x.jpg").write_bytes(b"not really an image")
    main_thread = threading.get_ident()
    calls = []

    def fake_image_part(path, *a, **k):
        calls.append(threading.get_ident())
        return {"type": "image_url", "image_url": {"url": "data:,x"}}
    monkeypatch.setattr(ai_client, "image_part", fake_image_part)
    monkeypatch.setattr(curator.wikipedia, "summary", lambda *a, **k: "Some summary.")
    legacy = {"title": "Test Painting", "description_narrative": "Legacy blurb.", "tags": ["a"]}
    monkeypatch.setattr(ai_client, "chat", _chat_stub([legacy]))
    handler, _ = _wikidata_router(down=True)       # grounding returns None -> legacy path reuses the part
    art = _artwork(testing_session)
    art.filename = "x.jpg"
    testing_session.commit()
    with respx.mock(assert_all_called=False) as m:
        m.route().mock(side_effect=handler)
        await asyncio.wait_for(curator.enrich_artwork(art.id, testing_session, context_hints=None), 10)
    assert len(calls) == 1 and calls[0] != main_thread
