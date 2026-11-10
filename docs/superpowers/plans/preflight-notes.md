# Preflight notes (Task 1)

Date: 2026-11-08. Server: Debian 13, Claude Code 2.1.319 at `/home/claude/.local/bin/claude`, account type `pro` (from `claude auth status`, login valid to 2026-12-08).

Legend: **[V]** verified (I ran it on the server), **[D]** documented (source URL given), **[U]** unverified.

## Step 1 outcome: UNCLEAR

Unattended `claude -p` on the Owner's Pro plan: not explicitly forbidden for personal use, not explicitly allowed either. Nothing here proposes a plan change; the Owner decides.

Evidence for "works and is a supported use" :
- `claude -p` is a documented, first-class mode (https://code.claude.com/docs/en/headless, fetched 2026-11-08). It lists no subscription restriction for scripts or scheduled jobs. It even documents "a scheduled job" with `--permission-prompts none`. [D]
- It runs on the Pro login on the server: five runs succeeded, `total_cost_usd` is reported but nothing was billed to an API key. [V]
- Legal page: OAuth login is "intended exclusively for purchasers of ... Pro, Max ..." and "designed to support ordinary use of Claude Code". It does not prevent "an end user from signing in to the unmodified Claude Code binary with their own Claude subscription, including where a platform hosts Claude Code". https://code.claude.com/docs/en/legal-and-compliance [D]

Evidence for "risk / unclear":
- Same page: "Advertised usage limits for Pro, Max, and Ultra plans assume ordinary, individual usage of Claude Code and the Agent SDK." A bot that runs many jobs a day may exceed "ordinary, individual usage"; the docs do not define it. [D]
- Consumer Terms (effective Oct 8, 2025, https://www.anthropic.com/legal/consumer-terms, section 3): users may not "access the Services through automated or non-human means, whether through a bot, script, or otherwise" except via an API key "or where we otherwise explicitly permit it". Claude Code `-p` is arguably the "explicitly permitted" case, but no page says so in words. [D]
- Hard NO if the bot serves anyone besides the Owner: "Anthropic does not permit third-party developers to ... route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their users", and credentials may not be collected or intermediated (legal page). Also Agent SDK overview: no claude.ai login or rate limits for products built on the SDK "unless previously approved". [D]
- `--bare` (the docs' recommended scripted mode, "will become the default for `-p` in a future release") never reads OAuth/keychain and needs `ANTHROPIC_API_KEY`. So the docs steer scripted use to API keys. A future default change could break subscription use of `-p`. [D]
- Support page (https://support.claude.com/en/articles/11145838, "Updated over a week ago", no exact date) does not mention `-p`, headless or automation. [D]

Limits that apply [D]: Pro has a 5-hour session limit plus a weekly limit, shared across claude.ai, Claude Code and Desktop (https://support.claude.com/en/articles/11647753). Every bot run consumes the same pool the Owner uses by hand. On limit: wait for reset, enable usage credits, or use a Console API key. Setting `ANTHROPIC_API_KEY` in the environment silently switches Claude Code from the plan to API billing, so keep it unset on the server (currently unset [V]).

Practical cost [V]: a default run on this server loads ~39k tokens of cache-creation even for "Reply OK" (see Step 2). That is what eats the plan, not the prompt.

## Step 2: Memory and cost measurements [V]

| Run | Config | Peak | Cost |
|---|---|---|---|
| 1 | plain `claude -p --model haiku --output-format json "Reply OK"`, sampled `/proc` every 0.1 s (no `/usr/bin/time` on this box) | **283 MB** summed RSS (single-process VmHWM 283,164 kB) | $0.0079, cache_creation 39,004 tok |
| 2 | via `systemd-run ... MemoryMax=1200M` with 1 tool call | cgroup "Memory peak: 192.8M", 13.7 s runtime, 7.9 s CPU | $0.0092 |
| 3 | `--setting-sources project,local --strict-mcp-config` | not sampled | $0.0015, cache_creation 7,052 tok |

Conclusions: one run needs about 0.2 to 0.3 GB. `MemoryMax=1200M` is about 4x headroom, so no revision is needed (800M would also be fine). With 2 GB RAM + 4 GB swap and the Owner's own interactive session (~300 MB; free showed 579 MB used while his session was up), two concurrent bot runs fit; a floor of "one bot run at a time" is safe, two is workable. Swap was 0 B used. Longer tasks with big tool output will use more than these trivial runs; these figures are a floor, not a ceiling.

## Biggest finding: default config loads the Owner's whole account [V]

A default `claude -p` on the server (HOME=/home/claude) loads: 22 plugins, 178 skills, 494 tools, 68 MCP servers (13 connected, 16 failed, 39 needs-auth) and fires `SessionStart` hooks (one failed: `agent-recall-session-start: not found`, exit 127, non-blocking). Plugins sit in `~/.claude/plugins` (incl. a `synced` dir). The run can see and call the Owner's account connectors (see Step 6).

With `--setting-sources project,local --strict-mcp-config`: 0 MCP servers, 18 skills, 23 tools, 4 built-in `cc-plugin-*` plugins, cost down about 5x (7k vs 39k cache-creation tokens). Login still works. Later tasks should use that isolation (or `--bare` with an API key, if that ever becomes the choice) so the bot gets only what we hand it. Add `--disable-slash-commands` only if cwd skills are not wanted (it disables all skills; not tested).

## Step 3: Flags (all on 2.1.319 unless noted)

- `--append-system-prompt-file <file>`: **works** [V]. File content (`The secret word is PINEAPPLE42...`) was followed. NOTE: it is NOT listed in `claude --help` (only `--append-system-prompt <prompt>` is, and the help text for `--bare` mentions `--append-system-prompt[-file]`). Documented at headless page. [V+D]
- `--append-system-prompt <prompt>`, `--system-prompt <prompt>`: in help [D]. `--system-prompt-snapshot on` (default): the system prompt, including appended text, is recorded on the conversation's first request and reused verbatim on every later request and resume, even if a later launch passes different text, until compaction [D, `claude --help`]. So on `--resume`, changing `--append-system-prompt*` has no effect. Not tested.
- `--agents <json-or-file>`: works with inline JSON; agent appeared in init `agents` list (`pfagent`) [V]. With `--print` a file path is also accepted [D, help]. Whether the subagent can be invoked was not tested.
- `--model haiku`: works; resolves to `claude-haiku-5-5` [V]. Bad name: exit 1, JSON `is_error:true`, `api_error_status:404`, `terminal_reason:"api_error"`, `total_cost_usd:0`, result "There's an issue with the selected model (nosuchmodel-xyz). It may not exist or you may not have access to it..." [V]. `~/.claude/settings.json` has `"model":"sonnet"` as default.
- `--effort low`: accepted with haiku, no error [V]. Hook input showed `effort.level:"medium"` for a run without the flag [V]. Whether it changes behavior on haiku was not measured. Levels: low, medium, high, xhigh, max [D, help].
- `--mcp-config <file-or-json>`: loaded; a dummy server pointing at `/bin/false` appeared as `{"name":"pfdummy","status":"failed","source":"dynamic"}` in init `mcp_servers`, and the run continued normally (no hard error) [V]. Invalid entries are skipped and listed in `mcp_server_errors` [D]. `--strict-mcp-config` drops all other MCP sources (0 servers) [V].
- `--plugin-dir <path>`: works; plugin `pfplug` loaded and its skill appeared as `pfplug:pf-plug-skill` [V]. Not restored on resume; pass again [D].
- `--settings <file-or-json>`: works; `{"env":{...}}` was applied (a Bash `printenv` returned the value) and hooks passed via `--settings` ran [V]. Not restored on resume [D].
- `--session-id <uuid>`: works; transcript file name equals the id [V]. A non-UUID gives `Error: Invalid session ID. Must be a valid UUID.` [V]. Reusing an existing id was not tested.
- `--resume <id>`: works from a different cwd (found by id across projects), same session id returned, remembered earlier context ("PINEAPPLE42"), and `total_cost_usd` in the result is the whole conversation's total (0.0102 after two turns), per docs "earlier runs' spend included" [V+D]. Unknown id: `No conversation found with session ID: ...`, exit 1 [V]. `--continue` skips `-p` sessions from the picker but `claude -p --continue` includes them [D].
- Skills in `<cwd>/.claude/skills/<name>/SKILL.md` load in `-p`: **yes**, `pf-cwd-skill` listed, also with `--setting-sources project,local` [V].
- `--output-format json`: single object. Fields seen [V]: `type:"result"`, `subtype` (always `"success"`, even on errors; do not use it), `is_error` (bool), `result` (text), `session_id`, `uuid`, `total_cost_usd`, `usage` {`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `server_tool_use`, `service_tier`, `iterations`, ...}, `modelUsage` {per model: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `costUSD`, `contextWindow`, `maxOutputTokens`}, `num_turns`, `stop_reason` ("end_turn"; "stop_sequence" on API error), `terminal_reason` ("completed"; "api_error" on failure), `api_error_status` (null or HTTP code such as 404), `permission_denials` (array), `duration_ms`, `duration_api_ms`, `ttft_ms`, `fast_mode_state`, `subagent_stats`, `safety_stops`, `credential_expires_at`. `structured_output` only with `--json-schema` [D].
- `--output-format stream-json --verbose`: first event `system/init` has `session_id, cwd, model, permissionMode, agents, plugins, mcp_servers[{name,status,source}], skills, tools, claude_code_version, apiKeySource`; last line is the `result` object above [V].
- Exit codes [V unless noted]: success 0; any in-run failure (bad model, not logged in) exit 1 with the failure printed on stdout as the result (JSON when `--output-format json`); `unknown option` exit 1 (stderr, before the run); not logged in text: `Not logged in · Please run /login` (JSON: `is_error:true, terminal_reason:"api_error", api_error_status:null`); SIGTERM gives 143, no result recorded [D]. Exit 1 alone does not tell failure types apart; read `is_error`, `terminal_reason`, `api_error_status`.
- Stdin gotcha [V]: when run over ssh with no terminal and stdin not redirected, `claude -p` waits 3 s and warns `no stdin data received in 3s, proceeding without it`. Use `< /dev/null` (or pipe the prompt).
- Permission mode: with no mode set, `-p` runs start in Manual and deny anything needing approval (v2.1.302+); tools must be pre-approved with `--allowedTools` (e.g. `Bash(printenv PF_SETTINGS_MARK)` worked) or a `--permission-mode` [V+D].
- Transcript path [V+D]: `~/.claude/projects/<cwd with every non-alphanumeric char replaced by "-">/<session-id>.jsonl`. Examples: cwd `/tmp/pf2` -> `-tmp-pf2`; `/tmp/preflight1.II20` -> `-tmp-preflight1-II20` (dot becomes dash); `/home/claude/ClaudeBot` -> `-home-claude-ClaudeBot`. Names over 200 chars are truncated plus a hash [D]. Override with `CLAUDE_CONFIG_DIR` + `CLAUDE_CODE_PROJECT_DIR_NAME` [D]. A `memory/` subdir appears per project dir. Even failed runs (bad model, no auth) wrote a transcript. `--no-session-persistence` suppresses writes [D]. Retention 30 days by default (`cleanupPeriodDays`) [D]. Transcript format is internal and unstable [D]; prefer the JSON result or hook fields.
- `sudo systemd-run --wait --collect --uid=claude -p MemoryMax=1200M -p RuntimeMaxSec=1800 --working-directory=... --setenv=... claude -p ...`: **works and keeps login** [V]. Requirements found: (1) the command must be an absolute path (`/home/claude/.local/bin/claude`); a bare `claude` fails with `Failed to find executable claude`, even with PATH in `--setenv`; (2) set `--setenv=HOME=/home/claude` (the login lives in `~/.claude`); (3) add `--pipe` to get stdout back to the caller (without it output goes to the journal), and redirect stdin; (4) systemd prints on stderr: `Finished with result: success`, `Service runtime`, `CPU time consumed`, `Memory peak`. Exit code of the unit propagated (0). `sudo` is passwordless for `claude` [V]. cgroup v2 with the memory controller is present [V]. Not tested: what happens on OOM kill or RuntimeMaxSec expiry.

## Usage-limit failure text

Not triggered (instructed not to). **[U] for the `-p` form.** Documented strings (https://code.claude.com/docs/en/errors, fetched 2026-11-08, only the first 100k of 610k chars were readable): `You've hit your session limit`, `You've hit your weekly limit`, `You've hit your Opus limit`, `You've hit your Sonnet limit`; also `Server is temporarily limiting requests`, `Request rejected (429)`, `Credit balance is too low`, `You've hit your monthly spend limit`, and 529: `API Error: Repeated 529 Overloaded errors. The API is at capacity...` (not a usage limit). The page says `-p` prints these in `result` for json/stream-json. By analogy with the verified API failures, expect exit 1, `is_error:true`, `terminal_reason:"api_error"`, probably `api_error_status:429`, but that mapping is a guess. Reset time wording was not seen. Safest detection: match on `is_error` plus a regexp on `result` for `hit your .* limit`, and also log the raw JSON. The `StopFailure` hook (below) gets `error:"rate_limit"` [D]. Tasks that parse this should keep the raw text and treat unknown shapes as "failed, retry later".

## Step 4: Hooks [V where marked; also https://code.claude.com/docs/en/hooks, fetched 2026-11-08]

Hooks configured through `--settings` DO fire in `-p` mode [V]. Both hooks received stdin JSON:

UserPromptSubmit [V]:
`{"session_id","transcript_path","cwd","prompt_id","permission_mode":"default","hook_event_name":"UserPromptSubmit","prompt":"Reply OK"}`
Docs add optional `session_title`. `transcript_path` was `/home/claude/.claude/projects/-tmp-pf2/<session-id>.jsonl`. For a typed `/skill args` prompt the hook gets the line as typed [D].

Stop [V]:
`{"session_id","transcript_path","cwd","prompt_id","permission_mode","effort":{"level":"medium"},"hook_event_name":"Stop","stop_hook_active":false,"last_assistant_message":"OK","background_tasks":[],"session_crons":[]}`
Docs: use `last_assistant_message` rather than reading the transcript (the file may not yet contain the final message at Stop time) [D]. `stop_hook_active` is true when already continuing because of a stop hook; max 8 consecutive continuations [D].

Other [D]: `StopFailure` (runs instead of Stop on API errors) gets `error` in {`rate_limit`, `overloaded`, `authentication_failed`, `oauth_org_not_allowed`, `account_on_hold`, `billing_error`, `invalid_request`, `model_not_found`, `server_error`, `max_output_tokens`, `cloud_credential_error`, `unknown`}, optional `error_details` (e.g. `"429 Too Many Requests"`), optional `last_assistant_message` (e.g. `"API Error: Rate limit reached"`); output and exit code ignored. `SessionEnd` gets `reason`. Exit 0 with JSON on stdout is parsed; exit 2 blocks (UserPromptSubmit: prompt dropped; Stop: Claude continues); other non-zero is a non-blocking error. Stop block form: `{"decision":"block","reason":"..."}`; `UserPromptSubmit` uses the same. SIGTERM runs only `SessionEnd` hooks [D]. `SessionStart` hooks also ran in `-p` [V].

## Step 5: Folders on the server [V]

`~/ClaudeBot`: empty (no entries). `$CLAUDEBOT_HOME`: **empty** (no entries; created 05:19 on Nov 9 server time). Nothing to ask the Owner about. Not read or changed.

Context seen, names only: `~/.claude/projects/` already held `-home-claude-ClaudeBot`, `-home-claude-ClaudeBot-Linux` and later `-home-claude-Linux` (the Owner's sessions; one tmux server socket `/tmp/tmux-1000` appeared during my work). I did not touch these. The Owner had no live claude process when I started.

## Step 6: Models and connectors

Models on Pro [U]: the support page does not list models (https://support.claude.com/en/articles/11145838). Seen working on this Pro login: `claude-haiku-5-5` [V]; default setting `sonnet` (`claude-sonnet-5-5` has an effort setting in `~/.claude/settings.json`). Docs say Max and Ultra have a separate weekly Fable limit [D]; whether Pro can use Fable or Opus was not tested.

Connectors [V for status, U for tool calls]: in an unattended `-p` run launched via systemd-run, the claude.ai account connectors loaded automatically (source `claudeai`): connected: Claude Docs, Cloudflare Developer Platform, Adobe for creativity, Excalidraw, Context7, Microsoft Learn, Sentry, Notion, Linear, Figma; needs-auth: Gmail, Canva. Plugin-provided servers connected: google-calendar, google-drive, shopify (from the small-business plugin). I did not call any connector tool (they act on the Owner's real accounts). So account connectors do reach unattended runs on the server, using the stored login, but only "connected" at startup is proven. Docs: a `-p` run cannot do an OAuth flow; a server needing auth must be signed in beforehand from an interactive session (`/mcp` or `claude mcp login <name>`), otherwise it reports `needs-auth` [D, https://code.claude.com/docs/en/mcp]. A configured `headers.Authorization` that is rejected shows as failed, with no OAuth fallback [D]. Remote URL/header values do not expand credential env names such as `ANTHROPIC_API_KEY`/`NPM_TOKEN`; use a custom variable name [D].

Path where a connector is missing or must not be used: own MCP server via `--mcp-config` with `{"type":"http","url":...,"headers":{"Authorization":"Bearer ${MY_TOKEN}"}}` plus the token stored in a root-readable env file loaded by the unit; `headersHelper` for rotating tokens [D]. Use `--strict-mcp-config` so the bot sees only those servers (this also removes all account connectors) [V].

Risk for later tasks: unless isolated, a bot prompt can reach the Owner's Notion, Linear, Figma, Sentry, Cloudflare and Google Drive/Calendar through the server login. Isolation with `--strict-mcp-config` should be the default.

## Cleanup

Removed all temp files and the three transcript dirs created by my runs (`-tmp-pf2`, `-tmp-pf-other`, `-tmp-preflight1-II20`) from the server. Nothing else changed on the server. Total measurement usage: 5 haiku calls that reached the API (about $0.02 list-price estimate in total) plus 6 free error-path calls.
