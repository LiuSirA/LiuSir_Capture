# -*- coding: utf-8 -*-
"""Autonomous sync-accuracy driver for note 02787256 — PCIe transport (isolated data root)."""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CODE = Path(r"J:\Mywork\Project_LH\git\LiuSir_Capture")
DATA = Path(os.environ.get("SYNC_ACC_DATA_ROOT", r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie"))
LOCK_ROOT = Path(r"J:\Mywork\Project_LH\sync_acc_runs\_locks")
INSTANCE_LOCK = LOCK_ROOT / "autonomous_pcie.instance.lock"
TRANSPORT = os.environ.get("SYNC_ACC_TRANSPORT", "pcie").strip().lower() or "pcie"
MIJIA_LOCK = LOCK_ROOT / "mijia.power.lock"
PY = sys.executable
RUNNER = CODE / "sync_acc_run.py"
MIJIA = CODE / "sync_acc_mijia_power.py"
MIJIA_PY = Path(r"J:\Mywork\Project_LH\git\mijia-panel\.venv\Scripts\python.exe")

def runner_args(*extra: str) -> list[str]:
    """Build sync_acc_run argv with forced --transport."""
    return [PY, str(RUNNER), "--transport", TRANSPORT, *extra]


# PCIe enumerates as QHY600MPCIE-*; drop stale USB ID prefs if inherited.
os.environ["SYNC_ACC_DATA_ROOT"] = str(DATA)  # hard-isolate PCIe from USB 20261102
os.environ["SYNC_ACC_TRANSPORT"] = TRANSPORT
for _k in ("SYNC_ACC_MASTER", "SYNC_ACC_SLAVE"):
    _v = os.environ.get(_k, "").strip()
    if _v.startswith("QHY600M-") and "PCIE" not in _v.upper():
        os.environ.pop(_k, None)

STATUS = DATA / "status"
LOGS = DATA / "logs"
REPORTS = DATA / "reports"
STATUS.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)
LOCK_ROOT.mkdir(parents=True, exist_ok=True)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_path = LOGS / f"autonomous_{stamp}.log"
state_path = STATUS / "_sync_test_status.json"


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"groups": {}, "gates": {}, "updated": None}


def save_state(st: dict) -> None:
    st["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    state_path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        # Windows: OpenProcess via tasklist / os.kill(pid, 0) may raise
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            errors="replace",
        )
        return str(pid) in out and "INFO:" not in out
    except Exception:
        return False


def acquire_instance_lock() -> None:
    if INSTANCE_LOCK.exists():
        try:
            old = int(INSTANCE_LOCK.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            old = -1
        if old > 0 and old != os.getpid() and _pid_alive(old):
            print(f"another autonomous alive pid={old}; exit", flush=True)
            raise SystemExit(9)
        print(f"stale instance lock pid={old}; clearing", flush=True)
        try:
            INSTANCE_LOCK.unlink()
        except Exception:
            pass
    INSTANCE_LOCK.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")

    def _clear():
        try:
            if INSTANCE_LOCK.exists():
                cur = INSTANCE_LOCK.read_text(encoding="utf-8").strip().split()[0]
                if cur == str(os.getpid()):
                    INSTANCE_LOCK.unlink()
        except Exception:
            pass

    atexit.register(_clear)


def with_mijia_lock(timeout_s: float = 180.0) -> bool:
    """Return True if we hold mijia lock; False if timed out waiting."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if MIJIA_LOCK.exists():
            try:
                old = int(MIJIA_LOCK.read_text(encoding="utf-8").strip().split()[0])
            except Exception:
                old = -1
            if old > 0 and _pid_alive(old) and old != os.getpid():
                time.sleep(2.0)
                continue
            try:
                MIJIA_LOCK.unlink()
            except Exception:
                pass
        MIJIA_LOCK.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
        return True
    return False


def release_mijia_lock() -> None:
    try:
        if MIJIA_LOCK.exists():
            cur = MIJIA_LOCK.read_text(encoding="utf-8").strip().split()[0]
            if cur == str(os.getpid()):
                MIJIA_LOCK.unlink()
    except Exception:
        pass


def list_sync_acc_pids() -> list[tuple[int, str]]:
    out = []
    try:
        raw = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe' or name='python3.12.exe' or name='pythonw.exe'", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
            text=True,
            errors="replace",
        )
    except Exception:
        # fallback powershell-less: tasklist only shows names
        return out
    cur_cmd, cur_pid = "", None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("CommandLine="):
            cur_cmd = line[len("CommandLine=") :]
        elif line.startswith("ProcessId="):
            try:
                cur_pid = int(line[len("ProcessId=") :])
            except Exception:
                cur_pid = None
            if cur_pid and cur_cmd and any(
                k in cur_cmd for k in ("sync_acc_run", "sync_acc_autonomous", "sync_acc_mijia_power")
            ):
                out.append((cur_pid, cur_cmd))
            cur_cmd, cur_pid = "", None
    return out


def kill_other_sync_acc() -> None:
    """Only stop other sync_acc_run camera grabbers. Never touch OCR/tesseract/autonomous."""
    for pid, cmd in list_sync_acc_pids():
        if pid == os.getpid():
            continue
        low = cmd.lower()
        if "tesseract" in low or "_ocr" in low or "run_g1g6_ocr" in low or "ocr_work" in low:
            log(f"KEEP OCR-related pid={pid}")
            continue
        if "sync_acc_autonomous" in cmd:
            log(f"KEEP autonomous-related pid={pid} (instance lock owns serialization)")
            continue
        if "sync_acc_mijia_power" in cmd:
            log(f"KEEP mijia pid={pid}")
            continue
        if "sync_acc_run.py" not in cmd and "sync_acc_run" not in cmd:
            log(f"KEEP non-runner pid={pid}")
            continue
        log(f"KILL sync_acc_run pid={pid}")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
        except Exception as exc:
            log(f"kill warn {exc}")
    time.sleep(1.5)



def run_cmd(args: list[str], timeout_s: float | None = None) -> int:
    log("CMD " + " ".join(args))
    with log_path.open("a", encoding="utf-8") as f:
        f.write("CMD " + " ".join(args) + "\n")
        try:
            p = subprocess.run(
                args,
                cwd=str(CODE),
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                env=os.environ.copy(),
            )
            log(f"EXIT {p.returncode}")
            return int(p.returncode)
        except subprocess.TimeoutExpired:
            log("TIMEOUT cmd")
            return 124


def power_cycle(off_s: float = 15.0, boot_s: float = 60.0) -> bool:
    py = str(MIJIA_PY if MIJIA_PY.exists() else PY)
    if not with_mijia_lock(180):
        log("POWER skip: mijia lock busy")
        return False
    try:
        log(f"POWER cycle QHY600Eris off={off_s} boot={boot_s}")
        rc = run_cmd(
            [py, str(MIJIA), "cycle", "--off-s", str(off_s), "--boot-s", str(boot_s)],
            timeout_s=max(180, int(off_s + boot_s + 60)),
        )
        return rc == 0
    finally:
        release_mijia_lock()


def cameras_ready(need: int = 2, tries: int = 6, gap_s: float = 5.0) -> bool:
    """Quick scan via runner import path using a tiny inline script."""
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "CODE = Path(r'J:\\Mywork\\Project_LH\\git\\LiuSir_Capture')\n"
        "sys.path.insert(0, str(CODE / 'host'))\n"
        "sys.path.insert(0, str(CODE))\n"
        "from qhyccd_highlevel import QhyApi, scan_cameras\n"
        "api = QhyApi(CODE / 'host')\n"
        "ids = scan_cameras(api)\n"
        "names=[]\n"
        "for b in ids:\n"
        "    s=b.split(b'\\x00',1)[0].decode('ascii','replace')\n"
        "    names.append(s)\n"
        "print('SCAN_N=%d'%len(names))\n"
        "print('SCAN_IDS=' + ','.join(names))\n"
    )
    for i in range(tries):
        try:
            p = subprocess.run(
                [PY, "-c", probe],
                cwd=str(CODE),
                capture_output=True,
                text=True,
                timeout=90,
                env=os.environ.copy(),
            )
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            n = 0
            names = []
            for ln in out.splitlines():
                ln = ln.strip()
                if ln.startswith("SCAN_N="):
                    try:
                        n = int(ln.split("=", 1)[1])
                    except Exception:
                        n = 0
                if ln.startswith("SCAN_IDS="):
                    names = [x for x in ln.split("=", 1)[1].split(",") if x]
            log(f"SCAN try={i+1} n={n} ids={names}")
            if n >= need:
                return True
        except Exception as exc:
            log(f"SCAN err {exc}")
        time.sleep(gap_s)
    return False



def bind_pcie_roles() -> tuple[str, str]:
    """Map Master/Slave to currently enumerated QHY600MPCIE IDs."""
    helper = CODE / "sync_acc_bind_roles.py"
    p = subprocess.run(
        [PY, str(helper)],
        cwd=str(CODE),
        capture_output=True,
        text=True,
        timeout=90,
        env=os.environ.copy(),
    )
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    names: list[str] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("SCAN_IDS="):
            names = [x for x in ln.split("=", 1)[1].split(",") if x]
    if len(names) < 2:
        raise RuntimeError(f"bind_pcie_roles need 2 cams, got {names!r} rc={p.returncode}")
    m_pref = os.environ.get("SYNC_ACC_MASTER", "").strip()
    s_pref = os.environ.get("SYNC_ACC_SLAVE", "").strip()
    if m_pref in names and s_pref in names and m_pref != s_pref:
        master, slave = m_pref, s_pref
    elif m_pref in names:
        master = m_pref
        slave = next(n for n in names if n != master)
    else:
        # Default: first enumerated = Master (must be the SMA/IDLE driver unit).
        master, slave = names[0], names[1]
    os.environ["SYNC_ACC_MASTER"] = master
    os.environ["SYNC_ACC_SLAVE"] = slave
    log(f"PCIe roles MASTER={master} SLAVE={slave} scan={names}")
    (STATUS / "pcie_roles.json").write_text(
        json.dumps({"master": master, "slave": slave, "scan": names}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return master, slave


def read_lock(bin_tag: str) -> float | None:
    for name in (f"traffic_lock_{bin_tag}.json", "traffic_lock.json"):
        p = STATUS / name
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if name.endswith(f"_{bin_tag}.json") or (name == "traffic_lock.json" and bin_tag == "1x1"):
                v = doc.get("locked_traffic")
                if v is not None:
                    return float(v)
        except Exception:
            pass
    st = load_state()
    v = (st.get("gates") or {}).get(bin_tag, {}).get("locked_traffic")
    if v is None:
        return None
    return float(v)


def clear_stale_traffic_locks() -> None:
    for name in ("traffic_lock.json", "traffic_lock_1x1.json", "traffic_lock_3x3.json"):
        p = STATUS / name
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if doc.get("locked_traffic") is None:
                log(f"clear null lock file {name}")
                # keep file but ensure null does not confuse; already null
        except Exception:
            pass


def gate(
    bin_n: int,
    start: float,
    n_frames: int,
    burst_end: int,
    label: str,
    *,
    need_clean: int = 2,
    force: bool = False,
) -> float | None:
    bin_tag = f"{bin_n}x{bin_n}"
    existing = read_lock(bin_tag)
    # Only skip climb when a real (non-null) lock exists and not forced.
    if (not force) and existing is not None and label.startswith("climb"):
        log(f"GATE skip climb {bin_tag} locked={existing}")
        return existing
    rc = run_cmd(
                runner_args(
            "--gate",
            "--bin",
            str(bin_n),
            "--start-traffic",
            str(start),
            "--traffic-step",
            "5",
            "--max-traffic",
            "60",
            "--n-frames",
            str(n_frames),
            "--burst-start",
            "1",
            "--burst-end",
            str(burst_end),
            "--need-clean",
            str(need_clean),
        
        ),
        timeout_s=3600,
    )
    locked = read_lock(bin_tag)
    st = load_state()
    st.setdefault("gates", {})[bin_tag] = {
        "locked_traffic": locked,
        "rc": rc,
        "label": label,
        "n_frames": n_frames,
        "need_clean": need_clean,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    save_state(st)
    return locked


def capture_group(gid: str, bin_n: int, n_eff: int, traffic: float) -> dict:
    burst_start = 1
    burst_end = n_eff + 2
    tag = f"{gid}_b{bin_n}_t{int(traffic):03d}_n{n_eff}_{datetime.now().strftime('%H%M%S')}"
    rc = run_cmd(
                runner_args(
            "--bin",
            str(bin_n),
            "--n-frames",
            str(n_eff),
            "--traffic",
            str(traffic),
            "--burst-start",
            str(burst_start),
            "--burst-end",
            str(burst_end),
            "--tag",
            tag,
            "--save-images",
        
        ),
        timeout_s=7200,
    )
    cap = STATUS / f"capture_{tag}.json"
    doc: dict = {}
    if cap.exists():
        doc = json.loads(cap.read_text(encoding="utf-8"))
    doc["rc"] = rc
    doc["group_id"] = gid
    st = load_state()
    st.setdefault("groups", {})[gid] = {
        "tag": tag,
        "ok": doc.get("ok"),
        "any_drop": doc.get("any_drop"),
        "M_missing": doc.get("M_missing"),
        "S_missing": doc.get("S_missing"),
        "group_dir": doc.get("group_dir"),
        "traffic": traffic,
        "bin": bin_n,
        "n": n_eff,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    save_state(st)
    return doc


def lock_resolution(bin_n: int) -> float | None:
    bin_tag = f"{bin_n}x{bin_n}"
    locked: float | None = None
    # Phase A1: short window (5 effective frames, end=7)
    for attempt in range(1, 4):
        log(f"=== climb {bin_tag} attempt {attempt} short window ===")
        # force=True so null lock never skips; real lock may skip inside gate()
        need = 2 if attempt == 1 else 1
        start = 0.0 if attempt < 3 else 10.0
        locked = gate(
            bin_n,
            start=start,
            n_frames=5,
            burst_end=7,
            label=f"climb_short_{attempt}",
            need_clean=need,
            force=(attempt > 1),
        )
        if locked is not None:
            break
        # escalate power cycle on repeated failure
        off, boot = (15.0, 60.0) if attempt == 1 else (20.0, 90.0)
        power_cycle(off_s=off, boot_s=boot)
        if not cameras_ready(2, tries=8, gap_s=5.0):
            log("cameras not ready after power_cycle; longer wait")
            power_cycle(off_s=20.0, boot_s=90.0)
            cameras_ready(2, tries=10, gap_s=6.0)
    if locked is None:
        locked = gate(
            bin_n,
            start=30.0,
            n_frames=5,
            burst_end=7,
            label="climb_short_from30",
            need_clean=1,
            force=True,
        )
    if locked is None:
        return None

    # Phase A2: verify with 9-frame window at locked traffic
    log(f"=== verify {bin_tag} traffic={locked} n=9 ===")
    tag = f"verify_{bin_tag}_t{int(locked):03d}_{datetime.now().strftime('%H%M%S')}"
    run_cmd(
                runner_args(
            "--bin",
            str(bin_n),
            "--n-frames",
            "9",
            "--traffic",
            str(locked),
            "--burst-start",
            "1",
            "--burst-end",
            "11",
            "--tag",
            tag,
        
        ),
        timeout_s=1800,
    )
    cap = STATUS / f"capture_{tag}.json"
    ok = False
    if cap.exists():
        doc = json.loads(cap.read_text(encoding="utf-8"))
        ok = bool(doc.get("ok")) and not doc.get("any_drop")
        log(f"verify result ok={ok} missM={doc.get('M_missing')} missS={doc.get('S_missing')} runM={doc.get('M_cycle')} runS={doc.get('S_cycle')}")
        if not ok:
            for bump in (5, 10, 15):
                t2 = locked + bump
                if t2 > 60:
                    break
                log(f"verify failed; try traffic={t2}")
                tag2 = f"verify_{bin_tag}_t{int(t2):03d}_{datetime.now().strftime('%H%M%S')}"
                run_cmd(
                                        runner_args(
                        "--bin",
                        str(bin_n),
                        "--n-frames",
                        "9",
                        "--traffic",
                        str(t2),
                        "--burst-start",
                        "1",
                        "--burst-end",
                        "11",
                        "--tag",
                        tag2,
                    
                    ),
                    timeout_s=1800,
                )
                cap2 = STATUS / f"capture_{tag2}.json"
                if cap2.exists():
                    d2 = json.loads(cap2.read_text(encoding="utf-8"))
                    if d2.get("ok") and not d2.get("any_drop"):
                        locked = t2
                        ok = True
                        lock_doc = {
                            "bin": bin_tag,
                            "locked_traffic": locked,
                            "verified_n": 9,
                            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        }
                        (STATUS / f"traffic_lock_{bin_tag}.json").write_text(
                            json.dumps(lock_doc, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        if bin_tag == "1x1":
                            (STATUS / "traffic_lock.json").write_text(
                                json.dumps(lock_doc, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8",
                            )
                        break
    st = load_state()
    st.setdefault("gates", {})[bin_tag] = {
        "locked_traffic": locked if ok else locked,
        "verified": ok,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    save_state(st)
    return locked


def write_final_report(t_full, t_bin) -> Path:
    st = load_state()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = REPORTS / f"autonomous_final_{ts}.md"
    lines = [
        f"# 同步精度自主测试结论 (PCIe) {ts}",
        "",
        f"- 日志: `{log_path}`",
        f"- status: `{state_path}`",
        f"- transport: `{TRANSPORT}`",
        f"- locked_traffic 1x1: `{t_full}`",
        f"- locked_traffic 3x3: `{t_bin}`",
        "",
        "## G1–G6",
        "",
    ]
    for gid in ("G1", "G2", "G3", "G4", "G5", "G6"):
        g = (st.get("groups") or {}).get(gid) or {}
        lines.append(
            f"- **{gid}**: ok={g.get('ok')} any_drop={g.get('any_drop')} "
            f"missM={g.get('M_missing')} missS={g.get('S_missing')} "
            f"dir=`{g.get('group_dir')}` traffic={g.get('traffic')}"
        )
    lines.append("")
    lines.append("## gates")
    lines.append("```json")
    lines.append(json.dumps(st.get("gates") or {}, ensure_ascii=False, indent=2))
    lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"FINAL report {path}")
    return path


def main() -> int:
    acquire_instance_lock()
    log(f"DATA={DATA}")
    log(f"TRANSPORT={TRANSPORT}")
    log(f"INSTANCE_LOCK={INSTANCE_LOCK}")
    log(f"LOG={log_path}")
    log(f"PID={os.getpid()}")
    if "20261102" in str(DATA).replace("/", "\\"):
        log("FATAL: refusing to write USB data root 20261102 from PCIe driver")
        return 4

    kill_other_sync_acc()
    # clear stale locks that are null / ours
    for lp in (MIJIA_LOCK,):
        if lp.exists():
            try:
                old = int(lp.read_text(encoding="utf-8").strip().split()[0])
            except Exception:
                old = -1
            if old <= 0 or not _pid_alive(old) or old == os.getpid():
                try:
                    lp.unlink()
                    log(f"cleared stale {lp.name}")
                except Exception:
                    pass
    clear_stale_traffic_locks()

    power_cycle(off_s=15.0, boot_s=60.0)
    if not cameras_ready(2, tries=10, gap_s=5.0):
        log("WARN: <2 cameras after first cycle; escalate power")
        power_cycle(off_s=20.0, boot_s=90.0)
        if not cameras_ready(2, tries=12, gap_s=5.0):
            log("FATAL: cameras never enumerated to 2")
            write_final_report(None, None)
            return 3

    try:
        bind_pcie_roles()
    except Exception as exc:
        log(f"FATAL: bind_pcie_roles failed: {exc}")
        write_final_report(None, None)
        return 3

    t_full = lock_resolution(1)
    if t_full is None:
        log("FATAL: cannot lock full-frame traffic")
        write_final_report(None, None)
        return 2
    log(f"FULL locked_traffic={t_full}")

    t_bin = lock_resolution(3)
    if t_bin is None:
        log("FATAL: cannot lock bin3 traffic")
        write_final_report(t_full, None)
        return 2
    log(f"BIN3 locked_traffic={t_bin}")

    matrix = [
        ("G1", 1, 1, t_full),
        ("G2", 1, 10, t_full),
        ("G3", 1, 100, t_full),
        ("G4", 3, 1, t_bin),
        ("G5", 3, 10, t_bin),
        ("G6", 3, 100, t_bin),
    ]
    for gid, bn, n, traf in matrix:
        st = load_state()
        prev = (st.get("groups") or {}).get(gid)
        if prev and prev.get("ok") and not prev.get("any_drop"):
            log(f"SKIP {gid} already ok")
            continue
        log(f"=== {gid} bin={bn} n={n} traffic={traf} ===")
        ok = False
        for attempt in range(1, 4):
            doc = capture_group(gid, bn, n, traf)
            if doc.get("ok") and not doc.get("any_drop"):
                ok = True
                log(f"{gid} OK attempt={attempt}")
                break
            log(f"{gid} FAIL attempt={attempt} missM={doc.get('M_missing')} missS={doc.get('S_missing')}")
            if attempt < 3:
                power_cycle(off_s=15.0 if attempt == 1 else 20.0, boot_s=60.0 if attempt == 1 else 90.0)
                cameras_ready(2, tries=8, gap_s=5.0)
            time.sleep(2.0)
        if not ok:
            log(f"{gid} gave up after retries; continue")

    log("MATRIX capture phase done")
    (STATUS / "_capture_phase_done.json").write_text(
        json.dumps({"done": True, "at": datetime.now().astimezone().isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    write_final_report(t_full, t_bin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
