"""tools/audit_licenses.py — the pack-ship gate (ADR-142 Stage A). Focus: BUNDLE_SAFE no longer
contradicts ADR-045 (no cc-by-sa), and audit_served_catalog()/--offline check every served row against
core.licensing.check_pack_row with no network."""

import json
import subprocess
import sys

from tools import audit_licenses


def test_bundle_safe_excludes_share_alike():
    """ADR-142 (amends ADR-045): packs may ship PD/CC0/CC-BY — never CC-BY-SA."""
    assert audit_licenses.BUNDLE_SAFE == {"pd", "cc-by"}
    assert "cc-by-sa" not in audit_licenses.BUNDLE_SAFE


def test_classify_still_buckets_verdicts():
    assert audit_licenses.classify("Public Domain") == "pd"
    assert audit_licenses.classify("CC0 1.0") == "pd"
    assert audit_licenses.classify("CC BY 4.0") == "cc-by"
    assert audit_licenses.classify("CC BY-SA 4.0") == "cc-by-sa"
    assert audit_licenses.classify("CC BY-NC 4.0") == "restricted"
    assert audit_licenses.classify(None) == "unknown"


def test_audit_served_catalog_clean_dir_passes(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "demo.json").write_text(json.dumps({
        "id": "demo", "title": "Demo", "items": [
            {"title": "A", "license": "PDM-1.0"},
            {"title": "B", "license": "CC-BY-4.0", "credit_line": "X",
             "license_url": "https://creativecommons.org/licenses/by/4.0/",
             "attribution_url": "https://example.org/b"},
        ]}))
    monkeypatch.setattr(audit_licenses, "CATALOG_DIR", catalog_dir)
    assert audit_licenses.audit_served_catalog() == []


def test_audit_served_catalog_flags_bad_rows(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "demo.json").write_text(json.dumps({
        "id": "demo", "title": "Demo", "items": [
            {"title": "Unmigrated", "license": "Public Domain"},          # free text, not an id
            {"title": "Incomplete CC BY", "license": "CC-BY-4.0"},        # missing attribution fields
        ]}))
    # skip index.json / _-prefixed files
    (catalog_dir / "_pack_pins.json").write_text(json.dumps({"collections": {}}))
    (catalog_dir / "index.json").write_text(json.dumps({"ignored": True}))
    monkeypatch.setattr(audit_licenses, "CATALOG_DIR", catalog_dir)

    failures = audit_licenses.audit_served_catalog()
    by_title = {title: problems for _cid, title, problems in failures}
    assert set(by_title) == {"Unmigrated", "Incomplete CC BY"}
    assert any("not pack-allowed" in p for p in by_title["Unmigrated"])
    assert len(by_title["Incomplete CC BY"]) == 3


def test_offline_cli_exits_clean_on_the_real_catalog():
    """No network, no --limit sampling noise — the real served catalog is all PD/CC0 today."""
    r = subprocess.run([sys.executable, "-m", "tools.audit_licenses", "--offline", "--strict"],
                        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_audit_served_catalog_flags_cc_by_sa_as_not_pack_allowed(tmp_path, monkeypatch):
    """main()'s --offline path is exercised end-to-end (clean) by the subprocess test above; here we
    exercise the same underlying check against a deliberately bad row without parsing sys.argv."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "demo.json").write_text(json.dumps({
        "id": "demo", "title": "Demo", "items": [{"title": "Bad", "license": "CC-BY-SA-4.0"}]}))
    monkeypatch.setattr(audit_licenses, "CATALOG_DIR", catalog_dir)
    failures = audit_licenses.audit_served_catalog()
    assert failures and failures[0][1] == "Bad"
