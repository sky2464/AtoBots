"""AtoBots engine."""
import re
from pathlib import Path

__version__ = "0.2.1"


def full_version() -> str:
    """The version to show people. A local build (a zip or folder installed
    with `cb update --zip`) carries engine/LOCAL_BUILD with its hash and
    reports `<version>+local.<hash>`, so it is never mistaken for the release
    with the same number."""
    try:
        sha = (Path(__file__).resolve().parent.parent
               / "LOCAL_BUILD").read_text().strip()
    except OSError:
        return __version__
    if re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return f"{__version__}+local.{sha[:7]}"
    return __version__
