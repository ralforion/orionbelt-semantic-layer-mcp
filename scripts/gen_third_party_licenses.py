"""Generate an aggregate third-party license file for the runtime dependency set.

Stdlib only, on purpose: it runs inside the Docker builder stage against the
already-synced runtime virtualenv (`uv sync --no-dev`), so it must not need a
package that virtualenv does not have.

It reports every distribution installed in the *running interpreter's*
environment, minus this project itself. That set is exactly the runtime closure
when the environment was built with `--no-dev`. For a local run against a
development venv (which also holds pytest, ruff, ...), use the
`scripts/gen-third-party-licenses.sh` wrapper, which materializes a throwaway
runtime-only venv first.

    python scripts/gen_third_party_licenses.py -o LICENSES-THIRD-PARTY.txt
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

SELF = "orionbelt-semantic-layer-mcp"

# dist-info members that carry license text. PEP 639 puts them under
# `licenses/`; older wheels drop them at the dist-info root.
LICENSE_STEMS = ("LICEN", "COPYING", "COPYRIGHT", "NOTICE", "AUTHORS")

RULE = "=" * 78
THIN = "-" * 78

# Fallback identification for wheels that ship license text but declare nothing
# machine-readable in their metadata (no License-Expression, no classifier).
# Matched against the opening lines of the license file, longest marker first.
TEXT_MARKERS = (
    ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL"),
    ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL"),
    ("GNU GENERAL PUBLIC LICENSE", "GPL"),
    ("MOZILLA PUBLIC LICENSE", "MPL-2.0"),
    ("APACHE LICENSE", "Apache-2.0"),
    ("MIT LICENSE", "MIT"),
    ("ISC LICENSE", "ISC"),
    ("BSD 3-CLAUSE", "BSD-3-Clause"),
    ("BSD 2-CLAUSE", "BSD-2-Clause"),
    ("THE UNLICENSE", "Unlicense"),
)


def _detect_license(texts: list[tuple[str, str]]) -> str:
    """Guess an SPDX-ish identifier from the head of a license file."""
    for _, text in texts:
        head = "\n".join(text.splitlines()[:12]).upper()
        for marker, identifier in TEXT_MARKERS:
            if marker in head:
                return identifier
        if "PERMISSION IS HEREBY GRANTED, FREE OF CHARGE" in head:
            return "MIT"
    return ""


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def _declared_license(meta: metadata.Message) -> str:
    """Best available license identifier, preferring the PEP 639 expression."""
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()

    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return " OR ".join(c.split(":: ")[-1].strip() for c in classifiers)

    legacy = (meta.get("License") or "").strip()
    if legacy and "\n" not in legacy and len(legacy) <= 80:
        return legacy
    return "UNKNOWN"


def _homepage(meta: metadata.Message) -> str:
    home = (meta.get("Home-page") or "").strip()
    if home:
        return home
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("homepage", "source", "source code", "repository"):
            return url.strip()
    return ""


def _license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """Every license-ish file the wheel installed, as (member path, text)."""
    files = dist.files or []
    found: dict[str, str] = {}

    for path in files:
        parts = path.parts
        if not any(part.endswith((".dist-info", ".egg-info")) for part in parts):
            continue
        if not path.name.upper().startswith(LICENSE_STEMS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if text and text.strip():
            found[str(path)] = text.strip()

    if found:
        return sorted(found.items())

    # Some wheels inline the full text in the legacy `License:` metadata field
    # instead of shipping a file.
    legacy = (dist.metadata.get("License") or "").strip()
    if "\n" in legacy or len(legacy) > 80:
        return [("METADATA (License field)", legacy)]
    return []


def collect(env_path: list[str] | None = None) -> list[metadata.Distribution]:
    dists = metadata.distributions() if env_path is None else metadata.distributions(path=env_path)
    seen: dict[str, metadata.Distribution] = {}
    for dist in dists:
        name = dist.metadata.get("Name")
        if not name or _normalize(name) == SELF:
            continue
        seen.setdefault(_normalize(name), dist)
    return [seen[key] for key in sorted(seen)]


def render(
    dists: list[metadata.Distribution],
    project_version: str,
    platform_note: str = "",
) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    out: list[str] = [
        "OrionBelt Semantic Layer MCP — Third-Party Licenses",
        f"Generated {stamp} for orionbelt-semantic-layer-mcp {project_version}",
    ]
    if platform_note:
        out.append(f"Resolved for {platform_note}")
    out += [
        "",
        "This distribution bundles the Python packages listed below. Each entry gives",
        "the package, its version, its declared license and the license text exactly as",
        "shipped by its author. The individual texts also remain in place alongside each",
        "package in site-packages; this file is an aggregate for convenience.",
        "",
        "SCOPE: Python packages only. The Docker image also inherits its base operating",
        "system from python:3.14-slim, whose packages carry their own licenses — some of",
        "them GPL/LGPL system components such as coreutils and libc6. Those licenses ship",
        "in the image under /usr/share/doc/<package>/copyright and are not repeated here.",
        "",
        "The MCP server's own code is Apache-2.0 — see LICENSE.",
        "",
        RULE,
        f"SUMMARY ({len(dists)} packages)",
        RULE,
        "",
    ]

    missing: list[str] = []
    resolved: list[tuple[str, str, list[tuple[str, str]]]] = []
    for dist in dists:
        name = dist.metadata.get("Name") or "?"
        texts = _license_texts(dist)
        declared = _declared_license(dist.metadata)
        if declared == "UNKNOWN":
            detected = _detect_license(texts)
            if detected:
                declared = f"{detected} (identified from license text)"
        resolved.append((name, declared, texts))

    for (name, declared, _), dist in zip(resolved, dists, strict=True):
        out.append(f"  {name:<32} {dist.version:<12} {declared}")
    out.append("")

    for (name, declared, texts), dist in zip(resolved, dists, strict=True):
        meta = dist.metadata
        out += ["", RULE, f"{name} {dist.version}", RULE, f"License: {declared}"]
        home = _homepage(meta)
        if home:
            out.append(f"Homepage: {home}")
        out.append(THIN)
        if texts:
            for member, text in texts:
                out += [f"[{member}]", "", text, ""]
        else:
            missing.append(name)
            out += [
                "Upstream ships no license text in its distribution; the license above is",
                "the one it declares in its package metadata. Obtain the full text from",
                "the project homepage or from the SPDX identifier.",
                "",
            ]

    if missing:
        out += [
            "",
            RULE,
            "PACKAGES WITHOUT AN UPSTREAM LICENSE FILE",
            RULE,
            "",
            "These declare a license in metadata but ship no license text of their own:",
            "",
            *(f"  - {name}" for name in missing),
            "",
        ]

    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("LICENSES-THIRD-PARTY.txt"),
        help="file to write (default: LICENSES-THIRD-PARTY.txt)",
    )
    parser.add_argument(
        "--project-version",
        default="",
        help="version to stamp in the header (default: read from installed metadata)",
    )
    parser.add_argument(
        "--path",
        type=Path,
        action="append",
        default=None,
        help=(
            "directory of installed distributions to scan instead of this "
            "interpreter's environment; repeatable. Lets the report describe a "
            "cross-target `uv pip install --target` tree (see "
            "scripts/gen-third-party-licenses.sh)."
        ),
    )
    parser.add_argument(
        "--platform-note",
        default="",
        help="one line describing the target the report was resolved for",
    )
    args = parser.parse_args()

    version = args.project_version
    if not version:
        try:
            version = metadata.version(SELF)
        except metadata.PackageNotFoundError:
            version = "unknown"

    env_path = [str(p) for p in args.path] if args.path else None
    dists = collect(env_path)
    if not dists:
        where = ", ".join(env_path) if env_path else "this environment"
        print(f"error: no distributions found in {where}", file=sys.stderr)
        return 1

    args.output.write_text(render(dists, version, args.platform_note), encoding="utf-8")
    print(f"wrote {args.output} — {len(dists)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
