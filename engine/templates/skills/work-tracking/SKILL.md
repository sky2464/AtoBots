---
name: work-tracking
description: How to track every Owner request as an item, switch tasks without losing work, hand off or resume, split big work and work through open items when idle. Use at the start of every session and whenever you are interrupted or given new work.
---

# Work tracking

Nothing depends on remembering. The database remembers. Your job is to keep it
true.

## Every session starts here

1. `cb request unhandled` lists Owner messages not yet linked to an item.
2. `cb next` lists what to do next. `cb open` lists everything still open.
3. Read before you answer. Then deal with the unhandled messages first.

## Link each request

Every Owner message is saved word for word before you see it. Turn each task
in it into an item, then link the message:

```
cb item new "Short title of the task" --owner admin
cb request link 12 T-0007 T-0008
```

One message with ten tasks becomes ten items. A request that is only a
question, or already finished, still gets linked, to an item you close as done.

## Statuses

`pending, todo, next, doing, paused, feature, slice, finding, done, parked`.
Change one with `cb item status T-0007 doing`. Close finished work with
`cb item status T-0007 done`, and say in your reply what is actually finished.
Use `--owner-waiting` on `cb item new` when the next move is the Owner's, and
`--priority N` to raise something.

## Before you switch

When the Owner interrupts or something more urgent arrives, pause first:

```
cb item pause T-0007 "Done: drafted outline. Next: fill section 2, then ask the Owner about the title."
```

The note must let a fresh session pick it up cold. Then start the new request.

## Every open item has one of three paths

1. Continue it yourself later: `cb item resume T-0007`.
2. Hand it to another bot: `cb item assign T-0007 NAME --note "context"`.
3. Ask the Owner, with one short question and your recommended answer.

## Split big work

`cb item split T-0007 "Step one" "Step two" "Step three"` makes child items, so
progress lives in the database and not in a conversation. Keep working them in
the order `cb next` gives.

## When you are idle

Do not wait to be asked. Run `cb next`, take the first item you may do within
your rules, and work it. If you finish, report briefly and take the next one.
If an item needs something only the Owner can give, set it owner-waiting and
move on.

## When asked "what is open?"

Run `cb open` and answer from it: in progress, paused, handed off, stuck, and
what needs his yes. Short, no padding.

## After a restart

Sessions end, chats roll over, servers reboot. Start again from step 1 above.
The items and their pause notes are the continuity.
