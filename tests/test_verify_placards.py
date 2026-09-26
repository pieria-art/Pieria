"""Tests for tools/verify_placards.py — fake chat fn only, no network (conftest blocks real sockets)."""
from __future__ import annotations

import json

import pytest

from tools import verify_placards as vp


# --------------------------------------------------------------------------------------- fixtures
def _write_catalog(tmp_path, collection, items):
    d = tmp_path / "catalog"
    d.mkdir(exist_ok=True)
    (d / f"{collection}.json").write_text(json.dumps({"items": items}))
    return d


def _write_packet(tmp_path, collection, key, *, title, agent_name="", facts=None, is_version_of=None):
    d = tmp_path / "packets" / collection
    d.mkdir(parents=True, exist_ok=True)
    packet = {
        "key": key,
        "collection": collection,
        "title": title,
        "catalog": {"title": title, "agent_name": agent_name},
        "facts": facts or [],
        "is_version_of": is_version_of,
        "structured": {},
    }
    (d / f"{key}.json").write_text(json.dumps(packet))
    return packet


def _item(title, **kw):
    base = {
        "title": title,
        "agent_name": "",
        "date_display": "",
        "medium": "",
        "current_repository": "",
        "physical_dimensions": "",
        "description_narrative": "A narrative.",
    }
    base.update(kw)
    return base


class FakeChat:
    """Injectable chat_fn stand-in. `responses` is a list of strings/None consumed in order per call;
    each entry may be an Exception instance to raise instead."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, messages, *, max_tokens, temperature, extra=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "extra": extra})
        resp = self.responses.pop(0)
        if isinstance(resp, tuple):
            return resp
        return resp, None


GOOD_VERDICT = json.dumps({
    "identity": "same",
    "identity_reason": "facts match the header",
    "header_issues": [],
    "narrative_issues": [],
    "severity": "none",
})


# ------------------------------------------------------------------------------------------- parsing
def test_parse_verdict_unfenced():
    verdict, err = vp.parse_verdict(GOOD_VERDICT)
    assert err is None
    assert verdict["identity"] == "same"


def test_parse_verdict_fenced():
    fenced = f"```json\n{GOOD_VERDICT}\n```"
    verdict, err = vp.parse_verdict(fenced)
    assert err is None
    assert verdict["severity"] == "none"


def test_parse_verdict_invalid():
    verdict, err = vp.parse_verdict("not json at all")
    assert verdict is None
    assert err is not None


@pytest.mark.asyncio
async def test_judge_one_retries_once_on_invalid_json_then_succeeds():
    chat = FakeChat(["nope not json", GOOD_VERDICT])
    verdict, raw, err = await vp.judge_one(chat, "cartography", {"title": "X"}, "narrative", [])
    assert verdict["identity"] == "same"
    assert raw is None
    assert err is None
    assert len(chat.calls) == 2


@pytest.mark.asyncio
async def test_judge_one_records_parse_failure_after_retry():
    chat = FakeChat(["still not json", "also not json"])
    verdict, raw, err = await vp.judge_one(chat, "cartography", {"title": "X"}, "narrative", [])
    assert verdict is None
    assert raw == "also not json"
    assert "invalid JSON" in err


@pytest.mark.asyncio
async def test_judge_one_propagates_transport_error():
    chat = FakeChat([(None, "local-llm: timed out")])
    verdict, raw, err = await vp.judge_one(chat, "cartography", {"title": "X"}, "narrative", [])
    assert verdict is None
    assert raw is None
    assert err == "local-llm: timed out"


# ------------------------------------------------------------------------------------------- matching
def test_packet_index_matches_by_title_and_agent(tmp_path):
    _write_packet(tmp_path, "cartography", "map-0000", title="A New Map", agent_name="John Speed")
    idx = vp.PacketIndex(tmp_path / "packets")
    path, reason = idx.find("cartography", "A New Map", "John Speed")
    assert reason == "title+agent"
    assert path is not None


def test_packet_index_survives_dropped_item_index_shift(tmp_path):
    # Simulate the import dropping item 0001: the surviving packet keys jump from -0000 to -0002,
    # which would break any lookup keyed on positional index. Matching is by title, so it's unaffected.
    _write_packet(tmp_path, "cartography", "map-a-0000", title="Map A")
    _write_packet(tmp_path, "cartography", "map-c-0002", title="Map C")
    idx = vp.PacketIndex(tmp_path / "packets")
    path, reason = idx.find("cartography", "Map C", "")
    assert reason in ("title+agent", "title-only")
    assert path.name == "map-c-0002.json"


def test_packet_index_falls_back_to_title_only_when_unique(tmp_path):
    _write_packet(tmp_path, "cartography", "map-0000", title="Unique Title", agent_name="Some Agent")
    idx = vp.PacketIndex(tmp_path / "packets")
    # catalog's agent_name differs (e.g. casing/typo drift) — title alone is unique, so still resolves.
    path, reason = idx.find("cartography", "Unique Title", "Different Agent Spelling")
    assert reason == "title-only"
    assert path is not None


def test_packet_index_ambiguous_title_is_skipped(tmp_path):
    _write_packet(tmp_path, "cartography", "map-0000", title="Same Title", agent_name="Agent One")
    _write_packet(tmp_path, "cartography", "map-0001", title="Same Title", agent_name="Agent Two")
    idx = vp.PacketIndex(tmp_path / "packets")
    path, reason = idx.find("cartography", "Same Title", "Agent Three")
    assert path is None
    assert reason == "ambiguous"


def test_packet_index_unmatched(tmp_path):
    idx = vp.PacketIndex(tmp_path / "packets")
    path, reason = idx.find("cartography", "Nothing Here", "")
    assert path is None
    assert reason == "unmatched"


# --------------------------------------------------------------------------- round 13 (D): exact drop mapping
def test_item_key_matches_slug_plus_zero_padded_index():
    assert vp.item_key(0, "New Map") == "new-map-0000"
    assert vp.item_key(232, "Mandarin Duck") == "mandarin-duck-0232"


def test_pre_drop_index_no_drops_is_identity():
    assert vp.pre_drop_index(0, []) == 0
    assert vp.pre_drop_index(5, []) == 5


def test_pre_drop_index_single_drop_before_target():
    # pre-drop array 0..9, index 2 removed: post 2 -> pre 3, post 4 -> pre 6... (only one drop here)
    assert vp.pre_drop_index(0, [2]) == 0
    assert vp.pre_drop_index(2, [2]) == 3
    assert vp.pre_drop_index(3, [2]) == 4


def test_pre_drop_index_multiple_drops_in_one_collection():
    # pre-drop indices 2 and 5 removed.
    assert vp.pre_drop_index(0, [2, 5]) == 0
    assert vp.pre_drop_index(1, [2, 5]) == 1
    assert vp.pre_drop_index(2, [2, 5]) == 3
    assert vp.pre_drop_index(3, [2, 5]) == 4
    assert vp.pre_drop_index(4, [2, 5]) == 6
    assert vp.pre_drop_index(5, [2, 5]) == 7


def test_pre_drop_index_drops_before_and_after_target():
    # dropped indices straddle the target from both sides.
    assert vp.pre_drop_index(1, [0, 3, 7]) == 2
    assert vp.pre_drop_index(6, [0, 3, 7]) == 9


def test_load_deferred_drops_groups_by_collection_and_sorts(tmp_path):
    data = [
        {"key": "aristotle-with-a-bust-of-homer-0063", "collection": "dutch-golden-age"},
        {"key": "two-mandarin-ducks-0189", "collection": "ukiyo-e"},  # gitleaks:allow (catalog key, not a secret)
        {"key": "mandarin-duck-in-snow-0138", "collection": "ukiyo-e"},
    ]
    p = tmp_path / "deferred_drops.json"
    p.write_text(json.dumps(data))
    m = vp.load_deferred_drops(p)
    assert m["dutch-golden-age"] == [63]
    assert m["ukiyo-e"] == [138, 189]


@pytest.mark.asyncio
async def test_run_uses_exact_key_mapping_for_same_titled_works_when_drops_given(tmp_path):
    # Four same-titled works in one collection (like Cézanne's four "Bathers") must each resolve to
    # their OWN packet, not collapse onto one by title matching, once a drops file maps the indices.
    catalog_dir = _write_catalog(tmp_path, "post-impressionism", [
        _item("Bathers", agent_name="Paul Cézanne"),
        _item("Bathers", agent_name="Paul Cézanne"),
        _item("Bathers", agent_name="Paul Cézanne"),
        _item("Bathers", agent_name="Paul Cézanne"),
    ])
    for i in range(4):
        _write_packet(tmp_path, "post-impressionism", f"bathers-{i:04d}", title="Bathers", agent_name="Paul Cézanne")
    drops_path = tmp_path / "deferred_drops.json"
    drops_path.write_text(json.dumps([]))  # no drops in this collection — index-aligned
    output_dir = tmp_path / "verify"

    chat = FakeChat([GOOD_VERDICT] * 4)
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, concurrency=2, drops=drops_path)

    assert result["attempted"] == 4
    for i in range(4):
        assert (output_dir / "post-impressionism" / f"bathers-{i:04d}.json").exists()


@pytest.mark.asyncio
async def test_run_exact_key_mapping_accounts_for_a_drop_before_the_target(tmp_path):
    # Pre-drop there were 3 items (0,1,2); item 1 was dropped, so post-drop item 1 is really pre-drop 2.
    catalog_dir = _write_catalog(tmp_path, "cartography", [
        _item("Map A", agent_name="John Speed"),
        _item("Map C", agent_name="John Speed"),
    ])
    _write_packet(tmp_path, "cartography", "map-a-0000", title="Map A", agent_name="John Speed")
    _write_packet(tmp_path, "cartography", "map-c-0002", title="Map C", agent_name="John Speed")
    drops_path = tmp_path / "deferred_drops.json"
    drops_path.write_text(json.dumps([
        {"key": "map-b-0001", "collection": "cartography"},
    ]))
    output_dir = tmp_path / "verify"

    chat = FakeChat([GOOD_VERDICT] * 2)
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, concurrency=2, drops=drops_path)

    assert result["attempted"] == 2
    assert (output_dir / "cartography" / "map-a-0000.json").exists()
    assert (output_dir / "cartography" / "map-c-0002.json").exists()


@pytest.mark.asyncio
async def test_run_exact_key_mapping_reports_unmatched_when_packet_missing(tmp_path):
    catalog_dir = _write_catalog(tmp_path, "cartography", [_item("Orphan Map", agent_name="Agent")])
    drops_path = tmp_path / "deferred_drops.json"
    drops_path.write_text(json.dumps([]))
    output_dir = tmp_path / "verify"

    chat = FakeChat([])
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, concurrency=2, drops=drops_path)

    assert result["attempted"] == 0
    assert len(result["unmatched"]) == 1
    assert "exact-key-missing" in result["unmatched"][0]["reason"]


# ----------------------------------------------------------------------------------------- run/resume
@pytest.mark.asyncio
async def test_run_writes_output_and_records_unmatched(tmp_path):
    catalog_dir = _write_catalog(tmp_path, "cartography", [
        _item("Matched Work", agent_name="John Speed"),
        _item("Orphan Work"),
    ])
    _write_packet(tmp_path, "cartography", "matched-work-0000", title="Matched Work", agent_name="John Speed")
    output_dir = tmp_path / "verify"

    chat = FakeChat([GOOD_VERDICT])
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, concurrency=2)

    assert result["attempted"] == 1
    assert len(result["unmatched"]) == 1
    assert result["unmatched"][0]["title"] == "Orphan Work"

    out_file = output_dir / "cartography" / "matched-work-0000.json"
    assert out_file.exists()
    rec = json.loads(out_file.read_text())
    assert rec["verdict"]["identity"] == "same"
    assert rec["key"] == "matched-work-0000"


@pytest.mark.asyncio
async def test_run_is_resumable_skips_existing_output(tmp_path):
    catalog_dir = _write_catalog(tmp_path, "cartography", [_item("Matched Work", agent_name="John Speed")])
    _write_packet(tmp_path, "cartography", "matched-work-0000", title="Matched Work", agent_name="John Speed")
    output_dir = tmp_path / "verify"
    output_dir_coll = output_dir / "cartography"
    output_dir_coll.mkdir(parents=True)
    (output_dir_coll / "matched-work-0000.json").write_text(json.dumps({"already": "done"}))

    chat = FakeChat([])  # must not be called
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, concurrency=2)

    assert result["attempted"] == 0
    assert result["skipped_already_done"] == 1
    assert len(chat.calls) == 0


@pytest.mark.asyncio
async def test_run_respects_limit(tmp_path):
    items = [_item(f"Work {i}", agent_name="Agent") for i in range(5)]
    catalog_dir = _write_catalog(tmp_path, "cartography", items)
    for i in range(5):
        _write_packet(tmp_path, "cartography", f"work-{i}-0000{i}", title=f"Work {i}", agent_name="Agent")
    output_dir = tmp_path / "verify"

    chat = FakeChat([GOOD_VERDICT] * 2)
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, limit=2, concurrency=2)
    assert result["attempted"] == 2


@pytest.mark.asyncio
async def test_run_sample_filter_restricts_to_listed_titles(tmp_path):
    catalog_dir = _write_catalog(tmp_path, "cartography", [
        _item("Work A", agent_name="Agent"),
        _item("Work B", agent_name="Agent"),
    ])
    _write_packet(tmp_path, "cartography", "work-a-0000", title="Work A", agent_name="Agent")
    _write_packet(tmp_path, "cartography", "work-b-0001", title="Work B", agent_name="Agent")
    sample_file = tmp_path / "sample.json"
    sample_file.write_text(json.dumps([{"collection": "cartography", "title": "Work B"}]))
    output_dir = tmp_path / "verify"

    chat = FakeChat([GOOD_VERDICT])
    result = await vp.run(catalog_dir, tmp_path / "packets", output_dir, chat, sample=sample_file, concurrency=2)
    assert result["attempted"] == 1
    assert (output_dir / "cartography" / "work-b-0001.json").exists()
    assert not (output_dir / "cartography" / "work-a-0000.json").exists()


# ------------------------------------------------------------------------------------------- summary
def test_summarize_aggregates_and_writes_flagged(tmp_path):
    output_dir = tmp_path / "verify"
    (output_dir / "cartography").mkdir(parents=True)
    (output_dir / "cartography" / "a.json").write_text(json.dumps({
        "collection": "cartography", "key": "a",
        "verdict": {"identity": "same", "severity": "none"},
    }))
    (output_dir / "cartography" / "b.json").write_text(json.dumps({
        "collection": "cartography", "key": "b",
        "verdict": {"identity": "different_object", "severity": "major"},
    }))
    (output_dir / "cartography" / "c.json").write_text(json.dumps({
        "collection": "cartography", "key": "c",
        "verdict": {"identity": "same", "severity": "minor"},
    }))

    summary = vp.summarize(output_dir)
    assert summary["total"] == 3
    assert summary["by_severity"]["major"] == 1
    assert summary["by_severity"]["minor"] == 1
    assert summary["by_severity"]["none"] == 1
    assert summary["by_identity"]["different_object"] == 1
    assert summary["flagged_count"] == 2

    flagged = json.loads((output_dir / "flagged.json").read_text())
    assert len(flagged) == 2
    assert flagged[0]["verdict"]["severity"] == "major"  # major sorts first
