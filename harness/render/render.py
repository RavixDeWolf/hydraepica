#!/usr/bin/env python3
"""render.py - turn a diagram, page or markdown into a PNG the Telegram lane can send (TIK-0361).

Rav 2026-10-24: diagrams and charts arrive as PNG in the chat. Usage (prints the PNG path on success):
  render.py mermaid FILE|-  [-o out.png] [--width 1200]   a mermaid diagram (mermaid 11.12, vendored, offline)
  render.py html    FILE|-  [-o out.png] [--width 1080]   a full HTML page, full-height screenshot
  render.py md      FILE|-  [-o out.png] [--width 900]    markdown (big tables, notes) as a styled page
Charts: draw them with matplotlib and save a PNG; no wrapper needed.
Then put `MEDIA:/that/path.png` on its own line in the reply. Headless Chromium via Playwright, no network.
"""
import argparse
import html
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MERMAID_JS = os.path.join(HERE, "vendor", "mermaid-11.12.0.min.js")
OUT_DIR = os.path.expanduser("~/.local/state/harness/media/renders")

CSS = """
:root { color-scheme: dark; }
body { margin: 0; padding: 28px; background: #0f1115; color: #e6e6e6;
       font: 16px/1.5 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
h1, h2, h3 { color: #fff; margin: .6em 0 .3em; }
table { border-collapse: collapse; margin: 12px 0; }
th, td { border: 1px solid #333a45; padding: 6px 12px; text-align: left; }
th { background: #1b2029; } tr:nth-child(even) td { background: #151920; }
code, pre { font-family: "JetBrains Mono", Menlo, Consolas, monospace; background: #161a21; border-radius: 6px; }
code { padding: 1px 5px; } pre { padding: 12px; overflow: hidden; }
a { color: #7cb7ff; }
"""


def _src(path):
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def page_for(kind, text):
    """The HTML page to screenshot. Pure."""
    if kind == "html":
        return text
    if kind == "md":
        import markdown
        body = markdown.markdown(text, extensions=["tables", "fenced_code"])
        return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><body><div id=root>{body}</div>"
    if kind == "mermaid":
        return (f"<!doctype html><meta charset=utf-8><style>{CSS} body{{display:inline-block}}</style>"
                f"<body><pre class=mermaid id=root>{html.escape(text)}</pre>"
                f"<script src='file://{MERMAID_JS}'></script>"
                "<script>mermaid.initialize({startOnLoad:false, theme:'dark', securityLevel:'strict'});"
                "mermaid.run({querySelector:'.mermaid'}).then(()=>{document.body.dataset.done='1'})"
                ".catch(e=>{document.body.dataset.error=String(e);document.body.dataset.done='1'});</script>")
    raise ValueError(f"unknown kind {kind!r}")


def default_out(kind, now=None):
    return os.path.join(OUT_DIR, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{kind}.png")


def render(kind, text, out, width):
    from playwright.sync_api import sync_playwright
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".html"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page_for(kind, text))
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--allow-file-access-from-files"])
            pg = b.new_page(viewport={"width": width, "height": 800}, device_scale_factor=2)
            if kind != "html":       # our own pages never fetch anything; a user's html page may
                pg.route("http*://**", lambda r: r.abort())
            pg.goto("file://" + os.path.abspath(tmp), wait_until="load", timeout=30000)
            if kind == "mermaid":
                pg.wait_for_function("document.body.dataset.done === '1'", timeout=20000)
                err = pg.evaluate("document.body.dataset.error || ''")
                if err:
                    raise RuntimeError("mermaid could not parse the diagram: " + err[:300])
                pg.locator("#root").screenshot(path=out)     # the svg plus its padding
            elif kind == "md":
                pg.locator("#root").screenshot(path=out)
            else:
                pg.screenshot(path=out, full_page=True)
            b.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["mermaid", "html", "md"])
    ap.add_argument("src", help="file path, or - for stdin")
    ap.add_argument("-o", "--out")
    ap.add_argument("--width", type=int)
    a = ap.parse_args(argv)
    width = a.width or {"mermaid": 1200, "html": 1080, "md": 900}[a.kind]
    try:
        print(render(a.kind, _src(a.src), a.out or default_out(a.kind), width))
        return 0
    except Exception as e:
        print(f"render failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
