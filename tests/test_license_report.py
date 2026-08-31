"""Guards for the third-party notice.

The image copies THIRD_PARTY_NOTICES.md rather than generating it, and CI
regenerates it from the locked set to prove the committed copy is current. These
tests cover what that check cannot: that the file says true things about its own
scope, and that the classifier behind it puts licences in the right bucket.

Deliberately no network and no resolved environment — they read the committed
notice and call pure functions, so they run in the ordinary suite.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTICE = ROOT / "THIRD_PARTY_NOTICES.md"


def _load():
    spec = importlib.util.spec_from_file_location(
        "third_party_notices", ROOT / "scripts" / "third_party_notices.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notices = _load()


@pytest.mark.parametrize(
    ("expression", "verdict"),
    [
        ("MIT", "permissive"),
        ("Apache-2.0", "permissive"),
        ("BSD License", "permissive"),
        ("Apache Software License", "permissive"),
        ("Mozilla Public License 2.0 (MPL 2.0)", "weak-copyleft"),
        ("GPL-3.0-only", "strong-copyleft"),
        # A choice that includes the GPL reads as strong until somebody elects a
        # branch in writing — the weaker option is not ours until then.
        ("Public Domain OR BSD License OR GNU General Public License (GPL)", "strong-copyleft"),
        ("", "unknown"),
        ("Other/Proprietary License", "unknown"),
    ],
)
def test_classify_buckets_the_licences_it_is_given(expression, verdict):
    assert notices.classify(expression) == verdict


def test_no_package_falls_through_to_unknown():
    """The old suite asserted this of the report; here the gate enforces it.

    An unrecognised licence is not merely cosmetic — it fails enforce_policy, so
    a package can never reach the notice with its terms unaccounted for.
    """
    package = notices.Package(
        name="mystery",
        version="1.0",
        license_expression="",
        verdict="unknown",
        texts=(),
        in_image=True,
    )
    assert notices.enforce_policy([package])


def test_acknowledgements_are_bound_to_the_licence_that_was_reviewed():
    """Relicensing must re-enter the gate rather than inherit an old approval."""
    entry = notices.ACKNOWLEDGED.get("certifi")
    assert isinstance(entry, notices.Acknowledgement), "certifi must be reviewed, not Pending"
    ok = notices.Package(
        name="certifi",
        version="1.0",
        license_expression=entry.license_expression,
        verdict="weak-copyleft",
        texts=(),
        in_image=True,
    )
    assert notices.enforce_policy([ok]) == []

    relicensed = notices.Package(
        name="certifi",
        version="1.0",
        license_expression="GPL-3.0-only",
        verdict="strong-copyleft",
        texts=(),
        in_image=True,
    )
    failures = notices.enforce_policy([relicensed])
    assert [kind for kind, _ in failures] == ["mismatch"]


@pytest.mark.skipif(not NOTICE.exists(), reason="notice not generated in this tree")
def test_the_notice_scopes_itself_to_python_packages():
    """It must not read as though it covered the base image's OS packages."""
    text = NOTICE.read_text(encoding="utf-8")
    assert "uv.lock" in text
    assert "Platform layer" in text, "the OS layer must be called out as separate"
    assert "/usr/share/doc" in text, "Debian's own copyright files must be pointed at"


@pytest.mark.skipif(not NOTICE.exists(), reason="notice not generated in this tree")
def test_the_notice_excludes_the_project_itself():
    """Our own package is covered by LICENSE, not by a third-party notice."""
    text = NOTICE.read_text(encoding="utf-8")
    assert f"### {notices.ROOT_PACKAGE} " not in text


@pytest.mark.skipif(not NOTICE.exists(), reason="notice not generated in this tree")
def test_a_known_licence_text_reaches_the_notice_verbatim():
    """httpx ships a BSD licence file; it must arrive intact, not summarised."""
    text = NOTICE.read_text(encoding="utf-8")
    assert "### httpx " in text
    assert "Redistributions of source code must retain the above copyright" in text
