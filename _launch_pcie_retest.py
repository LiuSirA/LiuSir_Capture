import subprocess, os, time, sys
from pathlib import Path
from datetime import datetime

def list_pids():
    out = []
    try:
        raw = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe' or name='python3.12.exe' or name='pythonw.exe'", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
            text=True, errors="replace",
        )
    except Exception as e:
        print("wmic err", e)
        return out
    cur_cmd, cur_pid = "", None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("CommandLine="):
            cur_cmd = line[len("CommandLine="):]
        elif line.startswith("ProcessId="):
            try:
                cur_pid = int(line[len("ProcessId="):])
            except Exception:
                cur_pid = None
            if cur_pid and cur_cmd and any(k in cur_cmd for k in ("sync_acc_run", "sync_acc_autonomous", "sync_acc_mijia_power")):
                out.append((cur_pid, cur_cmd))
            cur_cmd, cur_pid = "", None
    return out

pids = list_pids()
print("FOUND", len(pids))
for pid, cmd in pids:
    # Do not kill unrelated OCR; kill prior runners/autonomous for clean start
    print("KILL", pid, cmd[:200])
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
time.sleep(1.0)

DATA = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3")
LOGS = DATA / "logs"
for d in (LOGS, DATA / "status", DATA / "groups", DATA / "reports"):
    d.mkdir(parents=True, exist_ok=True)

LOCK = Path(r"J:\Mywork\Project_LH\sync_acc_runs\_locks\autonomous_pcie_retest.instance.lock")
LOCK.parent.mkdir(parents=True, exist_ok=True)
if LOCK.exists():
    LOCK.unlink()
    print("cleared lock")

# also clear old pcie autonomous lock if stale
for name in ("autonomous_pcie.instance.lock", "autonomous_usb_retest.instance.lock"):
    p = LOCK.parent / name
    if p.exists():
        try:
            old = int(p.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            old = -1
        alive = False
        if old > 0:
            try:
                out = subprocess.check_output(["tasklist", "/FI", f"PID eq {old}", "/FO", "CSV", "/NH"], text=True, errors="replace")
                alive = str(old) in out and "INFO:" not in out
            except Exception:
                alive = False
        if not alive:
            p.unlink()
            print("cleared stale", name)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
outer_log = LOGS / ("launch_pcie_retest_%s.log" % stamp)
script = Path(r"J:\Mywork\Project_LH\git\LiuSir_Capture\sync_acc_autonomous_pcie_retest.py")
pyw = Path(sys.executable).with_name("pythonw.exe")
exe = str(pyw) if pyw.exists() else sys.executable

env = os.environ.copy()
env["SYNC_ACC_DATA_ROOT"] = str(DATA)
env["SYNC_ACC_TRANSPORT"] = "pcie"
env["SYNC_ACC_MASTER"] = "QHY600MPCIE-a7c01148f4017d814a3c59ef3d57c907"
env["SYNC_ACC_SLAVE"] = "QHY600MPCIE-847c685cf4d479cf2c6ed43f08ada04d"
env["SYNC_ACC_GAIN"] = "50"
env["SYNC_ACC_EXP_US"] = "10000"
env["SYNC_ACC_TRAFFIC_UNSET"] = "1"   # capture_gui leaves usb_traffic to the camera default
env["SYNC_ACC_NO_POWER"] = "1"        # Mijia power cycling loses the slave; recover manually
env["SYNC_ACC_SKIP_BIND"] = "1"
env["SYNC_ACC_GPS_MODE"] = "after"     # frame seq needs the SDK GPS overlay; applied after Live like capture_gui
if os.environ.get("SYNC_ACC_GROUPS"):
    env["SYNC_ACC_GROUPS"] = os.environ["SYNC_ACC_GROUPS"]

creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
with open(outer_log, "w", encoding="utf-8") as lf:
    lf.write("launch exe=%s script=%s\n" % (exe, script))
    lf.flush()
    proc = subprocess.Popen(
        [exe, str(script)],
        cwd=str(script.parent),
        stdout=lf,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=creation,
        close_fds=True,
    )
print("STARTED pid", proc.pid)
print("OUTER_LOG", outer_log)
(DATA / "status" / "_launch_pid.txt").write_text("%s %s\n" % (proc.pid, outer_log), encoding="utf-8")
time.sleep(5.0)
alive = True
try:
    os.kill(proc.pid, 0)
except Exception:
    alive = False
print("ALIVE_AFTER_5S", alive)
logs = sorted(LOGS.glob("autonomous_pcie_retest_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
print("AUTONOMOUS_LOGS", [str(x) for x in logs[:3]])
if logs:
    print("--- first lines ---")
    print(logs[0].read_text(encoding="utf-8", errors="replace")[:2000])
print("--- outer ---")
print(outer_log.read_text(encoding="utf-8", errors="replace")[:2000])