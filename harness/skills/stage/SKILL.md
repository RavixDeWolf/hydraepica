---
name: stage
description: Put something on a person's live phone stage (the bot's Mini App) - a card, image, gallery, video, sandboxed HTML page or link. Use for anything bigger than a chat message - a document to read, a chart, a dashboard, a long table, a page - instead of pasting a file path.
---

# The stage

The stage is a Mini App inside the bot. Whatever you put there shows at once if the person has it open; otherwise it's
waiting the next time they do. **A file meant for a person to read goes on the stage, never as a path in chat.** They
are on a phone and a path is no use to them.

```bash
python3 ~/harness/harness/stage/stage.py --to <person> card "Title" "Body text"
python3 ~/harness/harness/stage/stage.py --to <person> image /abs/chart.png [--caption "..."]
python3 ~/harness/harness/stage/stage.py --to <person> gallery "/abs/dir/*.png" [--title "..."]
python3 ~/harness/harness/stage/stage.py --to <person> video /abs/clip.mp4 [--caption "..."]   # up to ~4.4 MB
python3 ~/harness/harness/stage/stage.py --to <person> html /abs/page.html [--title "..."]     # runs sandboxed
python3 ~/harness/harness/stage/stage.py --to <person> url https://example.com [--title "..."]
```

`<person>` is a name from `[[users]]` in the config. `~/harness` is where the README clones this repo; use your path if you put it elsewhere.

**Markdown, diagrams and pages** become a picture first, then go up as an image:

```bash
P=$(python3 ~/harness/harness/render/render.py md notes.md)        # also: mermaid FILE, html FILE
python3 ~/harness/harness/stage/stage.py --to <person> image "$P"
```

Charts: draw with matplotlib, save a PNG, send the PNG.

**Casting to a TV** is the person's choice: they press cast on the stage item, and screens come from `[[cast.screens]]`.
A gallery or video they cast follows their phone (next picture, play, pause).

Then say in your reply that it's on their stage. Don't paste the same content into the chat as well.
