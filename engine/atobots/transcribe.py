"""Speech-to-text runner for voice and audio files.

`cb transcribe FILE` runs the command in config `transcribe_cmd` with the
file path as its last argument, under the memory limit and a 5-minute timeout.
Without `transcribe_cmd`, it exits 1 with a clear notice.
"""
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import chat
from .config import Config

TIMEOUT_SECONDS = 300  # 5 minutes
NO_TOOL_MESSAGE = "no speech-to-text tool is set up; ask the Owner"


def transcribe(cfg: Config, file_path: str | Path,
               runner=None) -> str:
    """Run `transcribe_cmd` with `file_path` appended as last argument.

    Enforces MemoryMax via chat.memory_wrap when systemd-run is available
    and a 5-minute timeout. Returns stdout as stripped text.
    """
    if runner is None:
        runner = subprocess.run
    if not cfg.transcribe_cmd or not cfg.transcribe_cmd.strip():
        raise RuntimeError(NO_TOOL_MESSAGE)

    cmd_parts = shlex.split(cfg.transcribe_cmd)
    if not cmd_parts:
        raise RuntimeError(NO_TOOL_MESSAGE)

    exe = cmd_parts[0]
    if not shutil.which(exe) and not Path(exe).is_file():
        raise RuntimeError(f"{exe}: command not found")

    full_cmd = list(cmd_parts) + [str(file_path)]
    wrapped = chat.memory_wrap(cfg, full_cmd)

    try:
        proc = runner(
            wrapped,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"transcription timed out after {TIMEOUT_SECONDS}s") from e
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"transcription failed (exit {e.returncode}): {err}") from e
    except OSError as e:
        raise RuntimeError(f"transcription failed to start: {e}") from e

    return (proc.stdout or "").strip()


def _cmd_transcribe(args) -> int:
    cfg = Config.load()
    try:
        text = transcribe(cfg, args.file)
        if text:
            print(text)
        return 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def register(subparsers) -> None:
    p = subparsers.add_parser("transcribe", help="transcribe an audio or voice file")
    p.add_argument("file", help="path to audio/voice file")
    p.set_defaults(func=_cmd_transcribe)
