# -*- coding: utf-8 -*-
"""PCIe sync-accuracy RE-TEST: 16-bit only; skip climb/verify; G1x10@1s; traffic 55/30."""
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
DATA = Path(os.environ.get("SYNC_ACC_DATA_ROOT", r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3"))
LOCK_ROOT = Path(r"J:\Mywork\Project_LH\sync_acc_runs\_locks")
INSTANCE_LOCK = LOCK_ROOT / "autonomous_pcie_retest.instance.lock"
MIJIA_LOCK = LOCK_ROOT / "mijia.power.lock"
TRANSPORT = "pcie"
BITS = 16
PY = sys.executable
RUNNER = CODE / "sync_acc_run.py"
MIJIA = CODE / "sync_acc_mijia_power.py"
MIJIA_PY = Path(r"J:\Mywork\Project_LH\git\mijia-panel\.venv\Scripts\python.exe")

# Prefer prior PCIe roles; bind_pcie_roles will refresh from live scan.
os.environ.setdefault("SYNC_ACC_MASTER", "QHY600MPCIE-a7c01148f4017d814a3c59ef3d57c907")
os.environ.setdefault("SYNC_ACC_SLAVE", "QHY600MPCIE-847c685cf4d479cf2c6ed43f08ada04d")
os.environ["SYNC_ACC_TRANSPORT"] = TRANSPORT
os.environ["SYNC_ACC_DATA_ROOT"] = str(DATA)
# Drop stale USB ID prefs if somehow inherited as non-PCIe.
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
(DATA / "groups").mkdir(parents=True, exist_ok=True)
LOCK_ROOT.mkdir(parents=True, exist_ok=True)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_path = LOGS / f"autonomous_pcie_retest_{stamp}.log"
state_path = STATUS / "_sync_test_status.json"


def runner_args(*extra: str) -> list[str]:
    return [PY, str(RUNNER), "--transport", TRANSPORT, "--bits", str(BITS), *extra]


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
            [
                "wmic",
                "process",
                "where",
                "name='python.exe' or name='python3.12.exe' or name='pythonw.exe'",
                "get",
                "ProcessId,CommandLine",
                "/FORMAT:LIST",
            ],
            text=True,
            errors="replace",
        )
    except Exception:
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


def power_cycle(off_s: float = 20.0, boot_s: float = 120.0) -> bool:
    """PCIe enumerates slowly after power; default boot wait is longer than USB."""
    if os.environ.get("SYNC_ACC_NO_POWER") == "1":
        log("POWER cycle skipped (SYNC_ACC_NO_POWER=1)")
        return False
    py = str(MIJIA_PY if MIJIA_PY.exists() else PY)
    if not with_mijia_lock(180):
        log("POWER skip: mijia lock busy")
        return False
    try:
        log(f"POWER cycle QHY600Eris off={off_s} boot={boot_s}")
        rc = run_cmd(
            [py, str(MIJIA), "cycle", "--off-s", str(off_s), "--boot-s", str(boot_s)],
            timeout_s=max(240, int(off_s + boot_s + 90)),
        )
        return rc == 0
    finally:
        release_mijia_lock()


def cameras_ready(need: int = 2, tries: int = 10, gap_s: float = 8.0) -> bool:
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
                timeout=120,
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
    helper = CODE / "sync_acc_bind_roles.py"
    p = subprocess.run(
        [PY, str(helper)],
        cwd=str(CODE),
        capture_output=True,
        text=True,
        timeout=120,
        env=os.environ.copy(),
    )
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    names: list[str] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("SCAN_IDS="):
            names = [x for x in ln.split("=", 1)[1].split(",") if x]
    if len(names) < 2:
        # Fallback: use cameras_ready probe style
        raise RuntimeError(f"bind_pcie_roles need 2 cams, got {names!r} rc={p.returncode} out={out[-500:]}")
    pcie = [n for n in names if "PCIE" in n.upper()]
    if len(pcie) < 2:
        raise RuntimeError(f"expected QHY600MPCIE cams, got {names}")
    names = pcie
    m_pref = os.environ.get("SYNC_ACC_MASTER", "").strip()
    s_pref = os.environ.get("SYNC_ACC_SLAVE", "").strip()
    if m_pref in names and s_pref in names and m_pref != s_pref:
        master, slave = m_pref, s_pref
    elif m_pref in names:
        master = m_pref
        slave = next(n for n in names if n != master)
    else:
        master, slave = names[0], names[1]
    os.environ["SYNC_ACC_MASTER"] = master
    os.environ["SYNC_ACC_SLAVE"] = slave
    log(f"PCIe roles MASTER={master} SLAVE={slave} scan={names}")
    (STATUS / "pcie_roles.json").write_text(
        json.dumps({"master": master, "slave": slave, "scan": names}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return master, slave


def capture_group(gid: str, bin_n: int, n_eff: int, traffic: float) -> dict:
    burst_start = 1
    burst_end = n_eff + 2
    tag = f"{gid}_b{bin_n}_t{int(traffic):03d}_n{n_eff}_bits{BITS}_{datetime.now().strftime('%H%M%S')}"
    # PCIe groups can take longer (settle + stutter); allow more wall time.
    timeout_s = 10800 if n_eff >= 100 else 7200
    rc = run_cmd(
        runner_args(
            "--bin",
            str(bin_n),
            "--n-frames",
            str(n_eff),
            "--traffic",
            str(traffic),
            "--exp-us",
            os.environ.get("SYNC_ACC_EXP_US", "10000"),
            "--burst-start",
            str(burst_start),
            "--burst-end",
            str(burst_end),
            "--tag",
            tag,
            "--save-images",
        ),
        timeout_s=timeout_s,
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
        "bits": BITS,
        "transport": TRANSPORT,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    save_state(st)
    return doc


def write_final_report(t_full, t_bin) -> Path:
    st = load_state()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = REPORTS / f"autonomous_final_{ts}.md"
    lines = [
        f"# 同步精度自主测试结论 (PCIe 16bit retest) {ts}",
        "",
        f"- 日志: `{log_path}`",
        f"- status: `{state_path}`",
        f"- transport: `{TRANSPORT}`",
        f"- bits: `{BITS}`",
        f"- locked_traffic 1x1: `{t_full}` (skipped climb)",
        f"- locked_traffic 3x3: `{t_bin}` (skipped climb)",
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
    roles = STATUS / "pcie_roles.json"
    if roles.exists():
        lines.append("")
        lines.append("## roles")
        lines.append("```json")
        lines.append(roles.read_text(encoding="utf-8").rstrip())
        lines.append("```")
    lines.append("")
    lines.append("## gates")
    lines.append("```json")
    lines.append(json.dumps(st.get("gates") or {}, ensure_ascii=False, indent=2))
    lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"FINAL report {path}")
    return path


def _want_group(gid: str) -> bool:
    """SYNC_ACC_GROUPS="G3,G4" limits the run (G1 selects G1_01..G1_10). Empty = all."""
    sel = {x.strip() for x in os.environ.get("SYNC_ACC_GROUPS", "").split(",") if x.strip()}
    return (not sel) or gid in sel or (gid.startswith("G1_") and "G1" in sel)


def run_matrix(t_full: float, t_bin: float) -> None:
    st = load_state()
    st.setdefault("gates", {})["1x1"] = {
        "locked_traffic": t_full,
        "verified": False,
        "skipped_climb": True,
        "bits": BITS,
        "transport": TRANSPORT,
        "note": "PCIe retest fixed traffic same as USB 20261103 retest",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    st.setdefault("gates", {})["3x3"] = {
        "locked_traffic": t_bin,
        "verified": False,
        "skipped_climb": True,
        "bits": BITS,
        "transport": TRANSPORT,
        "note": "PCIe retest fixed traffic same as USB 20261103 retest",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    st["phase_bits"] = BITS
    st["transport"] = TRANSPORT
    save_state(st)
    for bin_tag, tv in (("1x1", t_full), ("3x3", t_bin)):
        lock_doc = {
            "bin": bin_tag,
            "locked_traffic": tv,
            "skipped_climb": True,
            "bits": BITS,
            "transport": TRANSPORT,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (STATUS / ("traffic_lock_%s.json" % bin_tag)).write_text(
            json.dumps(lock_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    log(f"=== PHASE PCIe {BITS}-bit START traffic 1x1={t_full} 3x3={t_bin}; skip climb/verify ===")

    for i in range(1, 11):
        gid = "G1_%02d" % i
        if not _want_group(gid):
            continue
        log(f"=== [{BITS}bit/pcie] {gid} bin=1 n=1 traffic={t_full} ===")
        ok = False
        for attempt in range(1, 4):
            doc = capture_group(gid, 1, 1, t_full)
            if doc.get("ok") and not doc.get("any_drop"):
                ok = True
                log(f"{gid} OK attempt={attempt}")
                break
            log(f"{gid} FAIL attempt={attempt} missM={doc.get('M_missing')} missS={doc.get('S_missing')}")
            if attempt < 3:
                if os.environ.get("SYNC_ACC_NO_POWER") == "1":
                    log(f"{gid} retry without power cycle / re-scan (NO_POWER); waiting 30s")
                    time.sleep(30.0)
                else:
                    power_cycle(off_s=20.0 if attempt == 1 else 25.0, boot_s=120.0 if attempt == 1 else 150.0)
                    cameras_ready(2, tries=12, gap_s=8.0)
                    try:
                        bind_pcie_roles()
                    except Exception as exc:
                        log(f"rebind warn {exc}")
            time.sleep(3.0)
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
        if not _want_group(gid):
            continue
        log(f"=== [{BITS}bit/pcie] {gid} bin={bn} n={n} traffic={traf} ===")
        ok = False
        for attempt in range(1, 4):
            doc = capture_group(gid, bn, n, traf)
            if doc.get("ok") and not doc.get("any_drop"):
                ok = True
                log(f"{gid} OK attempt={attempt}")
                break
            log(f"{gid} FAIL attempt={attempt} missM={doc.get('M_missing')} missS={doc.get('S_missing')}")
            if attempt < 3:
                if os.environ.get("SYNC_ACC_NO_POWER") == "1":
                    log(f"{gid} retry without power cycle / re-scan (NO_POWER); waiting 30s")
                    time.sleep(30.0)
                else:
                    power_cycle(off_s=20.0 if attempt == 1 else 25.0, boot_s=120.0 if attempt == 1 else 150.0)
                    cameras_ready(2, tries=12, gap_s=8.0)
                    try:
                        bind_pcie_roles()
                    except Exception as exc:
                        log(f"rebind warn {exc}")
            time.sleep(3.0)
        if not ok:
            log(f"{gid} gave up after retries; continue")

    (STATUS / "_capture_phase_done.json").write_text(
        json.dumps(
            {
                "done": True,
                "bits": BITS,
                "transport": TRANSPORT,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    rep = write_final_report(t_full, t_bin)
    log(f"=== PHASE PCIe {BITS}-bit DONE report={rep} ===")


def main() -> int:
    acquire_instance_lock()
    DATA.mkdir(parents=True, exist_ok=True)
    global log_path
    log_path = LOGS / ("autonomous_pcie_retest_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))

    log(f"DATA={DATA}")
    log(f"LOG={log_path}")
    log(f"PID={os.getpid()}")
    log("PCIe RETEST: 16bit only; skip climb/verify; traffic 55/30; G1 x10 @1s; longer boot")
    if "20261103_usb" in str(DATA) or str(DATA).rstrip("\\/") .endswith("20261103_pcie"):
        # refuse overwriting USB or old pcie root (exact old dir)
        if str(DATA).replace("/", "\\").rstrip("\\").endswith("20261103_pcie"):
            log("FATAL: refusing to write old 20261103_pcie root; use 20261103_pcie16")
            return 4

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

    # Skip the opening power cycle when both cameras already enumerate (avoids losing the slave again).
    if os.environ.get("SYNC_ACC_SKIP_BIND") == "1":
        log("SKIP_BIND=1: no pre-scan / role binding; roles from SYNC_ACC_MASTER/SLAVE (run_burst scans with retry)")
    else:
        if cameras_ready(2, tries=2, gap_s=5.0):
            log("both cameras already enumerated; skip opening power cycle")
        else:
            # Longer first boot for PCIe enumeration.
            power_cycle(off_s=20.0, boot_s=120.0)
        if not cameras_ready(2, tries=12, gap_s=8.0):
            log("WARN: <2 cameras after first cycle; escalate power")
            power_cycle(off_s=25.0, boot_s=150.0)
            if not cameras_ready(2, tries=15, gap_s=8.0):
                log("FATAL: cameras never enumerated to 2")
                return 3

        try:
            bind_pcie_roles()
        except Exception as exc:
            log(f"FATAL: bind_pcie_roles failed: {exc}")
            return 3

    t_full = 55.0
    t_bin = 30.0
    run_matrix(t_full, t_bin)
    log("PCIe 16bit retest DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())