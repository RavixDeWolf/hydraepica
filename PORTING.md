# Porting ledger

Lab features that are not in the public core yet. Each one is tagged `hydra:core` (it belongs here) or `hydra:plugin`
(optional, shipped here but off by default). Lab-only features (`hydra:lab`) are not listed.

| Feature | Tag | Notes |
|---|---|---|
| Email tab in the stage | hydra:plugin | Bring-your-own IMAP/SMTP in config; password from an env var. Never tied to a hosted mail service. |

## Ported

| Feature | Tag | Where |
|---|---|---|
| `/ram /vram /storage /load /loadavg` | hydra:core | `harness/hostload.py`; machines from `[machines]` in the config, Linux / Mac / Windows |
| Still-working lines, long replies delivered, refused reply resent, drop / rate-limit retry + Try again | hydra:core | `harness/head_lane.py` |
| Auto-mode refusals said in the thread | hydra:core | `PermissionDenied` hook event, said by the lane |
| Demo-safe leak check (reply text, OCR of images, step labels, mid-turn sends) | hydra:core | `harness/hooks/leakcheck.py`, names in `[leakcheck]` |
| Stage html pages report errors and blank canvases (with a snapshot) to the head that posted them | hydra:core | `harness/stage/stage_report.py` |
| A head about to start the wrong Claude Code says so in its topic | hydra:core | `harness/cli_check.py`, ExecStartPre in the head unit |

## Queued

| Feature | Tag | Notes |
|---|---|---|
| Hold a done/can't reply until the head has looked | hydra:plugin | off by default |
| Vault + write-only inject from the stage's Settings | hydra:core | encrypted store, fingerprints only |
