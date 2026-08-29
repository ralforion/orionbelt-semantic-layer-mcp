"""Guards for the third-party license report generator.

The Docker image runs `scripts/gen_third_party_licenses.py` at build time, so a
regression there surfaces only during the release build. These tests catch it in
the normal suite instead.
"""

import importlib.util
import sys
from importlib import metadata
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_third_party_licenses.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_third_party_licenses", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_module()


def test_collect_excludes_the_project_itself():
    names = {d.metadata.get("Name", "").lower().replace("_", "-") for d in gen.collect()}
    assert "orionbelt-semantic-layer-mcp" not in names
    # The declared runtime dependencies are always present in any test env.
    assert {"httpx", "fastmcp", "pydantic-settings"} <= names


def test_render_produces_a_summary_and_a_block_per_package():
    dists = gen.collect()
    report = gen.render(dists, "9.9.9")

    assert report.startswith("OrionBelt Semantic Layer MCP — Third-Party Licenses")
    assert "for orionbelt-semantic-layer-mcp 9.9.9" in report
    assert f"SUMMARY ({len(dists)} packages)" in report

    for dist in dists:
        name = dist.metadata.get("Name")
        assert f"{name} {dist.version}" in report, f"no block for {name}"


def test_every_package_reports_a_license():
    """No entry may fall through to a bare 'UNKNOWN'."""
    report = gen.render(gen.collect(), "0.0.0")
    summary = report.split("SUMMARY", 1)[1].split("\n\n", 2)[1]
    unknown = [line.strip() for line in summary.splitlines() if line.endswith("UNKNOWN")]
    assert not unknown, f"packages with no identifiable license: {unknown}"


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        ("                     Apache License\n              Version 2.0", "Apache-2.0"),
        ("MIT License\n\nCopyright (c)", "MIT"),
        ("Mozilla Public License Version 2.0", "MPL-2.0"),
        ("Permission is hereby granted, free of charge, to any person", "MIT"),
        ("GNU GENERAL PUBLIC LICENSE\nVersion 3", "GPL"),
        ("some vendor's bespoke terms", ""),
    ],
)
def test_detect_license_from_text(head, expected):
    assert gen._detect_license([("LICENSE", head)]) == expected


def test_render_records_the_resolution_target_when_given_one():
    dists = gen.collect()
    note = "x86_64-unknown-linux-gnu / CPython 3.14 (the Docker image target)"
    assert f"Resolved for {note}" in gen.render(dists, "1.2.3", note)
    # Omitted when not supplied, rather than printed empty.
    assert "Resolved for" not in gen.render(dists, "1.2.3")


def test_render_scopes_itself_to_python_packages():
    """The report must not read as if it covered the base image's OS packages."""
    report = gen.render(gen.collect(), "1.2.3")
    assert "SCOPE: Python packages only." in report
    assert "/usr/share/doc/<package>/copyright" in report


def test_collect_can_scan_an_arbitrary_install_tree(tmp_path):
    """`--path` drives the cross-target report the shell wrapper generates."""
    assert gen.collect([str(tmp_path)]) == []

    dist_info = tmp_path / "widget-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: widget\nVersion: 1.0\nLicense-Expression: MIT\n"
    )
    (dist_info / "RECORD").write_text("widget-1.0.dist-info/METADATA,,\n")

    found = gen.collect([str(tmp_path)])
    assert [d.metadata.get("Name") for d in found] == ["widget"]
    assert "widget 1.0" in gen.render(found, "1.2.3")


def test_license_text_is_carried_for_a_known_package():
    """httpx ships a BSD license file; it must reach the report verbatim."""
    dist = metadata.distribution("httpx")
    texts = gen._license_texts(dist)
    assert texts, "httpx shipped no license text"
    assert "Copyright" in "\n".join(text for _, text in texts)
