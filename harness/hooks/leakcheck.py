"""Demo-safe leak check. A lane whose state dir holds a `demo_safe` file (a topic you film or show people) never
sends IP addresses, internal paths, keys, local service ports, this machine's account name, or the names you list
in the config. On Stop it checks the reply text (MEDIA: lines removed, they are stripped before sending) and OCRs
every image the reply attaches (tesseract). On PreToolUse it checks the status-bubble label and mid-turn sends.
A hit blocks the reply or the step and tells the head why; an image that can't be read is a hit (never send blind).

    [leakcheck]
    aliases = { "build-box" = "Builder", "nas01" = "Storage" }   # real name -> what the demo thread says instead
    words = ["Project Falcon"]                                   # anything else that must never appear
    domains = ["internal.example.com"]                            # any subdomain of these is flagged (www is fine)
"""
import getpass, json, os, re, subprocess, time
_MEDIA = re.compile(r"^\s*MEDIA:\s*(/\S+)\s*$", re.M)
RULES = [
    ("IP address", re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")),
    ("internal path", re.compile(r"(?:~/|/home/|/etc/|/mnt/|/opt/|/var/|/srv/|/root/|/tmp/|/run/)[\w.\-/]*")),
    ("local service port", re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d{2,5}\b")),
    ("key or token", re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|xox[bpa]-[\w\-]{10,}"
                                r"|\d{8,10}:AA[\w\-]{30,}|eyJ[\w\-]{20,}\.[\w\-]{20,})")),
    ("long secret-like string", re.compile(r"\b[A-Fa-f0-9]{40,}\b")),
]
IMG = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def enabled(state_dir):
    return os.path.exists(os.path.join(state_dir, "demo_safe"))


def find(text):
    """-> [(rule, matched text)] for every hit, deduplicated, in order."""
    hits, seen = [], set()
    for name, rx in RULES:
        for m in rx.finditer(text or ""):
            k = (name, m.group(0))
            if k not in seen:
                seen.add(k); hits.append(k)
    return hits


def ocr(path):
    r = subprocess.run(["tesseract", path, "-", "--psm", "11"], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"tesseract exit {r.returncode}: {r.stderr.strip()[:120]}")
    return r.stdout


def scan_reply(text, ocr_fn=ocr):
    """-> list of problem lines ('' list = clean). An image that can't be read is a problem: never send blind."""
    probs = []
    body = _MEDIA.sub("", text or "")
    for rule, m in find_all(body):
        probs.append(f"reply text: {rule} '{m}'")
    for p in _MEDIA.findall(text or ""):
        if not p.lower().endswith(IMG):
            continue
        name = os.path.basename(p)
        try:
            for rule, m in find_all(ocr_fn(p)):
                probs.append(f"image {name}: {rule} '{m}'")
        except Exception as e:
            probs.append(f"image {name}: could not be checked ({e})")
    return probs


def reason(probs):
    return ("Demo-safe leak check (this thread is filmed): the reply would show " + "; ".join(probs[:8])
            + ". Remove or describe it by role (\"the home server\", \"the site folder\"), crop or retake the image, "
            "then send again.")


def log(state_dir, probs):
    """One ISO-stamped line per checked reply in <state>/leakcheck.log (held or clean)."""
    try:
        with open(os.path.join(state_dir, "leakcheck.log"), "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + json.dumps({"held": bool(probs), "problems": probs[:8]}) + "\n")
    except OSError:
        pass


def _settings():
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # harness/
        import config
        return config.section("leakcheck") or {}
    except (Exception, SystemExit):
        return {}


def _build(s):
    """Config -> (aliases, extra rules). Pure."""
    aliases = {str(k): str(v) for k, v in (s.get("aliases") or {}).items()}
    rules = []
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if user and len(user) >= 3 and user not in ("root", "user", "admin"):
        rules.append(("account name", re.compile(r"\b" + re.escape(user) + r"\b", re.I)))
    words = [str(w) for w in (s.get("words") or []) if str(w).strip()]
    if words:
        rules.append(("private word", re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I)))
    doms = [str(d).lower().lstrip(".") for d in (s.get("domains") or []) if str(d).strip()]
    if doms:
        rules.append(("internal site name", re.compile(r"\b(?!www\.)[a-z0-9-]+\.(?:" + "|".join(map(re.escape, doms))
                                                      + r")\b", re.I)))
    return aliases, rules


ALIASES, _EXTRA = _build(_settings())
RULES.extend(_EXTRA)
_NAME = (re.compile(r"(?<![\w.-])(" + "|".join(sorted(map(re.escape, ALIASES), key=len, reverse=True)) + r")(?![\w-])")
         if ALIASES else None)
TEXTISH = (".html", ".htm", ".md", ".txt")


def find_all(text):
    """find() plus the configured machine names (each hit says which alias to use)."""
    hits = find(text)
    for m in dict.fromkeys(_NAME.findall(text or "") if _NAME else ()):
        hits.append((f"server name (say '{ALIASES[m]}')", m))
    return hits


def label_of(name, inp):
    """The line the Telegram status bubble shows for this tool call (mirrors head_lane.tool_brief). '' = none."""
    inp = inp or {}
    if name == "Bash":
        return (inp.get("description") or inp.get("command") or "").strip().split("\n")[0][:70]
    if name in ("Grep", "Glob"):
        return inp.get("pattern") or ""
    if name in ("Agent", "Task"):
        return inp.get("description") or ""
    if name in ("WebSearch", "WebFetch"):
        return inp.get("query") or inp.get("url") or ""
    return ""


def _check_arg(tok, ocr_fn, probs, where):
    p = os.path.expanduser(tok)
    if tok.startswith(("/", "~")) and os.path.isfile(p):       # a file being sent: check what it shows
        n = os.path.basename(p)
        try:
            if p.lower().endswith(IMG):
                probs += [f"{where} image {n}: {r} '{m}'" for r, m in find_all(ocr_fn(p))]
            elif p.lower().endswith(TEXTISH):
                probs += [f"{where} file {n}: {r} '{m}'" for r, m in find_all(open(p, errors="replace").read())]
        except Exception as e:
            probs.append(f"{where} image {n}: could not be checked ({e})")
    else:
        probs += [f"{where}: {r} '{m}'" for r, m in find_all(tok)]


_SEND = re.compile(r"\b(tg|stage|bg)\.py\b")


def scan_tool(name, inp, ocr_fn=ocr):
    """PreToolUse: problems with what this call would put in the thread (its label, or a mid-turn send). Pure-ish."""
    import shlex
    inp, probs = inp or {}, []
    lab = label_of(name, inp)
    probs += [f"step label: {r} '{m}'" for r, m in find_all(lab)]
    if name == "Bash":
        cmd = inp.get("command") or ""
        for seg in re.split(r"&&|\|\||;|\n", cmd):
            m = _SEND.search(seg)
            if not m:
                continue
            try:
                toks = shlex.split(seg[m.end():])
            except ValueError:
                toks = seg[m.end():].split()
            if m.group(1) == "bg":                           # only the bar's name is shown
                toks = [toks[i + 1] for i, t in enumerate(toks[:-1]) if t == "--name"]
            for t in toks:
                _check_arg(t, ocr_fn, probs, f"{m.group(1)}.py send")
    elif name.endswith("tg_send"):
        for k in ("message", "text", "caption"):
            if inp.get(k):
                _check_arg(str(inp[k]), ocr_fn, probs, "tg_send")
        for k in ("file", "path", "file_path"):
            if inp.get(k):
                _check_arg(str(inp[k]), ocr_fn, probs, "tg_send")
    return probs


def tool_reason(probs):
    return ("Demo-safe leak check (this thread is filmed): this step would show " + "; ".join(probs[:8])
            + ". Give it a plain label / message (describe by role, use the machine aliases), then run it again.")
