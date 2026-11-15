---
name: telegram
description: Use Telegram features from a head - send files with a MEDIA line, ask with tap-able buttons, pin, react, forward, edit or delete your own messages, show a progress bar, or read the messages around one the person replied to.
---

# Telegram as a tool

Your final reply already goes to your topic as a reply to the message it answers. **Don't also send it with tg.py.**
Use tg.py for everything else. It prints one JSON line. Every send is logged and recorded in the chat ledger.

**Attach a file to your reply:** put `MEDIA:/abs/path` on its own line. The lane sends the file and removes the line.

```bash
python3 ~/harness/harness/tg/tg.py buttons "Deploy now?" "Yes" "Not yet"     # up to 8 choices
python3 ~/harness/harness/tg/tg.py photo|video|voice|videonote|file /abs/path ["caption"]
python3 ~/harness/harness/tg/tg.py progress build 40 "compiling" [ETA]        # one silent message, edited in place
python3 ~/harness/harness/tg/tg.py progress build done                        # removes it
python3 ~/harness/harness/tg/tg.py context <msg_id> [--n 3]                   # a message and its neighbours
python3 ~/harness/harness/tg/tg.py search <words...>
python3 ~/harness/harness/tg/tg.py react <msg_id> 👍
python3 ~/harness/harness/tg/tg.py pin <msg_id>   |  unpin <msg_id>
python3 ~/harness/harness/tg/tg.py forward <msg_id> [--thread T]  |  copy <msg_id> [--thread T]
python3 ~/harness/harness/tg/tg.py edit <msg_id> "new text"  |  delete <msg_id>   # your own messages only
python3 ~/harness/harness/tg/tg.py send "text"                                # a mid-turn note
```

- The person's tap on a button comes back to you as a new turn: `[<name> tapped "Yes" on your question: "..."]`.
  End your turn after asking.
- Poll votes are **not** reported back. To get an answer, use buttons.
- You can only reach the owner's DM, and your own topic by default. `--thread` picks another topic in that DM.
- Messages that arrive while you work reach you mid-turn. Answer each in your final reply, on its own line.
