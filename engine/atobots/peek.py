"""Status peek: while the always-on Admin works on something the Owner asked
for by Telegram, one silent message in the chat says what it is doing.

It costs no model tokens. The engine reads what Claude Code already writes
(the session transcript) and talks to Telegram's Bot API itself:

- The prompt hook (`cb hook prompt`) notes that a turn started and in which
  chat; the stop hook (`cb hook stop`) notes that it ended. Both are files in
  the always-on Admin's state folder.
- `Peek.tick()` is called about once a second from the session's wait loop.
  It shows "typing" at once, posts one silent status message after
  POST_AFTER seconds, edits it when the label, the minute or the queue count
  changes (never more often than EDIT_EVERY seconds), and deletes it when the
  turn ends, so the real reply arrives as a normal message.

What is shown is a fixed label from a small table ("Reading files"). No file
name, command, message text or other tool argument ever leaves the server.

Nothing here may disturb the work: every failure is swallowed, a failed
Telegram call pauses the peek for a while, and a transcript line that does
not parse is skipped. The transcript format is not a public interface.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TURN_FILE = "peek-turn.json"
POST_AFTER = 10         # s of work before the status message appears
EDIT_EVERY = 5          # s between two edits, at least
TYPING_EVERY = 4        # s; Telegram shows "typing" for about five
MAX_TURN = 3600         # s; a turn file older than this is stale
PAUSE_AFTER_ERROR = 60  # s without calls after a failed one
API_TIMEOUT = 5         # s; the wait loop also watches the session
_TAIL_LIMIT = 2 * 1024 * 1024

WORKING = "Working"
REPLYING = "Writing the reply"
LABELS = {
    "Read": "Reading files",
    "NotebookRead": "Reading files",
    "Glob": "Searching",
    "Grep": "Searching",
    "ToolSearch": "Searching",
    "Edit": "Editing files",
    "MultiEdit": "Editing files",
    "Write": "Editing files",
    "NotebookEdit": "Editing files",
    "Bash": "Running a command",
    "BashOutput": "Running a command",
    "WebFetch": "Looking something up",
    "WebSearch": "Looking something up",
    "Task": "Asking a helper",
    "Agent": "Asking a helper",
    "TodoWrite": "Planning",
    "Skill": "Following a procedure",
}
# The only look at a tool argument: is this command a known test runner?
# The command itself is never shown.
TEST_COMMANDS = ("pytest", "python -m pytest", "python3 -m pytest",
                 "python -m unittest", "python3 -m unittest", "npm test",
                 "npm run test", "yarn test", "pnpm test", "go test",
                 "cargo test", "make test", "tox", "phpunit", "rspec")
# `cb` commands that hand work to another bot.
DELEGATE_COMMANDS = ("cb send", "cb ask", "cb task")


def label_for(name, tool_input=None) -> str:
    """The fixed label for one tool call. Unknown tools read as "Working"."""
    if not isinstance(name, str):
        return WORKING
    if name.startswith("mcp__"):
        return REPLYING if "telegram" in name.lower() \
            else "Using a connected service"
    if name == "Bash" and isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str):
            head = " ".join(command.split())
            if head.startswith(TEST_COMMANDS):
                return "Running tests"
            if head.startswith(DELEGATE_COMMANDS):
                return "Handing work to a bot"
    return LABELS.get(name, WORKING)


def elapsed_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    return "under a minute" if minutes < 1 else f"{minutes} min"


def status_text(label: str, seconds: float, waiting: int = 0) -> str:
    text = f"{label} · {elapsed_text(seconds)}"
    if waiting > 0:
        text += f" · {waiting} more waiting"
    return text


def scan(lines, label: str = WORKING, waiting: int = 0) -> tuple:
    """Fold new transcript lines into (label, waiting). Anything that does
    not look as expected is skipped."""
    for raw in lines:
        if not raw or not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind == "queue-operation":
            op = entry.get("operation")
            if op == "enqueue":
                waiting += 1
            elif op in ("dequeue", "remove"):
                waiting = max(0, waiting - 1)
            elif op == "popAll":
                waiting = 0
            continue
        if kind != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                label = label_for(block.get("name"), block.get("input"))
    return label, waiting


# ---- the turn file (written by the hooks) ----

def turn_path(state_dir) -> Path:
    return Path(state_dir) / TURN_FILE


def note_turn_started(state_dir, chats, now: float | None = None) -> None:
    """Called by the prompt hook for a Telegram message from the Owner. A
    turn already noted keeps its start time, so the clock does not jump when
    a second message arrives."""
    path = turn_path(state_dir)
    now = time.time() if now is None else now
    data = read_turn(state_dir, now) or {"started": now, "chats": []}
    seen = [str(c) for c in data.get("chats", [])]
    for chat in chats:
        chat = str(chat)
        if chat.lstrip("-").isdigit() and chat not in seen:
            seen.append(chat)
    data["chats"] = seen[:5]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def note_turn_ended(state_dir) -> None:
    try:
        os.unlink(turn_path(state_dir))
    except OSError:
        pass


def read_turn(state_dir, now: float | None = None) -> dict | None:
    try:
        data = json.loads(turn_path(state_dir).read_text())
        started = float(data["started"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    now = time.time() if now is None else now
    if not isinstance(data, dict) or now - started > MAX_TURN \
            or started > now + 60:
        return None
    return data


# ---- Telegram ----

def api(token: str, method: str, params: dict) -> dict:
    """One Bot API call. The token is used here and nowhere else; it is
    never logged or put in an error message."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise OSError(f"telegram {method} failed ({e.code})") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise OSError(f"telegram {method} failed") from None
    return data if isinstance(data, dict) else {}


def read_token(path) -> str | None:
    """The bot token from the plugin's own file, which exists only while the
    session runs. Read when sending, kept nowhere."""
    try:
        for line in Path(path).read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "TELEGRAM_BOT_TOKEN" and value.strip():
                return value.strip()
    except OSError:
        pass
    return None


class Peek:
    """One per always-on Admin session. `tick()` never raises."""

    def __init__(self, state_dir, enabled: bool = True, token_fn=None,
                 transcript_fn=None, chats_fn=None, api_fn=None,
                 clock=time.time):
        self.state_dir = Path(state_dir)
        self.enabled = enabled
        self.token_fn = token_fn or (lambda: None)
        self.transcript_fn = transcript_fn or (lambda: None)
        self.chats_fn = chats_fn or (lambda: [])
        self.api = api_fn or api
        self.clock = clock
        self._paused_until = 0.0
        self._reset()

    def _reset(self) -> None:
        self.started = None
        self.chats = []
        self.messages = {}       # chat id -> message id of the status
        self.shown = None        # text now in the chat
        self.last_edit = 0.0
        self.last_typing = 0.0
        self.label = WORKING
        self.waiting = 0
        self.path = None
        self.offset = 0

    def tick(self) -> None:
        if not self.enabled:
            return
        try:
            self._tick()
        except Exception:  # noqa: BLE001  # the work matters, not the peek
            self._paused_until = self.clock() + PAUSE_AFTER_ERROR

    def close(self) -> None:
        """The session is ending: take the status message away."""
        try:
            self._finish()
        except Exception:  # noqa: BLE001
            self._reset()

    # ---- inside ----

    def _tick(self) -> None:
        now = self.clock()
        turn = read_turn(self.state_dir, now)
        if turn is None:
            if self.started is not None:
                self._finish()
            return
        if self.started is None:
            self._begin(turn)
        if now < self._paused_until:
            return
        token = self.token_fn()
        if not token:
            return
        self._read_transcript()
        elapsed = now - self.started
        if not self.messages:
            if elapsed < POST_AFTER:
                if now - self.last_typing >= TYPING_EVERY:
                    self.last_typing = now
                    for chat in self.chats:
                        self.api(token, "sendChatAction",
                                 {"chat_id": chat, "action": "typing"})
                return
            text = status_text(self.label, elapsed, self.waiting)
            for chat in self.chats:
                got = self.api(token, "sendMessage", {
                    "chat_id": chat, "text": text,
                    "disable_notification": "true"})
                result = got.get("result") if got.get("ok") else None
                if isinstance(result, dict) and "message_id" in result:
                    self.messages[chat] = result["message_id"]
            self.shown, self.last_edit = text, now
            return
        text = status_text(self.label, elapsed, self.waiting)
        if text != self.shown and now - self.last_edit >= EDIT_EVERY:
            self.shown, self.last_edit = text, now
            for chat, message in self.messages.items():
                self.api(token, "editMessageText", {
                    "chat_id": chat, "message_id": message, "text": text})

    def _begin(self, turn: dict) -> None:
        self._reset()
        self.started = float(turn["started"])
        chats = [str(c) for c in turn.get("chats") or []]
        self.chats = chats or [str(c) for c in self.chats_fn()][:5]
        self.path = self.transcript_fn()
        try:
            # Steps of earlier turns are not this turn's status.
            self.offset = os.path.getsize(self.path) if self.path else 0
        except OSError:
            self.offset = 0

    def _read_transcript(self) -> None:
        if self.path is None:
            self.path = self.transcript_fn()
            if self.path is None:
                return
        try:
            size = os.path.getsize(self.path)
            if size < self.offset:      # replaced or cut: start over
                self.offset = 0
            if size == self.offset:
                return
            with open(self.path, "rb") as f:
                f.seek(max(self.offset, size - _TAIL_LIMIT))
                chunk = f.read(_TAIL_LIMIT)
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return                       # a line still being written
        self.offset = max(self.offset, size - _TAIL_LIMIT) + end + 1
        lines = chunk[:end].decode("utf-8", "replace").split("\n")
        self.label, self.waiting = scan(lines, self.label, self.waiting)

    def _finish(self) -> None:
        messages, self.messages = dict(self.messages), {}
        token = self.token_fn() if messages else None
        self._reset()
        if not token:
            return
        for chat, message in messages.items():
            try:
                self.api(token, "deleteMessage",
                         {"chat_id": chat, "message_id": message})
            except OSError:
                continue
