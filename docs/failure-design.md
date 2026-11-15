# Designing for failure when nobody is watching

An agent you sit beside fails in front of you. You see the bad edit, the stuck turn, the test that passed for the wrong
reason, and you fix it. HydraEpica runs about thirty agents that mostly work while their owner is asleep, on a phone,
or busy with something else. Each failure below happened at least once in the lab this was extracted from. This page
covers what we built so that a failure fixes itself, or at least gets reported.

Three rules sit under all of it:

1. **A failure is either undone or reported.** Nothing just stays broken without anyone being told.
2. **A rule has to be a check that runs.** "Remember to do X" is not enough. Anything that matters becomes code that
   exits non-zero.
3. **Be clear about what a check does not prove.** Every safety piece below says where it stops working.

## 1. An agent that edits itself (`safety/headsafe.py`)

Heads can change their own lane, hook, bridge, settings, units and instructions. That is useful, and dangerous: a head
that breaks its own bridge can no longer hear the message telling it to fix the bridge.

So a self-edit is a transaction:

1. `snapshot` saves every file the head is about to touch (a tar.gz and a manifest) and prints an id.
2. The head makes its change.
3. `arm --snap ID --restart CMD` starts a watchdog in **its own systemd unit**, so the watchdog is still running after
   the head restarts. It waits for the head's turn to end, then runs the restart.
4. **Liveness check.** The watchdog types a probe into the head's REPL. If the exact answer doesn't come back within
   20 s, it restores the snapshot, restarts the head, and messages the owner.
5. **Finalize.** If the head answers, it gets 60 s to confirm everything works and run `finalize`. If it doesn't
   finalize in time, the snapshot is restored.

Every step is logged. A revert leaves a `REVERTED` marker and a message; a revert that itself fails leaves `FAILED`.

**Where it stops working.** The head runs as the same Unix user as the safety net. So "a head can't edit headsafe" is
a rule backed by a deny list, not something the OS enforces. The docstring says so. Fully closing it means the operator
makes the code and the snapshot store owned by root.

## 2. A green test suite is not evidence (`safety/ship_check.py`)

The lab's pager for denied actions passed every test, and then sent nothing the first time it ran for real. Four
separate mistakes added up to that:

- the regression test couldn't see the bug it was written for;
- the tests wrote into live state;
- the code imported fine from the test's working directory but not from the server's;
- the running process was still on the old code.

Every one of those already had a written note about it. Notes are advice, and advice gets skipped. So anything that
**catches, surfaces or gates** something (a pager, a watchdog, an auth gate, a healer) has to pass `ship_check`, which
exits 0 only when five checks pass:

| Check | What it proves |
|---|---|
| `run_mutants` | Break the code on purpose; the suite must go red. The suite has to be green first, because a red baseline makes every mutant look caught. A mutant whose anchor doesn't match exactly once is reported as NOT_APPLIED rather than counted as caught. |
| `live_unchanged` | Run the suites; the live state they must never touch is byte-identical afterwards. |
| `run_as_hub` | Run the code from the production working directory and `sys.path`, not the test's. |
| `process_predates` | The running process is not older than the file it's meant to be running. |
| `live_proof` | A real end-to-end artifact exists, and it's newer than the code it proves. |

Mutating code in place is risky in its own way, so ship_check writes a sidecar copy first, restores each mutant in a
`finally`, restores on SIGTERM and SIGHUP, checks the result byte for byte, and recovers from the sidecar on the next
run if it was killed with `-9`.

## 3. Risky actions wait for a human

- **Permission prompts become phone buttons.** The Claude Code `PermissionRequest` hook (`harness/hooks/head_hook.py`)
  posts an Allow / Deny card and waits up to about 9 minutes for the owner's tap. With no answer, the request goes
  back to Claude Code's normal terminal prompt: a timeout never becomes an "allow".
- **`exec` is read-only by construction.** The hub's command tool treats a command as read-only only when it has no
  shell syntax at all, and is an allow-listed binary and subcommand with no denied flag (`git -c`, `find -exec`,
  `journalctl --vacuum`, ...). Those commands run as a plain argv list, with no shell. Everything else needs a confirm,
  and known destructive patterns never run. The confirm flag is advisory, and SECURITY.md says so.
- **Only the owner can drive a head.** A message must come from an allow-listed user *in their own private chat*, so
  a head's topic forwarded into a group can't be steered from there.

## 4. Nothing fails silently

Retries, fallbacks and self-heals are allowed. Silent ones are not. A recovery that hides its first two failed attempts
behind a green "worked on attempt 3" counts as the bug.

- The gateway logs any update that fails and still advances the offset, so one bad message can't wedge the bot.
- The quick-reply LLM path (`harness/llm.py`) logs each endpoint that fails, logs which fallback answered, and logs
  the give-up when none did. The user then gets a plain "can't answer right now, it's logged" instead of silence.
- The stage disables a cast screen whose key is too short, and says why on stderr, instead of running it insecurely.

## 5. Tests that can't hurt the live system

All 516 tests run against a throwaway config and state directory. The router suite refuses, at the socket level, to
connect to a live head. That rule exists because a test once did.

## What's still open

- headsafe's "can't edit the safety net" is a rule, not a lock (above).
- `live_proof` needs a real deployment to produce its artifact. On a fresh checkout that check can't pass until you
  run the system once.
- Security fixes made in this repo are tracked separately for the lab it came from. The public release waits for
  those fixes to land in the lab.
