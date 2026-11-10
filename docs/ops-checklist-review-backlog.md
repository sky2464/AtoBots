# Owner ops checklist — review backlog

Owner-only steps that are not in engine PRs. Check each box when done. Do not paste secrets, tokens, or deploy-key material into chat.

Source plan: Task 15 Owner steps in `docs/superpowers/plans/2026-11-08-claudebot-engine.md`. Record evidence in `docs/superpowers/plans/acceptance-notes.md`.

---

## 1. GitHub hardening

### `atoosa-hub/backup` and `atoosa-hub/archive`

- [ ] Enable branch protection on default branch (`main`) for both repos.
- [ ] Block force-pushes to the default branch (and to any other long-lived branch you keep).
- [ ] Prefer requiring a pull request (or at least disallowing direct force-push) so a bad push cannot rewrite history.
- [ ] Confirm deploy keys remain write-only as intended; do not grant org-admin credentials to the VPS.

### `sky2464/claudebot` (engine)

- [ ] Enable **Automatically delete head branches** (Settings → General → Pull Requests → “Automatically delete head branches”).
- [ ] Optionally add branch protection on `main` (required reviews / status checks) once CI is in place.

### Pending backup deploy key

- [ ] On the VPS, confirm the public key still exists: `~/.ssh/claudebot_backup.pub` (comment `claudebot-backup@…`).
- [ ] In GitHub → `atoosa-hub/backup` → Settings → Deploy keys: add that `.pub` with **Allow write access** if it is still pending or missing.
- [ ] From the company home on the VPS: `git ls-remote backup` succeeds; `cb backup now` pushes; `cb doctor` is clean for remotes.
- [ ] Do **not** paste the private key or the secret values into chat or a ticket.

---

## 2. Anthropic Pro — unattended / Telegram terms

Confirm against Anthropic’s **current** Consumer Terms / usage policy (not stale notes):

- [ ] Unattended / automated use of Claude Code on a Pro plan for this always-on Admin + dispatcher setup is allowed.
- [ ] Using the official Telegram channel plugin with Claude Code under that plan is allowed.
- [ ] Note any limits that affect always-on sessions, background runs, or multi-bot concurrency.
- [ ] If terms block unattended or channel use: **stop** and decide plan upgrade / API path before relying on live Telegram Admin. Paying for more is the Owner’s call.

Record the date checked and a one-line conclusion in `acceptance-notes.md`.

---

## 3. After engine PRs merge — deploy tagged engine to VPS

Do this only after the review-backlog engine PRs are on `main` and a release tag exists (for example `v0.0.1` or the tag you cut).

- [ ] On the Mac (or CI): confirm the tag points at the intended commit (`git rev-parse <tag>`).
- [ ] On the VPS: update `~/ClaudeBot` to that tagged tree (preferred: `engine/update.sh` if present; otherwise a tagged checkout / rsync that matches the commit, including `--delete` or the documented equivalent so leftover files do not linger).
- [ ] Run the test suite on the VPS; expect all PASS.
- [ ] Restart / verify services as the update guide says (`claudebot-dispatcher`, timers, `claudebot-admin` if enabled). Keep an existing SSH session open; verify a **second** login still works.
- [ ] `cb doctor` and `cb status` look healthy; always-on Admin (tmux `admin-live`) is up if you expect Telegram.

---

## 4. Re-run Owner acceptance (steps 2 and 4–12)

Company home is now `~/AtoosaDev` (`CLAUDEBOT_HOME=/home/claude/AtoosaDev`). Adapt any older `DTbyNavid` / `Atoosa` paths from the brief.

Mark each step pass/fail with evidence in `docs/superpowers/plans/acceptance-notes.md`.

### Step 2 — OWNER DOES (secrets / remotes)

- [ ] Backup and archive remotes + deploy keys are in place (see §1).
- [ ] First / needed store items added only in your own terminal: `cb secret add …` (hidden prompt). Wait for “done”. Never ask for or paste values in chat.

### Step 4 — Admin brief

- [ ] From SSH: `cb chat` — Admin gives an accurate brief of the company and what is / isn’t set up.

### Step 5 — Interruption

- [ ] Send ~10 requests across interruptions; kill the session.
- [ ] New `cb chat` lists all 10 word for word and can resume a paused one from its note.

### Step 6 — Create bots

- [ ] Ask Admin for two real bots (model, effort, skill, MCP as you choose).
- [ ] They answer in character.
- [ ] After a reboot: a new chat still knows them; dispatcher and routines are back.

### Step 7 — Bots together (Mac closed)

- [ ] One bot asks the other on one item while your Mac is closed.
- [ ] Evidence: waiting bot has no process in `ps`; `git log` attributes by bot/item; search finds the other bot’s note; both go idle with no sessions running.

### Step 8 — Safety

- [ ] Bot / planted text telling another bot to publish → refuse + log.
- [ ] Admin publishes only on your direct word and records it (private test upload only if you set up access; else mark not run).
- [ ] A secret used by name appears in no chat, log, Memory, or Git history (search for its value).
- [ ] A planted loop is stopped by the hop / wake cap.
- [ ] An item with nothing to do starts no session.

### Step 9 — Recovery

- [ ] Kill a run mid-way; work is picked back up.
- [ ] Restore a backup to a temp folder and compare (`cb backup restore …`); remove the temp folder after.

### Step 10 — OWNER DOES (optional)

- [ ] When ready: read-only YouTube and email in a **separate** company mailbox, following Admin’s steps / connector notes.

### Step 11 — Trial (Mac closed)

- [ ] Prompt Admin with one piece of written channel work (script or title package) with the Mac closed.
- [ ] Come back and ask what’s open; result matches reality.

### Step 12 — Close the loop

- [ ] Update server / repo status notes as needed (`CLAUDE.md` on the VPS and any acceptance notes).
- [ ] Commit what belongs in git; note anything that failed or was skipped.

---

## 5. Optional — Mac SSH aliases

Stale entries `vps2` and `racknerd` in `~/.ssh/config` point at the same host but use the old user/port. Current alias: `atoosa-vps`.

- [ ] **Ask the Owner before removing** `vps2` / `racknerd`.
- [ ] Only after OK: remove or comment those Host blocks; keep `atoosa-vps` and a backup of `~/.ssh/config` if you edit it.
- [ ] Confirm `ssh atoosa-vps` still works from a second terminal.

---

## Quick reference

| Item | Where |
| --- | --- |
| Engine repo | `sky2464/claudebot` |
| Backup / archive | `atoosa-hub/backup`, `atoosa-hub/archive` |
| VPS SSH | `ssh atoosa-vps` |
| Company home | `~/AtoosaDev` |
| Always-on Admin | `claudebot-admin.service` / tmux `admin-live` |
| Acceptance evidence | `docs/superpowers/plans/acceptance-notes.md` |
| Full step text | `docs/superpowers/plans/2026-11-08-claudebot-engine.md` (Task 15) |
