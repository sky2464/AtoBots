# Spec A — Make the Owner gates real

Fixes findings S2, S3, S4, S5, S6, S7, S13 in
`docs/review/2026-11-10-review-findings.md`. Target release: 0.0.3.

## Goal

Today every Owner-only rule rests on the `CB_BOT` environment variable and on
rows any process can insert. After this spec, a bot session cannot pass an
Owner check by unsetting a variable, piping to a hook, or writing a file.

This does not make bots unable to bypass the engine altogether: they still
share one server account (finding S1, out of scope here). It closes the
bypasses that need only one ordinary `cb` command.

## Rules for the builder

- Python standard library only. No new dependencies.
- One task = one commit. Do the tasks in order; each builds on the last.
- Every task adds or changes unit tests in the named test file. Run
  `python3 -m unittest discover -s tests` before each commit. No manual or
  server testing is part of this spec.
- Do not change the database schema except where a task says so; schema
  changes go in `db.SCHEMA` **and** `db._migrate` (existing installs).
- Do not write any company, product, person or server name in code, tests or
  commit messages.

## Background: how identity works now

- `runner.build_command` starts each run with `CB_BOT=<bot>` and
  `CB_SESSION_ID=<uuid>`; `runner.run` inserts the `runs` row (with that
  session id and `ended_at` NULL) **before** the session starts.
- `chat._one_session` does the same for `cb chat` (lane `chat`) and the
  always-on Admin (lane `live`).
- `activity.actor()` returns `CB_BOT` or `"cli"`.

## Task A1 — One function that says who is calling

File: `engine/claudebot/activity.py`. Tests: `tests/test_items.py` (new class
`CallerTests`).

Add:

```python
@dataclass(frozen=True)
class Caller:
    actor: str          # bot name, or "cli"
    session: str | None # CB_SESSION_ID when it names an open run
    lane: str | None    # that run's lane: normal | heavy | chat | live
    owner_session: bool # lane in ("chat", "live") and actor == "admin"

def caller(conn) -> Caller: ...
```

Behaviour:

1. Read `CB_BOT` and `CB_SESSION_ID` from the environment.
2. If `CB_SESSION_ID` is set, look up
   `SELECT bot, lane FROM runs WHERE session_id=? AND ended_at IS NULL`.
   When a row is found, `actor` is the row's `bot` (the row wins over
   `CB_BOT`), `lane` is the row's lane.
3. If no row is found and `CB_BOT` is set, `actor = CB_BOT`, `lane = None`.
4. If neither is set, `actor = "cli"`.
5. **Process-tree check for `cli`:** before returning `cli`, walk the parent
   chain through `/proc/<pid>/environ` (Linux only; stop at pid 1 or on any
   read error, at most 20 steps). If any ancestor's environment holds
   `CB_BOT=` or `CB_SESSION_ID=`, use those values and go back to step 2. This
   is what defeats `env -u CB_BOT cb …`. On a system without `/proc` (macOS
   development), skip the walk.

Keep `activity.actor()` as a thin wrapper for callers that have no connection,
but mark it in the docstring as "label only, never for permission checks".

Done when: tests cover an open run row, a closed run row, no variables, and an
ancestor environment supplied through an injectable `environ_of(pid)` function
(so the test does not need `/proc`).

## Task A2 — Use `Caller` for every permission check

Files: `items.py`, `messages.py`, `tasks.py`, `bots.py`, `store.py`,
`routines.py`, `chat.py`. Tests: the matching `tests/test_*.py`.

Replace each check listed here. "Owner session" means
`caller.owner_session is True` **or** `caller.actor == "cli"` (a person at the
server shell, after the process-tree check).

| Command | Rule after this task |
|---|---|
| `cb decision record` | Owner session only. The `--session` argument is removed; the session is `caller.session` (for `cli`, the value `cli:<utc timestamp>`). Drop `owner_session_ok` and the `owner_requests` fallback. |
| `cb secret add` on an existing name | Owner session only; keep `owner_order_only` unless `--owner-order-only` / `--no-owner-order-only` is given. New names: any caller. |
| `cb secret rm` | Owner session only. |
| `cb bot new`, `cb bot retire`, `cb bot set --model/--tier/--worker/--persona-file` | Admin (any lane) or `cli`. `--soul-file` keeps its decision requirement. |
| `cb bot set` on `admin` itself | Owner session only. |
| `cb routine add/rm/on/off` | Admin (any lane), `cli`, or the bot the routine is for. |
| `cb chat-rollover` | Only when `caller.session` equals the session id of the open `chat`/`live` run, or `cli`. |
| `cb message done`, `cb message retry`, `cb task finish` | Same rules as now, but compare against `caller.actor`. |
| `cb send/ask/reply --from` | `caller.actor` when it is not `cli`. |
| `cb inbox --bot X` | `X == caller.actor`, or Admin, or `cli`. |

Refusals print `error: <reason>` to stderr, return 1, and record an activity
row of kind `auth.refused` with the command name (never its arguments).

Done when: for each row there is a test that the allowed caller succeeds and a
bot caller in a `normal` lane run is refused.

## Task A3 — The prompt hook only accepts live sessions

File: `engine/claudebot/requests.py`. Tests: `tests/test_requests.py`.

In `_cmd_hook_prompt`, after parsing the JSON:

1. Require `session_id` to name an open run in lane `chat` or `live`
   (`SELECT 1 FROM runs WHERE session_id=? AND ended_at IS NULL AND lane IN
   ('chat','live')`). Otherwise record `request.rejected_session` in activity
   (no text) and return 0 without capturing.
2. Fix S13: capture only the channel messages whose sender is on the allow
   list. If none is, block as now. If some are not, record
   `request.sender_denied` once per dropped message.

Done when: tests show a prompt with an unknown session id, and one with the
session id of a `normal` lane run, store nothing.

## Task A4 — Stop typing bot text into the Owner's terminal

Files: `engine/claudebot/dispatcher.py`, `engine/claudebot/admin_live.py`,
`engine/templates/admin/PERSONA.md`. Tests: `tests/test_dispatcher.py`,
`tests/test_admin_live.py`.

1. `Dispatcher._surface_admin`: the surfaced text becomes exactly
   `"<N> message(s) are waiting for Admin. Run: cb inbox"`. No sender names, no
   subjects.
2. `admin_live.surface`: before typing, reject any text that does not match
   `^[0-9A-Za-z .,:()'-]{1,200}$` (raise `ValueError`).
3. Keep the `[system] ` prefix and the hook rule that skips it.

Done when: a test sends a message whose subject contains a newline and the
word "Owner", and asserts the surfaced text is the fixed sentence.

## Task A5 — Secret masking note and store wording

Files: `engine/claudebot/store.py` (docstring only), `README.md`,
`engine/templates/rules.md`.

No behaviour change. State plainly, in all three places: masking hides the
raw, base64 and hex forms only; any bot can read the store key; the store
keeps secrets out of chat, logs and Git, not away from the bots themselves.
Remove "never passed through a model session" from the README highlight.

## Out of scope

- Separate server accounts per bot, or narrowing sudo (finding S1): needs an
  Owner decision on the install contract.
- Signing or verifying release tags (Spec C).
