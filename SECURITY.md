# Security

This harness runs agents with real tools on real machines, so a bug in its gates matters. Please report one
privately before you talk about it in public.

**Report:** open a private security advisory on this repository, or email the maintainer address on the profile.
Say what you found, how to reproduce it, and what it lets someone do. You will get an answer within a few days.

## What the design relies on

- **Telegram:** only user ids listed in your config reach the bot; everyone else gets a fixed reply and is logged.
  Only the owner reaches a head, and only from the owner's own private chat with the bot (a group topic that
  happens to share a head's topic number is refused). Buttons pressed by anyone else are ignored.
- **The stage:** every person-facing route checks Telegram's signed Mini App `initData` (header only) against your
  bot token and your users list. URLs that cannot carry a header (video, the live event stream) use a signed
  ticket that expires in an hour, so the login data itself never lands in a URL or a proxy log. The agent browser
  view is for admins only. Pushes from the heads need the host-only push key (`push.key` in the state dir:
  create it yourself, `chmod 600`). Run the stage on a unix socket behind your own HTTPS proxy, and keep
  `/api/push*` off the public side.
- **Cast receiver screens:** each screen has its own key, held in an environment variable, 32 characters or more
  (a shorter one disables the screen). No key, no screen. A key can only view what is cast to its screen. It rides
  in the screen's URL, so tell your proxy not to log query strings for `/screen/`.
- **exec:** destructive patterns never run. A command counts as read-only only if it is on a short list, uses no
  shell syntax at all (no newline, glob, `$`, `~`, braces, pipes or redirects) and no option that writes or changes
  things; it then runs without a shell, exactly as checked. Anything else needs a confirm, which Claude Code turns
  into a permission prompt (and the hook into Allow / Deny on your phone). The confirm flag itself is set by the
  caller: the real gate is that prompt. It is a guard rail, not a sandbox: run the heads as a user with only the
  access you mean them to have.
- **The hub over HTTP** (`--http`) binds 127.0.0.1 and refuses to start without `HUB_TOKEN` (32+ chars); every
  request must send it as a bearer token. stdio mode needs none: Claude Code starts it per head.
- **Secrets:** the bot token, API keys and screen keys live in environment variables or files outside the repo.
  `tools/scan.py` refuses a commit that carries a token, a key, a private address or a home path.

## Known limits

- The heads run as an ordinary Unix user. `headsafe`'s "the safety net is off limits" is a rule plus a deny list,
  not a lock, unless you make its code and store owned by another user.
- `exec` classification is a pattern list. Treat it as defence in depth, not a boundary.
