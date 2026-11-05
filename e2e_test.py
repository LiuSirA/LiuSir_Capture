# -*- coding: utf-8 -*-
"""端到端真机测试（mainloop 架构版，行为级验证）。

tkinter 约束：Tcl 解释器只在主线程处于 mainloop() 内时接受跨线程调用。
主线程 root.mainloop()；测试编排放独立线程；碰 tkinter 的操作经 root.after
调度到主线程执行，用 threading.Event + 结果槽等待。

验证方式（与移植前旧软件一致，不用不可靠的寄存器回读）：
  · 寄存器配置是否符合移植前：确定性对拍 GUI 一键序列 vs fpga_soft_modes.json
  · 同步功能是否生效：行为级「压住/放行」帧数判据（主 35=0 两台停、
    主 35=1 两台恢复、再压又停、退出恢复自由出图）

关键：同步阶段保持**连续 Live 会话**（先起 Live 再配寄存器，与旧软件
test_fix_reg_before_live 的已知结论一致——Init 会冲掉 39/58，故必须先 Live
再配置，中间不 Stop/Restart，否则从机触发配置被清、放行后从机不出图）。

Part A 非同步：扫描→连接→回读参数→Live 多机出图→单帧 Capture（Live 中）
Part B 同步：   ① 一键主从(压住)→行为判「两台停」→② 开拍(放行)→行为判「两台恢复」
Part C 重复：   ③ 退出→行为判「恢复自由出图」→①② 再进→重复 2 轮（连续 Live）
"""
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

import tkinter as tk  # noqa: E402
import capture_gui as G  # noqa: E402


class _Tee:
    """stdout 同时写到终端与 e2e_result.log（绕开 shell 管道丢行）。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


_LOGF = open(HERE / "e2e_result.log", "w", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _LOGF)

PASS = 0
FAIL = 0
SKIP = 0
ROOT = None
APP = None


def check(name, cond, extra=""):
    global PASS, FAIL
    tag = "ok" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    line = f"  [{tag}] {name} {extra}".rstrip()
    try:
        _LOGF.write(line + "\n")
        _LOGF.flush()
    except Exception:
        pass
    sys.__stdout__.write(line + "\n")
    sys.__stdout__.flush()


def skip(name, reason):
    global SKIP
    SKIP += 1
    line = f"  [SKIP] {name}: {reason}"
    try:
        _LOGF.write(line + "\n")
        _LOGF.flush()
    except Exception:
        pass
    sys.__stdout__.write(line + "\n")
    sys.__stdout__.flush()


def log(name, msg=""):
    line = f"  {name} {msg}".rstrip()
    try:
        _LOGF.write(line + "\n")
        _LOGF.flush()
    except Exception:
        pass
    sys.__stdout__.write(line + "\n")
    sys.__stdout__.flush()


def ui_call(fn, timeout_s=120.0):
    box = {}
    ev = threading.Event()

    def _run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e
        ev.set()

    ROOT.after(0, _run)
    if not ev.wait(timeout_s):
        return False, "ui_call 超时"
    if "e" in box:
        return False, box["e"]
    return True, box.get("r")


def wait_until(pred, timeout_s, interval_s=0.3):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(interval_s)
        try:
            if pred():
                return True
        except Exception:
            pass
    return False


def sleep_pump(seconds):
    time.sleep(seconds)


def raw_frames(name):
    """该相机的原始采图帧数（_latest_n，每收到一帧 +1）。判「出图/被压住」用它。"""
    return APP._latest_n.get(name, 0)


def preview_frames(name):
    win = APP._cam_preview_wins.get(name)
    return win.frames if win else 0


def frame_delta(name, seconds):
    """等 seconds 秒，返回该相机原始采图帧增量（0=被压住，>0=在出图）。"""
    f0 = raw_frames(name)
    sleep_pump(seconds)
    return raw_frames(name) - f0


def app_log():
    return APP.log_text.get("1.0", tk.END) + APP.cap_log.get("1.0", tk.END)


# ---------------- 确定性：GUI 一键序列 vs fpga_soft_modes.json ----------------
def _final_map(steps):
    d = {}
    for i, v in steps:
        d[i] = v
    return d


def _json_final_map(key):
    doc = json.loads((HERE / "host" / "fpga_soft_modes.json").read_text(encoding="utf-8"))
    mode = doc["modes"][key]
    d = {}
    for st in mode["steps"]:
        if st["cmd"] == "WRITE_FPGA":
            d[st["params"]["index"]] = st["params"]["value"]
        elif st["cmd"] == "GEN_WRITE_SPLIT" and st.get("gen", {}).get("target") == "WRITE_FPGA":
            g = st["gen"]
            base, val = g["base_index"], g["value"]
            ln = int(g["length"]) if g.get("length") not in ("auto", None) else 1
            endian, stride = g.get("endian", "LE"), g.get("stride", 1)
            for i in range(ln):
                byte = ((val >> (8 * (ln - 1 - i))) & 0xFF) if endian == "BE" else ((val >> (8 * i)) & 0xFF)
                d[base + i * stride] = byte
    return d


REG_JSON_PAIRS = [
    ("usb_master", "主从同步_主机(Master)一键设置"),
    ("usb_slave", "主从同步_从机(Slave)一键设置"),
    ("exit_sync", "主从同步_退出同步拍摄(恢复单机默认)"),
    ("trigger_release", "主从同步_主机触发开拍(释放IDLE)"),
]

SYNC_H = 15  # 压住判据观察窗（秒）：被压住应 ≤1 帧（实测恒 0）
SYNC_R = 15  # 放行/恢复判据观察窗（秒）：应 ≥2 帧（全幅 PCIe 约 0.5fps，慢速）


def main_test():
    log(f"=== E2E: {HERE} ===")

    # ---------------- 寄存器序列对拍（确定性） ----------------
    log("\n== 寄存器配置对拍：GUI 一键序列 vs fpga_soft_modes.json ==")
    all_match = True
    for gui_key, json_key in REG_JSON_PAIRS:
        got = _final_map(APP._build_sync_steps(gui_key))
        want = _json_final_map(json_key)
        ok = got == want
        all_match &= ok
        check(f"{gui_key} 序列 == {json_key}", ok,
              "" if ok else f"仅GUI={ {k:v for k,v in got.items() if want.get(k)!=v} } 仅JSON={ {k:v for k,v in want.items() if got.get(k)!=v} }")

    # ---------------- Part A: 非同步（连接/回读/Live/单帧，Live 中拍单帧） ----------------
    log("\n== Part A: 扫描/连接/回读/Live/单帧 ==")

    def do_scan():
        return APP.scan_cameras()

    ui_call(do_scan, 10.0)
    if not wait_until(lambda: len(APP.cameras) > 0, 150.0, 1.0):
        skip("扫描", "未检测到相机")
        log(f"\n=== E2E: {PASS} ok, {FAIL} fail, {SKIP} skip ===")
        return 1
    names = [c.name for c in APP.cameras]
    log("scanned:", names)
    A = names[0]
    B = names[1] if len(names) >= 2 else None
    idxA, idxB = names.index(A), (names.index(B) if B else -1)

    for i in (idxA, idxB):
        if i < 0:
            continue
        ui_call(lambda i=i: (APP._sel_idx.set(i), APP.connect_selected()), 10.0)
        connected = wait_until(lambda i=i: APP.cameras[i].connected, 150.0, 1.0)
        check(f"connect {APP.cameras[i].name}", connected)

    check("readback logged",
          wait_until(lambda: "[params] " in app_log(), 90.0, 0.5),
          f"exptime={APP.v_exptime.get()!r} gain={APP.v_gain.get()!r}")
    check("readback res_info", str(APP.v_res_info.get()).strip() != "",
          f"res={APP.v_res_info.get()!r} roi={APP.v_roi_w.get()}x{APP.v_roi_h.get()} "
          f"bin={APP.v_bin.get()!r} readmode={APP.v_readmode.get()!r}")

    # 出图设置对齐移植前行为验证用的已知可用配置（8bit / 20ms / traffic60 / 全幅）。
    ui_call(lambda: (APP.v_bits.set("8"), APP.v_exptime.set("20000"),
                     APP.v_traffic.set("60")))

    # ---------------- 单帧 Capture（独立验证，非多机、非 Live） ----------------
    # 关键：cap_multi_var 默认 True 时 run_capture_single 会对「全部」相机
    # apply_settings(SINGLE)，把相机切到 SINGLE 模式、污染随后的 Live。故单帧
    # 单独用一台、multi 关，验证完再整体复位，让同步测试跑干净会话。
    ui_call(lambda: APP.cap_multi_var.set(False))
    ui_call(lambda: APP._sel_idx.set(idxB if B else idxA))
    single_cam = B if B else A
    pf0 = preview_frames(single_cam)
    ui_call(lambda: APP.run_capture_single(), 10.0)
    ok_single = wait_until(lambda: preview_frames(single_cam) > pf0, 90.0, 0.5)
    check("single capture frame", ok_single,
          f"{single_cam[:20]} preview={preview_frames(single_cam)} (was {pf0}) "
          f"status={APP.status_var.get()}")

    # ---------------- 整体复位：关掉上一段会话，给同步测试一条干净 connect→live 会话 ----------------
    log("\n== 复位相机会话（为同步测试） ==")
    ui_call(lambda: APP._request_stop_live())
    wait_until(lambda: not APP._capture_live_running, 40.0, 0.5)
    ui_call(lambda: APP.close_all_cams(), 60.0)
    sleep_pump(2.0)
    ui_call(lambda: APP.scan_cameras(), 60.0)
    wait_until(lambda: len(APP.cameras) >= 2, 120.0, 1.0)
    idxA2 = next((i for i, c in enumerate(APP.cameras) if c.name == A), idxA)
    idxB2 = next((i for i, c in enumerate(APP.cameras) if c.name == B), idxB)
    for i in (idxA2, idxB2):
        ui_call(lambda i=i: (APP._sel_idx.set(i), APP.connect_selected()), 10.0)
        wait_until(lambda i=i: APP.cameras[i].connected, 150.0, 1.0)
    check("reconnect A/B (clean session)", APP.cameras[idxA2].connected and APP.cameras[idxB2].connected,
          f"A={APP.cameras[idxA2].connected} B={APP.cameras[idxB2].connected}")

    # 起 Live（连续会话贯穿同步测试，不再 Stop/Restart——
    # Stop/Restart + Init 会冲掉从机触发配置，放行后从机不出图）。
    ui_call(lambda: APP.cap_multi_var.set(True))
    ui_call(lambda: APP._sel_idx.set(idxA2))
    ui_call(lambda: APP.toggle_live(), 10.0)
    ok_live = wait_until(
        lambda: (APP._capture_live_running and raw_frames(A) >= 5
                 and raw_frames(B) >= 5), 240.0, 1.0)
    check("live multi frames (8bit)", ok_live,
          f"A={raw_frames(A)} B={raw_frames(B)} running={APP._capture_live_running}")

    # ---------------- Part B + C: 同步（连续 Live，行为级） ----------------
    if B is None:
        skip("Part B/C 同步", "只检测到 1 台相机，无法主从测试")
        if APP._capture_live_running:
            ui_call(lambda: APP._request_stop_live())
        log(f"\n=== E2E: {PASS} ok, {FAIL} fail, {SKIP} skip ===")
        return 1 if FAIL else 0

    log("\n== Part B: 同步（连续 Live 中 ① 压住 → ② 放行）==")
    ui_call(lambda: (APP._sel_idx.set(idxA2), APP._set_selected_role(G.ROLE_MASTER)))
    ui_call(lambda: (APP._sel_idx.set(idxB2), APP._set_selected_role(G.ROLE_SLAVE)))
    check("role A=master B=slave",
          APP.cameras[idxA2].role == G.ROLE_MASTER and APP.cameras[idxB2].role == G.ROLE_SLAVE)

    ui_call(lambda: APP.onekey_set_sync_params(), 10.0)
    check("① onekey set",
          wait_until(lambda: "① 主从参数已设置完成" in APP.status_var.get(), 120.0, 0.5),
          APP.status_var.get())
    sleep_pump(5.0)  # 等 0xB9 落地
    dA = frame_delta(A, SYNC_H)
    dB = frame_delta(B, SYNC_H)
    check("①后 两台被压住(35=0)", dA <= 2 and dB <= 2, f"A +{dA}, B +{dB}")

    ui_call(lambda: APP.onekey_enter_sync(), 10.0)
    check("② enter sync",
          wait_until(lambda: "② 已开拍" in APP.status_var.get(), 120.0, 0.5),
          APP.status_var.get())
    sleep_pump(1.0)
    dA = frame_delta(A, SYNC_R)
    dB = frame_delta(B, SYNC_R)
    check("②后 两台同步出图(35=1)", dA >= 2 and dB >= 2, f"A +{dA}, B +{dB}")

    log("\n== Part C: 退出同步 + 反复进出 2 轮（连续 Live，行为判据）==")
    for r in range(2):
        ui_call(lambda: APP.onekey_exit_sync(), 10.0)
        if not wait_until(lambda: "③ 已退出同步" in APP.status_var.get(), 120.0, 0.5):
            check(f"③ exit round{r}", False, APP.status_var.get())
            break
        check(f"③ exit round{r}", True)
        sleep_pump(1.0)
        dA = frame_delta(A, SYNC_R)
        dB = frame_delta(B, SYNC_R)
        check(f"exit 后恢复自由出图 round{r}", dA >= 2 and dB >= 2, f"A +{dA}, B +{dB}")
        if r < 1:
            ui_call(lambda: APP.onekey_set_sync_params(), 10.0)
            ok_set = wait_until(lambda: "① 主从参数已设置完成" in APP.status_var.get(), 120.0, 0.5)
            sleep_pump(4.0)
            ui_call(lambda: APP.onekey_enter_sync(), 10.0)
            ok_re = wait_until(lambda: "② 已开拍" in APP.status_var.get(), 120.0, 0.5)
            sleep_pump(1.0)
            check(f"re-enter sync round{r} (①+②)", ok_set and ok_re,
                  f"set={ok_set} enter={ok_re} status={APP.status_var.get()}")
            dA = frame_delta(A, SYNC_R)
            dB = frame_delta(B, SYNC_R)
            check(f"re-sync 出图 round{r}", dA >= 2 and dB >= 2, f"A +{dA}, B +{dB}")

    # 收尾：正常停止 Live
    try:
        if APP._capture_live_running:
            ui_call(lambda: APP._request_stop_live())
            wait_until(lambda: not APP._capture_live_running, 40.0, 0.5)
    except Exception:
        pass
    sleep_pump(0.5)
    summary = f"\n=== E2E: {PASS} ok, {FAIL} fail, {SKIP} skip ==="
    try:
        _LOGF.write(summary + "\n")
        _LOGF.flush()
    except Exception:
        pass
    sys.__stdout__.write(summary + "\n")
    sys.__stdout__.flush()
    try:
        APP._quit_app()
    except Exception as e:  # noqa: BLE001
        log("  [WARN] _quit_app:", e)
    ROOT.quit()


def main():
    global ROOT, APP
    ROOT = tk.Tk()
    ROOT.withdraw()
    APP = G.CaptureApp(ROOT, HERE)
    ROOT.update_idletasks()
    t = threading.Thread(target=main_test, daemon=True)
    t.start()
    ROOT.mainloop()
    try:
        ROOT.destroy()
    except Exception:
        pass
    rc = 1 if FAIL else 0
    os._exit(rc)  # 防 multiprocessing worker join 卡死拖垮相机


if __name__ == "__main__":
    raise SystemExit(main())
