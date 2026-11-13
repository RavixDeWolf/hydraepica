# Porting ledger

Lab features that are not in the public core yet. Each one is tagged `hydra:core` (it belongs here) or `hydra:plugin`
(optional, shipped here but off by default). Lab-only features (`hydra:lab`) are not listed.

| Feature | Tag | Notes |
|---|---|---|
| Email tab in the stage | hydra:plugin | Bring-your-own IMAP/SMTP in config; password from an env var. Never tied to a hosted mail service. |
