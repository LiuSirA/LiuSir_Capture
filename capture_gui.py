#!/usr/bin/env python3
"""Standalone QHY camera capture app, extracted from softGUI (fpga_soft_gui.py).

Scope (exactly the requested slice, nothing more):
  - 相机采集: scan / connect / single-frame + live capture with per-camera preview windows.
  - 主从相机: after scanning, manually assign each camera a role (主机 Master / 从机 Slave / 单机),
    one master + many slaves. Capture parameters then key off role + transport.
  - 主从同步: six one-click sync buttons, grouped by USB / PCIe-fiber / common, executed on the
    cameras that match their role. The six modes come verbatim from host/fpga_soft_modes.json.

Reused backend (copied verbatim into this tree; the original softGUI source is NOT modified):
  qhyccd_api / qhyccd_highlevel / capture_proxy / capture_worker / capture_ipc /
  fpga_soft_commands / fpga_soft_image / fpga_paths / fpga_soft_modes.json
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# 可选依赖：图像保存用 PIL（16-bit → PNG / 8-bit → PNG）。缺失时回退为 .raw 原数据。
try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None
try:
    from PIL import Image as _PILImage
except Exception:  # noqa: BLE001
    _PILImage = None

# --- path setup: this app lives next to fpga_paths.py and a host/ backend dir ---
APP_ROOT = Path(__file__).resolve().parent
HOST_DIR = APP_ROOT / "host"
for p in (APP_ROOT, HOST_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 系统托盘（pystray）：关闭窗口后驻留托盘；缺失时退化为“直接退出”
try:
    import pystray
    from pystray import Menu, MenuItem
    _HAVE_PYSTRAY = True
except Exception:  # noqa: BLE001
    pystray = None
    Menu = MenuItem = None
    _HAVE_PYSTRAY = False

import fpga_paths  # noqa: E402  (registers script dirs, resolves remoteupdate_deps)
from fpga_soft_image import (  # noqa: E402
    adjust_gray8,
    crop_gray8_center,
    downscale_gray8,
    fit_zoom_for_size,
    gray8_percentile_levels,
    gray8_quick_stats,
    gray8_to_tk_photo,
    render_preview_gray8,
    scale_gray8,
)
from capture_proxy import CaptureWorker, CaptureWorkerPool, RemoteCamera, open_remote_camera  # noqa: E402
from qhyccd_highlevel import (  # noqa: E402
    STREAM_LIVE,
    STREAM_SINGLE,
    CameraSettings,
    capture_timeout_s,
    date_to_js1995,
    exp_to_us,
    format_gps_report,
    format_gps_seq_brief,
    gps_header_issues,
    is_burst_waste_frame,
    js1995_to_utc_tuple,
    parse_gps_header,
    utc_now_plus_js1995,
)

# --- SDK DLL selection (same priority as the original: deps > AllInOne) ---
_DEPS_DLL = APP_ROOT / "remoteupdate_deps" / "qhyccd.dll"
_ALLINONE_DLL = Path(r"C:\Program Files\QHYCCD\AllInOne\sdk\x64\qhyccd.dll")
if not os.environ.get("QHYCCD_DLL"):
    if _DEPS_DLL.is_file():
        os.environ["QHYCCD_DLL"] = str(_DEPS_DLL)
    elif _ALLINONE_DLL.is_file():
        os.environ["QHYCCD_DLL"] = str(_ALLINONE_DLL)


def _ensure_cwd_qhyccd_ini() -> Path | None:
    """SDK Init reads getcwd()/qhyccd.ini. Mirror the DLL's ini into the CWD once."""
    dll = Path(os.environ.get("QHYCCD_DLL", "") or "")
    if not dll.is_file():
        return None
    src = dll.with_name("qhyccd.ini")
    if not src.is_file():
        return None
        dst = Path.cwd() / "qhyccd.ini"
        if not dst.is_file():
            try:
                dst.write_text(src.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
            except Exception:
                return None
        return dst


def save_frame_to_file(frame, save_dir: str | Path, cam_name: str, *, seq: int,
                       ext_hint: str = "png", meta: dict | None = None) -> Path | None:
    """把一帧原始像素写到 save_dir，文件名带时间戳 + 相机短名 + 序号。

    - 8-bit：直接存 PNG（单通道灰度）。
    - 16-bit：存 PNG（PIL 的 'I;16'）；无 PIL/numpy 时退回 .raw 原字节。
    - 同时写同名 .json 旁路文件（基础参数 / GPS / 相机身份），便于多机统计。
    返回写入的图像路径；失败返回 None（异常信息挂到 frame._save_err 便于 GUI 日志）。
    """
    def _fail(msg: str):
        try:
            setattr(frame, "_save_err", msg)
        except Exception:
            pass
        return None

    try:
        d = Path(save_dir)
        d.mkdir(parents=True, exist_ok=True)
        w = int(getattr(frame, "width", 0) or 0)
        h = int(getattr(frame, "height", 0) or 0)
        bpp = int(getattr(frame, "bpp", 8) or 8)
        raw = getattr(frame, "data", None)
        # 立刻拷成独立 bytes，避免 Live 下一帧复用缓冲导致异步写盘读到空/残缺数据
        if raw is None:
            data = b""
        elif isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        else:
            try:
                data = bytes(raw)
            except Exception as exc:
                return _fail(f"data coerce: {exc}")
        ch = max(1, int(getattr(frame, "channels", 1) or 1))
        need = w * h * max(1, (bpp + 7) // 8) * ch
        if w <= 0 or h <= 0:
            return _fail(f"bad size {w}x{h}")
        if len(data) < need:
            return _fail(f"short data {len(data)}<{need} ({w}x{h} bpp={bpp} ch={ch})")
        tag = short_cam_id(cam_name)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # GPS 帧序号（SDK/IPC 字段名是 seq_number，不是 frame_counter）
        gps_seq = None
        gps0 = getattr(frame, "gps", None)
        if gps0 is not None:
            for attr in ("seq_number", "frame_counter"):
                v = getattr(gps0, attr, None)
                if v is None:
                    continue
                try:
                    gps_seq = int(v)
                    break
                except Exception:
                    continue
        if gps_seq is not None:
            base = f"{ts}_{tag}_g{gps_seq}_f{seq:06d}"
        else:
            base = f"{ts}_{tag}_f{seq:06d}"
        out: Path | None = None
        # 仅灰度（channels==1）才走 PIL 直接存；彩色帧退回 raw，避免误判通道排布。
        if ch == 1 and _PILImage is not None and _np is not None:
            try:
                if bpp <= 8:
                    arr = _np.frombuffer(data, dtype=_np.uint8, count=w * h).reshape(h, w)
                    out = d / f"{base}.png"
                    _PILImage.fromarray(arr, mode="L").save(out)
                else:
                    # 大画幅 16-bit PNG 编码极慢且峰值内存高（QHY600 ~120MB×数倍）
                    # Live 连续存时优先写 raw，旁路 json 仍保留；小图仍试 PNG
                    if w * h >= 8_000_000:
                        out = d / f"{base}.u16.raw"
                        out.write_bytes(data[: w * h * 2])
                    else:
                        n16 = min(w * h, len(data) // 2)
                        arr16 = _np.frombuffer(data, dtype="<u2", count=n16)
                        if arr16.size != w * h:
                            return _fail(f"u16 size {arr16.size}!={w*h}")
                        arr16 = arr16.reshape(h, w)
                        out = d / f"{base}.png"
                        try:
                            _PILImage.fromarray(arr16, mode="I;16").save(out)
                        except Exception:
                            out = d / f"{base}.u16.raw"
                            out.write_bytes(data[: w * h * 2])
            except Exception as exc:
                # PIL 失败则退回 raw，尽量不丢帧
                out = d / f"{base}.raw"
                out.write_bytes(data)
                try:
                    setattr(frame, "_save_err", f"png fallback raw: {exc}")
                except Exception:
                    pass
        else:
            out = d / f"{base}.raw"
            out.write_bytes(data)
        if out is None:
            return _fail("no output path")
        # sidecar JSON with basic params
        info = {
            "image": out.name,
            "cam_name": cam_name,
            "cam_short": tag,
            "seq": seq,
            "gps_seq_number": gps_seq,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "width": w,
            "height": h,
            "bpp": bpp,
            "channels": ch,
        }
        gps = getattr(frame, "gps", None)
        if gps is not None:
            try:
                info["gps"] = {
                    "seq_number": getattr(gps, "seq_number", None),
                    "frame_counter": getattr(gps, "seq_number", None),  # alias
                    "exposure_us": getattr(gps, "exposure_us", None),
                    "start_sec": getattr(gps, "start_sec", None),
                    "start_us": getattr(gps, "start_us", None),
                    "end_sec": getattr(gps, "end_sec", None),
                    "end_us": getattr(gps, "end_us", None),
                    "now_sec": getattr(gps, "now_sec", None),
                    "now_us": getattr(gps, "now_us", None),
                    "pps": getattr(gps, "pps", None),
                    "gps_status": getattr(gps, "gps_status", None),
                    "latitude": getattr(gps, "latitude", None),
                    "longitude": getattr(gps, "longitude", None),
                }
            except Exception:
                info["gps"] = str(gps)
        if meta:
            info["params"] = meta
        try:
            (d / f"{base}.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except Exception:
            pass
        return out
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")



def _default_save_dir() -> Path:
    return APP_ROOT / "captures"



class QueueWriter(io.TextIOBase):
    def __init__(self, out_queue: "queue.Queue[str]"):
        self.q = out_queue

    def write(self, s: str):
        if s:
            self.q.put(s)
        return len(s)

    def flush(self):
        pass


# ============================================================================
# 多显示器工具（从 fpga_soft_gui.py 原样移植：MonitorInfo / list_monitors /
#   monitor_containing_point / short_cam_id）。用于把多台相机的预览窗平铺到
# 指定物理显示器（上下/左右/网格/整屏/右侧）。
# ============================================================================
def short_cam_id(cam_name: str) -> str:
    """QHY600M-ac14f178... → ac14；过短则原样截断。"""
    name = (cam_name or "").strip()
    if "-" in name:
        tail = name.split("-", 1)[1]
        return (tail[:4] if len(tail) >= 4 else tail) or name[-8:]
    return name[-8:] if len(name) > 8 else name


@dataclass
class MonitorInfo:
    """一块物理显示器（工作区已扣任务栏）。"""
    index: int
    label: str
    is_primary: bool
    x: int
    y: int
    w: int
    h: int
    work_x: int
    work_y: int
    work_w: int
    work_h: int


def list_monitors() -> list[MonitorInfo]:
    """枚举全部显示器。Windows 用 EnumDisplayMonitors；其它平台退回主屏。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            class MONITORINFOEXW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", RECT),
                    ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32),
                ]

            user32 = ctypes.windll.user32
            found: list[MonitorInfo] = []

            def _cb(hmon, _hdc, _lprc, _data):
                info = MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                    return 1
                mx, my = int(info.rcMonitor.left), int(info.rcMonitor.top)
                mw = int(info.rcMonitor.right - info.rcMonitor.left)
                mh = int(info.rcMonitor.bottom - info.rcMonitor.top)
                wx, wy = int(info.rcWork.left), int(info.rcWork.top)
                ww = int(info.rcWork.right - info.rcWork.left)
                wh = int(info.rcWork.bottom - info.rcWork.top)
                primary = bool(info.dwFlags & 1)  # MONITORINFOF_PRIMARY
                idx = len(found) + 1
                tag = "主" if primary else f"{idx}"
                label = f"显示器{idx} {mw}x{mh} ({tag})"
                found.append(
                    MonitorInfo(
                        index=idx,
                        label=label,
                        is_primary=primary,
                        x=mx,
                        y=my,
                        w=mw,
                        h=mh,
                        work_x=wx,
                        work_y=wy,
                        work_w=max(1, ww),
                        work_h=max(1, wh),
                    )
                )
                return 1

            MONITORENUMPROC = ctypes.WINFUNCTYPE(
                ctypes.c_int,
                wintypes.HMONITOR,
                wintypes.HDC,
                ctypes.POINTER(RECT),
                wintypes.LPARAM,
            )
            cb = MONITORENUMPROC(_cb)
            user32.EnumDisplayMonitors(0, 0, cb, 0)
            if found:
                # 主屏优先，其余按几何位置
                found.sort(key=lambda m: (not m.is_primary, m.x, m.y, m.index))
                for i, m in enumerate(found, start=1):
                    m.index = i
                    tag = "主" if m.is_primary else f"{i}"
                    m.label = f"显示器{i} {m.w}x{m.h} ({tag})"
                return found
        except Exception:
            pass
    return [
        MonitorInfo(
            index=1,
            label="显示器1 (主)",
            is_primary=True,
            x=0,
            y=0,
            w=1920,
            h=1080,
            work_x=0,
            work_y=0,
            work_w=1920,
            work_h=1040,
        )
    ]


def monitor_containing_point(monitors: list[MonitorInfo], x: int, y: int) -> MonitorInfo | None:
    """点 (x,y) 落在哪块屏的工作区（先 work，再整屏矩形）。"""
    for m in monitors:
        if m.work_x <= x < m.work_x + m.work_w and m.work_y <= y < m.work_y + m.work_h:
            return m
    for m in monitors:
        if m.x <= x < m.x + m.w and m.y <= y < m.y + m.h:
            return m
    return monitors[0] if monitors else None


# --- master/slave sync modes (verbatim names from host/fpga_soft_modes.json) ---
# (button label, mode name in fpga_soft_modes.json, target: master | slaves | all)
SYNC_GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    # (button label, step key, role target: master|slaves|all, transport: usb|pcie|any)
    ("USB", [
        ("主机一键设置", "usb_master", "master", "usb"),
        ("从机一键设置", "usb_slave", "slaves", "usb"),
        ("Burst补包_USB", "usb_burst", "all", "usb"),
    ]),
    ("PCIe / 光纤", [
        ("主机配置(压住IDLE)", "pcie_master_hold", "master", "pcie"),
        ("从机配置", "pcie_slave", "slaves", "pcie"),
        ("Burst补包_PCIe", "pcie_burst", "all", "pcie"),
    ]),
    ("公共", [
        ("主机触发开拍(释放IDLE)", "trigger_release", "master", "any"),
        ("退出同步拍摄(恢复单机)", "exit_sync", "all", "any"),
    ]),
]

ROLE_MASTER = "主机"
ROLE_SLAVE = "从机"
ROLE_STANDALONE = "单机"
ROLE_CHOICES = [ROLE_MASTER, ROLE_SLAVE, ROLE_STANDALONE]
TRANSPORT_USB = "USB"
TRANSPORT_PCIE = "PCIe/光纤"
TRANSPORT_CHOICES = [TRANSPORT_USB, TRANSPORT_PCIE]

# ===========================================================================
# i18n (中英文切换).  默认英文; 键 -> {en, zh}.  角色/传输等「逻辑值」仍用
# 中文常量, 显示层按语言映射 (见 CaptureApp.role_display / transport_display).
# ===========================================================================
I18N = {
    "app_title": {"en": "LiuSir Camera Capture (Master/Slave Sync)", "zh": "LiuSir 相机采集 (主从同步版)"},
    "project_root": {"en": "Project Root:", "zh": "Project Root:"},
    "scan": {"en": "Scan Cameras", "zh": "扫描相机"},
    "connect": {"en": "Connect", "zh": "连接"},
    "disconnect": {"en": "Disconnect", "zh": "断开"},
    "layout": {"en": "Layout", "zh": "布局"},
    "retile": {"en": "Retile Previews", "zh": "重排预览窗"},
    "tab_cams": {"en": "Cameras / Roles", "zh": "相机 / 主从"},
    "tab_params": {"en": "Params / Capture", "zh": "参数 / 采集"},
    "tab_sync": {"en": "Master/Slave Sync", "zh": "主从同步"},
    "cams_hint": {
        "en": "Workflow: click 'Scan Cameras' to list all cameras, then assign each a role "
              "(Master/Slave/Solo, one master) and a transport (USB / PCIe) in the table below. "
              "'Params' and 'Sync' then act according to this assignment.",
        "zh": "流程：点「扫描相机」列出所有相机 → 在下表手动给每台指定 主/从 角色（一主多从）"
              "与 传输方式（USB / PCIe光纤）。之后「参数设置」与「主从同步」都按这个分配来作用。"},
    "hdr_name": {"en": "Camera", "zh": "相机"},
    "hdr_role": {"en": "Role (Master/Slave/Solo)", "zh": "角色(主/从/单机)"},
    "hdr_transport": {"en": "Transport (USB/PCIe)", "zh": "传输(USB/PCIe)"},
    "hdr_state": {"en": "State", "zh": "状态"},
    "sel_arrow": {"en": "Selected camera →", "zh": "选中相机 →"},
    "trans_arrow": {"en": "  Transport →", "zh": "  传输 →"},
    "set_master": {"en": "Set Master", "zh": "设为主机"},
    "set_slave": {"en": "Set Slave", "zh": "设为从机"},
    "set_solo": {"en": "Set Solo", "zh": "设为单机"},
    "set_rest_slaves": {"en": "Rest → Slaves", "zh": "其余全部设为从机"},
    "clear_roles": {"en": "Clear Roles", "zh": "清空角色"},
    "target_cam": {"en": "Target Camera (current params apply to)", "zh": "目标相机 (当前参数应用到)"},
    "no_cam": {"en": "(none — select a camera in 'Cameras / Roles' first)", "zh": "(未选择 —— 先在「相机/主从」页选中一台)"},
    "capture_params": {"en": "Capture Parameters (SDK)", "zh": "采集参数 (SDK)"},
    "readmode": {"en": "Read mode", "zh": "Read mode"},
    "bits": {"en": "Bits", "zh": "Bits"},
    "exposure": {"en": "Exposure (us)", "zh": "Exposure (us)"},
    "gain": {"en": "Gain", "zh": "Gain"},
    "offset": {"en": "Offset", "zh": "Offset"},
    "usb_traffic": {"en": "USB traffic", "zh": "USB traffic"},
    "speed": {"en": "Speed", "zh": "Speed"},
    "bin": {"en": "Bin", "zh": "Bin"},
    "roi": {"en": "ROI x,y,w,h", "zh": "ROI x,y,w,h"},
    "ddr": {"en": "DDR", "zh": "DDR"},
    "query_res": {"en": "Query SDK res", "zh": "Query SDK res"},
    "readback": {"en": "Read back", "zh": "Read back"},
    "fill_roi": {"en": "Fill full ROI", "zh": "填入全幅 ROI"},
    "restore_full": {"en": "Restore full frame", "zh": "恢复全幅"},
    "apply_roi": {"en": "Apply ROI", "zh": "应用 ROI"},
    "roi_select": {"en": "Box ROI", "zh": "框选 ROI"},
    "roi_select_on": {"en": "Boxing…", "zh": "框选中…"},
    "roi_select_tip": {
        "en": "Drag on that camera's preview to set its own ROI. Stop Live if running, then Apply ROI (per cam).",
        "zh": "在对应相机的预览上拖框，只改该机 ROI。若正在 Live，请先 Stop，再点「应用 ROI」（按相机分别下发）。",
    },
    "roi_per_cam": {
        "en": "ROI is per camera; switching cams loads that cam's box.",
        "zh": "ROI 按相机分别保存；切换相机时载入该机的框选。",
    },
    "roi_applied": {"en": "ROI set to {x},{y} {w}x{h}", "zh": "ROI 已设为 {x},{y} {w}x{h}"},
    "roi_applied_cam": {
        "en": "{cam}: ROI {x},{y} {w}x{h}",
        "zh": "{cam}: ROI {x},{y} {w}x{h}",
    },
    "roi_too_small": {"en": "ROI too small", "zh": "框选区域太小"},
    "roi_need_stop": {
        "en": "ROI needs session rebuild. Stop Live, then click Apply ROI.",
        "zh": "ROI 需重建会话：请先 Stop Live，再点「应用 ROI」。",
    },
    "roi_restored": {"en": "Restored full frame", "zh": "已恢复全幅分辨率"},
    "resolution": {"en": "Resolution", "zh": "分辨率"},
    "gps_trigger": {"en": "Frame Seq / GPS", "zh": "帧序号 / GPS"},
    "gps_on": {
        "en": "Frame seq on (GPS overlay / SetGPSOn)",
        "zh": "帧序号 on（开 overlay / SetGPSOn）",
    },
    "gps_on_note": {
        "en": "Turns on FPGA GPS head replace + FrameNumberEnable (reg142/reg56 via CAM_GPS). "
              "Host parses seq from the frame header.",
        "zh": "打开 FPGA 帧头替换 + 帧序号使能（SDK CAM_GPS → reg142/reg56）；"
              "主机从帧头解析序号。",
    },
    "gps_advanced_toggle": {
        "en": "Show advanced (LED cal / POS / Slave UTC)",
        "zh": "显示高级（校灯 / POS / Slave UTC）",
    },
    "gps_slave": {"en": "GPS slave (follow master SMA)", "zh": "GPS slave（从机跟主机 SMA）"},
    "gps_led_cal": {"en": "GPS LED cal (keep light ON)", "zh": "GPS LED cal（保持校灯 ON）"},
    "gps_apply": {"en": "Apply GPS POS / LED cal", "zh": "Apply GPS POS / LED cal"},
    "gps_panel": {"en": "Frame Seq List", "zh": "帧序号列表"},
    "gps_mode_refresh": {"en": "Show: Refresh", "zh": "显示: 默认刷新"},
    "gps_mode_seq": {"en": "Show: Seq List", "zh": "显示: 帧序号列表"},
    "gps_reset_seq": {"en": "Reset Seq", "zh": "复位序号"},
    "gps_list_max": {"en": "List max", "zh": "列表上限"},
    "gps_panel_note": {
        "en": "Preview shows seq by default (Detail for full GPS). Sync / multi-cam auto-switches "
              "to Seq List in the main monitor: [01]..[N], freeze at N, dedup; gaps marked ←miss k. "
              "② Shoot auto-resets the list. Missing seqs = (burst start+1 .. burst end-1) not seen.",
        "zh": "预览默认只显示帧序号（点「详情」看完整 GPS）。同步/多机自动切到「帧序号列表」："
              "主窗监视区 [01]..[N]，满 N 冻结、同序号去重；跳号旁标 ←缺k。"
              "② 开拍自动复位序号。丢失帧=期望范围 burst开始+1～burst结束-1 中未收到的序号。",
    },
    "gps_gap_warn": {"en": "suspected drop", "zh": "疑似丢帧"},
    "gps_lost_seqs": {"en": "lost", "zh": "丢失"},
    "gps_preview_detail": {"en": "GPS detail", "zh": "GPS 详情"},
    "gps_utc": {"en": "GPS Slave UTC (Ezcap Set UTC)", "zh": "GPS Slave UTC（高级）"},
    "gps_utc_ymd": {"en": "UTC Y/M/D", "zh": "UTC Y/M/D"},
    "gps_utc_hms": {"en": "UTC H/M/S", "zh": "UTC H/M/S"},
    "gps_slave_exp": {"en": "Slave exp", "zh": "Slave exp"},
    "gps_target": {"en": "target_us", "zh": "target_us"},
    "gps_delta": {"en": "delta", "zh": "delta"},
    "gps_from_now": {"en": "From now", "zh": "From now"},
    "gps_fill_now": {"en": "Fill from now+N", "zh": "Fill from now+N"},
    "gps_js1995": {"en": "JS1995", "zh": "JS1995"},
    "gps_utc_note": {
        "en": "Editing UTC auto-converts JS1995; use the button below to send to the camera "
              "(SetQHYCCDGPSSlaveModeParameter).",
        "zh": "改 UTC 会自动换算 JS1995；下发到相机请点下面按钮（SetQHYCCDGPSSlaveModeParameter）。"},
    "gps_set_utc": {"en": "Set GPS Slave UTC", "zh": "Set GPS Slave UTC"},
    "param_scope": {"en": "Param Apply Scope (master/slave aware)", "zh": "参数下发范围 (可区分主从)"},
    "scope_current": {"en": "Current camera", "zh": "当前选中相机"},
    "scope_all": {"en": "All connected", "zh": "全部已连"},
    "scope_masters": {"en": "All masters", "zh": "全部主机"},
    "scope_slaves": {"en": "All slaves", "zh": "全部从机"},
    "apply_now": {"en": "Apply Now", "zh": "立即下发"},
    "onekey": {"en": "Master/Slave One-Click (set roles first)", "zh": "主从一键操作 (先确定主机/从机)"},
    "onekey1": {"en": "① Set Master/Slave Params", "zh": "① 一键设置主从参数"},
    "onekey2": {"en": "② Enter Sync (shoot)", "zh": "② 进入同步拍摄(开拍)"},
    "onekey3": {"en": "③ Exit Sync (restore)", "zh": "③ 退出同步拍摄(恢复)"},
    "onekey_note": {
        "en": "① = write master config to the master, slave config to each slave (per USB/PCIe); "
              "② = master releases IDLE to start; ③ = all master/slave cameras restore solo. "
              "Order: Live first → ① → ②; finish with ③.",
        "zh": "①=按各自 USB/PCIe 给主机写主机配置、从机写从机配置；"
              "②=仅主机释放 IDLE 开拍；③=全部主/从相机恢复单机。\n"
              "顺序：先 Live 出图 → ① 设置 → ② 开拍；结束点 ③。"},
    "sync_params": {"en": "Master/Slave Sync Params (USB / PCIe)", "zh": "主从同步相机参数 (区分 USB / PCIe)"},
    "usb_patch": {"en": "USB burst count (manual)", "zh": "USB burst 数量(可手动)"},
    "pcie_patch": {"en": "PCIe burst count (manual)", "zh": "PCIe burst 数量(可手动)"},
    "burst_start": {"en": "burst start (reg50)", "zh": "burst 开始 (reg50)"},
    "burst_window": {"en": "burst end (reg51/52, frames-1)", "zh": "burst 结束 (reg51/52，一组帧数-1)"},
    "min_pulse": {"en": "slave min pulse (×40ns)", "zh": "从机最小脉宽 (×40ns)"},
    "reg39": {"en": "reg39 (master/slave)", "zh": "reg39 (主/从)"},
    "reg58": {"en": "reg58 (master/slave)", "zh": "reg58 (主/从)"},
    "syncp_note": {
        "en": "Defaults = measured values of each one-click mode in host/fpga_soft_modes.json. "
              "The six buttons on the 'Sync' page generate register sequences from these params: "
              "USB group uses USB patch, PCIe group uses PCIe patch, shared burst start/end/pulse/39/58.",
        "zh": "默认值 = host/fpga_soft_modes.json 各一键模式实测值。"
              "「主从同步」页的六个按钮按这里的参数生成寄存器序列：USB 组用 USB 补包，"
              "PCIe 组用 PCIe 补包，共用 burst 起止/脉宽/39/58。"},
    "capture_entry": {"en": "Capture Entry", "zh": "拍摄入口"},
    "scope": {"en": "Scope:", "zh": "范围:"},
    "ls_current": {"en": "Current camera", "zh": "当前相机"},
    "ls_all": {"en": "All cameras", "zh": "全部相机"},
    "scope_note": {
        "en": "Decides WHICH cameras output frames for Live / Single Capture. "
              "'Current' = only the selected camera; 'All' = every camera from the last scan. "
              "Cameras open automatically — no manual Connect. In 'All' mode every listed camera "
              "streams at the same time.",
        "zh": "决定 Live / 单帧 时哪些相机会出图。「当前相机」= 只出选中的那台；"
              "「全部相机」= 上次扫描到的全部相机。相机会自动打开，无需手动点 Connect。"
              "「全部相机」模式下，列出的每台相机都会同时出图。"},
    "live_stop": {"en": "Stop Live", "zh": "停止 Live"},
    "stop_live": {"en": "Stopping Live", "zh": "正在停止 Live"},
    "capture_single": {"en": "Single Capture", "zh": "单帧 Capture"},
    "close": {"en": "Close", "zh": "关闭"},
    "close_all": {"en": "Close All Cameras", "zh": "断开全部相机"},
    "status": {"en": "Status", "zh": "状态"},
    "auto_push": {"en": "Auto-push on change (live-safe)", "zh": "改动即自动下发(不中断出图)"},
    "auto_push_note": {
        "en": "While a camera is live/connected, edits to params are pushed to it immediately "
              "(live-safe: exposure/gain/offset/traffic/speed/bits). 'Read back' re-syncs the GUI "
              "to the camera's real values; 'Full push' forces a re-init session.",
        "zh": "相机处于 live / 已连接时，改动参数会立即下发到相机（live-safe：曝光/gain/offset/"
              "traffic/speed/bits，不中断出图）。「回读」把相机真实值同步回界面；"
              "「完整下发」强制重建会话（含 bin/ROI/读出模式）。"},
    "filter_burst_waste": {
        "en": "Filter burst waste frames (GPS seq)",
        "zh": "过滤 burst 废帧 (按 GPS 序号)",
    },
    "filter_burst_waste_note": {
        "en": "When GPS seq is present: group period = end+1 (indices 0..end); keep [start, end], "
              "discard idx < start (default start=1 → drop 0,12,24…). "
              "Waste frames are not saved, do not consume Live save cadence, and are omitted "
              "from the GPS seq list. start=reg50, end=reg51/52.",
        "zh": "有 GPS 序号时：一组周期 = 结束+1（序号 0..结束）；保留 [开始, 结束]，"
              "丢弃 idx < 开始（默认开始=1 → 丢 0、12、24…）。"
              "废帧不存盘、不占 Live 保存节拍，也不写入帧序号列表。"
              "开始=reg50，结束=reg51/52。",
    },
    "refresh_before_capture": {
        "en": "After Scan: auto-connect one camera & read params",
        "zh": "扫描后自动连接并读回参数",
    },
    "refresh_before_capture_note": {
        "en": "After Scan finds cameras, auto-open one (current selection, else the first) "
              "and read its exposure/gain/offset/traffic/speed/bits/read mode into the capture "
              "params panel. Live/Capture then just apply the GUI values. Uncheck to only list "
              "cameras on Scan (manual Connect / Read back still work).",
        "zh": "扫描到相机后，自动打开一台（优先当前选中，否则第一台），"
              "并把该机曝光/gain/offset/traffic/speed/bits/读出模式读回采集参数区。"
              "之后 Live/Capture 直接按下发 GUI。取消勾选则扫描只列相机"
              "（仍可用手动 Connect / Read back）。",
    },
    "param_status": {"en": "Param status", "zh": "参数状态"},
    "ps_idle": {"en": "Idle — click Live / Single to auto-connect.", "zh": "空闲 —— 点 Live / 单帧 会自动连接。"},
    "busy_scan": {"en": "Scanning cameras…", "zh": "正在扫描相机…"},
    "busy_scan_done": {"en": "Scan done: {n} camera(s).", "zh": "扫描完成：{n} 台。"},
    "busy_autoconnect": {"en": "Opening {name} after scan…", "zh": "扫描后正在打开 {name}…"},
    "busy_readback": {"en": "Reading params from {name}…", "zh": "正在从 {name} 读回参数…"},
    "busy_push": {"en": "Pushing params {i}/{n}: {name}", "zh": "正在下发参数 {i}/{n}: {name}"},
    "busy_push_done": {"en": "Params pushed ({n}).", "zh": "参数下发完成（{n} 台）。"},
    "busy_live_open": {"en": "Live prepare: opening {i}/{n} {name}", "zh": "Live 准备：打开相机 {i}/{n} {name}"},
    "busy_live_apply": {"en": "Live prepare: applying params…", "zh": "Live 准备：正在下发参数…"},
    "busy_cap_open": {"en": "Single capture: opening {i}/{n} {name}", "zh": "单帧：打开相机 {i}/{n} {name}"},
    "busy_cap_apply": {"en": "Single capture: applying params…", "zh": "单帧：正在下发参数…"},
    "busy_done": {"en": "Ready.", "zh": "就绪。"},
    "ps_live": {"en": "Live — edits auto-push to the camera.", "zh": "Live 中 —— 改动会实时下发相机。"},
    "ps_pushed": {"en": "Pushed: {what} → {n} cam(s)", "zh": "已下发: {what} → {n} 台"},
    "ps_pushfail": {"en": "Push failed: {m}", "zh": "下发失败: {m}"},
    "ps_noconn": {"en": "Camera not open yet — start Live first.", "zh": "相机尚未打开 —— 请先点 Live。"},
    "ps_readback": {"en": "Read back {name} real params.", "zh": "已读回 {name} 实际参数。"},
    "save": {"en": "Save images", "zh": "图像存储"},
    "save_note": {
        "en": "When enabled, every Live frame and Single Capture frame is saved to the folder "
              "(timestamped PNG / RAW). Pick a folder or use the default.",
        "zh": "勾选后，每帧 Live 图与单帧图都会存到该文件夹（时间戳 PNG/RAW + 同名 json；开 GPS 帧序号时文件名带 g序号、json 写 seq_number）。"
              "多机时按相机短名分子目录；可设二级目录名。"
              "写盘慢时帧先进 RAM 缓存再慢慢落盘；预警显示缓存还能大约再收几张。"},
    "save_dir": {"en": "Folder:", "zh": "目录:"},
    "save_browse": {"en": "Browse…", "zh": "选择…"},
    "save_enabled": {"en": "Save frames", "zh": "保存帧"},
    "save_progress": {"en": "Save progress", "zh": "存图进度"},
    "save_prog_idle": {"en": "Save idle", "zh": "存图空闲"},
    "save_prog_fmt": {
        "en": "Saved {done}/{total} ({pct}%)  queue {q} (~{mib} MiB)  room~{room}  drop {drop}",
        "zh": "已存 {done}/{total}（{pct}%） 缓存 {q}（约 {mib} MiB） 还可约 {room} 张  异常丢 {drop}",
    },
    "save_drop_warn": {
        "en": "WARNING: save copy failed / OOM — dropped {n}. Free RAM or raise N / use ROI.",
        "zh": "预警：拷帧失败或内存不足，异常丢弃 {n} 帧。请腾出内存、加大 N，或用 ROI。",
    },
    "save_room_warn": {
        "en": "WARNING: save RAM cache low — about {room} frame(s) left before memory pressure.",
        "zh": "预警：存图 RAM 缓存将满，大约还能再收 {room} 张；写盘仍在追，请稍候或加大 N。",
    },
    "save_interval": {"en": "Live every N frames (1=each):", "zh": "Live 每 N 帧存(1=每帧):"},
    "save_subdir": {"en": "Subfolder (optional):", "zh": "二级目录(可选):"},
    "save_subdir_note": {
        "en": "Empty = save under Folder. Non-empty = Folder/Subfolder/; per-camera subdirs are created under that. If Subfolder already exists you will be asked.",
        "zh": "空=存到「目录」。非空=在目录下新建该二级文件夹；其下再按相机分子目录。若二级目录已存在会提示是否继续。"},
    "save_subdir_exists": {
        "en": "Subfolder already exists:\n{path}\nContinue saving into it?",
        "zh": "二级目录已存在：\n{path}\n是否继续保存到该目录？"},
    "save_subdir_bad": {
        "en": "Invalid subfolder name (no path separators / reserved names).",
        "zh": "二级目录名不合法（不能含路径分隔符或系统保留名）。"},
    "save_done": {"en": "Saved {n} image(s) → {dir}", "zh": "已保存 {n} 张 → {dir}"},
    "save_fail": {"en": "Save failed: {m}", "zh": "保存失败: {m}"},
    "full_push": {"en": "Full push", "zh": "完整下发"},
    "cfg_save": {"en": "Save cfg…", "zh": "保存配置…"},
    "cfg_load": {"en": "Load cfg…", "zh": "加载配置…"},
    "cfg_save_title": {"en": "Save parameter config", "zh": "保存参数配置"},
    "cfg_load_title": {"en": "Load parameter config", "zh": "加载参数配置"},
    "cfg_saved": {"en": "Config saved → {path}", "zh": "配置已保存 → {path}"},
    "cfg_loaded": {"en": "Config loaded ← {path}", "zh": "配置已加载 ← {path}"},
    "cfg_bad": {"en": "Invalid or unsupported config file.", "zh": "配置文件无效或不支持。"},
    "status_idle": {"en": "Not Live / Capture yet.", "zh": "尚未 Live / Capture。"},
    "dash_idle": {
        "en": "Not Live / Capture yet.\nPick 'All cameras' in Scope then click Live: every scanned "
              "camera opens automatically and gets its own preview window.",
        "zh": "尚未 Live / Capture。\n勾选「多机同时采集」后点 Live：扫描列表中的相机都会自动打开并各弹一窗。"},
    "current_assign": {"en": "Current Assignment (Master/Slave/Transport)", "zh": "当前分配 (主机/从机/传输)"},
    "capture_log": {"en": "Capture Log", "zh": "采集日志"},
    "sync_hint": {
        "en": "Master/Slave sync buttons (grouped by USB / PCIe / common, 8 total: 6 master/slave "
              "configs + 2 burst patches). Each writes a register sequence (built from 'Params' page "
              "sync params) to cameras matching the role/transport (via a local handle, so it won't "
              "clobber the capture session's Init). Use 'Preview Register Sequence' to inspect, "
              "'Dry-run All' to print all to the log.\nNote: enter Live first, then click "
              "'Master config / Trigger'; the reverse order yields 0 frames.",
        "zh": "主从同步按钮（按 USB / PCIe光纤 / 公共 分组，共 8 个：含 6 个主从配置 + 2 个 Burst补包）。"
              "每个按钮按「参数设置」页的『主从同步相机参数』生成寄存器序列，写到匹配角色/传输的相机"
              "（走本机独立句柄，不会冲掉采图会话的 Init）。点「预览寄存器序列」查看每个按钮具体写哪些寄存器，"
              "点「Dry-run 全部」把全部序列打印到日志。\n"
              "注意：必须先让采集进入 live 出图，再点「主机配置/触发」；顺序反了会出 0 帧。"},
    "sync_target": {"en": "Target:", "zh": "作用对象:"},
    "preview_seq": {"en": "Preview Register Sequence", "zh": "预览寄存器序列"},
    "dryrun_all": {"en": "Dry-run All", "zh": "Dry-run 全部"},
    "sync_status": {"en": "Sync Status / Log", "zh": "同步状态 / 日志"},
    "preview_title": {"en": "Sync Buttons -> Register Sequence (preview)", "zh": "主从同步按钮 -> 寄存器序列 预览"},
    "role_master": {"en": "Master", "zh": "主机"},
    "role_slave": {"en": "Slave", "zh": "从机"},
    "role_solo": {"en": "Solo", "zh": "单机"},
    "tr_usb": {"en": "USB", "zh": "USB"},
    "tr_pcie": {"en": "PCIe / Fiber", "zh": "PCIe/光纤"},
    "mon_follow_main": {"en": "Follow main window", "zh": "跟随主窗"},
    "mon_follow_focus": {"en": "Follow focused preview", "zh": "跟随焦点预览窗"},
    "layout_auto": {"en": "Auto", "zh": "自动"},
    "layout_v": {"en": "Vertical", "zh": "上下"},
    "layout_h": {"en": "Horizontal", "zh": "左右"},
    "layout_grid": {"en": "Grid", "zh": "网格"},
    "region_full": {"en": "Full screen", "zh": "整屏"},
    "region_right": {"en": "Right side", "zh": "右侧"},
    "st_live": {"en": "live", "zh": "live"},
    "st_conn": {"en": "connected", "zh": "已连"},
    "st_disconn": {"en": "disconnected", "zh": "未连"},
    "mark_current": {"en": "<-- current", "zh": "<-- 当前"},
    "no_cam_yet": {"en": "(no cameras scanned yet)", "zh": "(还没扫描相机)"},
    "preview_panel": {"en": "Panel", "zh": "面板"},
    "preview_compact": {"en": "Compact", "zh": "精简"},
    "preview_copy_gray": {"en": "Copy gray to all", "zh": "复制灰度到全部窗"},
    "gray_frame": {"en": "Gray (this preview only)", "zh": "灰度调节（仅本窗预览）"},
    "preview_retitle": {"en": "Retile All Previews", "zh": "重排全部预览窗"},
    "preview_this_monitor": {"en": "Tile to this window's monitor", "zh": "排到本窗所在显示器"},
    "preview_show_panel": {"en": "Show Panel (Gray/GPS)", "zh": "显示面板（灰度/GPS）"},
    "preview_compact_menu": {"en": "Compact Panel (image fills)", "zh": "精简面板（图像铺满）"},
    "preview_layout_prefix": {"en": "Layout: ", "zh": "排列："},
    "preview_region_prefix": {"en": "Area: ", "zh": "屏内区域："},
    "preview_monitor_prefix": {"en": "Monitor: ", "zh": "显示器："},
    "params": {"en": "Params", "zh": "参数"},
    "need_select_cam": {"en": "Select a camera in the 'Cameras / Roles' tab first.", "zh": "先在「相机/主从」页选中一台相机。"},
    "no_cam_apply": {"en": "No camera to apply params to (check the scope / selection).", "zh": "没有可下发参数的相机（检查范围 / 选中项）。"},
    "sel_conn": {"en": "Select and connect a camera first.", "zh": "先选中并连接一台相机。"},
    "readback_done": {"en": "Read back current params from {name}", "zh": "已读回 {name} 当前参数"},
    "connect_done": {"en": "Connected {name} (independent capture process)", "zh": "已连接 {name}（独立采图子进程）"},
    "connect_fail": {"en": "Connect failed: {m}", "zh": "连接失败: {m}"},
    "connected": {"en": "Connected {name}", "zh": "已连接 {name}"},
    "disconnect_done": {"en": "Disconnected {name}", "zh": "已断开 {name}"},
    "scan_found": {"en": "Found {n} cameras; assign Master/Slave roles in the table.", "zh": "扫描到 {n} 台相机，请在下页手动分配主/从角色。"},
    "scan_fail": {"en": "Scan failed: {m}", "zh": "扫描失败: {m}"},
    "need_stop_live": {
        "en": "Stop Live first, then try again.",
        "zh": "请先停止 Live，再操作。",
    },
    "ps_need_stop": {
        "en": "Live is running — Stop Live first.",
        "zh": "Live 进行中 —— 请先停止 Live。",
    },
    "closing_busy": {
        "en": "Stopping Live before disconnect...",
        "zh": "断开前正在停止 Live...",
    },
    "reconnect_hint": {
        "en": "Reconnect tip: Close All → Scan → Live.",
        "zh": "重连提示：先断开全部 → 扫描 → 再 Live。",
    },
    "readmode_live_tip": {
        "en": "Read mode changed. Stop Live, then Live again to apply.",
        "zh": "读出模式已改。请先停止 Live，再点 Live 生效。",
    },
    "bin_live_tip": {
        "en": "Bin changed. Stop Live, then Live again to apply.",
        "zh": "Bin 已改。请先停止 Live，再点 Live 生效。",
    },
    "pool_rebuild": {
        "en": "Capture worker died; rebuilt session for {name}.",
        "zh": "采图子进程已死，已为 {name} 重建会话。",
    },
}


@dataclass
class CamRow:
    """One scanned camera and its manual master/slave + transport assignment."""
    name: str
    role: str = ROLE_STANDALONE
    transport: str = TRANSPORT_USB
    live: bool = False
    connected: bool = False

    def role_tag(self) -> str:
        return {"主机": "master", "从机": "slave", "单机": "solo"}.get(self.role, "solo")


# ============================================================================
# 主窗托管的浮动预览窗（一台一窗），从 fpga_soft_gui.py 的 CamPreviewWin 忠实移植：
#   画布拖拽/缩放 + 每窗独立灰度(Bright/Contrast/黑/白) + 自动拉伸/复位/复制灰度到全部窗
#   + 布局入口(上下/左右/网格/排到本屏/选显示器/选区域/重排) + 右键菜单
#   + 每窗 GPS 明细 + GPS 帧序号统计(默认刷新 / 帧序号列表 两种模式) + 精简面板
# 显示走「原图缓存 + 主线程按当前滑条渲染」(queue_frame / _render_from_base /
#   flush / rerender)：采集线程只更新原图并记账，不碰 Tk；避免灰度调节被下一帧盖掉。
# ============================================================================
class CamPreviewWin:
    """主窗托管的浮动预览窗（一台一窗）：图像 + 本机灰度 + 本机 GPS。"""

    def __init__(self, app: "CaptureApp", cam_name: str):
        self.app = app
        self.cam_name = cam_name
        self.short = short_cam_id(cam_name)
        self.win = tk.Toplevel(app.root)
        self.win.title(
            f"预览 {self.short}" if app.lang == "zh" else f"Preview {self.short}")
        self.win.geometry("560x720")
        self.win.minsize(280, 180)

        # 每窗独立灰度 / Zoom（与主窗其它相机互不影响）
        self.brightness = tk.DoubleVar(value=float(app.v_bright.get()))
        self.contrast = tk.DoubleVar(value=float(app.v_contrast.get()))
        self.black = tk.DoubleVar(value=float(app.v_black.get()))
        self.white = tk.DoubleVar(value=float(app.v_white.get()))
        self.zoom_var = tk.StringVar(value="Fit")
        self.brightness_lbl = tk.StringVar(value="0")
        self.contrast_lbl = tk.StringVar(value="1.00")
        self.black_lbl = tk.StringVar(value="0")
        self.white_lbl = tk.StringVar(value="255")
        self.status_var = tk.StringVar(value=f"{self.short} | IDLE")
        self.stats_var = tk.StringVar(value="No frame yet.")
        self.gps_short = ""
        # GPS on 时按帧序号累计（仅本窗；不做跨机配对）
        self.last_seq: int | None = None
        self.first_seq: int | None = None
        self.seq_count = 0
        self.seq_gaps = 0  # Δseq>1 时累计 (delta-1)
        self.seq_dups = 0  # Δseq==0
        # 帧序号列表（主界面「显示: 帧序号列表」时追加；默认刷新模式不写）
        self.seq_list: list[int] = []
        self._seq_list_dirty = False  # 采图线程置位，主线程 flush 时刷 Text
        self._seq_stats_dirty = False  # last_seq 变了：主窗状态栏要刷，即使列表已满不再追加
        self._base: tuple[int, int, bytes] | None = None
        self._canvas_w = 480
        self._canvas_h = 360
        # 多窗平铺时默认精简面板，把高度留给图像（上下布局尤其需要）
        self._compact = False
        self._panel_btn_txt = tk.StringVar(value="面板")

        ttk.Label(self.win, textvariable=self.status_var, anchor="w").pack(
            fill=tk.X, padx=6, pady=(4, 0)
        )
        hdr = ttk.Frame(self.win)
        hdr.pack(fill=tk.X, padx=6, pady=(2, 0))
        ttk.Label(hdr, text="Zoom").pack(side=tk.LEFT)
        zc = ttk.Combobox(
            hdr,
            textvariable=self.zoom_var,
            values=app._preview_zoom_choices,
            state="readonly",
            width=8,
        )
        zc.pack(side=tk.LEFT, padx=(4, 0))
        zc.bind("<<ComboboxSelected>>", lambda _e: self.rerender())
        ttk.Button(hdr, text="Fit", command=lambda: self.set_zoom("Fit")).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(hdr, text="1x", command=lambda: self.set_zoom("1x")).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(hdr, textvariable=self._panel_btn_txt, width=4, command=self.toggle_compact).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self._roi_btn_txt = tk.StringVar(value=app.T("roi_select"))
        self._btn_roi_select = ttk.Button(
            hdr, textvariable=self._roi_btn_txt, command=self.toggle_roi_select
        )
        self._btn_roi_select.pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(hdr, text="设为当前相机", command=self._on_focus).pack(side=tk.RIGHT)

        # 布局入口：任意预览窗可改排列，并指定排到哪块物理显示器
        lay = ttk.Frame(self.win)
        lay.pack(fill=tk.X, padx=6, pady=(2, 0))
        ttk.Label(lay, text="布局").pack(side=tk.LEFT)
        lay_mode = ttk.Combobox(
            lay,
            textvariable=app.cap_layout_mode_var,
            values=app._layout_mode_choices,
            state="readonly",
            width=5,
        )
        lay_mode.pack(side=tk.LEFT, padx=(4, 0))
        lay_mode.bind("<<ComboboxSelected>>", app.on_preview_layout_changed)
        self.lay_monitor = ttk.Combobox(
            lay,
            textvariable=app.cap_layout_monitor_var,
            values=list(app._layout_monitor_special) + list(app._monitor_choice_labels),
            state="readonly",
            width=16,
        )
        self.lay_monitor.pack(side=tk.LEFT, padx=(4, 0))
        self.lay_monitor.bind("<<ComboboxSelected>>", app.on_preview_layout_changed)
        self.lay_monitor.bind("<Button-1>", lambda _e: app.refresh_monitor_list())
        lay_region = ttk.Combobox(
            lay,
            textvariable=app.cap_layout_region_var,
            values=app._layout_region_choices,
            state="readonly",
            width=4,
        )
        lay_region.pack(side=tk.LEFT, padx=(4, 0))
        lay_region.bind("<<ComboboxSelected>>", app.on_preview_layout_changed)
        ttk.Button(lay, text="重排", command=app.tile_multi_preview_windows).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(
            lay, text="排到本屏", command=self._tile_on_this_monitor
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(lay, text="上下", width=3, command=lambda: self._apply_layout("上下")).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(lay, text="左右", width=3, command=lambda: self._apply_layout("左右")).pack(
            side=tk.LEFT, padx=(2, 0)
        )
        ttk.Button(lay, text="网格", width=3, command=lambda: self._apply_layout("网格")).pack(
            side=tk.LEFT, padx=(2, 0)
        )

        # 画布放在中间并 expand；底部灰度/GPS 可折叠，避免上下平铺时面板占满高度
        self._wrap = ttk.Frame(self.win)
        self._wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.canvas = tk.Canvas(self._wrap, bg="#202020", highlightthickness=0)
        self._yscroll = ttk.Scrollbar(self._wrap, orient=tk.VERTICAL, command=self.canvas.yview)
        self._xscroll = ttk.Scrollbar(self._wrap, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self._xscroll.set, yscrollcommand=self._yscroll.set)
        self._yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._xscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._image_id = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._build_layout_menu()

        self._gray_frame = ttk.LabelFrame(self.win, text=app.T("gray_frame"), padding=4)
        self._gray_frame.columnconfigure(1, weight=1)

        def _row(r, label, var, lbl_var, from_, to, fmt):
            ttk.Label(self._gray_frame, text=label).grid(row=r, column=0, sticky="w")
            ttk.Scale(
                self._gray_frame,
                from_=from_,
                to=to,
                variable=var,
                orient=tk.HORIZONTAL,
                command=lambda _v, v=var, lv=lbl_var, f=fmt: self._on_gray_slide(v, lv, f),
            ).grid(row=r, column=1, sticky="ew", padx=4)
            ttk.Label(self._gray_frame, textvariable=lbl_var, width=6).grid(row=r, column=2, sticky="e")

        _row(0, "Bright", self.brightness, self.brightness_lbl, -128, 128, "{:.0f}")
        _row(1, "Contrast", self.contrast, self.contrast_lbl, 0.2, 4.0, "{:.2f}")
        _row(2, "Black", self.black, self.black_lbl, 0, 254, "{:.0f}")
        _row(3, "White", self.white, self.white_lbl, 1, 255, "{:.0f}")
        btns = ttk.Frame(self._gray_frame)
        btns.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Button(btns, text="Auto", command=self.auto_stretch).pack(side=tk.LEFT)
        ttk.Button(btns, text="Reset", command=self.reset_gray).pack(side=tk.LEFT, padx=(6, 0))
        self._copy_gray_btn = ttk.Button(btns, text=app.T("preview_copy_gray"), command=self.copy_gray_to_all)
        self._copy_gray_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._stats_lbl = ttk.Label(
            self.win, textvariable=self.stats_var, wraplength=520, justify=tk.LEFT
        )
        self._gps_box = ttk.LabelFrame(self.win, text=f"帧序号 · {self.short}", padding=4)
        gps_bar = ttk.Frame(self._gps_box)
        gps_bar.pack(fill=tk.X, pady=(0, 2))
        self.v_gps_detail = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            gps_bar, text=app.T("gps_preview_detail"), variable=self.v_gps_detail,
            command=self._on_gps_detail_toggled,
        ).pack(side=tk.LEFT)
        gps_wrap = ttk.Frame(self._gps_box)
        gps_wrap.pack(fill=tk.BOTH, expand=True)
        self.gps_text = tk.Text(gps_wrap, height=4, wrap=tk.NONE, font=("Consolas", 9))
        gys = ttk.Scrollbar(gps_wrap, orient=tk.VERTICAL, command=self.gps_text.yview)
        self.gps_text.configure(yscrollcommand=gys.set)
        gys.pack(side=tk.RIGHT, fill=tk.Y)
        self.gps_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._set_gps("帧序号 off / waiting for frame.")
        self._pack_detail_panels(compact=False)

        self._photo: tk.PhotoImage | None = None
        self._pending = None
        self._pending_stats = ""
        self._pending_gps = "帧序号 off / waiting for frame."
        self._dirty = False          # 有新原图待按当前滑条渲染
        self._slider_hold = False    # 拖动灰度时暂停 live 覆盖显示
        # RLock：queue_frame 已持锁时仍可能 reset_gps_seq_stats（GPS off），不可用普通 Lock
        self._lock = threading.RLock()
        self._ui_last = 0.0
        self._gps_ui_last = 0.0
        self._last_wh = (0, 0)
        self._last_frame = None
        self._roi_select_mode = False
        self._roi_drag_start: tuple[float, float] | None = None
        self._roi_rect_id = None
        # canvas 显示图 → 当前帧像素 映射（flush 时更新）
        self._view_map = {
            "img_x": 0, "img_y": 0, "disp_w": 1, "disp_h": 1,
            "frame_w": 1, "frame_h": 1,
        }
        self._frame_wh = (0, 0)  # 最近一帧的传感器/当前 ROI 尺寸
        self.state = "IDLE"
        self.frames = 0
        self.misses = 0
        self._fps_times: list[float] = []
        self._closed = False
        self._gray_after: str | None = None
        # 采集线程禁止碰 Tk；主线程改滑条/GPS 时刷新这份缓存
        self._view_cache = {
            "brightness": float(self.brightness.get()),
            "contrast": float(self.contrast.get()),
            "black": float(self.black.get()),
            "white": float(self.white.get()),
            "zoom": "Fit",
            "fill": False,
            "canvas_w": self._canvas_w,
            "canvas_h": self._canvas_h,
            "gps": bool(app._gps_parse_enabled),
        }
        self.win.protocol("WM_DELETE_WINDOW", self._on_user_close)
        self.win.bind("<FocusIn>", self._on_focus)
        self.refresh_view_cache()

    # ---- GPS 帧序号（线程安全：采集线程 note，主线程 flush 刷 UI）----
    def _reset_gps_seq_stats_unlocked(self) -> None:
        self.last_seq = None
        self.first_seq = None
        self.seq_count = 0
        self.seq_gaps = 0
        self.seq_dups = 0
        self.gps_short = ""
        self.seq_list.clear()
        self._seq_list_dirty = False
        self._seq_stats_dirty = False

    def reset_gps_seq_stats(self) -> None:
        with self._lock:
            self._reset_gps_seq_stats_unlocked()

    def clear_seq_list(self) -> None:
        with self._lock:
            self.seq_list.clear()
            self._seq_list_dirty = False
            self._seq_stats_dirty = False

    def reset_seq_list_keep_slots(self) -> None:
        """清掉 GPS 序号列表与累计，保留左侧 [01]..[N] 空位；同步清掉 gps_short 防残留。"""
        with self._lock:
            self.seq_list.clear()
            self.last_seq = None
            self.first_seq = None
            self.seq_count = 0
            self.seq_gaps = 0
            self.seq_dups = 0
            self.gps_short = ""
            self._seq_list_dirty = True
            self._seq_stats_dirty = True

    def _seq_list_mode(self) -> bool:
        # 必须读普通 bool：采图线程也会走到这里，禁止 Tk Var.get()
        return bool(getattr(self.app, "_gps_seq_list_mode", False))

    def _seq_list_max(self) -> int:
        n = int(getattr(self.app, "_gps_seq_list_max", 200) or 200)
        return n if n > 0 else 200

    def note_gps_seq(self, seq: int, *, valid: bool = True) -> None:
        """记下 GPS 帧序号；列表模式则追加到 seq_list（UI 由主线程 flush 刷新）。

        列表模式（目标 N 帧验收）：
          - 满 N 后**冻结**，不再滑动覆盖（避免把 burst 头挤掉、把自由跑混进窗）
          - 相同 seq（Δ=0）记 dup、不入列（避免 SDK/demo 重复交帧显示成 5,5 / 10,10）
          - Δ>1 记 gap，仍入列（洞会如实出现）
        点「复位序号」后重新从空槽收满 N。
        """
        seq = int(seq) & 0xFFFFFFFF
        # 列表模式：有解析到的 seq 就考虑入列；满 N 冻结
        if self._seq_list_mode():
            max_n = self._seq_list_max()
            if len(self.seq_list) >= max_n:
                # 已满：只更新 last_seq 供状态栏，不再改列表
                self.last_seq = seq
                self._seq_stats_dirty = True
                return
            if self.last_seq is not None:
                delta = (seq - self.last_seq) & 0xFFFFFFFF
                if delta >= 0x10000:
                    return
                if delta == 0:
                    self.seq_dups += 1
                    self._seq_stats_dirty = True
                    return
                if delta > 1:
                    self.seq_gaps += delta - 1
            if self.first_seq is None:
                self.first_seq = seq
            self.last_seq = seq
            self.seq_count += 1
            self.seq_list.append(seq)
            self._seq_list_dirty = True
            self._seq_stats_dirty = True
            return

        if not valid:
            return
        if self.last_seq is not None:
            delta = (seq - self.last_seq) & 0xFFFFFFFF
            if delta >= 0x10000:
                return
            if delta == 0:
                self.seq_dups += 1
            elif delta > 1:
                self.seq_gaps += delta - 1
        if self.first_seq is None:
            self.first_seq = seq
        self.last_seq = seq
        self.seq_count += 1
        self._seq_stats_dirty = True

    def missing_burst_seqs(self) -> list[int]:
        """同步期望范围内未收到的帧序号（burststart+1 .. burstend-1）。"""
        try:
            return list(self.app.missing_burst_frame_seqs(
                list(self.seq_list),
                frozen=len(self.seq_list) >= self._seq_list_max(),
            ))
        except Exception:
            return []

    def lost_seqs_label(self) -> str:
        missing = self.missing_burst_seqs()
        if not missing:
            return ""
        return "丢失:" + ",".join(str(x) for x in missing)

    def gps_seq_summary(self) -> str:
        if self.last_seq is None:
            return "seq=?"
        parts = [f"seq={self.last_seq}", f"n={self.seq_count}"]
        if self.first_seq is not None and self.first_seq != self.last_seq:
            parts.append(f"first={self.first_seq}")
        if self.seq_gaps:
            parts.append(f"gap={self.seq_gaps}⚠疑似丢帧")
        lost = self.lost_seqs_label()
        if lost:
            parts.append(f"⚠{lost}")
        if self.seq_dups:
            parts.append(f"dup={self.seq_dups}")
        if self._seq_list_mode():
            max_n = self._seq_list_max()
            filled = len(self.seq_list)
            parts.append(f"list={filled}/{max_n}")
            if filled >= max_n:
                parts.append("frozen")
        return " ".join(parts)

    def _rebuild_gps_short(self, gps_text: str | None = None) -> None:
        """根据 last_seq / seq_list 重建主窗状态栏用的 gps_short。"""
        if self.last_seq is not None:
            self.gps_short = self.gps_seq_summary()
            compact = self.burst_seq_list_compact()
            if compact:
                self.gps_short = f"{self.gps_short} | {compact}"
            return
        if self._seq_list_mode():
            # 复位后列表空、尚无新帧：不要沿用旧 compact，也不要用过期 gps_text 冒充
            self.gps_short = "(waiting)"
            return
        if gps_text:
            line = gps_text.strip().splitlines()[0] if gps_text.strip() else ""
            self.gps_short = line[:80]
        else:
            self.gps_short = ""

    def gps_status_line(self) -> str:
        """主窗监视区「GPS 帧序号」一行：始终按当前 seq_list/last_seq 现场拼，避免 stale gps_short。"""
        if self.last_seq is None:
            return "(waiting)"
        line = self.gps_seq_summary()
        compact = self.burst_seq_list_compact()
        if compact:
            return f"{line} | {compact}"
        return line

    def burst_seq_list_text(self) -> str:
        """帧序号列表：左侧固定 [01]..[N]，右侧为 GPS 序号（空位显示 -）；跳号标 ←缺k。"""
        max_n = self._seq_list_max()
        filled = len(self.seq_list)
        tag = "冻结" if filled >= max_n else "收集中"
        head = f"帧序号列表  n={filled}/{max_n}  ({tag})"
        if self.seq_gaps:
            head += f"  ⚠疑似丢帧 gap={self.seq_gaps}"
        lost = self.lost_seqs_label()
        if lost:
            head += f"  ⚠{lost}"
        if self.seq_dups:
            head += f"  dup={self.seq_dups}"
        lines = [head]
        # 丢失清单单独一行，方便对照 burststart+1..burstend-1
        if lost:
            lines.append(f"  ⚠ {lost}  (期望 {self.app.burst_expected_range_label()})")
        for i in range(1, max_n + 1):
            if i <= filled:
                seq = self.seq_list[i - 1]
                mark = ""
                if i >= 2:
                    prev = self.seq_list[i - 2]
                    delta = (int(seq) - int(prev)) & 0xFFFFFFFF
                    if 1 < delta < 0x10000:
                        # 列出中间缺失的绝对序号（便于对照）
                        miss_abs = [
                            (int(prev) + k) & 0xFFFFFFFF
                            for k in range(1, delta)
                        ]
                        if len(miss_abs) <= 12:
                            mark = f"  ←缺{delta - 1}:{','.join(str(x) for x in miss_abs)}"
                        else:
                            mark = (
                                f"  ←缺{delta - 1}:"
                                f"{miss_abs[0]}..{miss_abs[-1]}"
                            )
                lines.append(f"  [{i:02d}] {seq}{mark}")
            else:
                lines.append(f"  [{i:02d}] -")
        return "\n".join(lines)

    def burst_seq_list_compact(self) -> str:
        if not self._seq_list_mode() or not self.seq_list:
            return ""
        body = ",".join(str(s) for s in self.seq_list)
        lost = self.lost_seqs_label()
        if lost:
            return f"{body} | ⚠{lost}"
        return body

    def _pack_detail_panels(self, *, compact: bool) -> None:
        """显示或隐藏灰度/统计/GPS，把高度留给画布。"""
        for w in (self._gray_frame, self._stats_lbl, self._gps_box):
            try:
                w.pack_forget()
            except Exception:
                pass
        # 精简时去掉画布外边距，避免窗口边缘露底色/桌面感
        try:
            self._wrap.pack_configure(padx=0 if compact else 4, pady=0 if compact else 4)
        except Exception:
            pass
        if compact:
            try:
                self._yscroll.pack_forget()
                self._xscroll.pack_forget()
            except Exception:
                pass
            self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._panel_btn_txt.set(self.app.T("preview_panel"))
        else:
            try:
                self._yscroll.pack(side=tk.RIGHT, fill=tk.Y)
                self._xscroll.pack(side=tk.BOTTOM, fill=tk.X)
            except Exception:
                pass
            self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._gray_frame.pack(fill=tk.X, padx=6, pady=(0, 4))
            self._stats_lbl.pack(anchor=tk.W, padx=6)
            self._gps_box.pack(fill=tk.BOTH, expand=False, padx=6, pady=(4, 6))
            self._panel_btn_txt.set(self.app.T("preview_compact"))

    def apply_i18n(self):
        """语言切换时刷新本预览窗的可译文本（标题/灰度框/面板按钮）。"""
        try:
            self.win.title(
                f"预览 {self.short}" if self.app.lang == "zh" else f"Preview {self.short}")
        except Exception:
            pass
        try:
            self._gray_frame.configure(text=self.app.T("gray_frame"))
        except Exception:
            pass
        try:
            # 面板按钮文案随当前精简状态刷新
            self._panel_btn_txt.set(
                self.app.T("preview_panel") if self._compact else self.app.T("preview_compact"))
        except Exception:
            pass
        try:
            if getattr(self, "_copy_gray_btn", None) is not None:
                self._copy_gray_btn.configure(text=self.app.T("preview_copy_gray"))
        except Exception:
            pass

    def set_compact(self, compact: bool, *, rerender: bool = True) -> None:
        """精简模式：隐藏底部面板，Fit 改为铺满裁剪。"""
        compact = bool(compact)
        if compact == self._compact:
            return
        self._compact = compact
        self._pack_detail_panels(compact=compact)
        try:
            self.win.update_idletasks()
            self._canvas_w = max(64, int(self.canvas.winfo_width()))
            self._canvas_h = max(64, int(self.canvas.winfo_height()))
        except Exception:
            pass
        self.refresh_view_cache()
        if rerender and self._base is not None:
            self.rerender()

    def toggle_compact(self) -> None:
        self.set_compact(not self._compact)

    def _apply_layout(self, mode: str) -> None:
        """从本预览窗一键切换排列并重排全部窗口。"""
        self.app.cap_layout_mode_var.set(mode)
        self.app.tile_multi_preview_windows()

    def _tile_on_this_monitor(self) -> None:
        """把全部预览窗排到本预览窗当前所在的物理显示器。"""
        self.app.refresh_monitor_list()
        mon = self.app.monitor_for_toplevel(self.win)
        if mon is not None:
            self.app.cap_layout_monitor_var.set(mon.label)
        self.app.tile_multi_preview_windows()

    def _build_layout_menu(self) -> None:
        """右键菜单：任意预览窗都可重排到任意物理显示器。"""
        menu = tk.Menu(self.win, tearoff=0)

        def _popup(event):
            self.app.refresh_monitor_list()
            menu.delete(0, tk.END)
            menu.add_command(label=self.app.T("preview_retitle"), command=self.app.tile_multi_preview_windows)
            menu.add_command(label=self.app.T("preview_this_monitor"), command=self._tile_on_this_monitor)
            menu.add_command(
                label=self.app.T("preview_show_panel") if self._compact else self.app.T("preview_compact_menu"),
                command=self.toggle_compact,
            )
            menu.add_separator()
            for mode in self.app._layout_mode_choices:
                menu.add_command(
                    label=f"{self.app.T('preview_layout_prefix')}{self.app.layout_display(mode)}",
                    command=lambda m=mode: self._apply_layout(m),
                )
            menu.add_separator()
            for region in self.app._layout_region_choices:
                menu.add_command(
                    label=f"{self.app.T('preview_region_prefix')}{self.app.region_display(region)}",
                    command=lambda r=region: self._apply_region(r),
                )
            menu.add_separator()
            for label in self.app._monitor_choice_labels:
                menu.add_command(
                    label=f"{self.app.T('preview_monitor_prefix')}{label}",
                    command=lambda lb=label: self._apply_monitor(lb),
                )
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        self._layout_menu = menu
        for w in (self.win, self.canvas):
            w.bind("<Button-3>", _popup)
        self.win.bind("<Control-l>", lambda _e: self.app.tile_multi_preview_windows())
        self.win.bind("<Control-L>", lambda _e: self.app.tile_multi_preview_windows())
        self.win.bind("<Control-Shift-L>", lambda _e: self._tile_on_this_monitor())

    def _apply_region(self, region: str) -> None:
        self.app.cap_layout_region_var.set(region)
        self.app.tile_multi_preview_windows()

    def _apply_monitor(self, label: str) -> None:
        self.app.cap_layout_monitor_var.set(label)
        self.app.tile_multi_preview_windows()

    def toggle_roi_select(self) -> None:
        self._roi_select_mode = not bool(getattr(self, "_roi_select_mode", False))
        try:
            self._roi_btn_txt.set(
                self.app.T("roi_select_on") if self._roi_select_mode else self.app.T("roi_select")
            )
        except Exception:
            pass
        if not self._roi_select_mode:
            self._clear_roi_rubber()
        else:
            try:
                self.app.output_queue.put(self.app.T("roi_select_tip") + "\n")
            except Exception:
                pass
        self._on_focus()

    def _clear_roi_rubber(self) -> None:
        self._roi_drag_start = None
        if self._roi_rect_id is not None:
            try:
                self.canvas.delete(self._roi_rect_id)
            except Exception:
                pass
            self._roi_rect_id = None

    def _on_canvas_press(self, event):
        self._on_focus()
        if self._roi_select_mode:
            x = float(self.canvas.canvasx(event.x))
            y = float(self.canvas.canvasy(event.y))
            self._roi_drag_start = (x, y)
            self._clear_roi_rubber()
            self._roi_drag_start = (x, y)
            self._roi_rect_id = self.canvas.create_rectangle(
                x, y, x, y, outline="#00ff66", width=2, dash=(4, 2)
            )
            return
        self.canvas.scan_mark(event.x, event.y)

    def _on_canvas_drag(self, event):
        if self._roi_select_mode and self._roi_drag_start is not None:
            x0, y0 = self._roi_drag_start
            x1 = float(self.canvas.canvasx(event.x))
            y1 = float(self.canvas.canvasy(event.y))
            if self._roi_rect_id is not None:
                self.canvas.coords(self._roi_rect_id, x0, y0, x1, y1)
            return
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_canvas_release(self, event):
        if not self._roi_select_mode or self._roi_drag_start is None:
            return
        x0, y0 = self._roi_drag_start
        x1 = float(self.canvas.canvasx(event.x))
        y1 = float(self.canvas.canvasy(event.y))
        self._roi_drag_start = None
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        if (xb - xa) < 8 or (yb - ya) < 8:
            self._clear_roi_rubber()
            try:
                self.app.output_queue.put(self.app.T("roi_too_small") + "\n")
            except Exception:
                pass
            return
        mapped = self._map_canvas_rect_to_sensor(xa, ya, xb, yb)
        if mapped is None:
            self._clear_roi_rubber()
            return
        sx, sy, sw, sh = mapped
        # 保持橡皮筋，方便确认；退出框选模式以免误拖
        self._roi_select_mode = False
        try:
            self._roi_btn_txt.set(self.app.T("roi_select"))
        except Exception:
            pass
        self.app.apply_roi_selection(self.cam_name, sx, sy, sw, sh)

    def _map_canvas_rect_to_sensor(
        self, xa: float, ya: float, xb: float, yb: float
    ) -> tuple[int, int, int, int] | None:
        """把画布矩形映射到当前帧坐标系，再叠加已有 ROI 起点 → SDK 坐标。"""
        vm = dict(getattr(self, "_view_map", {}) or {})
        img_x = float(vm.get("img_x", 0))
        img_y = float(vm.get("img_y", 0))
        disp_w = max(1, int(vm.get("disp_w", 1) or 1))
        disp_h = max(1, int(vm.get("disp_h", 1) or 1))
        frame_w = max(1, int(vm.get("frame_w", 1) or 1))
        frame_h = max(1, int(vm.get("frame_h", 1) or 1))
        # 相对图像
        ix0 = max(0.0, min(float(disp_w), xa - img_x))
        iy0 = max(0.0, min(float(disp_h), ya - img_y))
        ix1 = max(0.0, min(float(disp_w), xb - img_x))
        iy1 = max(0.0, min(float(disp_h), yb - img_y))
        if ix1 - ix0 < 2 or iy1 - iy0 < 2:
            try:
                self.app.output_queue.put(self.app.T("roi_too_small") + "\n")
            except Exception:
                pass
            return None
        # 显示像素 → 当前帧像素
        fx0 = int(ix0 * frame_w / disp_w)
        fy0 = int(iy0 * frame_h / disp_h)
        fx1 = int(ix1 * frame_w / disp_w)
        fy1 = int(iy1 * frame_h / disp_h)
        if fx1 <= fx0:
            fx1 = fx0 + 2
        if fy1 <= fy0:
            fy1 = fy0 + 2
        # 若当前已是 ROI 子幅，叠加该机已存 ROI 起点
        ox, oy = self.app._current_roi_origin(self.cam_name)
        sx = ox + fx0
        sy = oy + fy0
        sw = fx1 - fx0
        sh = fy1 - fy0
        # QHY 分辨率常见偶对齐
        sx &= ~1
        sy &= ~1
        sw = max(2, sw & ~1)
        sh = max(2, sh & ~1)
        return sx, sy, sw, sh

    def _pan_mark(self, event):
        self.canvas.scan_mark(event.x, event.y)
        self._on_focus()

    def _pan_drag(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_canvas_configure(self, event=None):
        try:
            cw = int(event.width if event is not None else self.canvas.winfo_width())
            ch = int(event.height if event is not None else self.canvas.winfo_height())
        except Exception:
            return
        prev_w, prev_h = self._canvas_w, self._canvas_h
        if cw >= 64:
            self._canvas_w = cw
        if ch >= 64:
            self._canvas_h = ch
        # 仅画布尺寸真变化时重适配 Fit，避免 flush→scrollregion→Configure 死循环
        if (self._canvas_w, self._canvas_h) == (prev_w, prev_h):
            return
        self.refresh_view_cache()
        if self._base is not None and (self.zoom_var.get() or "Fit").strip().lower() == "fit":
            self.rerender()

    def _on_user_close(self):
        try:
            self.win.withdraw()
        except Exception:
            pass

    def _on_focus(self, _event=None):
        if not self._closed:
            self.app._on_preview_win_focus(self.cam_name)

    def _set_gps(self, text: str) -> None:
        try:
            self.gps_text.configure(state=tk.NORMAL)
            self.gps_text.delete("1.0", tk.END)
            self.gps_text.insert("1.0", text or "")
            self.gps_text.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _on_gps_detail_toggled(self) -> None:
        """切换短序号 / 完整 GPS 明细；立刻用最近一帧重刷框。"""
        try:
            detail = bool(self.v_gps_detail.get())
        except Exception:
            detail = False
        try:
            self.gps_text.configure(height=9 if detail else 4)
        except Exception:
            pass
        # 用缓存的最近 pending 文本不够（可能是 brief）；若有 last_frame 则重算
        fr = getattr(self, "_last_frame", None)
        gps_on = bool(getattr(self.app, "_gps_parse_enabled", False))
        if not gps_on:
            self._set_gps("帧序号 off.")
            return
        gps_obj = getattr(fr, "gps", None) if fr is not None else None
        if detail:
            self._set_gps(format_gps_report(gps_obj, wanted=True))
        else:
            self._set_gps(format_gps_seq_brief(gps_obj, wanted=True))


    def _on_gray_slide(self, var, lbl_var, fmt: str) -> None:
        try:
            lbl_var.set(fmt.format(float(var.get())))
        except Exception:
            pass
        self.refresh_view_cache()
        # 拖动期间挡住 live 抢刷新，松手/防抖后再按滑条重绘
        self._slider_hold = True
        if self._gray_after is not None:
            try:
                self.win.after_cancel(self._gray_after)
            except Exception:
                pass
        self._gray_after = self.win.after(80, self.rerender)

    def set_zoom(self, z: str) -> None:
        self.zoom_var.set(z)
        self.refresh_view_cache()
        self.rerender()

    def reset_gray(self) -> None:
        self.brightness.set(0.0)
        self.contrast.set(1.0)
        self.black.set(0.0)
        self.white.set(255.0)
        self.brightness_lbl.set("0")
        self.contrast_lbl.set("1.00")
        self.black_lbl.set("0")
        self.white_lbl.set("255")
        self.refresh_view_cache()
        self.rerender()

    def auto_stretch(self) -> None:
        if self._base is None:
            return
        _w, _h, gray = self._base
        try:
            lo, hi = gray8_percentile_levels(gray, low_pct=0.5, high_pct=99.5)
        except Exception:
            return
        self.black.set(float(lo))
        self.white.set(float(max(lo + 1, hi)))
        self.black_lbl.set(f"{lo:.0f}")
        self.white_lbl.set(f"{hi:.0f}")
        self.rerender()

    def copy_gray_to_all(self) -> None:
        self.app.copy_preview_gray_from(self.cam_name)

    def apply_gray_from(self, other: "CamPreviewWin") -> None:
        self.brightness.set(float(other.brightness.get()))
        self.contrast.set(float(other.contrast.get()))
        self.black.set(float(other.black.get()))
        self.white.set(float(other.white.get()))
        self.brightness_lbl.set(f"{float(self.brightness.get()):.0f}")
        self.contrast_lbl.set(f"{float(self.contrast.get()):.2f}")
        self.black_lbl.set(f"{float(self.black.get()):.0f}")
        self.white_lbl.set(f"{float(self.white.get()):.0f}")
        self.rerender()

    def refresh_view_cache(self) -> None:
        """仅主线程调用：把 Tk 滑条/GPS 快照成纯 dict，供采集线程无锁读取。"""
        zoom = (self.zoom_var.get() or "Fit").strip()
        fill = bool(self._compact) and zoom.lower() == "fit"
        self._view_cache = {
            "brightness": float(self.brightness.get()),
            "contrast": float(self.contrast.get()),
            "black": float(self.black.get()),
            "white": float(self.white.get()),
            "zoom": zoom,
            "fill": fill,
            "canvas_w": self._canvas_w if self._canvas_w >= 64 else 480,
            "canvas_h": self._canvas_h if self._canvas_h >= 64 else 360,
            "gps": bool(getattr(self.app, "_gps_parse_enabled", False)),
        }

    def view_dict(self) -> dict:
        # 采集线程只读缓存，禁止 cap_gps_var.get() / DoubleVar.get()（会与主线程 Text 刷新死锁）
        return dict(self._view_cache)

    def set_state(self, state: str, *, misses: int | None = None) -> None:
        self.state = state
        if misses is not None:
            self.misses = misses
        fps = 0.0
        if len(self._fps_times) >= 2:
            dt = self._fps_times[-1] - self._fps_times[0]
            if dt > 0:
                fps = (len(self._fps_times) - 1) / dt
        extra = f" miss={self.misses}" if state == "WAITING" and self.misses else ""
        gps_on = bool(getattr(self.app, "_gps_parse_enabled", False))
        if not gps_on:
            self.reset_gps_seq_stats()
        seq_part = ""
        if gps_on and self.last_seq is not None:
            seq_part = f" | {self.gps_seq_summary()}"
        self.status_var.set(
            f"{self.short} | {state} | #{self.frames} | {fps:.1f} fps{extra}{seq_part}"
        )
        try:
            title = f"预览 {self.short} [{state}]"
            if gps_on and self.last_seq is not None:
                title += f" seq={self.last_seq} n={self.seq_count}"
            self.win.title(title)
        except Exception:
            pass

    def queue_frame(self, frame, *, interval_s: float) -> None:
        """更新原图缓存；显示灰度始终在 flush/rerender 里按当前滑条计算。

        旧逻辑在采集线程里就 bake 进 gray，滑条 rerender 又会被下一帧旧参数盖掉，
        表现为「灰度调节无效」。
        """
        now = time.perf_counter()
        view = self.view_dict()
        try:
            bundle = self.app._prepare_preview_base(frame, view=view)
        except Exception as exc:
            try:
                self.app.output_queue.put(f"ERROR preview prepare {self.short}: {exc}\n")
            except Exception:
                pass
            return
        if bundle is None:
            return
        dw, dh, gray, stats, _src, gps_text, _dw2, _dh2, _disp, _zl = bundle
        gps_on = bool(view.get("gps"))
        gps_obj = getattr(frame, "gps", None) if gps_on else None
        try:
            self._frame_wh = (
                int(getattr(frame, "width", 0) or 0),
                int(getattr(frame, "height", 0) or 0),
            )
        except Exception:
            pass
        with self._lock:
            self._base = (dw, dh, gray)
            self._last_frame = frame
            self._pending_stats = stats
            self._pending_gps = gps_text
            if gps_on and gps_obj is not None and not getattr(frame, "_burst_waste", False):
                # 列表模式：有解析到的 seq 就记；默认刷新仍走门禁。
                # burst 第0废帧不入验收列表 / 不计入序号统计。
                list_mode = self._seq_list_mode()
                self.note_gps_seq(
                    int(gps_obj.seq_number),
                    valid=True
                    if list_mode
                    else bool(getattr(frame, "_gps_seq_valid", True)),
                )
            elif not gps_on:
                # 已在 self._lock 内，勿再调会抢锁的 reset_gps_seq_stats()
                self._reset_gps_seq_stats_unlocked()
            self.frames += 1
            self._fps_times.append(now)
            self._fps_times = self._fps_times[-30:]
            self.state = "LIVE"
            self.misses = 0
            # 滑条正在调时只更新原图，不抢显示；否则按 UI 节流标记待刷新
            if self._slider_hold:
                return
            if self._pending is not None or (now - self._ui_last) < interval_s:
                self._dirty = True
                return
            self._dirty = True

    def _render_from_base(self, *, keep_gps: bool) -> tuple | None:
        """用当前滑条把 `_base` 渲成待显示像素。须在持锁外调用（可能较慢）。"""
        with self._lock:
            base = self._base
            stats = self._pending_stats
            gps_text = self._pending_gps if keep_gps else None
        if base is None:
            return None
        dw, dh, gray = base
        view = self.view_dict()
        black = int(round(float(view["black"])))
        white = int(round(float(view["white"])))
        if white <= black:
            white = min(255, black + 1)
        zoom, zoom_label = self.app._preview_zoom_factor(dw, dh, view)
        cw = int(view.get("canvas_w") or 0)
        ch = int(view.get("canvas_h") or 0)
        fill = bool(view.get("fill"))
        # 铺满时允许缩放到画布尺寸，不被默认 1200 上限卡住
        max_dim = (
            self.app._live_preview_display_max_dim
            if self.app._capture_live_running
            else self.app._preview_display_max_dim
        )
        if fill and cw >= 64 and ch >= 64:
            max_dim = max(max_dim, cw, ch, 1920)
        try:
            disp_w, disp_h, disp_gray = render_preview_gray8(
                gray,
                dw,
                dh,
                brightness=float(view["brightness"]),
                contrast=float(view["contrast"]),
                black=black,
                white=white,
                zoom=zoom,
                max_dim=max_dim,
            )
            if fill and cw >= 64 and ch >= 64:
                disp_w, disp_h, disp_gray = crop_gray8_center(
                    disp_gray, disp_w, disp_h, cw, ch
                )
                zoom_label = f"Fill({zoom:.2f}x)"
        except Exception as exc:
            self.app.output_queue.put(f"ERROR preview render {self.short}: {exc}\n")
            return None
        return disp_w, disp_h, disp_gray, stats, gps_text, zoom_label

    def rerender(self) -> None:
        """滑条/Zoom 变化：按当前参数立刻重绘（不重新取帧）。"""
        self._gray_after = None
        self._slider_hold = False
        self.refresh_view_cache()
        pending = self._render_from_base(keep_gps=False)
        if pending is None:
            return
        with self._lock:
            self._pending = pending
            self._dirty = False
        self.flush()

    def _flush_gps_status_ui(self, gps_text: str | None) -> None:
        """刷新预览窗 GPS 明细框 + 主窗用的 gps_short（可无新图时也调用）。"""
        gps_on = bool(getattr(self.app, "_gps_parse_enabled", False))
        now_ui = time.perf_counter()
        if not gps_on:
            if now_ui - self._gps_ui_last >= 0.2:
                self._gps_ui_last = now_ui
                self._set_gps("帧序号 off.")
            self.reset_gps_seq_stats()
            return
        # 预览窗 GPS 框只跟当前帧明细，按 0.5s 节流；不要因 seq_list 滑动每帧重写 Text（会卡死 UI）
        refresh_gps_box = gps_text is not None and (now_ui - self._gps_ui_last >= 0.5)
        refresh_stats = (
            self._seq_stats_dirty
            or self._seq_list_dirty
            or refresh_gps_box
        )
        if refresh_gps_box:
            self._gps_ui_last = now_ui
            show = gps_text or ""
            try:
                if bool(self.v_gps_detail.get()):
                    gps_obj = getattr(self._last_frame, "gps", None) if self._last_frame is not None else None
                    show = format_gps_report(gps_obj, wanted=True)
                    if gps_obj is None:
                        raw = getattr(self._last_frame, "data", b"") if self._last_frame is not None else b""
                        if raw:
                            show += "\nraw[0:48] " + bytes(raw[:48]).hex()
            except Exception:
                pass
            self._set_gps(show)
        if refresh_stats:
            self._seq_list_dirty = False
            self._seq_stats_dirty = False
            self._rebuild_gps_short(gps_text)

    def flush(self) -> None:
        with self._lock:
            pending = self._pending
            dirty = self._dirty
            self._pending = None
            hold = self._slider_hold
            seq_ui_dirty = self._seq_list_dirty or self._seq_stats_dirty
        # 有新原图且未在拖滑条时，用最新滑条参数渲染
        if pending is None and dirty and not hold:
            pending = self._render_from_base(keep_gps=True)
            with self._lock:
                self._dirty = False
        if pending is None:
            # 无新图时仍刷序号列表（收集中/[冻结] 状态）
            if seq_ui_dirty:
                try:
                    with self._lock:
                        gps_text = self._pending_gps
                    self._flush_gps_status_ui(gps_text)
                except Exception:
                    pass
            self.set_state(self.state, misses=self.misses)
            return
        dw, dh, gray, stats, gps_text, zoom_label = pending
        try:
            img = gray8_to_tk_photo(gray, dw, dh)
            self._photo = img
            self.canvas.itemconfigure(self._image_id, image=img)
            # Fit contain：居中；Fill/精简铺满：已裁到画布尺寸，贴 (0,0)
            fit = (self.zoom_var.get() or "Fit").strip().lower() == "fit"
            fill = bool(self._compact) and fit
            if fit and not fill:
                try:
                    cw = max(1, int(self.canvas.winfo_width()))
                    ch = max(1, int(self.canvas.winfo_height()))
                except Exception:
                    cw, ch = self._canvas_w, self._canvas_h
                x = max(0, (cw - dw) // 2)
                y = max(0, (ch - dh) // 2)
                self.canvas.coords(self._image_id, x, y)
                self.canvas.configure(scrollregion=(0, 0, max(cw, dw), max(ch, dh)))
            else:
                self.canvas.coords(self._image_id, 0, 0)
                if (dw, dh) != self._last_wh:
                    self.canvas.configure(scrollregion=(0, 0, dw, dh))
            self._last_wh = (dw, dh)
            try:
                coords = self.canvas.coords(self._image_id)
                img_x = float(coords[0]) if coords else 0.0
                img_y = float(coords[1]) if coords and len(coords) > 1 else 0.0
            except Exception:
                img_x = img_y = 0.0
            fw, fh = self._frame_wh if getattr(self, "_frame_wh", (0, 0))[0] else (dw, dh)
            if fw <= 0 or fh <= 0:
                fw, fh = dw, dh
            self._view_map = {
                "img_x": img_x, "img_y": img_y,
                "disp_w": int(dw), "disp_h": int(dh),
                "frame_w": int(fw), "frame_h": int(fh),
            }
            self._ui_last = time.perf_counter()
            if stats:
                zl = f" | zoom={zoom_label}" if zoom_label else ""
                self.stats_var.set(f"{stats}{zl}" if "zoom=" not in (stats or "") else stats)
            if gps_text is not None:
                # 本框只显示「当前帧」GPS 明细；burst 序号列表在主窗监视区
                self._flush_gps_status_ui(gps_text)
            self.set_state("LIVE" if self.app._capture_live_running else "FRAME")
        except Exception as exc:
            self.app.output_queue.put(f"ERROR preview win {self.short}: {exc}\n")

    def show(self) -> None:
        try:
            self.win.deiconify()
            self.win.lift()
        except Exception:
            pass

    def destroy(self) -> None:
        self._closed = True
        try:
            self.win.destroy()
        except Exception:
            pass

    # ---- 兼容旧调用 ----
    def show_frame(self, frame) -> None:
        # 旧的「单帧直接画」路径：转成 queue_frame，主线程 _drain_output 会 flush 出来。
        self.app.root.after(0, lambda: self.queue_frame(frame, interval_s=0.0))


# ============================================================================
# Main application
# ============================================================================
class CaptureApp:
    def __init__(self, root: tk.Tk, project_root: Path):
        self.root = root
        self.project_root = Path(project_root)
        self.output_queue: "queue.Queue[str]" = queue.Queue()

        # --- i18n: 默认英文，可切中文。语言值保存在 self.lang ('en' | 'zh') ---
        self.lang = "en"
        self._i18n_widgets: list = []  # (widget_or_combobox, key) 待重译
        self._i18n_headings: list = []  # (column_id, key) 表格表头
        self._i18n_tabtext: list = []   # (frame, key) 笔记本页签
        # (combo, var, display_to_const, const_to_display)：角色/传输等逻辑值下拉的翻译
        self._i18n_combo_display: list = []

        # --- camera list / role assignment ---
        self.cameras: list[CamRow] = []
        self._sel_idx = tk.IntVar(value=-1)

        # --- shared capture settings (applied to selected / all connected cams) ---
        self.v_readmode = tk.StringVar(value="0")
        self.v_bits = tk.StringVar(value="16")
        self.v_exptime = tk.StringVar(value="100000")
        self.v_gain = tk.StringVar(value="")
        self.v_offset = tk.StringVar(value="")
        self.v_traffic = tk.StringVar(value="")
        self.v_speed = tk.StringVar(value="")
        self.v_bin = tk.StringVar(value="1x1")
        self.v_roi_x = tk.StringVar(value="0")
        self.v_roi_y = tk.StringVar(value="0")
        self.v_roi_w = tk.StringVar(value="")
        self.v_roi_h = tk.StringVar(value="")
        self.v_ddr = tk.StringVar(value="default")
        self.v_res_info = tk.StringVar(value="")

        # GPS params
        self.v_gps_on = tk.BooleanVar(value=False)
        self.v_gps_slave = tk.BooleanVar(value=False)  # 由主从角色自动联动（高级区隐藏，一键同步仍用）
        self.v_gps_led_cal = tk.BooleanVar(value=False)
        self.v_gps_advanced = tk.BooleanVar(value=False)  # 默认折叠校灯/POS/UTC
        self.v_gps_pos_a = tk.StringVar(value="")  # 空=未配置；勿默认 0（LedCal+0 会塌曝光窗）
        self.v_gps_pos_b = tk.StringVar(value="")
        self.v_gps_pos_width = tk.StringVar(value="40")
        self.v_gps_scope = tk.StringVar(value="current")  # current | all | masters | slaves

        # --- 主从同步寄存器参数（区分 USB / PCIe）；默认值 = host/fpga_soft_modes.json 里各一键模式 ---
        self.v_usb_patch = tk.StringVar(value="32001")    # USB burst 补包（41-44 大端）= 0x00007D01
        self.v_pcie_patch = tk.StringVar(value="8192")    # PCIe/光纤 burst 补包 = 0x00002000（fpga_soft_modes.json 实际写入值）
        self.v_burst_start = tk.StringVar(value="1")     # burst 开始 reg50（默认 1；有效帧 [start,end]）
        self.v_window = tk.StringVar(value="11")          # burst 结束 51/52（一组帧数-1；0..0xFFFF=连续）
        self.v_min_pulse = tk.StringVar(value="10")       # 从机 148 最小脉宽 (×40ns)
        self.v_master_39 = tk.StringVar(value="17")       # 主机 39 = 0x11（六针 GPS + SMA 输出）
        self.v_slave_39 = tk.StringVar(value="2")         # 从机 39 = 2（等 GPIO 电平）
        self.v_master_58 = tk.StringVar(value="0")        # 主机 58（不等 GPIO）
        self.v_slave_58 = tk.StringVar(value="8")         # 从机 58 = 0x08（TrigMode[3] 硬件触发输入）

        # --- transport / sync state ---
        self.sync_status_var = tk.StringVar(value="")

        # --- 多预览窗自动布局：排列方式 + 目标物理显示器 + 屏内区域 ---
        self.cap_layout_mode_var = tk.StringVar(value="自动")
        self.cap_layout_region_var = tk.StringVar(value="整屏")
        self.cap_layout_monitor_var = tk.StringVar(value="跟随主窗")
        self._layout_mode_choices = ("自动", "上下", "左右", "网格")
        self._layout_region_choices = ("整屏", "右侧")
        self._layout_monitor_special = ("跟随主窗", "跟随焦点预览窗")
        self._monitors: list[MonitorInfo] = []
        self._monitor_choice_labels: list[str] = []
        self.cap_multi_var = tk.BooleanVar(value=True)  # 池里所有相机同时采集
        self.cap_multi_status_var = tk.StringVar(value="")
        self.cap_stats_var = tk.StringVar(value="No frame yet.")
        self.cap_frame_var = tk.StringVar(value="Frame: -")
        self.cap_fps_var = tk.StringVar(value="FPS: -")

        # --- 预览窗默认灰度（新窗从这些取值；主窗自身不再画图）---
        self.v_bright = tk.DoubleVar(value=0.0)
        self.v_contrast = tk.DoubleVar(value=1.0)
        self.v_black = tk.DoubleVar(value=0.0)
        self.v_white = tk.DoubleVar(value=255.0)

        # --- GPS 面板 / 帧序号列表（普通 bool/int 供采集线程无锁读取）---
        self.cap_gps_panel_mode = tk.StringVar(value="refresh")  # refresh | seq_list
        self.cap_burst_n_var = tk.StringVar(value="10")
        self._gps_seq_list_mode = False
        self._gps_seq_list_max = 10
        self._gps_parse_enabled = False  # GPS on：主机解析；硬件经 apply_settings → SetGPSOn
        self._burst_waste_enabled = False  # Live 线程只读此 bool，禁止碰 Tk Var

        # --- 预览尺寸常量（与原始 GUI 一致）---
        self._preview_zoom_choices = ("Fit", "0.25x", "0.5x", "1x", "2x", "3x", "4x")
        self._preview_native_max_dim = 800
        self._preview_fit_max_dim = 640
        self._preview_display_max_dim = 1200
        self._live_preview_native_max_dim = 800
        self._live_preview_display_max_dim = 1200
        self._preview_ui_interval_s = 0.16

        # --- runtime state ---
        self._cap_pool: CaptureWorkerPool | None = None
        self._reg_api = None
        self._reg_ctxs: dict[str, object] = {}
        self._reg_lock = threading.RLock()
        self._cam_preview_wins: dict[str, CamPreviewWin] = {}
        self._multi_preview_active = False
        self._live_threads: dict[str, threading.Thread] = {}
        self._live_stops: dict[str, threading.Event] = {}
        self._primary_cam_name: str | None = None
        self._latest: dict[str, object] = {}
        self._latest_n: dict[str, int] = {}
        self._latest_lock = threading.Lock()
        self._capture_live_running = False
        self._capture_live_stopping = False
        self._capture_live_stop = threading.Event()
        self._multi_status_t = 0.0
        self._live_miss_limit = int(os.environ.get("FPGA_SOFT_LIVE_MISS_LIMIT", "20"))
        self._live_slice_s = max(0.2, min(float(os.environ.get("FPGA_SOFT_LIVE_SLICE_S", "0.75")), 2.0))

        # --- 图像存储 / 参数实时下发 状态 ---
        self.save_enabled = tk.BooleanVar(value=False)      # 是否保存每帧
        self.v_save_dir = tk.StringVar(value=str(_default_save_dir()))
        self.v_save_interval = tk.StringVar(value="1")
        self.v_save_subdir = tk.StringVar(value="")  # optional 2nd-level folder under save dir
        self._save_subdir_warned: str | None = None  # last subdir path user confirmed      # Live 每 N 帧存一张（1=每帧）
        self.auto_push = tk.BooleanVar(value=True)          # live/连接中改动参数即自动下发
        # 扫描完成后自动连接一台并把采集参数读回 GUI（默认开）
        self.refresh_before_capture = tk.BooleanVar(value=True)
        # 同步 burst：按 GPS seq 丢弃 idx < burst_start 的废帧（默认 start=1 → 丢第 0 帧）
        self.filter_burst_waste = tk.BooleanVar(value=True)
        self._burst_waste_no_gps_logged = False             # 本轮 Live 无 GPS 时只提示一次
        self.param_status_var = tk.StringVar(value="")      # 参数状态行
        self._push_lock = threading.Lock()                  # 防重入（同一时刻只跑一次下发）
        self._last_settings = None                          # 上一次下发到的 settings（用于 diff）
        self._frame_seq = 0                                 # 每相机帧序号（存文件名用）
        self._frame_seq_lock = threading.Lock()
        self._save_cnt: dict[str, int] = {}                # 每相机已存帧计数（Live 每 N 帧）
        # 无界写盘队列：有图就进 RAM，写盘线程慢慢落盘；用剩余内存估算「还能收几张」做预警，不因队列满丢帧。
        self._save_q: "queue.Queue[tuple]" = queue.Queue()  # unbounded
        self._save_q_bytes = 0
        self._save_q_last_frame_bytes = 0
        self._save_drop_n = 0  # 仅 MemoryError / 拷帧失败等异常
        self._save_drop_log_t = 0.0
        self._save_room_warn_t = 0.0
        self._save_enqueued = 0   # 本轮入队总数（进度分母）
        self._save_done = 0       # 成功落盘
        self._save_fail = 0       # 落盘失败
        self._save_stats_lock = threading.Lock()
        self._save_ui_pending = False
        self._live_wait_log_t: dict[str, float] = {}
        for _si in range(2):
            threading.Thread(
                target=self._save_worker_loop, daemon=True, name=f"save-writer-{_si}"
            ).start()
        self._param_edit_silent = False                     # 回读/程序写参数时为 True，防止触发自动下发回环
        self._param_cam_name: str | None = None             # 参数页当前对应的相机
        # 每台相机独立 ROI：{name: {"x","y","w","h"}}；w/h 为 None 表示全幅
        self._roi_by_cam: dict[str, dict] = {}
        self._suppress_cam_switch_readback = False  # 框选下发 ROI 时避免换机回读抢会话
        self._last_good_readmode: str = "0"                 # 读出模式下拉最近合法值
        self._closing_cams = False                          # 断开/关闭流程中，禁用拍摄按钮

        # --- 系统托盘状态 ---
        self._tray_icon = None
        self._tray_running = False

        self._build_ui()
        self._load_modes()
        self.refresh_monitor_list()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_tray()
        self.root.after(100, self._drain_output)

    # ------------------------------------------------------------------ i18n
    def T(self, key: str, *, fmt: bool = False) -> str:
        """按当前语言取译文；缺键退回英文，再退回 key 本身。"""
        d = I18N.get(key)
        if d is None:
            return key
        s = d.get(self.lang) or d.get("en") or key
        return s

    def toggle_lang(self):
        self.set_lang("zh" if self.lang == "en" else "en")

    def set_lang(self, lang: str):
        self.lang = lang if lang in ("en", "zh") else "en"
        # 切换按钮显示「另一种语言」：英文界面显示"中文"，中文界面显示"EN"
        try:
            if hasattr(self, "btn_lang"):
                self.btn_lang.configure(text="EN" if self.lang == "zh" else "中文")
        except Exception:
            pass
        self._retranslate_all()
        # 角色/传输等逻辑值仍是中文常量；切语言后重刷已显示的表格/列表
        self._refresh_cam_tree()
        self._refresh_param_cam_list()
        self._update_sync_targets()
        # 重刷选中相机摘要（角色/传输显示随语言）
        c = self._selected_cam()
        if c is not None:
            self._update_sel_label()

    def _register(self, widget, key: str):
        """登记一个可重译的 widget（Label/Button/Checkbutton/Radiobutton 的 text）。"""
        self._i18n_widgets.append((widget, key))

    def _retranslate_all(self):
        for w, key in self._i18n_widgets:
            try:
                w.configure(text=self.T(key))
            except Exception:
                pass
        for col, key in self._i18n_headings:
            try:
                self.cam_tree.heading(col, text=self.T(key))
            except Exception:
                pass
        for frame, key in self._i18n_tabtext:
            try:
                self.notebook.tab(frame, text=self.T(key))
            except Exception:
                pass
        self._retranslate_combo_display()
        # 动态文案按钮：按当前状态重设
        try:
            if hasattr(self, "btn_gps_panel_mode"):
                self._set_gps_panel_mode_text()
        except Exception:
            pass
        try:
            if hasattr(self, "btn_live"):
                if self._capture_live_running:
                    self.btn_live.configure(text="Stop live")
                elif self._capture_live_stopping:
                    self.btn_live.configure(text="Stopping...")
                else:
                    self.btn_live.configure(text="Live")
        except Exception:
            pass
        # 预览窗标题/灰度框/右键菜单等随语言刷新
        for win in self._cam_preview_wins.values():
            try:
                win.apply_i18n()
            except Exception:
                pass
        # 参数状态行是动态文案：按当前 live 状态用新语言重渲染
        try:
            if getattr(self, "param_status_var", None) is not None:
                self._set_param_status("ps_live" if self._capture_live_running else "ps_idle")
        except Exception:
            pass
        self._refresh_multi_status_bar(force=True)

    # 逻辑值（中文常量）→ 当前语言显示名
    def role_display(self, role: str) -> str:
        return {
            ROLE_MASTER: self.T("role_master"),
            ROLE_SLAVE: self.T("role_slave"),
            ROLE_STANDALONE: self.T("role_solo"),
        }.get(role, role)

    def transport_display(self, t: str) -> str:
        return {
            TRANSPORT_USB: self.T("tr_usb"),
            TRANSPORT_PCIE: self.T("tr_pcie"),
        }.get(t, t)

    # ---- 角色/传输下拉：显示层用当前语言，值层用中文常量 ----
    def _display_role_choices(self) -> list[str]:
        return [self.role_display(r) for r in ROLE_CHOICES]

    def _display_transport_choices(self) -> list[str]:
        return [self.transport_display(t) for t in TRANSPORT_CHOICES]

    def _role_display_to_const(self, display: str) -> str:
        for r in ROLE_CHOICES:
            if self.role_display(r) == display:
                return r
        return display

    def _const_to_role_display(self, const: str) -> str:
        return self.role_display(const)

    def _trans_display_to_const(self, display: str) -> str:
        for t in TRANSPORT_CHOICES:
            if self.transport_display(t) == display:
                return t
        return display

    def _const_to_trans_display(self, const: str) -> str:
        return self.transport_display(const)

    def layout_display(self, mode: str) -> str:
        return {
            "自动": self.T("layout_auto"), "上下": self.T("layout_v"),
            "左右": self.T("layout_h"), "网格": self.T("layout_grid"),
        }.get(mode, mode)

    def region_display(self, region: str) -> str:
        return {"整屏": self.T("region_full"), "右侧": self.T("region_right")}.get(region, region)

    def _retranslate_combo_display(self):
        for combo, var, to_const, to_display in self._i18n_combo_display:
            try:
                combo.configure(values=[to_display(c) for c in (
                    ROLE_CHOICES if to_const is self._role_display_to_const else TRANSPORT_CHOICES)])
                cur = var.get()
                const = to_const(cur)
                var.set(to_display(const))
            except Exception:
                pass

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.root.title(self.T("app_title"))
        self.root.geometry("1280x880")
        self.root.minsize(1000, 700)

        _ensure_cwd_qhyccd_ini()

        # top bar
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)
        lbl_root = ttk.Label(top, text=self.T("project_root"))
        lbl_root.pack(side=tk.LEFT)
        self._register(lbl_root, "project_root")
        ttk.Label(top, text=str(self.project_root)).pack(side=tk.LEFT, padx=(4, 16))
        self.btn_scan = ttk.Button(top, text=self.T("scan"), command=self.scan_cameras)
        self.btn_scan.pack(side=tk.LEFT)
        self._register(self.btn_scan, "scan")
        # Connect 不再放顶栏：Scan 后点 Live / Single 会自动 open；保留按钮对象便于 i18n / 偶发手动调用。
        self.btn_connect = ttk.Button(top, text=self.T("connect"), command=self.connect_selected)
        self._register(self.btn_connect, "connect")
        self.btn_disconnect = ttk.Button(top, text=self.T("disconnect"), command=self.disconnect_selected)
        self.btn_disconnect.pack(side=tk.LEFT, padx=(6, 0))
        self._register(self.btn_disconnect, "disconnect")
        lbl_layout = ttk.Label(top, text=self.T("layout"))
        lbl_layout.pack(side=tk.LEFT, padx=(16, 4))
        self._register(lbl_layout, "layout")
        self.btn_tile = ttk.Button(top, text=self.T("retile"), command=self.tile_previews)
        self.btn_tile.pack(side=tk.LEFT)
        self._register(self.btn_tile, "retile")
        # 语言切换按钮（默认英文）：EN ↔ 中文
        self.btn_lang = ttk.Button(top, text="中文", command=self.toggle_lang, width=6)
        self.btn_lang.pack(side=tk.LEFT, padx=(12, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        self.tab_cams = ttk.Frame(self.notebook, padding=6)
        self.tab_params = ttk.Frame(self.notebook, padding=6)
        self.tab_sync = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.tab_cams, text=self.T("tab_cams"))
        self.notebook.add(self.tab_params, text=self.T("tab_params"))
        self.notebook.add(self.tab_sync, text=self.T("tab_sync"))
        self._i18n_tabtext.append((self.tab_cams, "tab_cams"))
        self._i18n_tabtext.append((self.tab_params, "tab_params"))
        self._i18n_tabtext.append((self.tab_sync, "tab_sync"))

        self._build_cams_tab()
        self._build_params_tab()
        self._build_sync_tab()

        foot = ttk.Frame(self.root)
        foot.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 6))
        ttk.Separator(foot, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 4))
        ttk.Label(foot, text="powered by liusir", font=("Segoe UI", 8),
                  foreground="#8a8a8a").pack(side=tk.RIGHT)
        self.status_var = tk.StringVar(value="Ready.")
        self.log_text = tk.Text(self.root, height=8, wrap=tk.WORD,
                                font=("Consolas", 10), state=tk.DISABLED)
        self.log_text.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))

        # GPS on 联动：主机解析 + 刷新各预览窗缓存
        try:
            self.v_gps_on.trace_add("write", self._on_gps_on_changed)
        except Exception:
            pass
        # 参数改动 → 实时(live-safe)下发到已打开的目标相机
        self._bind_auto_push_traces()
        self._sync_live_guards()

    def _build_cams_tab(self):
        tip = ttk.Label(
            self.tab_cams,
            text=self.T("cams_hint"),
            wraplength=1080, foreground="#444", justify=tk.LEFT,
        )
        tip.pack(anchor=tk.W, pady=(0, 6))
        self._register(tip, "cams_hint")

        cols = ("name", "role", "transport", "state")
        self.cam_tree = ttk.Treeview(self.tab_cams, columns=cols, show="headings", height=16,
                                     selectmode="browse")
        for c, w, key in [("name", 320, "hdr_name"), ("role", 90, "hdr_role"),
                          ("transport", 120, "hdr_transport"), ("state", 160, "hdr_state")]:
            self.cam_tree.heading(c, text=self.T(key))
            self.cam_tree.column(c, width=w, anchor=tk.W)
            self._i18n_headings.append((c, key))
        self.cam_tree.pack(fill=tk.BOTH, expand=True)
        self.cam_tree.bind("<<TreeviewSelect>>", self._on_cam_selected)

        btns = ttk.Frame(self.tab_cams, padding=(0, 6))
        btns.pack(fill=tk.X)
        lbl_sel = ttk.Label(btns, text=self.T("sel_arrow"))
        lbl_sel.pack(side=tk.LEFT)
        self._register(lbl_sel, "sel_arrow")
        # 下拉显示用当前语言的角色/传输名；值仍存中文常量（逻辑用），回读时再翻译回常量。
        self._role_combo_var = tk.StringVar(value=self.role_display(ROLE_STANDALONE))
        self._role_combo = ttk.Combobox(btns, textvariable=self._role_combo_var,
                                        values=self._display_role_choices(),
                                        state="readonly", width=8)
        self._role_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_selected_role())
        self._role_combo.pack(side=tk.LEFT, padx=(6, 0))
        self._i18n_combo_display.append((self._role_combo, self._role_combo_var,
                                         self._role_display_to_const, self._const_to_role_display))
        lbl_tr = ttk.Label(btns, text=self.T("trans_arrow"))
        lbl_tr.pack(side=tk.LEFT)
        self._register(lbl_tr, "trans_arrow")
        self._trans_combo_var = tk.StringVar(value=self.transport_display(TRANSPORT_USB))
        self._trans_combo = ttk.Combobox(btns, textvariable=self._trans_combo_var,
                                         values=self._display_transport_choices(),
                                         state="readonly", width=12)
        self._trans_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_selected_transport())
        self._trans_combo.pack(side=tk.LEFT, padx=(6, 0))
        self._i18n_combo_display.append((self._trans_combo, self._trans_combo_var,
                                         self._trans_display_to_const, self._const_to_trans_display))
        for key, role, pad in [("set_master", ROLE_MASTER, (10, 4)), ("set_slave", ROLE_SLAVE, (4, 0)),
                               ("set_solo", ROLE_STANDALONE, (4, 0))]:
            b = ttk.Button(btns, text=self.T(key), command=lambda r=role: self._set_selected_role(r))
            b.pack(side=tk.LEFT, padx=pad)
            self._register(b, key)
        b = ttk.Button(btns, text=self.T("set_rest_slaves"), command=self._set_rest_slaves)
        b.pack(side=tk.LEFT, padx=(16, 0))
        self._register(b, "set_rest_slaves")
        b = ttk.Button(btns, text=self.T("clear_roles"), command=self._clear_roles)
        b.pack(side=tk.LEFT, padx=16)
        self._register(b, "clear_roles")

    def _build_params_tab(self):
        outer = ttk.Panedwindow(self.tab_params, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True)

        # column 1: 拍摄控制 (单帧 / 连续) + 当前分配。默认压窄，把空间让给中间设置区。
        col_left = ttk.Frame(outer, padding=4, width=260)
        outer.add(col_left, weight=1)
        self._build_capture_control(col_left)

        # column 2: scrollable parameter sections (fully visible even in a small window)
        # 中间设置区加宽：左侧拍摄控制 / 右侧日志都压小，这里给最大权重，
        # 保证所有设置按钮 + 参数输入框默认就完整显示出来。
        col_mid = ttk.Frame(outer, padding=4)
        outer.add(col_mid, weight=6)
        canvas = tk.Canvas(col_mid, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(col_mid, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        content = ttk.Frame(canvas, padding=4)
        content_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_canvas_resize(event):
            if str(canvas.itemcget(content_id, "width")) != str(event.width):
                canvas.itemconfigure(content_id, width=event.width)

        def _on_content_resize(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _wheel(event):
            # Only scroll this column when the pointer is over it,
            # so the 当前分配 list and logs keep their own scroll.
            top = canvas.winfo_containing(event.x_root, event.y_root)
            while top is not None and top is not self.root:
                if top in (canvas, content):
                    canvas.yview_scroll(int(-event.delta / 120), "units")
                    break
                top = top.master
        canvas.bind("<Configure>", _on_canvas_resize)
        content.bind("<Configure>", _on_content_resize)
        canvas.bind_all("<MouseWheel>", _wheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        left = content  # build all left sections inside the scrollable content frame

        def _lbl(parent, key, **kw):
            w = ttk.Label(parent, text=self.T(key), **kw)
            self._register(w, key)
            return w

        # column 3: 采集日志
        col_right = ttk.Frame(outer, padding=4)
        outer.add(col_right, weight=2)
        self._build_capture_log(col_right)

        # 默认分栏位置：左侧拍摄控制约 180px、右侧日志约 360px，中间设置区占满其余。
        # ttk.Panedwindow 的 sash 位置需要在窗口可见（有实际宽度）时才能设置，
        # 因此监听页签切换，切到本页时按比例重设一次；并延迟一次兜底。
        self._params_pane = outer

        def _apply_params_sash(_e=None):
            try:
                # notebook.tab(..., "selected") returns 0/1, NOT the tab index string —
                # comparing to "1" made this always skip, so mid stayed crushed.
                if self.notebook.index(self.notebook.select()) != self.notebook.index(self.tab_params):
                    return
                w = max(outer.winfo_width(), 0)
                if w < 60:
                    return
                # Left needs ~260 for save path; keep right log ~280; mid gets the rest
                # so Capture Parameters / GPS / buttons are fully visible.
                left_w = min(300, max(250, int(w * 0.22)))
                right_w = min(320, max(260, int(w * 0.22)))
                mid_min = 420
                sash0 = left_w
                sash1 = max(sash0 + mid_min, w - right_w)
                if sash1 >= w - 40:
                    sash1 = max(sash0 + 200, w - 200)
                outer.sashpos(0, sash0)
                outer.sashpos(1, sash1)
            except Exception:
                pass

        self.notebook.bind("<<NotebookTabChanged>>", _apply_params_sash)
        self.root.after(120, _apply_params_sash)

        # left: camera + role/transport summary
        sel = ttk.LabelFrame(left, text=self.T("target_cam"), padding=8)
        self._register(sel, "target_cam")
        sel.pack(fill=tk.X)
        self._sel_cam_label_var = tk.StringVar(value=self.T("no_cam"))
        ttk.Label(sel, textvariable=self._sel_cam_label_var).pack(anchor=tk.W, fill=tk.X)
        self._sel_role_label = ttk.Label(sel, text="Role: -    Transport: -", foreground="#333")
        self._sel_role_label.pack(anchor=tk.W, pady=(4, 0))

        form = ttk.LabelFrame(left, text=self.T("capture_params"), padding=8)
        self._register(form, "capture_params")
        form.pack(fill=tk.X, pady=(8, 0))
        form.columnconfigure(1, weight=1)

        def row(r, key, widget):
            _lbl(form, key).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", padx=(6, 0), pady=2)

        self._readmode_combo = ttk.Combobox(
            form, textvariable=self.v_readmode, values=["0", "1"], state="readonly", width=14)
        self._readmode_combo.bind("<<ComboboxSelected>>", self._on_readmode_changed)
        try:
            self.v_readmode.trace_add("write", lambda *_: self._normalize_readmode_var())
        except Exception:
            pass
        row(0, "readmode", self._readmode_combo)
        row(1, "bits", ttk.Combobox(form, textvariable=self.v_bits, values=["8", "16"], state="readonly", width=8))
        row(2, "exposure", ttk.Entry(form, textvariable=self.v_exptime))
        row(3, "gain", ttk.Entry(form, textvariable=self.v_gain))
        row(4, "offset", ttk.Entry(form, textvariable=self.v_offset))
        row(5, "usb_traffic", ttk.Entry(form, textvariable=self.v_traffic))
        row(6, "speed", ttk.Entry(form, textvariable=self.v_speed))
        self._bin_combo = ttk.Combobox(
            form, textvariable=self.v_bin, values=["1x1", "2x2", "3x3", "4x4"], state="readonly", width=8)
        self._bin_combo.bind("<<ComboboxSelected>>", self._on_bin_changed)
        row(7, "bin", self._bin_combo)
        roi = ttk.Frame(form)
        for var, wdt in [(self.v_roi_x, 5), (self.v_roi_y, 5), (self.v_roi_w, 6), (self.v_roi_h, 6)]:
            ttk.Entry(roi, textvariable=var, width=wdt).pack(side=tk.LEFT, padx=(0, 4))
        row(8, "roi", roi)
        row(9, "ddr", ttk.Combobox(form, textvariable=self.v_ddr, values=["default", "on", "off"], state="readonly", width=8))
        ttk.Label(form, textvariable=self.v_res_info, wraplength=320, foreground="#444").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(2, 4))
        # 分辨率/ROI 操作按钮拆成两行，避免窄窗口把「加载配置」挤出可视区。
        rb = ttk.Frame(form)
        rb1 = ttk.Frame(rb)
        rb1.pack(fill=tk.X)
        rb2 = ttk.Frame(rb)
        rb2.pack(fill=tk.X, pady=(4, 0))
        b = ttk.Button(rb1, text=self.T("query_res"), command=self.run_query_resolution)
        b.pack(side=tk.LEFT)
        self._register(b, "query_res")
        # 手动 Read back 已弱化：Live / Single 起流后会自动 _run_readback；保留方法供调试调用。
        self.btn_readback = ttk.Button(rb1, text=self.T("readback"), command=lambda: self.read_params_from_cam())
        self._register(self.btn_readback, "readback")
        # 不 pack：默认不占设置区；调试时可 self.btn_readback.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_full_push = ttk.Button(rb1, text=self.T("full_push"), command=self._full_push_selected)
        self.btn_full_push.pack(side=tk.LEFT, padx=(6, 0))
        self._register(self.btn_full_push, "full_push")
        b = ttk.Button(rb1, text=self.T("fill_roi"), command=self.fill_roi_full)
        b.pack(side=tk.LEFT, padx=(6, 0))
        self._register(b, "fill_roi")
        b = ttk.Button(rb1, text=self.T("apply_roi"), command=self.apply_roi_now)
        b.pack(side=tk.LEFT, padx=(6, 0))
        self._register(b, "apply_roi")
        b = ttk.Button(rb1, text=self.T("restore_full"), command=self.restore_full_frame)
        b.pack(side=tk.LEFT, padx=(6, 0))
        self._register(b, "restore_full")
        b = ttk.Button(rb2, text=self.T("cfg_save"), command=self._save_param_config)
        b.pack(side=tk.LEFT)
        self._register(b, "cfg_save")
        b = ttk.Button(rb2, text=self.T("cfg_load"), command=self._load_param_config)
        b.pack(side=tk.LEFT, padx=(6, 0))
        self._register(b, "cfg_load")
        row(11, "resolution", rb)
        _lbl(form, "roi_per_cam", wraplength=320, foreground="#666", justify=tk.LEFT).grid(
            row=12, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))

        # 扫描后自动连接并读回：放在采集参数区旁（不再绑 Live/Capture）
        rb_scan = ttk.Checkbutton(form, text=self.T("refresh_before_capture"),
                                  variable=self.refresh_before_capture)
        rb_scan.grid(row=13, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._register(rb_scan, "refresh_before_capture")
        _lbl(form, "refresh_before_capture_note", wraplength=320, foreground="#666",
             justify=tk.LEFT).grid(row=14, column=0, columnspan=2, sticky="w", pady=(2, 0))

        gps = ttk.LabelFrame(left, text=self.T("gps_trigger"), padding=8)
        self._register(gps, "gps_trigger")
        gps.pack(fill=tk.X, pady=(8, 0))
        cb = ttk.Checkbutton(gps, text=self.T("gps_on"), variable=self.v_gps_on)
        cb.grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        self._register(cb, "gps_on")
        _lbl(gps, "gps_on_note", wraplength=320, foreground="#666", justify=tk.LEFT).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))

        gps_panel = ttk.LabelFrame(left, text=self.T("gps_panel"), padding=8)
        self._register(gps_panel, "gps_panel")
        gps_panel.pack(fill=tk.X, pady=(8, 0))
        gp1 = ttk.Frame(gps_panel)
        gp1.pack(fill=tk.X)
        self.btn_gps_panel_mode = ttk.Button(gp1, text=self.T("gps_mode_refresh"), command=self.toggle_gps_panel_mode)
        self.btn_gps_panel_mode.pack(side=tk.LEFT)
        self._register(self.btn_gps_panel_mode, "gps_mode_refresh")
        b = ttk.Button(gp1, text=self.T("gps_reset_seq"), command=self.reset_all_gps_seq_lists)
        b.pack(side=tk.LEFT, padx=(8, 0))
        self._register(b, "gps_reset_seq")
        gp2 = ttk.Frame(gps_panel)
        gp2.pack(fill=tk.X, pady=(6, 0))
        _lbl(gp2, "gps_list_max").pack(side=tk.LEFT)
        ttk.Entry(gp2, textvariable=self.cap_burst_n_var, width=8).pack(side=tk.LEFT, padx=(6, 0))
        _lbl(gps_panel, "gps_panel_note", wraplength=320, foreground="#666", justify=tk.LEFT).pack(
            anchor=tk.W, pady=(6, 0), fill=tk.X)

        # 高级 GPS：校灯 / POS / Slave / UTC —— 默认折叠，底层 API 与一键主从仍保留
        adv_toggle = ttk.Checkbutton(
            left, text=self.T("gps_advanced_toggle"), variable=self.v_gps_advanced,
            command=self._toggle_gps_advanced_ui)
        adv_toggle.pack(anchor=tk.W, pady=(8, 0))
        self._register(adv_toggle, "gps_advanced_toggle")

        self._gps_adv_host = ttk.Frame(left)
        # 默认不 pack；勾选后 _toggle_gps_advanced_ui 再 pack

        gps_adv = ttk.LabelFrame(self._gps_adv_host, text=self.T("gps_advanced_toggle"), padding=8)
        gps_adv.pack(fill=tk.X, pady=(4, 0))
        cb = ttk.Checkbutton(gps_adv, text=self.T("gps_slave"), variable=self.v_gps_slave)
        cb.grid(row=0, column=0, columnspan=2, sticky="w")
        self._register(cb, "gps_slave")
        cb = ttk.Checkbutton(gps_adv, text=self.T("gps_led_cal"), variable=self.v_gps_led_cal)
        cb.grid(row=1, column=0, columnspan=2, sticky="w")
        self._register(cb, "gps_led_cal")
        gpp = ttk.Frame(gps_adv)
        ttk.Label(gpp, text="POSB Start").pack(side=tk.LEFT)
        ttk.Entry(gpp, textvariable=self.v_gps_pos_b, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(gpp, text="POSA End").pack(side=tk.LEFT)
        ttk.Entry(gpp, textvariable=self.v_gps_pos_a, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(gpp, text="width").pack(side=tk.LEFT)
        ttk.Entry(gpp, textvariable=self.v_gps_pos_width, width=5).pack(side=tk.LEFT)
        gpp.grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        b = ttk.Button(gps_adv, text=self.T("gps_apply"), command=self.run_apply_gps_pos)
        b.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._register(b, "gps_apply")

        gps_utc = ttk.LabelFrame(self._gps_adv_host, text=self.T("gps_utc"), padding=8)
        self._register(gps_utc, "gps_utc")
        gps_utc.pack(fill=tk.X, pady=(8, 0))
        gps_utc.columnconfigure(1, weight=1)

        def gup(r, key, widget):
            _lbl(gps_utc, key).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", padx=(6, 0), pady=2)

        _utc = utc_now_plus_js1995(5.0)[1]
        self.v_gps_y = tk.StringVar(value=str(_utc[0]))
        self.v_gps_m = tk.StringVar(value=str(_utc[1]))
        self.v_gps_d = tk.StringVar(value=str(_utc[2]))
        self.v_gps_h = tk.StringVar(value=str(_utc[3]))
        self.v_gps_min = tk.StringVar(value=str(_utc[4]))
        self.v_gps_s = tk.StringVar(value=str(_utc[5]))
        self.v_gps_target_us = tk.StringVar(value="0")
        self.v_gps_delta_sec = tk.StringVar(value="0")
        self.v_gps_delta_us = tk.StringVar(value="0")
        self.v_gps_slave_exp = tk.StringVar(value="100")
        self.v_gps_slave_exp_unit = tk.StringVar(value="ms")
        self.v_gps_from_now = tk.StringVar(value="5")
        self.v_gps_js1995 = tk.StringVar(value="")
        ymd = ttk.Frame(gps_utc)
        for var, w in [(self.v_gps_y, 5), (self.v_gps_m, 3), (self.v_gps_d, 3)]:
            ttk.Entry(ymd, textvariable=var, width=w).pack(side=tk.LEFT, padx=(0, 4))
        gup(0, "gps_utc_ymd", ymd)
        hms = ttk.Frame(gps_utc)
        for var, w in [(self.v_gps_h, 3), (self.v_gps_min, 3), (self.v_gps_s, 3)]:
            ttk.Entry(hms, textvariable=var, width=w).pack(side=tk.LEFT, padx=(0, 4))
        gup(1, "gps_utc_hms", hms)
        exp_row = ttk.Frame(gps_utc)
        ttk.Entry(exp_row, textvariable=self.v_gps_slave_exp, width=8).pack(side=tk.LEFT)
        ttk.Combobox(exp_row, textvariable=self.v_gps_slave_exp_unit, values=["us", "ms", "s"],
                     state="readonly", width=5).pack(side=tk.LEFT, padx=(4, 0))
        gup(2, "gps_slave_exp", exp_row)
        gup(3, "gps_target", ttk.Entry(gps_utc, textvariable=self.v_gps_target_us))
        delta_row = ttk.Frame(gps_utc)
        ttk.Entry(delta_row, textvariable=self.v_gps_delta_sec, width=8).pack(side=tk.LEFT)
        ttk.Label(delta_row, text="s +").pack(side=tk.LEFT, padx=2)
        ttk.Entry(delta_row, textvariable=self.v_gps_delta_us, width=8).pack(side=tk.LEFT)
        ttk.Label(delta_row, text="us").pack(side=tk.LEFT, padx=2)
        gup(4, "gps_delta", delta_row)
        now_row = ttk.Frame(gps_utc)
        ttk.Entry(now_row, textvariable=self.v_gps_from_now, width=8).pack(side=tk.LEFT)
        ttk.Label(now_row, text="s").pack(side=tk.LEFT, padx=(2, 6))
        b = ttk.Button(now_row, text=self.T("gps_fill_now"), command=self.fill_gps_utc_from_now)
        b.pack(side=tk.LEFT)
        self._register(b, "gps_fill_now")
        gup(5, "gps_from_now", now_row)
        gup(6, "gps_js1995", ttk.Label(gps_utc, textvariable=self.v_gps_js1995, wraplength=220))
        _lbl(gps_utc, "gps_utc_note", wraplength=320, foreground="#666").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))
        b = ttk.Button(gps_utc, text=self.T("gps_set_utc"), command=self.run_set_gps_slave_utc)
        b.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._register(b, "gps_set_utc")
        self._bind_gps_utc_preview_traces()
        self.root.after(50, self.preview_gps_js1995)

        scope = ttk.LabelFrame(left, text=self.T("param_scope"), padding=8)
        self._register(scope, "param_scope")
        scope.pack(fill=tk.X, pady=(8, 0))
        rb = ttk.Radiobutton(scope, text=self.T("scope_current"), value="current", variable=self.v_gps_scope)
        rb.pack(side=tk.LEFT)
        self._register(rb, "scope_current")
        rb = ttk.Radiobutton(scope, text=self.T("scope_all"), value="all", variable=self.v_gps_scope)
        rb.pack(side=tk.LEFT, padx=(8, 0))
        self._register(rb, "scope_all")
        rb = ttk.Radiobutton(scope, text=self.T("scope_masters"), value="masters", variable=self.v_gps_scope)
        rb.pack(side=tk.LEFT, padx=(8, 0))
        self._register(rb, "scope_masters")
        rb = ttk.Radiobutton(scope, text=self.T("scope_slaves"), value="slaves", variable=self.v_gps_scope)
        rb.pack(side=tk.LEFT, padx=(8, 0))
        self._register(rb, "scope_slaves")
        b = ttk.Button(scope, text=self.T("apply_now"), command=self.apply_params_now)
        b.pack(side=tk.LEFT, padx=(16, 0))
        self._register(b, "apply_now")

        # --- 三个一键按钮（主从确定后）：一键设置主从参数 / 进入同步拍摄 / 退出同步拍摄 ---
        onekey = ttk.LabelFrame(left, text=self.T("onekey"), padding=8)
        self._register(onekey, "onekey")
        onekey.pack(fill=tk.X, pady=(8, 0))
        b = ttk.Button(onekey, text=self.T("onekey1"), command=self.onekey_set_sync_params, width=22)
        b.pack(side=tk.LEFT, padx=(0, 8))
        self._register(b, "onekey1")
        b = ttk.Button(onekey, text=self.T("onekey2"), command=self.onekey_enter_sync, width=22)
        b.pack(side=tk.LEFT, padx=8)
        self._register(b, "onekey2")
        b = ttk.Button(onekey, text=self.T("onekey3"), command=self.onekey_exit_sync, width=22)
        b.pack(side=tk.LEFT, padx=(8, 0))
        self._register(b, "onekey3")
        _lbl(onekey, "onekey_note", wraplength=320, foreground="#666", justify=tk.LEFT).pack(
            anchor=tk.W, pady=(6, 0), fill=tk.X)

        # --- 主从同步相机参数（区分 USB / PCIe）：六个同步按钮据此生成寄存器序列 ---
        syncp = ttk.LabelFrame(left, text=self.T("sync_params"), padding=8)
        self._register(syncp, "sync_params")
        syncp.pack(fill=tk.X, pady=(8, 0))
        syncp.columnconfigure(1, weight=1)

        def sp(r, key, widget):
            _lbl(syncp, key).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", padx=(6, 0), pady=2)

        sp(0, "usb_patch", ttk.Entry(syncp, textvariable=self.v_usb_patch, width=10))
        sp(1, "pcie_patch", ttk.Entry(syncp, textvariable=self.v_pcie_patch, width=10))
        sp(2, "burst_start", ttk.Entry(syncp, textvariable=self.v_burst_start, width=10))
        sp(3, "burst_window", ttk.Entry(syncp, textvariable=self.v_window, width=10))
        fw = ttk.Checkbutton(syncp, text=self.T("filter_burst_waste"),
                             variable=self.filter_burst_waste)
        fw.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._register(fw, "filter_burst_waste")
        _lbl(syncp, "filter_burst_waste_note", wraplength=320, foreground="#666",
             justify=tk.LEFT).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
        sp(6, "min_pulse", ttk.Entry(syncp, textvariable=self.v_min_pulse, width=10))
        row39 = ttk.Frame(syncp)
        ttk.Label(row39, text=self.T("role_master") + "39").pack(side=tk.LEFT)
        ttk.Entry(row39, textvariable=self.v_master_39, width=6).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row39, text=self.T("role_slave") + "39").pack(side=tk.LEFT)
        ttk.Entry(row39, textvariable=self.v_slave_39, width=6).pack(side=tk.LEFT, padx=(4, 0))
        sp(7, "reg39", row39)
        row58 = ttk.Frame(syncp)
        ttk.Label(row58, text=self.T("role_master") + "58").pack(side=tk.LEFT)
        ttk.Entry(row58, textvariable=self.v_master_58, width=6).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row58, text=self.T("role_slave") + "58").pack(side=tk.LEFT)
        ttk.Entry(row58, textvariable=self.v_slave_58, width=6).pack(side=tk.LEFT, padx=(4, 0))
        sp(8, "reg58", row58)
        _lbl(syncp, "syncp_note", wraplength=320, foreground="#666", justify=tk.LEFT).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_capture_control(self, parent: ttk.Frame):
        """Column 1: 拍摄入口 (单帧 / 连续 / 断开) + 范围 + 存储 + 参数状态 + 状态 + 当前分配."""
        col_left = parent

        def _lbl(parent_, key, **kw):
            w = ttk.Label(parent_, text=self.T(key), **kw)
            self._register(w, key)
            return w

        ops = ttk.LabelFrame(col_left, text=self.T("capture_entry"), padding=8)
        self._register(ops, "capture_entry")
        ops.pack(fill=tk.X)
        # 出图范围（单一开关）：current=仅当前选中相机；all=扫描列表里所有相机。
        # 旧版这里还有个「多机同时采集」勾选框，但它默认勾选且会覆盖下面的单选，
        # 导致「选什么都一起出图」。现在直接用这一个单选决定 Live/Capture 的出图集合。
        scope = ttk.Frame(ops)
        _lbl(scope, "scope").pack(side=tk.LEFT)
        self.live_scope = tk.StringVar(value="all")
        self._live_scope_rb_cur = ttk.Radiobutton(scope, text=self.T("ls_current"), value="current", variable=self.live_scope)
        self._live_scope_rb_cur.pack(side=tk.LEFT, padx=(6, 0))
        self._register(self._live_scope_rb_cur, "ls_current")
        self._live_scope_rb_all = ttk.Radiobutton(scope, text=self.T("ls_all"), value="all", variable=self.live_scope)
        self._live_scope_rb_all.pack(side=tk.LEFT)
        self._register(self._live_scope_rb_all, "ls_all")
        scope.pack(fill=tk.X, pady=(0, 6))
        _lbl(ops, "scope_note", wraplength=230, foreground="#666", justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        b3 = ttk.Frame(ops)
        for c in range(3):
            b3.columnconfigure(c, weight=1)
        self.btn_capture = ttk.Button(b3, text=self.T("capture_single"), command=self.run_capture_single)
        self.btn_capture.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self._register(self.btn_capture, "capture_single")
        self.btn_live = ttk.Button(b3, text="Live", command=self.toggle_live)
        self.btn_live.grid(row=0, column=1, sticky="ew", padx=3)
        self.btn_close = ttk.Button(b3, text=self.T("close"), command=self.close_all_cams)
        self.btn_close.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        self._register(self.btn_close, "close")
        # b3 必须 pack 进 ops：之前漏了这一步，3 个按钮 grid 进 b3 后整行 0 尺寸不可见
        b3.pack(fill=tk.X, pady=(0, 6))
        self.btn_close_all = ttk.Button(ops, text=self.T("close_all"), command=self.close_all_cams)
        self.btn_close_all.pack(fill=tk.X, pady=(6, 0))
        self._register(self.btn_close_all, "close_all")
        self._reconnect_hint = ttk.Label(
            ops, text=self.T("reconnect_hint"), wraplength=230, foreground="#666", justify=tk.LEFT)
        self._reconnect_hint.pack(anchor=tk.W, pady=(6, 0))
        self._register(self._reconnect_hint, "reconnect_hint")

        # 参数状态 + 自动下发开关：live/连接中改动参数实时下发，无需手动 Connect/Read back。
        ps = ttk.LabelFrame(col_left, text=self.T("param_status"), padding=6)
        self._register(ps, "param_status")
        ps.pack(fill=tk.X, pady=(8, 0))
        self.param_status_var.set(self.T("ps_idle"))
        ttk.Label(ps, textvariable=self.param_status_var, wraplength=240, justify=tk.LEFT,
                  foreground="#2a6").pack(anchor=tk.W)
        self._busy_msg = tk.StringVar(value="")
        self._busy_lbl = ttk.Label(ps, textvariable=self._busy_msg, wraplength=240,
                                   justify=tk.LEFT, foreground="#666")
        self._busy_lbl.pack(anchor=tk.W, pady=(4, 0))
        self._busy_bar = ttk.Progressbar(ps, mode="indeterminate", length=220)
        # hidden until a long op starts
        self._busy_token = 0
        auto = ttk.Checkbutton(ps, text=self.T("auto_push"), variable=self.auto_push)
        auto.pack(anchor=tk.W, pady=(6, 0))
        self._register(auto, "auto_push")
        _lbl(ps, "auto_push_note", wraplength=240, foreground="#666", justify=tk.LEFT).pack(
            anchor=tk.W, pady=(4, 0))

        # 图像存储：勾选后每帧 Live / 单帧图自动存到该目录。
        sv = ttk.LabelFrame(col_left, text=self.T("save"), padding=6)
        self._register(sv, "save")
        sv.pack(fill=tk.X, pady=(8, 0))
        self._save_chk = ttk.Checkbutton(sv, text=self.T("save_enabled"), variable=self.save_enabled)
        self._save_chk.pack(anchor=tk.W)
        self._register(self._save_chk, "save_enabled")
        iv = ttk.Frame(sv)
        iv.pack(fill=tk.X, pady=(6, 0))
        _lbl(iv, "save_interval").pack(side=tk.LEFT)
        ttk.Entry(iv, textvariable=self.v_save_interval, width=4).pack(side=tk.LEFT, padx=(4, 0))
        sd = ttk.Frame(sv)
        sd.pack(fill=tk.X, pady=(6, 0))
        _lbl(sd, "save_subdir").pack(side=tk.LEFT)
        ent_sub = ttk.Entry(sd, textvariable=self.v_save_subdir)
        ent_sub.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        ent_sub.bind("<FocusOut>", lambda _e: self._on_save_subdir_focus_out())
        # Directory on its own row: label | expanding path | browse (must not pack side=LEFT into LabelFrame or it overlaps later rows)
        dr = ttk.Frame(sv)
        dr.pack(fill=tk.X, pady=(6, 0))
        _lbl(dr, "save_dir").pack(side=tk.LEFT)
        ttk.Entry(dr, textvariable=self.v_save_dir).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        b = ttk.Button(dr, text=self.T("save_browse"), width=6, command=self._choose_save_dir)
        b.pack(side=tk.LEFT)
        self._register(b, "save_browse")
        _lbl(sv, "save_subdir_note", wraplength=240, foreground="#666", justify=tk.LEFT).pack(
            anchor=tk.W, pady=(4, 0))
        # 存图百分比进度 + 丢帧预警
        self._save_prog_var = tk.StringVar(value=self.T("save_prog_idle"))
        self._save_prog_lbl = ttk.Label(sv, textvariable=self._save_prog_var,
                                       wraplength=240, justify=tk.LEFT)
        self._save_prog_lbl.pack(anchor=tk.W, pady=(6, 0))
        self._save_bar = ttk.Progressbar(sv, mode="determinate", maximum=100, length=220)
        self._save_bar.pack(fill=tk.X, pady=(2, 0))
        self._save_warn_var = tk.StringVar(value="")
        self._save_warn_lbl = ttk.Label(
            sv, textvariable=self._save_warn_var, wraplength=240,
            justify=tk.LEFT, foreground="#c62828",
        )
        self._save_warn_lbl.pack(anchor=tk.W, pady=(4, 0))
        _lbl(sv, "save_note", wraplength=240, foreground="#666", justify=tk.LEFT).pack(
            anchor=tk.W, pady=(4, 0))

        stats = ttk.LabelFrame(col_left, text=self.T("status"), padding=8)
        self._register(stats, "status")
        stats.pack(fill=tk.X, pady=(8, 0))
        self.cap_status_var = tk.StringVar(value=self.T("status_idle"))
        ttk.Label(stats, textvariable=self.cap_status_var, wraplength=230, justify=tk.LEFT).pack(anchor=tk.W)
        self.multi_var = tk.StringVar(value="")
        ttk.Label(stats, textvariable=self.multi_var, wraplength=230, justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))

        # 多机总览：当前相机 + 每台状态/帧/GPS 帧序号 + 帧率
        self.cap_dash_text = tk.Text(stats, height=12, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED)
        self.cap_dash_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self._set_dash_text(self.T("dash_idle"))
        ttk.Label(stats, textvariable=self.cap_multi_status_var, wraplength=230,
                  foreground="#333", justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(stats, textvariable=self.cap_frame_var, font=("Consolas", 9)).pack(anchor=tk.W)
        ttk.Label(stats, textvariable=self.cap_fps_var, font=("Consolas", 9)).pack(anchor=tk.W)
        b = ttk.Button(stats, text=self.T("retile"), command=self.tile_multi_preview_windows)
        b.pack(anchor=tk.W, pady=(6, 0))
        self._register(b, "retile")

        info = ttk.LabelFrame(col_left, text=self.T("current_assign"), padding=6)
        self._register(info, "current_assign")
        info.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._param_cam_list = tk.Text(info, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED)
        self._param_cam_list.pack(fill=tk.BOTH, expand=True)

    def _build_capture_log(self, parent: ttk.Frame):
        """Column 3: 采集日志."""
        logf = ttk.LabelFrame(parent, text=self.T("capture_log"), padding=4)
        self._register(logf, "capture_log")
        logf.pack(fill=tk.BOTH, expand=True)
        self.cap_log = tk.Text(logf, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED)
        cap_ys = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.cap_log.yview)
        self.cap_log.configure(yscrollcommand=cap_ys.set)
        cap_ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.cap_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _build_sync_tab(self):
        tip = ttk.Label(
            self.tab_sync,
            text=self.T("sync_hint"),
            wraplength=1100, foreground="#444", justify=tk.LEFT,
        )
        tip.pack(anchor=tk.W, pady=(0, 8), fill=tk.X)
        self._register(tip, "sync_hint")

        for group_name, buttons in SYNC_GROUPS:
            grp = ttk.LabelFrame(self.tab_sync, text=group_name, padding=8)
            grp.pack(fill=tk.X, pady=(0, 8))
            for i, (label, key, target, transport) in enumerate(buttons):
                b = ttk.Button(grp, text=f"{label}",
                               command=lambda k=key, t=target, lbl=label, tr=transport: self.run_sync_mode(k, t, lbl, transport=tr))
                b.grid(row=i // 4, column=i % 4, sticky="ew", padx=4, pady=4)
            for c in range(4):
                grp.columnconfigure(c, weight=1)

        # target summary + status
        summary = ttk.Frame(self.tab_sync, padding=(0, 4))
        summary.pack(fill=tk.X)
        self._sync_target_var = tk.StringVar(value="")
        lbl_target = ttk.Label(summary, text=self.T("sync_target"))
        lbl_target.pack(side=tk.LEFT)
        self._register(lbl_target, "sync_target")
        ttk.Label(summary, textvariable=self._sync_target_var, foreground="#333").pack(side=tk.LEFT, padx=(6, 0))
        b = ttk.Button(summary, text=self.T("preview_seq"), command=self.preview_sync_steps)
        b.pack(side=tk.RIGHT, padx=(0, 8))
        self._register(b, "preview_seq")
        b = ttk.Button(summary, text=self.T("dryrun_all"), command=self.dry_run_all_sync)
        b.pack(side=tk.RIGHT)
        self._register(b, "dryrun_all")

        status = ttk.LabelFrame(self.tab_sync, text=self.T("sync_status"), padding=6)
        self._register(status, "sync_status")
        status.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.sync_text = tk.Text(status, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED)
        self.sync_text.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------- cameras
    def _scan_names_reg(self) -> list[str]:
        from qhyccd_api import QhyApi, init_qhy_resource_with_retry, list_camera_names, scan_cameras
        from qhyccd_highlevel import _ensure_highlevel_bindings
        with self._reg_lock:
            if self._reg_api is None:
                api = QhyApi(self.project_root)
                _ensure_highlevel_bindings(api)
                init_qhy_resource_with_retry(api)
                self._reg_api = api
            return list_camera_names(scan_cameras(self._reg_api))

    def scan_cameras(self):
        token = self._busy_begin(self.T("busy_scan"))
        try:
            self.btn_scan.configure(state=tk.DISABLED)
        except Exception:
            pass

        def work():
            try:
                names = self._scan_names_reg()
                self.root.after(0, lambda: self._on_scanned(names, busy_token=token))
            except Exception as exc:
                self.output_queue.put(f"[scan] ERROR: {exc}\n")
                self.root.after(0, lambda m=str(exc): self.status_var.set(f"扫描失败: {m}"))
                self._busy_end(token, f"扫描失败: {exc}")
            finally:
                try:
                    self.root.after(0, lambda: self.btn_scan.configure(state=tk.NORMAL))
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()

    def _on_scanned(self, names: list[str], busy_token: int | None = None):
        # preserve existing role/transport assignments for cameras we already know
        prev_name = None
        try:
            sc = self._selected_cam()
            if sc is not None:
                prev_name = sc.name
        except Exception:
            prev_name = None
        old = {c.name: c for c in self.cameras}
        name_set = set(names)
        # 离线名从采图池强制 close，避免热插拔后残留死连接。
        # Live 中勿对仍在采集的相机 close（会打穿 live 线程）；提示先 Stop。
        if self._cap_pool is not None:
            live_names = set(self._live_threads.keys())
            for opened in list(self._cap_pool.opened()):
                if opened not in name_set:
                    if (
                        opened in live_names
                        or self._capture_live_running
                        or self._capture_live_stopping
                    ):
                        self.output_queue.put(
                            f"[scan] {opened} 仍在 Live/Stopping，跳过强制关闭（请先 Stop Live）\n"
                        )
                        continue
                    try:
                        self._cap_pool.close(opened)
                        self.output_queue.put(f"[scan] 离线相机已从池关闭: {opened}\n")
                    except Exception as exc:
                        self.output_queue.put(f"[scan] 关闭离线 {opened} 失败: {exc}\n")
        cams = []
        for n in names:
            if n in old:
                cams.append(old[n])
            else:
                cams.append(CamRow(name=n))
        self.cameras = cams
        # 选中目标：优先保留扫描前选中名；否则第一台
        target_idx = 0
        if prev_name:
            for i, c in enumerate(cams):
                if c.name == prev_name:
                    target_idx = i
                    break
        if cams:
            self._sel_idx.set(target_idx)
        self._refresh_cam_tree()
        if cams:
            iid = str(target_idx)
            try:
                self.cam_tree.selection_set(iid)
                self.cam_tree.focus(iid)
                self.cam_tree.see(iid)
            except Exception:
                pass
            self._param_cam_name = cams[target_idx].name
            self._update_sel_label()
        self._refresh_param_cam_list()
        self._update_sync_targets()
        self.status_var.set(self.T("scan_found").format(n=len(cams)))
        tok = busy_token if busy_token is not None else getattr(self, "_busy_token", 0)

        # 扫描后自动连接一台并读回采集参数（由勾选控制；Live 中跳过以免冲突）
        try:
            do_auto = bool(self.refresh_before_capture.get())
        except Exception:
            do_auto = True
        if do_auto and cams:
            if self._capture_live_running or self._capture_live_stopping:
                self.output_queue.put(
                    "[scan] Live/Stopping 中，跳过扫描后自动连接读回（请先 Stop Live）\n"
                )
                self._busy_end(tok, self.T("busy_scan_done").format(n=len(cams)))
            else:
                self._busy_update(tok, self.T("busy_autoconnect").format(name=cams[target_idx].name))
                self._auto_connect_and_readback_after_scan(
                    cams[target_idx].name, busy_token=tok)
        else:
            self._busy_end(tok, self.T("busy_scan_done").format(n=len(cams)))

    def _auto_connect_and_readback_after_scan(self, name: str, busy_token: int | None = None) -> None:
        """扫描完成后：后台打开指定相机，并把采集参数读回 GUI。"""
        token = busy_token if busy_token is not None else self._busy_begin(
            self.T("busy_autoconnect").format(name=name))

        def work():
            try:
                # 再确认一次：用户可能在线程启动后点了 Live
                if self._capture_live_running or self._capture_live_stopping:
                    self.output_queue.put(
                        f"[scan] Live/Stopping 中，取消自动连接读回 {name}\n"
                    )
                    self._busy_end(token, self.T("busy_done"))
                    return
                self._busy_update(token, self.T("busy_autoconnect").format(name=name))
                pool = self._capture_pool()
                self._pool_open_resilient(pool, name)
                for c in self.cameras:
                    if c.name == name:
                        c.connected = True
                        break
                self.root.after(0, self._clear_roi_fields)
                self.root.after(0, self._refresh_cam_tree)
                self.output_queue.put(f"[scan] 扫描后自动连接并读回 {name}\n")
                self._busy_update(token, self.T("busy_readback").format(name=name))
                self.root.after(0, lambda n=name: self.status_var.set(f"已自动连接 {n}"))
                self.root.after(0, lambda n=name: self.read_params_from_cam(n, auto=True))
                self._busy_end(token, self.T("busy_done"), status_key="ps_readback")
            except Exception as exc:
                self.output_queue.put(f"[scan] 扫描后自动连接读回失败 {name}: {exc}\n")
                self.root.after(
                    0, lambda m=str(exc): self.status_var.set(f"扫描后自动连接失败: {m}"))
                self._busy_end(token, f"扫描后自动连接失败: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def _refresh_cam_tree(self):
        # 「已连接」以采图池里实际打开的相机为准：Live / 单帧 Capture 会自动打开
        # 目标相机（无需先点 Connect），这里据此同步 connected 标志，状态列随之刷新。
        live_names = set(self._live_threads.keys())
        if self._cap_pool is not None:
            opened = set(self._cap_pool.opened())
            for c in self.cameras:
                c.connected = c.name in opened
                c.live = c.name in live_names
        else:
            for c in self.cameras:
                c.live = c.name in live_names
        for i in self.cam_tree.get_children():
            self.cam_tree.delete(i)
        for i, c in enumerate(self.cameras):
            state = self.T("st_live") if c.live else (self.T("st_conn") if c.connected else self.T("st_disconn"))
            self.cam_tree.insert("", "end", iid=str(i), values=(c.name, self.role_display(c.role), self.transport_display(c.transport), state))
        if self.cameras and self._sel_idx.get() < 0:
            self._sel_idx.set(0)
            self.cam_tree.selection_set(str(0))

    def _selected_cam(self) -> CamRow | None:
        i = self._sel_idx.get()
        if 0 <= i < len(self.cameras):
            return self.cameras[i]
        return None

    def _on_cam_selected(self, _e):
        sel = self.cam_tree.selection()
        if not sel:
            return
        i = int(sel[0])
        self._sel_idx.set(i)
        c = self.cameras[i]
        prev = self._param_cam_name
        self._param_cam_name = c.name
        self._role_combo_var.set(self.role_display(c.role))
        self._trans_combo_var.set(self.transport_display(c.transport))
        self._update_sel_label()
        self._refresh_param_cam_list()
        self._update_sync_targets()
        # 换机：先存上一台 ROI，再载入本机 ROI；GPS 从/主跟角色走
        try:
            self.v_gps_slave.set(c.role == ROLE_SLAVE)
        except Exception:
            pass
        if prev is not None and prev != c.name:
            self._store_roi_for_cam(prev)
            self._load_roi_for_cam(c.name)
            opened = set(self._cap_pool.opened()) if self._cap_pool is not None else set()
            if c.name in opened and not getattr(self, "_suppress_cam_switch_readback", False):
                self.root.after(0, lambda n=c.name: self.read_params_from_cam(n, auto=True))

    def _set_selected_role(self, role: str = None):
        c = self._selected_cam()
        if c is None:
            return
        # 下拉存的是显示名（随语言），回读成中文常量；按钮直接传常量
        if role is None:
            role = self._role_display_to_const(self._role_combo_var.get())
        c.role = role
        # 一主：同一时间只允许一台主机
        if role == ROLE_MASTER:
            for other in self.cameras:
                if other is not c and other.role == ROLE_MASTER:
                    other.role = ROLE_STANDALONE
        # 从机 → GPS slave 联动
        self.v_gps_slave.set(role == ROLE_SLAVE)
        self._update_sel_label()
        self._refresh_cam_tree()
        self._refresh_param_cam_list()
        self._update_sync_targets()
        if role in (ROLE_MASTER, ROLE_SLAVE):
            try:
                self._maybe_auto_seq_list_mode(None)
            except Exception:
                pass

    def _update_sel_label(self):
        c = self._selected_cam()
        if c is None:
            return
        self._sel_cam_label_var.set(c.name)
        self._sel_role_label.configure(
            text=f"Role: {self.role_display(c.role)}    Transport: {self.transport_display(c.transport)}"
            if self.lang == "en"
            else f"角色: {self.role_display(c.role)}    传输: {self.transport_display(c.transport)}")

    def _set_selected_transport(self):
        c = self._selected_cam()
        if c is None:
            return
        c.transport = self._trans_display_to_const(self._trans_combo_var.get())
        self._update_sel_label()
        self._refresh_cam_tree()
        self._refresh_param_cam_list()
        self._update_sync_targets()

    def _set_rest_slaves(self):
        """把除当前选中（主机）之外的其余全部设为从机。"""
        cur = self._selected_cam()
        if cur is None:
            messagebox.showinfo("主从", "先在列表里选中要作为主机的那台相机。")
            return
        cur.role = ROLE_MASTER
        for c in self.cameras:
            if c is not cur:
                c.role = ROLE_SLAVE
        self._refresh_cam_tree()
        self._refresh_param_cam_list()
        self._update_sync_targets()
        try:
            self._maybe_auto_seq_list_mode(None)
        except Exception:
            pass

    def _clear_roles(self):
        for c in self.cameras:
            c.role = ROLE_STANDALONE
        self._refresh_cam_tree()
        self._refresh_param_cam_list()
        self._update_sync_targets()

    def _refresh_param_cam_list(self):
        self._param_cam_list.configure(state=tk.NORMAL)
        self._param_cam_list.delete("1.0", tk.END)
        if not self.cameras:
            self._param_cam_list.insert("end", self.T("no_cam_yet"))
        cur = self._selected_cam()
        for c in self.cameras:
            mark = f"   {self.T('mark_current')}" if (cur is not None and c.name == cur.name) else ""
            self._param_cam_list.insert(
                "end",
                f"{self.role_display(c.role):<8}  {self.transport_display(c.transport):<12}  {c.name}{mark}\n")
        self._param_cam_list.configure(state=tk.DISABLED)

    def _update_sync_targets(self):
        masters = [c.name for c in self.cameras if c.role == ROLE_MASTER]
        slaves = [c.name for c in self.cameras if c.role == ROLE_SLAVE]
        usb_slaves = [c.name for c in self.cameras if c.role == ROLE_SLAVE and c.transport == TRANSPORT_USB]
        pcie_slaves = [c.name for c in self.cameras if c.role == ROLE_SLAVE and c.transport == TRANSPORT_PCIE]
        if self.lang == "en":
            txt = (f"Master {len(masters)} {masters} | Slave {len(slaves)} "
                   f"(USB: {usb_slaves} / PCIe: {pcie_slaves})")
        else:
            txt = (f"主机 {len(masters)} 台 {masters} | 从机 {len(slaves)} 台 "
                   f"(USB: {usb_slaves} / PCIe: {pcie_slaves})")
        self._sync_target_var.set(txt)

    # ------------------------------------------------------------- connect
    def connect_selected(self):
        c = self._selected_cam()
        if c is None:
            messagebox.showinfo("相机", "先在「相机/主从」页选中一台相机。")
            return
        name = c.name
        def work():
            try:
                pool = self._capture_pool()
                self._pool_open_resilient(pool, name)
                c.connected = True
                self.root.after(0, self._clear_roi_fields)
                self.root.after(0, self._refresh_cam_tree)
                self.output_queue.put(f"[connect] 已连接 {name}（独立采图子进程）\n")
                self.root.after(0, lambda: self.status_var.set(f"已连接 {name}"))
                # 连接成功后自动回读相机当前参数（读出模式/分辨率/曝光/gain...）刷新到 GUI
                self.root.after(0, lambda: self.read_params_from_cam(name))
            except Exception as exc:
                self.output_queue.put(f"[connect] ERROR {name}: {exc}\n")
                self.root.after(0, lambda m=str(exc): self.status_var.set(f"连接失败: {m}"))
        threading.Thread(target=work, daemon=True).start()

    def disconnect_selected(self):
        c = self._selected_cam()
        if c is None:
            return
        name = c.name
        def work():
            try:
                self.root.after(0, lambda: self._set_capture_ops_enabled(False))
                self._closing_cams = True
                if not self._ensure_live_stopped_blocking():
                    self.output_queue.put(f"[disconnect] 等待 Stop Live 超时，仍尝试关闭 {name}\n")
                if self._cap_pool:
                    self._cap_pool.close(name)
                c.connected = False
                c.live = False
                self.root.after(0, self._clear_roi_fields)
                self.root.after(0, self._refresh_cam_tree)
                self.output_queue.put(f"[disconnect] 已断开 {name}\n")
            except Exception as exc:
                self.output_queue.put(f"[disconnect] ERROR: {exc}\n")
            finally:
                self._closing_cams = False
                self.root.after(0, self._sync_live_guards)
        threading.Thread(target=work, daemon=True).start()

    def close_all_cams(self):
        def work():
            try:
                self.root.after(0, lambda: self._set_capture_ops_enabled(False))
                self._closing_cams = True
                if not self._ensure_live_stopped_blocking():
                    self.output_queue.put("[close] 等待 Stop Live 超时，仍尝试断开全部\n")
                if self._cap_pool:
                    self._cap_pool.close_all()
            except Exception as exc:
                self.output_queue.put(f"[close] ERROR: {exc}\n")
            for c in self.cameras:
                c.connected = False
                c.live = False
            self.root.after(0, self._clear_roi_fields)
            self.root.after(0, self._refresh_cam_tree)
            self._closing_cams = False
            self.root.after(0, self._sync_live_guards)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- params
    def _selected_bin(self) -> tuple[int, int]:
        try:
            x, y = self.v_bin.get().split("x")
            return int(x), int(y)
        except Exception:
            return 1, 1

    def _opt_int(self, t: str) -> int | None:
        t = (t or "").strip()
        if not t:
            return None
        try:
            return int(t)
        except Exception:
            return None

    def _opt_float(self, t: str) -> float | None:
        t = (t or "").strip()
        if not t:
            return None
        try:
            return float(t)
        except Exception:
            return None


    @staticmethod
    def _parse_read_mode(raw) -> int:
        """读出模式下拉可能是 '0' 或回读后的 '0: Photo'；只取前导整数。"""
        s = str(raw or "").strip()
        if not s:
            return 0
        # 允许 "1: High Gain" / "1：高增益" / "1"
        m = re.match(r"^(\d+)", s)
        if m:
            return int(m.group(1))
        raise ValueError(f"invalid read mode: {raw!r}")

    def _read_mode_from_gui(self) -> int:
        return self._parse_read_mode(self.v_readmode.get())

    def _roi_fields_as_dict(self) -> dict:
        """当前 ROI 输入框 → dict；空宽高表示全幅。"""
        try:
            x = int((self.v_roi_x.get() or "0").strip() or "0")
        except Exception:
            x = 0
        try:
            y = int((self.v_roi_y.get() or "0").strip() or "0")
        except Exception:
            y = 0
        w = self._opt_int(self.v_roi_w.get())
        h = self._opt_int(self.v_roi_h.get())
        return {"x": x, "y": y, "w": w, "h": h}

    def _set_roi_fields(self, x: int = 0, y: int = 0, w=None, h=None, *, silent: bool = True) -> None:
        prev = self._param_edit_silent
        try:
            if silent:
                self._param_edit_silent = True
            self.v_roi_x.set(str(int(x)))
            self.v_roi_y.set(str(int(y)))
            self.v_roi_w.set("" if w is None else str(int(w)))
            self.v_roi_h.set("" if h is None else str(int(h)))
        finally:
            self._param_edit_silent = prev

    def _store_roi_for_cam(self, name: str | None) -> None:
        if not name:
            return
        self._roi_by_cam[name] = self._roi_fields_as_dict()

    def _load_roi_for_cam(self, name: str | None) -> None:
        """把该机已存 ROI 填入输入框；没有记录则空=全幅。"""
        if not name:
            self._clear_roi_fields()
            return
        d = self._roi_by_cam.get(name)
        if not d:
            self._clear_roi_fields()
            return
        self._set_roi_fields(
            int(d.get("x", 0) or 0),
            int(d.get("y", 0) or 0),
            d.get("w"),
            d.get("h"),
        )

    def _roi_dict_for_cam(self, name: str | None) -> dict:
        """取某机 ROI：优先缓存；当前选中机用输入框；否则全幅。"""
        if name and name in self._roi_by_cam:
            return dict(self._roi_by_cam[name])
        if name and name == getattr(self, "_param_cam_name", None):
            return self._roi_fields_as_dict()
        if name is None and getattr(self, "_param_cam_name", None):
            return self._roi_fields_as_dict()
        return {"x": 0, "y": 0, "w": None, "h": None}

    def _clear_roi_fields(self, *, clear_cache: bool = False) -> None:
        """清空 ROI 输入框；clear_cache 时连当前机缓存一起清（bin/读出模式变更）。"""
        self._set_roi_fields(0, 0, None, None)
        if clear_cache:
            cur = getattr(self, "_param_cam_name", None)
            if cur:
                self._roi_by_cam[cur] = {"x": 0, "y": 0, "w": None, "h": None}

    def _live_busy(self) -> bool:
        return bool(
            getattr(self, "_capture_live_running", False)
            or getattr(self, "_capture_live_stopping", False)
            or getattr(self, "_live_threads", None)
        )

    def _set_capture_ops_enabled(self, enabled: bool) -> None:
        """启用/禁用 Live 外会撕裂会话的按钮（完整下发 / 单帧 / 关闭过程）。"""
        st = "normal" if enabled else "disabled"
        for name in ("btn_capture", "btn_full_push", "btn_close", "btn_close_all", "btn_disconnect"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state=st)
            except Exception:
                pass
        # Live 按钮在 Stopping 时仍可点以催停；关闭流程中禁用
        if not enabled:
            try:
                if getattr(self, "btn_live", None) is not None and self._closing_cams:
                    self.btn_live.configure(state="disabled")
            except Exception:
                pass

    def _sync_live_guards(self) -> None:
        """按 Live/关闭状态同步按钮与读出模式下拉。"""
        if self._closing_cams:
            self._set_capture_ops_enabled(False)
            return
        live = bool(self._capture_live_running or self._capture_live_stopping)
        # 完整下发 / 单帧：Live 中禁用（R3）
        for name in ("btn_capture", "btn_full_push"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state="disabled" if live else "normal")
            except Exception:
                pass
        for name in ("btn_close", "btn_close_all", "btn_disconnect"):
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.configure(state="normal")
            except Exception:
                pass
        try:
            if getattr(self, "btn_live", None) is not None:
                self.btn_live.configure(state="normal")
        except Exception:
            pass
        # U2: Live 时禁用读出模式 / bin 下拉；否则 readonly
        for attr in ("_readmode_combo", "_bin_combo"):
            combo = getattr(self, attr, None)
            if combo is None:
                continue
            try:
                combo.configure(state="disabled" if live else "readonly")
            except Exception:
                pass

    def _ensure_live_stopped_blocking(self, timeout_s: float = 45.0) -> bool:
        """在工作线程中：请求 Stop Live 并等待采集线程退完。"""
        if not (
            self._capture_live_running
            or self._capture_live_stopping
            or self._live_threads
        ):
            return True
        gate = threading.Event()

        def _do_stop():
            try:
                self.cap_status_var.set(self.T("closing_busy"))
            except Exception:
                pass
            self._request_stop_live()
            gate.set()

        try:
            self.root.after(0, _do_stop)
        except Exception:
            self._request_stop_live()
            gate.set()
        gate.wait(timeout=5.0)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            threads = [t for t in list(self._live_threads.values()) if t.is_alive()]
            for t in threads:
                t.join(timeout=0.2)
            if not any(t.is_alive() for t in list(self._live_threads.values())):
                if not self._capture_live_running and not self._capture_live_stopping:
                    return True
                # UI 收尾可能还在 after 回调里
                time.sleep(0.05)
                if not self._live_threads and not self._capture_live_running:
                    return True
            time.sleep(0.05)
        return not any(t.is_alive() for t in list(self._live_threads.values()))

    @staticmethod
    def _is_dead_worker_error(exc: BaseException) -> bool:
        msg = str(exc)
        keys = ("采图子进程未运行", "采图子进程已退出", "子进程未运行", "not running", "has exited")
        return any(k in msg for k in keys)

    def _pool_open_resilient(self, pool, name: str):
        """open；若命中死子进程类错误则 close 后重试一次（R1）。"""
        try:
            return pool.open(name)
        except Exception as exc:
            if not self._is_dead_worker_error(exc):
                raise
            self.output_queue.put(f"[pool] {name} 死连接，自动重建...\n")
            try:
                pool.close(name)
            except Exception:
                pass
            cam = pool.open(name)
            try:
                self.root.after(0, lambda n=name: self._set_param_status("pool_rebuild", name=n))
            except Exception:
                pass
            return cam

    def _normalize_readmode_var(self, *_args):
        """校验读出模式；优先对齐下拉标签。非法则回退上次合法值（U1）。"""
        if getattr(self, "_param_edit_silent", False):
            return
        raw = (self.v_readmode.get() or "").strip()
        try:
            idx = self._parse_read_mode(raw)
        except ValueError:
            last = getattr(self, "_last_good_readmode", "0")
            self._param_edit_silent = True
            try:
                self.v_readmode.set(last)
            finally:
                self._param_edit_silent = False
            return
        want = str(idx)
        combo = getattr(self, "_readmode_combo", None)
        if combo is not None:
            try:
                vals = list(combo.cget("values") or ())
            except Exception:
                vals = []
            for v in vals:
                s = str(v)
                if s == str(idx) or s.startswith(f"{idx}:") or s.startswith(f"{idx}："):
                    want = s
                    break
        self._last_good_readmode = want
        if raw != want:
            self._param_edit_silent = True
            try:
                self.v_readmode.set(want)
            finally:
                self._param_edit_silent = False

    def _on_readmode_changed(self, *_args):
        """切换读出模式：ROI 按旧模式尺寸会越界 → 清空让下次 apply 用全幅。
        Live 中改读出模式必须重建会话，提示用户 Stop 后再 Live。"""
        if getattr(self, "_param_edit_silent", False):
            return
        self._normalize_readmode_var()
        self._clear_roi_fields(clear_cache=True)
        if self._live_busy():
            try:
                self._set_param_status("ps_need_stop")
            except Exception:
                pass
            try:
                self.cap_status_var.set(self.T("readmode_live_tip"))
            except Exception:
                pass
            try:
                messagebox.showinfo(self.T("readmode"), self.T("readmode_live_tip"))
            except Exception:
                pass

    def _on_bin_changed(self, *_args):
        """bin 同属会话级：清 ROI；Live 中提示 Stop 再 Live，不走 auto_push（F1）。"""
        if getattr(self, "_param_edit_silent", False):
            return
        self._clear_roi_fields(clear_cache=True)
        if self._live_busy():
            try:
                self._set_param_status("ps_need_stop")
            except Exception:
                pass
            try:
                self.cap_status_var.set(self.T("bin_live_tip"))
            except Exception:
                pass
            try:
                messagebox.showinfo(self.T("bin"), self.T("bin_live_tip"))
            except Exception:
                pass

    def _cam_role(self, cam_name: str | None) -> str | None:
        if not cam_name:
            return None
        for c in self.cameras:
            if c.name == cam_name:
                return c.role
        return None

    def _gps_slave_for_cam(self, cam_name: str | None) -> bool:
        """多机时 GPS 从机标志跟相机角色走，不再共用 GUI 勾选。"""
        role = self._cam_role(cam_name)
        if role == ROLE_SLAVE:
            return True
        if role == ROLE_MASTER:
            return False
        return bool(self.v_gps_slave.get())

    def _settings_from_gui(self, stream_mode: int, cam_name: str | None = None,
                           *, sync_current_roi: bool = True) -> CameraSettings:
        """从 GUI 组设置；ROI / GPS 从机按 cam_name 分别取。"""
        ddr_sel = self.v_ddr.get()
        ddr = None if ddr_sel == "default" else ddr_sel == "on"
        bx, by = self._selected_bin()
        cur = getattr(self, "_param_cam_name", None)
        if sync_current_roi and cur:
            self._store_roi_for_cam(cur)
        roi = self._roi_dict_for_cam(cam_name or cur)
        return CameraSettings(
            stream_mode=stream_mode,
            read_mode=self._read_mode_from_gui(),
            bits=int(self.v_bits.get()),
            exposure_us=float(self.v_exptime.get().strip() or "100000"),
            gain=self._opt_float(self.v_gain.get()),
            offset=self._opt_float(self.v_offset.get()),
            usb_traffic=self._opt_float(self.v_traffic.get()),
            speed=self._opt_float(self.v_speed.get()),
            bin_x=bx, bin_y=by,
            roi_x=int(roi.get("x", 0) or 0),
            roi_y=int(roi.get("y", 0) or 0),
            roi_w=roi.get("w"),
            roi_h=roi.get("h"),
            ddr_enable=ddr,
            gps_enable=bool(self.v_gps_on.get()),
            gps_slave=self._gps_slave_for_cam(cam_name or cur),
            gps_led_cal=bool(self.v_gps_led_cal.get()),
            gps_pos_a=self._opt_int(self.v_gps_pos_a.get()),
            gps_pos_b=self._opt_int(self.v_gps_pos_b.get()),
            gps_pos_width=40 if (w := self._opt_int(self.v_gps_pos_width.get())) is None else w,
        )

    def _target_names(self) -> list[str]:
        """相机名列表，按「参数下发范围」。不再要求已连接：池里会按需打开。"""
        scope = self.v_gps_scope.get()
        if scope == "all":
            return [c.name for c in self.cameras]
        if scope == "masters":
            return [c.name for c in self.cameras if c.role == ROLE_MASTER]
        if scope == "slaves":
            return [c.name for c in self.cameras if c.role == ROLE_SLAVE]
        c = self._selected_cam()
        return [c.name] if c else []

    def apply_params_now(self):
        if self._live_busy():
            messagebox.showinfo(self.T("params"), self.T("need_stop_live"))
            self._set_param_status("ps_need_stop")
            return
        names = self._target_names()
        if not names:
            messagebox.showinfo(self.T("params"), self.T("no_cam_apply"))
            return
        self._store_roi_for_cam(getattr(self, "_param_cam_name", None))
        def work():
            try:
                pool = self._capture_pool()
                for n in names:
                    settings = self._settings_from_gui(STREAM_SINGLE, cam_name=n, sync_current_roi=False)
                    cam = self._pool_open_resilient(pool, n)
                    cam.apply_settings(settings)
                    self.output_queue.put(f"[params] 已下发 {n}\n")
                self.root.after(0, lambda: self.status_var.set(f"参数已下发到 {len(names)} 台"))
            except Exception as exc:
                self.output_queue.put(f"[params] ERROR: {exc}\n")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ param status / live-safe push
    def _set_param_status(self, key: str, **kw):
        """更新左侧「参数状态」行（带 i18n + 格式化）。"""
        try:
            self.param_status_var.set(self.T(key).format(**kw) if kw else self.T(key))
        except Exception:
            pass

    def _busy_begin(self, msg: str, *, total: int | None = None) -> int:
        """Show progress under 参数状态. Returns token; stale ends ignored."""
        self._busy_token = int(getattr(self, "_busy_token", 0)) + 1
        token = self._busy_token

        def ui():
            if token != self._busy_token:
                return
            try:
                if not hasattr(self, "_busy_msg"):
                    self._busy_msg = tk.StringVar(value="")
                self._busy_msg.set(msg)
                self.param_status_var.set(msg)
                try:
                    self.status_var.set(msg)
                except Exception:
                    pass
                bar = getattr(self, "_busy_bar", None)
                if bar is None:
                    return
                try:
                    bar.pack(fill=tk.X, pady=(4, 0))
                except Exception:
                    pass
                if total and total > 0:
                    bar.configure(mode="determinate", maximum=max(int(total), 1), value=0)
                    try:
                        bar.stop()
                    except Exception:
                        pass
                else:
                    bar.configure(mode="indeterminate")
                    try:
                        bar.start(12)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self.root.after(0, ui)
        except Exception:
            ui()
        return token

    def _busy_update(self, token: int, msg: str, *, value: int | None = None,
                     total: int | None = None) -> None:
        def ui():
            if token != getattr(self, "_busy_token", 0):
                return
            try:
                self._busy_msg.set(msg)
                self.param_status_var.set(msg)
                try:
                    self.status_var.set(msg)
                except Exception:
                    pass
                bar = getattr(self, "_busy_bar", None)
                if bar is None:
                    return
                if total and total > 0:
                    bar.configure(mode="determinate", maximum=max(int(total), 1))
                    try:
                        bar.stop()
                    except Exception:
                        pass
                    if value is not None:
                        bar["value"] = max(0, min(int(value), int(total)))
            except Exception:
                pass

        try:
            self.root.after(0, ui)
        except Exception:
            ui()

    def _busy_end(self, token: int, msg: str | None = None, *,
                  status_key: str | None = None, **kw) -> None:
        def ui():
            if token != getattr(self, "_busy_token", 0):
                return
            try:
                bar = getattr(self, "_busy_bar", None)
                if bar is not None:
                    try:
                        bar.stop()
                    except Exception:
                        pass
                    try:
                        bar.pack_forget()
                    except Exception:
                        pass
                if hasattr(self, "_busy_msg"):
                    self._busy_msg.set("")
                if status_key:
                    self._set_param_status(status_key, **kw)
                elif msg:
                    self.param_status_var.set(msg)
                    try:
                        self.status_var.set(msg)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self.root.after(0, ui)
        except Exception:
            ui()

    def _open_live_targets(self) -> list[str]:
        """参数可「实时」下发的目标 = Live/Capture 范围集合 ∩ 池里已打开。

        只要相机在池里开着（live 或 single 会话），apply_runtime_controls 都
        是 live-safe 的（只写曝光/gain/offset/traffic/speed/bits/DDR/GPS，
        不 stop_live、不 re-Init），因此 live 与单帧场景都适用。
        """
        pool = self._cap_pool
        if pool is None:
            return []
        targets = self._live_targets()
        opened = set(pool.opened())
        return [n for n in targets if n in opened]

    def _push_opened_targets(self, live: bool = True) -> None:
        """live/连接中：把 GUI 当前参数「实时」下发到所有已打开的目标相机。

        走 live-safe 的 apply_runtime_controls（只写 exposure/gain/offset/
        usb_traffic/speed/bits/DDR/GPS，不 stop_live、不 re-Init），因此不会
        打断出图。这是修复「live 中改参数不生效」的关键路径。
        """
        if not self.auto_push.get():
            return
        try:
            names = self._open_live_targets()
        except Exception:
            names = []
        if not names:
            return
        settings = self._settings_from_gui(STREAM_LIVE if live else STREAM_SINGLE)
        with self._push_lock:
            def work():
                try:
                    pool = self._capture_pool()
                    applied_any = []
                    for n in names:
                        cam = pool.get(n)
                        if cam is None:
                            continue
                        applied = cam.apply_runtime_controls(settings)
                        applied_any.append(f"{n}:{','.join(applied) or 'ok'}")
                    if applied_any:
                        self._last_settings = settings
                        what = ",".join(sorted({a.split(":", 1)[1].split(",")[0]
                                                for a in applied_any})) or "params"
                        self.root.after(0, lambda: self._set_param_status(
                            "ps_pushed", what=what, n=len(names)))
                    self.output_queue.put(
                        f"[params] live-safe 实时下发 → {', '.join(names)}\n")
                except Exception as exc:
                    self.root.after(0, lambda m=str(exc): self._set_param_status("ps_pushfail", m=m))
                    self.output_queue.put(f"[params] 实时下发失败: {exc}\n")
            threading.Thread(target=work, daemon=True).start()

    def _full_push_selected(self) -> None:
        """「完整下发」：对已打开的目标相机走完整 apply_settings（含 bin/ROI/读出模式）。

        会 stop_live + re-Init，因此只适合非 live（或用户明确要重建会话）时
        用；live 中请靠「实时下发 / 自动下发」走 live-safe 路径。
        """
        if self._live_busy():
            messagebox.showinfo(self.T("params"), self.T("need_stop_live"))
            self._set_param_status("ps_need_stop")
            return
        try:
            names = self._open_live_targets() or self._live_targets()
        except Exception:
            names = []
        if not names:
            self._set_param_status("ps_noconn")
            return
        self._store_roi_for_cam(getattr(self, "_param_cam_name", None))
        token = self._busy_begin(
            self.T("busy_push").format(i=0, n=len(names), name="…"), total=len(names))

        def work():
            try:
                pool = self._capture_pool()
                for i, n in enumerate(names, 1):
                    self._busy_update(
                        token, self.T("busy_push").format(i=i, n=len(names), name=n),
                        value=i - 1, total=len(names))
                    settings = self._settings_from_gui(STREAM_SINGLE, cam_name=n, sync_current_roi=False)
                    cam = self._pool_open_resilient(pool, n)
                    cam.apply_settings(settings)
                    self._busy_update(
                        token, self.T("busy_push").format(i=i, n=len(names), name=n),
                        value=i, total=len(names))
                self._last_settings = settings
                self.output_queue.put(f"[params] 完整下发(重建会话) → {', '.join(names)}\n")
                self._busy_end(
                    token, self.T("busy_push_done").format(n=len(names)),
                    status_key="ps_pushed", what="full(session)", n=len(names))
            except Exception as exc:
                self.root.after(0, lambda m=str(exc): self._set_param_status("ps_pushfail", m=m))
                self.output_queue.put(f"[params] 完整下发失败: {exc}\n")
                self._busy_end(token, f"下发失败: {exc}")
        threading.Thread(target=work, daemon=True).start()


    def _sanitize_save_subdir(self, name: str) -> str | None:
        """Return cleaned single-folder name, or None if empty; raise ValueError if illegal."""
        s = (name or "").strip()
        if not s:
            return None
        bad = set('\\/:*?"<>|')
        if any(ch in bad for ch in s):
            raise ValueError("separators")
        if s in (".", "..") or s.upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise ValueError("reserved")
        return s

    def _base_save_dir(self) -> Path:
        return Path((self.v_save_dir.get() or "").strip() or str(_default_save_dir()))

    def _on_save_subdir_focus_out(self) -> None:
        try:
            sub = self._sanitize_save_subdir(self.v_save_subdir.get())
        except ValueError:
            messagebox.showwarning(self.T("save"), self.T("save_subdir_bad"))
            self.v_save_subdir.set("")
            self._save_subdir_warned = None
            return
        if not sub:
            self._save_subdir_warned = None
            return
        path = self._base_save_dir() / sub
        key = str(path.resolve()) if path.exists() else str(path)
        if path.exists() and self._save_subdir_warned != key:
            ok = messagebox.askyesno(
                self.T("save"),
                self.T("save_subdir_exists").format(path=path),
            )
            if ok:
                self._save_subdir_warned = key
            else:
                self.v_save_subdir.set("")
                self._save_subdir_warned = None

    def _collect_save_meta(self, cam_name: str) -> dict:
        meta: dict = {
            "exposure_us": (self.v_exptime.get() or "").strip(),
            "gain": (self.v_gain.get() or "").strip(),
            "offset": (self.v_offset.get() or "").strip(),
            "usb_traffic": (self.v_traffic.get() or "").strip(),
            "speed": (self.v_speed.get() or "").strip(),
            "bits": (self.v_bits.get() or "").strip(),
            "bin": (self.v_bin.get() or "").strip(),
            "readmode": (self.v_readmode.get() or "").strip(),
        }
        try:
            for row in self.cameras:
                if row.name == cam_name:
                    meta["role"] = getattr(row, "role", "")
                    meta["transport"] = getattr(row, "transport", "")
                    break
        except Exception:
            pass
        return meta

    def _resolve_frame_save_dir(self, cam_name: str) -> Path | None:
        """base [/ subdir] / cam_short — creates dirs; FocusOut already warns if subdir exists."""
        try:
            base = self._base_save_dir()
            try:
                sub = self._sanitize_save_subdir(self.v_save_subdir.get())
            except ValueError:
                self.root.after(0, lambda: messagebox.showwarning(self.T("save"), self.T("save_subdir_bad")))
                return None
            dest = base / sub if sub else base
            cam_dir = dest / short_cam_id(cam_name)
            cam_dir.mkdir(parents=True, exist_ok=True)
            return cam_dir
        except Exception:
            return None

    def _choose_save_dir(self):
        d = filedialog.askdirectory(title=self.T("save_browse"),
                                    initialdir=self.v_save_dir.get() or str(_default_save_dir()))
        if d:
            self.v_save_dir.set(str(Path(d)))

    def _param_config_dir(self) -> Path:
        d = APP_ROOT / "configs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _collect_param_config(self) -> dict:
        """当前 GUI 采集参数 + 常用开关，可存盘复用。"""
        def g(var):
            try:
                return (var.get() or "").strip()
            except Exception:
                return ""

        def gb(var):
            try:
                return bool(var.get())
            except Exception:
                return False

        return {
            "version": 1,
            "kind": "liulsir_capture_params",
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "capture": {
                "readmode": g(self.v_readmode),
                "bits": g(self.v_bits),
                "exposure_us": g(self.v_exptime),
                "gain": g(self.v_gain),
                "offset": g(self.v_offset),
                "usb_traffic": g(self.v_traffic),
                "speed": g(self.v_speed),
                "bin": g(self.v_bin),
                "roi_x": g(self.v_roi_x),
                "roi_y": g(self.v_roi_y),
                "roi_w": g(self.v_roi_w),
                "roi_h": g(self.v_roi_h),
                "roi_by_cam": {
                    k: {"x": v.get("x", 0), "y": v.get("y", 0),
                        "w": v.get("w"), "h": v.get("h")}
                    for k, v in (getattr(self, "_roi_by_cam", {}) or {}).items()
                },
                "ddr": g(self.v_ddr),
            },
            "options": {
                "auto_push": gb(self.auto_push),
                "refresh_before_capture": gb(self.refresh_before_capture),
                "gps_on": gb(self.v_gps_on),
                "filter_burst_waste": gb(self.filter_burst_waste),
                "save_enabled": gb(self.save_enabled),
                "save_interval": g(self.v_save_interval),
                "save_dir": g(self.v_save_dir),
                "save_subdir": g(self.v_save_subdir),
                "live_scope": g(self.live_scope) if hasattr(self, "live_scope") else "all",
            },
        }

    def _apply_param_config(self, cfg: dict) -> None:
        if not isinstance(cfg, dict):
            raise ValueError("not a dict")
        if cfg.get("kind") not in (None, "liulsir_capture_params") and cfg.get("version") not in (None, 1):
            # still try if capture section exists
            pass
        cap = cfg.get("capture") or cfg.get("params") or cfg
        if not isinstance(cap, dict):
            raise ValueError("no capture section")

        mapping = {
            "readmode": "v_readmode",
            "bits": "v_bits",
            "exposure_us": "v_exptime",
            "exptime": "v_exptime",
            "gain": "v_gain",
            "offset": "v_offset",
            "usb_traffic": "v_traffic",
            "traffic": "v_traffic",
            "speed": "v_speed",
            "bin": "v_bin",
            "roi_x": "v_roi_x",
            "roi_y": "v_roi_y",
            "roi_w": "v_roi_w",
            "roi_h": "v_roi_h",
            "ddr": "v_ddr",
        }
        for k, attr in mapping.items():
            if k not in cap:
                continue
            var = getattr(self, attr, None)
            if var is None:
                continue
            try:
                var.set("" if cap[k] is None else str(cap[k]))
            except Exception:
                pass

        opt = cfg.get("options") or {}
        if isinstance(opt, dict):
            bool_map = {
                "auto_push": "auto_push",
                "refresh_before_capture": "refresh_before_capture",
                "gps_on": "v_gps_on",
                "filter_burst_waste": "filter_burst_waste",
                "save_enabled": "save_enabled",
            }
            for k, attr in bool_map.items():
                if k not in opt:
                    continue
                var = getattr(self, attr, None)
                if var is None:
                    continue
                try:
                    var.set(bool(opt[k]))
                except Exception:
                    pass
            str_map = {
                "save_interval": "v_save_interval",
                "save_dir": "v_save_dir",
                "save_subdir": "v_save_subdir",
            }
            for k, attr in str_map.items():
                if k not in opt:
                    continue
                var = getattr(self, attr, None)
                if var is None:
                    continue
                try:
                    var.set("" if opt[k] is None else str(opt[k]))
                except Exception:
                    pass
            if "live_scope" in opt and hasattr(self, "live_scope"):
                try:
                    v = str(opt["live_scope"] or "all")
                    if v in ("all", "current"):
                        self.live_scope.set(v)
                except Exception:
                    pass

        # 每机 ROI 缓存
        rbc = cfg.get("roi_by_cam") if isinstance(cfg, dict) else None
        if isinstance(rbc, dict):
            out = {}
            for k, v in rbc.items():
                if not isinstance(v, dict):
                    continue
                out[str(k)] = {
                    "x": int(v.get("x", 0) or 0),
                    "y": int(v.get("y", 0) or 0),
                    "w": v.get("w"),
                    "h": v.get("h"),
                }
            self._roi_by_cam = out
            cur = getattr(self, "_param_cam_name", None)
            if cur:
                self._load_roi_for_cam(cur)


    def _save_param_config(self) -> None:
        initial = self._param_config_dir()
        path = filedialog.asksaveasfilename(
            title=self.T("cfg_save_title"),
            initialdir=str(initial),
            initialfile="capture_params.json",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            cfg = self._collect_param_config()
            Path(path).write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.output_queue.put(self.T("cfg_saved").format(path=path) + "\n")
        except Exception as exc:
            messagebox.showerror(self.T("cfg_save_title"), str(exc))

    def _load_param_config(self) -> None:
        initial = self._param_config_dir()
        path = filedialog.askopenfilename(
            title=self.T("cfg_load_title"),
            initialdir=str(initial),
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                raise ValueError("root not object")
            self._apply_param_config(cfg)
            self.output_queue.put(self.T("cfg_loaded").format(path=path) + "\n")
        except Exception as exc:
            messagebox.showerror(self.T("cfg_load_title"), f"{self.T('cfg_bad')}\n{exc}")

    def _save_frame(self, frame, cam_name: str) -> None:
        """单帧 Capture 用：勾选存储则存一张（不受 N 帧节拍限制）。"""
        try:
            if not self.save_enabled.get():
                return
        except Exception:
            return
        with self._frame_seq_lock:
            self._frame_seq += 1
            seq = self._frame_seq
        self._save_frame_live(cam_name, frame, seq=seq)

    def _reset_save_progress(self) -> None:
        """Live / 新一轮存图开始时清零进度与丢帧计数。"""
        with self._save_stats_lock:
            self._save_enqueued = 0
            self._save_done = 0
            self._save_fail = 0
            self._save_drop_n = 0
        self._save_drop_log_t = 0.0
        self._schedule_save_progress_ui()

    def _note_save_drop(self, cam_name: str = "", *, reason: str = "backlog") -> None:
        """记一次因积压未存的帧，并触发预警 UI。"""
        with self._save_stats_lock:
            self._save_drop_n += 1
            n = self._save_drop_n
        now = time.perf_counter()
        if now - float(getattr(self, "_save_drop_log_t", 0.0) or 0.0) >= 1.0:
            self._save_drop_log_t = now
            cn = cam_name or "?"
            self.root.after(
                0,
                lambda nn=n, c=cn, r=reason: self.output_queue.put(
                    f"[save] 丢帧预警: 已丢 {nn}（{c}, {r}）；加大 N / 用 ROI\n"
                ),
            )
        self._schedule_save_progress_ui()

    def _note_save_enqueued(self) -> None:
        with self._save_stats_lock:
            self._save_enqueued += 1
        self._schedule_save_progress_ui()

    def _note_save_result(self, ok: bool) -> None:
        with self._save_stats_lock:
            if ok:
                self._save_done += 1
            else:
                self._save_fail += 1
        self._schedule_save_progress_ui()

    def _schedule_save_progress_ui(self) -> None:
        if getattr(self, "_save_ui_pending", False):
            return
        self._save_ui_pending = True
        try:
            self.root.after(50, self._flush_save_progress_ui)
        except Exception:
            self._save_ui_pending = False

    def _flush_save_progress_ui(self) -> None:
        self._save_ui_pending = False
        try:
            with self._save_stats_lock:
                done = int(self._save_done)
                fail = int(self._save_fail)
                enq = int(self._save_enqueued)
                drop = int(self._save_drop_n)
                q_bytes = int(self._save_q_bytes)
            try:
                q = int(self._save_q.qsize())
            except Exception:
                q = 0
            room = self._save_q_room_frames()
            mib = max(0, int(round(q_bytes / (1024 * 1024))))
            # 进度：已完成(成功+失败) / 已入队；队列中未完成的不算进分子
            finished = done + fail
            total = max(enq, finished)
            if total <= 0 and drop <= 0 and q <= 0:
                pct = 0
                self._save_prog_var.set(self.T("save_prog_idle"))
            else:
                pct = int(round(100.0 * finished / total)) if total else 0
                pct = max(0, min(100, pct))
                self._save_prog_var.set(
                    self.T("save_prog_fmt").format(
                        done=done, total=total, pct=pct, q=q, mib=mib, room=room, drop=drop
                    )
                )
            bar = getattr(self, "_save_bar", None)
            if bar is not None:
                bar["value"] = pct
            warn = getattr(self, "_save_warn_var", None)
            if warn is not None:
                if drop > 0:
                    warn.set(self.T("save_drop_warn").format(n=drop))
                elif room <= 8:
                    warn.set(self.T("save_room_warn").format(room=room))
                else:
                    warn.set("")
            if drop > 0:
                try:
                    self.cap_status_var.set(self.T("save_drop_warn").format(n=drop))
                except Exception:
                    pass
            elif room <= 8 and (enq > 0 or q > 0):
                try:
                    self.cap_status_var.set(self.T("save_room_warn").format(room=room))
                except Exception:
                    pass
        except Exception:
            pass

    def _avail_ram_bytes(self) -> int:
        """当前大致可用物理内存（失败则给一个保守默认）。"""
        try:
            import ctypes
            class _MEM(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            m = _MEM()
            m.dwLength = ctypes.sizeof(_MEM)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullAvailPhys)
        except Exception:
            pass
        return 2 * 1024 * 1024 * 1024

    def _save_q_room_frames(self) -> int:
        """按可用 RAM 与近期帧大小，估算缓存大概还能再收几张（仅预警，不截流）。"""
        with self._save_stats_lock:
            last = int(self._save_q_last_frame_bytes or 0)
            queued = int(self._save_q_bytes or 0)
        if last <= 0:
            last = 8 * 1024 * 1024  # ROI/未知时的占位
        # 给系统/预览留余量：只用可用物理内存的一半估算
        avail = max(0, self._avail_ram_bytes() // 2 - queued)
        return max(0, int(avail // last))

    def _save_frame_live(self, cam_name: str, frame, *, save_dir: str | None = None,
                         seq: int | None = None) -> None:
        """实际落盘：像素拷走后进无无界**写盘队列，由固定 2 个写盘线程慢慢落盘。

        Live 路径里 save_this 已在循环外按「每 N 帧」决定；这里只负责排队写文件。
        目录：Folder[/Subfolder]/相机短名/；旁路同名 .json 写基础参数。
        不因队列长度丢帧；仅拷帧 MemoryError 等异常才记 drop，并预警剩余可缓存张数。
        """
        if seq is None:
            with self._frame_seq_lock:
                self._frame_seq += 1
                seq = self._frame_seq
        meta = self._collect_save_meta(cam_name)
        if save_dir is None:
            resolved = self._resolve_frame_save_dir(cam_name)
            if resolved is None:
                return
            save_dir = str(resolved)
        else:
            # explicit override still nests under camera short name
            save_dir = str(Path(save_dir) / short_cam_id(cam_name))
            Path(save_dir).mkdir(parents=True, exist_ok=True)

        # 立刻拷成独立 bytes，再交给写盘线程；调用方随后可丢掉 frame.data
        try:
            raw0 = getattr(frame, "data", None)
            if raw0 is None:
                data = b""
            elif isinstance(raw0, (bytes, bytearray)):
                data = bytes(raw0)
            else:
                data = bytes(raw0)
        except MemoryError:
            self._note_save_drop(cam_name, reason="MemoryError")
            return
        except Exception:
            data = b""

        from types import SimpleNamespace
        fr = SimpleNamespace(
            width=int(getattr(frame, "width", 0) or 0),
            height=int(getattr(frame, "height", 0) or 0),
            bpp=int(getattr(frame, "bpp", 8) or 8),
            channels=max(1, int(getattr(frame, "channels", 1) or 1)),
            data=data,
            gps=getattr(frame, "gps", None),
        )
        nbytes = len(data)
        item = (fr, save_dir, cam_name, seq, meta, nbytes)
        self._save_q.put(item)
        with self._save_stats_lock:
            self._save_q_bytes += nbytes
            if nbytes > 0:
                self._save_q_last_frame_bytes = nbytes
        self._note_save_enqueued()
        # 剩余容量偏低时打节流日志（不丢帧）
        room = self._save_q_room_frames()
        if room <= 8:
            now = time.perf_counter()
            if now - float(getattr(self, "_save_room_warn_t", 0.0) or 0.0) >= 1.0:
                self._save_room_warn_t = now
                self.root.after(
                    0,
                    lambda r=room: self.output_queue.put(
                        f"[save] 缓存预警: 大约还能再收 {r} 张（写盘仍在追）\n"
                    ),
                )

    def _save_q_room(self) -> bool:
        """兼容旧调用：无界队列始终允许拉全幅；仅用剩余估算做 UI 预警。"""
        return True

    def _save_worker_loop(self) -> None:
        while True:
            try:
                item = self._save_q.get()
            except Exception:
                continue
            if item is None:
                try:
                    self._save_q.task_done()
                except Exception:
                    pass
                break
            if len(item) >= 6:
                fr, sd, cn, sq, mt, nbytes = item[:6]
            else:
                fr, sd, cn, sq, mt = item[:5]
                nbytes = len(getattr(fr, "data", b"") or b"")
            try:
                p = save_frame_to_file(fr, sd, cn, seq=sq, meta=mt)
            except Exception as exc:
                self._note_save_result(False)
                self.root.after(
                    0,
                    lambda e=exc, c=cn: self.output_queue.put(
                        f"[save] 保存失败: {c}: {type(e).__name__}: {e}\n"
                    ),
                )
            else:
                if p is not None:
                    self._note_save_result(True)
                    self.root.after(
                        0, lambda path=p: self.output_queue.put(f"[save] {path}\n")
                    )
                else:
                    self._note_save_result(False)
                    reason = getattr(fr, "_save_err", None) or "unknown"
                    self.root.after(
                        0,
                        lambda r=reason, c=cn: self.output_queue.put(
                            f"[save] 保存失败: {c}: {r}\n"
                        ),
                    )
            finally:
                # 尽快松开大块像素，并扣减缓存字节计数
                try:
                    fr.data = b""
                except Exception:
                    pass
                with self._save_stats_lock:
                    self._save_q_bytes = max(0, int(self._save_q_bytes) - int(nbytes or 0))
                try:
                    self._save_q.task_done()
                except Exception:
                    pass

    def _reset_save_cnt(self, cam_name: str | None = None):
        with self._frame_seq_lock:
            if cam_name is None:
                self._save_cnt = {}
            else:
                self._save_cnt = {k: v for k, v in getattr(self, "_save_cnt", {}).items()
                                  if k != cam_name}

    def _bind_auto_push_traces(self):
        """参数输入框改动 → 防抖 → live-safe 实时下发到已打开的目标相机。

        只绑 live-safe 运行时参数（曝光/gain/offset/traffic/speed/bits/GPS）。
        这些可以不打断 live 就写到相机；bin/ROI/读出模式属于会话级，需要
        「完整下发」或重启 Live 才生效（避免 live 中误触发 re-Init 把流打停）。
        """
        debounce_ms = 300
        self._auto_push_timer: int | None = None

        def _schedule(*_args):
            if getattr(self, "_param_edit_silent", False):
                return
            if self._auto_push_timer is not None:
                try:
                    self.root.after_cancel(self._auto_push_timer)
                except Exception:
                    pass
            self._auto_push_timer = self.root.after(
                debounce_ms, lambda: self._push_opened_targets(live=True))

        for var in (self.v_exptime, self.v_gain, self.v_offset,
                    self.v_traffic, self.v_speed, self.v_bits):
            try:
                var.trace_add("write", _schedule)
            except Exception:
                pass
        for var in (self.v_gps_on, self.v_gps_slave, self.v_gps_led_cal):
            try:
                var.trace_add("write", _schedule)
            except Exception:
                pass

    def run_query_resolution(self):
        c = self._selected_cam()
        if c is None:
            messagebox.showinfo(self.T("params"), self.T("need_select_cam"))
            return
        name = c.name
        def work():
            try:
                pool = self._capture_pool()
                cam = self._pool_open_resilient(pool, name)
                bx, by = self._selected_bin()
                geo = cam.refresh_geometry(read_mode=self._read_mode_from_gui(), bin_x=bx, bin_y=by)
                full = geo.get("full_frame", {})
                self.root.after(0, lambda: self.v_res_info.set(
                    f"全幅: {full.get('size_x')}x{full.get('size_y')}  (未bin {full.get('unbinned_w')}x{full.get('unbinned_h')}, bin {bx}x{by})"))
            except Exception as exc:
                self.output_queue.put(f"[res] ERROR: {exc}\n")
        threading.Thread(target=work, daemon=True).start()

    def read_params_from_cam(self, name: str | None = None, auto: bool = False):
        """回读相机实际参数：读出模式/分辨率/ROI/曝光/Gain/Offset 等刷到 GUI。

        走采图子进程的 camera_info（内部 _prepare_session → Init，与 Live/
        Capture 同一会话，不会另开第二套 SDK 资源）。Live / 单帧 Capture 已经
        把相机自动打开，因此这里无需先手动 Connect：直接按名字 open（已开则复用），
        非 Live 时进 SINGLE 会话后回读。

        auto=True（由 Live / 单帧 启动时自动触发）不弹「未选中相机」提示，
        没有选中相机则静默跳过。
        """
        if name is None:
            c = self._selected_cam()
            if c is None:
                if not auto:
                    messagebox.showinfo(self.T("params"), self.T("need_select_cam"))
                return
            name = c.name
        self._run_readback(name)

    def _apply_readback_to_gui(self, name: str, info: dict, geo: dict | None,
                                *, status: bool = True) -> None:
        """把 camera_info / geometry 填进采集参数区（须在 Tk 线程或持 silent 标志）。

        用 `_param_edit_silent` 包住，避免触发 auto_push 连环下发。
        """
        full = (geo or {}).get("full_frame") or {}
        bx, by = self._selected_bin()
        bxv = int(full.get("bin_x", bx) or bx)
        byv = int(full.get("bin_y", by) or by)
        self._param_edit_silent = True
        try:
            modes = [m for m in (info.get("read_modes") or []) if isinstance(m, dict)]
            if modes:
                labels = [f"{m.get('index')}: {m.get('name')}" for m in modes]
                cur = self._read_mode_from_gui()
                want = next((lb for lb in labels if lb.startswith(f"{cur}:")), labels[0])
                combo = self._readmode_combo if hasattr(self, "_readmode_combo") else None
                if combo is not None:
                    combo["values"] = labels
                self.v_readmode.set(want)
                self._last_good_readmode = want
            self.v_bin.set(f"{bxv}x{byv}")
            if full:
                self.v_res_info.set(
                    f"全幅: {full.get('size_x')}x{full.get('size_y')} "
                    f"(未bin {full.get('unbinned_w')}x{full.get('unbinned_h')}, "
                    f"bin {bxv}x{byv})")
            if name in self._roi_by_cam:
                d = self._roi_by_cam[name]
                self._set_roi_fields(
                    int(d.get("x", 0) or 0), int(d.get("y", 0) or 0),
                    d.get("w"), d.get("h"), silent=True,
                )
            elif full:
                self._set_roi_fields(
                    int(full.get("start_x", 0) or 0),
                    int(full.get("start_y", 0) or 0),
                    full.get("size_x"), full.get("size_y"), silent=True,
                )
            controls = info.get("controls") or {}

            def setnum(var, val):
                if isinstance(val, (int, float)):
                    var.set(str(int(val)) if float(val) == int(val) else f"{val:g}")

            exp = (controls.get("exposure") or {}).get("value")
            if isinstance(exp, (int, float)) and exp > 0:
                setnum(self.v_exptime, exp)
            setnum(self.v_gain, (controls.get("gain") or {}).get("value"))
            setnum(self.v_offset, (controls.get("offset") or {}).get("value"))
            setnum(self.v_traffic, (controls.get("usb_traffic") or {}).get("value"))
            setnum(self.v_speed, (controls.get("speed") or {}).get("value"))
            bits = (controls.get("bits") or {}).get("value")
            if isinstance(bits, (int, float)) and int(bits) in (8, 16):
                self.v_bits.set(str(int(bits)))
            if status:
                self.status_var.set(self.T("readback_done").format(name=name))
                self._set_param_status("ps_readback", name=name)
        finally:
            self._param_edit_silent = False

    def _fetch_cam_params(self, name: str) -> tuple[dict, dict | None, int, int, int]:
        """open→(非 live 则 SINGLE 会话)→geometry/camera_info。可在工作线程调用。

        返回 (info, geo, read_mode, bin_x, bin_y)。
        """
        pool = self._capture_pool()
        cam = self._pool_open_resilient(pool, name)
        rm = self._read_mode_from_gui()
        bx, by = self._selected_bin()
        if not getattr(cam, "_live", False):
            # 非 Live：按当前读出模式进 SINGLE 会话（Init）后回读
            cam._prepare_session(rm, STREAM_SINGLE)
        geo = cam.refresh_geometry(read_mode=rm, bin_x=bx, bin_y=by)
        info = cam.camera_info()
        return info, geo, rm, bx, by

    def _run_readback(self, name: str) -> None:
        """后台线程里真正执行一次回读（open→会话→读回→刷 GUI）。

        Live 中直接读当前值（不 re-init，不打断出图）；非 Live 按当前读出模式
        进 SINGLE 会话后读。用于手动 Read back / Connect 后 / Live 起流后 / 单帧后。
        """
        def work():
            try:
                info, geo, rm, bx, by = self._fetch_cam_params(name)
                full = (geo or {}).get("full_frame") or {}
                bxv = int(full.get("bin_x", bx) or bx)
                byv = int(full.get("bin_y", by) or by)

                def refresh_ui():
                    self._apply_readback_to_gui(name, info, geo)

                self.root.after(0, refresh_ui)
                self.output_queue.put(
                    f"[params] {name} 已读回: readmode={rm} "
                    f"bin={bxv}x{byv} ROI={self.v_roi_x.get()},{self.v_roi_y.get()}"
                    f" {self.v_roi_w.get()}x{self.v_roi_h.get()} "
                    f"exp={self.v_exptime.get()}us gain={self.v_gain.get()}\n")
            except Exception as exc:
                self.output_queue.put(f"[params] 读回 {name} 失败: {exc}\n")
        threading.Thread(target=work, daemon=True).start()


    def _burst_window_value(self) -> int:
        """FPGA 51/52 burst 结束（一组帧数-1 / end）。"""
        try:
            return self._opt_int_sync(self.v_window, 11) & 0xFFFF
        except Exception:
            return 11

    def _burst_start_value(self) -> int:
        """FPGA 50 burst 开始（单字节）。非法时回退默认 1。"""
        try:
            v = self._opt_int_sync(self.v_burst_start, 1)
            if v < 0 or v > 0xFF:
                return 1
            return int(v) & 0xFF
        except Exception:
            return 1

    def _burst_expected_seq_bounds(self) -> tuple[int, int, int] | None:
        """期望 GPS 帧序号范围：burststart+1 .. burstend-1（开区间）。

        返回 ``(lo, hi, period)``，``period = burstend+1`` 用于把绝对 seq 映射到组内索引。
        连续跑 / 非法区间返回 None（不报假丢失）。
        """
        start = self._burst_start_value()
        end = self._burst_window_value()
        if end <= 0 or end >= 0xFFFF:
            return None
        lo = int(start) + 1
        hi = int(end) - 1
        if hi < lo:
            return None
        period = int(end) + 1
        if period <= 1:
            return None
        return lo, hi, period

    def burst_expected_range_label(self) -> str:
        bounds = self._burst_expected_seq_bounds()
        if bounds is None:
            return "(无有效期望范围)"
        lo, hi, _period = bounds
        return f"{lo}..{hi}"

    def missing_burst_frame_seqs(
        self,
        seq_list: list[int],
        *,
        frozen: bool = False,
    ) -> list[int]:
        """对比本轮 seq_list 与期望集合 {burststart+1..burstend-1}，返回缺失序号。

        绝对 GPS 计数经 ``seq % (burstend+1)`` 映射到组内索引后再比（与废帧过滤同周期）。
        收集未越过期望上界前，只报高水位之下的空洞，避免把「尚未到达」当成丢失。
        """
        if not seq_list:
            return []
        bounds = self._burst_expected_seq_bounds()
        if bounds is None:
            return []
        lo, hi, period = bounds
        expected = set(range(lo, hi + 1))
        observed: set[int] = set()
        for raw in seq_list:
            try:
                observed.add((int(raw) & 0xFFFFFFFF) % period)
            except Exception:
                continue
        if not observed:
            return []
        missing = expected - observed
        if not missing:
            return []
        hi_obs = max(observed)
        if (not frozen) and hi_obs < hi:
            # 仍在收帧：只列出已经越过的空洞
            return sorted(x for x in missing if x < hi_obs)
        return sorted(missing)

    def _gps_seq_of_frame(self, frame) -> int | None:
        """从帧对象取 GPS seq；必要时用帧头解析。无 GPS 返回 None。"""
        gps = getattr(frame, "gps", None)
        data = getattr(frame, "data", b"") or b""
        if gps is None and len(data) >= 44:
            try:
                fw = int(getattr(frame, "width", 0) or 0) or None
                fh = int(getattr(frame, "height", 0) or 0) or None
                gps = parse_gps_header(data, expect_w=fw, expect_h=fh)
                try:
                    frame.gps = gps
                except Exception:
                    pass
            except Exception:
                return None
        if gps is None:
            return None
        try:
            return int(gps.seq_number) & 0xFFFFFFFF
        except Exception:
            return None

    def _should_discard_burst_waste(self, frame) -> tuple[bool, int | None]:
        """是否丢弃同步 burst 废帧。

        判定：``is_burst_waste_frame(seq, end, start)`` —
        一组周期 ``end+1``（序号 ``0..end``），保留 ``[start, end]``，
        丢弃 ``idx = seq % (end+1)`` 且 ``idx < start``（默认 start=1 → 丢 0、12…）。
        无 GPS seq 时不丢（只打一次日志）；开关关闭时不丢。
        """
        try:
            enabled = bool(self.filter_burst_waste.get())
        except Exception:
            enabled = True
        if not enabled:
            return False, None
        seq = self._gps_seq_of_frame(frame)
        if seq is None:
            if not getattr(self, "_burst_waste_no_gps_logged", False):
                self._burst_waste_no_gps_logged = True
                try:
                    self.output_queue.put(
                        "[burst] waste filter skipped (no GPS seq); "
                        "enable GPS on / SetGPSOn for seq-based discard\n")
                except Exception:
                    pass
            return False, None
        end = self._burst_window_value()
        start = self._burst_start_value()
        if is_burst_waste_frame(seq, end, start):
            return True, seq
        return False, seq

    def fill_roi_full(self):
        """只把当前机全幅几何填进 ROI 输入框并写入该机缓存（不下发）。"""
        c = self._selected_cam()
        if c is None:
            messagebox.showinfo(self.T("params"), self.T("need_select_cam"))
            return
        name = c.name
        def work():
            try:
                pool = self._capture_pool()
                cam = self._pool_open_resilient(pool, name)
                bx, by = self._selected_bin()
                geo = cam.refresh_geometry(read_mode=self._read_mode_from_gui(), bin_x=bx, bin_y=by)
                full = geo["full_frame"]
                self.root.after(0, lambda: (
                    self._set_roi_fields(
                        int(full["start_x"]), int(full["start_y"]),
                        int(full["size_x"]), int(full["size_y"])),
                    self._store_roi_for_cam(
                        getattr(self, "_param_cam_name", None)
                        or (c.name if c else None)),
                ))
            except Exception as exc:
                self.output_queue.put(f"[roi] ERROR: {exc}\n")
        threading.Thread(target=work, daemon=True).start()

    def _current_roi_origin(self, cam_name: str | None = None) -> tuple[int, int]:
        """该机 ROI 起点；空宽高视为全幅起点 0,0。"""
        d = self._roi_dict_for_cam(cam_name or getattr(self, "_param_cam_name", None))
        w, h = d.get("w"), d.get("h")
        if w is None and h is None:
            return 0, 0
        return int(d.get("x", 0) or 0), int(d.get("y", 0) or 0)

    def apply_roi_selection(self, cam_name: str, sx: int, sy: int, sw: int, sh: int) -> None:
        """预览框选 → 只写入该机 ROI；Live 中提示 Stop，否则只对该机下发。"""
        cur = getattr(self, "_param_cam_name", None)
        if cur and cur != cam_name:
            self._store_roi_for_cam(cur)
        self._roi_by_cam[cam_name] = {"x": int(sx), "y": int(sy), "w": int(sw), "h": int(sh)}
        self._param_cam_name = cam_name
        self._set_roi_fields(sx, sy, sw, sh)
        # 避免 selection 触发的回读与 ROI 下发抢同一台会话（从机上尤其容易失败）
        self._suppress_cam_switch_readback = True
        try:
            for i, c in enumerate(self.cameras):
                if c.name == cam_name:
                    kids = self.cam_tree.get_children()
                    if i < len(kids):
                        self.cam_tree.selection_set(kids[i])
                        self._sel_idx.set(i)
                    try:
                        self.v_gps_slave.set(c.role == ROLE_SLAVE)
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        finally:
            self.root.after(
                800,
                lambda: setattr(self, "_suppress_cam_switch_readback", False),
            )
        msg = self.T("roi_applied_cam").format(
            cam=short_cam_id(cam_name), x=sx, y=sy, w=sw, h=sh
        )
        self.output_queue.put(f"[roi] {msg}\n")
        self.param_status_var.set(msg)
        if self._live_busy():
            messagebox.showinfo(self.T("roi"), self.T("roi_need_stop"))
            self._set_param_status("roi_need_stop")
            return
        self.apply_roi_now(names=[cam_name])

    def apply_roi_now(self, names: list[str] | None = None) -> None:
        """按机分别下发各自缓存的 ROI（会话级）；单台失败不阻断其它台。"""
        if self._live_busy():
            messagebox.showinfo(self.T("params"), self.T("roi_need_stop"))
            self._set_param_status("roi_need_stop")
            return
        self._store_roi_for_cam(getattr(self, "_param_cam_name", None))
        if names is None:
            names = self._target_names()
            if not names:
                c = self._selected_cam()
                names = [c.name] if c else []
        if not names:
            messagebox.showinfo(self.T("params"), self.T("no_cam_apply"))
            return
        bundle: list[tuple[str, object]] = []
        for n in names:
            try:
                bundle.append(
                    (n, self._settings_from_gui(STREAM_SINGLE, cam_name=n, sync_current_roi=False))
                )
            except Exception as exc:
                self.output_queue.put(f"[roi] 组参失败 {n}: {exc}\n")
        if len(bundle) > 1:
            shapes = []
            for n, s in bundle:
                shapes.append((n, getattr(s, "roi_w", None), getattr(s, "roi_h", None)))
            ws = {w for _, w, _ in shapes}
            hs = {h for _, _, h in shapes}
            if len(ws) > 1 or len(hs) > 1:
                detail = ", ".join(
                    f"{short_cam_id(n)}={w}x{h}" for n, w, h in shapes
                )
                self.output_queue.put(
                    f"[roi] 警告: 多机 ROI 尺寸不一致（{detail}），"
                    f"全幅机更易 USB 争用导致单机 timeout；请为每台分别框选或统一恢复全幅\n"
                )
        if not bundle:
            return
        token = self._busy_begin(self.T("apply_roi"), total=len(bundle))

        def work():
            ok_n = 0
            fail_n = 0
            last_ok = None
            try:
                pool = self._capture_pool()
                for i, (n, settings) in enumerate(bundle, 1):
                    self.root.after(
                        0,
                        lambda ii=i, nn=n, tok=token: self._busy_update(
                            tok,
                            self.T("apply_roi") + f" {ii}/{len(bundle)} {short_cam_id(nn)}",
                            value=ii,
                            total=len(bundle),
                        ),
                    )
                    try:
                        cam = self._pool_open_resilient(pool, n)
                        cam.apply_settings(settings)
                        ok_n += 1
                        last_ok = settings
                        role = self._cam_role(n)
                        self.output_queue.put(
                            f"[roi] applied {n} (role={role}, gps_slave={settings.gps_slave}): "
                            f"{settings.roi_x},{settings.roi_y} "
                            f"{settings.roi_w}x{settings.roi_h}\n"
                        )
                    except Exception as exc:
                        fail_n += 1
                        self.output_queue.put(
                            f"[roi] FAIL {n}: {type(exc).__name__}: {exc}\n"
                        )
                if last_ok is not None:
                    self.root.after(0, lambda s=last_ok: self.status_var.set(
                        self.T("roi_applied").format(
                            x=s.roi_x, y=s.roi_y, w=s.roi_w or 0, h=s.roi_h or 0,
                        )
                    ))
                self.output_queue.put(f"[roi] 完成：成功 {ok_n}，失败 {fail_n}\n")
                if fail_n and not ok_n:
                    self.root.after(
                        0,
                        lambda: self._set_param_status("ps_pushfail", m=f"ROI fail x{fail_n}"),
                    )
            finally:
                self.root.after(0, lambda tok=token: self._busy_end(tok))

        threading.Thread(target=work, daemon=True).start()

    def restore_full_frame(self) -> None:
        """目标范围内每台各自恢复全幅 ROI；Live 中只改缓存并提示 Stop。"""
        names = self._target_names()
        c = self._selected_cam()
        if not names:
            names = [c.name] if c else []
        if not names:
            messagebox.showinfo(self.T("params"), self.T("need_select_cam"))
            return
        for n in names:
            self._roi_by_cam[n] = {"x": 0, "y": 0, "w": None, "h": None}
        cur = getattr(self, "_param_cam_name", None) or names[0]
        self._load_roi_for_cam(cur)
        self.output_queue.put(f"[roi] {self.T('roi_restored')} ({len(names)} cams)\n")
        self.param_status_var.set(self.T("roi_restored"))
        if self._live_busy():
            messagebox.showinfo(self.T("params"), self.T("roi_need_stop"))
            self._set_param_status("roi_need_stop")
            return

        def work():
            try:
                pool = self._capture_pool()
                bx, by = self._selected_bin()
                rm = self._read_mode_from_gui()
                for n in names:
                    cam = self._pool_open_resilient(pool, n)
                    try:
                        geo = cam.refresh_geometry(read_mode=rm, bin_x=bx, bin_y=by)
                        full = geo["full_frame"]
                        self._roi_by_cam[n] = {
                            "x": int(full["start_x"]),
                            "y": int(full["start_y"]),
                            "w": int(full["size_x"]),
                            "h": int(full["size_y"]),
                        }
                    except Exception as exc:
                        self.output_queue.put(f"[roi] {n} geo failed, empty ROI: {exc}\n")
                        self._roi_by_cam[n] = {"x": 0, "y": 0, "w": None, "h": None}
                    settings = self._settings_from_gui(STREAM_SINGLE, cam_name=n, sync_current_roi=False)
                    cam.apply_settings(settings)
                    self.output_queue.put(
                        f"[roi] restored full {n}: {settings.roi_w}x{settings.roi_h}\n"
                    )

                def sync_gui():
                    self._load_roi_for_cam(getattr(self, "_param_cam_name", None) or names[0])

                self.root.after(0, sync_gui)
            except Exception as exc:
                self.output_queue.put(f"[roi] restore ERROR: {exc}\n")

        threading.Thread(target=work, daemon=True).start()


    # ------------------------------------------------------------- capture
    def _capture_pool(self) -> CaptureWorkerPool:
        if self._cap_pool is None:
            self._cap_pool = CaptureWorkerPool(
                self.project_root, log=lambda s: self.output_queue.put(str(s) + "\n"))
        return self._cap_pool

    # ------------------------------------------------------------- preview pipeline
    def _prepare_preview_base(self, frame, *, view: dict | None = None):
        """采集线程调用：把一帧备成原图缓存 (dw, dh, gray) + 统计/GPS 文本。

        与原始 GUI 一致：live 用 preview_max_dim 预缩放；GPS 在主机侧解析头
        (采图线程不碰 Tk)。返回 (dw, dh, gray, stats, frame, gps_text, dw, dh, disp, zoom_label)。
        """
        live = self._capture_live_running
        native_max = self._live_preview_native_max_dim if live else self._preview_native_max_dim
        display_max = self._live_preview_display_max_dim if live else self._preview_display_max_dim
        view = dict(view or {"brightness": 0.0, "contrast": 1.0, "black": 0, "white": 255,
                             "zoom": "Fit", "canvas_w": 480, "canvas_h": 360, "gps": False})
        if getattr(frame, "preview_gray", b"") and int(getattr(frame, "preview_w", 0) or 0) > 0:
            dw, dh, gray = int(frame.preview_w), int(frame.preview_h), frame.preview_gray
        else:
            dw, dh, gray = downscale_gray8(frame, max_dim=native_max)
        if dw <= 0 or dh <= 0:
            return None
        if live:
            vmin = vmax = 0
            mean = std = 0.0
        else:
            vmin, vmax, mean, std = gray8_quick_stats(gray)
        gps_wanted = bool(view.get("gps"))
        gps_obj = getattr(frame, "gps", None)
        parse_err = ""
        fw = int(getattr(frame, "width", 0) or 0)
        fh = int(getattr(frame, "height", 0) or 0)
        if gps_wanted and gps_obj is None and len(getattr(frame, "data", b"") or b"") >= 44:
            try:
                gps_obj = parse_gps_header(frame.data, expect_w=fw or None, expect_h=fh or None)
                frame.gps = gps_obj
            except Exception as exc:
                parse_err = str(exc)
                gps_obj = None
        gps_issues = (
            gps_header_issues(gps_obj, expect_w=fw or None, expect_h=fh or None)
            if gps_obj is not None
            else ["gps is None"]
        )
        # 帧序号门禁：有解析结果即可累计。无 GNSS 锁时 timestamps/经纬常为 0，
        # 不能因此丢掉 FrameCounter；仅 overlay 尺寸明显不符才当假头。
        try:
            severe = [
                i for i in gps_issues
                if i == "gps is None" or str(i).startswith("overlay=")
            ]
            frame._gps_seq_valid = bool(
                gps_wanted and gps_obj is not None and not severe
            )
        except Exception:
            pass
        if not gps_wanted:
            gps_text = "GPS off."
            gps_short = ""
        elif parse_err:
            gps_text = f"GPS parse failed: {parse_err}"
            gps_short = " | GPS parse failed"
        else:
            # 预览默认短序号；勾选「GPS 详情」才用完整 format_gps_report
            gps_text = format_gps_seq_brief(gps_obj, wanted=True)
            if gps_obj is None or gps_issues:
                gps_text += "\nraw[0:48] " + bytes(frame.data[:48]).hex()
            if gps_obj is not None and not gps_issues:
                seq = int(gps_obj.seq_number) & 0xFFFFFFFF
                gps_short = f" | seq={seq}"
            elif gps_obj is not None:
                gps_short = " | GPS (bad header, seq ignored)"
            else:
                gps_short = " | GPS (no overlay)"
        bpp_bytes = max(1, (int(getattr(frame, "bpp", 8) or 8) + 7) // 8)
        nbytes = int(frame.width) * int(frame.height) * bpp_bytes * max(1, int(getattr(frame, "channels", 1) or 1))
        stats = (
            f"{frame.width}x{frame.height} bpp={frame.bpp} bytes={nbytes} "
            f"| preview {dw}x{dh} min={vmin} max={vmax} mean={mean:.1f} std={std:.1f}{gps_short}"
        )
        black = int(round(float(view.get("black", 0))))
        white = int(round(float(view.get("white", 255))))
        if white <= black:
            white = min(255, black + 1)
        zoom, zoom_label = self._preview_zoom_factor(dw, dh, view)
        disp_w, disp_h, disp_gray = render_preview_gray8(
            gray,
            dw,
            dh,
            brightness=float(view.get("brightness", 0.0)),
            contrast=float(view.get("contrast", 1.0)),
            black=black,
            white=white,
            zoom=zoom,
            max_dim=display_max,
        )
        return dw, dh, gray, stats, frame, gps_text, disp_w, disp_h, disp_gray, zoom_label

    def _preview_zoom_factor(self, base_w: int, base_h: int, view: dict | None = None) -> tuple[float, str]:
        if view is None:
            raw = "Fit"
            fill = False
            cw = ch = 0
        else:
            raw = str(view.get("zoom") or "Fit").strip()
            fill = bool(view.get("fill"))
            cw = int(view.get("canvas_w") or 0)
            ch = int(view.get("canvas_h") or 0)
        if raw.lower() == "fit":
            fit_w = cw if cw >= 64 else self._preview_fit_max_dim
            fit_h = ch if ch >= 64 else self._preview_fit_max_dim
            # fill=cover 铺满画布；普通 Fit=contain 完整显示
            if fill:
                z = max(fit_w / float(base_w), fit_h / float(base_h))
            else:
                z = min(fit_w / float(base_w), fit_h / float(base_h))
            if z <= 0:
                z = fit_zoom_for_size(base_w, base_h, self._preview_fit_max_dim)
            return z, (f"Fill({z:.2f}x)" if fill else f"Fit({z:.2f}x)")
        try:
            z = float(raw.lower().rstrip("x"))
        except ValueError:
            z = 1.0
        if z <= 0:
            z = 1.0
        return z, f"{z:g}x"

    # ------------------------------------------------------------- monitor / layout
    def refresh_monitor_list(self) -> None:
        """刷新物理显示器列表，并同步主窗/各预览窗的下拉。"""
        self._monitors = list_monitors()
        self._monitor_choice_labels = [m.label for m in self._monitors]
        values = list(self._layout_monitor_special) + self._monitor_choice_labels
        cur = (self.cap_layout_monitor_var.get() or "").strip()
        if cur not in values:
            self.cap_layout_monitor_var.set("跟随主窗")
        for win in self._cam_preview_wins.values():
            combo = getattr(win, "lay_monitor", None)
            if combo is not None:
                try:
                    combo.configure(values=values)
                except Exception:
                    pass

    def monitor_for_toplevel(self, widget: tk.Misc) -> MonitorInfo | None:
        """根据窗口中心点判断所在物理显示器。"""
        if not self._monitors:
            self.refresh_monitor_list()
        try:
            widget.update_idletasks()
            x = int(widget.winfo_rootx())
            y = int(widget.winfo_rooty())
            w = max(1, int(widget.winfo_width()))
            h = max(1, int(widget.winfo_height()))
            cx, cy = x + w // 2, y + h // 2
        except Exception:
            return self._monitors[0] if self._monitors else None
        return monitor_containing_point(self._monitors, cx, cy)

    def _selected_layout_monitor(self) -> MonitorInfo | None:
        """按 UI 选择解析目标物理显示器。"""
        if not self._monitors:
            self.refresh_monitor_list()
        if not self._monitors:
            return None
        choice = (self.cap_layout_monitor_var.get() or "跟随主窗").strip()
        if choice == "跟随主窗":
            return self.monitor_for_toplevel(self.root) or self._monitors[0]
        if choice == "跟随焦点预览窗":
            name = (self._primary_cam_name or "")
            win = self._cam_preview_wins.get(name) if name else None
            if win is None and self._cam_preview_wins:
                win = next(iter(self._cam_preview_wins.values()))
            if win is not None:
                return self.monitor_for_toplevel(win.win) or self._monitors[0]
            return self.monitor_for_toplevel(self.root) or self._monitors[0]
        for m in self._monitors:
            if m.label == choice:
                return m
        # 标签因刷新略有变化时：按「显示器N」前缀兜底
        for m in self._monitors:
            if choice.startswith(f"显示器{m.index}"):
                return m
        return self._monitors[0]

    def on_preview_layout_changed(self, _event=None) -> None:
        """布局/显示器/区域变化时，若已有预览窗则立刻重排。"""
        if self._cam_preview_wins:
            self.tile_multi_preview_windows()

    def _resolve_preview_layout(self, n: int, mon: MonitorInfo | None) -> tuple[str, int, int]:
        """返回 (mode, rows, cols)。mode ∈ 上下/左右/网格。"""
        mode = (self.cap_layout_mode_var.get() or "自动").strip()
        if mode == "自动":
            if n <= 1:
                mode = "左右"
            elif n == 2:
                mw = mon.work_w if mon else 1920
                mh = mon.work_h if mon else 1080
                # 横屏并排、竖屏叠放，避免横屏上下时每窗过矮被面板挤占
                mode = "上下" if mh > mw else "左右"
            else:
                mode = "网格"
        if mode == "上下":
            return mode, n, 1
        if mode == "左右":
            return mode, 1, n
        cols = max(1, int(round(n ** 0.5)))
        if cols * cols < n:
            cols += 1
        rows = (n + cols - 1) // cols
        return "网格", rows, cols

    @staticmethod
    def _tile_span(index: int, count: int, total: int, gap: int) -> tuple[int, int]:
        """把 total 像素均分为 count 段（段间 gap），返回第 index 段的 (offset, size)。末段吃掉余数以铺满。"""
        if count <= 1:
            return 0, max(1, total)
        usable = max(1, total - gap * (count - 1))
        start = (usable * index) // count
        end = usable if index + 1 >= count else (usable * (index + 1)) // count
        return start + index * gap, max(1, end - start)

    def _preview_layout_area(self) -> tuple[int, int, int, int, MonitorInfo | None]:
        """返回 (x, y, w, h, monitor)：整屏贴齐工作区（零边距），右侧模式才留缝。"""
        mon = self._selected_layout_monitor()
        if mon is None:
            try:
                sw = int(self.root.winfo_screenwidth())
                sh = int(self.root.winfo_screenheight())
            except Exception:
                sw, sh = 1920, 1080
            return 0, 0, max(420, sw), max(360, sh), None

        region = (self.cap_layout_region_var.get() or "整屏").strip()
        wx, wy, ww, wh = mon.work_x, mon.work_y, mon.work_w, mon.work_h
        if region == "右侧":
            gap = 4
            x = wx + int(ww * 0.48)
            w = max(420, ww - int(ww * 0.48) - gap)
            h = max(360, wh)
            y = wy
            return x, y, w, h, mon
        # 整屏：贴齐工作区四边（扣任务栏），零边距，避免左上角露桌面
        return wx, wy, max(420, ww), max(360, wh), mon

    @staticmethod
    def _place_toplevel(win: tk.Toplevel, x: int, y: int, w: int, h: int) -> None:
        """把顶层窗精确放到 (x,y,w,h)。Windows 优先 SetWindowPos，避免多屏坐标偏差。"""
        w = max(120, int(w))
        h = max(80, int(h))
        x, y = int(x), int(y)
        try:
            win.minsize(120, 80)
        except Exception:
            pass
        placed = False
        if sys.platform == "win32":
            try:
                import ctypes

                win.update_idletasks()
                hwnd = int(win.winfo_id())
                # Tk 内容窗的父级才是带边框的顶层 HWND
                parent = ctypes.windll.user32.GetParent(hwnd)
                if parent:
                    hwnd = parent
                SWP_NOZORDER = 0x0004
                SWP_SHOWWINDOW = 0x0040
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_SHOWWINDOW
                )
                placed = True
            except Exception:
                placed = False
        if not placed:
            try:
                win.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                pass

    def tile_multi_preview_windows(self) -> None:
        """按布局把托管预览窗排进选定的物理显示器工作区。

        支持多显示器：可选具体屏 / 跟随主窗 / 跟随焦点预览窗；屏内再选整屏或右侧。
        上下/多窗时自动精简面板，图像区域铺满格子。
        """
        self.refresh_monitor_list()
        order = [c.name for c in self.cameras]
        wins: list[CamPreviewWin] = []
        seen: set[str] = set()
        for name in order:
            w = self._cam_preview_wins.get(name)
            if w is not None and not w._closed:
                wins.append(w)
                seen.add(name)
        for name, w in self._cam_preview_wins.items():
            if name not in seen and not w._closed:
                wins.append(w)
        if not wins:
            return
        for w in wins:
            w.show()

        n = len(wins)
        area_x, area_y, area_w, area_h, mon = self._preview_layout_area()
        mode, rows, cols = self._resolve_preview_layout(n, mon)
        # 整屏零缝隙贴齐；仅「右侧」模式在格子间留 2px
        region = (self.cap_layout_region_var.get() or "整屏").strip()
        gap = 0 if region == "整屏" else 2
        mon_name = mon.label if mon else "主屏"
        cell_h_est = max(1, (area_h - gap * max(0, rows - 1)) // max(1, rows))
        # 多窗或上下布局：精简面板 + Fit 铺满裁剪
        use_compact = n >= 2 or mode == "上下" or cell_h_est < 780
        for w in wins:
            w.set_compact(use_compact, rerender=False)

        if n == 1:
            cell_w, cell_h = area_w, area_h
            self._place_toplevel(wins[0].win, area_x, area_y, cell_w, cell_h)
            try:
                wins[0].win.update_idletasks()
                wins[0]._canvas_w = max(64, int(wins[0].canvas.winfo_width()))
                wins[0]._canvas_h = max(64, int(wins[0].canvas.winfo_height()))
            except Exception:
                pass
            if wins[0]._base is not None:
                wins[0].rerender()
            self.output_queue.put(
                f"[preview] → {mon_name} | {mode}/{region} | 1 窗 {cell_w}x{cell_h} @({area_x},{area_y})"
                f"{' 精简铺满' if use_compact else ''}\n"
            )
            return

        sizes: list[tuple[int, int]] = []
        for i, w in enumerate(wins):
            r, c = divmod(i, cols)
            if mode == "上下":
                r, c = i, 0
            elif mode == "左右":
                r, c = 0, i
            x_off, cell_w = self._tile_span(c, cols, area_w, gap)
            y_off, cell_h = self._tile_span(r, rows, area_h, gap)
            x = area_x + x_off
            y = area_y + y_off
            sizes.append((cell_w, cell_h))
            self._place_toplevel(w.win, x, y, cell_w, cell_h)
        for w in wins:
            try:
                w.win.update_idletasks()
                w._canvas_w = max(64, int(w.canvas.winfo_width()))
                w._canvas_h = max(64, int(w.canvas.winfo_height()))
            except Exception:
                pass
            if w._base is not None:
                w.rerender()
        cw0, ch0 = sizes[0] if sizes else (0, 0)
        self.output_queue.put(
            f"[preview] → {mon_name} | {mode}/{region} | {n} 窗 → {rows}行×{cols}列  首格 {cw0}x{ch0}"
            f"{' 精简铺满' if use_compact else ''}\n"
        )

    def _open_preview_windows(self, names: list[str]) -> None:
        """按名单打开/保留预览窗；主窗永不画图，哪怕只有一台。"""
        wanted = [n for n in names if n]
        for name in list(self._cam_preview_wins):
            if name not in wanted:
                self._cam_preview_wins.pop(name).destroy()
        for name in wanted:
            if name not in self._cam_preview_wins:
                self._cam_preview_wins[name] = CamPreviewWin(self, name)
        self._multi_preview_active = bool(wanted)
        if wanted:
            self.tile_multi_preview_windows()
            self._refresh_multi_status_bar()
            self.output_queue.put(
                f"[preview] 托管预览窗 ×{len(wanted)}（图像/灰度/GPS 均在窗内）\n"
            )
        else:
            self.cap_multi_status_var.set("")

    def _close_multi_preview_windows(self) -> None:
        for win in list(self._cam_preview_wins.values()):
            win.destroy()
        self._cam_preview_wins.clear()
        self._multi_preview_active = False
        self.cap_multi_status_var.set("")

    def _flush_cam_preview_windows(self) -> None:
        for win in list(self._cam_preview_wins.values()):
            try:
                win.flush()
            except Exception:
                pass
        if self._cam_preview_wins:
            self._refresh_multi_status_bar()

    def _on_preview_win_focus(self, cam_name: str) -> None:
        if not cam_name or (cam_name not in self._live_threads and cam_name not in self._cam_preview_wins):
            return
        self._primary_cam_name = cam_name
        win = self._cam_preview_wins.get(cam_name)
        if win is not None and win.stats_var.get():
            self.cap_stats_var.set(win.stats_var.get())
        self._refresh_multi_status_bar()

    def _note_cam_waiting(self, cam_name: str, misses: int) -> None:
        win = self._cam_preview_wins.get(cam_name)
        if win is not None:
            win.set_state("WAITING", misses=misses)
        self._refresh_multi_status_bar()

    def _refresh_multi_status_bar(self, *, force: bool = False) -> None:
        # Live 时整表重写 Text 很重；节流避免拖死主线程（复位等用 force=True）
        now = time.perf_counter()
        if (not force and self._capture_live_running and (now - self._multi_status_t) < 0.15):
            return
        self._multi_status_t = now
        cur_name = (self._primary_cam_name or "")
        cur = short_cam_id(cur_name)
        if not self._cam_preview_wins:
            self.cap_multi_status_var.set("")
            return
        try:
            gps_on = bool(self.v_gps_on.get())
        except Exception:
            gps_on = False
        parts = []
        dash_lines = [
            f"当前相机: {cur_name or '(none)'}",
            "状态 / 帧" + (" / GPS 帧序号" if gps_on else "") + "（明细在各预览窗）:",
            "",
        ]
        for name, win in self._cam_preview_wins.items():
            seq_bit = ""
            if gps_on and win.last_seq is not None:
                seq_bit = f" {win.gps_seq_summary()}"
            elif gps_on:
                seq_bit = " seq=?"
            parts.append(f"{win.short}:{win.state} #{win.frames}{seq_bit}")
            mark = " ←" if name == cur_name else ""
            if gps_on:
                gps = win.gps_status_line()
                win.gps_short = gps
                dash_lines.append(
                    f"[{win.short}] {win.state}  frames={win.frames}{mark}\n  GPS 帧序号: {gps}"
                )
                if win.seq_gaps or win.missing_burst_seqs():
                    lost = win.lost_seqs_label()
                    dash_lines.append(
                        "  ⚠ 疑似丢帧"
                        + (f" gap={win.seq_gaps}" if win.seq_gaps else "")
                        + (f"  {lost}" if lost else "")
                        + (f" dup={win.seq_dups}" if win.seq_dups else "")
                    )
                if self._gps_seq_list_mode:
                    burst_list = win.burst_seq_list_text()
                    if burst_list:
                        for line in burst_list.splitlines():
                            dash_lines.append(f"  {line}")
            else:
                dash_lines.append(f"[{win.short}] {win.state}  frames={win.frames}{mark}")
        self.cap_multi_status_var.set(f"当前={cur}  " + " | ".join(parts))
        self._set_dash_text("\n".join(dash_lines))
        win = self._cam_preview_wins.get(cur_name)
        if win is not None:
            frame_txt = f"Frame: {win.short} #{win.frames}"
            if gps_on and win.last_seq is not None:
                frame_txt += f" {win.gps_seq_summary()}"
            self.cap_frame_var.set(frame_txt)
            fps = 0.0
            if len(win._fps_times) >= 2:
                dt = win._fps_times[-1] - win._fps_times[0]
                if dt > 0:
                    fps = (len(win._fps_times) - 1) / dt
            self.cap_fps_var.set(f"FPS: {win.short} {fps:.2f}")

    def _set_dash_text(self, text: str) -> None:
        try:
            self.cap_dash_text.configure(state=tk.NORMAL)
            self.cap_dash_text.delete("1.0", tk.END)
            self.cap_dash_text.insert("1.0", text or "")
            self.cap_dash_text.configure(state=tk.DISABLED)
        except Exception:
            pass

    # ------------------------------------------------------------- multi-cam live
    def _live_targets(self) -> list[str]:
        """要起流的相机。

        由「范围」单选决定：current = 仅当前选中相机；all = 扫描列表里全部相机
        + 池里已开的（不只限已 Connect）。相机在 Live/Capture 时自动打开。
        """
        targets: list[str] = []
        c = self._selected_cam()
        if c is not None:
            targets.append(c.name)
        if self.live_scope.get() == "all":
            for n in [x.name for x in self.cameras]:
                if n not in targets:
                    targets.append(n)
            if self._cap_pool is not None:
                for n in self._cap_pool.opened():
                    if n not in targets:
                        targets.append(n)
        return targets

    def toggle_live(self):
        if self._capture_live_running or self._capture_live_stopping:
            self._request_stop_live()
            return
        targets = self._live_targets()
        if not targets:
            messagebox.showerror("相机", "没有选中的相机。")
            return

        self._gps_parse_enabled = bool(self.v_gps_on.get())
        try:
            self._burst_waste_enabled = bool(self.filter_burst_waste.get())
        except Exception:
            self._burst_waste_enabled = False
        self._burst_waste_no_gps_logged = False
        # 同步/多机：自动切到帧序号列表，便于对照丢帧
        self._maybe_auto_seq_list_mode(targets)
        self._capture_live_running = True
        self._reset_save_progress()
        self._capture_live_stopping = False
        self._capture_live_stop.clear()
        self._live_stops.clear()
        self._live_threads.clear()
        self._reset_save_cnt()  # 新一轮 Live：保存计数归零
        with self._latest_lock:
            self._latest.clear()
            self._latest_n.clear()
        if self._selected_cam() is not None:
            self._primary_cam_name = self._selected_cam().name
        self._set_param_status("ps_live")
        try:
            self.btn_live.configure(text="Stop live")
        except Exception:
            pass
        self._sync_live_guards()

        # 先弹出预览窗（多机），再后台串行 Open 尚未 Connect 的相机，最后起采集线程。
        self._open_preview_windows(targets)
        for win in self._cam_preview_wins.values():
            win.reset_gps_seq_stats()
            win.frames = 0
            win._fps_times.clear()
        if len(targets) > 1:
            self.cap_status_var.set(f"多机 Live：正在打开 {len(targets)} 台相机（串行 Init，请稍候）...")
            self.output_queue.put(f"[live] 准备同时采集 {len(targets)} 台: {', '.join(targets)}\n")
        else:
            self.cap_status_var.set("Live preview starting...")

        def prepare_and_start():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    pool = self._capture_pool()
                    token = self._busy_begin(
                        self.T("busy_live_open").format(i=0, n=len(targets), name="…"),
                        total=len(targets))
                    for i, name in enumerate(targets, 1):
                        if not self._capture_live_running:
                            print("[live] 准备阶段已取消", flush=True)
                            self._busy_end(token, self.T("busy_done"))
                            return
                        self._busy_update(
                            token,
                            self.T("busy_live_open").format(i=i, n=len(targets), name=name),
                            value=i - 1, total=len(targets))
                        if pool.get(name) is None:
                            print(f"[live] 自动 Connect {name} ...", flush=True)
                            self._pool_open_resilient(pool, name)
                        self._busy_update(
                            token,
                            self.T("busy_live_open").format(i=i, n=len(targets), name=name),
                            value=i, total=len(targets))
                    if not self._capture_live_running:
                        print("[live] 准备阶段已取消", flush=True)
                        self._busy_end(token, self.T("busy_done"))
                        return
                    self._busy_update(token, self.T("busy_live_apply"))

                    # 开拍直接用 GUI 当前参数 apply（读回已在扫描后自动连接时完成）
                    def start_with_settings(tok=token):
                        if not self._capture_live_running or self._capture_live_stopping:
                            return
                        try:
                            self._store_roi_for_cam(getattr(self, "_param_cam_name", None))
                            settings = self._settings_from_gui(
                                STREAM_LIVE, sync_current_roi=False)
                        except Exception as exc:
                            messagebox.showerror("Live", f"Invalid params (often read mode): {exc}")
                            self._abort_live_prepare(str(exc))
                            return
                        timeout_s = capture_timeout_s(settings.exposure_us, live=True)
                        if any(self._gps_slave_for_cam(n) for n in targets) or settings.gps_slave:
                            timeout_s = max(timeout_s, 90.0)
                        self._busy_end(tok, self.T("busy_done"), status_key="ps_live")
                        self._start_live_threads(targets, settings, timeout_s)

                    self.root.after(0, start_with_settings)
                except Exception as exc:
                    msg = str(exc)
                    self.output_queue.put(f"[live] 多机准备失败: {msg}\n")
                    self.root.after(0, lambda m=msg: self._abort_live_prepare(m))

        threading.Thread(target=prepare_and_start, daemon=True).start()

    def _live_start_order(self, targets: list[str]) -> list[str]:
        """主机先、单机次、从机最后，避免 GPS 从机在主机尚未起流时空等 timeout。"""
        masters, solos, slaves, rest = [], [], [], []
        seen = set()
        for n in targets:
            if not n or n in seen:
                continue
            seen.add(n)
            role = self._cam_role(n)
            if role == ROLE_MASTER:
                masters.append(n)
            elif role == ROLE_SLAVE:
                slaves.append(n)
            elif role == ROLE_STANDALONE:
                solos.append(n)
            else:
                rest.append(n)
        return masters + solos + rest + slaves

    def _spawn_live_thread(self, name: str, settings, timeout_s: float,
                           *, skip_post_readback: bool = False) -> None:
        if not self._capture_live_running or self._capture_live_stopping:
            return
        if name in self._live_threads and self._live_threads[name].is_alive():
            return
        for c in self.cameras:
            if c.name == name:
                c.live = True
        ev = threading.Event()
        self._live_stops[name] = ev
        th = threading.Thread(
            target=self._live_worker,
            args=(name, settings, timeout_s, ev, skip_post_readback),
            daemon=True)
        self._live_threads[name] = th
        th.start()

    def _start_live_threads(self, targets: list[str], settings, timeout_s: float,
                              *, skip_post_readback: bool = False) -> None:
        if not self._capture_live_running or self._capture_live_stopping:
            return
        ordered = self._live_start_order(list(targets))
        if not ordered:
            return
        # 打出每机 ROI，便于发现一台 ROI、一台全幅导致 USB/读出不对称
        for n in ordered:
            roi = self._roi_dict_for_cam(n)
            role = self._cam_role(n)
            slave = self._gps_slave_for_cam(n)
            msg = (
                f"[live] start-order {n} role={role} gps_slave={slave} "
                f"roi={roi.get('x')},{roi.get('y')} {roi.get('w')}x{roi.get('h')}"
            )
            self.output_queue.put(msg + chr(10))
        masters = [n for n in ordered if self._cam_role(n) == ROLE_MASTER]
        lead = masters[0] if masters else ordered[0]
        followers = [n for n in ordered if n != lead]

        self._spawn_live_thread(lead, settings, timeout_s,
                                skip_post_readback=skip_post_readback)

        def _start_followers():
            # 等主机（或首台）真正出第一帧再起从机，ROI 重配后尤其需要
            deadline = time.perf_counter() + 8.0
            while (self._capture_live_running and not self._capture_live_stopping
                   and time.perf_counter() < deadline):
                with self._latest_lock:
                    if int(self._latest_n.get(lead, 0) or 0) > 0:
                        break
                time.sleep(0.05)
            with self._latest_lock:
                got = int(self._latest_n.get(lead, 0) or 0)
            if got <= 0:
                self.output_queue.put(
                    f"[live] 首台 {short_cam_id(lead)} 8s 内未出帧，仍继续起其余相机" + chr(10)
                )
            else:
                self.output_queue.put(
                    f"[live] 首台 {short_cam_id(lead)} 已出帧，启动其余 {len(followers)} 台" + chr(10)
                )
            for name in followers:
                if not self._capture_live_running or self._capture_live_stopping:
                    return
                self._spawn_live_thread(name, settings, timeout_s,
                                        skip_post_readback=skip_post_readback)
            self.root.after(0, self._refresh_cam_tree)

        if followers:
            threading.Thread(target=_start_followers, daemon=True).start()
        else:
            self._refresh_cam_tree()

        if len(ordered) > 1:
            self.cap_status_var.set(
                f"Live 运行中（{len(ordered)} 台，主机优先起流，预览窗已打开）"
            )
        else:
            self.cap_status_var.set("Live preview running...")
        self.multi_var.set(", ".join(ordered))

    def _live_worker(self, cam_name: str, settings, timeout_s: float, stop_ev,
                     skip_post_readback: bool = False):
        """一台相机的采集线程。多机时每台都进自己的预览窗。"""
        writer = QueueWriter(self.output_queue)
        tag = short_cam_id(cam_name)
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            try:
                cam_settings = self._settings_from_gui(
                    STREAM_LIVE, cam_name=cam_name, sync_current_roi=False)
                cam = self._pool_open_resilient(self._cap_pool, cam_name)
                try:
                    cam.apply_settings(cam_settings)
                    cam.begin_live()
                except Exception as exc:
                    if not self._is_dead_worker_error(exc):
                        raise
                    print(f"[live {tag}] 子进程中途死亡，重建后重试一次", flush=True)
                    try:
                        self._cap_pool.close(cam_name)
                    except Exception:
                        pass
                    cam = self._pool_open_resilient(self._cap_pool, cam_name)
                    cam.apply_settings(cam_settings)
                    cam.begin_live()
                settings = cam_settings
                # 起流成功：仅对当前选中 / primary 回写 GUI，避免多机互相覆盖（F4）。
                # skip_post_readback 保留给调用方（默认 False：起流后再读一次对齐界面）。
                if not skip_post_readback:
                    def _maybe_readback(n=cam_name):
                        primary = self._primary_cam_name
                        sel = None
                        try:
                            sc = self._selected_cam()
                            sel = sc.name if sc is not None else None
                        except Exception:
                            sel = None
                        if n == primary or n == sel:
                            self._run_readback(n)
                    self.root.after(0, _maybe_readback)
                # 取帧超时拆成短片：Stop 后不必等整段 exposure/GPS 超时才能退出。
                # 外层 timeout_s 在 GPS 从机/多机准备时已抬到 >=90；此处必须沿用，
                # 否则从机每 8s 就被判 timeout（ROI 重配后主机尚未稳定时更明显）。
                while self._capture_live_running and not stop_ev.is_set():
                    live_timeout_s = capture_timeout_s(settings.exposure_us, live=True)
                    try:
                        live_timeout_s = max(live_timeout_s, float(timeout_s or 0.0))
                    except Exception:
                        pass
                    if self._gps_slave_for_cam(cam_name) or bool(
                            getattr(settings, 'gps_slave', False)):
                        live_timeout_s = max(live_timeout_s, 90.0)
                    deadline = time.perf_counter() + live_timeout_s
                    # 本帧是否是「该存」的帧：按每 N 帧节拍判断（成功取帧才计数，
                    # 取帧失败不消耗保存节拍）。只有该存这一帧才取全幅像素。
                    save_this = False
                    if self.save_enabled.get():
                        with self._frame_seq_lock:
                            cur_cnt = self._save_cnt.get(cam_name, 0)
                            try:
                                n = max(1, int((self.v_save_interval.get() or "1").strip() or "1"))
                            except Exception:
                                n = 1
                            save_this = (cur_cnt % n) == 0
                        # 无界缓存：始终允许拉全幅入队；容量仅作预警（_flush_save_progress_ui）
                    frame = None
                    last_exc: Exception | None = None
                    while (self._capture_live_running and not stop_ev.is_set()
                           and time.perf_counter() < deadline):
                        remain = deadline - time.perf_counter()
                        if remain <= 0:
                            break
                        try:
                            # 帧序号 / 废帧过滤需要 parse_gps；勿写死 False（否则依赖主机重解析，
                            # 且 IPC pack 缺字段时 Capture 路径会直接炸预览）。
                            # 开了「GPS 帧序号」或废帧过滤，或本帧要存盘且 GPS 功能开着 → 解析 GPS
                            want_gps = bool(getattr(self, "_gps_parse_enabled", False)) or bool(
                                getattr(self, "_burst_waste_enabled", False)
                            )
                            if save_this and bool(getattr(self, "_gps_parse_enabled", False)):
                                want_gps = True
                            frame = cam.capture_live_frame(
                                parse_gps=want_gps,
                                timeout_s=min(self._live_slice_s, remain),
                                copy_pixels=save_this,  # SDK 侧拷全幅像素
                                # IPC 默认只回 64B 帧头；存盘必须 want_data=True 才走 data_shm
                                want_data=save_this,
                                preview_max_dim=self._live_preview_native_max_dim,
                            )
                            last_exc = None
                            break
                        except RuntimeError as exc:
                            last_exc = exc
                            continue
                    if not self._capture_live_running or stop_ev.is_set():
                        break
                    if frame is not None:
                        is_waste, waste_seq = self._should_discard_burst_waste(frame)
                        if is_waste:
                            try:
                                frame._burst_waste = True
                            except Exception:
                                pass
                            print(f"[burst] discard waste frame seq={waste_seq}", flush=True)
                            # 废帧：不入保存节拍、不存盘；预览跳过（避免闪很亮的废帧）
                            continue
                        with self._frame_seq_lock:
                            self._save_cnt[cam_name] = self._save_cnt.get(cam_name, 0) + 1
                        with self._latest_lock:
                            self._latest[cam_name] = frame
                            self._latest_n[cam_name] = self._latest_n.get(cam_name, 0) + 1
                        win = self._cam_preview_wins.get(cam_name)
                        if win is not None:
                            win.queue_frame(frame, interval_s=self._preview_ui_interval_s)
                        elif cam_name:
                            fr = frame
                            self.root.after(0, lambda n=cam_name, f=fr: self._ensure_preview(n).queue_frame(f, interval_s=self._preview_ui_interval_s))
                        if save_this:
                            self._save_frame_live(cam_name, frame)
                        # 预览 / _latest 只需帧头 + preview；丢掉全幅像素防泄漏
                        try:
                            raw = getattr(frame, "data", None) or b""
                            if isinstance(raw, (bytes, bytearray)) and len(raw) > 64:
                                frame.data = bytes(raw[:64])
                        except Exception:
                            pass
                    else:
                        exc = last_exc or RuntimeError("live frame timeout")
                        # 同步等待超时很吵：每相机最多 2s 打一行；真 MemoryError 必打
                        msg = str(exc)
                        is_mem = "MemoryError" in type(exc).__name__ or "MemoryError" in msg
                        now = time.perf_counter()
                        last_t = float(self._live_wait_log_t.get(tag, 0.0) or 0.0)
                        if is_mem or (now - last_t) >= 2.0:
                            self._live_wait_log_t[tag] = now
                            note = "（内存不足，写盘/全幅请降频）" if is_mem else "（同步等待中属正常）"
                            print(f"[live {tag}] {exc}{note}", flush=True)
                        self.root.after(0, lambda n=cam_name: self._note_cam_waiting(n, 1))
                try:
                    cam.stop_live()
                except Exception:
                    pass
                n = self._latest_n.get(cam_name, 0)
                print(f"[live {tag}] 结束，共 {n} 帧", flush=True)
            except Exception as exc:
                print(f"ERROR live {tag}: {exc}", flush=True)
                win = self._cam_preview_wins.get(cam_name)
                if win is not None:
                    self.root.after(0, lambda w=win: w.set_state("ERROR"))
            finally:
                for c in self.cameras:
                    if c.name == cam_name:
                        c.live = False
                self._live_threads.pop(cam_name, None)
                self._live_stops.pop(cam_name, None)
                self.root.after(0, self._on_live_ui_stopped)

    def _request_stop_live(self) -> None:
        """Stop live：可重复点击，不会再误走「启动」路径。"""
        self._capture_live_stopping = True
        self._capture_live_running = False
        self._capture_live_stop.set()
        for ev in list(self._live_stops.values()):
            ev.set()
        try:
            self.btn_live.configure(text="Stopping...", state="normal")
        except Exception:
            pass
        if not self._live_threads:
            self._capture_live_stopping = False
            self._close_multi_preview_windows()
            try:
                self.btn_live.configure(text="Live")
            except Exception:
                pass
            self.cap_status_var.set("Live cancelled.")
            self._sync_live_guards()
            return
        self.cap_status_var.set("Stopping live preview...")
        self._sync_live_guards()

    def _abort_live_prepare(self, message: str) -> None:
        try:
            self._busy_end(getattr(self, "_busy_token", 0), f"Live 失败: {message}")
        except Exception:
            pass
        self._capture_live_running = False
        self._capture_live_stopping = False
        self._capture_live_stop.set()
        self._close_multi_preview_windows()
        self._live_threads.clear()
        self._live_stops.clear()
        try:
            self.btn_live.configure(text="Live", state="normal")
        except Exception:
            pass
        self._sync_live_guards()
        self.cap_status_var.set(f"Live 失败: {message}")
        messagebox.showerror("Live", f"多机 Live 准备失败:\n{message}")

    def _on_live_ui_stopped(self):
        # 多机时主线程可能先结束；等其它采集线程也退完再收尾。
        alive = {n: t for n, t in list(self._live_threads.items()) if t.is_alive()}
        self._live_threads = alive
        if alive:
            self._capture_live_stopping = True
            self._capture_live_running = False
            try:
                self.btn_live.configure(text="Stopping...", state="normal")
            except Exception:
                pass
            self.cap_status_var.set(f"Stopping live preview... ({len(alive)} left)")
            self.root.after(200, self._on_live_ui_stopped)
            return
        self._capture_live_stopping = False
        self._capture_live_running = False
        try:
            self.btn_live.configure(text="Live", state="normal")
        except Exception:
            pass
        self._set_param_status("ps_idle")  # Live 已停：回到空闲
        self._sync_live_guards()
        self._flush_cam_preview_windows()
        # 保留预览窗与最后一帧/GPS，便于对照；下次 Live/Capture 会按名单重建
        for win in self._cam_preview_wins.values():
            if win.state != "ERROR":
                win.set_state("IDLE")
        self._live_threads.clear()
        self._live_stops.clear()
        self.cap_status_var.set("Live 已停止。")
        self._refresh_cam_tree()
        self._refresh_multi_status_bar(force=True)

    def run_capture_single(self):
        if self._live_busy():
            messagebox.showinfo(self.T("capture_single"), self.T("need_stop_live"))
            self._set_param_status("ps_need_stop")
            return
        targets = self._live_targets()
        if not targets:
            messagebox.showerror("相机", "没有选中的相机。")
            return
        self._burst_waste_no_gps_logged = False
        self._gps_parse_enabled = bool(self.v_gps_on.get())
        try:
            self._burst_waste_enabled = bool(self.filter_burst_waste.get())
        except Exception:
            self._burst_waste_enabled = False
        self._maybe_auto_seq_list_mode(targets)

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    pool = self._capture_pool()
                    # 开拍直接用 GUI 当前参数（读回已在扫描后自动连接时完成）
                    ready = threading.Event()
                    box: dict = {}

                    def build_settings():
                        try:
                            box["settings"] = self._settings_from_gui(STREAM_SINGLE)
                        except Exception as exc:
                            box["error"] = exc
                        finally:
                            ready.set()

                    self.root.after(0, build_settings)
                    if not ready.wait(timeout=10.0):
                        raise RuntimeError("build settings timeout")
                    if box.get("error") is not None:
                        raise box["error"]
                    settings = box["settings"]
                    timeout_s = capture_timeout_s(settings.exposure_us, live=False)
                    want_gps = bool(self.v_gps_on.get()) or bool(self.filter_burst_waste.get())
                    token = self._busy_begin(
                        self.T("busy_cap_open").format(i=0, n=len(targets), name="…"),
                        total=len(targets))

                    for i, n in enumerate(targets, 1):
                        self._busy_update(
                            token,
                            self.T("busy_cap_open").format(i=i, n=len(targets), name=n),
                            value=i - 1, total=len(targets))
                        cam = self._pool_open_resilient(pool, n)

                        def _capture_once(c=cam):
                            return c.capture_single(parse_gps=want_gps, timeout_s=timeout_s)

                        try:
                            self._busy_update(token, self.T("busy_cap_apply"))
                            cam.apply_settings(settings)
                            frame = _capture_once()
                        except Exception as exc:
                            if not self._is_dead_worker_error(exc):
                                raise
                            print(f"[capture] {n} 子进程死亡，重建后重试", flush=True)
                            try:
                                pool.close(n)
                            except Exception:
                                pass
                            cam = self._pool_open_resilient(pool, n)
                            cam.apply_settings(settings)
                            frame = _capture_once(cam)

                        # 同步 burst 残留废帧：丢掉后再采，最多共 4 次
                        for _attempt in range(3):
                            is_waste, waste_seq = self._should_discard_burst_waste(frame)
                            if not is_waste:
                                break
                            print(f"[burst] discard waste frame seq={waste_seq}", flush=True)
                            frame = _capture_once(cam)
                        else:
                            is_waste, waste_seq = self._should_discard_burst_waste(frame)
                            if is_waste:
                                print(f"[burst] discard waste frame seq={waste_seq} "
                                      f"(single; not saved)", flush=True)
                                try:
                                    frame._burst_waste = True
                                except Exception:
                                    pass

                        win = self._ensure_preview(n)
                        win.show_frame(frame)
                        if not getattr(frame, "_burst_waste", False):
                            self._save_frame(frame, n)
                        print(f"[capture] {n}: {frame.width}x{frame.height} {frame.bpp}bit "
                              f"len={len(frame.data)}\n", flush=True)

                    # 单帧后再读一次，保持界面与相机一致
                    self.root.after(0, lambda: self._run_readback(targets[0]))
                    self.root.after(0, lambda: self._set_param_status(
                        "ps_pushed", what="single", n=len(targets)))
                    self.root.after(0, lambda: self.status_var.set(f"Capture 完成 {len(targets)} 台"))
                    self._busy_end(token, self.T("busy_done"),
                                   status_key="ps_pushed", what="single", n=len(targets))
                except Exception as exc:
                    self.output_queue.put(f"[capture] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"Capture 失败: {m}"))
                    try:
                        self._busy_end(token, f"Capture 失败: {exc}")
                    except Exception:
                        pass
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- previews
    def _ensure_preview(self, name: str) -> CamPreviewWin:
        """确保某台相机有托管预览窗（供单帧 Capture 使用）。"""
        win = self._cam_preview_wins.get(name)
        if win is None or win._closed:
            win = CamPreviewWin(self, name)
            self._cam_preview_wins[name] = win
        return win

    def _drop_preview(self, name: str):
        win = self._cam_preview_wins.pop(name, None)
        if win is not None:
            win.destroy()

    def copy_preview_gray_from(self, cam_name: str) -> None:
        """把一台预览窗的灰度复制到其它所有预览窗。"""
        src = self._cam_preview_wins.get(cam_name)
        if src is None:
            return
        n = 0
        for name, win in self._cam_preview_wins.items():
            if name == cam_name:
                continue
            win.apply_gray_from(src)
            n += 1
        self.output_queue.put(f"[preview] 已把 {short_cam_id(cam_name)} 的灰度复制到另外 {n} 个窗\n")

    def tile_previews(self):
        # 重排全部预览窗（兼容旧按钮名）
        self.tile_multi_preview_windows()

    # ------------------------------------------------------------- GPS panel / seq list
    def _sync_seq_list_max_from_ui(self) -> None:
        try:
            n = int(str(self.cap_burst_n_var.get()).strip() or "0")
        except Exception:
            n = 200
        self._gps_seq_list_max = n if n > 0 else 200

    def _sync_context_active(self, targets: list[str] | None = None) -> bool:
        """是否处于同步/多机观察场景（应自动用帧序号列表）。"""
        try:
            if any(c.role in (ROLE_MASTER, ROLE_SLAVE) for c in self.cameras):
                return True
        except Exception:
            pass
        names = targets
        if names is None:
            try:
                names = self._live_targets()
            except Exception:
                names = []
        if len(names or []) >= 2:
            return True
        try:
            if len(self._cam_preview_wins) >= 2:
                return True
        except Exception:
            pass
        return False

    def _maybe_auto_seq_list_mode(self, targets: list[str] | None = None) -> bool:
        """同步/多机时自动切到帧序号列表；已在列表模式则只同步上限。返回是否处于列表模式。"""
        if not self._sync_context_active(targets):
            return bool(self._gps_seq_list_mode)
        self._sync_seq_list_max_from_ui()
        if self._gps_seq_list_mode:
            self._set_gps_panel_mode_text()
            return True
        self.cap_gps_panel_mode.set("seq_list")
        self._gps_seq_list_mode = True
        self._set_gps_panel_mode_text()
        for win in list(self._cam_preview_wins.values()):
            try:
                win.reset_seq_list_keep_slots()
            except Exception:
                pass
        try:
            self.status_var.set("已自动切换到「帧序号列表」（同步/多机）")
        except Exception:
            pass
        try:
            self._refresh_multi_status_bar(force=True)
        except Exception:
            pass
        return True

    def toggle_gps_panel_mode(self) -> None:
        """切换 GPS 面板：默认刷新 ↔ 帧序号列表。"""
        cur = (self.cap_gps_panel_mode.get() or "refresh").strip()
        if cur == "seq_list":
            self.cap_gps_panel_mode.set("refresh")
            self._gps_seq_list_mode = False
            self._set_gps_panel_mode_text()
            for win in list(self._cam_preview_wins.values()):
                try:
                    win._set_gps("已切回默认刷新，等待下一帧 GPS 明细…" if self.lang == "zh"
                                 else "Back to refresh; waiting for the next GPS frame detail…")
                except Exception:
                    pass
            return
        self.cap_gps_panel_mode.set("seq_list")
        self._sync_seq_list_max_from_ui()
        self._gps_seq_list_mode = True
        self._set_gps_panel_mode_text()
        for win in list(self._cam_preview_wins.values()):
            try:
                win.reset_seq_list_keep_slots()
            except Exception:
                pass

    def _set_gps_panel_mode_text(self):
        """按当前模式 + 语言刷新 GPS 面板模式按钮文案。"""
        key = "gps_mode_seq" if (self.cap_gps_panel_mode.get() or "refresh").strip() == "seq_list" \
            else "gps_mode_refresh"
        try:
            self.btn_gps_panel_mode.configure(text=self.T(key))
        except Exception:
            pass

    def reset_all_gps_seq_lists(self) -> None:
        """清掉 burst 帧序号统计（上一 burst 残留），保留 [01]..[N] 空位；不改预览窗当前帧 GPS 框。"""
        self._sync_seq_list_max_from_ui()
        for win in list(self._cam_preview_wins.values()):
            try:
                win.reset_seq_list_keep_slots()
            except Exception:
                pass
        self._refresh_multi_status_bar(force=True)
        self.status_var.set("已复位 GPS 帧序号列表（满 N 冻结、同序号去重；预览窗当前帧 GPS 明细未改）")

    def _toggle_gps_advanced_ui(self) -> None:
        """折叠/展开校灯·POS·Slave UTC；默认隐藏，不打断主从一键底层路径。"""
        try:
            show = bool(self.v_gps_advanced.get())
        except Exception:
            show = False
        host = getattr(self, "_gps_adv_host", None)
        if host is None:
            return
        try:
            if show:
                host.pack(fill=tk.X, pady=(0, 0))
            else:
                host.pack_forget()
        except Exception:
            pass

    def _on_gps_on_changed(self, *_args):
        """帧序号 on：主机解析 + 刷新各预览窗缓存；off 时清序号统计。"""
        try:
            on = bool(self.v_gps_on.get())
        except Exception:
            on = False
        self._gps_parse_enabled = on
        if on:
            # 已分配主从 / 多机时自动切列表，免再手点
            try:
                self._maybe_auto_seq_list_mode(None)
            except Exception:
                pass
        for win in list(self._cam_preview_wins.values()):
            try:
                win.refresh_view_cache()
                if not on:
                    win.reset_gps_seq_stats()
                    win._set_gps("帧序号 off.")
                else:
                    win._set_gps("帧序号 on，等待下一帧…")
            except Exception:
                pass

    def _bind_gps_utc_preview_traces(self):
        for var in (self.v_gps_y, self.v_gps_m, self.v_gps_d,
                    self.v_gps_h, self.v_gps_min, self.v_gps_s):
            try:
                var.trace_add("write", lambda *_a: self.preview_gps_js1995())
            except Exception:
                pass

    def _parse_gps_utc_fields(self) -> tuple[int, int, int, int, int, int]:
        return (
            int(self.v_gps_y.get().strip()),
            int(self.v_gps_m.get().strip()),
            int(self.v_gps_d.get().strip()),
            int(self.v_gps_h.get().strip()),
            int(self.v_gps_min.get().strip()),
            int(self.v_gps_s.get().strip()),
        )

    def fill_gps_utc_from_now(self):
        try:
            offset = float(self.v_gps_from_now.get().strip() or "0")
        except ValueError as exc:
            messagebox.showerror("GPS UTC", f"Invalid from-now offset: {exc}")
            return
        _js, cal = utc_now_plus_js1995(offset)
        self.v_gps_y.set(str(cal[0]))
        self.v_gps_m.set(str(cal[1]))
        self.v_gps_d.set(str(cal[2]))
        self.v_gps_h.set(str(cal[3]))
        self.v_gps_min.set(str(cal[4]))
        self.v_gps_s.set(str(cal[5]))
        self.preview_gps_js1995()

    def preview_gps_js1995(self):
        try:
            y, M, d, h, m, s = self._parse_gps_utc_fields()
            js = date_to_js1995(y, M, d, h, m, s)
            back = js1995_to_utc_tuple(js)
            self.v_gps_js1995.set(
                f"{js}  (= {back[0]:04d}-{back[1]:02d}-{back[2]:02d} "
                f"{back[3]:02d}:{back[4]:02d}:{back[5]:02d})"
            )
        except Exception as exc:
            self.v_gps_js1995.set(f"(invalid: {exc})")

    def run_set_gps_slave_utc(self):
        """Date2Js1995 + SetQHYCCDGPSSlaveModeParameter 下发到选中相机。"""
        if self._live_busy():
            messagebox.showinfo("GPS Slave UTC", self.T("need_stop_live"))
            self._set_param_status("ps_need_stop")
            return
        c = self._selected_cam()
        if c is None:
            messagebox.showinfo("GPS Slave UTC", self.T("need_select_cam"))
            return
        name = c.name
        try:
            y, M, d, h, m, s = self._parse_gps_utc_fields()
            target_us = int(self.v_gps_target_us.get().strip() or "0", 0)
            delta_sec = int(self.v_gps_delta_sec.get().strip() or "0", 0)
            delta_us = int(self.v_gps_delta_us.get().strip() or "0", 0)
            exp_us = exp_to_us(float(self.v_gps_slave_exp.get().strip() or "0"),
                               self.v_gps_slave_exp_unit.get())
        except ValueError as exc:
            messagebox.showerror("GPS Slave UTC", f"Invalid field: {exc}")
            return
        self.preview_gps_js1995()
        slave = bool(self.v_gps_slave.get())

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    pool = self._capture_pool()
                    cam = self._pool_open_resilient(pool, name)
                    cam.set_gps_enable(True)
                    cam.try_set_gps_master_slave(slave)
                    cam.set_gps_slave_target_utc(y, M, d, h, m, s,
                                                 target_us=target_us,
                                                 delta_t_sec=delta_sec,
                                                 delta_t_us=delta_us,
                                                 exp_time_us=exp_us)
                    self.output_queue.put(
                        f"[gps] Slave UTC 已下发 {name}: {y}-{M}-{d} {h}:{m}:{s} "
                        f"target_us={target_us} delta={delta_sec}s{delta_us}us exp={exp_us}us\n")
                    self.root.after(0, lambda: self.status_var.set("GPS Slave UTC 已下发"))
                except Exception as exc:
                    self.output_queue.put(f"[gps] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"GPS Slave UTC 失败: {m}"))
        threading.Thread(target=work, daemon=True).start()

    def run_apply_gps_pos(self):
        """Ezcap sequence: LedCalMode ON → POSB/POSA → LedCalMode OFF (or leave ON)。"""
        if self._live_busy():
            messagebox.showinfo("GPS POS", self.T("need_stop_live"))
            self._set_param_status("ps_need_stop")
            return
        c = self._selected_cam()
        if c is None:
            messagebox.showinfo("GPS POS", self.T("need_select_cam"))
            return
        name = c.name
        try:
            pos_a = int(self.v_gps_pos_a.get().strip() or "0", 0)
            pos_b = int(self.v_gps_pos_b.get().strip() or "0", 0)
            width = int(self.v_gps_pos_width.get().strip() or "40", 0)
        except ValueError as exc:
            messagebox.showerror("GPS POS", f"Invalid POS value: {exc}")
            return
        if pos_a == 0 and pos_b == 0:
            messagebox.showwarning(
                "GPS POS",
                "POSA/POSB 均为 0 时禁止下发 LedCal：会把曝光窗锁成 0。\n"
                "请填入非零校灯位置后再 Apply。")
            return
        slave = bool(self.v_gps_slave.get())
        leave_on = bool(self.v_gps_led_cal.get())

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    pool = self._capture_pool()
                    cam = self._pool_open_resilient(pool, name)
                    cam.apply_settings(self._settings_from_gui(STREAM_SINGLE))
                    cam.set_gps_enable(True)
                    if slave:
                        cam.try_set_gps_master_slave(True)
                    cam.apply_gps_led_positions(pos_a=pos_a, pos_b=pos_b, width=width,
                                                slave=slave, leave_led_cal_on=leave_on)
                    self.output_queue.put(
                        f"[gps] POS 已下发 {name}: POSB={pos_b} POSA={pos_a} width={width} "
                        f"slave={slave} leave_led_cal={leave_on}\n")
                    self.root.after(0, lambda: self.status_var.set("GPS POS / LED cal 已下发"))
                except Exception as exc:
                    self.output_queue.put(f"[gps] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"GPS POS 失败: {m}"))
        self.status_var.set("Applying GPS POS / LED cal...")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- sync modes
    def _ensure_reg_ctx(self, camera_name: str):
        from qhyccd_api import (QhyApi, init_qhy_resource_with_retry, list_camera_names,
                                open_camera_by_name, scan_cameras)
        from qhyccd_highlevel import _ensure_highlevel_bindings
        with self._reg_lock:
            ctx = self._reg_ctxs.get(camera_name)
            if ctx is not None:
                return ctx
            if self._reg_api is None:
                api = QhyApi(self.project_root)
                _ensure_highlevel_bindings(api)
                init_qhy_resource_with_retry(api)
                self._reg_api = api
            api = self._reg_api
            ids = scan_cameras(api)
            names = list_camera_names(ids)
            if camera_name not in names:
                raise RuntimeError(f"寄存器会话找不到相机: {camera_name}")
            ctx = open_camera_by_name(api, ids, camera_name)
            self._reg_ctxs[camera_name] = ctx
            print(f"[reg] 寄存器会话已打开 {ctx.cam_id}（未 Init，不影响采图）", flush=True)
            return ctx

    @staticmethod
    def _transport_const(t: str) -> str:
        if t == "usb":
            return TRANSPORT_USB
        if t == "pcie":
            return TRANSPORT_PCIE
        return "any"

    def _sync_target_cameras(self, target: str, transport: str = "any") -> list[str]:
        tr = self._transport_const(transport)
        def ok(c: CamRow) -> bool:
            role_ok = (target == "master" and c.role == ROLE_MASTER) or \
                      (target == "slaves" and c.role == ROLE_SLAVE) or \
                      (target == "all" and c.role in (ROLE_MASTER, ROLE_SLAVE))
            tr_ok = tr == "any" or c.transport == tr
            return role_ok and tr_ok
        return [c.name for c in self.cameras if ok(c)]

    def _opt_int_sync(self, var: tk.StringVar, default: int) -> int:
        t = (var.get() or "").strip()
        if not t:
            return default
        try:
            return int(t, 0)  # accept 0x.. or decimal
        except Exception:
            return default

    def _patch_bytes(self, value: int) -> list[int]:
        """Split a 32-bit patch into 4 big-endian bytes for FPGA 41..44."""
        v = int(value) & 0xFFFFFFFF
        return [(v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]

    def _build_sync_steps(self, key: str) -> list[tuple[int, int]]:
        """Build the (fpga_index, value) list for a sync button from 参数设置 fields.

        Defaults equal the exact register sequences in host/fpga_soft_modes.json, so with
        the default fields the buttons behave identically to the original one-click modes.
        """
        usb_b = self._patch_bytes(self._opt_int_sync(self.v_usb_patch, 32001))
        pcie_b = self._patch_bytes(self._opt_int_sync(self.v_pcie_patch, 8192))
        burst_start_raw = self._opt_int_sync(self.v_burst_start, 1)
        window = self._opt_int_sync(self.v_window, 11) & 0xFFFF  # burst end (reg51/52)
        if burst_start_raw < 0 or burst_start_raw > 0xFF:
            raise ValueError(
                f"burst 开始非法: {burst_start_raw}（reg50 为单字节，范围 0..255）")
        burst_start = int(burst_start_raw) & 0xFF
        if window < 0xFFFF and burst_start > window:
            raise ValueError(
                f"burst 开始({burst_start}) 不能大于结束({window})；"
                f"有效帧为 [开始, 结束]，一组序号 0..结束")
        min_pulse = self._opt_int_sync(self.v_min_pulse, 10) & 0xFF
        m39 = self._opt_int_sync(self.v_master_39, 17) & 0xFFFF
        s39 = self._opt_int_sync(self.v_slave_39, 2) & 0xFFFF
        m58 = self._opt_int_sync(self.v_master_58, 0) & 0xFFFF
        s58 = self._opt_int_sync(self.v_slave_58, 8) & 0xFFFF

        def burst(patch):
            return [(41, patch[0]), (42, patch[1]), (43, patch[2]), (44, patch[3])]

        def window_steps():
            # reg50=burst_start, reg51/52=burst_end (hi/lo)
            return [(50, burst_start),
                    (51, (window >> 8) & 0xFF),
                    (52, window & 0xFF)]

        if key == "usb_master":
            return (burst(usb_b) + window_steps()
                    + [(142, 1), (56, 1), (58, m58), (39, m39), (57, 1), (35, 0)])
        if key == "usb_slave":
            return (burst(usb_b) + window_steps()
                    + [(81, 0), (39, s39), (142, 1), (56, 1),
                       (144, 0), (145, 0), (146, 0), (147, 0), (148, min_pulse),
                       (58, s58), (57, 1), (35, 0)])
        if key == "usb_burst":
            return burst(usb_b)
        if key == "pcie_master_hold":
            return (burst(pcie_b) + window_steps()
                    + [(142, 1), (56, 1), (58, m58), (39, m39), (57, 1),
                       (35, 0), (63, 1), (63, 0)])
        if key == "pcie_slave":
            return (burst(pcie_b) + window_steps()
                    + [(81, 0), (39, s39), (142, 1), (56, 1),
                       (144, 0), (145, 0), (146, 0), (147, 0), (148, min_pulse),
                       (58, s58), (57, 1), (35, 0)])
        if key == "pcie_burst":
            return burst(pcie_b)
        if key == "trigger_release":
            return [(35, 0), (35, 1)]
        if key == "exit_sync":
            return [(57, 0), (81, 0), (58, 0), (35, 1), (39, 1),
                    (41, 0), (42, 0), (43, 0), (44, 0), (50, 0), (51, 0), (52, 0)]
        raise ValueError(f"unknown sync key: {key}")

    def _write_sync_steps(self, ctx, steps: list[tuple[int, int]]) -> None:
        for idx, val in steps:
            ctx.write_fpga(idx, val)

    def run_sync_mode(self, key: str, target: str, label: str, dry: bool = False, transport: str = "any"):
        cams = self._sync_target_cameras(target, transport)
        if not dry and not cams:
            messagebox.showinfo("主从同步", f"「{label}」没有匹配的相机。\n"
                            "请先在「相机/主从」页把相关相机设为 主机/从机，"
                            f"且传输方式与按钮分组（{'USB' if transport=='usb' else 'PCIe' if transport=='pcie' else 'USB或PCIe'}）一致。")
            return
        try:
            steps = self._build_sync_steps(key)
        except Exception as exc:
            messagebox.showerror("主从同步", f"参数解析失败: {exc}")
            return
        preview = "; ".join(f"{i}={v}" for i, v in steps)
        self._log_sync(f"=== {label} [{key}] -> {cams or 'dry-run'}\n    {preview}\n")

        if dry:
            # 无硬件，直接打印即可（不占线程、不会乱序）
            for cam_name in (cams or ["(dry-run)"]):
                self._log_sync(f"  [dry] {cam_name}: " + "  ".join(f"{i}={v}" for i, v in steps) + "\n")
            return

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    for cam_name in cams:
                        ctx = self._ensure_reg_ctx(cam_name)
                        print(f"--- 写 {cam_name} ---", flush=True)
                        self._write_sync_steps(ctx, steps)
                        self.root.after(0, lambda n=cam_name, l=label: self._log_sync(f"  ✓ {n} {l} 完成\n"))
                    self.root.after(0, lambda l=label: self.status_var.set(f"{l} 完成"))
                except Exception as exc:
                    self.output_queue.put(f"[sync] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"{label} 失败: {m}"))
                    self.root.after(0, lambda m=str(exc): self._log_sync(f"  ✗ ERROR: {m}\n"))
        self.status_var.set(f"执行 {label} ...")
        threading.Thread(target=work, daemon=True).start()

    def preview_sync_steps(self):
        """Pop up a dialog listing the register steps for all 8 sync buttons given current params."""
        lines = []
        for group_name, buttons in SYNC_GROUPS:
            lines.append(f"【{group_name}】")
            for label, key, target, transport in buttons:
                try:
                    steps = self._build_sync_steps(key)
                    preview = "  ".join(f"{i}={v}" for i, v in steps)
                except Exception as exc:
                    preview = f"<错误: {exc}>"
                tr = {"usb": "USB", "pcie": "PCIe/光纤", "any": "USB/PCIe"}[transport]
                lines.append(f"  {label} [{key}] -> {target} / {tr}\n      {preview}")
        lines.append("")
        lines.append("（默认 burst 起止 = 1–11 → reg50=1, reg51/52=11；改「主从同步相机参数」可同步改这里）")
        win = tk.Toplevel(self.root)
        win.title("主从同步按钮 -> 寄存器序列 预览")
        win.geometry("760x480")
        txt = tk.Text(win, wrap=tk.WORD, font=("Consolas", 10))
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert(tk.END, "\n".join(lines))
        txt.configure(state=tk.DISABLED)

    def _log_sync(self, text: str):
        try:
            self.sync_text.configure(state=tk.NORMAL)
            self.sync_text.insert(tk.END, text)
            self.sync_text.see(tk.END)
            self.sync_text.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _load_modes(self):
        path = HOST_DIR / "fpga_soft_modes.json"
        with open(path, encoding="utf-8") as f:
            self._modes_doc = json.load(f)

    def dry_run_all_sync(self):
        """Log the register sequence for every sync button given current params (no hardware)."""
        for group_name, buttons in SYNC_GROUPS:
            for label, key, target, transport in buttons:
                self.run_sync_mode(key, target, label, dry=True, transport=transport)

    def _sync_key_for_cam(self, cam: CamRow) -> str | None:
        """Pick the master/slave config step key for a camera by role + transport."""
        if cam.role == ROLE_MASTER:
            return "pcie_master_hold" if cam.transport == TRANSPORT_PCIE else "usb_master"
        if cam.role == ROLE_SLAVE:
            return "pcie_slave" if cam.transport == TRANSPORT_PCIE else "usb_slave"
        return None

    def _apply_sync_key_to_cam(self, cam_name: str, key: str) -> None:
        """Open a reg ctx for the camera and write the given step sequence (main-thread reg session)."""
        ctx = self._ensure_reg_ctx(cam_name)
        steps = self._build_sync_steps(key)
        print(f"--- {cam_name} [{key}] ---", flush=True)
        self._write_sync_steps(ctx, steps)

    def onekey_set_sync_params(self):
        """① 一键设置主从参数：主机写主机配置、所有从机写从机配置（各按 USB/PCIe）。"""
        try:
            self._maybe_auto_seq_list_mode(None)
        except Exception:
            pass
        jobs = []  # (name, key, label)
        for c in self.cameras:
            key = self._sync_key_for_cam(c)
            if key:
                jobs.append((c.name, key, f"{c.role}/{c.transport}"))
        if not jobs:
            messagebox.showinfo("主从一键", "还没有确定主机/从机。\n"
                            "请先在「相机/主从」页把相机设为 主机 或 从机（一主多从）。")
            return
        self.status_var.set("① 一键设置主从参数 ...")
        self._log_sync(f"=== ① 一键设置主从参数: {[j[0] for j in jobs]} ===\n")

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    for name, key, tag in jobs:
                        self._apply_sync_key_to_cam(name, key)
                        self.root.after(0, lambda n=name, k=key, t=tag: self._log_sync(f"  ✓ {n}  {k} ({t})\n"))
                    self.root.after(0, lambda: self.status_var.set("① 主从参数已设置完成"))
                except Exception as exc:
                    self.output_queue.put(f"[onekey] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"一键设置失败: {m}"))
                    self.root.after(0, lambda m=str(exc): self._log_sync(f"  ✗ ERROR: {m}\n"))
        threading.Thread(target=work, daemon=True).start()

    def onekey_enter_sync(self):
        """② 进入同步拍摄(开拍)：仅主机释放 IDLE，主从同时出图。"""
        try:
            self._maybe_auto_seq_list_mode(None)
        except Exception:
            pass
        masters = [c.name for c in self.cameras if c.role == ROLE_MASTER]
        if not masters:
            messagebox.showinfo("主从一键", "没有主机。\n"
                            "请先在「相机/主从」页指定一台主机，并先点 ① 设置、让 Live 出图。")
            return
        # 同步开拍：自动复位帧序号列表/累计（等同「复位序号」），避免上一轮残留
        try:
            self.reset_all_gps_seq_lists()
        except Exception:
            pass
        self.status_var.set("② 进入同步拍摄（释放 IDLE）... 已复位序号")
        self._log_sync(f"=== ② 进入同步拍摄（释放 IDLE）: {masters}（已复位序号）===\n")

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    for name in masters:
                        self._apply_sync_key_to_cam(name, "trigger_release")
                        self.root.after(0, lambda n=name: self._log_sync(f"  ✓ {n} 释放 IDLE 开拍\n"))
                    self.root.after(0, lambda: self.status_var.set("② 已开拍，主从同步出图中"))
                except Exception as exc:
                    self.output_queue.put(f"[onekey] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"开拍失败: {m}"))
                    self.root.after(0, lambda m=str(exc): self._log_sync(f"  ✗ ERROR: {m}\n"))
        threading.Thread(target=work, daemon=True).start()

    def onekey_exit_sync(self):
        """③ 退出同步拍摄(恢复单机)：全部主/从相机恢复单机默认。"""
        cams = [c.name for c in self.cameras if c.role in (ROLE_MASTER, ROLE_SLAVE)]
        if not cams:
            messagebox.showinfo("主从一键", "没有主机/从机需要同步退出。")
            return
        self.status_var.set("③ 退出同步拍摄（恢复单机）...")
        self._log_sync(f"=== ③ 退出同步拍摄（恢复单机）: {cams} ===\n")

        def work():
            writer = QueueWriter(self.output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    for name in cams:
                        self._apply_sync_key_to_cam(name, "exit_sync")
                        self.root.after(0, lambda n=name: self._log_sync(f"  ✓ {n} 已恢复单机\n"))
                    self.root.after(0, lambda: self.status_var.set("③ 已退出同步，恢复单机出图"))
                except Exception as exc:
                    self.output_queue.put(f"[onekey] ERROR: {exc}\n")
                    self.root.after(0, lambda m=str(exc): self.status_var.set(f"退出失败: {m}"))
                    self.root.after(0, lambda m=str(exc): self._log_sync(f"  ✗ ERROR: {m}\n"))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- plumbing
    def _run_status(self, msg: str):
        self.root.after(0, lambda: self.status_var.set(msg))

    def _drain_output(self):
        # 每 tick 排空日志队列 + 刷所有预览窗（渲染帧/刷新 GPS 序号）
        n = 0
        chars = 0
        while n < 20 and chars < 16_000:
            try:
                text = self.output_queue.get_nowait()
            except queue.Empty:
                break
            for w in (self.log_text, self.cap_log):
                try:
                    w.configure(state=tk.NORMAL)
                    w.insert(tk.END, text)
                except Exception:
                    pass
            n += 1
            chars += len(text)
        if n:
            for w in (self.log_text, self.cap_log):
                try:
                    # 限制日志长度，避免 Text 控件本身吃掉数 GB
                    end_idx = w.index("end-1c")
                    if float(end_idx.split(".")[0]) > 4000:
                        w.delete("1.0", "end-2000l")
                    w.see(tk.END)
                    w.configure(state=tk.DISABLED)
                except Exception:
                    pass
        self._flush_cam_preview_windows()
        delay = 50 if self._capture_live_running else 80
        self.root.after(delay, self._drain_output)

    def _on_close(self):
        """主窗关闭按钮：优先进入托盘（后台继续运行）；托盘不可用时直接退出。"""
        if self._tray_running:
            self.root.withdraw()
            return
        self._quit_app()

    # ------------------------------------------------------------------ 系统托盘
    @staticmethod
    def _make_tray_image():
        """托盘图标：PIL 绘制的相机（青绿机身 + 镜头 + 红点），不依赖外部文件。"""
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((20, 14, 34, 22), fill=(43, 184, 144, 255))   # 取景器
        d.rounded_rectangle((6, 22, 58, 52), radius=8, fill=(43, 184, 144, 255))
        d.ellipse((24, 28, 44, 48), fill=(20, 32, 44, 255))       # 镜头
        d.ellipse((28, 32, 40, 44), fill=(180, 220, 210, 255))
        d.ellipse((46, 26, 54, 34), fill=(255, 80, 80, 255))      # 录制红点
        return img

    def _build_tray(self):
        """启动系统托盘图标（run_detached 独立线程，tk 主循环不受影响）。失败静默降级。"""
        if pystray is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", self._tray_show, default=True),
                pystray.MenuItem("隐藏到托盘", self._tray_hide),
                pystray.MenuItem("退出", self._tray_quit),
            )
            self._tray_icon = pystray.Icon(
                "LiuSirCapture", self._make_tray_image(), "LiuSir 相机采集", menu)
            self._tray_icon.run_detached()
            self._tray_running = True
        except Exception:
            self._tray_icon = None
            self._tray_running = False

    def _tray_show(self, _icon=None, _item=None):
        self.root.after(0, self._show_from_tray)

    def _show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(250, lambda: self.root.attributes("-topmost", False))

    def _tray_hide(self, _icon=None, _item=None):
        self.root.after(0, self.root.withdraw)

    def _tray_quit(self, _icon=None, _item=None):
        self.root.after(0, self._quit_app)

    def _quit_app(self):
        """真正退出：停止采集、释放相机/SDK、销毁窗口与托盘图标。"""
        self._shutdown_capture()
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
            self._tray_running = False
        self.root.destroy()

    def _shutdown_capture(self):
        try:
            self._request_stop_live()
        except Exception:
            pass
        try:
            if self._cap_pool:
                self._cap_pool.close_all()
        except Exception:
            pass
        try:
            from qhyccd_api import close_camera
            with self._reg_lock:
                for name, ctx in list(self._reg_ctxs.items()):
                    try:
                        close_camera(self._reg_api, ctx)
                    except Exception:
                        pass
                if self._reg_api is not None:
                    try:
                        self._reg_api.dll.ReleaseQHYCCDResource()
                    except Exception:
                        pass
                    self._reg_api = None
        except Exception:
            pass
        try:
            for w in list(self._cam_preview_wins.values()):
                w.destroy()
            self._cam_preview_wins.clear()
        except Exception:
            pass


def main() -> int:
    _ensure_cwd_qhyccd_ini()
    project_root = fpga_paths.resolve_project_root(
        os.environ.get("FPGA_PROJECT_ROOT") or None, required=False) or APP_ROOT
    root = tk.Tk()
    CaptureApp(root, Path(project_root))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
