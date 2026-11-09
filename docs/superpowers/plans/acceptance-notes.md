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
