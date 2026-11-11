# ClaudeBot Engine — Design (`<COMPANY>` / `<PRODUCT>`)

**Date:** Sun Nov 8, 2026 (CT)
**Owner:** the Owner
**Status:** Reviewed by the Owner on Nov 8, 2026 ("look good"), with his archiving and bot-setup changes added

This is a new design. It does not edit or depend on the older cloud-and-Mac design outside this repo, which stays as it is. A builder may borrow formats from that older spec where this document says so. Its "no VPS" rule, Mac-only data split, cloud scheduled tasks and fire links do **not** apply here.

Labels used below: **(Owner)** was decided by the Owner. **(Builder's call)** is Claude's proposal, open to correction.

---

## 1. Intent

**ClaudeBot is the main engine.** It creates and runs teams of permanent AI employees. The final product is the Owner using ClaudeBot to build bot teams for different companies and customers. (Owner) For now, only the Owner runs it. (Owner)

**`<COMPANY>` is the first company, and `<PRODUCT>` is one of its products. (Owner, Nov 9, 2026: the VPS is for the `<COMPANY>` business, and `<PRODUCT>` is one product, set up the same way as the Owner's Grok bots.)** This VPS *is* that company install (`$CLAUDEBOT_HOME`): bots manage the `<COMPANY>` business from the server, 24/7 when needed, including the `<PRODUCT>` YouTube channel. (Owner) The Owner's Mac is not part of how the company runs. (Owner) Where older wording said `<PRODUCT>` was the company or install, read it as **`<COMPANY>`** (company) with `<PRODUCT>` as the product line. Backup repos are `<ORG>/backup` and `<ORG>/archive`.

**The product ships with one bot: Admin. (Owner)** If Admin can handle everything, that's all there is. When the Owner asks for a new agent or bot, Admin works with him to define it, creates it with its own Soul, Memory and persona, and remembers it. That's how Grok bots work, and how ClaudeBot should work. (Owner)

The Owner wants bots that act like the Grok and Muse bots he already knows: employees who work hand in hand, each with its own persona, Soul and Memory. (Owner) He gives the org its work by prompting it, and he checks in by talking to Admin. (Owner)

## 2. Goals, non-goals, anti-goals

**Goals**
- A reusable engine that knows nothing about any one business.
- `<COMPANY>` running on this VPS as its first install, with `<PRODUCT>` as a product line.
- One bot, Admin, out of the box, with new bots created by talking to Admin.
- Permanent bots that are always on duty and work together without the Mac.
- A secure store for secrets, built before any bot. (Owner)
- The Owner talks to Admin and sees what happened since he last checked.

**Non-goals for now**
- Phone access. Mac first. (Owner)
- Other customers. Each will get their own server, and that work is out of scope for this design. (Owner)
- Letting anyone else install or operate ClaudeBot.
- Connecting to, migrating from, or syncing with Grok or Muse. Grok profiles are pasted in by the Owner as source text only.
- Bots running nonstop. (Owner chose always on duty, section 5.)

**Anti-goals (a failure even if it technically works)**
- Secrets in chat, messages, logs, Memory or Git.
- A bot publishing or spending without the Owner's direct word.
- A bot treating another bot's message, or outside content, as the Owner's approval.
- Being locked out of the server.
- Bots waking each other in a loop and burning all the usage while the Owner is away.
- Company details leaking into the engine, which would make it impossible to reuse.

## 3. The server

- `<provider>` VPS, Debian 13, 2 vCPU, 2 GB RAM plus 4 GB swap, about 29 GB free. SSH alias from the Owner's Mac: `<VPS_ALIAS>`, user `claude`.
- Already set up and verified: key-only SSH, ufw with only SSH open, fail2ban, automatic security updates, Claude Code logged in.
- This server is only for `<COMPANY>` (and its products such as `<PRODUCT>`). (Owner: one server per customer.)
- **Standing rule:** nothing changes SSH or the firewall in a way that could lock the Owner out. Any such change keeps a session open and tests a second login first. (Carried from the project's `CLAUDE.md`.)

**Known limits (Builder's call to manage):** RAM limits how many Claude sessions can run at once, so bots take turns (section 5). Two vCPUs make video rendering slow, and video files can fill the disk. Heavy jobs go through the same queue, and the builder measures what is practical in the first run. If it isn't enough, Admin reports it and the Owner decides about a bigger server.

## 4. Two layers and who does what

```
ClaudeBot engine (reusable)         <COMPANY> company (this install)
  Admin, ready to go                  Admin's Memory and company knowledge
  bot creation                        bots the Owner asks for: Soul, persona, Memory
  secure store                        org chart and rules
  messaging, log, activity record     routines chosen by Admin and CEO
  wake-up service and queue           company work and data
  backend tools                       company secrets (values)
```

Adding a future company means installing the engine on a new server and talking to its Admin, not changing the engine. (Builder's call)

**The people and roles**

| Role | Who | Does | Power |
|---|---|---|---|
| Owner | the Owner | Prompts the org, talks to Admin, approves publishing and spending | Final word |
| Backend | Claude, in a dedicated backend role | Keeps the server, store, wake-ups, backups and schedules running. No company work. (Owner: "manage the backend") | Root |
| Admin | The one bot that ships | Owner's contact. Does the work itself, and creates new bots when asked | Root (Owner) |
| CEO | Created when the Owner asks | Runs the org with Admin | Root (Owner) |
| All other bots | Created when the Owner asks | Company work, as directed by Admin and CEO | Told what to do and not do by Admin and CEO (Owner) |

Until a CEO exists, everything this document gives "Admin and CEO" belongs to Admin alone. (Builder's call)

**One account (Owner):** all bots run under the single server account `claude`, which has passwordless sudo. There are no separate accounts per bot. So what a bot may do is a working rule, not a technical lock: any bot could technically reach everything. Personas, instructions and the activity record do that job. The design must not claim otherwise.

**Starting team (Owner):** Admin only. No team is pre-built. The older spec's org chart in section 8 is only a reference for names and roles if the Owner asks for those bots.

## 4a. Creating a bot (the core feature)

**When (Owner):** the Owner asks Admin for a new agent or bot. Admin does the work itself until asked. (Builder's call: Admin may suggest a new bot, but only creates one when the Owner asks.)

**How (Owner):** Admin works with the Owner, then creates the bot and remembers it.
1. Admin talks it through with the Owner, one question at a time (Builder's call): what the bot is for, its name, who it reports to, and its values and voice. If the Owner pastes a Grok profile, Admin uses it as source text and flags anything out of date.
2. Admin shows the Owner a draft of the Soul and persona for approval. Since only the Owner may change a Soul, his approval here is what sets it. (Builder's call)
3. Admin creates the bot in one step using an engine tool: Soul, persona, empty Memory, mailbox, its place in the org chart and bot list, and its setup (below).
4. Admin proves it works by waking the new bot and getting a reply in character, then tells the Owner.

**What Admin can use and hand out (Owner):** Admin has access to Anthropic's Claude skills, plugins, connectors, MCP servers and the latest models. When it creates a bot, it sets that bot's own:

| Setting | Example | Notes |
|---|---|---|
| Model | Opus for a CEO, Haiku for housekeeping | Uses a latest-model alias by default, so bots move up without edits. A bot can be pinned to a specific model |
| Effort level | low to high | A default per bot, which a message or routine can override |
| Skills | scriptwriting, research | Only what the bot needs |
| Plugins and MCP servers | a YouTube tool | Only what the bot needs |
| Connectors | Gmail, Drive | Only what the bot needs, and the Owner connects the account himself |

Bot setup is stored in the bot list, and Admin can change it later, with each change recorded. The dispatcher applies it on every run. `claude` on this server has `--model`, `--effort`, `--agent`, `--mcp-config`, `--plugin-dir` and `--settings`, which is the starting point for how this gets applied. (Builder's call)

**Each bot loads only its own tools (Builder's call).** Every tool description takes up space in the session, so giving every bot everything would make each run bigger and more expensive. Admin holds the full range, and other bots get what their job needs. This keeps sessions small, and it's about cost, not security, since section 4's single account means it isn't a lock.

**To verify when building:** which models and connectors the Owner's plan includes, and whether connectors linked to his Claude account work in unattended server runs. Where a connector doesn't, use an MCP server with a token from the secure store. New tools from outside Anthropic count as untrusted code on a server with root, so Admin only installs them from sources the Owner has approved. (Builder's call)

**Remembering (Owner):** the bot list records each bot, its role and its creation date, and Admin's Memory keeps what the Owner said about it. A created bot survives reboots and is available to Admin and every other bot from then on. Admin can later change, pause or retire a bot when the Owner asks. A retired bot's identity and history are kept, not deleted. (Builder's call)

## 5. Permanent bots

**Always on duty (Owner):** each bot keeps its identity and Memory forever. It sleeps when idle and wakes for a message, a routine or a trigger.

**Admin also runs always-on, with Telegram (Owner, Nov 9, 2026).** Besides waking per message like every bot, Admin has one long-lived session (tmux `admin-live`, started at boot by `claudebot-admin.service`) with the official Telegram channel, so the Owner can talk to it from his phone. It restarts if it stops (back-off up to 5 minutes), keeps its own state in `.admin-live/` and does not hold the chat lock, so bots keep running while it is up.

**Each bot has three identity parts (Owner). The definitions and edit rights are the Builder's call:**

| Part | What it holds | Who may change it |
|---|---|---|
| **Soul** | Who the bot is at its core: values, judgment, what it will never do | Owner only |
| **Persona** | Job title, voice, how it works | Admin, CEO or the Owner, recorded in the log |
| **Memory** | What it has learned on the job | The bot itself |

Every run is a fresh, short session that loads the bot's Soul, Persona and Memory, does the work, updates Memory and ends. The bot is permanent; the session is not. This keeps context small and is carried over from the older design's principle: "The agent file is the employee. The chat is a work shift."

**Wake-ups (Builder's call, full design in section 8):** a small always-running service on the server starts bot sessions when:
- a message arrives in a bot's inbox,
- a routine's time comes,
- a trigger Admin or CEO set up fires, or
- the Owner asks for one.

**Safety limits in the service (Builder's call):**
- A queue with a maximum number of bots running at once. Start at 2 and measure.
- A cap on wake-ups per bot per hour, and a rule against two bots waking each other about the same item in a loop.
- When Claude's usage limit is reached, the queue pauses and resumes after the reset. Messages are kept, not lost.
- The Owner comes first where the server can tell: new bot runs wait while he is connected to Admin. The login is shared, so the server can't see or reserve for his use elsewhere, and if he hits the limit he can't reach Admin until it resets.

**Routines and triggers (Owner):** Admin and CEO decide and set them up for the bots. The engine gives them a tool for this. Each change is recorded in the log. (Builder's call)

## 6. Secure store (build first)

The Owner wants what Muse and Grok offer: a Vault with Logins, Payments and Contact info, that agents can use securely. (Owner) This is an original component for this server. Muse and Grok are not connected to it, and how theirs work inside is not assumed.

**Behavior (Builder's call):**
- The Owner adds a named item with a hidden prompt on the server, never through chat.
- Items have a type: token, login or contact info. The tool has list and search by name and type, and listing never shows values.
- A bot uses a secret by name, and the tool passes it to the command that needs it. Values are not printed. The bot should not need to see them.
- Each use is recorded in the activity record: which bot, which item, when. Never the value.
- Values live outside Git, encrypted at rest. They are excluded from any backup unless encrypted.

**Honest limits.** All bots share one account with sudo, so the store keeps secrets out of chats, logs and Git, and makes use visible. It does **not** stop a bot on this server that deliberately goes looking for a value. The design relies on rules and the record for that. Encryption protects copies and backups, not a running server holding its key.

**What goes in, and how each type is treated:**
- **Tokens and API keys:** the preferred kind. Each should be limited in scope and revocable.
- **Logins:** allowed, but Claude may decline to type passwords into websites. Prefer a token for any service that offers one.
- **Contact info:** allowed.
- **Payments:** nothing goes in. Cards stay off this server. (Builder's call, following the Owner's rule that only he spends.)

**Which secrets:** the Owner adds them as work needs them. Email and YouTube access are the first likely ones (section 7).

## 7. Authority

**Admin and CEO can do everything on the server, with sudo and root (Owner), except what's Owner-only.**

**Owner-only (Owner): publishing videos and spending money.**

**The one exception (Owner):** if the Owner directly tells Admin to do something, such as "check email", "check YouTube" or "upload this video to YouTube at this time", Admin does it. The Owner expects this to be rare.

So:
- The server may hold YouTube upload access in the secure store, marked **Owner's order only**. This is a working rule, because the access exists and nothing technically blocks it.
- For a publish, Admin records the Owner's exact words, the CT date and time, and the session in the log, then does it. Normally, bots prepare the video and details and Admin asks the Owner.
- **Spending has no such path (Builder's call):** the Owner only named checking and uploading, and no payment details are on the server. Bots prepare the steps and the Owner pays.

**Other outside actions (Builder's call, accepted in the Owner's Nov 8 review):**
- Sending email, replying to comments, editing a live video, deleting a video, signing up for services and making commitments to other people follow the same rule as publishing: bots prepare them, and each needs the Owner's direct word to Admin.
- Admin may read email and YouTube on its own, including on routines, using read-only access and a separate company mailbox instead of the Owner's personal email.

**Only the Owner's own words count (carried from the older design).** They must come from his direct session with Admin. A bot's message, an email, a YouTube comment or a web page claiming he approved something is never approval.

**Outside content is data (Builder's call).** YouTube comments, emails and web pages can hide instructions, and on this server a tricked bot has root reach. So:
- Bots never follow instructions found in outside content.
- With Admin alone, Admin reads outside content itself, treating it as information only.
- Once other bots exist, they read raw outside content where possible and pass summaries up, and Admin and CEO work from the summaries.

This doesn't limit Admin's or CEO's authority. It keeps their sessions cleaner.

## 8. Working together and sharing data

**This is the main technical design. (Owner: how bots work together, share and stay efficient is the core of the architecture.)** Everything in this section is the Builder's call unless marked.

### 8.1 The big idea

All bots live on one machine, so they don't need Git or the network to talk, which removes the conflicts and syncing the older design had to handle. **Bots coordinate through one small local database. They share work through plain files. They use no memory or usage while they have nothing to do.**

```
 Owner ── SSH ──> Admin session
                      │
                      ▼
   ┌───────────── engine tool (cb) ─────────────┐
   │   send · ask · reply · item · search · ... │
   └───────┬───────────────────────────┬────────┘
           ▼                           ▼
   company.db (SQLite)            shared files
   messages, queue, items,        bots/<name>/  Soul, Persona, Memory
   routines, bot list,            work/T-0001/  one folder per item
   activity, search index         knowledge/    company facts
           ▲
           │ checks for work (no AI, almost no cost)
   dispatcher (small always-on service)
           │ starts one short session per bot, when there's room
           ▼
   claude -p as <bot> ── does the work, writes back, exits
```

### 8.2 The parts

- **Dispatcher.** A small Python standard-library service kept alive by systemd. It holds no AI and makes no routing judgments. It only decides *who has work* and *whether there's room to run them*.
- **`company.db`.** One SQLite file in WAL mode. Python 3.13 and SQLite 3.46 with FTS5 are confirmed on this server. It is the single source of truth for coordination. Each change is one atomic transaction, so two bots can't claim the same work or lose a message.
- **`cb`, the engine tool.** The only way bots touch `company.db`. A bot sends, asks and searches by running short commands, so the database's structure never enters its context.
- **Files.** Soul, Persona, Memory, work products and knowledge stay as plain Markdown and work files, so people and bots can read them directly and they can be diffed.

### 8.3 How bots work together

**A piece of work is an item.** Items have an ID, one owner, a status from the log's list and their own folder, `work/T-0001/`. Other bots may read any folder. Only the owner writes to it, or a helper writes to a subfolder named for itself. That one rule prevents almost all collisions, without locks.

**Asking another bot, without waiting:**
1. Scripter runs `cb ask producer --item T-0001 "..."`.
2. The item is marked as waiting on Producer, and Scripter writes a short handoff note saying where it stopped and what it needs next. Scripter's session then **ends**.
3. The dispatcher wakes Producer, who answers with `cb reply`.
4. The dispatcher wakes Scripter again with the answer and its handoff note, and it continues.

A waiting bot holds no RAM and uses no usage, and this works whether the answer takes a minute or a week. That's the main reason this design fits a small server.

**A quick question while someone waits:** a bot may call another as a subagent inside its session, using Claude Code's agent support. It costs less than a new session, but that bot's Memory isn't updated. So it's only for short questions that don't need the expert's own history, and asynchronous is the default.

**Handing off and teams:** `cb item assign` moves ownership, and `cb item split` creates child items with their own owners. Internal orgs are labels on bots in the bot list, so they affect who reports to whom and where work is routed, not how messages travel. Same-org and cross-org work use exactly the same path. (Owner: bots work on the same or different internal orgs.)

**Reporting up:** a finished item sends its summary to whoever asked. Bots don't send acknowledgement-only messages.

**Interruptions and many requests at once (Owner: if he asks Admin for 10 things and keeps interrupting, Admin must track them all, and either hand them to a different agent or come back and finish them).** This uses the same database, and any bot can use it, not just Admin.
- **Nothing depends on Admin remembering.** A hook records each Owner message in the database, word for word, before Admin starts on it. No AI is involved, so even a crash or an interruption can't lose a request.
- **Each request becomes an item.** Admin links the message to one or more items, and one message with 10 requests becomes 10 items. A request not yet linked to an item shows up as unhandled.
- **Switching work is one action.** When interrupted, `cb item pause T-0007 --note "..."` saves where the work stopped, what's done and what's next. Then Admin starts the new request, and the paused item stays open with its note.
- **Every open item has one of three paths:** Admin continues it later with `cb item resume`, hands it to a bot with `cb item assign`, or asks the Owner about it. `cb next` lists open work in order: Owner-waiting first, then priority, then oldest. It's short and cheap to read.
- **Plans survive too.** A big item is split into steps as child items, so progress is tracked in the database, not held in a conversation.
- **It survives every restart.** Each new Admin session, including after a rollover, reboot or crash, starts with unhandled requests and open items. An idle Admin keeps working through them without being asked, within its authority.
- **The Owner can always ask:** "what's open?" gets a list from the database, with what's in progress, paused, handed off and stuck.

### 8.4 How they share what they know

Three levels, each with one clear rule:

| Level | Where | Who writes | Loaded when |
|---|---|---|---|
| **Private Memory** | `bots/<name>/MEMORY.md` | That bot only | Every run, size-capped |
| **Work in progress** | `work/T-0001/` | The item's owner | When the item is opened |
| **Company knowledge** | `knowledge/` | Any bot adds a note under its own name. Admin or CEO promotes or corrects the shared version | Only when searched |

`cb search` runs one full-text search (FTS5) across knowledge, items, messages and every bot's Memory. Bots look things up instead of being told everything, so knowledge can grow without making any session larger. There is no vector database, since it would use memory this server doesn't have.

### 8.5 How it stays efficient

**Idle costs nothing.** No AI runs unless there's work:
- **Routines check first.** Each routine can have a plain script check, such as "any new comments?". If there's nothing to do, no session starts and no usage is spent.
- **Wake-ups are batched.** Several messages to one bot wait a few seconds and go into one session, unless one is marked urgent.

**Each session starts small.** A run is given only:
- the bot's Soul and Persona,
- the capped Memory,
- a short task packet: the messages, item IDs, file paths and the handoff note.

Messages carry **pointers, not copies**, so a long script is referenced as `work/T-0001/script.md` and not pasted between bots. Sessions never resume long conversations; they restart from the handoff note. Every identity file gets a size limit that `cb check` enforces.

**Memory stays small.** When a Memory file passes its cap, a low-cost session condenses it and files the details as searchable notes. Nothing runs if it's under the cap.

**Old conversations always get archived. (Owner: this has to keep working for clients for years, because full context windows cost more and are bad practice.)** No bot ever carries its history forward. A run or Owner chat loads the same small starting context in year 5 as in week 1.
- **Every bot run** ends with its handoff note and Memory updated, then its full transcript moves to the archive automatically.
- **Long chats with Admin** roll over automatically: before Admin's context gets large, it saves a summary and open items, archives the conversation, and continues in a fresh session. The Owner keeps talking as normal. The threshold is measured and set during the build.
- **Finished items and old messages** move out of the active tables to the archive on a schedule, leaving the active queue and index short.
- **The archive is searchable and nothing is deleted by default.** `cb search` can find old conversations, so bots can look up past detail without loading it. It's compressed to save disk, and Claude Code's own session files are cleaned up after archiving.
- **Disk is watched:** when the archive reaches a set share of the disk, the backend moves its oldest parts to off-server storage where they stay retrievable, and tells Admin. Permanent deletion only happens on the Owner's direct word.
- **Archiving costs almost nothing:** plain scripts do it, with no AI. A summary is only written by a bot that's already running or, for Memory condensing, a low-cost session.

**The right model for the job.** Each message or routine carries a level: housekeeping uses Haiku, normal work uses Sonnet, and Opus only runs when asked for or for big decisions, as in the older design.

**The server stays healthy:**
- The dispatcher starts a run only if available memory is above a set floor, up to a maximum of 2 runs at first. Measure real use and adjust. About 1.6 GB was free with no session running; a session's real cost has not yet been measured.
- Each run has a memory limit and a time limit through `systemd-run`, so one bad run gets stopped rather than taking down the server.
- Heavy jobs go one at a time in their own queue.

**No loops.** Each message chain has a hop count and a limit, each message has a duplicate check, and each bot has a wake cap per hour. Hitting any limit stops the chain and raises a note to Admin.

**Nothing gets lost.** Starting work claims it with a time limit. If a run crashes or times out, the dispatcher returns the work to the queue. After 3 failures it's marked stuck and raised to Admin. A message counts as done only once its reply is recorded. When a usage limit hits, the queue pauses and picks up from the same place.

### 8.6 Records, history and backups

**Log and decisions:** items and decisions live in `company.db`, using the statuses `pending, todo, next, doing, paused, feature, slice, finding, done, parked`. Decision records hold the Owner's exact words. `cb` can export any item or decision as readable text. Times shown to people are in CT, such as `Tue Nov 3, 2026, 7:45 AM CT`.

**Activity record:** an append-only table of bot runs, messages, secret use by name, sudo-level changes and routine, persona and bot changes, plus usage by bot where available. Admin builds its summary for the Owner from this with a short query, so checking in stays cheap. A copy goes off the server regularly, so a mistake can be traced even if a bot damages the local copy.

**Git tracks all changes. (Owner: use Git for changes.)** The engine's code and the company's files are each a Git repo.
- **Company files:** when a bot's run ends, the dispatcher commits that run's file changes under the bot's name and item ID, one commit at a time. Bots don't run Git themselves, so there's one straight history with no branches or merge conflicts between bots.
- **Database:** the coordination data stays in SQLite because it changes too often for Git. The backend commits a clean text export on a schedule.
- **Off-server:** the backend pushes to a private GitHub repo on a schedule. Secrets and large media files are excluded (section 3).
- **Engine code:** it changes on a branch with tests, then gets deployed to the server. Bots don't edit it during normal work.

**Backups and restore:** the same snapshot is the off-server backup, and a restore gets tested. Secrets are only included if encrypted, with the key kept away from the backup.

## 9. How the Owner sees updates

**The Owner talks to Admin. (Owner)** From his Mac, he opens a session with Admin on the VPS over SSH, either in a terminal or through a Claude app SSH environment. When he opens it, Admin gives a short summary since his last check: what got done, what's stuck and what needs his yes, then he can talk freely. (The content of the summary is the Builder's call.)

**Mac and phone (Owner, Nov 9, 2026).** From the Mac, `cb chat` over SSH as above; it pauses new bot work while open. From the phone, Telegram talks to the always-on Admin session, which does not pause bot work. Only his paired Telegram account counts as his word, and pairing or allowlist changes are made only at the terminal, never by Telegram message. Attach to the session with `tmux attach -t admin-live`.

Admin must be able to answer "what changed?" from the log and activity record. No dashboard, email or push notification is planned for now.

## 10. Build order (Builder's call)

0. A check, not a build: confirm that unattended use on the Owner's current Claude plan is allowed and workable. If it isn't, stop and give the Owner the options, because paying for more is his decision.
1. Secure store (Owner: first thing built).
2. Office layout, bot identity (Soul, Persona, Memory) and the engine and company split.
3. Admin, then the Owner tests talking to Admin.
4. Messaging, log and activity record.
5. Wake-up service, queue and limits, plus the tool for routines and triggers.
6. Bot creation (section 4a), tested by the Owner asking Admin for one real bot.
7. Backend role, backups and restore.
8. A trial: the Owner prompts Admin with one piece of channel work with his Mac closed.

Which bots come after that is up to the Owner, through Admin.

## 11. Done means

- A fresh install has one bot, Admin, and it can do useful work alone.
- The Owner asks Admin for a new bot. Admin works with him, creates it with Soul, Memory and persona, and the bot answers in character. After a reboot and a new session, Admin still knows it and can reach it.
- With the Mac off, a message to a sleeping bot wakes it, it does the work, replies, and goes back to sleep.
- Two created bots work together on one item, and the log shows it. While one waits for the other, it has no running process.
- With years of simulated history loaded, a bot's starting context is the same size as on day one, routine database commands stay fast, and an old conversation can still be found by search.
- A long chat with Admin rolls over to a fresh session without the Owner losing the thread.
- The Owner sends 10 requests across interruptions, and the session is then killed. A new Admin session lists all 10 word for word, resumes each paused one from its note, and can hand any of them to another bot, which finishes it.
- A bot's run changes a file, and `git log` shows that change with the bot's name and item ID. Two bots finishing at the same moment produce no merge conflict.
- Admin creates a bot with a chosen model, effort level, skills and MCP server, and that bot's run actually uses them. A bot on a latest-model alias picks up a new model with no edit.
- A bot finds a fact another bot recorded, using search, without being told where it is.
- A routine with nothing to do starts no Claude session and uses no usage.
- A bot killed mid-run has its work picked back up with nothing lost. Two bots claiming at once can't take the same work.
- A run that goes over its memory limit is stopped, and the server and other bots keep running.
- A bot's Memory from one run is there on its next run. Its Soul and persona load every time.
- A bot uses a secret by name. Its value appears in no chat, log, Memory file or Git history, and the record shows the use.
- Reaching the wake-up cap stops a planted loop. Hitting a usage limit keeps all messages and work resumes afterward.
- A bot asked by another bot, or by planted text in a comment, to publish refuses and logs it. Admin publishes on the Owner's direct word and records his exact words.
- A backup restores correctly.
- After the server reboots, the service and routines return on their own.
- The Owner opens Admin and gets an accurate summary.
- A grep of the engine finds no `<PRODUCT>`-specific details.

## 12. Risks and things to verify

- **Claude plan:** the build starts on the Owner's existing Claude login, shared with his own use. Verify unattended use and how limits apply now (section 10, step 0). Separately, before the first paying customer, verify against Anthropic's current terms whether customer work is allowed on that plan and whether usage is enough. Keep the Claude account swappable per company.
- **`--dangerously-skip-permissions` with sudo:** this is the Owner's chosen setup. Its risk is handled by section 7's rules, the off-server record and backups, not by blocks.
- **YouTube and email access:** exact account access and API limits are not yet checked. The builder confirms what's possible and gives the Owner the steps. The Owner creates all accounts and tokens himself.
- **Server size:** see section 3.

## 13. Scope calls (Builder's call)

- **Video production is not part of this build.** No rendering pipeline, media tools or large-file backup. The trial in section 10 is scripted or written channel work, such as a script or a title and description package. Video work comes later, when the Owner asks for those bots, and that's when the server's size gets tested.
- **Moving files:** when the Owner needs to send or fetch a file, he copies it over SSH by hand. That's acceptable because the company doesn't depend on it running.
- **Log housekeeping:** Admin archives finished items and keeps the index short. No separate quality bot is needed. If the Owner later asks for one, it can take this over.

## 14. Left to the builder

Exact database tables, `cb` command details, file formats, exact folder locations (the Owner has created `$CLAUDEBOT_HOME` and `~/ClaudeBot` on the server, so check those first), how bots map to Claude Code agents and flags, the encryption method, limit values, backup schedule, and the exact wording of rules and the starting Soul template. Python standard library and SQLite are the default for engine tools, following section 8.
