# Porting ledger

Lab features that are not in the public core yet. Each one is tagged `hydra:core` (it belongs here) or `hydra:plugin`
(optional, shipped here but off by default). Lab-only features (`hydra:lab`) are not listed.

| Feature | Tag | Notes |
|---|---|---|

## Ported

| Feature | Tag | Where |
|---|---|---|
| `/ram /vram /storage /load /loadavg` | hydra:core | `harness/hostload.py`; machines from `[machines]` in the config, Linux / Mac / Windows |
| Still-working lines, long replies delivered, refused reply resent, drop / rate-limit retry + Try again | hydra:core | `harness/head_lane.py` |
| Auto-mode refusals said in the thread | hydra:core | `PermissionDenied` hook event, said by the lane |
| Demo-safe leak check (reply text, OCR of images, step labels, mid-turn sends) | hydra:core | `harness/hooks/leakcheck.py`, names in `[leakcheck]` |
| Stage html pages report errors and blank canvases (with a snapshot) to the head that posted them | hydra:core | `harness/stage/stage_report.py` |
| A head about to start the wrong Claude Code says so in its topic | hydra:core | `harness/cli_check.py`, ExecStartPre in the head unit |
| Vault: encrypted secrets, `vault:NAME` in the config, write-only add/replace from the stage's Settings | hydra:core | `harness/vault.py`, `/api/vault*` |
| Mail tab: bring-your-own IMAP/SMTP, password from the vault | hydra:plugin | `harness/stage/mail.py`, `/api/mail*` (a new build; the lab's tab frames its own mail service) |
| Hold a done / can't reply once until the head has looked | hydra:plugin | `harness/hooks/recheck.py`, off by default |
| Idle sleep: an unused head's session ends to free memory, any message or socket send wakes it (⏰ Waking Head); the idle clock survives restarts; Always-on per head from Settings > Heads | hydra:core | `harness/head_bridge.py` (`HEAD_IDLE_SLEEP_S`, off by default), `harness/head_lane.py`, `/api/heads*` |
| A comment and the forward sent right after it are one turn | hydra:core | `harness/head_lane.py` (`FWD_JOIN_S`, mid-turn while a turn starts) |
| Highlight-to-comment on html stage pages; Send hands the notes to the publishing head | hydra:core | `harness/stage/stage_comments.py`, `/api/comments` |
| A lane head's self-edit probe goes to that lane, not the main head | hydra:core | `head_bridge.lane_env` (HEADSAFE_SOCK / HEADSAFE_EVENTS) |
