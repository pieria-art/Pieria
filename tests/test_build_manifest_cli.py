"""tools/build_manifest CLI — CSV → (optionally signed) Manifest v2; fail-closed on invalid input."""

import json

import federation
import publisher
from manifest_validator import validate_manifest
from tools import build_manifest

CSV_HEADER = "image_url,title,artist,license,tags,focal_x,focal_y,width,height\n"
CSV_ROW = "https://cdn.jane.test/a.jpg,Sunrise,Monet,CC0-1.0,sea|dawn,0.3,0.6,2400,1600\n"


def _meta_file(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text(json.dumps({"slug": "janes-art", "title": "Jane's Art",
                             "publisher": {"id": "jane", "name": "Jane Doe"}}))
    return p


def test_cli_builds_valid_unsigned_manifest(tmp_path):
    csv = tmp_path / "items.csv"; csv.write_text(CSV_HEADER + CSV_ROW)
    out = tmp_path / "manifest.json"
    rc = build_manifest.main(["--csv", str(csv), "--meta", str(_meta_file(tmp_path)), "--out", str(out)])
    assert rc == 0
    m = json.loads(out.read_text())
    assert validate_manifest(m) == []
    assert m["items"][0]["image"]["focal_point"] == [0.3, 0.6]
    assert "signature" not in m       # unsigned without --key


def test_cli_signs_with_key(tmp_path):
    csv = tmp_path / "items.csv"; csv.write_text(CSV_HEADER + CSV_ROW)
    out = tmp_path / "manifest.json"
    priv, pub = publisher.keygen()
    rc = build_manifest.main(["--csv", str(csv), "--meta", str(_meta_file(tmp_path)),
                              "--out", str(out), "--key", priv])
    assert rc == 0
    m = json.loads(out.read_text())
    assert federation.verify_signature(m) is True


def test_cli_meta_flags_override(tmp_path):
    csv = tmp_path / "items.csv"; csv.write_text(CSV_HEADER + CSV_ROW)
    out = tmp_path / "m.json"
    rc = build_manifest.main(["--csv", str(csv), "--slug", "flagslug", "--title", "Flag Title",
                              "--publisher-id", "jane", "--publisher-name", "Jane", "--out", str(out)])
    assert rc == 0
    m = json.loads(out.read_text())
    assert m["id"] == "flagslug" and m["title"] == "Flag Title"


def test_cli_fails_closed_on_invalid(tmp_path):
    # CC-BY without attribution → invalid → exit non-zero, nothing written
    csv = tmp_path / "items.csv"
    csv.write_text("image_url,title,license\nhttps://x/y.jpg,Bad,CC-BY-4.0\n")
    out = tmp_path / "should_not_exist.json"
    rc = build_manifest.main(["--csv", str(csv), "--meta", str(_meta_file(tmp_path)), "--out", str(out)])
    assert rc == 1 and not out.exists()


def test_cli_requires_slug_and_title(tmp_path):
    csv = tmp_path / "items.csv"; csv.write_text(CSV_HEADER + CSV_ROW)
    rc = build_manifest.main(["--csv", str(csv), "--out", str(tmp_path / "m.json")])
    assert rc == 2
