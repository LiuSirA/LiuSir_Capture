#!/usr/bin/env python3
"""High-level QHYCCD camera + GPS helpers for FPGA soft automation.

Builds on qhyccd_api.QhyApi (DLL load / vend). Reference flows:
  - Ezcap gpsTool.cpp / downloadCapThread.cpp
  - QHYCCD SDK: Init / single-frame / SetQHYCCDGPS*

powered by liusir
"""

from __future__ import annotations

import sys
from pathlib import Path as _ToolsPath
_tools_root = _ToolsPath(__file__).resolve().parents[1]
if str(_tools_root) not in sys.path:
    sys.path.insert(0, str(_tools_root))

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Optional
import time

from qhyccd_api import (
    CameraCtx,
    Color,
    QhyApi,
    close_camera,
    cprint,
    open_camera_by_name,
    open_selected_camera,
    read_fpga_version,
    scan_cameras,
)


# CONTROL_ID (qhyccdstruct.h) — values used by this module
CONTROL_GAIN = 6
CONTROL_OFFSET = 7
CONTROL_EXPOSURE = 8
CONTROL_SPEED = 9
CONTROL_TRANSFERBIT = 10
CONTROL_USBTRAFFIC = 12
CONTROL_DDR = 48
CAM_GPS = 36
CAM_SINGLEFRAMEMODE = 57
CAM_LIVEVIDEOMODE = 58
# IsQHYCCDControlAvailable IDs for square bin modes (qhyccdstruct.h)
CAM_BIN1X1MODE = 21
CAM_BIN2X2MODE = 22
CAM_BIN3X3MODE = 23
CAM_BIN4X4MODE = 24

STREAM_SINGLE = 0
STREAM_LIVE = 1


@dataclass
class CameraSettings:
    """Unified camera configuration (Ezcap-style SDK path)."""

    stream_mode: int = STREAM_SINGLE
    read_mode: int = 0
    bits: int = 16
    exposure_us: float = 100_000.0
    gain: float | None = None
    offset: float | None = None
    usb_traffic: float | None = None
    speed: float | None = None
    bin_x: int = 1
    bin_y: int = 1
    roi_x: int = 0
    roi_y: int = 0
    roi_w: int | None = None
    roi_h: int | None = None
    ddr_enable: bool | None = None
    gps_enable: bool = False
    gps_slave: bool = False
    gps_led_cal: bool = False
    # Ezcap gpsTool: Start digits → POSB, End digits → POSA, pulse width default 40
    gps_pos_a: int | None = None
    gps_pos_b: int | None = None
    gps_pos_width: int = 40

    def as_dict(self) -> dict:
        return {
            "stream_mode": self.stream_mode,
            "read_mode": self.read_mode,
            "bits": self.bits,
            "exposure_us": self.exposure_us,
            "gain": self.gain,
            "offset": self.offset,
            "usb_traffic": self.usb_traffic,
            "speed": self.speed,
            "bin_x": self.bin_x,
            "bin_y": self.bin_y,
            "roi_x": self.roi_x,
            "roi_y": self.roi_y,
            "roi_w": self.roi_w,
            "roi_h": self.roi_h,
            "ddr_enable": self.ddr_enable,
            "gps_enable": self.gps_enable,
            "gps_slave": self.gps_slave,
            "gps_led_cal": self.gps_led_cal,
            "gps_pos_a": self.gps_pos_a,
            "gps_pos_b": self.gps_pos_b,
            "gps_pos_width": self.gps_pos_width,
        }

    def runtime_key(self) -> tuple:
        """Fields that can be written without Init / StopLive / ROI change."""
        return (
            int(self.bits),
            float(self.exposure_us),
            self.gain,
            self.offset,
            self.usb_traffic,
            self.speed,
            self.ddr_enable,
            bool(self.gps_enable),
            bool(self.gps_slave),
            bool(self.gps_led_cal),
            self.gps_pos_a,
            self.gps_pos_b,
            int(self.gps_pos_width),
        )

    def gps_pos_configured(self) -> bool:
        """True when POS looks user-set (at least one non-zero).

        Default/empty POSA=POSB=0 must not trigger LedCalMode：否则会把
        start/end 锁成 0，曝光窗塌掉（一点 GPS on 就花/黑/极短曝光）。
        LED cal 勾选本身不足以下发——必须同时有非零 POS。
        """
        if self.gps_pos_a is None and self.gps_pos_b is None:
            return False
        return int(self.gps_pos_a or 0) != 0 or int(self.gps_pos_b or 0) != 0

QHYCCD_SUCCESS = 0
QHYCCD_ERROR = 0xFFFFFFFF
# ExpQHYCCDSingleFrame may return this: image ready for immediate readout (not an error)
QHYCCD_READ_DIRECTLY = 0x2001
QHYCCD_DELAY_200MS = 0x2000


def capture_timeout_s(exposure_us: float, *, live: bool = False, extra_s: float = 20.0) -> float:
    """Deadline for GetQHYCCD*Frame polling: exposure plus readout margin."""
    exp_s = max(0.0, float(exposure_us) / 1_000_000.0)
    if live:
        return max(8.0, exp_s + 5.0)
    return max(30.0, exp_s * 2.0 + extra_s)


# MAX10 counter1: 10 MHz, wrap 10000500 → overlay us fields are 0.1 µs ticks.
GPS_TICK_US = 0.1
GPS_SEC_TICKS = 10_000_500


def gps_ticks_to_us(ticks: int) -> float:
    """Convert GPS overlay us-field ticks to microseconds (1 tick = 0.1 µs)."""
    return float(ticks) * GPS_TICK_US


def gps_span_ticks(start_sec: int, start_us: int, end_sec: int, end_us: int) -> int:
    """Tick span from start to end, wrapping us at GPS_SEC_TICKS per GPS second."""
    return (int(end_sec) - int(start_sec)) * GPS_SEC_TICKS + (int(end_us) - int(start_us))


def gps_exposure_us(start_sec: int, start_us: int, end_sec: int, end_us: int) -> float:
    """True exposure in microseconds from GPS start/end overlay fields."""
    return gps_span_ticks(start_sec, start_us, end_sec, end_us) * GPS_TICK_US


@dataclass
class GpsInfo:
    """Parsed from first 44 bytes of image when CAM_GPS is enabled (Ezcap layout).

    start_us / end_us / now_us / pps are raw MAX10 counter1 ticks (0.1 µs each).
    exposure_us is converted to true microseconds.
    """

    seq_number: int = 0
    width: int = 0
    height: int = 0
    latitude: float = 0.0
    longitude: float = 0.0
    start_sec: int = 0
    start_us: int = 0
    end_sec: int = 0
    end_us: int = 0
    now_flag: int = 0
    now_sec: int = 0
    now_us: int = 0
    pps: int = 0
    elevation: int = 0
    gps_status: int = 0
    exposure_us: float = 0.0
    exposure_ticks: int = 0
    raw: bytes = field(default_factory=bytes)

    def as_dict(self) -> dict:
        antenna_status = self.gps_status % 10
        gnss_mode = self.gps_status // 10
        return {
            "seq_number": self.seq_number,
            "width": self.width,
            "height": self.height,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "start_sec": self.start_sec,
            "start_us": self.start_us,
            "start_true_us": gps_ticks_to_us(self.start_us),
            "end_sec": self.end_sec,
            "end_us": self.end_us,
            "end_true_us": gps_ticks_to_us(self.end_us),
            "now_flag": self.now_flag,
            "now_sec": self.now_sec,
            "now_us": self.now_us,
            "now_true_us": gps_ticks_to_us(self.now_us),
            "pps": self.pps,
            "pps_us": gps_ticks_to_us(self.pps),
            "elevation": self.elevation,
            "gps_status": self.gps_status,
            "antenna_status": antenna_status,
            "gnss_mode": gnss_mode,
            "exposure_ticks": self.exposure_ticks,
            "exposure_us": self.exposure_us,
            "tick_us": GPS_TICK_US,
        }


def format_gps_report(gps: GpsInfo | None, *, wanted: bool = False) -> str:
    """Multi-line GPS overlay summary for CLI/GUI."""
    if gps is None:
        if wanted:
            return (
                "GPS on, but this frame has no parsed overlay.\n"
                "CAM_GPS / FPGA reg56 (FrameNumberEnable) may be off, "
                "or the header is not in the first 44 bytes."
            )
        return "GPS off."

    def _utc(sec: int) -> str:
        try:
            y, M, d, h, m, s = js1995_to_utc_tuple(int(sec) & 0xFFFFFFFF)
            return f"{y:04d}-{M:02d}-{d:02d} {h:02d}:{m:02d}:{s:02d}"
        except Exception:
            return "?"

    def _frac(ticks: int) -> str:
        return f"{float(int(ticks)) / float(GPS_SEC_TICKS):.7f}"[1:]

    def _us(ticks: int) -> str:
        return f"{gps_ticks_to_us(ticks):.1f}us"

    antenna = gps.gps_status % 10
    gnss = gps.gps_status // 10
    issues = gps_header_issues(gps)
    lines = [
        f"seq={int(gps.seq_number) & 0xFFFFFFFF}  overlay={gps.width}x{gps.height}",
        f"lat={gps.latitude:.7f}  lon={gps.longitude:.7f}  elev={gps.elevation}",
        f"start {gps.start_sec}{_frac(gps.start_us)}  "
        f"({gps.start_us} ticks={_us(gps.start_us)})  UTC {_utc(gps.start_sec)}{_frac(gps.start_us)}",
        f"end   {gps.end_sec}{_frac(gps.end_us)}  "
        f"({gps.end_us} ticks={_us(gps.end_us)})  UTC {_utc(gps.end_sec)}{_frac(gps.end_us)}",
        f"now   {gps.now_sec}{_frac(gps.now_us)}  "
        f"({gps.now_us} ticks={_us(gps.now_us)})  UTC {_utc(gps.now_sec)}{_frac(gps.now_us)}",
        f"exp={gps.exposure_us:.1f} us  ({gps.exposure_ticks} ticks @ 0.1 us)  "
        f"flag={gps.now_flag}  pps={gps.pps} ticks ({_us(gps.pps)})",
        f"gnss_mode={gnss}  antenna={antenna}  status={gps.gps_status}",
        "validity: " + (", ".join(issues) if issues else "OK"),
    ]
    return "\n".join(lines)


def format_gps_seq_brief(gps: GpsInfo | None, *, wanted: bool = False) -> str:
    """短显示：以帧序号为主（GUI 默认预览用）；完整明细见 format_gps_report。"""
    if gps is None:
        if wanted:
            return (
                "帧序号 on，但本帧无 overlay。\n"
                "CAM_GPS / FPGA reg56(FrameNumberEnable) 可能未开，"
                "或帧头不在前 44 字节。"
            )
        return "帧序号 off."
    seq = int(getattr(gps, "seq_number", 0) or 0) & 0xFFFFFFFF
    issues = gps_header_issues(gps)
    ow = int(getattr(gps, "width", 0) or 0)
    oh = int(getattr(gps, "height", 0) or 0)
    exp = float(getattr(gps, "exposure_us", 0.0) or 0.0)
    st = int(getattr(gps, "gps_status", 0) or 0)
    lines = [
        f"帧序号 seq={seq}  overlay={ow}x{oh}",
        f"exp≈{exp:.1f} us  status={st}",
    ]
    if issues:
        lines.append("validity: " + ", ".join(issues))
    return "\n".join(lines)


@dataclass
class FrameResult:
    width: int
    height: int
    bpp: int
    channels: int
    data: bytes
    gps: Optional[GpsInfo] = None
    preview_w: int = 0
    preview_h: int = 0
    preview_gray: bytes = b""


def date2jd(year: int, month: int, day: int) -> int:
    """Ezcap gpsTool::Date2Jd — integer Julian Day used by Date2Js1995."""
    ky = int(year)
    km = int(month)
    d = int(day)
    b = -2
    if km <= 2:
        ky -= 1
        km += 12
    if ky > 1582 or (ky == 1582 and (km > 10 or (km == 10 and d >= 15))):
        b = ky // 400 - ky // 100
    jd = int(365.25 * ky) + int(30.6001 * (km + 1)) + b + 1720996.5 + d
    return int(jd)


def date_to_js1995(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    """UTC calendar → Ezcap JS1995 seconds (gpsTool::Date2Js1995).

    Zero reference follows Ezcap: Jd1995 = Date2Jd(y,M,d) - 2450000, then
    Js1995 = Jd1995 * 86400 + h*3600 + m*60 + s.
    """
    jd = date2jd(year, month, day)
    jd1995 = jd - 2450000
    return int(jd1995 * 24 * 3600 + hour * 3600 + minute * 60 + second)


def jd_int_to_ymd(jd_int: int) -> tuple[int, int, int]:
    """Inverse of date2jd for civil Gregorian dates (adequate for camera GPS range)."""
    # Standard Fliegel/Van Flandern style on JD at noon; our date2jd truncates
    # JD+0.5 style values, so recover with jd_int + 0.5 then convert.
    jd = float(jd_int) + 0.5
    z = int(jd + 0.5)
    f = (jd + 0.5) - z
    if z < 2299161:
        a = z
    else:
        alpha = int((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - alpha // 4
    b = a + 1524
    c = int((b - 122.1) / 365.25)
    d = int(365.25 * c)
    e = int((b - d) / 30.6001)
    day = int(b - d - int(30.6001 * e) + f)
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715
    return int(year), int(month), int(day)


def js1995_to_utc_tuple(js1995: int) -> tuple[int, int, int, int, int, int]:
    """Inverse of Ezcap Date2Js1995 → (Y,M,D,h,m,s)."""
    day_index = js1995 // 86400
    rem = js1995 - day_index * 86400
    jd_int = 2450000 + day_index
    year, month, day = jd_int_to_ymd(jd_int)
    hour = rem // 3600
    minute = (rem % 3600) // 60
    second = rem % 60
    return year, month, day, hour, minute, second


def utc_now_plus_js1995(offset_s: float = 0.0) -> tuple[int, tuple[int, int, int, int, int, int]]:
    """Current UTC (+ offset seconds) → (js1995, calendar tuple)."""
    now = datetime.now(timezone.utc).timestamp() + float(offset_s)
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    cal = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    return date_to_js1995(*cal), cal


def exp_to_us(value: float, unit: str) -> int:
    """Ezcap GPS exp unit: us / ms / s → microseconds."""
    u = (unit or "us").strip().lower()
    if u == "us":
        mult = 1
    elif u == "ms":
        mult = 1000
    elif u in ("s", "sec", "second"):
        mult = 1_000_000
    else:
        raise ValueError(f"unsupported exp unit: {unit}")
    return int(float(value) * mult)


def _swap16_bytes(buf: bytes) -> bytes:
    out = bytearray(buf)
    for i in range(0, len(out) - 1, 2):
        out[i], out[i + 1] = out[i + 1], out[i]
    return bytes(out)


def _parse_gps_bytes(b: bytes) -> GpsInfo:
    def u32_be(off: int) -> int:
        return (b[off] << 24) | (b[off + 1] << 16) | (b[off + 2] << 8) | b[off + 3]

    def u24_be(off: int) -> int:
        return (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]

    def u16_be(off: int) -> int:
        return (b[off] << 8) | b[off + 1]

    seq = u32_be(0)
    width = u16_be(5)
    height = u16_be(7)

    temp = u32_be(9)
    south = temp > 1000000000
    deg = (temp % 1000000000) // 10000000
    minute = (temp % 10000000) // 100000
    fract_min = (temp % 100000) / 100000.0
    latitude = (deg + (minute + fract_min) / 60.0) * (-1 if south else 1)

    temp = u32_be(13)
    # SharpCap QHYGPSStatus / Ezcap updateGPSInfo: west if >1e9,
    # dddmm + 4 fractional minute digits (deg = %1e9 / 1e6).
    west = temp > 1000000000
    deg = (temp % 1000000000) // 1000000
    minute = (temp % 1000000) // 10000
    fract_min = (temp % 10000) / 10000.0
    longitude = (deg + (minute + fract_min) / 60.0) * (-1 if west else 1)

    start_sec = u32_be(18)
    start_us = u24_be(22)
    end_sec = u32_be(26)
    end_us = u24_be(30)
    now_flag = (b[33] // 16) % 4
    now_sec = u32_be(34)
    now_us = u24_be(38)
    pps = u24_be(41)
    elevation = u24_be(44) if len(b) >= 47 else 0
    gps_status = b[47] if len(b) >= 48 else 0
    exposure_ticks = gps_span_ticks(start_sec, start_us, end_sec, end_us)
    exposure_us = exposure_ticks * GPS_TICK_US

    return GpsInfo(
        seq_number=seq,
        width=width,
        height=height,
        latitude=latitude,
        longitude=longitude,
        start_sec=start_sec,
        start_us=start_us,
        end_sec=end_sec,
        end_us=end_us,
        now_flag=now_flag,
        now_sec=now_sec,
        now_us=now_us,
        pps=pps,
        elevation=elevation,
        gps_status=gps_status,
        exposure_us=exposure_us,
        exposure_ticks=exposure_ticks,
        raw=b,
    )


def parse_gps_header(
    img: bytes,
    *,
    expect_w: int | None = None,
    expect_h: int | None = None,
) -> GpsInfo:
    """Parse GPS overlay (first 44 bytes), SDK / Ezcap / SharpCap formula.

    会尝试 aligned / swap16 / +4 偏移。实机上 +4+swap16 偶发得分更高，
    把 seq 解析成 ``0x25XX1980`` 这类假值（GUI 表现为帧序号乱跳）。
    因此：默认锚定 offset0；其它候选必须明显更高分才采纳；并优先匹配帧宽高。
    """
    if len(img) < 44:
        raise ValueError(f"GPS header needs >=44 bytes, got {len(img)}")
    raw = img[:64] if len(img) >= 64 else img
    ew = int(expect_w) if expect_w else 0
    eh = int(expect_h) if expect_h else 0

    def _score(g: GpsInfo) -> int:
        score = 0
        if 0 <= int(g.start_us) < GPS_SEC_TICKS:
            score += 3
        if 0 <= int(g.end_us) < GPS_SEC_TICKS:
            score += 3
        if 0 <= int(g.now_us) < GPS_SEC_TICKS:
            score += 1
        if 8_000_000 <= g.pps <= 12_000_000:
            score += 4
        elif 0 < int(g.pps) < GPS_SEC_TICKS:
            score += 1
        if -90.0 <= g.latitude <= 90.0 and abs(g.latitude) > 0.01:
            score += 2
        if -180.0 <= g.longitude <= 180.0 and abs(g.longitude) > 0.01:
            score += 2
        if g.now_flag == 3:
            score += 2
        if ew > 0 and eh > 0 and int(g.width) == ew and int(g.height) == eh:
            score += 12
        elif 100 <= g.width <= 20000 and 100 <= g.height <= 20000:
            score += 1
        # 无秒时间戳全 0 → 多半是像素噪声当帧头
        if int(g.start_sec) == 0 and int(g.start_us) == 0:
            score -= 6
        return score

    aligned = raw if len(raw) >= 48 else raw[:44]
    swapped = _swap16_bytes(aligned)
    cands: list[GpsInfo] = [
        _parse_gps_bytes(aligned[:48] if len(aligned) >= 48 else aligned[:44]),
        _parse_gps_bytes(swapped[:48] if len(swapped) >= 48 else swapped[:44]),
    ]
    # 仅当帧头带 FPGA magic 时才试 +4（跳过 0x11223344）；
    # 否则 +4/swap 会把正常 Ezcap 头拆坏，seq 变成 0x25XX1980。
    magic = b"\x11\x22\x33\x44"
    try_offset4 = raw[:4] == magic or raw[4:8] == magic
    if try_offset4:
        if len(raw) >= 52:
            cands.append(_parse_gps_bytes(raw[4:52]))
            cands.append(_parse_gps_bytes(_swap16_bytes(raw[4:52])))
        elif len(raw) >= 48:
            cands.append(_parse_gps_bytes(raw[4:48]))
            cands.append(_parse_gps_bytes(_swap16_bytes(raw[4:48])))

    scored = [(_score(g), idx, g) for idx, g in enumerate(cands)]
    best_score, best_idx, best = max(scored, key=lambda t: (t[0], -t[1]))
    aligned_score = scored[0][0]
    # 非 aligned 必须高出一截，避免 lat/lon 偶然得分抢走正确头
    if best_idx != 0 and best_score < aligned_score + 4:
        return cands[0]
    return best


def gps_header_issues(
    gps: GpsInfo | None,
    *,
    lat_range: tuple[float, float] | None = None,
    lon_range: tuple[float, float] | None = None,
    min_exp_us: float = 0.0,
    require_seq: int | None = None,
    expect_w: int | None = None,
    expect_h: int | None = None,
) -> list[str]:
    """Return failure strings; empty list means the header passed the gates."""
    if gps is None:
        return ["gps is None"]
    fails: list[str] = []
    if require_seq is not None and gps.seq_number != require_seq:
        fails.append(f"seq=0x{gps.seq_number:08X}")
    if lat_range is not None and not (lat_range[0] < gps.latitude < lat_range[1]):
        fails.append(f"lat={gps.latitude}")
    if lon_range is not None and not (lon_range[0] < gps.longitude < lon_range[1]):
        fails.append(f"lon={gps.longitude}")
    if min_exp_us and gps.exposure_us < min_exp_us:
        fails.append(f"exp={gps.exposure_us:.1f}us")
    if gps.start_sec == 0 and gps.start_us == 0:
        fails.append("timestamps zero")
    # us 场是 0.1µs tick，必须落在一秒计数范围内；越界几乎必是错对齐
    if not (0 <= int(gps.start_us) < GPS_SEC_TICKS):
        fails.append(f"start_us={gps.start_us}")
    if not (0 <= int(gps.end_us) < GPS_SEC_TICKS):
        fails.append(f"end_us={gps.end_us}")
    if expect_w and expect_h:
        if int(gps.width) != int(expect_w) or int(gps.height) != int(expect_h):
            fails.append(f"overlay={gps.width}x{gps.height}!=frame={expect_w}x{expect_h}")
    return fails


def is_burst_waste_frame(seq: int, end: int, start: int = 1) -> bool:
    """True if GPS ``seq`` is a waste/dummy frame of a sync-burst group.

    FPGA:
      - reg50 = ``burst_start`` (GUI: burst 开始，默认 1)
      - reg51/52 = ``burst_end`` (GUI: burst 结束 / 原 window，默认 11)

    One group has indices ``0 .. end`` (period = end + 1). Valid frames are
    ``[start, end]``; waste frames are those with ``idx = seq % (end+1)`` and
    ``idx < start``.

    Defaults (start=1, end=11):
      - group length 12 → frames 0..11
      - frame 0 is waste; frames 1..11 are valid ("burst 1–11")
      - also drops 12, 24, … (same rule)

    Continuous / unset: ``end <= 0`` or ``end >= 0xFFFF`` → never treat as waste
    (0xFFFF is the continuous-run sentinel used with these regs).
    Invalid ``start > end`` (non-continuous) → never treat as waste (caller should
    reject the config before writing regs).

    Without a GPS seq the caller must skip this helper (no reliable index).
    """
    e = int(end) & 0xFFFF
    s = int(start) & 0xFFFF
    if e <= 0 or e >= 0xFFFF:
        return False
    if s > e:
        return False
    period = e + 1
    idx = (int(seq) & 0xFFFFFFFF) % period
    return idx < s


def discard_frames(cam: "SoftCamera", count: int, *, live: bool = False, timeout_s: float = 45.0) -> None:
    for _ in range(max(0, int(count))):
        if live:
            cam.capture_live_frame(parse_gps=True, timeout_s=min(timeout_s, 20.0))
        else:
            cam.capture_single(parse_gps=True, timeout_s=timeout_s)


def grab_valid_gps(
    cam: "SoftCamera",
    *,
    live: bool = False,
    attempts: int = 4,
    discards: int = 0,
    timeout_s: float = 45.0,
    lat_range: tuple[float, float] | None = None,
    lon_range: tuple[float, float] | None = None,
    min_exp_us: float = 0.0,
    require_seq: int | None = None,
    settle_s: float = 0.05,
) -> tuple[FrameResult, GpsInfo | None]:
    """Capture until GPS header passes optional gates; return last frame if none pass."""
    discard_frames(cam, discards, live=live, timeout_s=timeout_s)
    last: tuple[FrameResult, GpsInfo | None] | None = None
    for i in range(max(1, int(attempts))):
        if live:
            frame = cam.capture_live_frame(parse_gps=True, timeout_s=min(timeout_s, 20.0))
        else:
            frame = cam.capture_single(parse_gps=True, timeout_s=timeout_s)
        gps = frame.gps or (
            parse_gps_header(frame.data, expect_w=frame.width, expect_h=frame.height)
            if len(frame.data) >= 44
            else None
        )
        last = (frame, gps)
        if not gps_header_issues(
            gps,
            lat_range=lat_range,
            lon_range=lon_range,
            min_exp_us=min_exp_us,
            require_seq=require_seq,
            expect_w=frame.width,
            expect_h=frame.height,
        ):
            return frame, gps
        if settle_s:
            time.sleep(settle_s)
    assert last is not None
    return last


def measure_exp_jitter(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "min": None, "max": None, "mean": None, "ptp": None}
    mean = sum(values) / n
    return {
        "n": n,
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "ptp": max(values) - min(values),
    }


def _ensure_highlevel_bindings(api: QhyApi) -> None:
    if getattr(api, "_hl_bound", False):
        return
    dll = api.dll

    dll.InitQHYCCD.argtypes = [wintypes.HANDLE]
    dll.InitQHYCCD.restype = wintypes.DWORD

    dll.SetQHYCCDParam.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_double]
    dll.SetQHYCCDParam.restype = wintypes.DWORD

    dll.GetQHYCCDParam.argtypes = [wintypes.HANDLE, ctypes.c_int]
    dll.GetQHYCCDParam.restype = ctypes.c_double

    dll.IsQHYCCDControlAvailable.argtypes = [wintypes.HANDLE, ctypes.c_int]
    dll.IsQHYCCDControlAvailable.restype = wintypes.DWORD

    dll.GetQHYCCDChipInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(wintypes.DWORD),
    ]
    dll.GetQHYCCDChipInfo.restype = wintypes.DWORD

    if hasattr(dll, "GetQHYCCDReadModeResolution"):
        dll.GetQHYCCDReadModeResolution.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        dll.GetQHYCCDReadModeResolution.restype = wintypes.DWORD

    if hasattr(dll, "GetQHYCCDEffectiveArea"):
        dll.GetQHYCCDEffectiveArea.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        dll.GetQHYCCDEffectiveArea.restype = wintypes.DWORD

    if hasattr(dll, "GetQHYCCDOverScanArea"):
        dll.GetQHYCCDOverScanArea.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        dll.GetQHYCCDOverScanArea.restype = wintypes.DWORD

    dll.SetQHYCCDBinMode.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    dll.SetQHYCCDBinMode.restype = wintypes.DWORD

    dll.SetQHYCCDResolution.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    dll.SetQHYCCDResolution.restype = wintypes.DWORD

    dll.GetQHYCCDMemLength.argtypes = [wintypes.HANDLE]
    dll.GetQHYCCDMemLength.restype = wintypes.DWORD

    dll.ExpQHYCCDSingleFrame.argtypes = [wintypes.HANDLE]
    dll.ExpQHYCCDSingleFrame.restype = wintypes.DWORD

    dll.GetQHYCCDSingleFrame.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_ubyte),
    ]
    dll.GetQHYCCDSingleFrame.restype = wintypes.DWORD

    if hasattr(dll, "BeginQHYCCDLive"):
        dll.BeginQHYCCDLive.argtypes = [wintypes.HANDLE]
        dll.BeginQHYCCDLive.restype = wintypes.DWORD

    if hasattr(dll, "GetQHYCCDLiveFrame"):
        dll.GetQHYCCDLiveFrame.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        dll.GetQHYCCDLiveFrame.restype = wintypes.DWORD

    if hasattr(dll, "StopQHYCCDLive"):
        dll.StopQHYCCDLive.argtypes = [wintypes.HANDLE]
        dll.StopQHYCCDLive.restype = wintypes.DWORD

    if hasattr(dll, "GetQHYCCDParamMinMaxStep"):
        dll.GetQHYCCDParamMinMaxStep.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
        ]
        dll.GetQHYCCDParamMinMaxStep.restype = wintypes.DWORD

    if hasattr(dll, "CancelQHYCCDExposingAndReadout"):
        dll.CancelQHYCCDExposingAndReadout.argtypes = [wintypes.HANDLE]
        dll.CancelQHYCCDExposingAndReadout.restype = wintypes.DWORD

    dll.GetQHYCCDNumberOfReadModes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    dll.GetQHYCCDNumberOfReadModes.restype = wintypes.DWORD

    dll.GetQHYCCDReadModeName.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_char_p]
    dll.GetQHYCCDReadModeName.restype = wintypes.DWORD

    dll.SetQHYCCDReadMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.SetQHYCCDReadMode.restype = wintypes.DWORD

    dll.SetQHYCCDGPSVCOXFreq.argtypes = [wintypes.HANDLE, wintypes.WORD]
    dll.SetQHYCCDGPSVCOXFreq.restype = wintypes.DWORD

    dll.SetQHYCCDGPSLedCalMode.argtypes = [wintypes.HANDLE, ctypes.c_ubyte]
    dll.SetQHYCCDGPSLedCalMode.restype = wintypes.DWORD

    dll.SetQHYCCDGPSPOSA.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_ubyte]
    dll.SetQHYCCDGPSPOSA.restype = None

    dll.SetQHYCCDGPSPOSB.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_ubyte]
    dll.SetQHYCCDGPSPOSB.restype = None

    dll.SetQHYCCDGPSMasterSlave.argtypes = [wintypes.HANDLE, ctypes.c_ubyte]
    dll.SetQHYCCDGPSMasterSlave.restype = wintypes.DWORD

    dll.SetQHYCCDGPSSlaveModeParameter.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    dll.SetQHYCCDGPSSlaveModeParameter.restype = None

    api._hl_bound = True


def _check(ret: int, what: str) -> None:
    if int(ret) != QHYCCD_SUCCESS:
        raise RuntimeError(f"{what} failed, ret={ret}")


def _check_exp(ret: int, what: str = "ExpQHYCCDSingleFrame") -> int:
    """Accept SUCCESS or QHYCCD_READ_DIRECTLY (0x2001 = read now)."""
    code = int(ret) & 0xFFFFFFFF
    if code in (QHYCCD_SUCCESS, QHYCCD_READ_DIRECTLY):
        return code
    raise RuntimeError(f"{what} failed, ret={ret}")


class SoftCamera:
    """QHYCCD session: single-frame + live capture, GPS, ROI, DDR."""

    def __init__(self, ctx: CameraCtx):
        self.ctx = ctx
        self.api = ctx.api
        _ensure_highlevel_bindings(self.api)
        self.image_w = 0
        self.image_h = 0
        self.bpp = 16
        self._inited = False
        self._live = False
        self._last_read_mode: int | None = None
        self._last_stream_mode: int | None = None
        self._frame_buf = None
        self._frame_buf_len = 0
        self._session_dirty = False  # stop_live 失败等：下次 _prepare_session 强制 reopen
        self.geometry: dict = {}
        self.settings = CameraSettings()

    @property
    def handle(self):
        return self.ctx.handle

    def set_stream_mode(self, mode: int = STREAM_SINGLE) -> None:
        _check(self.api.dll.SetQHYCCDStreamMode(self.handle, mode), "SetQHYCCDStreamMode")

    def init_camera(self) -> None:
        _check(self.api.dll.InitQHYCCD(self.handle), "InitQHYCCD")
        self._inited = True

    def list_read_modes(self) -> list[tuple[int, str]]:
        num = wintypes.DWORD()
        _check(self.api.dll.GetQHYCCDNumberOfReadModes(self.handle, ctypes.byref(num)), "GetQHYCCDNumberOfReadModes")
        out: list[tuple[int, str]] = []
        for i in range(int(num.value)):
            buf = ctypes.create_string_buffer(64)
            ret = self.api.dll.GetQHYCCDReadModeName(self.handle, i, buf)
            name = buf.value.decode("ascii", errors="ignore") if ret == 0 else f"mode_{i}"
            out.append((i, name))
        return out

    def list_bin_modes(self) -> list[dict]:
        """Square bin modes advertised by IsQHYCCDControlAvailable(CAM_BINnXnMODE)."""
        table = (
            (CAM_BIN1X1MODE, 1),
            (CAM_BIN2X2MODE, 2),
            (CAM_BIN3X3MODE, 3),
            (CAM_BIN4X4MODE, 4),
        )
        out: list[dict] = []
        for cid, n in table:
            if self.is_control_available(cid):
                out.append({"bin_x": n, "bin_y": n, "label": f"{n}x{n}"})
        if not any(item["bin_x"] == 1 for item in out):
            out.insert(0, {"bin_x": 1, "bin_y": 1, "label": "1x1"})
        return out

    def set_read_mode(self, mode: int) -> None:
        _check(self.api.dll.SetQHYCCDReadMode(self.handle, mode), f"SetQHYCCDReadMode({mode})")
        self._last_read_mode = int(mode)

    def get_read_mode_resolution(self, mode: int | None = None) -> tuple[int, int]:
        """GetQHYCCDReadModeResolution — native W/H for a read mode (Ezcap)."""
        fn = getattr(self.api.dll, "GetQHYCCDReadModeResolution", None)
        if fn is None:
            raise RuntimeError("GetQHYCCDReadModeResolution not in DLL")
        m = int(self.settings.read_mode if mode is None else mode)
        imagew = wintypes.DWORD()
        imageh = wintypes.DWORD()
        _check(fn(self.handle, m, ctypes.byref(imagew), ctypes.byref(imageh)), "GetQHYCCDReadModeResolution")
        return int(imagew.value), int(imageh.value)

    def get_effective_area(self) -> dict:
        """GetQHYCCDEffectiveArea → start_x/y, size_x/y."""
        fn = getattr(self.api.dll, "GetQHYCCDEffectiveArea", None)
        if fn is None:
            raise RuntimeError("GetQHYCCDEffectiveArea not in DLL")
        sx = wintypes.DWORD()
        sy = wintypes.DWORD()
        wx = wintypes.DWORD()
        wy = wintypes.DWORD()
        _check(fn(self.handle, ctypes.byref(sx), ctypes.byref(sy), ctypes.byref(wx), ctypes.byref(wy)), "GetQHYCCDEffectiveArea")
        return {"start_x": int(sx.value), "start_y": int(sy.value), "size_x": int(wx.value), "size_y": int(wy.value)}

    def get_overscan_area(self) -> dict:
        """GetQHYCCDOverScanArea → start_x/y, size_x/y."""
        fn = getattr(self.api.dll, "GetQHYCCDOverScanArea", None)
        if fn is None:
            raise RuntimeError("GetQHYCCDOverScanArea not in DLL")
        sx = wintypes.DWORD()
        sy = wintypes.DWORD()
        wx = wintypes.DWORD()
        wy = wintypes.DWORD()
        _check(fn(self.handle, ctypes.byref(sx), ctypes.byref(sy), ctypes.byref(wx), ctypes.byref(wy)), "GetQHYCCDOverScanArea")
        return {"start_x": int(sx.value), "start_y": int(sy.value), "size_x": int(wx.value), "size_y": int(wy.value)}

    def refresh_geometry(self, *, read_mode: int | None = None, bin_x: int = 1, bin_y: int = 1) -> dict:
        """Ezcap post-Init geometry: ChipInfo + ReadModeResolution + Effective/Overscan + full ROI after bin.

        Full-frame SetResolution uses start=0,0 and size = minImageW/binx, minImageH/biny
        (managementMenu after read-mode / BIN change).
        """
        chip = self.get_chip_info()
        mode = int(self.settings.read_mode if read_mode is None else read_mode)
        rm_w = rm_h = 0
        try:
            rm_w, rm_h = self.get_read_mode_resolution(mode)
        except Exception:
            rm_w = rm_h = 0

        # Ezcap: minImage starts as ChipInfo; shrink if ReadModeResolution product is smaller.
        min_w = int(chip["image_w"])
        min_h = int(chip["image_h"])
        if rm_w > 0 and rm_h > 0:
            if (rm_w * rm_h) < (min_w * min_h) or min_w == 0 or min_h == 0:
                min_w, min_h = rm_w, rm_h

        effective = None
        overscan = None
        try:
            effective = self.get_effective_area()
        except Exception:
            pass
        try:
            overscan = self.get_overscan_area()
        except Exception:
            pass

        bx = max(1, int(bin_x))
        by = max(1, int(bin_y))
        full = {
            "start_x": 0,
            "start_y": 0,
            "size_x": max(1, min_w // bx),
            "size_y": max(1, min_h // by),
            "unbinned_w": min_w,
            "unbinned_h": min_h,
            "bin_x": bx,
            "bin_y": by,
        }
        geo = {
            "chip": chip,
            "read_mode": mode,
            "read_mode_w": rm_w,
            "read_mode_h": rm_h,
            "min_image_w": min_w,
            "min_image_h": min_h,
            "effective": effective,
            "overscan": overscan,
            "full_frame": full,
            "chip_vs_readmode_mismatch": bool(rm_w and rm_h and (chip["image_w"] != rm_w or chip["image_h"] != rm_h)),
        }
        self.geometry = geo
        return geo

    def resolve_resolution(
        self,
        *,
        roi_x: int = 0,
        roi_y: int = 0,
        roi_w: int | None = None,
        roi_h: int | None = None,
        bin_x: int = 1,
        bin_y: int = 1,
        read_mode: int | None = None,
        use_full_if_empty: bool = True,
    ) -> tuple[int, int, int, int, dict]:
        """Return (start_x, start_y, size_x, size_y, geometry) for SetQHYCCDResolution."""
        geo = self.refresh_geometry(read_mode=read_mode, bin_x=bin_x, bin_y=bin_y)
        full = geo["full_frame"]
        empty = roi_w is None and roi_h is None and int(roi_x) == 0 and int(roi_y) == 0
        if use_full_if_empty and empty:
            return full["start_x"], full["start_y"], full["size_x"], full["size_y"], geo
        sx = int(roi_x)
        sy = int(roi_y)
        sw = int(full["size_x"] if roi_w is None else roi_w)
        sh = int(full["size_y"] if roi_h is None else roi_h)
        fx = int(full.get("start_x", 0) or 0)
        fy = int(full.get("start_y", 0) or 0)
        fw = int(full.get("size_x", 0) or 0)
        fh = int(full.get("size_y", 0) or 0)
        # 非空 ROI 相对全幅越界 / 非法 → 回退全幅，避免 SetResolution/取帧失败
        if fw > 0 and fh > 0:
            oob = (
                sx < fx
                or sy < fy
                or sw <= 0
                or sh <= 0
                or sx + sw > fx + fw
                or sy + sh > fy + fh
            )
            if oob:
                cprint(
                    f"ROI ({sx},{sy},{sw}x{sh}) out of full ({fx},{fy},{fw}x{fh}); fallback full",
                    Color.YELLOW,
                )
                return fx, fy, fw, fh, geo
        return sx, sy, sw, sh, geo

    def get_chip_info(self) -> dict:
        chipw = ctypes.c_double()
        chiph = ctypes.c_double()
        imagew = wintypes.DWORD()
        imageh = wintypes.DWORD()
        pixelw = ctypes.c_double()
        pixelh = ctypes.c_double()
        bpp = wintypes.DWORD()
        _check(
            self.api.dll.GetQHYCCDChipInfo(
                self.handle,
                ctypes.byref(chipw),
                ctypes.byref(chiph),
                ctypes.byref(imagew),
                ctypes.byref(imageh),
                ctypes.byref(pixelw),
                ctypes.byref(pixelh),
                ctypes.byref(bpp),
            ),
            "GetQHYCCDChipInfo",
        )
        self.image_w = int(imagew.value)
        self.image_h = int(imageh.value)
        self.bpp = int(bpp.value)
        return {
            "chip_w": chipw.value,
            "chip_h": chiph.value,
            "image_w": self.image_w,
            "image_h": self.image_h,
            "pixel_w": pixelw.value,
            "pixel_h": pixelh.value,
            "bpp": self.bpp,
        }

    def set_bin_mode(self, wbin: int = 1, hbin: int = 1) -> None:
        _check(self.api.dll.SetQHYCCDBinMode(self.handle, wbin, hbin), "SetQHYCCDBinMode")

    def set_resolution(self, x: int = 0, y: int = 0, xsize: int | None = None, ysize: int | None = None) -> None:
        if xsize is None or ysize is None:
            info = self.get_chip_info()
            xsize = xsize or info["image_w"]
            ysize = ysize or info["image_h"]
        _check(
            self.api.dll.SetQHYCCDResolution(self.handle, x, y, int(xsize), int(ysize)),
            "SetQHYCCDResolution",
        )
        self.image_w = int(xsize)
        self.image_h = int(ysize)

    def set_param(self, control_id: int, value: float) -> None:
        _check(self.api.dll.SetQHYCCDParam(self.handle, control_id, float(value)), f"SetQHYCCDParam({control_id})")

    def try_set_param(self, control_id: int, value: float, *, name: str) -> bool:
        """Set an optional control. Missing/out-of-range/rejected values are skipped."""
        if not self.is_control_available(control_id):
            return False
        v = float(value)
        rng = self.get_param_range(control_id)
        if rng:
            lo, hi = float(rng["min"]), float(rng["max"])
            if hi >= lo and (v < lo or v > hi):
                cprint(f"Skip {name}={v:g}: out of range [{lo:g}, {hi:g}]", Color.YELLOW)
                return False
        try:
            self.set_param(control_id, v)
            return True
        except RuntimeError as exc:
            cprint(f"Skip {name}={v:g}: {exc}", Color.YELLOW)
            return False

    def get_param(self, control_id: int) -> float:
        return float(self.api.dll.GetQHYCCDParam(self.handle, control_id))

    def is_control_available(self, control_id: int) -> bool:
        return int(self.api.dll.IsQHYCCDControlAvailable(self.handle, control_id)) == QHYCCD_SUCCESS

    def set_exposure_us(self, us: float) -> None:
        self.set_param(CONTROL_EXPOSURE, us)

    def set_bits(self, bits: int) -> None:
        self.set_param(CONTROL_TRANSFERBIT, float(bits))
        self.bpp = int(bits)

    def get_param_range(self, control_id: int) -> dict | None:
        fn = getattr(self.api.dll, "GetQHYCCDParamMinMaxStep", None)
        if fn is None:
            return None
        mn = ctypes.c_double()
        mx = ctypes.c_double()
        step = ctypes.c_double()
        ret = int(fn(self.handle, control_id, ctypes.byref(mn), ctypes.byref(mx), ctypes.byref(step)))
        if ret != QHYCCD_SUCCESS:
            return None
        return {"min": mn.value, "max": mx.value, "step": step.value}

    def set_speed(self, speed: float) -> None:
        self.set_param(CONTROL_SPEED, speed)

    def set_usb_traffic(self, value: float) -> None:
        self.set_param(CONTROL_USBTRAFFIC, value)

    def set_ddr_sdk(self, on: bool) -> None:
        if self.is_control_available(CONTROL_DDR):
            self.set_param(CONTROL_DDR, 1.0 if on else 0.0)
            return
        self.set_ddr_vendor(on)

    def set_ddr_vendor(self, on: bool) -> None:
        """Low-level A9 via vend 0xD1 (0xFF=on, 0x00=off)."""
        payload = bytes([0xA9, 0xFF if on else 0x00] + [0] * 14)
        self.api.vend_write(self.handle, 0xD1, 0, 0, payload)

    def _prepare_session(self, read_mode: int, stream_mode: int) -> None:
        """Ezcap-style session: SetReadMode → SetStreamMode → Init (reopen if mode changes)."""
        from qhyccd_api import reopen_camera

        if self._live:
            self.stop_live()

        rm = int(read_mode)
        sm = int(stream_mode)
        mode_changed = self._inited and (
            self._last_read_mode != rm or self._last_stream_mode != sm
        )
        need_reopen = mode_changed or bool(self._session_dirty)
        if need_reopen:
            try:
                reopen_camera(self.ctx)
                self._inited = False
                self._last_read_mode = None
                self._last_stream_mode = None
                self._session_dirty = False
            except Exception:
                self._session_dirty = True
                raise
            finally:
                # 成功/失败都丢掉旧缓冲，避免半 Init / 分辨率变化后沿用旧 MemLength
                self._frame_buf = None
                self._frame_buf_len = 0

        if not self._inited:
            # Must set read/stream before Init (SDK / Ezcap).
            try:
                self.set_read_mode(rm)
                self.set_stream_mode(sm)
                self.init_camera()
                self._last_stream_mode = sm
            except Exception:
                self._frame_buf = None
                self._frame_buf_len = 0
                self._session_dirty = True
                raise

    def apply_settings(self, settings: CameraSettings) -> dict:
        """Apply full CameraSettings (Ezcap order: ReadMode → StreamMode → Init → Bin → Resolution → params)."""
        self.settings = settings
        self._prepare_session(settings.read_mode, settings.stream_mode)

        self.set_bin_mode(settings.bin_x, settings.bin_y)
        sx, sy, sw, sh, geo = self.resolve_resolution(
            roi_x=settings.roi_x,
            roi_y=settings.roi_y,
            roi_w=settings.roi_w,
            roi_h=settings.roi_h,
            bin_x=settings.bin_x,
            bin_y=settings.bin_y,
            read_mode=settings.read_mode,
        )
        self.set_resolution(sx, sy, sw, sh)
        # Keep settings ROI synced to what was actually programmed.
        self.settings.roi_x = sx
        self.settings.roi_y = sy
        self.settings.roi_w = sw
        self.settings.roi_h = sh

        self.set_bits(settings.bits)
        self.set_exposure_us(settings.exposure_us)
        if settings.gain is not None:
            self.try_set_param(CONTROL_GAIN, settings.gain, name="gain")
        if settings.offset is not None:
            self.try_set_param(CONTROL_OFFSET, settings.offset, name="offset")
        if settings.usb_traffic is not None:
            self.try_set_param(CONTROL_USBTRAFFIC, settings.usb_traffic, name="usb_traffic")
        if settings.speed is not None:
            self.try_set_param(CONTROL_SPEED, settings.speed, name="speed")
        if settings.ddr_enable is not None:
            try:
                self.set_ddr_sdk(settings.ddr_enable)
            except RuntimeError as exc:
                cprint(f"Skip ddr: {exc}", Color.YELLOW)
        # GPS on → SDK SetGPSOn：写 Titan 142/39/56/35（帧头替换 + 帧序号）。
        # 仅主机解析：FPGA_SOFT_GPS_HOST_ONLY=1。
        try:
            self.set_gps_enable(bool(settings.gps_enable))
        except RuntimeError as exc:
            cprint(f"Skip CAM_GPS: {exc}", Color.YELLOW)
            self.settings.gps_enable = bool(settings.gps_enable)
        if settings.gps_enable:
            if settings.gps_slave:
                self.try_set_gps_master_slave(True)
            # Do not force MasterSlave=0: some cameras (QHY600) return ERROR.
            # POS/LED 仅在真正配置过时下发；默认 0/0 会塌曝光窗。
            if settings.gps_pos_configured():
                try:
                    self.apply_gps_led_positions(
                        pos_a=settings.gps_pos_a if settings.gps_pos_a is not None else 0,
                        pos_b=settings.gps_pos_b if settings.gps_pos_b is not None else 0,
                        width=settings.gps_pos_width,
                        slave=settings.gps_slave,
                        leave_led_cal_on=settings.gps_led_cal,
                    )
                except RuntimeError as exc:
                    cprint(f"Skip GPS POS/LED: {exc}", Color.YELLOW)
        return geo

    def apply_runtime_controls(self, settings: CameraSettings) -> list[str]:
        """Write live-safe params without Init, StopLive, bin, or ROI.

        Intended for GUI changes while connected / live: gain, offset, traffic,
        speed, bits, exposure, DDR, GPS enable/slave/POS/LED.
        """
        if not self._inited:
            return []
        prev = self.settings
        applied: list[str] = []

        def _changed(attr: str) -> bool:
            return getattr(settings, attr) != getattr(prev, attr)

        if _changed("bits"):
            try:
                self.set_bits(settings.bits)
                applied.append("bits")
                self._frame_buf = None
                self._frame_buf_len = 0
            except RuntimeError as exc:
                cprint(f"Skip bits={settings.bits}: {exc}", Color.YELLOW)
        if _changed("exposure_us"):
            try:
                self.set_exposure_us(settings.exposure_us)
                applied.append("exposure")
            except RuntimeError as exc:
                cprint(f"Skip exposure={settings.exposure_us:g}: {exc}", Color.YELLOW)
        if settings.gain is not None and _changed("gain"):
            if self.try_set_param(CONTROL_GAIN, settings.gain, name="gain"):
                applied.append("gain")
        if settings.offset is not None and _changed("offset"):
            if self.try_set_param(CONTROL_OFFSET, settings.offset, name="offset"):
                applied.append("offset")
        if settings.usb_traffic is not None and _changed("usb_traffic"):
            if self.try_set_param(CONTROL_USBTRAFFIC, settings.usb_traffic, name="usb_traffic"):
                applied.append("usb_traffic")
        if settings.speed is not None and _changed("speed"):
            if self.try_set_param(CONTROL_SPEED, settings.speed, name="speed"):
                applied.append("speed")
        if settings.ddr_enable is not None and _changed("ddr_enable"):
            try:
                self.set_ddr_sdk(settings.ddr_enable)
                applied.append("ddr")
            except RuntimeError as exc:
                cprint(f"Skip ddr: {exc}", Color.YELLOW)

        if _changed("gps_enable"):
            try:
                self.set_gps_enable(bool(settings.gps_enable))
                applied.append("gps")
            except RuntimeError as exc:
                cprint(f"Skip GPS enable={settings.gps_enable}: {exc}", Color.YELLOW)
                self.settings.gps_enable = bool(settings.gps_enable)
                applied.append("gps")
        if settings.gps_enable and _changed("gps_slave"):
            # MasterSlave(0) 在部分机型会 ERROR；try_ 吞掉失败，角色切回主机时仍尝试清掉。
            if self.try_set_gps_master_slave(bool(settings.gps_slave)):
                applied.append("gps_slave")
        # 只在 POS/LED 字段本身变化时下发；绝不能因勾选 GPS on 就带默认 0/0
        # 跑 LedCalMode（会把 start/end 锁成 0 → 出图异常）。
        pos_field_changed = (
            _changed("gps_pos_a")
            or _changed("gps_pos_b")
            or _changed("gps_pos_width")
            or _changed("gps_led_cal")
        )
        if settings.gps_enable and pos_field_changed and settings.gps_pos_configured():
            try:
                self.apply_gps_led_positions(
                    pos_a=settings.gps_pos_a if settings.gps_pos_a is not None else 0,
                    pos_b=settings.gps_pos_b if settings.gps_pos_b is not None else 0,
                    width=settings.gps_pos_width,
                    slave=settings.gps_slave,
                    leave_led_cal_on=settings.gps_led_cal,
                )
                applied.append("gps_pos")
            except RuntimeError as exc:
                cprint(f"Skip GPS POS/LED: {exc}", Color.YELLOW)

        self.settings.bits = settings.bits
        self.settings.exposure_us = settings.exposure_us
        self.settings.gain = settings.gain
        self.settings.offset = settings.offset
        self.settings.usb_traffic = settings.usb_traffic
        self.settings.speed = settings.speed
        self.settings.ddr_enable = settings.ddr_enable
        self.settings.gps_enable = settings.gps_enable
        self.settings.gps_slave = settings.gps_slave
        self.settings.gps_led_cal = settings.gps_led_cal
        self.settings.gps_pos_a = settings.gps_pos_a
        self.settings.gps_pos_b = settings.gps_pos_b
        self.settings.gps_pos_width = settings.gps_pos_width
        return applied

    def configure_single_frame(
        self,
        *,
        read_mode: int = 0,
        bits: int = 16,
        exposure_us: float = 100000.0,
        gain: float | None = None,
        offset: float | None = None,
        usb_traffic: float | None = None,
        binning: tuple[int, int] = (1, 1),
        speed: float | None = None,
        roi_x: int = 0,
        roi_y: int = 0,
        roi_w: int | None = None,
        roi_h: int | None = None,
        ddr_enable: bool | None = None,
        gps_enable: bool = False,
        gps_slave: bool = False,
        gps_led_cal: bool = False,
        gps_pos_a: int | None = None,
        gps_pos_b: int | None = None,
        gps_pos_width: int = 40,
    ) -> dict:
        """StreamMode=0 + Init + readout/resolution/exposure for single-frame capture."""
        settings = CameraSettings(
            stream_mode=STREAM_SINGLE,
            read_mode=read_mode,
            bits=bits,
            exposure_us=exposure_us,
            gain=gain,
            offset=offset,
            usb_traffic=usb_traffic,
            speed=speed,
            bin_x=binning[0],
            bin_y=binning[1],
            roi_x=roi_x,
            roi_y=roi_y,
            roi_w=roi_w,
            roi_h=roi_h,
            ddr_enable=ddr_enable,
            gps_enable=gps_enable,
            gps_slave=gps_slave,
            gps_led_cal=gps_led_cal,
            gps_pos_a=gps_pos_a,
            gps_pos_b=gps_pos_b,
            gps_pos_width=gps_pos_width,
        )
        return self.apply_settings(settings)

    def configure_live(
        self,
        *,
        read_mode: int = 0,
        bits: int = 8,
        exposure_us: float = 100000.0,
        gain: float | None = None,
        offset: float | None = None,
        usb_traffic: float | None = None,
        binning: tuple[int, int] = (1, 1),
        speed: float | None = None,
        roi_x: int = 0,
        roi_y: int = 0,
        roi_w: int | None = None,
        roi_h: int | None = None,
        ddr_enable: bool | None = None,
        gps_enable: bool = False,
        gps_slave: bool = False,
        gps_led_cal: bool = False,
        gps_pos_a: int | None = None,
        gps_pos_b: int | None = None,
        gps_pos_width: int = 40,
    ) -> dict:
        """StreamMode=1 + Init for continuous live capture."""
        settings = CameraSettings(
            stream_mode=STREAM_LIVE,
            read_mode=read_mode,
            bits=bits,
            exposure_us=exposure_us,
            gain=gain,
            offset=offset,
            usb_traffic=usb_traffic,
            speed=speed,
            bin_x=binning[0],
            bin_y=binning[1],
            roi_x=roi_x,
            roi_y=roi_y,
            roi_w=roi_w,
            roi_h=roi_h,
            ddr_enable=ddr_enable,
            gps_enable=gps_enable,
            gps_slave=gps_slave,
            gps_led_cal=gps_led_cal,
            gps_pos_a=gps_pos_a,
            gps_pos_b=gps_pos_b,
            gps_pos_width=gps_pos_width,
        )
        return self.apply_settings(settings)

    def camera_info(self) -> dict:
        if self._inited and not self.geometry:
            try:
                self.refresh_geometry(
                    read_mode=self.settings.read_mode,
                    bin_x=self.settings.bin_x,
                    bin_y=self.settings.bin_y,
                )
            except Exception:
                pass
        info = self.get_chip_info() if self._inited else {}
        modes = self.list_read_modes()
        controls = {}
        for cid, name in [
            (CONTROL_GAIN, "gain"),
            (CONTROL_OFFSET, "offset"),
            (CONTROL_EXPOSURE, "exposure"),
            (CONTROL_SPEED, "speed"),
            (CONTROL_TRANSFERBIT, "bits"),
            (CONTROL_USBTRAFFIC, "usb_traffic"),
            (CONTROL_DDR, "ddr"),
            (CAM_GPS, "gps"),
        ]:
            if self.is_control_available(cid):
                controls[name] = {
                    "available": True,
                    "value": self.get_param(cid) if self._inited else None,
                    "range": self.get_param_range(cid),
                }
            else:
                controls[name] = {"available": False}
        read_modes_detail = []
        for i, n in modes:
            entry = {"index": i, "name": n}
            if self._inited:
                try:
                    rw, rh = self.get_read_mode_resolution(i)
                    entry["image_w"] = rw
                    entry["image_h"] = rh
                except Exception:
                    pass
            read_modes_detail.append(entry)
        return {
            "camera_id": self.ctx.cam_id,
            "inited": self._inited,
            "live": self._live,
            "chip": info,
            "geometry": self.geometry,
            "read_modes": read_modes_detail,
            "bin_modes": self.list_bin_modes(),
            "controls": controls,
            "settings": self.settings.as_dict(),
            "programmed_resolution": {
                "start_x": self.settings.roi_x,
                "start_y": self.settings.roi_y,
                "size_x": self.settings.roi_w,
                "size_y": self.settings.roi_h,
                "image_w": self.image_w,
                "image_h": self.image_h,
            },
        }

    # --- GPS ---

    def read_fpga_be(self, start: int, nbytes: int, *, bus: int = 1) -> int:
        return self.ctx.read_fpga_be(start, nbytes, bus=bus)

    def write_fpga_be(self, start: int, nbytes: int, value: int, *, bus: int = 1) -> None:
        self.ctx.write_fpga_be(start, nbytes, value, bus=bus)

    def read_fpga_version(self, *, bus: int = 1) -> dict:
        return read_fpga_version(self.ctx, bus=bus)

    def set_gps_enable(self, on: bool) -> None:
        """Enable GPS frame-head overlay + frame counter via SDK SetGPSOn.

        QHY600 ``SetGPSOn(1)`` writes Titan regs (see ``qhy600base.cpp``):
        - 142=1  GPS head replace (``gpsadd_pixdata.gps_enable`` / ``reg1X42``)
        - 39=1   6PIN GPS GPIO mode
        - 56=1   FrameNumberEnable (``reg56``)
        - 35=1   frame counter run

        Verified on QHY600 PCIe: after CAM_GPS=1, overlay WH matches ROI and
        ``seq`` increments by 1 each frame; CAM_GPS=0 returns garbage head.

        Opt-out: ``FPGA_SOFT_GPS_HOST_ONLY=1`` → only set host parse flag.
        Extra: ``FPGA_SOFT_GPS_REG56=1`` → also ``write_fpga(56)`` (usually redundant).
        """
        on = bool(on)
        self.settings.gps_enable = on
        host_only = os.environ.get("FPGA_SOFT_GPS_HOST_ONLY", "").lower() in (
            "1",
            "on",
            "true",
        )
        if not host_only:
            self.set_param(CAM_GPS, 1.0 if on else 0.0)
        if os.environ.get("FPGA_SOFT_GPS_REG56", "").lower() in ("1", "on", "true"):
            self.ctx.write_fpga(56, 1 if on else 0)

    def set_gps_master_slave(self, slave: bool) -> None:
        """i=1 slave, i=0 master (Ezcap comBox_GPSSlaveOnOff)."""
        _check(
            self.api.dll.SetQHYCCDGPSMasterSlave(self.handle, 1 if slave else 0),
            "SetQHYCCDGPSMasterSlave",
        )

    def try_set_gps_master_slave(self, slave: bool) -> bool:
        fn = getattr(self.api.dll, "SetQHYCCDGPSMasterSlave", None)
        if fn is None:
            cprint("Skip GPS MasterSlave: not exported", Color.YELLOW)
            return False
        try:
            self.set_gps_master_slave(slave)
            return True
        except RuntimeError as exc:
            cprint(f"Skip GPS MasterSlave(slave={int(bool(slave))}): {exc}", Color.YELLOW)
            return False

    def set_gps_led_cal_mode(self, on: bool) -> None:
        _check(self.api.dll.SetQHYCCDGPSLedCalMode(self.handle, 1 if on else 0), "SetQHYCCDGPSLedCalMode")

    def set_gps_pos_a(self, is_slave: int, pos: int, width: int = 40) -> None:
        """POSA = Ezcap End position (GPSEnd digits)."""
        self.api.dll.SetQHYCCDGPSPOSA(self.handle, is_slave & 0xFF, pos & 0xFFFFFFFF, width & 0xFF)

    def set_gps_pos_b(self, is_slave: int, pos: int, width: int = 40) -> None:
        """POSB = Ezcap Start position (GPSStart digits)."""
        self.api.dll.SetQHYCCDGPSPOSB(self.handle, is_slave & 0xFF, pos & 0xFFFFFFFF, width & 0xFF)

    def apply_gps_led_positions(
        self,
        *,
        pos_a: int,
        pos_b: int,
        width: int = 40,
        slave: bool = False,
        leave_led_cal_on: bool = False,
    ) -> None:
        """Ezcap gpsTool On/Off sequence for LED calibration positions.

        LedCalMode(1) → POSB(start) → POSA(end) → LedCalMode(0|1)
        width defaults to 40 (Ezcap hard-coded).

        Master: POSB→start_cnt (mode1), POSA→end_cnt (mode2). While calibrat_mode
        stays non-zero the MAX10 keeps re-latching from the live led_count
        register, so leaving the LED on requires POSB to be the last write —
        otherwise start_cnt ends up holding POSA and the measured window
        collapses (verified on PCIE: 22.6us -> 16.19ms).
        """
        is_slave = 1 if slave else 0
        self.set_gps_led_cal_mode(True)
        # Slave: POSA→start_cnt (mode1), POSB→end_cnt (mode2). Write POSB last and
        # settle so FPGA can latch end_cnt while calibrat_mode==2 (XTRIG idle-high).
        if slave:
            self.set_gps_pos_a(is_slave, int(pos_a), int(width))
            time.sleep(0.05)
            self.set_gps_pos_b(is_slave, int(pos_b), int(width))
            time.sleep(0.10)
        elif leave_led_cal_on:
            self.set_gps_pos_a(is_slave, int(pos_a), int(width))
            time.sleep(0.05)
            self.set_gps_pos_b(is_slave, int(pos_b), int(width))
            time.sleep(0.05)
        else:
            # Ezcap master order: POSB then POSA
            self.set_gps_pos_b(is_slave, int(pos_b), int(width))
            time.sleep(0.05)
            self.set_gps_pos_a(is_slave, int(pos_a), int(width))
            time.sleep(0.05)
        self.set_gps_led_cal_mode(bool(leave_led_cal_on))
        self.settings.gps_pos_a = int(pos_a)
        self.settings.gps_pos_b = int(pos_b)
        self.settings.gps_pos_width = int(width)
        self.settings.gps_led_cal = bool(leave_led_cal_on)

    def set_gps_slave_mode_parameter(
        self,
        target_sec: int,
        target_us: int = 0,
        delta_t_sec: int = 0,
        delta_t_us: int = 0,
        exp_time_us: int = 100000,
    ) -> None:
        """Set target time / delta / exposure for GPS slave trigger (Ezcap Set UTC)."""
        self.api.dll.SetQHYCCDGPSSlaveModeParameter(
            self.handle,
            int(target_sec) & 0xFFFFFFFF,
            int(target_us) & 0xFFFFFFFF,
            int(delta_t_sec) & 0xFFFFFFFF,
            int(delta_t_us) & 0xFFFFFFFF,
            int(exp_time_us) & 0xFFFFFFFF,
        )

    def set_gps_slave_target_utc(
        self,
        year: int,
        month: int,
        day: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        *,
        target_us: int = 0,
        delta_t_sec: int = 0,
        delta_t_us: int = 0,
        exp_time_us: int = 100000,
    ) -> int:
        target = date_to_js1995(year, month, day, hour, minute, second)
        self.set_gps_slave_mode_parameter(target, target_us, delta_t_sec, delta_t_us, exp_time_us)
        return target

    def enable_gps_slave(self, *, led_cal: bool = False, pos_a: int | None = None, pos_b: int | None = None, width: int = 40) -> None:
        """CAM_GPS on + MasterSlave=slave. Optional Ezcap POSA/POSB under LED cal."""
        self.set_gps_enable(True)
        self.set_gps_master_slave(True)
        if pos_a is not None or pos_b is not None or led_cal:
            self.apply_gps_led_positions(
                pos_a=0 if pos_a is None else pos_a,
                pos_b=0 if pos_b is None else pos_b,
                width=width,
                slave=True,
                leave_led_cal_on=led_cal,
            )

    def disable_gps_slave(self) -> None:
        self.set_gps_master_slave(False)

    def disable_gps(self) -> None:
        self.set_gps_enable(False)

    # --- single frame ---

    def capture_single(self, *, parse_gps: bool = True, timeout_s: float = 60.0) -> FrameResult:
        _check_exp(self.api.dll.ExpQHYCCDSingleFrame(self.handle), "ExpQHYCCDSingleFrame")
        try:
            frame = self._grab_frame_buffer(live=False, timeout_s=timeout_s, parse_gps=parse_gps)
        except RuntimeError:
            cancel = getattr(self.api.dll, "CancelQHYCCDExposingAndReadout", None)
            if cancel is not None:
                try:
                    cancel(self.handle)
                except Exception:
                    pass
            raise
        if parse_gps and frame.gps is None and len(frame.data) >= 44:
            try:
                frame.gps = parse_gps_header(
                    frame.data, expect_w=frame.width, expect_h=frame.height
                )
            except Exception as exc:
                cprint(f"GPS parse skipped: {exc}", Color.YELLOW)
        return frame

    def begin_live(self) -> None:
        if self._live:
            return
        fn = getattr(self.api.dll, "BeginQHYCCDLive", None)
        if fn is None:
            raise RuntimeError("BeginQHYCCDLive not exported by qhyccd.dll")
        _check(fn(self.handle), "BeginQHYCCDLive")
        self._live = True

    def stop_live(self) -> None:
        if not self._live:
            return
        fn = getattr(self.api.dll, "StopQHYCCDLive", None)
        if fn is not None:
            try:
                ret = fn(self.handle)
                # 部分绑定无抛错只返回错误码；非 0 视为可能固件仍在 live
                if ret is not None and int(ret) != 0:
                    self._session_dirty = True
                    cprint(f"StopQHYCCDLive ret={ret}; mark session dirty", Color.YELLOW)
            except Exception as exc:
                self._session_dirty = True
                cprint(f"StopQHYCCDLive failed: {exc}; mark session dirty", Color.YELLOW)
        self._live = False

    def _grab_frame_buffer(
        self,
        *,
        live: bool,
        timeout_s: float,
        parse_gps: bool = True,
        copy_pixels: bool = True,
        preview_max_dim: int | None = None,
    ) -> FrameResult:
        mem = int(self.api.dll.GetQHYCCDMemLength(self.handle))
        if mem <= 0:
            raise RuntimeError(f"GetQHYCCDMemLength invalid: {mem}")
        if self._frame_buf is None or self._frame_buf_len < mem:
            self._frame_buf = (ctypes.c_ubyte * mem)()
            self._frame_buf_len = mem
        buf = self._frame_buf
        w = wintypes.DWORD()
        h = wintypes.DWORD()
        bpp = wintypes.DWORD()
        channels = wintypes.DWORD()
        get_fn = self.api.dll.GetQHYCCDLiveFrame if live else self.api.dll.GetQHYCCDSingleFrame
        deadline = time.time() + timeout_s
        last = QHYCCD_ERROR
        while time.time() < deadline:
            last = int(
                get_fn(
                    self.handle,
                    ctypes.byref(w),
                    ctypes.byref(h),
                    ctypes.byref(bpp),
                    ctypes.byref(channels),
                    buf,
                )
            )
            if last == QHYCCD_SUCCESS and int(w.value) > 0 and int(h.value) > 0:
                break
            time.sleep(0.02 if live else 0.05)
        else:
            raise RuntimeError(f"{'GetQHYCCDLiveFrame' if live else 'GetQHYCCDSingleFrame'} timeout, last_ret={last}")
        width = int(w.value)
        height = int(h.value)
        bits = int(bpp.value)
        ch = max(1, int(channels.value))
        raw_len = width * height * max(1, (bits + 7) // 8) * ch
        header_n = min(64, mem, max(raw_len, 64))
        header = ctypes.string_at(buf, header_n)
        gps = None
        if parse_gps and len(header) >= 44:
            try:
                gps = parse_gps_header(header, expect_w=width, expect_h=height)
            except Exception:
                gps = None
        preview_w = preview_h = 0
        preview_gray = b""
        if preview_max_dim:
            from fpga_soft_image import downscale_gray8_cbuf

            preview_w, preview_h, preview_gray = downscale_gray8_cbuf(
                buf, width, height, bits, max_dim=int(preview_max_dim)
            )
        if copy_pixels:
            data = ctypes.string_at(buf, max(raw_len, header_n))
        else:
            data = header
        return FrameResult(
            width=width,
            height=height,
            bpp=bits,
            channels=ch,
            data=data,
            gps=gps,
            preview_w=preview_w,
            preview_h=preview_h,
            preview_gray=preview_gray,
        )

    def capture_live_frame(
        self,
        *,
        parse_gps: bool = True,
        timeout_s: float = 10.0,
        copy_pixels: bool = True,
        preview_max_dim: int | None = None,
    ) -> FrameResult:
        if not self._live:
            self.begin_live()
        return self._grab_frame_buffer(
            live=True,
            timeout_s=timeout_s,
            parse_gps=parse_gps,
            copy_pixels=copy_pixels,
            preview_max_dim=preview_max_dim,
        )

    def save_frame_sidecar(self, path: Path | str, frame: FrameResult) -> Path:
        """Write JSON metadata next to raw file."""
        raw = Path(path)
        meta = raw.with_suffix(raw.suffix + ".meta.json")
        doc = {
            "width": frame.width,
            "height": frame.height,
            "bpp": frame.bpp,
            "channels": frame.channels,
            "nbytes": len(frame.data),
            "gps": frame.gps.as_dict() if frame.gps else None,
            "settings": self.settings.as_dict(),
        }
        meta.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return meta

    def save_raw(self, path: Path | str, frame: FrameResult) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(frame.data)
        return out


def open_soft_camera(
    project_root: Path,
    *,
    camera_name: str | None = None,
) -> SoftCamera:
    api = QhyApi(project_root)
    ids = scan_cameras(api)
    if not ids:
        raise RuntimeError("no camera detected")
    if camera_name:
        ctx = open_camera_by_name(api, ids, camera_name)
    else:
        ctx = open_selected_camera(api, ids)
    cprint(f"Opened camera: {ctx.cam_id}", Color.GREEN)
    return SoftCamera(ctx)


def close_soft_camera(cam: SoftCamera | None) -> None:
    if cam is None:
        return
    try:
        cam.stop_live()
    except Exception:
        pass
    close_camera(cam.api, cam.ctx)
