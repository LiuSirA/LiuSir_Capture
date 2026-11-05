import subprocess, os, time
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
    print("KILL", pid, cmd[:200])
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
time.sleep(1.0)

DATA = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261103_usb")
LOGS = DATA / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
(DATA / "status").mkdir(parents=True, exist_ok=True)
(DATA / "groups").mkdir(parents=True, exist_ok=True)
(DATA / "reports").mkdir(parents=True, exist_ok=True)
LOCK = Path(r"J:\Mywork\Project_LH\sync_acc_runs\_locks\autonomous_usb_retest.instance.lock")
LOCK.parent.mkdir(parents=True, exist_ok=True)
if LOCK.exists():
    LOCK.unlink()
    print("cleared lock")

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
outer_log = LOGS / ("launch_usb_retest_%s.log" % stamp)
script = Path(r"J:\Mywork\Project_LH\git\LiuSir_Capture\sync_acc_autonomous_usb_retest.py")
# Prefer pythonw if available for background; else python with CREATE_NEW_PROCESS_GROUP
py = Path(r"C:\Users\catme\AppData\Local\Microsoft\WindowsApps\pythonw.exe")
if not py.exists():
    py = Path(r"C:\Users\catme\AppData\Local\Microsoft\WindowsApps\python.exe")
# Use same interpreter as interactive python
import sys
pyw = Path(sys.executable).with_name("pythonw.exe")
if pyw.exists():
    exe = str(pyw)
else:
    exe = sys.executable

env = os.environ.copy()
env["SYNC_ACC_DATA_ROOT"] = str(DATA)
env["SYNC_ACC_TRANSPORT"] = "usb"
env.setdefault("SYNC_ACC_MASTER", "QHY600M-ac14f178435e35c7")
env.setdefault("SYNC_ACC_SLAVE", "QHY600M-8765fd7c26d38aa4d")

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
Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261103_usb\status\_launch_pid.txt").write_text(
    "%s %s\n" % (proc.pid, outer_log), encoding="utf-8"
)
time.sleep(3.0)
alive = True
try:
    os.kill(proc.pid, 0)
except Exception:
    alive = False
print("ALIVE_AFTER_3S", alive)
# also list new autonomous logs
logs = sorted(LOGS.glob("autonomous_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
print("AUTONOMOUS_LOGS", [str(x) for x in logs[:3]])
if logs:
    print("--- first lines ---")
    print(logs[0].read_text(encoding="utf-8", errors="replace")[:1500])
print("--- outer ---")
print(outer_log.read_text(encoding="utf-8", errors="replace")[:1500])
