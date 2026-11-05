# -*- coding: utf-8 -*-
"""Off-screen smoke test for capture_gui: build UI, assign roles, dry-run a sync mode."""
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "host"))

import tkinter as tk
import capture_gui as G

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def main():
    root = tk.Tk()
    root.withdraw()
    app = G.CaptureApp(root, HERE)
    root.update_idletasks()

    print("== UI built ==")
    check("notebook has 3 tabs (capture merged)", len(app.notebook.tabs()) == 3)
    check("camera tree exists", hasattr(app, "cam_tree"))
    check("sync groups rendered", hasattr(app, "_sync_target_var"))
    check("single/continuous capture entries", hasattr(app, "btn_capture") and hasattr(app, "btn_live"))
    check("capture log present", hasattr(app, "cap_log"))

    print("== modes loaded ==")
    modes = app._modes_doc.get("modes", {})
    for mn in [
        "主从同步_主机(Master)一键设置",
        "主从同步_从机(Slave)一键设置",
        "主从同步_主机配置_PCIe光纤(压住IDLE)",
        "主从同步_从机配置_PCIe光纤",
        "主从同步_主机触发开拍(释放IDLE)",
        "主从同步_退出同步拍摄(恢复单机默认)",
    ]:
        check(f"mode present: {mn}", mn in modes)

    print("== simulate scan + role assignment ==")
    # emulate a scan result without hardware
    app._on_scanned(["CAM-A", "CAM-B", "CAM-C", "CAM-D"])
    check("4 cameras listed", len(app.cameras) == 4)

    # select CAM-A as master
    app._sel_idx.set(0)
    app._set_selected_role(G.ROLE_MASTER)
    check("CAM-A is master", app.cameras[0].role == G.ROLE_MASTER)
    check("CAM-A gps_slave False", app.v_gps_slave.get() is False)

    # select CAM-B, set slave
    app._sel_idx.set(1)
    app._set_selected_role(G.ROLE_SLAVE)
    check("CAM-B is slave", app.cameras[1].role == G.ROLE_SLAVE)
    check("CAM-B gps_slave True", app.v_gps_slave.get() is True)

    # select CAM-C, set master -> should demote CAM-A to standalone
    app._sel_idx.set(2)
    app._set_selected_role(G.ROLE_MASTER)
    check("CAM-C is master", app.cameras[2].role == G.ROLE_MASTER)
    check("CAM-A demoted to standalone", app.cameras[0].role == G.ROLE_STANDALONE)

    print("== '其余全部设为从机' from CAM-C ==")
    app._sel_idx.set(2)  # CAM-C
    app._set_rest_slaves()
    check("CAM-C master", app.cameras[2].role == G.ROLE_MASTER)
    check("CAM-A slave", app.cameras[0].role == G.ROLE_SLAVE)
    check("CAM-B slave", app.cameras[1].role == G.ROLE_SLAVE)
    check("CAM-D slave", app.cameras[3].role == G.ROLE_SLAVE)
    check("exactly 1 master", sum(1 for c in app.cameras if c.role == G.ROLE_MASTER) == 1)

    print("== sync target selection ==")
    check("master target = [CAM-C]", app._sync_target_cameras("master") == ["CAM-C"])
    check("slaves target = [A,B,D]", app._sync_target_cameras("slaves") == ["CAM-A", "CAM-B", "CAM-D"])
    check("all target = list order", app._sync_target_cameras("all") == ["CAM-A", "CAM-B", "CAM-C", "CAM-D"])

    print("== sync target selection (role + transport) ==")
    app.cameras[0].transport = G.TRANSPORT_USB      # A slave USB
    app.cameras[1].transport = G.TRANSPORT_PCIE    # B slave PCIe
    app.cameras[2].transport = G.TRANSPORT_PCIE    # C master PCIe
    app.cameras[3].transport = G.TRANSPORT_USB      # D slave USB
    check("usb_slave -> [A,D]", app._sync_target_cameras("slaves", "usb") == ["CAM-A", "CAM-D"])
    check("pcie_slave -> [B]", app._sync_target_cameras("slaves", "pcie") == ["CAM-B"])
    check("usb_master -> []", app._sync_target_cameras("master", "usb") == [])
    check("pcie_master -> [C]", app._sync_target_cameras("master", "pcie") == ["CAM-C"])
    check("any_master -> [C]", app._sync_target_cameras("master", "any") == ["CAM-C"])

    print("== one-key (①②③) job building ==")
    # state: C master PCIe, A/B/D slaves (A USB, B PCIe, D USB)
    check("① master cam -> pcie_master_hold", app._sync_key_for_cam(app.cameras[2]) == "pcie_master_hold")
    check("① slave USB cam -> usb_slave", app._sync_key_for_cam(app.cameras[0]) == "usb_slave")
    check("① slave PCIe cam -> pcie_slave", app._sync_key_for_cam(app.cameras[1]) == "pcie_slave")
    check("① standalone cam -> None", app._sync_key_for_cam(G.CamRow("x")) is None)
    # ② enters only on the master
    masters = [c.name for c in app.cameras if c.role == G.ROLE_MASTER]
    check("② masters = [C]", masters == ["CAM-C"])
    # ③ covers all master+slave cams
    sync_cams = [c.name for c in app.cameras if c.role in (G.ROLE_MASTER, G.ROLE_SLAVE)]
    check("③ all master+slave = [A,B,C,D]", sync_cams == ["CAM-A", "CAM-B", "CAM-C", "CAM-D"])

    print("== dry-run / parameter-driven sync sequences ==")
    # defaults must match the JSON one-click modes
    expect = {
        "usb_master": [(41, 0), (42, 0), (43, 125), (44, 1), (50, 0), (51, 0), (52, 11),
                       (142, 1), (56, 1), (58, 0), (39, 17), (57, 1), (35, 0)],
        "usb_burst": [(41, 0), (42, 0), (43, 125), (44, 1)],
        "pcie_burst": [(41, 0), (42, 0), (43, 32), (44, 0)],
        "trigger_release": [(35, 0), (35, 1)],
    }
    for k, v in expect.items():
        check(f"default {k} steps", app._build_sync_steps(k) == v)

    # changing a field must change the sequence (parameterization works)
    app.v_usb_patch.set("8192")
    check("usb patch 8192 -> 43=32", app._build_sync_steps("usb_burst") == [(41, 0), (42, 0), (43, 32), (44, 0)])
    app.v_window.set("20")
    check("window 20 -> 52=20", (52, 20) in app._build_sync_steps("usb_master"))
    app.v_min_pulse.set("100")
    check("min pulse 100 -> 148=100", (148, 100) in app._build_sync_steps("usb_slave"))
    # restore defaults
    app.v_usb_patch.set("32001"); app.v_pcie_patch.set("8192"); app.v_window.set("11"); app.v_min_pulse.set("10")
    app.v_master_39.set("17"); app.v_slave_39.set("2"); app.v_master_58.set("0"); app.v_slave_58.set("8")

    # dry-run a sync button (no hardware) logs the sequence
    app.run_sync_mode("usb_master", "master", "主机一键设置", dry=True)
    check("dry-run logged steps", "41=0" in app.sync_text.get("1.0", tk.END))

    # preview dialog builds
    try:
        app.preview_sync_steps()
        check("preview dialog opens", True)
    except Exception as exc:
        check(f"preview dialog raised: {exc}", False)

    print("== settings builder ==")
    app.v_bits.set("16")
    app.v_exptime.set("100000")
    app.v_gps_on.set(True)
    s = app._settings_from_gui(G.STREAM_LIVE)
    check("settings stream live", s.stream_mode == G.STREAM_LIVE)
    check("settings bits 16", s.bits == 16)
    check("settings gps on", s.gps_enable is True)

    print("== preview window (full port, off-screen) ==")
    try:
        app.v_gps_on.set(True)  # GPS on -> _gps_parse_enabled
        from qhyccd_highlevel import FrameResult, GpsInfo

        def mkframe(seq):
            # GpsInfo 通过 gps_header_issues 门禁（非零时间戳、us 在 0..10M、宽高匹配）
            gps = GpsInfo(seq_number=seq, width=4, height=4,
                          start_sec=1, start_us=100, end_sec=1, end_us=150)
            return FrameResult(width=4, height=4, bpp=8, channels=1,
                               data=bytes([100] * 16), gps=gps)

        win = app._ensure_preview("CAM-C")
        root.update_idletasks()
        check("preview window exists", win.win.winfo_exists())
        check("preview has layout combo", hasattr(win, "lay_monitor"))
        check("preview has compact toggle", hasattr(win, "toggle_compact"))
        win.refresh_view_cache()

        # GPS 帧序号统计（多机同步验收依赖；默认刷新模式）
        for s in [1, 2, 3, 5, 7, 7]:  # gap at 4, dup at last 7
            win.queue_frame(mkframe(s), interval_s=0.0)
        check("gps seq last", win.last_seq == 7)
        check("gps seq first", win.first_seq == 1)
        check("gps seq count", win.seq_count == 6)
        check("gps seq gap=2", win.seq_gaps == 2)
        check("gps seq dup=1", win.seq_dups == 1)
        check("gps seq summary", "seq=7" in win.gps_seq_summary() and "gap=2" in win.gps_seq_summary())
        win.reset_gps_seq_stats()
        check("gps seq reset", win.last_seq is None and win.seq_count == 0)

        # 帧序号列表模式（满 N 冻结）
        app._gps_seq_list_mode = True
        app._gps_seq_list_max = 10
        for s in [1, 2, 3]:
            win.queue_frame(mkframe(s), interval_s=0.0)
        check("seq list count", len(win.seq_list) == 3)
        check("seq list compact", win.burst_seq_list_compact() == "1,2,3")
        check("seq list text has slots", "[04] -" in win.burst_seq_list_text())
        app._gps_seq_list_mode = False

        # 多机布局 / 平铺（off-screen：验证不报错 + 生成行列 + 显示器列表）
        app._on_scanned(["CAM-A", "CAM-B", "CAM-C", "CAM-D"])
        app._open_preview_windows(["CAM-A", "CAM-B", "CAM-C"])
        root.update_idletasks()
        check("3 preview windows open", len(app._cam_preview_wins) == 3)
        check("monitors listed", len(app._monitors) >= 1)
        app.tile_multi_preview_windows()
        check("layout 4->grid", app._resolve_preview_layout(4, app._monitors[0])[0] == "网格")
        check("layout 2->auto ok", app._resolve_preview_layout(2, app._monitors[0])[0] in ("上下", "左右"))
        check("tile_span sums", sum(s for _, s in [app._tile_span(i, 3, 1500, 0) for i in range(3)]) == 1500)
        check("multi status bar set", isinstance(app.cap_multi_status_var.get(), str))
        # 灰度复制到其他窗
        app.copy_preview_gray_from("CAM-C")
        check("gray copied to other windows", app._cam_preview_wins["CAM-A"].brightness.get() == win.brightness.get())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check(f"preview window raised: {exc}", False)

    app._close_multi_preview_windows()
    root.destroy()
    print(f"\n=== SMOKE: {PASS} ok, {FAIL} fail ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
