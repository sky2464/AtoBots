# Deploy ClaudeBot from a tagged checkout

Ship a known engine revision to the VPS. Prefer a Git tag over an arbitrary
branch tip so the server matches a reviewable commit.

## Version

- Package / CLI version string: `claudebot.__version__` (printed by `cb version`).
- First public tag after the review stack merges: **`v0.0.1`** (create the tag on
  `main` once the hygiene PR lands; do not retag rewritten history).

```bash
git tag -a v0.0.1 -m "ClaudeBot v0.0.1"
git push origin v0.0.1
```

## Deploy to the VPS

On the Mac (or any machine with `<VPS_ALIAS>` SSH access):

```bash
# 1. Check out the tag you intend to ship
git fetch --tags origin
git switch --detach v0.0.1

# 2. Sync engine + tests so the server tree matches the tag
#    --delete removes files that left the repo (required for a true match).
rsync -a --delete \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  ./engine/ ./tests/ \
  <VPS_ALIAS>:~/ClaudeBot/
```

On the server:

```bash
ssh <VPS_ALIAS>
cd ~/ClaudeBot
python3 -m unittest discover -s tests
# Re-link / restart if install layout or units changed (they did in 0.0.1:
# the units now name the account that runs install.sh):
#   engine/install.sh --start "<COMPANY>" /home/claude/<COMPANY>
sudo systemctl restart claudebot-dispatcher
# If always-on Admin is enabled:
#   sudo systemctl restart claudebot-admin
cb doctor
cb version   # expect 0.0.1 (or the tagged package version)
```

Notes:

- Company data lives under `CLAUDEBOT_HOME` (for example `/home/claude/<COMPANY>`), not
  under `~/ClaudeBot`. rsync of the engine must not touch the company home.
- Older deploys sometimes used `rsync` without `--delete`, which left removed
  Python modules on the server. Always use `--delete` for engine/tests.
- Do not rsync `.git` from a detached tag checkout unless you intentionally want
  a full clone on the server.

## Company naming

- **Company (install):** `<COMPANY>` (`$CLAUDEBOT_HOME`)
- **Product / line of business:** `<PRODUCT>`
- Engine docs and templates stay company-agnostic; only Owner-only ops notes name `<COMPANY>`.
