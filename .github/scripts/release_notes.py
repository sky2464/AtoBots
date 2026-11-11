#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version, for a GitHub Release.

usage: release_notes.py X.Y.Z [CHANGELOG.md]

The section runs from the line `## X.Y.Z ...` to the next `## ` heading. The
heading line itself is left out (the Release already has a title). Exits 1,
printing why on stderr, when the version has no section or the section is
empty, so a release is never created with blank notes.
"""
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def extract(text: str, version: str) -> str:
    if not VERSION_RE.match(version):
        raise ValueError(f"not a version: {version!r}")
    lines = text.splitlines()
    heading = re.compile(r"^## " + re.escape(version) + r"(\s|$)")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise ValueError(f"the CHANGELOG.md section for {version} is empty")
    return notes + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[2]) if len(argv) == 3 else Path("CHANGELOG.md")
    try:
        sys.stdout.write(extract(path.read_text(encoding="utf-8"), argv[1]))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
