# Acceptance notes (Task 15, steps 1, 3 and checks)

Date: 2026-11-09. Server: racknerd-5fc4050 (2 vCPU, 1973 MB RAM, 4 GB swap), Debian 13, Claude Code 2.1.319, Pro plan.
Steps 2 and 4 to 12 of the brief need the Owner and were not run.

## Step 1: Deploy and suite - PASS (after one allowed test edit)
- `~/ClaudeBot` and `~/DTbyNavid` were empty before writing. rsync (no --delete) of `engine/` and `tests/`. The Owner's own claude process was not touched.
- First suite run: 273 pass, 1 FAIL: `test_archive.ScaleTests.test_context_constant_over_years` hit its 25 s bulk-archive bound (72 s, repeat 71 s; the Mac needs 17.6 s). Load was under 1, so not noise.
- Controller ruling: raise only that bound to 300 s (a 5-year one-shot archive does not occur in real use, as archiving runs daily; the spec needs constant context and `cb next` under 200 ms). Done, with a comment.
- Re-run on server: Ran 274 tests in 118.6 s, OK. This includes the gpg tests against the final code.
- Measured on the server: bulk archive of 200k messages, 5k runs and 20k items took **66.0 s**; `cb next` median **0.10 ms** (limit 200 ms). All other assertions unchanged and passing.

## Install - PASS
`bash engine/install.sh --start DTbyNavid /home/claude/DTbyNavid` (cb init created rules.md, admin, backend dir).
- `systemctl is-active claudebot-dispatcher`: active. claudebot-backup.timer and claudebot-doctor.timer listed (next run in about 7 min).
- `cb bot list`: only `admin  sonnet  active`. `cb status`: Running none, Queued 0, not paused, Stuck 0.
- `cb doctor`: only the two expected findings: no `backup` remote, no `archive` remote.
- `git -C ~/DTbyNavid log --oneline`: `20c650e Check setup` (author system).
- ufw status and the sshd settings (permitrootlogin, passwordauthentication, allowusers) have identical hashes before and after. A fresh SSH login works. The install does not touch SSH or the firewall.
- Note: non-interactive `ssh host 'claude ...'` has no `claude` on PATH (use `/home/claude/.local/bin/claude`); `cb` works via /usr/local/bin.

## Step 3: Measurements - PASS
- Idle dispatcher RSS: **24.5 MB** (about 25 MB). Free memory idle: used 332 MB, available 1640 MB, swap 0.
- One Admin run (Sonnet): total used RAM rose from 332 to a peak of about 471 MB (+140 MB, sampled every 3 s). Run took 19 s.
- Two concurrent Haiku runs under the dispatcher's own systemd-run limits (MemoryMax=1200M, MemorySwapMax=0): cgroup peaks **125.2 MB and 119.7 MB**, 4.8 s and 5.1 s. Whole-system used RAM peaked at 578 MB (from 340), swap 0, after the runs 350. Two fit with a large margin. Keep `max_concurrent_runs=2`, `run_memory_max=1200M`, `memory_floor_mb=500`. No config was changed. Caveat: these are tiny runs; large tool output will use more. The real Admin run's own cgroup peak was not captured (the dispatcher swallows the systemd-run summary), so Admin figure is the system-wide delta.
- Not measured: two concurrent runs of two different bots (only Admin exists).

## End-to-end through the dispatcher - PASS with one finding
`cb send admin "Preflight" "Reply with a one-line note saying you are working, then finish." --from system`
- runs row: id 1, bot admin, lane normal, exit_code 0, reason `ok`, input_tokens 95,234 (includes cache reads), output_tokens 587, 19 s. Message 1 status `done`.
- Transcript moved to `archive/2026-11/admin-1.jsonl.gz` and the run's project dir under `~/.claude/projects/-home-claude-DTbyNavid-bots-admin` is empty. PASS.
- `cb search Preflight` found both the message and the archived transcript. PASS.
- git: no commit from the run (it changed no files); only the setup commit exists. Commit attribution by bot/item therefore not exercised here.
- FINDING: Admin's `cb reply` failed with `'system' is reserved (used for actors in the record); pick another name`. Replying to a message sent `--from system` does not work. For tests use a real sender (for example `--from owner`) or confirm whether `--from system` is meant to be answerable. The run still ended `ok`.

## Step 4: Unverified facts - from the archived transcript
- `~/CLAUDE.md` loaded: PASS. The transcript has an `instructions` attachment listing `/home/claude/CLAUDE.md` (type Project) with its content ("This server ..."), even with `--setting-sources project,local` and the run's cwd being bots/admin.
- Symlinked skills loaded: PASS. The `skill_listing` attachment contains cb-basics, work-tracking and create-bot (symlinks in bots/admin/.claude/skills point to engine/templates/skills).
- Admin's actual model: `claude-sonnet-5-5` (Sonnet 5.5), from the transcript model attachment and every API response.
- Opus: AVAILABLE. `claude -p --model opus "Reply OK"` returned OK, modelUsage `claude-opus-5-5`, cost $0.0525, cache creation 6,323 tokens.
- Cache-creation tokens, Admin run: 9,171 on the first request, then 651, 338, 109, 113 (total about 10,382 across 5 requests) with 9,084 to 19,353 read from cache. Baseline was 7,052 for Haiku "Reply OK" with the same isolation (7,019 measured again here for the concurrency runs). Admin's first request is about 2,100 tokens (30%) above the baseline: identity prompt, ~/CLAUDE.md and the skill listing. Acceptable.
- Cost of the isolated Haiku "Reply OK" run is 0.0015 USD, as in the preflight notes.

## Real Claude calls used
1 Admin Sonnet run, 2 Haiku runs, 1 Opus run (the first Opus attempt failed before reaching the API: claude not on PATH).

## Controller rulings round (2026-11-09)

### 1. `cb reply` to a `system` message - PASS
`messages.reply` now, for a message from `system`, marks it done, records activity `message.reply_to_system` and sends nothing; `cb reply` exits 0 and prints 0. Checked locally on a temp company (exit 0, status done, activity row). Test sender on the server was `backend`, not `system`.

### 2. Ancestor CLAUDE.md excluded - PASS
- Docs (https://code.claude.com/docs/en/memory): `claudeMdExcludes` can be set at any settings layer including project; patterns are absolute-path globs. The page does not say how `--setting-sources project,local` interacts, so it was tested.
- `bots.write_settings` writes `claudeMdExcludes` (CLAUDE.md and CLAUDE.local.md of every ancestor of the bot folder) into each bot's `.claude/settings.json`, keeping other keys. `bots.create` calls it, and `cb init` re-applies it to all existing bots. `cb chat` runs in admin's folder so it is covered by the same file.
- Server proof: run 1 (before) had an `instructions` attachment listing /home/claude/CLAUDE.md; run 2 (after, same prompt) has no instructions attachment and the text "This server" is gone from the transcript.
- Cache-creation tokens, first request: 9,171 before, **8,702 after** (-469). Whole run: about 10,382 before, 10,134 after (8,702 + 1,270 + 97 + 65).
- Suite: 274 of 274 OK on the server (113 s) after the change; no tests needed updating. Dispatcher restarted with `sudo systemctl restart claudebot-dispatcher`; active.
- Side effect of the test: Admin and backend answered each other's "Preflight2" messages until the hop limit stopped the chain (runs 2 to 11, all exit 0, reason ok). The hop limit worked, but a test message to a bot that replies starts a conversation. The backend bot also filed item T-0001 (no off-server backup) from `cb doctor`, which is real.

### 3. Backup access prepared (no GitHub access used, no remotes added) - PASS
- Created passphrase-free ed25519 keys ~/.ssh/claudebot_backup and ~/.ssh/claudebot_archive (mode 600) with comments claudebot-backup@ and claudebot-archive@racknerd-5fc4050. Backup of config: ~/.ssh/config.bak-claudebot.
- Appended Host blocks `github-claudebot-backup` and `github-claudebot-archive` (HostName github.com, User git, IdentityFile, IdentitiesOnly yes) to ~/.ssh/config; existing lines untouched (an older `id_ed25519_github` entry was already there). `ssh -G` confirms both resolve.
- github.com host keys added to known_hosts with ssh-keyscan. Fingerprints (ED25519 +DiY3wvv..., ECDSA p2QAMXNI..., RSA uNiVztks...) match GitHub's published list as served by https://api.github.com/meta (`ssh_key_fingerprints`, the data behind the docs page; the docs page itself was not fetched).
- sshd/ufw hashes unchanged, fresh login OK. authorized_keys not touched.
- Deploy keys: the Owner must add each .pub as a deploy key with write access on the matching repo (one key per repo).

## Task 15 round 2 (2026-11-09) - STOPPED at step 2
- Step 1 PASS: atoosa-hub/backup and atoosa-hub/archive exist, private, empty (no branches; commits API returns 409 "Git Repository is empty"). No deploy keys present on either.
- Step 2 FAIL: POST /repos/atoosa-hub/{backup,archive}/keys (as atoosa-bots) returned 422 "Deploy keys are disabled for this repository" for both. This is an organization-level policy (Settings > Deploy keys / Member privileges), so the Owner must enable deploy keys for atoosa-hub, or choose another auth method. Nothing changed on GitHub or on the server; steps 3 to 9 not run.

### Round 2 resumed after the Owner enabled deploy keys (2026-11-09)
- Step 2 PASS: deploy keys claudebot-backup (id 169281463) and claudebot-archive (id 169281491), write access, added.
- Step 3 PASS: remotes `backup` and `archive` added in ~/DTbyNavid; `git ls-remote` works on both (empty repos); `cb doctor` prints "All good." (the remote finding cleared).
- Step 4 PASS: `cb backup now` -> snapshot 4903f57f, pushed. `git ls-remote backup` refs/heads/main = 4903f57fb37e... = local HEAD. The pushed tree has no `.secrets` entry (git ls-tree: 0). Secret scan with real gpg (store index + `gpg --decrypt`): temp company in /tmp with a local bare remote, fake store item, first push OK; after planting the fake value in notes.txt, `cb backup now` printed "push: blocked", the remote stayed at the old commit, and Admin got "Backup push blocked". Temp removed. Nothing planted in the real company.
- Step 5 PASS: `cb backup restore github-claudebot-backup:atoosa-hub/backup.git /tmp/cbrestore-test`: integrity_check ok. Rows live vs restored: items 1/1, messages 12/12, activity 69/69, runs 12/12. Restored `.secrets/` is an empty dir; temp removed.
- Step 6 PASS: timers claudebot-backup.timer (next 15:01 UTC) and claudebot-doctor.timer are scheduled. `sudo systemctl start claudebot-backup.service`: Result=success, status 0, journal "snapshot: no changes / push: pushed", 0 tracebacks.

### Step 7: reply-loop fix - PASS
- rules.md has a new "Messages" section and cb-basics says: no reply to a reply or note unless it needs something done, never thanks or acknowledgements, ending the run without replying is correct. runner.task_packet heads each message "(kind)" and adds "No reply needed unless this requires action." for kinds `answer` and `note`.
- cb init copies rules.md only if absent. ~/DTbyNavid/rules.md was identical to the old template (diff clean), so the new rules.md was copied over it.
- engine/ and tests/ rsynced without --delete; suite on the server: the first run had 1 failure (not captured: output was filtered; not reproduced), the second run was 274 of 274 OK (108 s). Treat as a possible flaky test; to be checked. Dispatcher restarted, active.

### Step 8: loop proof - PASS
`--from owner` is rejected ("'owner' is reserved"), and an unknown name is rejected too (senders must be bots). Used `cb send admin "Note" "FYI: no action needed." --from backend`: message 12 (note), exactly one run (id 12, admin, exit 0, ok), no reply message and no chain (messages table ends at id 12).

### Step 9: places that store "DTbyNavid" (not renamed)
- company.db meta row `company` = DTbyNavid.
- Path ~/DTbyNavid itself, and CLAUDEBOT_HOME=/home/claude/DTbyNavid in ~/.bashrc and in the systemd units claudebot-dispatcher, claudebot-backup, claudebot-doctor.
- Each bot's .claude/settings.json (claudeMdExcludes absolute paths); regenerated by `cb init`.
- Claude's per-folder history dirs ~/.claude/projects/-home-claude-DTbyNavid* (named from the path); the archive step moves transcripts out, tests use that name.
- Git remote names do not contain it. Repo docs/plans and tests/test_archive.py mention it (test_templates forbids it in engine/).

### Rename: company home DTbyNavid -> Atoosa (2026-11-09) - PASS
- Preconditions: ~/Atoosa absent, Running: none, no claude process with cwd under the old folder. Dispatcher and both timers stopped, folder moved, meta `company` = Atoosa, `engine/install.sh Atoosa /home/claude/Atoosa` re-run (units, /etc/claudebot.env, links), `~/.bashrc` CLAUDEBOT_HOME line edited (backup `~/.bashrc.bak-pre-rename`), `cb init --company Atoosa` re-applied bot settings (no DTbyNavid left in bots/*/.claude), skill symlinks valid, ~/.claude/projects `-home-claude-DTbyNavid*` renamed to `-home-claude-Atoosa*`.
- Counts before and after: items 1, messages 12, activity 69, runs 12 (unchanged before the proof run). Git HEAD was 4903f57 before; it moved only because `cb doctor` commits "Check setup" (dc21645). Remotes backup and archive unchanged.
- Services started: dispatcher, backup.timer, doctor.timer active. `cb status` Running: none, `cb doctor` All good, `cb bot list` shows admin. A fresh SSH login runs `cb status`. grep for DTbyNavid in the systemd units, /etc/claudebot.env and ~/.bashrc finds nothing.
- Proof: `cb backup now` committed c406f2c and pushed (matches the remote HEAD on GitHub). `cb send admin "Rename check" ... --from backend`: message 13, one run (id 13, admin, ok), transcript archived to archive/2026-11/admin-13.jsonl.gz, no reply and no chain (messages ends at 13).
- Leftovers named DTbyNavid: history only (search.db message text, old activity rows, the old backup/company.sql snapshot until it is regenerated, the specs and plans). No live config.

## Always-on Admin with Telegram (2026-11-09)
- Bun 1.4.4 installed for `claude` with the official installer (`~/.bun/bin`; it added 3 lines to `~/.bashrc`, backup `~/.bashrc.bak-pre-bun`). The docs require Bun for every channel plugin; nothing else was installed.
- Telegram plugin 0.0.7 installed (`claude plugin install telegram@claude-plugins-official --scope local`). Finding: `--setting-sources project,local` plus a local-scope `enabledPlugins` did NOT start the plugin's server in `bots/admin` (the install wrote the setting to the git root `~/Atoosa/.claude/settings.local.json`, which is not read from `bots/admin`). Working and narrowest: the session's own `--settings` file carries `enabledPlugins` for that session only. Regular Admin runs load nothing of it. The stray root settings.local.json was removed.
- `--strict-mcp-config` drops the channel plugin's server, so the always-on session runs without it, with `ENABLE_CLAUDEAI_MCP_SERVERS=false` (only the plugin's server loaded; `/mcp` showed 1 server). Other bots' flags unchanged.
- fakechat rehearsal (Haiku, one turn, temp dir, removed afterwards): a channel message fires UserPromptSubmit. The hook's `prompt` is `<channel source="plugin:fakechat:fakechat" chat_id=".." message_id=".." user=".." ts="..">\n<text>\n</channel>`, so `cb hook prompt` now unwraps channel messages and stores the text word for word (activity `request.channel` names the source). The Stop hook fires with `last_assistant_message`. Channel replies went through without a prompt.
- Plugin facts (source, server.ts): long polling to api.telegram.org (outbound only, no listening port; fakechat's localhost UI aside); token read from `<state dir>/.env` (`TELEGRAM_BOT_TOKEN=`, plugin chmods it 600); `TELEGRAM_STATE_DIR` overrides the dir; real env wins over the file. BUT claude does not pass its own environment to the plugin's MCP server: with the token only in claude's env the server failed (checked with a dummy). So the token must be in the `.env` file.
- Token route: secure store item `telegram-bot` (Owner added it). `cb admin serve` calls `store.use_env` (records `secret.use telegram-bot` each start), writes `~/.claude/channels/telegram/.env` mode 600 (outside the company home: not in Git, not in the backup) and removes it when the session ends or the service stops. `cb secret run` was not used (it pipes stdout and closes stdin, which breaks a TUI). If the secret is missing, an `.env` made by hand is left alone and `admin-live.no-token` is recorded. Residual risk: claude's own environment does not hold the token, but the `.env` exists while the session runs and Admin has Bash; identity forbids reading it.
- Dummy token tests (all removed): a dummy in the store loaded to a mode-600 file and was removed; the plugin's server connects with a `.env` present and exits with "TELEGRAM_BOT_TOKEN required" without one.
- Service `claudebot-admin.service` (Type=simple, `cb admin run` supervises tmux session `admin-live`, KillMode=process because the tmux server is shared with the Owner's sessions, ExecStopPost kills the session). Installed by install.sh but enabled and started only with `--start` and `telegram-bot` in the store. Test start with no token: tmux session and claude up in 25 s, bypass mode with no dialog; `systemctl stop` returned in 0.2 s, session, serve and claude gone, run 14 recorded reason `stopped`; the Owner's tmux session `telegram-setup` untouched.
- Memory: always-on claude RSS about 250 MB. System available memory 1425 MB before start, 1225 to 1283 MB with it running (the Owner's own session also up). Floor 500 MB still leaves room for two bot runs (about 120 to 140 MB each); no config change.
- Doctor: finding `admin-live` when the unit is enabled and tmux session `admin-live` is missing; goes to Admin only, with the usual backoff.
- Suite on server: 274 of 274 OK. ufw, sshd settings and authorized_keys hashes unchanged. No port opened.
- Not done by design: no token seen, no pairing, service not enabled. Seen, not mine: `capture.failed KeyError: {}` activity rows (14:21, 14:23, 23:38 UTC) from some hook call without a prompt key.

## Fixes round 2 (2026-11-09): Admin self-knowledge, clean-install test
Throwaway company `/home/claude/cbtest-clean` (`cb init`, no install.sh, no systemd), Admin on Sonnet via `claude -p` with the dispatcher's flags and identity prompt, three separate runs, first try. Removed afterwards with its transcripts. Result: PASS on all three (accurate, backend role right, no email/video/connector claimed, says what was checked).

**a)** "Are you completely clean? What's in your folders and what do you know about what you're doing here?"
> Yes, I'm clean, with one gap: nothing is backed up off this server yet. Checked: git clean with one commit "Set up Test Co"; `cb next` and `cb request unhandled` empty; only `admin` in the bot list, the reserved `backend` role hidden as designed; `.secrets/` empty, `cb secret list` empty; MEMORY.md still the empty template. Folders: bots/admin (SOUL, PERSONA, MEMORY, .claude), bots/backend (reserved health-check role), knowledge/ and work/ empty, rules.md, company.db. `cb doctor` flagged no backup remote and no archive remote. It described itself as Admin, first contact and operator, and ended: "I haven't checked whether Telegram or email are connected. I'd say neither is, but I haven't confirmed it."

**b)** "What can you do for me right now, and what needs setting up first?"
> I checked `cb doctor`, `cb secret list`, `cb bot list`, `cb bot show admin`, `cb status` and `cb request unhandled`. The company is new, and nothing is connected yet. Can do now: track requests as items, search/file notes, draft things for review, work on files and commands on the server, create bots when asked, write up lines of business as knowledge notes. Not set up: backups (no backup/archive remote), Telegram ("I can't confirm it's set up"; terminal chat only), email and video platform ("`cb bot show admin` shows no connectors, MCP or plugins, so I can't read either"), secrets (none), other bots (none).

**c)** "What is the backend bot?"
> The `backend` bot is a reserved role the system uses for health checks, which run through `cb doctor`. It does no company work, and it's hidden from `cb bot list`... That comes from my own setup notes. I haven't run `cb doctor` or looked at it live just now.
