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

## Queued

| Feature | Tag | Notes |
|---|---|---|
| Long-turn still-working lines, long reply delivered, rate-limit retry | hydra:core | head lane |
| Dropped-connection retry + Try again button | hydra:core | head lane |
| Telegram notice when auto mode refuses an action | hydra:core | head hook |
| Leak check on final replies (text + OCR of images) | hydra:core | private words from config |
| Stage pages report errors / blank canvas to their head | hydra:core | stage |
| Stale Claude Code warning at head start | hydra:core | new_lane / unit |
| Hold a done/can't reply until the head has looked | hydra:plugin | off by default |
| Vault + write-only inject from the stage's Settings | hydra:core | encrypted store, fingerprints only |
