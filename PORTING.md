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

## Queued

| Feature | Tag | Notes |
|---|---|---|
| Hold a done/can't reply until the head has looked | hydra:plugin | off by default |
