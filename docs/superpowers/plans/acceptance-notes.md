# Acceptance notes (Task 15)

Date: 2026-11-09. Server: racknerd-5fc4050 (2 vCPU, 2 GB RAM, 4 GB swap), Debian 13.

## Status: STOPPED after step 1 (suite failed on the server). Nothing installed.

## Step 1a: pre-checks - PASS
- `~/ClaudeBot` and `~/DTbyNavid` both empty (no entries) before writing.
- Owner's `claude --dangerously-skip-permissions` process (pid 10522, ~300 MB RSS) was running; not touched. No tmux server socket present.
- Free before the run: 1973 MB total, 534 used, 1439 available, swap 0 used.

## Step 1b: rsync engine/ and tests/ to ~/ClaudeBot/ (no --delete) - PASS
Result: `~/ClaudeBot/engine`, `~/ClaudeBot/tests`. __pycache__ excluded.

## Step 1c: full suite on the server - FAIL (273 pass, 1 fail)
`cd ~/ClaudeBot && python3 -m unittest discover -s tests`: Ran 274 tests in 116.4 s, FAILED (failures=1).
All gpg tests passed on the final code.

Failing test: `test_archive.ScaleTests.test_context_constant_over_years` (tests/test_archive.py:672),
`self.assertLess(time.monotonic() - start, 25)`: archiving 200,000 messages, 5,000 runs and 20,000 items took
72.1 s (first run) and 70.9 s (isolated re-run), against the 25 s budget.

Evidence it is a speed bound and not logic:
- Re-run alone with server load average under 1 (only the Owner's idle claude process): 70.9 s, same failure. Not load noise.
- Same test on the Mac: passes in 17.6 s total (also not far under 25 s).
- The later assertions (row counts, constant context) were not reached, because the timing assertion comes first. Whether they hold on the server is unknown.
- The 2 vCPU VPS disk and CPU are about 4x slower than the Mac for this bulk SQLite work.

Decision for the controller: either relax the 25 s bound for slow hardware (for example an environment variable or 120 s), or confirm the bulk archive is genuinely too slow for the VPS. Per the brief, install was not run; no engine code was changed on the server or in the repo.

## Steps 2 to 5: NOT RUN
Install (`install.sh --start DTbyNavid /home/claude/DTbyNavid`), service/timer checks, idle RSS and memory, the end-to-end Admin run, ~/CLAUDE.md and skills loading checks, Opus availability, cache-creation comparison with the 7,052 baseline, and concurrency measurement were not run.
