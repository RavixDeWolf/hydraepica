"""hostload: parsing per OS, the machine list from config, and failures said plainly."""
import subprocess
import _harness_env  # noqa: F401  (throwaway config first)
import hostload as h

G = 1024**3
KNOWN = h.machines({"build": {"ssh": "build-box", "os": "linux"}, "studio": {"ssh": "st", "os": "mac"},
                    "win": {"ssh": "w", "os": "windows"}, "bad": {"ssh": "x", "os": "beos"},
                    "nossh": {"os": "linux"}}, system="Linux")


class R:
    def __init__(self, out="", code=0, err=""):
        self.stdout, self.returncode, self.stderr = out, code, err


def run_with(out, code=0, err=""):
    seen = []
    def run(argv, **kw):
        seen.append(argv)
        return R(out, code, err)
    return run, seen


def test_machine_list():
    assert set(KNOWN) == {"local", "build", "studio", "win"}      # bad os and missing ssh are dropped
    assert KNOWN["local"] == {"ssh": None, "os": "linux"}
    assert h.machines({}, system="Darwin")["local"]["os"] == "mac"
    assert h.machines({}, system="Windows")["local"]["os"] == "windows"


def test_parse_and_unknown():
    assert h.parse_args("/ram", KNOWN) == ("/ram", "local")
    assert h.parse_args("/RAM@bot Build", KNOWN) == ("/ram", "build")
    assert h.parse_args("/ram; rm -rf /", KNOWN) == ("/ram;", None)
    assert h.report("/hello", known=KNOWN) is None
    msg = h.report("/ram nowhere", run=lambda *a, **k: 1 / 0, known=KNOWN)
    assert msg.startswith("Unknown machine") and "build" in msg


def test_argv_never_carries_the_name():
    assert h.argv_for("/ram", KNOWN["local"])[:2] == ["bash", "-c"]
    a = h.argv_for("/ram", KNOWN["build"])
    assert a[:len(h.SSH)] == h.SSH and a[len(h.SSH)] == "build-box"
    w = h.argv_for("/load", KNOWN["win"])
    assert "-EncodedCommand" in w[-1]


def test_linux_ram_and_swap():
    run, seen = run_with(f"M {16*G} {4*G} {11*G}\nS {2*G} {0}\n")
    out = h.report("/ram build", run=run, known=KNOWN)
    assert out == "🧠 build: RAM 4.0 GB used of 16.0 GB (25%), 11.0 GB available · Swap 0.0 GB of 2.0 GB"
    assert seen[0][len(h.SSH)] == "build-box"


def test_mac_outputs():
    assert "Apple M2" in h.render("/vram", "studio", "UNIFIED\nApple M2 Max\n")
    assert h.render("/load", "studio", "10\nPCT 12.5\n") == "⚡ studio: CPU 12% busy now (10 threads)"
    assert h.render("/loadavg", "studio", "10\n 1.50 1.20 1.00 \n").startswith("📈 studio: load 1.50 / 1.20 / 1.00")
    out = h.render("/storage", "studio", f"/System/Volumes/Data {500*G} {250*G}\n/Volumes/My Disk {100*G} {10*G}\n")
    assert "/Volumes/My Disk  10.0 GB" in out and "(50%)" in out


def test_windows_outputs():
    assert h.render("/load", "win", "8\nPCT 40\n") == "⚡ win: CPU 40% busy now (8 threads)"
    assert "Windows has no load average" in h.render("/loadavg", "win", "8\nAVG 33.3\n")
    assert "no usage counter" in h.render("/vram", "win", "NOCOUNT Intel UHD\n")


def test_linux_load_from_proc_stat():
    out = h.render("/load", "local", "4\ncpu 100 0 100 700 100 0 0 0\ncpu 150 0 150 750 150 0 0 0\n")
    assert out == "⚡ local: CPU 50% busy now (4 threads)"


def test_failures_are_said():
    run, _ = run_with("", 255, "ssh: connect to host")
    assert h.report("/ram build", run=run, known=KNOWN) == "⚠️ build: offline or unreachable"
    run, _ = run_with("", 1, "boom\nfree: not found")
    assert h.report("/ram build", run=run, known=KNOWN) == "⚠️ build: free: not found"
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 30)
    assert "did not answer" in h.report("/ram build", run=slow, known=KNOWN)
    run, _ = run_with("garbage\n")
    assert "could not read" in h.report("/loadavg build", run=run, known=KNOWN)


def test_lane_routes_the_commands():
    import head_lane
    for c in h.METRICS:
        assert head_lane.command_of(c + " build") == c
