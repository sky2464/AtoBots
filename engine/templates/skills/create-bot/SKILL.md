---
name: create-bot
description: How to create a new bot with the Owner: define it one question at a time, draft its Soul and Persona for their approval, record their words, create it, wake it and confirm it replies in character. Use only when the Owner asks for a new bot.
---

# Create a bot

Create a bot only when the Owner asks for one. If you think one would help,
say so in one sentence with your recommendation, and wait for their answer. Until
then, do the work yourself.

## 1. Talk it through, one question at a time

Ask only one short question at a time, each with your recommended answer. Cover:

1. What the bot is for.
2. Its name: lowercase letters, digits and hyphens, starting with a letter,
   30 characters at most. Check `cb bot list` (and `--status paused`, `--status retired`) so it is
   not taken. `cb bot new` also refuses a name already used.
3. Who it reports to (usually admin).
4. Its values and voice.

If they paste a Grok profile, use it as source text. Keep what still fits, and
flag anything that looks out of date, such as old tools, names, rules or
accounts, and ask whether to keep it. Do not copy secrets from it.

Decide, from the job and with their OK: model (`haiku`, `sonnet` or `opus`;
sonnet is the default), effort (leave unset unless needed), the skills it needs
(only those), and whether it needs plugins, MCP servers or connectors. Give
a bot only what its job needs, to keep its sessions small. The Owner connects
any account themselves. Install a new tool only from a source they approved.

Where things live, if the job needs them: an MCP server definition is a file
`$ATOBOTS_HOME/mcp/NAME.json` (name it in `--mcp NAME`), and a plugin is a
folder `$ATOBOTS_HOME/plugins/NAME/` (name it in `--plugins NAME`). A token an
MCP server needs is written in its definition as `${secret:NAME}`, the name
of an item in the secure store; the value is filled in when the bot's run
starts and each use is recorded. Never write a token value into the file.
`--connectors` exposes every account
connector the Owner has linked (mail, calendar, drive and so on) to that bot,
not just one, so ask them first and say so plainly.

## 2. Draft the Soul and Persona

Make an item for the job (`cb item new "Create bot NAME"`) and write both into
its folder, for example `$ATOBOTS_HOME/work/T-0009/soul.md` and `persona.md`.
Your working folder is your own bot folder, so use that full path.

- Soul (under 4 KB): who the bot is at its core. Values, judgment, what it will
  never do. Include the company rules by pointing to them: it follows the
  shared rules. Say it never puts secrets in Memory or messages.
- Persona (under 4 KB): job title, voice, how it works, how it uses `cb`
  (`cb inbox`, `cb next`, `cb reply`, `cb ask`), and who it hands work to.
- Plain, warm, direct language. Use "the Owner", not a name.

Show them both in full. Ask: "Approve these as they are? I'd say yes." Change
them until they say yes. Only their approval sets a Soul.

## 3. Record their approval

When they approve, in their own words, record them exactly:

```
cb decision record "Create bot NAME with this Soul and Persona" --quote "<their exact words>"
```

Note the decision ID it prints (D-0001 style). Never record words they did not say.

## 4. Create the bot

```
cb bot new NAME --role "short role" --soul-file $ATOBOTS_HOME/work/T-0009/soul.md \
  --persona-file $ATOBOTS_HOME/work/T-0009/persona.md --reports-to admin \
  --model sonnet --skills cb-basics,work-tracking
```

Add `--org`, `--effort`, `--mcp`, `--plugins` or `--connectors` only when the
job needs them. The bot starts with an empty Memory and its own mailbox.
Changing its setup later is `cb bot set NAME ...`. Changing its Soul needs
`--decision D-0001`, a decision the Owner approved. `cb bot pause NAME` and
`cb bot retire NAME` keep its history.

## 5. Check it works

```
cb check
cb send NAME "Welcome" "Introduce yourself in two sentences, in your own voice." --from admin --priority urgent
```

`--priority urgent` from you starts the new bot at once, even while the Owner
is in a chat with you (other messages wait until they leave). Its reply waits
the same way, so look for it with `cb inbox` and `cb search`. Confirm it answered in
character. If it did not answer or sounds wrong, fix the Persona (you may),
and for a Soul fix, show the Owner and record their word again.

## 6. Tell the Owner and remember

Tell them in a few lines: the name, what it does, its model, its skills, and its
first reply. Close the item. Add a line to your Memory with what they said about
the bot and the decision ID.
