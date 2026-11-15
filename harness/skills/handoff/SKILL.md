---
name: handoff
description: Hand a task to another head (another Telegram topic's agent) when the work belongs to that head's job, and report status on the shared ticket rather than by messaging.
---

# Handing work to another head

Each head owns one job. When a request belongs to another head's job, hand it over instead of doing it yourself.

```bash
python3 ~/harness/harness/tg/handoff.py --to <lane> "What to do, with every detail it needs. Report on TIK-1234."
```

- `<lane>` is `main` or any lane in `lanes.json`. A new topic's head is reachable as soon as it exists.
- The task shows up visibly in that head's topic ("📋 Task from ...") and is queued there as a turn.
- **Write the task self-contained.** The other head shares memory with you but not this conversation: include paths,
  ticket numbers, what's already ruled out, and what "done" means.
- **Status goes on the ticket** (the hub's `tickets` tool), not in more handoffs. A handoff is the doorbell; the
  ticket is the shared record both heads and the owner can read.
- Never restart the gateway, another head's unit or its tmux without saying so on a ticket first.
