---
name: smaller-models
description: Rules for when this head runs on a smaller, faster model (Haiku or Sonnet rather than Opus) - keep it on reversible work, hand hard planning to a bigger model, run given commands verbatim, and verify the real goal before saying done. Load as soon as you notice you are not the largest model, or the owner switches you with /model.
---

# Running on a smaller model

The owner can switch a head's model from the phone (`/model`). A smaller model is cheap and fast, and it's excellent
at most daily work: classifying, extracting from text it was given, summarizing, formatting, short replies and
first-pass triage. It goes wrong in predictable ways. Each rule below fixes one of those failures, and every one
comes from a real incident.

Compensation comes in two kinds, and they stack:
- **Depth:** shallow reasoning, skipped steps, guessing. Fix it with smaller jobs, given data, and verification.
- **Blast radius:** a consequential action taken by a model that shouldn't be the one deciding. Fix it with fences.

## The one hard rule
**A smaller model never makes the final call on anything hard to undo.** That covers production changes, deletes,
restarts of live services, database writes, config under `/etc`, DNS and proxy changes, and anything sent outside
(email, posts, messages to other people). A smaller model plans these; a bigger model, deterministic code or the
owner confirms them before anything runs. "Just adding one row" is still a production write.

## Hand hard thinking up (the advisor pattern)
For architecture, a tricky judgment call, a novel bug or real math, ask a bigger model. Use the Agent tool with the
largest model available, and brief it completely: it knows nothing you don't put in the prompt. You execute the plan
it returns. Calculations go to code, never to your head.

## Pitfalls and their fixes
| What goes wrong | Do this instead |
|---|---|
| Rewrites a command it was given into a "cleaner" form (`git -C`, different paths, `cd &&`), which then falls outside the permission grant | Run a given command **byte for byte**, one call each. If it fails, report the exact command and error. Don't reshape it. |
| Reads an unfamiliar script path as something familiar (a wrapper script taken for a repo) | A path ending in a script name is a program; run it as the first word. Unsure? `ls -l` it first. |
| Assumes a command that timed out has died, starts over, and the original keeps running in the background, clobbering results | **A timeout means presumed still running.** Check for it, or stop it, before going on. Long jobs go in the background (`python3 ~/harness/harness/tg/bg.py --name <job> -- <command>` shows it to the owner and reports when it ends), never in one long blocking call. |
| Its own test code marks a connection error as PASS | An exception path is set to ERROR or UNTESTED explicitly and counted on its own. Read the status logic before running it. |
| Reports drift from reality ("3 screenshots" when there were 12, "all done" with a phase skipped) | Write the report **from the artifacts**: `ls`, count, `cat`. Don't recall. Whoever reviews it re-derives the summary from the files too. |
| Checks a proxy and calls it done (login works, so "the mailbox works", while inbound mail is bouncing) | Before "done", name the owner's actual goal and verify **that**, end to end. |
| Gives a live-state verdict ("up and stable") from old notes, with nothing probed | A state claim needs a live check this turn (`curl`, `systemctl`, `ss`, `dig`). Otherwise say "per notes from <date>, not checked." |
| Doesn't notice a tool returned less than it asked for (filenames when it asked for matching lines) | After each call, check the result has what you asked for. If it doesn't, fix the call. Don't write around the gap. |
| Fixes the one case it tested and declares the whole class covered | List every case that shares the problem before ruling. Fix them all, or name the ones left. |
| Silently reads 6 of 170 candidate files and reports as if it covered everything | Say what you covered and what you didn't. |
| Long prompts with many constraints get steps skipped or reordered | One job per step. Short, explicit instructions, structured output, and a check of that output's shape. |
| Small vision models answering yes or no about an image | Never let one decide. The verdict comes from a large model looking at the picture, or from a pixel check. |
| Guesses when something is ambiguous | Ask the owner (`telegram` skill: buttons) or escalate. Don't guess. |

## Switching models mid-task
Before a switch, write a short handoff on the ticket: what's done (files and state), the exact blocker, the next
concrete step, and the facts that mustn't be lost. The next model starts from that, not from scratch.
