"""hostload.py - /ram /vram /storage /load /loadavg in any head's topic.

"/ram" = the machine HydraEpica runs on. "/ram build" = a machine named in the config:

    [machines]
    build = { ssh = "build-box", os = "linux" }      # os: linux | mac | windows
    studio = { ssh = "studio.local", os = "mac" }

The local machine's OS is detected, so with no [machines] table every command works out of the box.
Read-only: every probe is a fixed command; the machine name only picks from the config, never reaches a shell.
An unreachable machine is said plainly, never shown as zeros.
"""
import base64
import platform
import subprocess

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
METRICS = ("/ram", "/vram", "/storage", "/load", "/loadavg")
OSES = ("linux", "mac", "windows")

LINUX = {
    "/ram": "free -b | awk '/^Mem:/{print \"M\",$2,$3,$7} /^Swap:/{print \"S\",$2,$3}'",
    "/vram": "command -v nvidia-smi >/dev/null || { echo NONE; exit 0; }; nvidia-smi "
             "--query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits",
    # ZFS datasets share one pool, so they are shown once per pool (zpool), never per dataset
    "/storage": "df -B1 -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs -x zfs --output=target,size,used "
                "| tail -n +2; command -v zpool >/dev/null && zpool list -Hp -o name,size,alloc 2>/dev/null "
                "| sed 's/^/zfs:/'; true",
    "/load": "nproc; head -1 /proc/stat; sleep 1; head -1 /proc/stat",
    "/loadavg": "nproc; cat /proc/loadavg",
}
MAC = {
    # used = active + wired + compressed pages; available = free + inactive + speculative
    "/ram": "ps=$(sysctl -n hw.pagesize); vm_stat | awk -v ps=$ps -v tot=$(sysctl -n hw.memsize) "
            "'/Pages free/{f=$3}/Pages inactive/{i=$3}/Pages speculative/{s=$3}/Pages active/{a=$3}"
            "/Pages wired/{w=$4}/occupied by compressor/{c=$5}"
            "END{print \"M\",tot,(a+w+c)*ps,(f+i+s)*ps}'; sysctl -n vm.swapusage | "
            "awk '{t=$3;u=$6;sub(/M/,\"\",t);sub(/M/,\"\",u);print \"S\",t*1048576,u*1048576}'",
    "/vram": "echo UNIFIED; sysctl -n machdep.cpu.brand_string",
    # APFS lists its system volumes separately; keep / , the Data volume and real disks under /Volumes
    "/storage": "df -k -l | awk 'NR>1{m=$9; for(i=10;i<=NF;i++) m=m\" \"$i; "
                "if (m ~ /^\\/System\\/Volumes\\// && m != \"/System/Volumes/Data\") next; print m,$2*1024,$3*1024}'",
    "/load": "sysctl -n hw.ncpu; top -l 2 -n 0 -s 1 | awk '/CPU usage/{l=$7} END{sub(/%/,\"\",l); print \"PCT\",100-l}'",
    "/loadavg": "sysctl -n hw.ncpu; sysctl -n vm.loadavg | tr -d '{}'",
}
WIN = {
    "/ram": "$o=Get-CimInstance Win32_OperatingSystem; $p=Get-CimInstance Win32_PageFileUsage | Measure-Object "
            "AllocatedBaseSize,CurrentUsage -Sum; \"M $($o.TotalVisibleMemorySize*1024) "
            "$(($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)*1024) $($o.FreePhysicalMemory*1024)\"; "
            "\"S $($p[0].Sum*1MB) $($p[1].Sum*1MB)\"",
    "/vram": "$s=Get-Command nvidia-smi -ErrorAction SilentlyContinue; if($s){& $s.Source "
             "--query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits}else{"
             "Get-CimInstance Win32_VideoController | ForEach-Object {\"NOCOUNT $($_.Name)\"}}",
    "/storage": "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object "
                "{\"$($_.DeviceID) $($_.Size) $($_.Size-$_.FreeSpace)\"}",
    "/load": "\"$((Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors)\"; "
             "\"PCT $((Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average)\"",
    "/loadavg": "\"$((Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors)\"; "
                "$v=(Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 5).CounterSamples"
                " | Measure-Object CookedValue -Average; \"AVG $([math]::Round($v.Average,1))\"",
}
PROBES = {"linux": LINUX, "mac": MAC, "windows": WIN}


def local_os(system=None):
    s = (system or platform.system()).lower()
    return {"darwin": "mac", "windows": "windows"}.get(s, "linux")


def machines(table=None, system=None):
    """name -> {"ssh": alias or None, "os": ...}. 'local' is always there. Pure given its inputs."""
    if table is None:
        import config
        table = config.section("machines")
    out = {"local": {"ssh": None, "os": local_os(system)}}
    for name, m in (table or {}).items():
        os_ = str(m.get("os", "linux")).lower()
        if m.get("ssh") and os_ in OSES:
            out[str(name).lower()] = {"ssh": str(m["ssh"]), "os": os_}
    return out


def parse_args(text, known):
    """'/ram build' -> ('/ram', 'build'); '/ram' -> ('/ram', 'local'); unknown -> ('/ram', None). Pure."""
    parts = (text or "").strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    name = parts[1].lower() if len(parts) > 1 else "local"
    return cmd, (name if name in known else None)


def argv_for(cmd, m):
    """Fixed argv for a metric on a machine. Pure."""
    script = PROBES[m["os"]][cmd]
    if m["os"] == "windows":
        line = "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16-le")).decode()
        return SSH + [m["ssh"], line] if m["ssh"] else ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    if m["ssh"] is None:
        return ["bash", "-c", script]
    return SSH + [m["ssh"], script]


def gb(n):
    return f"{float(n) / 1024**3:.1f} GB"


def pct(a, b):
    return f"{100 * float(a) / float(b):.0f}%" if float(b) else "?"


def render(cmd, who, out):
    """Raw probe output -> one short message. Pure."""
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    if cmd == "/ram":
        r = []
        for l in lines:
            f = l.split()
            if f[0] == "M":
                r.append(f"RAM {gb(f[2])} used of {gb(f[1])} ({pct(f[2], f[1])}), {gb(f[3])} available")
            elif f[0] == "S" and float(f[1]):
                r.append(f"Swap {gb(f[2])} of {gb(f[1])}")
        return f"🧠 {who}: " + " · ".join(r)
    if cmd == "/vram":
        if not lines or lines[0] == "NONE":
            return f"🎮 {who}: no NVIDIA GPU"
        if lines[0] == "UNIFIED":
            chip = lines[1] if len(lines) > 1 else "Apple silicon"
            return f"🎮 {who}: {chip}, unified memory (GPU shares RAM; see /ram)"
        r = []
        for l in lines:
            if l.startswith("NOCOUNT"):
                r.append(f"{l[8:]} (no usage counter on this machine)")
                continue
            name, used, tot, util = [x.strip() for x in l.split(",")]
            r.append(f"{name}: {int(used)/1024:.1f} of {int(tot)/1024:.1f} GB ({pct(used, tot)}), GPU {util}% busy")
        return f"🎮 {who}: " + " · ".join(r)
    if cmd == "/storage":
        r = []
        for l in lines:
            mnt, size, used = l.rsplit(None, 2)
            if float(size) >= 2 * 1024**3:
                r.append(f"{mnt}  {gb(used)} of {gb(size)} ({pct(used, size)})")
        return f"💾 {who}:\n" + "\n".join(r)
    cores = lines[0]
    if cmd == "/load":
        if lines[1].startswith("PCT"):
            return f"⚡ {who}: CPU {float(lines[1].split()[1]):.0f}% busy now ({cores} threads)"
        a = [int(x) for x in lines[1].split()[1:]]
        b = [int(x) for x in lines[2].split()[1:]]
        tot = sum(b) - sum(a)
        idle = (b[3] + b[4]) - (a[3] + a[4])
        return f"⚡ {who}: CPU {100 * (tot - idle) / tot:.0f}% busy now ({cores} threads)" if tot else f"⚡ {who}: ?"
    if lines[1].startswith("AVG"):
        return f"📈 {who}: CPU {lines[1].split()[1]}% average over 5 s ({cores} threads; Windows has no load average)"
    l1, l5, l15 = lines[1].split()[:3]
    return f"📈 {who}: load {l1} / {l5} / {l15} (1 / 5 / 15 min) on {cores} threads"


def report(text, run=subprocess.run, known=None):
    """Full text of a Telegram command -> reply text (None if it isn't one of ours)."""
    known = known if known is not None else machines()
    cmd, name = parse_args(text, known)
    if cmd not in METRICS:
        return None
    if name is None:
        return f"Unknown machine. Use one of: {', '.join(known)} (e.g. {cmd} {list(known)[-1]})."
    m, who = known[name], name
    try:
        r = run(argv_for(cmd, m), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return f"⚠️ {who} did not answer in 30 s."
    except FileNotFoundError as e:
        return f"⚠️ {who}: {e.filename} is not installed here"
    if r.returncode != 0 or not r.stdout.strip():
        why = "offline or unreachable" if r.returncode == 255 else (r.stderr.strip().splitlines() or ["no output"])[-1][:150]
        return f"⚠️ {who}: {why}"
    try:
        return render(cmd, who, r.stdout)
    except (ValueError, IndexError) as e:
        return f"⚠️ {who}: could not read the answer ({type(e).__name__})"
