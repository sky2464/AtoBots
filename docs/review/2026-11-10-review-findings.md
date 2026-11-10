# ClaudeBot review findings — 2026-11-10

Scope: local checkout and the hosted GitHub repository, at `main` = `559d0b4`
(34 commits after tag `v0.0.1`). Read-only review: nothing here is fixed yet.
Private names, the server address and key material are referred to by commit
hash only, so this file does not put them back into the tree.

Severity: **Critical / High / Medium / Low**. IDs are stable; the build specs in
`docs/superpowers/specs/2026-11-10-*.md` refer to them.

| Area | Critical | High | Medium | Low |
|---|---|---|---|---|
| Security (S) | 1 | 4 | 4 | 4 |
| Logic (L) | – | 2 | 7 | 7 |
| Goal drift (D) | – | – | 5 | 3 |
| Leftovers (F) | – | – | 3 | 7 |

## 1. Private names, server and VPS content (the Owner's question)

**Current `main` tree: clean of the company, product and provider names and of
the server address.** One leftover: the repository owner slug in
`docs/branch-cleanup.md:3`. What remains is elsewhere:

| Where | What is there | Evidence |
|---|---|---|
| Git history on `main` | Server IP address and SSH port | added in `485e8bd` |
| Git history on `main` | Two SSH **public** keys with identifying comments (an email address and a host name) | added `485e8bd`, removed `ffdbda0` |
| Git history on `main` | Company, product and hosting-provider names in file contents | 25 commits, `485e8bd` … `2b9ebbf` |
| Commit subjects | Company / product names in the subject line | `09a9d71`, `e1c1a0a`, `79d4861`, `ee827ad` |
| Tag `v0.0.1` (`a1ccdd5`) | Names in 12 files of the tagged tree (README, deploy doc, ops checklist, acceptance notes, design spec, two test files) | `git grep -c` on the tag |
| GitHub PR text | Backup-organisation and provider names in PR #2 body; names in PR #3 | `gh pr view 2`, `gh pr view 3` |
| Commit metadata | A personal Gmail address as author on 116 commits; the Owner's first name on 23 merge commits | `git log --format='%an %ae'` |
| Local checkout only | `backups/` holds two tarballs named after the company (1.4 MB, git-ignored) | `ls backups/` |
| Local checkout only | `refs/backup/pre-split-…` stash ref and 22 stale local branches | `git for-each-ref` |

No private keys, API tokens or bot tokens were found anywhere in history
(searched for GitHub, Anthropic, Slack, AWS and Telegram token shapes and
private-key headers).

Why it matters: `UPDATING.md:3` says customers clone this repository. A clone
carries the whole history, the `v0.0.1` tag and (with access) the PR text. The
repository is private today with no forks, so exposure is limited to
collaborators, but the names cannot be removed by further commits. See S9.

Server-securing content that is still in the tree on purpose: `vps-bootstrap.sh`
and the bootstrap and sshd rules in `CLAUDE.md`. See D1.

## 2. Security

### S1 — Critical — Every bot is root-equivalent
`engine/install.sh:62` refuses to run without passwordless sudo;
`vps-bootstrap.sh:54` grants `NOPASSWD:ALL`; every session runs with
`--dangerously-skip-permissions` (`runner.py:160`, `chat.py:402`). The always-on
Admin takes Telegram messages as input. So one prompt injection in anything any
bot reads, or one compromised Telegram account, is root on the server.
`engine/templates/rules.md:5-9` says so honestly ("working rules, not a
technical lock"), but README and CHANGELOG describe the gates below as security
features. All of S2–S7 follow from this.

### S2 — High — Identity is an environment variable the bot controls
`activity.actor()` (`activity.py:17-20`) and `messages.bound_actor()`
(`messages.py:526-528`) trust `CB_BOT`. A bot that runs `env -u CB_BOT cb …`
becomes the actor `cli`, which every Owner-only check accepts:
`OWNER_DECISION_ACTORS` (`items.py:229`), the session-match rule that is skipped
for `cli` without `CB_SESSION_ID` (`items.py:279-283`), `message done` /
`message retry` (`messages.py:738`, `748`), `task finish` (`tasks.py`,
`_cmd_finish`). The `--from` lock listed under "Security" in the CHANGELOG is
therefore advisory.

### S3 — High — Owner requests and Owner decisions can be forged
`cb hook prompt` (`requests.py:244-291`) reads JSON from stdin from any
process and stores the text as an Owner request with whatever `session_id` the
caller supplies. `items.owner_session_ok()` (`items.py:233-243`) then treats
that session as an Owner session. A dispatcher-started Admin run can pipe a
fake prompt with its own session id, then run `cb decision record --session
"$CB_SESSION_ID"` and pass every check. The decision unlocks owner-order-only
secrets (`store.py:230-239`), Soul changes (`bots.py:265-272`) and pausing
Admin. Forged requests also appear in the opening brief as the Owner's words.

### S4 — High — Bot-written text is typed into the Owner's live session
When the always-on Admin is up, `Dispatcher._surface_admin`
(`dispatcher.py:752-776`) builds a digest that includes each queued message's
`from_bot` and `subject`, and `admin_live.surface()` (`admin_live.py:270-282`)
types it into the tmux session with `tmux send-keys`. Terminal input is the
Owner's voice in that session, the hook deliberately does not capture lines
starting with `[system] ` (`requests.py:261`), and the subject is written by
the sending bot. A bot can therefore put its own words on the Owner's prompt
line. The typed line can also collide with what the Owner is typing.

### S5 — High — Any bot can restart the Owner's chat with its own "summary"
`cb chat-rollover --summary-file F` (`chat.py:325-345`, `601-614`) has no
caller check. While a chat is open, any bot can kill the Owner's `claude`
process and have up to 20,000 characters of its own text placed in Admin's next
system prompt under the heading "Summary built by the engine from the record".

### S6 — Medium — Secure store gaps
- `cb secret add NAME` on an existing name silently replaces the value and
  resets `owner_order_only` to false (`store.py:152-171`); `getpass` falls back
  to stdin, so no terminal is needed.
- `cb secret rm` has no Owner check (`store.py:187-197`).
- `cb secret run` gives any command any non-owner-only secret; masking covers
  only the raw, base64 and hex forms (`store.py:243-247`).
- The key that decrypts the store sits next to the ciphertext
  (`store.py:90-107`), readable by every bot.
- During a live session the Telegram token is plain text in
  `~/.claude/channels/telegram/.env` (`admin_live.py:121-122`).

### S7 — Medium — No authorisation on bot and routine management
Any bot can run `cb bot new`, `cb bot set X --model opus` (a cost escalation),
`cb bot set admin --persona-file F`, `cb bot retire X`, and `cb routine add`
for any bot (`bots.py:491-547`, `routines.py:372-380`). Only the Soul and
pausing Admin are gated.

### S8 — Medium — Updates trust an unsigned remote tag
`engine/update.sh:129-134` fetches tags and checks out the newest `v*` tag,
then runs it with passwordless sudo. Nothing verifies who made the tag. Control
of the GitHub repository is root on every install. `vps-bootstrap.sh:126` pipes
a remote installer into bash.

### S9 — Medium — History exposure (section 1)
Fixing it needs a history rewrite or a fresh repository, re-created tags, and
editing PR #2 and #3. The server IP and the two public keys should be treated
as known to anyone who has ever cloned.

### S10 — Low — The name guard does not run
`tests/test_templates.py:167-171` skips when `CB_BANNED_TERMS` is unset, CI
never sets it (`.github/workflows/ci.yml`), and it scans only `engine/`, not
`README.md`, `docs/`, `tests/` or commit text.

### S11 — Low — Repository and CI settings
Actions are pinned by tag, not commit SHA; the workflow has no `permissions:`
block; it runs twice per PR (`push` and `pull_request`). No branch protection
on `main`, delete-branch-on-merge is off, no LICENSE, no secret scanning, no
GitHub Release for `v0.0.1`.

### S12 — Low — `/usr/local/bin/cb` points into a user-writable folder
`engine/install.sh:85`. If root ever runs `cb`, it runs code the bots can edit.

### S13 — Low — Mixed-sender Telegram prompt is captured whole
`requests.py:263-283` blocks a prompt only when *every* channel message is from
a sender off the allow list; otherwise all of them, including the unlisted
sender's, are stored as Owner requests.

## 3. Logic

### L1 — High — A message that makes no progress is retried forever
`Dispatcher.finish` (`dispatcher.py:308-309`) requeues with
`usage_limit=True`, which gives the attempt back (`dispatcher.py:245-248`).
The docstring (`284-289`) says the opposite: one attempt used, failed after
`retry_attempts`. A question or task the bot never answers is run again up to
the wake cap every hour, with no end. `tests/test_dispatcher.py:386-395` checks
only the first requeue.

### L2 — High — One mention of a token prefix blocks every backup
`backup.scan_sql_dump` (`backup.py:163-182`, patterns `56-64`) refuses the
snapshot when any row contains `sk-ant-`, `ghp_`, `github_pat_`, `xox…` and so
on — bare prefixes. If the Owner or a bot ever writes one in a request, message
or activity row, it stays (the activity table is append-only) and every hourly
backup fails from then on. The dump with the hit is also left in the working
tree (`backup/company.sql`) and a later `commit_sweep` from `cb init` or an
update commits it without this check.

### L3 — Medium — `update.sh` waits for idle without stopping new runs
`engine/update.sh:214-224` polls `cb dispatcher idle` for 30 minutes while the
old dispatcher keeps starting runs, then restarts regardless. `cb init` and
`cb templates refresh` (`205-208`) run on new code before the old dispatcher
stops. The Admin unit is restarted but never re-rendered (`226-244` cover the
dispatcher, backup and doctor units only).

### L4 — Medium — Updates overwrite the company's `rules.md`
`cli.refresh_engine_templates` (`cli.py:86-107`) copies the template over
`<home>/rules.md` and replaces bundled skills on every update, while
`UPDATING.md:146` says an update does not rewrite `rules.md`. An Owner's local
rule edits are lost unless the company Git history has them.

### L5 — Medium — Over-long identity files lose their top
`bots._read_capped` (`bots.py:341-348`) keeps the last N bytes. That suits
Memory; for `SOUL.md` and `rules.md` it drops the opening rules first. The
rules cap is a hard-coded 4 KB (`bots.py:368`) and the warning goes to stderr
on every run.

### L6 — Medium — "Owner messages bypass wake caps" is dead code
`dispatcher.py:710` tests `from_bot == "owner"`, but `owner` is a reserved name
(`bots.py:28`) and `_insert` requires an existing bot (`messages.py:560-561`),
so no such message can exist.

### L7 — Medium — Waiting items are treated as stalled
`Dispatcher._stall_doing` (`dispatcher.py:531-562`) reverts any `doing` item
older than the lease to `todo`, including items waiting on another bot or on
the Owner, and counts it toward the "keeps stalling" alert.

### L8 — Medium — `cb hook stop` exits 2 instead of doing nothing
The CHANGELOG says sessions started by an older engine may still call it and
that it does nothing. The subcommand is gone: `cb hook stop` exits 2, which a
Claude Code Stop hook treats as a blocking error.

### L9 — Medium — No schema version
`db._migrate` (`db.py:113-146`) probes columns on every `cb` call; there is no
version number in `meta`, so a rollback cannot tell whether an older engine can
open the database (`UPDATING.md` asks the operator to judge).

### L10 — Low — No indexes for the 5-second scans
`messages(status, to_bot)`, `items(status)`, `runs(bot, ended_at)` and
`activity(kind, ref)` lookups in `_start_ready`, `_wake_assigned` and
`_stall_doing` run every tick with per-row subqueries.

### L11 — Low — Central Time is baked in
Default zone and the literal label `CT` (`config.py:77`, `db.py:81`, `96`);
templates and help text say "Central Time" whatever `timezone` is set to
(`templates/admin/PERSONA.md:12`, `templates/backend/PERSONA.md:10`,
`templates/skills/cb-basics/SKILL.md:70`, `routines.py:415`).

### L12 — Low — Settings that ignore `config.json`
`claude_projects_dir` uses the caller's home, not `run_home` (`config.py:88`);
the Admin unit has `MemoryMax=1200M` fixed (`claudebot-admin.service:12`);
`ADMIN_MODEL` ignores `default_model` (`cli.py:21`); the secret name
`telegram-bot` is fixed in `install.sh:132`.

### L13 — Low — Chat memory limit can silently be absent
`chat.memory_wrap` (`chat.py:405-421`) returns the bare command when there is
no user systemd manager. The Admin unit's limit does not cover the session when
the Owner's own tmux server already exists.

### L14 — Low — Wake-cap alerts cost a run
`_wake_capped` (`dispatcher.py:727-732`) sends Admin a message per capped bot
per hour; each one is a paid Admin wake.

### L15 — Low — Pre-update database copies are never pruned
`engine/update.sh:172-195`.

### L16 — Low — Small defects
Broken help sentence (`routines.py:417-419`); `Config.load()` twice in
`routines._cmd_add`; unused `cfg` in `items.pause`; `--effort` is not validated
in `cb send`; `cb inbox --bot X` shows any bot's inbox; `items.archived_at` has
six readers and no writer.

## 4. Drift from the project goal

The design spec describes a reusable engine that "knows nothing about any one
business", with one Admin the Owner talks to.

### D1 — Medium — Server hardening lives in the engine
`vps-bootstrap.sh` (159 lines: users, sudoers, ufw, fail2ban, sshd, swap,
installing Claude Code) and the bootstrap and sshd rules in `CLAUDE.md` are one
Owner's server setup, not engine code. README lists it in the layout.

### D2 — Medium — First-company wording in generic templates
`engine/templates/rules.md:24-27` (email, "editing or deleting videos", "the
video platform"), `templates/admin/PERSONA.md:65`, and "he / him / his" for the
Owner throughout the prompts (`admin_live.py:48-84`, `chat.py:367-378`,
`rules.md`, both personas).

### D3 — Medium — The deploy story contradicts itself
`README.md:126` and `docs/deploy.md` say rsync with `--delete`;
`UPDATING.md:6` and `engine/update.sh:2-3` say never rsync, a clone is
required.

### D4 — Medium — Security wording is ahead of the implementation
README "Secure secret store … never passed through a model session" and the
CHANGELOG "Security" list describe gates that S2, S3 and S6 show are advisory.
The audience also moved from one Owner to "customers" (`UPDATING.md:3`) without
the trust model changing.

### D5 — Medium — Version and release drift
Tag `v0.0.1` is at `a1ccdd5`; `main` is 34 commits later and still reported
0.0.1; `CHANGELOG.md` said "0.0.1 (not yet tagged)" and its text differs from
the changelog inside the tag. (Addressed by the 0.0.2 release.)

### D6 — Low — Scrub placeholders left in README
`README.md:140-145` "First company install (ops)" with `<COMPANY>` and
`<PRODUCT>`; `docs/deploy.md` "Company naming".

### D7 — Low — README command table is out of date
Missing `usage`, `task`, `message`, `templates`, `log`; lists `check` as a
top-level command; the layout omits `update.sh`.

### D8 — Low — Tier and caps by substring
Worker tier and wake caps are decided by whether the model id contains
`haiku` / `opus` / `sonnet` (`bots.py:131-140`, `dispatcher.py:695-705`).

## 5. Files and code that should not be there

| ID | Sev | Item | Suggested action |
|---|---|---|---|
| F1 | Medium | `docs/branch-cleanup.md` — stale, and the only file with the owner slug | Delete |
| F2 | Medium | `docs/deploy.md` — obsolete rsync flow | Delete; UPDATING.md is the deploy doc |
| F3 | Medium | `backups/*.tar.gz` in the engine checkout (local, company data, company name in file names) | Move out of the checkout |
| F4 | Low | `docs/hygiene-leftovers.md` — internal PR notes | Turn into issues, delete |
| F5 | Low | `docs/superpowers/plans/*` and `preflight-notes.md` (about 1,050 lines of finished build plans) | Move to an internal archive |
| F6 | Low | Untracked `AGENTS.md` — a copy of `CLAUDE.md` with "Claude" replaced by "Codex", including the wrong `NEW_USER=Codex` | Delete, or commit a corrected one |
| F7 | Low | Untracked `docs/superpowers/plans/2026-11-10-name-scrub.md` — already carried out by PRs #28–#30 | Delete |
| F8 | Low | 22 local branches with deleted remotes, `refs/backup/pre-split-…`, local-only `integrate/review-fixes` (19 commits) and `feat/cost-control-v0.0.1` (8 commits) | Confirm the work re-landed through PRs #11–#27, then delete |
| F9 | Low | `tests/__init__.py` — empty; commit `813dd94` said it was dropped, `4a8522b` brought it back | Delete |
| F10 | Low | Dead code: the `owner` branch in `_owner_bypasses_cap`; the unused `lane` argument of `runner.build_command`; `runner.LEVEL_ORDER` duplicating `messages.LEVELS` | Remove |

## 6. Improvements worth building (detail in the specs)

1. Make the Owner gates real: session-bound identity, a hook that only
   accepts live sessions, no bot text on the Owner's prompt line (S2–S7).
2. Fix the two reliability bugs that cost money or lose backups (L1, L2), and
   the stall and cap logic (L6, L7, L14).
3. A safe update path: pause the dispatcher, re-render every unit, keep the
   Owner's `rules.md`, add a schema version, verify the tag (L3, L4, L8, L9, S8).
4. An engine-only repository: move server hardening and internal notes out,
   neutral templates, a name guard that runs in CI, a clean history before any
   customer clones (S9–S11, D1–D3, D6, D7, F1–F10, L11).
5. Narrow sudo to the few commands the engine needs (S1). This one changes the
   install contract and needs an Owner decision first.
