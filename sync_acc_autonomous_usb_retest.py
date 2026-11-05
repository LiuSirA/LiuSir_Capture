# -*- coding: utf-8 -*-
"""USB sync-accuracy RE-TEST: 8-bit then 16-bit; skip climb; G1x10@1s; traffic 55/30."""
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
DATA = Path(os.environ.get("SYNC_ACC_DATA_ROOT", r"J:\Mywork\Project_LH\sync_acc_runs\20261103_usb"))
LOCK_ROOT = Path(r"J:\Mywork\Project_LH\sync_acc_runs\_locks")
INSTANCE_LOCK = LOCK_ROOT / "autonomous_usb_retest.instance.lock"
MIJIA_LOCK = LOCK_ROOT / "mijia.power.lock"
PY = sys.executable
RUNNER = CODE / "sync_acc_run.py"
MIJIA = CODE / "sync_acc_mijia_power.py"
MIJIA_PY = Path(r"J:\Mywork\Project_LH\git\mijia-panel\.venv\Scripts\python.exe")

os.environ.setdefault("SYNC_ACC_MASTER", "QHY600M-ac14f178435e35c7")
os.environ.setdefault("SYNC_ACC_SLAVE", "QHY600M-8765fd7c26d38aa4d")
os.environ.setdefault("SYNC_ACC_TRANSPORT", "usb")
os.environ["SYNC_ACC_TRANSPORT"] = (os.environ.get("SYNC_ACC_TRANSPORT") or "usb").strip().lower() or "usb"
# Parent root; each bit-depth phase rebinds DATA/STATUS/LOGS under 8bit/ or 16bit/.
PARENT_DATA = DATA
os.environ["SYNC_ACC_DATA_ROOT"] = str(DATA)

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
CURRENT_BITS = 8


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
    me = os.getpid()
    for pid, cmd in list_sync_acc_pids():
        if pid == me:
            continue
        # Never kill sync_acc_autonomous wrappers (WindowsApps python stub may share cmdline
        # with a different PID than os.getpid()). Instance lock already serializes autonomous.
        if "sync_acc_autonomous" in cmd:
            log(f"KEEP autonomous-related pid={pid} (instance lock owns serialization)")
            continue
        log(f"KILL other pid={pid} cmd={cmd[:180]}")
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
        [
            PY,
            str(RUNNER),
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
        ],
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


def capture_group(gid: str, bin_n: int, n_eff: int, traffic: float, bits: int = 8) -> dict:
    burst_start = 1
    burst_end = n_eff + 2
    tag = f"{gid}_b{bin_n}_t{int(traffic):03d}_n{n_eff}_bits{int(bits)}_{datetime.now().strftime('%H%M%S')}"
    rc = run_cmd(
        [
            PY,
            str(RUNNER),
            "--transport",
            "usb",
            "--bits",
            str(int(bits)),
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
        ],
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
        "bits": int(bits),
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
        [
            PY,
            str(RUNNER),
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
        ],
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
                    [
                        PY,
                        str(RUNNER),
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
                    ],
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
        f"# 同步精度自主测试结论 {ts}",
        "",
        f"- 日志: `{log_path}`",
        f"- status: `{state_path}`",
        f"- locked_traffic 1x1: `{t_full}`",
        f"- locked_traffic 3x3: `{t_bin}`",
        "",
        "## G1–G6",
        "",
    ]
    gids = [f"G1_{i:02d}" for i in range(1, 11)] + ["G2", "G3", "G4", "G5", "G6"]
    for gid in gids:
        g = (st.get("groups") or {}).get(gid) or {}
        lines.append(
            f"- **{gid}**: ok={g.get('ok')} any_drop={g.get('any_drop')} "
            f"missM={g.get('M_missing')} missS={g.get('S_missing')} "
            f"bits={g.get('bits')} dir=`{g.get('group_dir')}` traffic={g.get('traffic')}"
        )
    lines.append("")
    lines.append("## gates")
    lines.append("```json")
    lines.append(json.dumps(st.get("gates") or {}, ensure_ascii=False, indent=2))
    lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"FINAL report {path}")
    return path


def bind_phase_root(bits: int) -> None:
    """Point DATA/STATUS/LOGS/REPORTS/state at parent/8bit or parent/16bit."""
    global DATA, STATUS, LOGS, REPORTS, state_path, CURRENT_BITS
    CURRENT_BITS = int(bits)
    sub = "16bit" if CURRENT_BITS >= 16 else "8bit"
    DATA = PARENT_DATA / sub
    os.environ["SYNC_ACC_DATA_ROOT"] = str(DATA)
    STATUS = DATA / "status"
    LOGS = DATA / "logs"
    REPORTS = DATA / "reports"
    for d in (STATUS, LOGS, REPORTS, DATA / "groups"):
        d.mkdir(parents=True, exist_ok=True)
    state_path = STATUS / "_sync_test_status.json"
    log(f"PHASE bind bits={CURRENT_BITS} DATA={DATA}")


def run_matrix_phase(bits: int, t_full: float, t_bin: float) -> None:
    bind_phase_root(bits)
    st = load_state()
    st.setdefault("gates", {})["1x1"] = {
        "locked_traffic": t_full,
        "verified": False,
        "skipped_climb": True,
        "bits": int(bits),
        "note": "USB retest fixed from 20261102 overnight",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    st.setdefault("gates", {})["3x3"] = {
        "locked_traffic": t_bin,
        "verified": False,
        "skipped_climb": True,
        "bits": int(bits),
        "note": "USB retest fixed from 20261102 overnight",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    st["phase_bits"] = int(bits)
    save_state(st)
    for bin_tag, tv in (("1x1", t_full), ("3x3", t_bin)):
        lock_doc = {
            "bin": bin_tag,
            "locked_traffic": tv,
            "skipped_climb": True,
            "bits": int(bits),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (STATUS / ("traffic_lock_%s.json" % bin_tag)).write_text(
            json.dumps(lock_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    log(f"=== PHASE {bits}-bit START traffic 1x1={t_full} 3x3={t_bin} ===")

    for i in range(1, 11):
        gid = "G1_%02d" % i
        log(f"=== [{bits}bit] {gid} bin=1 n=1 traffic={t_full} ===")
        ok = False
        for attempt in range(1, 4):
            doc = capture_group(gid, 1, 1, t_full, bits=bits)
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
        if i < 10:
            time.sleep(1.0)

    matrix = [
        ("G2", 1, 10, t_full),
        ("G3", 1, 100, t_full),
        ("G4", 3, 1, t_bin),
        ("G5", 3, 10, t_bin),
        ("G6", 3, 100, t_bin),
    ]
    for gid, bn, n, traf in matrix:
        log(f"=== [{bits}bit] {gid} bin={bn} n={n} traffic={traf} ===")
        ok = False
        for attempt in range(1, 4):
            doc = capture_group(gid, bn, n, traf, bits=bits)
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

    (STATUS / "_capture_phase_done.json").write_text(
        json.dumps(
            {
                "done": True,
                "bits": int(bits),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    rep = write_final_report(t_full, t_bin)
    log(f"=== PHASE {bits}-bit DONE report={rep} ===")


def main() -> int:
    acquire_instance_lock()
    # Parent log under 20261103_usb/logs; phase logs also continue on same log_path.
    PARENT_DATA.mkdir(parents=True, exist_ok=True)
    (PARENT_DATA / "logs").mkdir(parents=True, exist_ok=True)
    global log_path
    log_path = (PARENT_DATA / "logs") / ("autonomous_usb_retest_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))

    log(f"PARENT_DATA={PARENT_DATA}")
    log(f"LOG={log_path}")
    log(f"PID={os.getpid()}")
    log("USB RETEST: 8bit then 16bit; skip climb/verify; traffic 55/30; G1 x10 @1s")

    kill_other_sync_acc()
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

    power_cycle(off_s=15.0, boot_s=60.0)
    if not cameras_ready(2, tries=10, gap_s=5.0):
        log("WARN: <2 cameras after first cycle; escalate power")
        power_cycle(off_s=20.0, boot_s=90.0)
        if not cameras_ready(2, tries=12, gap_s=5.0):
            log("FATAL: cameras never enumerated to 2")
            return 3

    # Verify USB cams (not PCIe)
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
        "print('SCAN_IDS=' + ','.join(names))\n"
    )
    try:
        p = subprocess.run(
            [PY, "-c", probe],
            cwd=str(CODE),
            capture_output=True,
            text=True,
            timeout=90,
            env=os.environ.copy(),
        )
        scan_out = (p.stdout or "") + "\n" + (p.stderr or "")
        names = []
        for ln in scan_out.splitlines():
            if ln.strip().startswith("SCAN_IDS="):
                names = [x for x in ln.strip().split("=", 1)[1].split(",") if x]
        log(f"CAM check ids={names}")
        bad = [n for n in names if "PCIE" in n.upper()]
        usbish = [n for n in names if n.upper().startswith("QHY600M") and "PCIE" not in n.upper()]
        if bad or len(usbish) < 2:
            log(f"FATAL: expected QHY600M USB cams, got {names}")
            return 4
    except Exception as exc:
        log(f"FATAL: cam check failed {exc}")
        return 4

    t_full = 55.0
    t_bin = 30.0

    # Phase A: 8-bit full matrix
    run_matrix_phase(8, t_full, t_bin)

    # Power cycle between bit-depth phases for a clean sensor/SDK state
    log("Inter-phase power cycle before 16-bit")
    power_cycle(off_s=15.0, boot_s=60.0)
    if not cameras_ready(2, tries=10, gap_s=5.0):
        power_cycle(off_s=20.0, boot_s=90.0)
        if not cameras_ready(2, tries=12, gap_s=5.0):
            log("FATAL: cameras not ready for 16-bit phase")
            return 3

    # Phase B: 16-bit full matrix
    run_matrix_phase(16, t_full, t_bin)

    # Parent summary
    summary = PARENT_DATA / "reports" / ("usb_retest_both_bits_%s.md" % datetime.now().strftime("%Y%m%d_%H%M"))
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        "\n".join(
            [
                "# USB retest 8bit + 16bit",
                "",
                f"- parent: `{PARENT_DATA}`",
                f"- 8bit: `{PARENT_DATA / '8bit'}`",
                f"- 16bit: `{PARENT_DATA / '16bit'}`",
                f"- log: `{log_path}`",
                f"- traffic 1x1=55 3x3=30; G1_01..G1_10 n=1 @1s; then G2..G6",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"BOTH PHASES DONE summary={summary}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
