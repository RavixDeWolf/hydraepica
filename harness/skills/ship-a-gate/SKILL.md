---
name: ship-a-gate
description: Ship anything whose job is to catch, surface or gate (a pager, watchdog, alert, auth check, guard, healer) through ship_check, with a manifest and deliberate mutants. A green test suite alone is not evidence it works.
---

# Shipping something that catches, surfaces or gates

A guard that silently doesn't fire looks exactly like a quiet day. So these never ship on "the tests pass". They ship
when `python3 ~/harness/safety/ship_check.py manifest.json` exits 0.

**1. Write mutants.** These are the bugs your tests must catch. Each one is `(name, exact_old_text, new_text)`, and the
old text must appear **exactly once** in the target:

```python
# mutants_mything.py
MUTANTS = [
    ("alert never fires", "    if level >= THRESHOLD:\n        send(", "    if False:\n        send("),
    ("wrong threshold",   "THRESHOLD = 3", "THRESHOLD = 300"),
]
```

**2. Write the manifest.** Include only the checks that apply:

```json
{
  "name": "my watcher",
  "cwd": "/abs/path/to/code",
  "mutants": {"target": "./watcher.py", "test": "python3 -m pytest -q test_watcher.py",
              "file": "./mutants_mything.py", "timeout": 240},
  "suites": ["python3 -m pytest -q test_watcher.py"],
  "live_files": {"strict": ["/abs/live/state.json"], "append_ok": ["/abs/live/log.jsonl"]},
  "hub_path": {"name": "imports from production", "code": "import watcher; print('ok')", "expect_stdout": "ok"},
  "process": {"files": ["/abs/path/to/code/watcher.py"], "unit": "my-watcher.service"},
  "live_proof": {"file": "/abs/live/log.jsonl", "pattern": "ALERT sent", "newer_than": ["/abs/path/to/code/watcher.py"]}
}
```

| Check | Passes when |
|---|---|
| `mutants` | the suite is green on the real code, then red on every mutant |
| `suites` + `live_files` | the suites leave live state byte-identical (`append_ok` files may only grow) |
| `hub_path` | the code works from the production cwd and `sys.path`, not the test's |
| `process` | the running process started after the code last changed |
| `live_proof` | a real end-to-end result exists and is newer than the code |

**Reading the result:**
- `SURVIVED` means your tests can't see that bug. Write the test; don't delete the mutant.
- `NOT_APPLIED` means the anchor matched zero or several times. Fix the anchor.
- `REFUSED` means the mutant would delete the test seal (the switch keeping tests away from the real world). Prove
  the seal with a test instead.

Your guard's real side-effect function must refuse to act while `SHIP_CHECK_MUTATING=1` is set.

If it gets killed mid-run: `python3 ~/harness/safety/ship_check.py recover <file>` puts the original back.
