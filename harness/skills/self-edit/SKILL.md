---
name: self-edit
description: Safely change your own harness - lane, hook, bridge, settings, units or your instructions - with headsafe, so a change that breaks you rolls itself back. Use before editing any file that makes up this head.
---

# Editing yourself safely

You may change any part of yourself. A change that breaks you has to undo itself, because a broken head can't hear
the message telling it to fix itself. Always follow this order:

1. **Snapshot first.**
   `python3 ~/harness/safety/headsafe.py snapshot --label "what you are changing"` prints a snapshot id.
2. **Make the change.**
3. **Arm it.**
   `python3 ~/harness/safety/headsafe.py arm --snap <id> --restart "<restart command>"` (repeat `--restart` for each command).
   **Then end your turn.** After about 8 s it runs the restart(s).
4. **Answer the probe.** It types a line into your REPL. Reply with exactly the line it asks for
   (`ALIVE headsafe-probe-...`). No answer within 20 s: the snapshot is restored, you are restarted, and the owner
   is told.
5. **Check, then finalize** within 60 s: `python3 ~/harness/safety/headsafe.py finalize`. No finalize: revert.

Changed your mind? `python3 ~/harness/safety/headsafe.py revert --reason "why"`. Check with `status` or `list`.

**Never edit headsafe itself, its state dir or its snapshots.** That's the one thing a head doesn't touch. (It's a
rule plus a deny list, not an OS lock; see docs/failure-design.md.)
