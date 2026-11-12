# Security

This harness runs agents with real tools on real machines, so a bug in its gates matters. Please report one
privately before you talk about it in public.

**Report:** open a private security advisory on this repository, or email the maintainer address on the profile.
Say what you found, how to reproduce it, and what it lets someone do. You will get an answer within a few days.

## What the design relies on

- **Telegram:** only user ids listed in your config reach a head; everyone else gets a fixed reply and is logged.
  Buttons pressed by anyone not in that list are ignored.
- **The stage:** every person-facing route checks Telegram's signed Mini App `initData` against your bot token and
  your users list. Pushes from the heads need the host-only push key. Run it on a unix socket behind your own
  HTTPS proxy, and keep `/api/push*` off the public side.
- **Cast receiver screens:** each screen has its own key, held in an environment variable. No key, no screen.
- **exec:** destructive patterns never run; anything not provably read-only needs a confirm, which Claude Code
  turns into a permission prompt (and the hook into Allow / Deny on your phone). It is a guard rail, not a sandbox:
  run the heads as a user with only the access you mean them to have.
- **Secrets:** the bot token, API keys and screen keys live in environment variables or files outside the repo.
  `tools/scan.py` refuses a commit that carries a token, a key, a private address or a home path.

## Known limits

- The heads run as an ordinary Unix user. `headsafe`'s "the safety net is off limits" is a rule plus a deny list,
  not a lock, unless you make its code and store owned by another user.
- `exec` classification is a pattern list. Treat it as defence in depth, not a boundary.
