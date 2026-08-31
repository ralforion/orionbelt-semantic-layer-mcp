#!/usr/bin/env python
"""Generate (or verify) THIRD_PARTY_NOTICES.md for the redistributed dependencies.

The runner's wheel ships only ``src/orionbelt_runner`` — installers fetch the
dependencies themselves, so a wheel carries no third-party attribution duty. The
**Docker image does**: it copies a fully populated ``.venv``, which redistributes
every runtime package as a binary, and MIT / BSD / Apache-2.0 / MPL all require
their notice to travel with that binary.

Rather than trust that, this script derives the notice file from ``uv.lock`` (the
same resolution the image is built from) and the license texts each wheel
installs into ``*.dist-info/licenses/``.

    uv sync --locked --extra dev                                  # `dev` pulls in
                                                                  # pyarrow + weasyprint,
                                                                  # so the whole closure
                                                                  # is importable
    uv run --no-sync python scripts/third_party_notices.py         # write the file
    uv run --no-sync python scripts/third_party_notices.py --check # CI: policy + drift

Where ``COMMIT_NOTICE`` is False the notice is not part of the tree and
``--check`` is the licence gate alone.

``--check`` fails when the file is stale *and* when a dependency introduces a
license that is not permissive and not explicitly acknowledged below — the point
being that a new copyleft dependency has to be a decision someone made, not one
that arrives with a lockfile bump.
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import re
import sys
import textwrap
import tomllib
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any, Literal

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "uv.lock"
NOTICES_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
DOCKERFILE_PATH = ROOT / "Dockerfile"
OVERRIDE_DIR = Path(__file__).resolve().parent / "license-overrides"

# Shared machinery the per-repo config below refers to. Identical in every
# ralforion repo — see the banner that follows.

_BASE_ENV = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
}


def supported_pythons() -> list[str]:
    """The CPython minors this project claims, from pyproject's `requires-python`.

    Derived rather than listed because the two must agree and only one of them is
    checked by anything else: a project that raises its floor to 3.13 and leaves a
    hardcoded 3.12 here keeps crediting a dependency reachable only on the version
    it no longer supports. The upper bound is the newest release this script knows
    about, since `>=3.12` names no ceiling of its own.
    """
    spec = SpecifierSet(
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "requires-python"
        ]
    )
    known = [f"3.{minor}" for minor in range(8, 15)]
    supported = [v for v in known if spec.contains(f"{v}.0")]
    if not supported:
        raise SystemExit(
            f"error: no known CPython minor satisfies requires-python {str(spec)!r}. "
            f"If a newer release exists, widen the range in supported_pythons()."
        )
    return supported


PUBLISH_ENVIRONMENTS = [
    {**_BASE_ENV, "python_version": v, "python_full_version": f"{v}.0"} for v in supported_pythons()
]

Verdict = Literal["permissive", "weak-copyleft", "strong-copyleft", "unknown"]

# Normalised license tokens that need no further thought. Keyed on the SPDX id
# where a package declares one, plus the classifier spellings of the same
# licenses for packages still using the old `Classifier: License ::` form.
PERMISSIVE = {
    "0BSD",
    "APACHE-2.0",
    "BSD",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "HPND",
    "ISC",
    "MIT",
    "MIT-0",
    "MIT-CMU",
    "PSF-2.0",
    "PYTHON-2.0",
    "UNLICENSE",
    "ZLIB",
    # Seen only in compound expressions so far: numpy's bundled CC0 components,
    # regex's CNRI-Python half, simplejson's AFL alternative. Each is permissive
    # and none carries a reciprocal obligation, so a package is not escalated for
    # containing one.
    "AFL-2.1",
    "CC0-1.0",
    "CNRI-PYTHON",
}

# Spellings seen in the wild -> the token above. These are not cosmetic: an
# unrecognised spelling lands in "unknown" and fails the policy gate, so a
# package declaring "Apache 2.0" instead of "Apache-2.0" would otherwise demand a
# written acknowledgement for a license already on the permissive list. Every
# entry here was observed on a real dependency of one of these projects.
LICENSE_ALIASES = {
    "APACHE SOFTWARE LICENSE": "APACHE-2.0",
    "APACHE LICENSE, VERSION 2.0": "APACHE-2.0",
    "APACHE LICENSE VERSION 2.0": "APACHE-2.0",
    "APACHE LICENSE 2.0": "APACHE-2.0",
    "APACHE 2.0": "APACHE-2.0",
    "3-CLAUSE BSD LICENSE": "BSD-3-CLAUSE",
    "THE MIT LICENSE (MIT)": "MIT",
    "MIT LICENSE (MIT)": "MIT",
    "THE MIT LICENSE": "MIT",
    "THE UNLICENSE (UNLICENSE)": "UNLICENSE",
    "THE UNLICENSE": "UNLICENSE",
    "BSD LICENSE": "BSD",
    "BSD-2-CLAUSE LICENSE": "BSD-2-CLAUSE",
    "BSD-3-CLAUSE LICENSE": "BSD-3-CLAUSE",
    "ISC LICENSE (ISCL)": "ISC",
    "ISC LICENSE": "ISC",
    "MIT LICENSE": "MIT",
    "MIT NO ATTRIBUTION": "MIT-0",
    "PYTHON SOFTWARE FOUNDATION LICENSE": "PSF-2.0",
    "MOZILLA PUBLIC LICENSE 1.1 (MPL 1.1)": "MPL-1.1",
    "MOZILLA PUBLIC LICENSE 2.0 (MPL 2.0)": "MPL-2.0",
    "GNU GENERAL PUBLIC LICENSE V2 OR LATER (GPLV2+)": "GPL-2.0-OR-LATER",
    "GNU LESSER GENERAL PUBLIC LICENSE V2 OR LATER (LGPLV2+)": "LGPL-2.1-OR-LATER",
}


# pyphen offers a choice of three licenses (see _pyphen_note), and the image ships
# it, so one of them has to be chosen. RALFORION elects the LGPL: pyphen is used
# unmodified as an ordinary installed package that the runner imports, which is
# the case the LGPL is written for, and it is the election a licence audit expects
# to see. The GPL option is never available to us — it would reach the runner's
# own code. Set back to None only if the image stops installing the `pdf` extra.
@dataclass(frozen=True)
class Acknowledgement:
    """A non-permissive license we looked at and accepted, and why.

    `license_expression` is what was reviewed, verbatim. The exemption is bound to
    it rather than to the package name: a package that relicenses — certifi moving
    off MPL-2.0, say — must come back through the gate instead of inheriting an
    approval granted to different terms.
    """

    license_expression: str
    note: str


@dataclass(frozen=True)
class Pending:
    """A non-permissive license that has been *seen* but not yet decided on.

    The difference from simply leaving the package out of ACKNOWLEDGED is what the
    error says, not whether there is one — both fail. An absent entry reports a
    package nobody has looked at; a Pending reports one somebody looked at, could
    not clear, and wrote down. That distinction is the whole reason this exists:
    rolling this script out across several repositories surfaces a backlog at
    once, and without somewhere to record "seen, still open" the only ways to get
    a green build are to decide everything immediately or to acknowledge things
    nobody reviewed. Never let one of these ship: it fails the gate by design, and
    it is not a way to silence the check.
    """

    reason: str


# ═══════════════════════════════════════════════════════════════════════════════
# PER-REPO CONFIGURATION
#
# This script is vendored into every ralforion repository. Everything from here
# down to the END banner describes *this* repo; everything below that banner is
# identical in all of them and is synced wholesale, so a fix belongs there and a
# local edit to it will be overwritten.
#
# Deliberately vendored rather than packaged: the notice is a licence document,
# and the code that generated it should be reviewable in the same diff as its
# output, not resolved from an index at build time.
# ═══════════════════════════════════════════════════════════════════════════════

# Display name, and the import package inside the wheel.
PROJECT_NAME = "OrionBelt Semantic Layer MCP"
ROOT_PACKAGE = "orionbelt-semantic-layer-mcp"
MODULE_NAME = "server"

# Whether a wheel and sdist are published. Gates the paragraph about them: a
# project that publishes neither would otherwise assert something about artifacts
# that do not exist.
PUBLISHES_TO_PYPI = True

# A container image is published, so its dependencies really are handed over as
# binaries and the notice is an obligation rather than documentation. Both stay
# True together here: the image is what makes the file owed, and the file is what
# discharges it.
PUBLISHES_IMAGE = True
COMMIT_NOTICE = True

# Extras this file accounts for. Not the same question as what gets redistributed:
# the image installs only what the Dockerfile asks for (see dockerfile_extras), and
# packages reachable only through an extra it skips are listed as informational —
# a user can install them, but pip fetches them from PyPI, not from us. `dev` is in
# neither set: pytest, ruff and mypy are tools the project runs, not works it ships.
#
# Empty here: this project defines no optional-dependency groups, so its runtime
# closure and its redistributed closure are the same set.
NOTICED_EXTRAS: tuple[str, ...] = ()

# Build arguments whose value decides what the image installs, where the Dockerfile's
# own `ARG` default is not the whole answer. Empty here: the Dockerfile's `uv sync`
# takes no `--extra`, so there is nothing to resolve.
IMAGE_BUILD_ARGS: dict[str, str] = {}

# Libraries the platform notice names as dynamically linked. Repo-specific because
# it is a factual claim about what this image binds at runtime, and the LGPL
# argument in PLATFORM_SECTION rests on it.
PLATFORM_LINKED_LIBS = "glibc, libffi,\n  libssl, libsqlite3, liblzma and readline"

# No pyphen in this closure — nothing to elect. Kept at None so the shared guard
# below stays inert rather than absent, and starts working the moment a `pdf`-style
# extra pulls pyphen in.
PYPHEN_ELECTION: str | None = None


ACKNOWLEDGED: dict[str, Acknowledgement | Pending] = {
    "certifi": Acknowledgement(
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-2.0 is file-level copyleft: it reaches the files themselves, not the "
        "program that imports them. We ship certifi unmodified as a separate "
        "installed package, so the obligation is satisfied by shipping this notice "
        "and its license text. Do not patch certifi in place — patch it and the "
        "modified files must be published under MPL-2.0.",
    ),
    "caio": Pending(
        "declares no license at all in its wheel metadata, so there is nothing to "
        "classify. Its repository states MIT; confirm that against the released "
        "version and record it here, or drop the dependency"
    ),
    "docutils": Pending(
        "offers Public Domain OR BSD OR GPL, and distributing it means choosing one "
        "in writing the way PYPHEN_ELECTION does elsewhere. The GPL option is never "
        "available to us — it would reach this project's own code — so the decision "
        "is between the public-domain and BSD branches"
    ),
}

# ═══════════════════════════════════════════════════════════════════════════════
# END PER-REPO CONFIGURATION — everything below is shared across ralforion repos
# ═══════════════════════════════════════════════════════════════════════════════


# Attribution for what the image carries below the Python layer. uv.lock cannot
# see any of it, so this part is prose — kept here rather than in the Markdown so
# the whole notice file stays generated, and parameterised on the Dockerfile so a
# base-image bump cannot leave a stale interpreter version behind.
PLATFORM_SECTION = r"""## Platform layer (Docker image)

`uv.lock` knows only PyPI packages, so the list above stops at the Python layer.
The published image is built `FROM {{IMAGE}}` and therefore also
redistributes a CPython interpreter and a Debian userland. Neither is vendored, patched, or
rebuilt — the base image is used exactly as published.

**CPython** is under the Python Software Foundation License Agreement, Version 2:
permissive, no copyleft, nothing to elect. Its full text — including the
historical BeOpen, CNRI and CWI terms that Python inherits — ships inside the
image at `/usr/local/lib/python{{PYVER}}/LICENSE.txt`. PSF-2.0 §3 would require a
summary of changes made to Python; this project makes none.

**The Debian userland** is a mix of permissive, LGPL and GPL packages. Debian
ships every package's terms at `/usr/share/doc/<package>/copyright`, so that
attribution travels inside the image alongside the binaries it covers. Two points
on the copyleft there:

- Nothing GPL is *linked*. The libraries bound at runtime — glibc, libffi,
  {{LINKEDLIBS}} — are LGPL or permissive, and all are
  linked dynamically, which is the case the LGPL permits. (A Debian `copyright`
  file frequently mentions the GPL because a build script or a sibling binary in
  the same source package is GPL'd; glibc is the standard example, an LGPL
  library shipped next to GPL'd tools like `ldd`.)
- The genuinely GPL'd components are the OS utilities — `bash`, `coreutils`,
  `dpkg`, `apt`, `tar`, `sed`, `util-linux` and friends. They are *programs*
  sitting beside the application in the same filesystem, not code linked into or
  imported by it: mere aggregation under GPL-2 §2. They do not reach
  `{{MODULE}}`, which stays under its own license.

**Corresponding source.** Debian keeps a permanent, timestamped archive of every
package version it has ever published at <https://snapshot.debian.org>. The base
image is pinned when the image is built, so the exact source that produced every
OS binary in a given image stays retrievable there. To enumerate what an image
contains and pull the matching sources:

```bash
docker run --rm <image> dpkg-query -W -f='${source:Package} ${source:Version}\n'
# then `apt-get source <package>=<version>` against the snapshot.debian.org
# suite for that image's build date
```
"""


def render_platform_section() -> str:
    """PLATFORM_SECTION with every placeholder filled in.

    A function rather than an expression inside render() so that the test asserting
    no placeholder survives is checking the same substitution the notice is built
    from. When the two were separate, adding a placeholder here left the test
    passing against its own stale copy — which is how an unreplaced `{{LINKEDLIBS}}`
    reached a generated file.
    """
    image, python_version = runtime_base_image()
    return (
        PLATFORM_SECTION.replace("{{IMAGE}}", image)
        .replace("{{PYVER}}", python_version)
        .replace("{{LINKEDLIBS}}", PLATFORM_LINKED_LIBS)
        .replace("{{MODULE}}", MODULE_NAME)
    )


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    license_expression: str
    verdict: Verdict
    texts: tuple[tuple[str, str], ...]  # (filename, contents)
    in_image: bool  # redistributed by the Docker image, vs reachable via an extra
    # Declares Apache-2.0 but ships no license file. Its terms are Appendix A —
    # see the no-text branch of collect() for why that is a reproduction of this
    # package's license and not a guess about it.
    apache_by_declaration: bool = False


def runtime_base_image() -> tuple[str, str]:
    """The Dockerfile's runtime base image, as (image, python minor).

    Read rather than hardcoded: the platform notice names the interpreter version
    and the path its license sits at, and a base-image bump must not be able to
    leave either of those saying something untrue.

    The runtime stage is the *last* `FROM python:` in the file, which is what
    Docker itself builds when no `--target` is given. Keying on a stage named
    `runtime` would have been narrower than the fact it stands for: sibling repos
    name theirs `base`, or leave the final stage unnamed, and in both cases the
    interpreter shipped is still the one on that last line. Stages built `FROM`
    something other than `python:` (an uv binary stage, say) are skipped rather
    than refused — they contribute no interpreter to describe.
    """
    matches = re.findall(
        r"^FROM\s+(python:(\d+\.\d+)[^\s]*)",
        DOCKERFILE_PATH.read_text(encoding="utf-8"),
        re.IGNORECASE | re.MULTILINE,
    )
    if not matches:
        raise SystemExit(
            f"could not find a `FROM python:<version>` line in {DOCKERFILE_PATH.name} — "
            f"the platform-layer notice names the interpreter it ships and is derived "
            f"from that line"
        )
    image, python_version = matches[-1]
    return image, python_version


def is_first_party(package: dict[str, Any]) -> bool:
    """Whether a locked package is our own source rather than a dependency.

    uv records a workspace member as `source = {editable = "drivers/..."}` and a
    package fetched from an index as `source = {registry = "..."}`, so the two are
    told apart by how they were resolved rather than by a hand-kept list that
    would go stale the moment a member is added.

    They are excluded because this file credits *third parties*. A workspace
    member is covered by the repository's own LICENSE, and listing it here would
    claim its terms had been reviewed as an external dependency's — which for a
    sibling repo means printing BUSL-1.1, the project's own license, in a
    third-party notice as though someone had granted it to us.
    """
    source = package.get("source") or {}
    return not any(key in source for key in ("registry", "url"))


def normalize(name: str) -> str:
    """PEP 503 name normalisation — `ruamel.yaml` and `ruamel-yaml` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def marker_applies(marker: str | None) -> bool:
    if marker is None:
        return True
    parsed = Marker(marker)
    return any(parsed.evaluate(environment=env) for env in PUBLISH_ENVIRONMENTS)


def distributed_closure(lock: dict[str, Any], extras: Sequence[str]) -> dict[str, str]:
    """Walk uv.lock from the root package to every dependency we redistribute.

    Returns {normalised name: version}. Extras are followed as their own edges so
    `fonttools[woff]` pulls brotli while a plain `fonttools` would not.
    """
    packages = {normalize(p["name"]): p for p in lock["package"]}
    found: dict[str, str] = {}
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def walk(name: str, extras: tuple[str, ...]) -> None:
        key = (normalize(name), tuple(sorted(extras)))
        if key in seen:
            return
        seen.add(key)
        package = packages.get(normalize(name))
        if package is None:
            raise SystemExit(f"{name} is required but missing from uv.lock")
        # A project with `dynamic = ["version"]` has no `version` in uv.lock. That
        # is only ever true of the root — a locked third-party dependency always
        # carries one — and the root's is deleted below anyway, so tolerate it
        # there and treat it as corruption anywhere else rather than crediting a
        # package whose version the notice cannot state.
        version = package.get("version")
        if version is None:
            if normalize(name) != normalize(ROOT_PACKAGE):
                raise SystemExit(
                    f"error: {name} has no version in uv.lock. The notice has to name "
                    f"the version of every package it credits — re-lock, and if it "
                    f"persists this lockfile cannot be described accurately."
                )
            version = ""
        found[normalize(name)] = version
        edges = list(package.get("dependencies", []))
        for extra in extras:
            edges += package.get("optional-dependencies", {}).get(extra, [])
        for edge in edges:
            if marker_applies(edge.get("marker")):
                walk(edge["name"], tuple(edge.get("extra", ())))

    walk(ROOT_PACKAGE, tuple(extras))
    del found[normalize(ROOT_PACKAGE)]  # our own license is LICENSE, not a notice
    return found


# GPL but not LGPL: the lookbehind is what keeps "LGPL-2.1" out of the strong
# bucket, and it has to survive both "GPL-3.0-only" and a classifier's "(GPLv2+)".
_STRONG_COPYLEFT = re.compile(r"(?<![A-Z])A?GPL")
_WEAK_COPYLEFT = re.compile(r"(?<![A-Z])(LGPL|MPL|EPL|CDDL|CPL|OSL|EUPL)")


def image_closure_extras(lock: dict[str, Any]) -> list[str]:
    """Extras the root package actually defines, minus the ones we never ship."""
    for package in lock["package"]:
        if normalize(package["name"]) == normalize(ROOT_PACKAGE):
            return sorted(package.get("optional-dependencies", {}))
    raise SystemExit(f"{ROOT_PACKAGE} is missing from uv.lock")


def _is_apache(expression: str) -> bool:
    """Whether a declared expression is Apache-2.0 and nothing else."""
    text = expression.upper().lstrip("# ").strip()
    for phrase, token in sorted(LICENSE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(phrase, token)
    text = text.replace("(", " ").replace(")", " ")
    tokens = {t.strip() for t in re.split(r"\bOR\b|\bAND\b|[,;/]", text) if t.strip()}
    return tokens == {"APACHE-2.0"}


def classify(expression: str) -> Verdict:
    """Classify a declared license expression conservatively.

    Anything not recognisably permissive is escalated rather than guessed at — an
    expression this function cannot parse should stop a release, not pass one.
    Note the escalation order: a tri-licensed package offering GPL *or* something
    weaker reads as strong here, because the weaker option only applies once
    somebody has elected it in writing (see ACKNOWLEDGED).
    """
    # A leading "#" is a project that pasted Markdown into its License field.
    text = expression.upper().lstrip("# ").strip()
    # Longest phrase first, so "GNU LESSER GENERAL PUBLIC LICENSE V2 OR LATER"
    # is consumed before any shorter alias can bite into it.
    for phrase, token in sorted(LICENSE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(phrase, token)
    # Parentheses come off only after aliasing, because several aliases match on
    # them ("ISC LICENSE (ISCL)"). What is left is either grouping in an SPDX
    # expression — "MPL-2.0 AND (Apache-2.0 OR MIT)", whose "(APACHE-2.0" would
    # otherwise be an unrecognised token — or a trailing abbreviation the split
    # below is happy to see bare.
    text = text.replace("(", " ").replace(")", " ")
    # Split on the SPDX operators. This also splits an un-aliased classifier's
    # "v2 or later" into fragments, which is harmless: the regexes below match on
    # the fragment, and anything they miss lands in "unknown" and fails the gate.
    tokens = [t.strip() for t in re.split(r"\bOR\b|\bAND\b|[,;/]", text) if t.strip()]
    if not tokens:
        return "unknown"
    if any(_STRONG_COPYLEFT.search(t) for t in tokens):
        return "strong-copyleft"
    if any(_WEAK_COPYLEFT.search(t) for t in tokens):
        return "weak-copyleft"
    if all(t in PERMISSIVE for t in tokens):
        return "permissive"
    return "unknown"


def read_override(name: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """Load a hand-vendored license for a wheel that ships none.

    Format: a first line `SPDX: <expression>`, then the license text. Used where
    upstream simply forgot to include LICENSE in the wheel (webencodings), so the
    text has to come from that project's own repository instead.
    """
    path = OVERRIDE_DIR / f"{name}.txt"
    if not path.exists():
        return None
    head, _, body = path.read_text(encoding="utf-8").partition("\n")
    if not head.startswith("SPDX:"):
        raise SystemExit(f"{path} must start with a 'SPDX: <expression>' line")
    return head.removeprefix("SPDX:").strip(), ((f"{path.name} (vendored)", body.strip()),)


def declared_license(metadata: importlib_metadata.PackageMetadata) -> str:
    """The package's own license claim, newest metadata form first."""
    expression = metadata.get("License-Expression")
    if expression:
        return str(expression)
    classifiers = [
        c.split("::")[-1].strip()
        for c in metadata.get_all("Classifier", [])
        if str(c).startswith("License ::")
    ]
    if classifiers:
        return " OR ".join(classifiers)
    declared = metadata.get("License")
    # Some projects paste the whole license text into `License:`; keep the first
    # line, which is invariably the name.
    return str(declared).strip().splitlines()[0] if declared else ""


def license_texts(dist: importlib_metadata.Distribution) -> tuple[tuple[str, str], ...]:
    """Every LICENSE / NOTICE / COPYING file the wheel installed, in path order.

    NOTICE is deliberately included: Apache-2.0 section 4(d) makes propagating it
    mandatory, and pyarrow ships one.
    """
    wanted = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)
    out = []
    for entry in sorted(dist.files or [], key=str):
        text = str(entry)
        if ".dist-info" not in text or not wanted.search(Path(text).name):
            continue
        # Read through locate_file: Distribution.read_text() resolves relative to
        # the .dist-info directory, while entries in RECORD are relative to
        # site-packages, so it would silently miss every one of these.
        try:
            body = Path(str(dist.locate_file(entry))).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append((Path(text).name, body))
    return tuple((name, body.strip()) for name, body in out if body.strip())


# Apache-2.0 is the one license here whose text repeats verbatim across packages:
# its terms carry no copyright holder, so every package shipping it ships the same
# ~10 KB, and only what follows END OF TERMS AND CONDITIONS (the APPENDIX example
# with the holder filled in, or bundled third-party notices) differs. Matching is
# on content, never on the declared license or the file name: pillow and fonttools
# both bundle Apache-2.0 *inside* a larger collection of licenses, and pyphen's GPL
# and LGPL texts end with the same sentence, so a looser rule would hoist a block
# that is not the Apache terms or truncate a file that has more to say.
APACHE_OPENING = re.compile(r"\A\s*Apache License\s+Version 2\.0,")
APACHE_TERMS_END = "END OF TERMS AND CONDITIONS"
APACHE_ANCHOR = "[Appendix A](#appendix-a--apache-license-20)"


def apache_terms(body: str) -> tuple[str, str] | None:
    """Split an Apache-2.0 text into its shared terms and the rest, or None.

    None means "reproduce this file as it stands" — the text does not *open* with
    the Apache-2.0 terms, so whatever it contains is either a different license or
    a compilation this script has no business taking apart.
    """
    if not APACHE_OPENING.match(body):
        return None
    end = body.find(APACHE_TERMS_END)
    if end == -1:
        return None
    cut = end + len(APACHE_TERMS_END)
    return body[:cut], body[cut:].strip()


def shared_apache_terms(packages: list[Package]) -> str | None:
    """The Apache-2.0 terms to publish as Appendix A, or None if there are none.

    Two things want that block, and either is enough to justify hoisting it: more
    than one package shipping the same text (where the appendix removes a real
    duplicate), and any package declaring Apache-2.0 while shipping no text of its
    own (where the appendix is the only place its terms appear at all).

    Only *byte-identical* texts are pooled, and this is the whole design. Projects
    ship Apache-2.0 in several layouts — one closure here holds four, differing by
    indentation and by `http` versus `https` in the header URL — and no amount of
    normalising reliably sorts a reformat from an edit. So nothing is normalised:
    the appendix reproduces the single most common exact text, packages matching it
    byte for byte reference it, and every other package prints the file it actually
    ships. The dedup still does its job (52 of 57 files in that closure collapse
    into one), and no package is ever shown terms it did not install.

    An earlier version compared whitespace-collapsed text and refused to continue
    when two variants disagreed, on the theory that a mismatch meant tampering. It
    fired twice on ordinary formatting drift and never on anything else, which is
    the signature of a check measuring the wrong thing — and it was guarding
    against a hazard that only existed because it was merging texts that differed.
    Merging only identical ones removes the hazard instead of policing it.
    """
    counts = Counter(
        split[0]
        for package in packages
        for _, body in package.texts
        if (split := apache_terms(body))
    )
    if not counts:
        by_declaration = [p.name for p in packages if p.apache_by_declaration]
        if not by_declaration:
            return None
        # Those packages ship no text, so the appendix has to come from one that
        # does. Sourcing it from an installed wheel rather than a constant in this
        # script keeps the published terms traceable to something a reader can diff.
        raise SystemExit(
            f"error: {len(by_declaration)} package(s) declare Apache-2.0 and ship no "
            f"license text ({', '.join(sorted(by_declaration)[:3])}...), but no package "
            f"in the closure ships an Apache-2.0 text to reproduce as Appendix A. Vendor "
            f"one at scripts/license-overrides/_apache-2.0.txt."
        )
    # Most common layout wins, so the appendix serves the largest number of packages
    # and the fewest are left reproducing their own copy. The text itself breaks ties
    # so the choice is stable across runs rather than dependent on dict ordering.
    best = max(counts, key=lambda terms: (counts[terms], terms))
    if counts[best] > 1 or any(p.apache_by_declaration for p in packages):
        return best
    return None


def marker_excluded(lock: dict[str, Any], extras: Sequence[str]) -> list[str]:
    """Packages in the graph that no environment we publish for can reach.

    Computed by walking the closure twice, once honouring environment markers and
    once ignoring them, because the Scope section claims these are excluded *and
    names them*. Hardcoding that list is how it goes stale — a dependency that
    gains or loses a marker silently makes the sentence false, and a notice that
    lists the wrong exclusions is a notice nobody can check.
    """
    global marker_applies
    honoured = set(distributed_closure(lock, extras))
    original = marker_applies
    try:
        marker_applies = lambda marker: True  # noqa: E731
        everything = set(distributed_closure(lock, extras))
    finally:
        marker_applies = original
    return sorted(everything - honoured)


def dev_requirements() -> list[str]:
    """The names of the project's direct development dependencies.

    Named in Scope so a reader can see what "development-only" covers here rather
    than taking the phrase on trust. Only the direct ones: the full dev closure is
    long, and the point is to show the shape of what was left out.
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared: list[str] = list(project.get("dependency-groups", {}).get("dev", []))
    declared += project.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    names = {re.split(r"[<>=!~\[; ]", str(entry).strip())[0] for entry in declared}
    return sorted(name for name in names if name)


def collect(lock: dict[str, Any], image_extras: frozenset[str]) -> list[Package]:
    packages = []
    problems = []
    defined = set(image_closure_extras(lock))
    if not image_extras <= defined:
        raise SystemExit(
            f"error: the Dockerfile installs undefined extra(s) "
            f"{sorted(image_extras - defined)} — pyproject defines {sorted(defined)}. "
            f"A typo here would silently shrink what this file claims the image ships."
        )
    if not image_extras <= set(NOTICED_EXTRAS):
        raise SystemExit(
            f"error: the Dockerfile installs {sorted(image_extras - set(NOTICED_EXTRAS))}, "
            f"which this notice does not account for. Add it to NOTICED_EXTRAS so the "
            f"packages it pulls in are credited — they are being redistributed."
        )
    in_image = distributed_closure(lock, sorted(image_extras))
    locked = {normalize(entry["name"]): entry for entry in lock["package"]}
    for name, version in sorted(distributed_closure(lock, NOTICED_EXTRAS).items()):
        if is_first_party(locked[name]):
            continue
        override = read_override(name)
        if override is not None:
            expression, texts = override
        else:
            try:
                dist = importlib_metadata.distribution(name)
            except importlib_metadata.PackageNotFoundError:
                problems.append(
                    f"{name} is in the distributed closure but not installed — "
                    f"run `uv sync --locked --extra dev` first"
                )
                continue
            if dist.version != version:
                problems.append(f"{name}: uv.lock pins {version} but {dist.version} is installed")
            expression = declared_license(dist.metadata)
            texts = license_texts(dist)
            if not texts and classify(expression) == "permissive" and _is_apache(expression):
                # Apache-2.0 is the one license whose text can be supplied without
                # guessing: it names no copyright holder, so the terms in Appendix A
                # *are* this package's terms, not a stand-in for them. Every other
                # license carries a holder-specific copyright line that only the
                # project itself can supply, which is why the general case below
                # still demands a vendored copy. Wheels omitting their license file
                # are common enough (one sibling has 39, all Apache-2.0) that
                # requiring a hand-vendored duplicate of one identical text for each
                # would be busywork that makes the notice no more accurate.
                packages.append(
                    Package(
                        name,
                        version,
                        expression,
                        classify(expression),
                        (),
                        name in in_image,
                        apache_by_declaration=True,
                    )
                )
                continue
            if not texts:
                problems.append(
                    f"{name} {version} declares {expression or 'no license'} and ships no "
                    f"license file — add scripts/license-overrides/{name}.txt with its "
                    f"text, taken from the project's own repository"
                )
                continue
        packages.append(
            Package(name, version, expression, classify(expression), texts, name in in_image)
        )
    if problems:
        raise SystemExit("\n".join(f"error: {p}" for p in problems))
    return packages


def paragraph(text: str) -> list[str]:
    """Wrap a generated paragraph to the width the hand-written prose uses.

    Generated sentences interpolate names whose length is not known when the
    sentence is written, so they cannot be hand-wrapped like the literal
    paragraphs around them. Without this the file is a mix of 78-column prose and
    250-column lines, which reads as broken and makes every regeneration a noisy
    diff on whichever line a package name grew.
    """
    # break_on_hyphens is the load-bearing argument: the default splits
    # "[Appendix A](#appendix-a--apache-license-20)" at a hyphen inside the
    # anchor, which silently produces a dead link in the rendered Markdown.
    # break_long_words guards the same thing for a long unhyphenated URL.
    return [
        *textwrap.wrap(
            " ".join(text.split()),
            width=78,
            break_on_hyphens=False,
            break_long_words=False,
        ),
        "",
    ]


def english_list(items: Sequence[str]) -> str:
    """ "a", "a and b", "a, b and c" — a bare comma join reads as a truncated list."""
    items = list(items)
    if not items:
        return ""
    return " and ".join(filter(None, [", ".join(items[:-1])] + items[-1:]))


def render(packages: list[Package], image_extras: frozenset[str], lock: dict[str, Any]) -> str:
    apache = shared_apache_terms(packages)
    # Say so in Scope rather than letting a reader hit the first reference cold and
    # wonder whether something was left out. The second half of the sentence is the
    # load-bearing one: an appendix is only honest if nothing else was dropped with it.
    apache_note = (
        paragraph(
            f"Apache-2.0's terms carry no copyright holder and are byte-identical "
            f"wherever they appear, so the packages under them point at {APACHE_ANCHOR} "
            f"instead of repeating ~10 KB each. Whatever a package adds beyond those "
            f"terms — its own copyright line, the third-party notices it bundles — is "
            f"still reproduced in full under its own heading below."
        )
        if apache
        else []
    )
    # Packages whose text had to be vendored ship none of their own, so inside the
    # image this file is the only place their license exists. Saying so keeps the
    # sentence above honest without hardcoding which package it is.
    vendored = sorted(
        package.name for package in packages if (OVERRIDE_DIR / f"{package.name}.txt").exists()
    )
    one = len(vendored) == 1
    # Built as named parts rather than inline conditionals: the possessive differs
    # between "the project's own repository" and "the projects' own repositories",
    # and an apostrophe placed by string concatenation is easy to get wrong.
    owner = "project's" if one else "projects'"
    vendored_note = (
        paragraph(
            f"One exception: {english_list([f'`{name}`' for name in vendored])} "
            f"{'ships' if one else 'ship'} no license file of "
            f"{'its' if one else 'their'} own — upstream omits it from the wheel — so "
            f"inside the image this file is the only copy of "
            f"{'its' if one else 'those'} terms. "
            f"{'Its text is' if one else 'Their texts are'} reproduced below from the "
            f"{owner} own {'repository' if one else 'repositories'}."
        )
        if vendored
        else []
    )

    extras_phrase = (
        english_list([f"`--extra {extra}`" for extra in sorted(image_extras)]) or "no extras"
    )
    in_image = sum(1 for package in packages if package.in_image)
    everything = in_image == len(packages)
    image_scope = "every package below" if everything else "the packages marked *yes* below"
    # Only describe an informational set when one exists — with every extra
    # installed there is nothing outside the image, and the paragraph would be
    # describing an empty set.
    outside_image: list[str] = (
        []
        if everything or not PUBLISHES_IMAGE
        else paragraph(
            f"The remaining packages are reachable only through an extra the image does "
            f"not install. They are credited here because `pip install {ROOT_PACKAGE}"
            f"[{sorted(set(NOTICED_EXTRAS) - image_extras)[0]}]` can pull them in and "
            f"because a future artifact may bundle them — but no artifact we publish "
            f"redistributes them today."
        )
    )
    pythons = supported_pythons()
    python_range = pythons[0] if len(pythons) == 1 else f"{pythons[0]}–{pythons[-1]}"
    noticed_phrase = (
        f" plus the {english_list([f'`{e}`' for e in NOTICED_EXTRAS])} "
        f"{'extra' if len(NOTICED_EXTRAS) == 1 else 'extras'}"
        if NOTICED_EXTRAS
        else ""
    )
    dev_names = dev_requirements()
    dev_phrase = f" ({english_list(dev_names)})" if 0 < len(dev_names) <= 12 else ""
    excluded = marker_excluded(lock, NOTICED_EXTRAS)
    excluded_note = (
        paragraph(
            f"Resolutions no published environment can reach "
            f"({english_list([f'`{name}`' for name in excluded])}) are excluded: the "
            f"image is linux/CPython, and so is every environment this project supports "
            f"being installed into."
        )
        if excluded
        else []
    )
    # The wheel/sdist paragraph is a statement about artifacts on PyPI. A project
    # that publishes none would be asserting something about a thing that does not
    # exist, so it is omitted rather than reworded.
    pypi_note = (
        [
            f"The **wheel** on PyPI contains only `{MODULE_NAME}`. The **sdist** also",
            "carries this repository's own sources. Neither contains a single third-party",
            "package: pip resolves and downloads everything below from PyPI itself, so both",
            "artifacts redistribute nothing and this file is informational for them.",
            "",
        ]
        if PUBLISHES_TO_PYPI
        else []
    )
    lines = [
        "# Third-party notices",
        "",
        f"<!-- Generated by scripts/{Path(__file__).name} — do not edit by hand. -->",
        "",
        f"{PROJECT_NAME} itself is licensed under the",
        "[Business Source License 1.1](LICENSE). This file covers the third-party",
        "packages it depends on and reproduces the attribution each one requires.",
        "",
        "## Scope",
        "",
        *pypi_note,
        *(
            paragraph(
                f"The **Docker image redistributes** {image_scope}. It copies a "
                f"populated virtualenv built with {extras_phrase}, handing those "
                f"packages over as binaries, which is what makes their notices "
                f"mandatory rather than courteous. Their license texts are present "
                f"inside the image too, at "
                f"`/app/.venv/lib/python*/site-packages/*.dist-info/licenses/`, "
                f"alongside `/app/LICENSE` and `/app/{NOTICES_PATH.name}`."
            )
            if PUBLISHES_IMAGE
            else paragraph(
                "**No artifact here redistributes any of it.** This project publishes "
                "no container image, so every package below reaches a user from PyPI "
                "rather than from us, and none of the attribution duties that "
                "redistribution triggers apply. The list is published because knowing "
                "what a tool pulls into your environment is worth having, and because "
                "the licence gate that produces it is worth running — not because "
                "anything here is owed."
            )
        ),
        *vendored_note,
        *apache_note,
        *outside_image,
        *paragraph(
            f"The list is the dependency closure of `{ROOT_PACKAGE}`{noticed_phrase}, "
            f"resolved from `uv.lock` for linux/CPython on Python {python_range}. "
            f"Development-only dependencies{dev_phrase} are excluded: they are tools "
            f"the project runs, not code it ships."
        ),
        *excluded_note,
        *(
            [
                "`uv.lock` sees only PyPI packages, so the interpreter and the operating",
                "system the Docker image is built on are covered separately under",
                "[Platform layer](#platform-layer-docker-image) below.",
                "",
            ]
            if PUBLISHES_IMAGE
            else []
        ),
        "Regenerate with:",
        "",
        "```bash",
        "uv sync --locked --extra dev",
        f"uv run --no-sync python scripts/{Path(__file__).name}",
        "```",
        "",
        "## Summary",
        "",
        (
            f"{len(packages)} packages, none of them redistributed by anything this "
            f"project publishes."
            if not PUBLISHES_IMAGE
            else f"{len(packages)} packages, all of them redistributed by the published image."
            if everything
            else f"{len(packages)} packages: {in_image} in the published image, "
            f"{len(packages) - in_image} reachable only through an extra it does not install."
        ),
        "",
        # The "In image" column answers "is this redistributed?". Without an image
        # the answer is no for every row, and a column of identical "no"s reads as
        # though it were telling them apart.
        *(
            ["| Package | Version | License | In image |", "| --- | --- | --- | --- |"]
            if PUBLISHES_IMAGE
            else ["| Package | Version | License |", "| --- | --- | --- |"]
        ),
    ]
    for package in packages:
        flag = "" if package.verdict == "permissive" else " ⚠️"
        row = f"| {package.name} | {package.version} | {package.license_expression}{flag} |"
        if PUBLISHES_IMAGE:
            row += f" {'yes' if package.in_image else 'no'} |"
        lines.append(row)

    # Only reviewed entries have a note to publish. A Pending one fails the policy
    # gate before a file is ever written, so this is defensive rather than a branch
    # a released notice can take.
    unresolved = [
        package for package in packages if isinstance(ACKNOWLEDGED.get(package.name), Pending)
    ]
    if unresolved:
        lines += [
            "",
            "## ⚠️ Unresolved licence questions",
            "",
            "**This notice is incomplete.** The packages below are redistributed but their",
            "terms have not been reviewed and accepted, so nothing here should be treated as",
            "a licence clearance for them. The generator exits non-zero while this section",
            "exists; it is present so the open questions are visible rather than implied by",
            "a build failure nobody reads.",
            "",
            "| Package | Version | Declared license | What is open |",
            "| --- | --- | --- | --- |",
        ]
        for package in unresolved:
            entry = ACKNOWLEDGED[package.name]
            assert isinstance(entry, Pending)
            reason = " ".join(entry.reason.split())
            lines.append(
                f"| {package.name} | {package.version} "
                f"| {package.license_expression or '*none declared*'} | {reason} |"
            )
        lines.append("")

    flagged = [
        package
        for package in packages
        if isinstance(ACKNOWLEDGED.get(package.name), Acknowledgement)
    ]
    if flagged:
        lines += ["", "## Conditions worth knowing (⚠️ above)", ""]
        for package in flagged:
            entry = ACKNOWLEDGED[package.name]
            assert isinstance(entry, Acknowledgement)
            lines += [
                f"### {package.name} — {package.license_expression}",
                "",
                entry.note,
                "",
            ]
    else:
        lines.append("")

    if PUBLISHES_IMAGE:
        lines += [render_platform_section()]

    by_declaration = [package for package in packages if package.apache_by_declaration]
    if by_declaration:
        lines += [
            "## Packages shipping no license file",
            "",
            "These declare Apache-2.0 in their metadata but install no license file with",
            f"the wheel. Apache-2.0 names no copyright holder, so the terms in {APACHE_ANCHOR}",
            "are these packages' own terms in full, not a substitute for a text that could",
            "not be found. A package under any other license that shipped no text would not",
            "be listed here — its notice would have to be vendored into the repository",
            "instead, because only the project itself can supply its copyright line.",
            "",
            "| Package | Version | Declared license |",
            "| --- | --- | --- |",
        ]
        for package in by_declaration:
            lines.append(f"| {package.name} | {package.version} | {package.license_expression} |")
        lines.append("")

    lines += ["## Full license texts", ""]
    for package in packages:
        lines += [f"### {package.name} {package.version}", ""]
        if package.apache_by_declaration:
            lines += [
                f"Ships no license file. Declares {package.license_expression}, whose terms "
                f"are reproduced in {APACHE_ANCHOR}.",
                "",
            ]
            continue
        for filename, body in package.texts:
            split = apache_terms(body) if apache else None
            if split is None or split[0] != apache:
                lines += [f"*{filename}*", "", "```text", body, "```", ""]
                continue
            terms, remainder = split
            lines += [
                f"*{filename}*",
                "",
                f"Opens with the Apache License 2.0 terms, reproduced verbatim in {APACHE_ANCHOR}"
                + ("." if not remainder else ", and continues:"),
                "",
            ]
            if remainder:
                lines += ["```text", remainder, "```", ""]

    if apache:
        lines += [
            "## Appendix A — Apache License 2.0",
            "",
            "Referenced by every package above whose license file opens with these terms.",
            "Reproduced once because the text is identical in each of them; it is the",
            "same copy, not a summary of them.",
            "",
            "```text",
            apache,
            "```",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


# Installers this parser understands. Anything else in the Dockerfile that
# installs the project is refused rather than ignored — see dockerfile_extras.
_INSTALL_COMMAND = re.compile(
    r"\b(uv sync|uv pip install|pip install|pip3 install|poetry install)\b"
)


# `${VAR}` and `$VAR`, the two spellings Docker accepts in a RUN line.
_ARG_REFERENCE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_ARG_DECLARATION = re.compile(r"^\s*ARG\s+(\w+)\s*=\s*(\S+)\s*$", re.MULTILINE)


def build_arg_defaults(text: str) -> dict[str, str]:
    """`ARG NAME=value` declarations, plus whatever IMAGE_BUILD_ARGS overrides."""
    defaults: dict[str, str] = {
        str(name): str(value) for name, value in _ARG_DECLARATION.findall(text)
    }
    defaults.update(IMAGE_BUILD_ARGS)
    return defaults


def expand_build_args(command: str, defaults: dict[str, str]) -> str:
    """Substitute `ARG` defaults into a single install command before it is parsed.

    A sibling repo selects its image variant with `uv sync --extra ${OB_EXTRA}`,
    and left unexpanded that reads as an extra literally named `${OB_EXTRA}` —
    which the defined-extras check would then reject as undefined, failing the
    build for a Dockerfile that is perfectly correct.

    Deliberately scoped to the install command rather than applied to the whole
    file. A Dockerfile is full of `$VAR` that has nothing to do with build
    arguments — `ENV PATH="/app/.venv/bin:$PATH"` is in this very repo — and
    expanding those would either rewrite lines this parser has no business
    touching or, worse, refuse the build over a variable that never influenced
    what got installed.

    Within that scope an unresolvable reference does raise, for the same reason an
    unparsed install command does: the answer would not be wrong in an obvious
    way, it would be wrong in a way that still renders. A `--build-arg` supplied
    at build time and given no default here is a variant this script cannot see,
    and a notice silently describing the default while CI ships something else is
    worse than one that refuses to be written.

    Note this resolves the *default* variant only. Where CI builds several (an
    `OB_EXTRA` matrix), the notice must cover the union, and IMAGE_BUILD_ARGS is
    where those values are declared — see its comment.
    """

    def resolve(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in defaults:
            raise SystemExit(
                f"error: {DOCKERFILE_PATH.name} refers to build argument ${{{name}}} but "
                f"declares no default for it, so what the image installs depends on a "
                f"`--build-arg` this script cannot see. Give the ARG a default, or record "
                f"the value CI builds with in IMAGE_BUILD_ARGS in {Path(__file__).name}."
            )
        return defaults[name]

    return _ARG_REFERENCE.sub(resolve, command)


def image_installs_dev(text: str, lock: dict[str, Any]) -> bool:
    """Whether the image's virtualenv includes the dev dependency group.

    Both halves are needed, and either one alone gives a wrong answer on a repo in
    this family. `uv sync` installs dev by default, so the absence of `--no-dev` is
    necessary — but not sufficient, because what it installs is a PEP 735
    `[dependency-groups]` group, and a project whose `dev` is an *extra* has
    nothing for it to pull in. A sibling here is exactly that shape: it syncs
    without `--no-dev` and still ships no test tooling, because its `dev` is an
    extra that no `--extra dev` ever asks for.

    Answered across every sync line rather than the last one, erring toward
    including dev: over-crediting a package puts an unnecessary licence in the
    notice, while under-crediting one withholds attribution the image owes.
    """
    packages = {normalize(entry["name"]): entry for entry in lock["package"]}
    root = packages.get(normalize(ROOT_PACKAGE), {})
    if not root.get("dev-dependencies"):
        return False
    syncs = re.findall(r"uv sync[^\n]*", text)
    return any(not re.search(r"(?<![\w-])(--no-dev|--only-group)\b", command) for command in syncs)


def dockerfile_extras(defined_extras: Collection[str]) -> frozenset[str]:
    """The extras the Dockerfile installs — that is, what the image really contains.

    This is the seam deciding which packages a published artifact redistributes and
    whether pyphen needs an election, so it parses rather than pattern-matches:
    comment lines are dropped, continuations joined, `--extra=pdf` accepted beside
    `--extra pdf`, `--all-extras` expanded against what pyproject defines, and
    `--no-extra` subtracted. A guard defeated by reformatting a RUN line would be
    worse than no guard, because it would still look like one.

    The failure mode to design against is not a wrong answer but an empty one: a
    silent `frozenset()` would mark every package as not redistributed and skip the
    election check, all while the file still reads as though it had been verified.
    So an install command this parser does not understand raises instead — better a
    build that stops on an unfamiliar line than a notice quietly describing an image
    nobody shipped.
    """
    # No image means nothing is redistributed, so "no extras" is the correct
    # answer rather than the dangerous silent one this docstring warns about. It
    # is decided by configuration, not by a parser that happened to match nothing.
    if not PUBLISHES_IMAGE:
        return frozenset()
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    text = re.sub(r"\\\s*\n\s*", " ", text)

    unknown = {
        match.group(1) for match in _INSTALL_COMMAND.finditer(text) if match.group(1) != "uv sync"
    }
    if unknown:
        raise SystemExit(
            f"error: the Dockerfile installs with {sorted(unknown)}, which "
            f"{Path(__file__).name} cannot read extras from. Teach dockerfile_extras() "
            f"that form — leaving it unparsed would silently report an image that "
            f"redistributes nothing."
        )

    extras: set[str] = set()
    defaults = build_arg_defaults(text)
    for raw in re.findall(r"uv sync[^\n]*", text):
        command = expand_build_args(raw, defaults)
        # --all-extras means every extra pyproject defines, `dev` included. Expanding
        # it rather than special-casing is what lets the existing NOTICED_EXTRAS check
        # object to shipping an extra this notice does not account for.
        if re.search(r"(?<![\w-])--all-extras\b", command):
            extras.update(defined_extras)
        extras.update(re.findall(r"(?<![\w-])--extra[=\s]+([A-Za-z0-9_.-]+)", command))
        for excluded in re.findall(r"(?<![\w-])--no-extra[=\s]+([A-Za-z0-9_.-]+)", command):
            extras.discard(excluded)
    return frozenset(extras)


def enforce_pyphen_election(ships_pdf: bool, election: str | None) -> list[tuple[str, str]]:
    """Force the election at the moment it starts to matter, and not a moment before.

    Both directions are errors. Shipping pyphen with no election distributes a
    GPL-optional package with no record of which option was taken. Recording one
    while nothing we publish contains pyphen commits the company to terms it has no
    need to accept — and makes the generated notice untrue, because the elected
    branch of that note states outright that the image hands over a copy.
    """
    if not ships_pdf and election is not None:
        return [
            (
                "election",
                f"the Dockerfile no longer installs the `pdf` extra, so nothing we publish "
                f"redistributes pyphen — but PYPHEN_ELECTION still records {election!r}. "
                f"That commits RALFORION to terms nothing requires, and makes the generated "
                f"note say the image hands over a copy of pyphen when it does not. Set "
                f"PYPHEN_ELECTION back to None.",
            )
        ]
    if ships_pdf and election is None:
        return [
            (
                "election",
                "the Dockerfile now installs the `pdf` extra, so a published artifact "
                "redistributes pyphen (GPL-2.0+ OR LGPL-2.1+ OR MPL-1.1). Distributing it "
                "means choosing one of those, and the choice has to be recorded: set "
                "PYPHEN_ELECTION in " + Path(__file__).name + ' to "LGPL-2.1-or-later" '
                '(the usual choice for an unmodified imported library) or "MPL-1.1". Never '
                "the GPL option — it conflicts with this project's own license.",
            )
        ]
    return []


def enforce_policy(packages: list[Package]) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for package in packages:
        if package.verdict == "permissive":
            continue
        # Bound outside the f-strings below rather than inlined: a multi-line
        # expression inside `{}` is PEP 701, which needs Python 3.12, and this file
        # is vendored into a repository whose floor is 3.11.
        declared = package.license_expression or "no license declared"
        acknowledgement = ACKNOWLEDGED.get(package.name)
        if isinstance(acknowledgement, Pending):
            failures.append(
                (
                    "pending",
                    f"{package.name} {package.version} ({declared}"
                    f") is recorded as Pending in {Path(__file__).name}: "
                    f"{acknowledgement.reason}. Replace it with an Acknowledgement stating "
                    f"why the terms are acceptable, or drop the dependency — a Pending entry "
                    f"is a note that the question is open, not an answer to it.",
                )
            )
        elif acknowledgement is None:
            failures.append(
                (
                    "unreviewed",
                    f"{package.name} {package.version} is {package.verdict} "
                    f"({declared}). "
                    f"Distributing it is a decision, not a lockfile bump: either drop the "
                    f"dependency or add it to ACKNOWLEDGED in {Path(__file__).name} with the "
                    f"reason it is acceptable.",
                )
            )
        elif acknowledgement.license_expression != package.license_expression:
            failures.append(
                (
                    "mismatch",
                    f"{package.name} {package.version} is acknowledged under "
                    f"{acknowledgement.license_expression!r} but now declares "
                    f"{package.license_expression!r}. The exemption covers the terms that "
                    f"were reviewed, not the package name: re-read the new license and "
                    f"update its ACKNOWLEDGED entry, or drop the dependency.",
                )
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the license policy and that the notices file is current",
    )
    parser.add_argument(
        "--allow-pending",
        action="store_true",
        help=(
            "write the notice even though licences are still Pending, marking them "
            "unresolved in the file itself. Still exits non-zero. For bootstrapping a "
            "repository, never for a release."
        ),
    )
    args = parser.parse_args()

    lock = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))
    image_extras = dockerfile_extras(image_closure_extras(lock))
    packages = collect(lock, image_extras)
    rendered = render(packages, image_extras, lock)

    failures = enforce_policy(packages)
    failures += enforce_pyphen_election("pdf" in image_extras, PYPHEN_ELECTION)
    if failures:
        for _kind, message in failures:
            print(f"error: {message}", file=sys.stderr)
        # --allow-pending exists for one situation: standing this file up in a
        # repository whose backlog of undecided licences is exactly what the notice
        # is meant to surface. Refusing to write anything there leaves nothing to
        # review, so the file is written with those packages called out in it and
        # the exit code stays non-zero, which is what any pipeline reads. It cannot
        # launder anything: a Pending package is named in the notice as unresolved,
        # so a file produced this way announces its own incompleteness.
        # Only when *every* failure is a recorded-and-open licence. One unreviewed
        # package, or one acknowledgement whose terms have changed underneath it, and
        # this writes nothing: those are not questions somebody has already logged.
        if not (args.allow_pending and all(kind == "pending" for kind, _ in failures)):
            return 1
        unresolved = [kind for kind, _ in failures]
        NOTICES_PATH.write_text(rendered, encoding="utf-8")
        print(
            f"wrote {NOTICES_PATH.name} with {len(unresolved)} unresolved licence "
            f"question(s) marked in it — this is not a releasable notice",
            file=sys.stderr,
        )
        return 1

    if not args.check:
        NOTICES_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {NOTICES_PATH.relative_to(ROOT)} ({len(packages)} packages)")
        return 0

    # With no committed file there is nothing that can be stale, so --check is the
    # licence gate alone; every failure above has already been reported. It says
    # which mode it ran in on purpose — a gate that printed the same line as a
    # drift check would let a repository lose its committed notice with nothing
    # noticing.
    if not COMMIT_NOTICE:
        print(
            f"licence policy ok ({len(packages)} packages). No committed notice here — "
            f"nothing this project publishes redistributes them."
        )
        return 0

    current = NOTICES_PATH.read_text(encoding="utf-8") if NOTICES_PATH.exists() else ""
    if current != rendered:
        print(
            f"error: {NOTICES_PATH.name} is out of date — regenerate it with\n"
            "  uv run --no-sync python scripts/third_party_notices.py",
            file=sys.stderr,
        )
        diff = unified_diff(
            current.splitlines(), rendered.splitlines(), "on disk", "expected", lineterm=""
        )
        print("\n".join(list(diff)[:40]), file=sys.stderr)
        return 1
    print(f"{NOTICES_PATH.name} is current ({len(packages)} packages, policy ok)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
