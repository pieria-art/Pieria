"""Unit tests for tools.clean_titles — the verbose-title normalizer (served + pack-source catalogs)."""
import json

import pytest

from tools.clean_titles import clean_title, process_file


@pytest.mark.parametrize("raw, title, series", [
    # HTML entity decode
    ("Young 2 &amp; 3", "Young 2 & 3", None),
    # artifact suffix strip
    ("Western Blue-bird (cropped)", "Western Blue-bird", None),
    ("A Study (Detail)", "A Study", None),
    # Audubon plate boilerplate → species (plain, and the [i.e. …] editorial correction)
    ("The birds of America. [Livraison] 25, Snowy Owl, Male 1. Female 2. : [estampe] / Drawn from Nature",
     "Snowy Owl", None),
    ("The birds of America. [Livraison] 20, Mottled Owl, Adult 1. Young 2 &amp; 3. "
     "[i.e. Eastern Screech-Owl] ; Vulgo Jersey Pine : [estampe] / Drawn", "Eastern Screech-Owl", None),
    # ukiyo-e "also known as … from the series" — lift series, keep the canonical work name
    ('Under the Wave off Kanagawa (Kanagawa oki nami ura), also known as The Great Wave, '
     'from the series "Thirty-Six Views of Mount Fuji (Fugaku sanjūrokkei)"',
     "Under the Wave off Kanagawa", "Thirty-Six Views of Mount Fuji (Fugaku sanjūrokkei)"),
    # ukiyo-e trailing "(from Series)" tail
    ("Evening Cherry Blossoms at Gotenyama (from Famous Places in the Eastern Capital)",
     "Evening Cherry Blossoms at Gotenyama", "Famous Places in the Eastern Capital"),
    # numbered-plate OCR fix (Roman I → Arabic 1), multi-species list preserved
    ("I. Townsend's Warbler - 2. Arctic Blue-bird - 3. Western Blue-bird (cropped)",
     "1. Townsend's Warbler - 2. Arctic Blue-bird - 3. Western Blue-bird", None),
    # …and a SOLITARY plate (no numbered list) still gets the Roman I → Arabic 1 fix
    ("I. Mourning Warbler", "1. Mourning Warbler", None),
    # …but a genuine Roman-numeral outline (a later "II.") is left intact
    ("I. Prologue II. Finale", "I. Prologue II. Finale", None),
    # fully-quoted title unwrapped; a lone internal quote is NOT stripped
    ('"Snap-the-Whip"', "Snap-the-Whip", None),
    # untouched titles pass through unchanged (no false positives)
    ("Mona Lisa", "Mona Lisa", None),
    ("Still Life with Apples", "Still Life with Apples", None),
])
def test_clean_title_rules(raw, title, series):
    assert clean_title(raw) == (title, series)


def test_clean_title_is_idempotent():
    raw = "Evening Cherry Blossoms at Gotenyama (from Famous Places in the Eastern Capital)"
    once_title, _ = clean_title(raw)
    assert clean_title(once_title) == (once_title, None)


def test_lone_quote_not_unbalanced():
    # a single stray quote must never be stripped into an unbalanced string
    assert clean_title('The Poet Says "Hello') == ('The Poet Says "Hello', None)


def test_process_file_lifts_series_next_to_title(tmp_path):
    f = tmp_path / "ukiyo-e.json"
    f.write_text(json.dumps({"id": "ukiyo-e", "items": [
        {"title": "Foo (from Bar Series)", "agent_name": "Hiroshige"},
        {"title": "Mona Lisa", "agent_name": "Leonardo"},
    ]}, ensure_ascii=False))
    changes = process_file(f, write=True)
    assert len(changes) == 1
    doc = json.loads(f.read_text())
    it = doc["items"][0]
    assert it["title"] == "Foo" and it["series"] == "Bar Series"
    assert list(it) == ["title", "series", "agent_name"]        # series inserted right after title
    assert doc["items"][1] == {"title": "Mona Lisa", "agent_name": "Leonardo"}   # untouched
    assert process_file(f, write=True) == []                    # idempotent second pass


def test_process_file_bare_list_shape(tmp_path):
    f = tmp_path / "bare.json"
    f.write_text(json.dumps([{"title": "Egg (cropped)"}], ensure_ascii=False))
    assert len(process_file(f, write=True)) == 1
    assert json.loads(f.read_text())[0]["title"] == "Egg"
