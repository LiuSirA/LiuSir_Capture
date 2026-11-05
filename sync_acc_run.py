# -*- coding: utf-8 -*-
"""正式同步精度测试 runner（数据与代码分离）。

数据默认落盘：J:\\Mywork\\Project_LH\\sync_acc_runs\\20261102
可用环境变量 SYNC_ACC_DATA_ROOT 覆盖。

Phase A：--gate 做 traffic 丢帧门禁（全幅 1x1 起）。
采集基于 capture2：释放前排空 + 指纹去重；GPS seq 记入文件名与 meta jsonl。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

CODE_ROOT = Path(__file__).resolve().parent
HOST = Path(os.environ.get("SYNC_ACC_HOST", "").strip() or (CODE_ROOT / "host"))
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(CODE_ROOT))

from qhyccd_highlevel import (  # noqa: E402
    QhyApi,
    scan_cameras,
    open_camera_by_name,
    close_camera,
    SoftCamera,
    CameraSettings,
    STREAM_LIVE,
)
from fpga_soft_commands import expand_mode_steps  # noqa: E402

DEFAULT_DATA_ROOT = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261102")

# USB / PCIe soft-mode keys (host/fpga_soft_modes.json).
# RELEASE/EXIT have no PCIe-specific variants; FPGA IDLE release / restore are shared.
MODE_RELEASE = "主从同步_主机触发开拍(释放IDLE)"
MODE_EXIT = "主从同步_退出同步拍摄(恢复单机默认)"
MODE_SETS = {
    "usb": {
        "master": "主从同步_主机(Master)一键设置",
        "slave": "主从同步_从机(Slave)一键设置",
        "release": MODE_RELEASE,
        "exit": MODE_EXIT,
        # USB one-key already embeds patch=32001; reinforce key kept for clarity:
        "burst_patch": "Burst补包_USB (patch 32001)",
        "patch_value": 32001,
    },
    "pcie": {
        "master": "主从同步_主机配置_PCIe光纤(压住IDLE)",
        "slave": "主从同步_从机配置_PCIe光纤",
        "release": MODE_RELEASE,  # shared: release IDLE after Live
        "exit": MODE_EXIT,        # shared: restore single-cam defaults
        # PCIe one-key already embeds 8192 (41..44 = 0,0,32,0); reinforce explicitly:
        "burst_patch": "Burst补包_PCIe光纤 (patch 8192)",
        "patch_value": 8192,
    },
}


def resolve_modes(transport: str) -> dict:
    t = (transport or "usb").strip().lower()
    if t not in MODE_SETS:
        raise ValueError(f"unknown transport={transport!r}; use usb|pcie")
    return dict(MODE_SETS[t], transport=t)


def data_root() -> Path:
    env = os.environ.get("SYNC_ACC_DATA_ROOT", "").strip()
    p = Path(env) if env else DEFAULT_DATA_ROOT
    for sub in ("groups", "reports", "logs", "status"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def clean_id(b: bytes) -> str:
    i = b.find(b"\x00")
    return (b[:i] if i >= 0 else b).decode("ascii", errors="replace")


def load_modes() -> dict:
    path = HOST / "fpga_soft_modes.json"
    return json.loads(path.read_text(encoding="utf-8"))["modes"]


def write_mode(ctx, modes: dict, key: str) -> None:
    if key not in modes:
        raise RuntimeError(f"mode missing: {key}")
    for st in expand_mode_steps(list(modes[key].get("steps") or [])):
        cmd = str(st.get("cmd", "")).upper()
        if cmd == "WRITE_FPGA":
            params = st.get("params") or {}
            ctx.write_fpga(int(params["index"]), int(params["value"]))
        elif cmd in {"WRITE_FPGA2", "WRITE_FPGA3", "WRITE_CMOS"}:
            # not needed for USB sync presets
            pass


def write_burst_window(ctx, start: int, end: int) -> None:
    """Override FPGA burst window after one-key (reg50 / 51 / 52)."""
    start = int(start) & 0xFF
    end = int(end) & 0xFFFF
    ctx.write_fpga(50, start)
    ctx.write_fpga(51, (end >> 8) & 0xFF)
    ctx.write_fpga(52, end & 0xFF)


def expected_seq_set(start: int, end: int) -> set[int] | None:
    """User/GUI rule: burststart+1 .. burstend-1."""
    if end <= 0 or end >= 0xFFFF:
        return None
    lo = int(start) + 1
    hi = int(end) - 1
    if hi < lo:
        return None
    return set(range(lo, hi + 1))


def seq_index(seq: int, end: int) -> int:
    period = int(end) + 1
    return (int(seq) & 0xFFFFFFFF) % period


def seq_cycle(seq: int, end: int) -> int:
    period = int(end) + 1
    return (int(seq) & 0xFFFFFFFF) // period


def analyze_seqs(observed_raw: list[int], start: int, end: int) -> dict:
    """Longest consecutive absolute GPS run inside expected band; idx = seq % (end+1)."""
    expected = expected_seq_set(start, end) or set()
    period = int(end) + 1
    in_band: list[int] = []
    for s in observed_raw:
        s_i = int(s) & 0xFFFFFFFF
        idx = s_i % period
        if idx in expected:
            in_band.append(s_i)
    in_band = sorted(set(in_band))
    if not in_band:
        return {
            "cycle": None,
            "observed_idx": [],
            "missing": sorted(expected),
            "raw_in_cycle": [],
            "complete": False,
            "run_len": 0,
        }

    best_run: list[int] = [in_band[0]]
    cur: list[int] = [in_band[0]]
    for s in in_band[1:]:
        if s == cur[-1] + 1:
            cur.append(s)
        else:
            if len(cur) > len(best_run):
                best_run = cur
            cur = [s]
    if len(cur) > len(best_run):
        best_run = cur

    idxs = sorted({(s % period) for s in best_run})
    missing = sorted(expected - set(idxs))
    cycle = best_run[0] // period
    return {
        "cycle": cycle,
        "observed_idx": idxs,
        "missing": missing,
        "raw_in_cycle": list(best_run),
        "complete": not missing,
        "run_len": len(best_run),
    }
def missing_seqs(observed_raw: list[int], start: int, end: int, *, frozen: bool = True) -> list[int]:
    """Compat wrapper: missing expected indices in the best cycle."""
    info = analyze_seqs(observed_raw, start, end)
    missing = list(info["missing"])
    if (not frozen) and info["observed_idx"]:
        hi_obs = max(info["observed_idx"])
        return [x for x in missing if x <= hi_obs]
    return missing


def to_gray8(fr) -> np.ndarray:
    w, h = fr.width, fr.height
    n = w * h
    if fr.bpp > 8:
        return (np.frombuffer(fr.data, dtype="<u2", count=n) >> 8).astype("uint8").reshape(h, w)
    return np.frombuffer(fr.data, dtype="uint8", count=n).reshape(h, w)


def fingerprint(img: np.ndarray) -> str:
    h, w = img.shape
    th, tw = 320, 480
    ys = max(1, int(round(h / th)))
    xs = max(1, int(round(w / tw)))
    small = img[::ys, ::xs]
    return hashlib.sha1(small.tobytes()).hexdigest()


@dataclass
class FrameRec:
    tag: str
    save_idx: int
    seq: int | None
    path: str
    width: int
    height: int
    fp: str
    gps: dict | None


class Collector:
    def __init__(
        self,
        cam: SoftCamera,
        tag: str,
        gdir: Path,
        save_images: bool,
        *,
        burst_start: int | None = None,
        burst_end: int | None = None,
        lock_cycle: bool = True,
    ):
        self.cam = cam
        self.tag = tag
        self.gdir = gdir
        self.save_images = save_images
        self.burst_start = burst_start
        self.burst_end = burst_end
        self.lock_cycle = lock_cycle
        self.cycle: int | None = None
        self.last_fp: str | None = None
        self.frames: list[FrameRec] = []

    @property
    def n_saved(self) -> int:
        return len(self.frames)

    @property
    def seqs(self) -> list[int]:
        out = []
        for fr in self.frames:
            if fr.seq is not None:
                out.append(int(fr.seq))
        return out

    def grab_new(self, timeout_s: float = 4.0) -> FrameRec | None:
        # Drop-check path: GPS is in the header. Avoid full-frame host copies unless saving JPG.
        need_pixels = bool(self.save_images)
        _kw = {"want_data": need_pixels} if hasattr(self.cam, "_w") else {}
        fr = self.cam.capture_live_frame(
            parse_gps=True, timeout_s=timeout_s, copy_pixels=need_pixels, **_kw
        )
        if fr is None:
            return None
        seq = None
        gps_dict = None
        if fr.gps is not None:
            seq = int(fr.gps.seq_number) & 0xFFFFFFFF
            gps_dict = fr.gps.as_dict() if hasattr(fr.gps, "as_dict") else dict(vars(fr.gps))

        img = None
        if need_pixels:
            img = to_gray8(fr)
            fp = fingerprint(img)
            if fp == self.last_fp:
                return None
            self.last_fp = fp
        else:
            if seq is None:
                return None
            fp = f"seq:{seq}"
            if fp == self.last_fp:
                return None
            self.last_fp = fp

        replace_i = None
        if (
            seq is not None
            and self.burst_start is not None
            and self.burst_end is not None
        ):
            expected = expected_seq_set(self.burst_start, self.burst_end) or set()
            idx_in = seq_index(seq, self.burst_end)
            if idx_in not in expected:
                return None
            cyc = seq_cycle(seq, self.burst_end)
            # Rising-edge lock: first accepted frame must be window start (min expected idx).
            # Prevents residual high abs GPS seq that only match via seq%(end+1) from stealing the burst.
            edge = min(expected) if expected else None
            if self.cycle is None:
                if edge is not None and idx_in != edge:
                    return None
                self.cycle = cyc
            elif cyc != self.cycle:
                return None
            # Absolute-seq dedup: replace older with newer (overwrite jpg + meta).
            for _i, _x in enumerate(self.frames):
                if _x.seq is not None and (int(_x.seq) & 0xFFFFFFFF) == (seq & 0xFFFFFFFF):
                    replace_i = _i
                    break
            if replace_i is not None:
                old_rec = self.frames.pop(replace_i)
                try:
                    Path(old_rec.path).unlink(missing_ok=True)
                except Exception:
                    pass
                idx = int(old_rec.save_idx)
            else:
                idx = self.n_saved + 1
        else:
            idx = self.n_saved + 1
        if seq is not None:
            name = f"{self.tag}_seq{seq:04d}.jpg"
        else:
            name = f"{self.tag}_f{idx:03d}_noseq.jpg"
        path = self.gdir / name
        if path.exists():
            stem = path.stem
            n = 2
            while True:
                alt = path.with_name(f"{stem}__r{n}{path.suffix}")
                if not alt.exists():
                    path = alt
                    break
                n += 1
        if self.save_images and img is not None:
            h, w = img.shape
            scale = 4 if max(h, w) >= 6000 else (2 if max(h, w) > 2400 else 1)
            out = img if scale == 1 else img[::scale, ::scale]
            Image.fromarray(out).save(path, quality=85)
        else:
            path = self.gdir / (name.replace(".jpg", ".meta_only.json"))
            if path.exists():
                stem = path.stem
                n = 2
                while True:
                    alt = path.with_name(f"{stem}__r{n}{path.suffix}")
                    if not alt.exists():
                        path = alt
                        break
                    n += 1
            path.write_text(
                json.dumps({"tag": self.tag, "seq": seq, "fp": fp, "w": fr.width, "h": fr.height}, ensure_ascii=False),
                encoding="utf-8",
            )
        rec = FrameRec(
            tag=self.tag,
            save_idx=idx,
            seq=seq,
            path=str(path),
            width=int(fr.width),
            height=int(fr.height),
            fp=fp,
            gps=gps_dict,
        )
        self.frames.append(rec)
        return rec


def drain(cam: SoftCamera, rounds: int = 12, timeout_s: float = 1.0) -> int:
    n = 0
    for _ in range(rounds):
        try:
            fr = cam.capture_live_frame(parse_gps=False, timeout_s=timeout_s, copy_pixels=False)
            if fr is not None:
                n += 1
        except Exception:
            pass
    return n


def pcie_clear_ddr(api: QhyApi, ctx):
    fn = getattr(api.dll, "PCIEClearDDR", None)
    if fn is None:
        return None
    fn.argtypes = [ctypes.c_void_p]
    fn.restype = ctypes.c_uint32
    return int(fn(ctx.handle)) & 0xFFFFFFFF


def pcie_setup_burst(api: QhyApi, ctx, *, slave: bool, start: int, end: int, patch: int) -> None:
    """PCIe reference flow (debug/usb_dual_burst.py setup_burst): program FPGA while IDLE, BEFORE begin_live."""
    for i, sh in enumerate((24, 16, 8, 0)):
        ctx.write_fpga(41 + i, (int(patch) >> sh) & 0xFF)
    fn = getattr(api.dll, "SetQHYCCDBurstModePatchNumber", None)
    if patch and fn is not None:
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        fn(ctx.handle, ctypes.c_uint32(int(patch)))
    write_burst_window(ctx, start, end)
    if slave:
        for reg, val in ((81, 0), (39, 2), (142, 1), (56, 1), (144, 0), (145, 0), (146, 0), (147, 0), (148, 10), (58, 0x08)):
            ctx.write_fpga(reg, val)
    else:
        for reg, val in ((142, 1), (56, 1), (58, 0), (39, 0x11)):
            ctx.write_fpga(reg, val)
    ctx.write_fpga(57, 1)
    ctx.write_fpga(35, 0)
    pcie_clear_ddr(api, ctx)
    ctx.write_fpga(63, 1)
    ctx.write_fpga(63, 0)


def pcie_restore_after_live(m_ctx, s_ctx) -> None:
    """BeginQHYCCDLive / InitChipRegs can reset 39/58; restore the GPIO sync path (reference flow)."""
    m_ctx.write_fpga(39, 0x11)
    m_ctx.write_fpga(58, 0)
    m_ctx.write_fpga(35, 0)
    s_ctx.write_fpga(81, 0)
    s_ctx.write_fpga(39, 2)
    s_ctx.write_fpga(58, 0x08)
    s_ctx.write_fpga(35, 0)


def pick_roles(names: list[str]) -> tuple[str, str]:
    """Master/slave from SYNC_ACC_MASTER/SLAVE. The same physical card shows up under different SDK ids
    depending on the DLL build (deps DLL: a7c0.../847c...; AllInOne DLL: ac14.../8765...), so resolve through aliases."""
    _alias_groups = (("a7c01148", "ac14f178"), ("847c685c", "8765fd7c"))

    def _resolve(pref: str) -> str:
        if not pref or pref in names:
            return pref
        for grp in _alias_groups:
            if any(k in pref for k in grp):
                for n in names:
                    if any(k in n for k in grp):
                        return n
        return pref

    master_pref = _resolve(os.environ.get("SYNC_ACC_MASTER", "").strip())
    slave_pref = _resolve(os.environ.get("SYNC_ACC_SLAVE", "").strip())
    if master_pref and master_pref in names and slave_pref and slave_pref in names:
        return master_pref, slave_pref
    if master_pref and master_pref in names:
        return master_pref, next(n for n in names if n != master_pref)
    return names[0], names[1]


def open_pair(api: QhyApi, ids, bin_xy, traffic: float, exp_us: float, gain: float, bits: int = 8, begin_live: bool = True, gps_after_live: bool = False, open_both_first: bool = False,
              gps_apply_after: bool = False):
    names = [clean_id(x) for x in ids]
    m_name, s_name = pick_roles(names)

    cams = {}
    bx, by = bin_xy
    # Open Master first, pause, then Slave — reduces USB/SDK contention.
    # QHY600 cold-start: applying bin>=2 as the FIRST Init/bin often yields LiveFrame
    # timeouts; warm up with 1x1 then switch to the target bin (same Ezcap session).
    # PCIe reference flow: open BOTH cards first, then configure both (opening the 2nd card after
    # configuring the 1st can disturb the 1st).
    pre_ctx = None
    if (not begin_live) or open_both_first:
        pre_ctx = {"M": open_camera_by_name(api, ids, m_name), "S": open_camera_by_name(api, ids, s_name)}
        print("open_pair: both cards opened before configure (PCIe reference order)", flush=True)
    for tag, nm in (("M", m_name), ("S", s_name)):
        if tag == "S" and pre_ctx is None:
            settle = 2.5 if (os.environ.get("SYNC_ACC_TRANSPORT", "").strip().lower() == "pcie") else 1.0
            time.sleep(settle)
            print(f"open_pair: Master live settled; opening Slave after {settle:.1f}s", flush=True)
        t_open = time.time()
        ctx = pre_ctx[tag] if pre_ctx is not None else open_camera_by_name(api, ids, nm)
        cam = SoftCamera(ctx)
        bits_i = 16 if int(bits) >= 16 else 8
        base_kw = dict(
            stream_mode=STREAM_LIVE,
            read_mode=0,
            bits=bits_i,
            exposure_us=exp_us,
            gain=(float(os.environ["SYNC_ACC_GAIN"]) if os.environ.get("SYNC_ACC_GAIN", "").strip() else gain),
            # SYNC_ACC_TRAFFIC_UNSET=1: leave usb_traffic to the camera default (capture_gui leaves the field empty).
            usb_traffic=(None if os.environ.get("SYNC_ACC_TRAFFIC_UNSET", "") == "1" else float(traffic)),
            # PCIe reference flow keeps SDK GPS off: gps_enable=True makes the SDK rewrite 39/81/58 behind our FPGA setup.
            gps_enable=(False if gps_after_live else (bool(begin_live) or os.environ.get("SYNC_ACC_PCIE_GPS", "0") == "1")),
        )
        if bx != 1 or by != 1:
            cam.apply_settings(CameraSettings(bin_x=1, bin_y=1, **base_kw))
            cam.begin_live()
            try:
                _wu = 12.0 if (os.environ.get("SYNC_ACC_TRANSPORT", "").strip().lower() == "pcie") else 6.0
                cam.capture_live_frame(parse_gps=False, timeout_s=_wu, copy_pixels=False)
            except Exception as exc:
                print(f"{tag} bin1 warmup frame warn: {exc}", flush=True)
            try:
                cam.stop_live()
            except Exception:
                pass
            print(f"{tag} bin warmup 1x1 -> {bx}x{by}", flush=True)
        geo = cam.apply_settings(CameraSettings(bin_x=bx, bin_y=by, **base_kw))
        if os.environ.get("SYNC_ACC_COOLING_OFF", "1") == "1":
            # Test requirement: camera cooling OFF (CONTROL_MANULPWM=16 -> 0); safe now that Init is done.
            try:
                cam.set_param(16, 0.0)
                print(f"{tag} cooling off: MANULPWM=0 CURPWM={cam.get_param(15)}", flush=True)
            except Exception as exc:
                print(f"{tag} cooling off WARN: {exc}", flush=True)
        ff = geo.get("full_frame") if isinstance(geo, dict) else None
        if gps_after_live and os.environ.get("SYNC_ACC_TRAFFIC_UNSET", "") == "1":
            # capture_gui: the acquisition-parameter panel reads the camera defaults FIRST, Live starts after.
            try:
                _ctl = (cam.camera_info() or {}).get("controls") or {}
                _dv = {k: (v or {}).get("value") for k, v in _ctl.items() if (v or {}).get("available")}
                print(f"{tag} camera defaults read: {_dv}", flush=True)
                if _dv.get("usb_traffic") is not None:
                    base_kw["usb_traffic"] = float(_dv["usb_traffic"])
                    cam.apply_settings(CameraSettings(bin_x=bx, bin_y=by, **base_kw))
            except Exception as exc:
                print(f"{tag} camera defaults read warn: {exc}", flush=True)
        if gps_after_live and os.environ.get("SYNC_ACC_PRE_IDLE", "0") == "1":
            # Hold IDLE before Live so the card does not free-run 16-bit frames while nobody reads them
            # (unread PCIe DMA overflows -> GetOverlappedResult 87 flood -> wedged camera).
            ctx.write_fpga(57, 1)
            ctx.write_fpga(35, 0)
        if begin_live:
            cam.begin_live()
        if gps_after_live and gps_apply_after:
            # SYNC_ACC_GPS_MODE=after: Live first, THEN SDK GPS on (NOT what capture_gui does; floods GetOverlappedResult 87) (apply_settings -> SetGPSOn on the running live).
            time.sleep(1.0)
            cam.apply_settings(CameraSettings(bin_x=bx, bin_y=by, **dict(base_kw, gps_enable=True)))
            print(f"{tag} GPS on after live (capture_gui order)", flush=True)
        cams[tag] = {"cam": cam, "ctx": ctx, "name": nm}
        dt = time.time() - t_open
        print(
            f"{tag} live bin={bx}x{by} traffic={traffic} gps=1 {nm} "
            f"roi={getattr(cam.settings, 'roi_w', None)}x{getattr(cam.settings, 'roi_h', None)} "
            f"full={ff} open_dt={dt:.2f}s",
            flush=True,
        )
    return cams


def open_pair_gui(api: QhyApi, ids, bin_xy, traffic: float, exp_us: float, gain: float, bits: int, gps_apply_after: bool):
    """capture_gui architecture: one capture SUBPROCESS per camera (settings / Live / frames, own SDK instance);
    FPGA registers are written from THIS process through its own un-Init'd session (never touches the live)."""
    from capture_proxy import CaptureWorkerPool

    names = [clean_id(x) for x in ids]
    m_name, s_name = pick_roles(names)
    bx, by = bin_xy
    bits_i = 16 if int(bits) >= 16 else 8
    g = float(os.environ["SYNC_ACC_GAIN"]) if os.environ.get("SYNC_ACC_GAIN", "").strip() else gain
    tr = None if os.environ.get("SYNC_ACC_TRAFFIC_UNSET", "") == "1" else float(traffic)

    def mk(bin_x: int, bin_y: int, gps: bool = False, slave: bool = False) -> CameraSettings:
        # capture_gui._gps_slave_for_cam: the SLAVE role always carries gps_slave=True (SDK master/slave API).
        return CameraSettings(
            stream_mode=STREAM_LIVE, read_mode=0, bits=bits_i, exposure_us=exp_us, gain=g,
            usb_traffic=tr, bin_x=bin_x, bin_y=bin_y, gps_enable=gps, gps_slave=slave,
        )

    pool = CaptureWorkerPool(CODE_ROOT, log=lambda x: print(x, flush=True))
    cams: dict = {}
    for tag, nm in (("M", m_name), ("S", s_name)):
        t_open = time.time()
        cam = pool.open(nm)
        ctx = open_camera_by_name(api, ids, nm)
        cams[tag] = {"cam": cam, "ctx": ctx, "name": nm}
        print(f"{tag} capture worker + register session opened {nm} dt={time.time() - t_open:.2f}s", flush=True)
    def cooling_off(when: str) -> None:
        # Test requirement: camera cooling OFF during the run (CONTROL_MANULPWM=16 -> 0; CURPWM=15 read back).
        if os.environ.get("SYNC_ACC_COOLING_OFF", "1") != "1":
            return
        for t in ("M", "S"):
            try:
                cams[t]["cam"].set_param(16, 0.0)
                try:
                    cur = cams[t]["cam"].get_param(15)
                except Exception:
                    cur = "?"
                print(f"{t} cooling off ({when}): MANULPWM=0 CURPWM={cur}", flush=True)
            except Exception as exc:
                print(f"{t} cooling off ({when}) WARN: {exc}", flush=True)

    for tag in ("M", "S"):
        cam = cams[tag]["cam"]
        if bx != 1 or by != 1:
            # QHY600 cold start: bin>=2 as the first Init often yields LiveFrame timeouts; warm up at 1x1.
            cam.apply_settings(mk(1, 1, False, tag == "S"))
            cam.begin_live()
            try:
                cam.capture_live_frame(parse_gps=False, timeout_s=12.0, copy_pixels=False)
            except Exception as exc:
                print(f"{tag} bin1 warmup frame warn: {exc}", flush=True)
            try:
                cam.stop_live()
            except Exception:
                pass
            print(f"{tag} bin warmup 1x1 -> {bx}x{by}", flush=True)
        cam.apply_settings(mk(bx, by, False, tag == "S"))
        print(f"{tag} settings applied bin={bx}x{by} bits={bits_i} gain={g} exp_us={exp_us} traffic={tr}", flush=True)
    cooling_off("after init")
    if os.environ.get("SYNC_ACC_PRE_IDLE", "0") == "1":
        # Hold IDLE before Live so the cards do not free-run 16-bit frames while nobody reads them.
        for tag in ("M", "S"):
            cams[tag]["ctx"].write_fpga(57, 1)
            cams[tag]["ctx"].write_fpga(35, 0)
    for tag in ("M", "S"):
        cams[tag]["cam"].begin_live()
    # capture_gui keeps a Live reader thread per camera running; PCIe stalls (and SetGPSOn gets very slow,
    # GetOverlappedResult 87 flood) when nobody consumes frames. Mimic that while the runtime controls go in.
    stop_evt = threading.Event()
    first_frame = {"M": threading.Event(), "S": threading.Event()}

    def _bg_reader(cam, ev) -> None:
        while not stop_evt.is_set():
            try:
                cam.capture_live_frame(parse_gps=False, timeout_s=0.5, copy_pixels=False)
                ev.set()
            except Exception:
                time.sleep(0.05)

    readers = [threading.Thread(target=_bg_reader, args=(cams[t]["cam"], first_frame[t]), daemon=True) for t in ("M", "S")]
    for th in readers:
        th.start()
    try:
        # After changing gain/exposure/etc. the first image can take a long time to appear (user note):
        # do not continue until BOTH cards really deliver a frame.
        _t_live = time.time()
        _tmo = float(os.environ.get("SYNC_ACC_FIRST_FRAME_S", "90"))
        for t in ("M", "S"):
            _ok = first_frame[t].wait(max(0.0, _tmo - (time.time() - _t_live)))
            print(f"{t} first frame " + (f"after {time.time() - _t_live:.1f}s" if _ok else f"NOT seen within {_tmo:.0f}s"), flush=True)
        time.sleep(1.0)
        cooling_off("after live")
        if gps_apply_after:
            for tag in ("M", "S"):
                try:
                    cams[tag]["cam"].apply_runtime_controls(mk(bx, by, True, tag == "S"))
                    print(f"{tag} GPS on after Live (apply_runtime_controls)", flush=True)
                except Exception as exc:
                    print(f"{tag} GPS on warn (continuing): {exc}", flush=True)
            time.sleep(1.0)
    finally:
        stop_evt.set()
        for th in readers:
            th.join(timeout=15.0)
    return cams, pool


def close_pair(api: QhyApi, cams: dict, modes: dict, mode_set: dict | None = None) -> None:
    exit_key = (mode_set or {}).get("exit", MODE_EXIT)
    for tag in ("M", "S"):
        try:
            write_mode(cams[tag]["ctx"], modes, exit_key)
        except Exception as exc:
            print(f"{tag} exit-mode warn: {exc}", flush=True)
    for tag in ("M", "S"):
        try:
            cams[tag]["cam"].stop_live()
        except Exception:
            pass
        try:
            close_camera(api, cams[tag]["ctx"])
        except Exception:
            pass


def run_burst(
    *,
    bin_xy: tuple[int, int],
    n_frames: int,
    tag: str,
    traffic: float,
    burst_start: int,
    burst_end: int,
    exp_us: float = 10_000.0,
    gain: float = 50.0,
    save_images: bool = True,
    drain_rounds: int = 16,
    transport: str = "usb",
    bits: int = 8,
) -> dict:
    root = data_root()
    gdir = root / "groups" / tag
    gdir.mkdir(parents=True, exist_ok=True)
    # Keep all prior artifacts; tags are unique per trial stamp.

    mode_set = resolve_modes(transport)
    modes = load_modes()
    for k in ("master", "slave", "release", "exit", "burst_patch"):
        key = mode_set[k]
        if key not in modes:
            raise KeyError(f"soft mode missing for transport={mode_set['transport']}: {key}")
    print(
        f"TRANSPORT={mode_set['transport']} "
        f"MASTER={mode_set['master']} SLAVE={mode_set['slave']} "
        f"RELEASE={mode_set['release']} EXIT={mode_set['exit']} "
        f"PATCH={mode_set['burst_patch']}({mode_set['patch_value']})",
        flush=True,
    )
    gui_mode = (
        mode_set["transport"] == "pcie"
        and os.environ.get("SYNC_ACC_PCIE_REF", "0") != "1"
        and os.environ.get("SYNC_ACC_PCIE_GUI", "1") != "0"
    )
    if gui_mode and not os.environ.get("QHYCCD_DLL", "").strip():
        # same DLL priority as capture_gui / capture_worker: remoteupdate_deps (side-loaded qhyccd.ini) > AllInOne
        _deps = CODE_ROOT / "remoteupdate_deps" / "qhyccd.dll"
        _all = Path("C:/Program Files/QHYCCD/AllInOne/sdk/x64/qhyccd.dll")
        os.environ["QHYCCD_DLL"] = str(_deps if (_deps.exists() and os.environ.get("SYNC_ACC_DLL", "deps") == "deps") else _all)
        print(f"QHYCCD_DLL={os.environ['QHYCCD_DLL']}", flush=True)
    api = QhyApi(HOST)
    ids = scan_cameras(api)
    # Back-to-back PCIe scans are flaky (each may see only one card): retry with a pause before giving up.
    for _try in range(6):
        if len(ids) >= 2:
            break
        print(f"scan saw {len(ids)} camera(s); retry {_try + 1}/6 in 20s", flush=True)
        time.sleep(20.0)
        ids = scan_cameras(api)
    if len(ids) < 2:
        raise RuntimeError(f"need 2 cameras, got {len(ids)}")

    pcie_ref = mode_set["transport"] == "pcie" and os.environ.get("SYNC_ACC_PCIE_REF", "0") == "1"
    # SYNC_ACC_GPS_MODE (pcie): off = SDK GPS stays off like capture_gui (frame seq is only parsed on the host);
    # after = SDK GPS applied on the running live; before = SDK GPS on at configure time (legacy).
    _gps_mode = os.environ.get("SYNC_ACC_GPS_MODE", "after").strip().lower()
    gps_after_live = mode_set["transport"] == "pcie" and not pcie_ref and _gps_mode in ("off", "after")
    pool = None
    if gui_mode:
        cams, pool = open_pair_gui(api, ids, bin_xy, traffic, exp_us, gain, bits, gps_apply_after=(_gps_mode == "after"))
    else:
        cams = open_pair(api, ids, bin_xy, traffic, exp_us, gain, bits=bits, begin_live=not pcie_ref, gps_after_live=gps_after_live,
                         open_both_first=(mode_set["transport"] == "pcie"), gps_apply_after=(_gps_mode == "after"))
    post_open = (0.0 if (pcie_ref or gui_mode) else 3.0) if mode_set["transport"] == "pcie" else 1.2
    print(f"post-open settle {post_open:.1f}s transport={mode_set['transport']}", flush=True)
    time.sleep(post_open)
    result = {
        "tag": tag,
        "bin": list(bin_xy),
        "traffic": traffic,
        "bits": int(16 if int(bits) >= 16 else 8),
        "burst_start": burst_start,
        "burst_end": burst_end,
        "expected": sorted(expected_seq_set(burst_start, burst_end) or []),
        "n_frames_target": n_frames,
        "save_images": save_images,
        "transport": mode_set["transport"],
        "mode_keys": {
            "master": mode_set["master"],
            "slave": mode_set["slave"],
            "release": mode_set["release"],
            "exit": mode_set["exit"],
            "burst_patch": mode_set["burst_patch"],
            "patch_value": mode_set["patch_value"],
        },
        "t0": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cameras": {"M": cams["M"]["name"], "S": cams["S"]["name"]},
        "ok": False,
    }
    try:
        M = Collector(
            cams["M"]["cam"], "M", gdir, save_images,
            burst_start=burst_start, burst_end=burst_end, lock_cycle=False,
        )
        S = Collector(
            cams["S"]["cam"], "S", gdir, save_images,
            burst_start=burst_start, burst_end=burst_end, lock_cycle=False,
        )

        if pcie_ref:
            # PCIe: follow the accepted reference flow (debug/usb_dual_burst.py --pcie --hw-trig):
            # program FPGA while IDLE and BEFORE begin_live (+PCIEClearDDR, reg63), begin_live,
            # restore 39/58/35, release on master only, then round-robin single-thread grab.
            patch = int(mode_set["patch_value"])
            for _t, _slave in (("M", False), ("S", True)):
                pcie_setup_burst(api, cams[_t]["ctx"], slave=_slave, start=burst_start, end=burst_end, patch=patch)
            print(f"PCIe pre-live setup done window={burst_start}..{burst_end} patch={patch}", flush=True)
            cams["M"]["cam"].begin_live()
            cams["S"]["cam"].begin_live()
            time.sleep(0.15)
            pcie_restore_after_live(cams["M"]["ctx"], cams["S"]["ctx"])
            time.sleep(0.1)
            if os.environ.get("SYNC_ACC_PCIE_POSTLIVE", "0") == "1":
                # SDK GPS-on rewrites registers inside begin_live: re-apply the whole sync setup (slave first, then master).
                for _t, _slave in (("S", True), ("M", False)):
                    pcie_setup_burst(api, cams[_t]["ctx"], slave=_slave, start=burst_start, end=burst_end, patch=patch)
                pcie_restore_after_live(cams["M"]["ctx"], cams["S"]["ctx"])
                time.sleep(0.2)
                print("PCIe post-live re-setup applied", flush=True)
            M.last_fp = None
            S.last_fp = None
            M.frames.clear()
            S.frames.clear()
            M.cycle = None
            S.cycle = None
            t_rel = time.time()
            cams["M"]["ctx"].write_fpga(35, 1)
            print(
                f"RELEASE at {t_rel:.3f} window={burst_start}..{burst_end} "
                f"transport=pcie (master 35=1 only)",
                flush=True,
            )
            time.sleep(0.4)
        else:
            pre_rounds = max(int(drain_rounds), 16)
            print(f"DRAIN pre-sync rounds={pre_rounds} ...", flush=True)
            dm = drain(M.cam, pre_rounds, timeout_s=0.5)
            ds = drain(S.cam, pre_rounds, timeout_s=0.5)
            print(f"  drained M={dm} S={ds}", flush=True)

            # Hold config (Master one-key ends 35=0 / hold IDLE) + Slave; no full reconfig after RELEASE.
            write_mode(cams["M"]["ctx"], modes, mode_set["master"])
            write_mode(cams["M"]["ctx"], modes, mode_set["burst_patch"])  # reinforce transport patch
            write_burst_window(cams["M"]["ctx"], burst_start, burst_end)
            time.sleep(0.4)
            write_mode(cams["S"]["ctx"], modes, mode_set["slave"])
            write_mode(cams["S"]["ctx"], modes, mode_set["burst_patch"])
            write_burst_window(cams["S"]["ctx"], burst_start, burst_end)
            time.sleep(0.8)

            # Second drain AFTER mode/window settle so LIVE residuals from prior groups cannot enter collect.
            post_rounds = max(int(drain_rounds), 12)
            print(f"DRAIN post-mode pre-release rounds={post_rounds} ...", flush=True)
            dm2 = drain(M.cam, post_rounds, timeout_s=0.5)
            ds2 = drain(S.cam, post_rounds, timeout_s=0.5)
            print(f"  drained post-mode M={dm2} S={ds2}", flush=True)

            # Session warm-up (notes 96726048): a fresh Live can sit in a "bad phase" (0 frames) while a
            # re-armed window in the SAME Live is full. Do throw-away rounds [hold IDLE -> reg63 re-arm ->
            # master release] until both cards deliver, then re-arm once more for the formal release.
            _warm_n = int(os.environ.get("SYNC_ACC_WARMUP", "3")) if gui_mode else 0
            if _warm_n > 0:
                def _rearm() -> None:
                    for _t in ("M", "S"):
                        cams[_t]["ctx"].write_fpga(35, 0)
                    for _t in ("M", "S"):
                        cams[_t]["ctx"].write_fpga(63, 1)
                        cams[_t]["ctx"].write_fpga(63, 0)
                    time.sleep(0.5)
                    drain(M.cam, 4, timeout_s=0.5)
                    drain(S.cam, 4, timeout_s=0.5)

                # Warm up on a SHORT window: a long formal window (e.g. 100 frames) would still be streaming when we
                # re-arm and the leftover frames jam the next release.
                _short = burst_end > 14
                if _short:
                    for _t in ("M", "S"):
                        write_burst_window(cams[_t]["ctx"], 1, 12)
                for _w in range(_warm_n):
                    _rearm()
                    cams["M"]["ctx"].write_fpga(35, 0)
                    cams["M"]["ctx"].write_fpga(35, 1)
                    time.sleep(0.4)
                    _got: dict = {"M": [], "S": []}
                    _t_end = time.time() + 12.0
                    while time.time() < _t_end:
                        for _t, _c in (("M", M), ("S", S)):
                            try:
                                _fr = _c.cam.capture_live_frame(parse_gps=True, timeout_s=1.5, copy_pixels=False)
                                _g = getattr(_fr, "gps", None)
                                _got[_t].append(int(getattr(_g, "seq_number", -1)) & 0xFFFFFFFF if _g is not None else -1)
                            except Exception:
                                pass
                    print(f"WARMUP {_w}: M={len(_got['M'])} {_got['M'][:8]} S={len(_got['S'])} {_got['S'][:8]}", flush=True)
                    if _got["M"] and _got["S"]:
                        break
                if _short:
                    for _t in ("M", "S"):
                        write_burst_window(cams[_t]["ctx"], burst_start, burst_end)
                _rearm()

            # Reset collectors after drain/config so only post-release frames count
            M.last_fp = None
            S.last_fp = None
            M.frames.clear()
            S.frames.clear()
            M.cycle = None
            S.cycle = None

            t_rel = time.time()
            write_mode(cams["M"]["ctx"], modes, mode_set["release"])
            print(
                f"RELEASE at {t_rel:.3f} window={burst_start}..{burst_end} "
                f"transport={mode_set['transport']}",
                flush=True,
            )
            time.sleep(0.05)

        expected_n = len(expected_seq_set(burst_start, burst_end) or []) or n_frames
        target = max(n_frames, expected_n)
        # Keep collecting past n_frames until longest-consecutive run is complete.
        soft_cap = max(target * 4, target + 12)
        max_iters = soft_cap * 12 + 80
        t0 = time.time()
        consec_to = 0

        def _grab_one(collector: Collector, grab_to: float):
            try:
                collector.grab_new(timeout_s=grab_to)
                return None
            except Exception as exc:
                return exc

        for i in range(max_iters):
            info_m_live = analyze_seqs(M.seqs, burst_start, burst_end)
            info_s_live = analyze_seqs(S.seqs, burst_start, burst_end)
            m_done = info_m_live["complete"]
            s_done = info_s_live["complete"]
            if m_done and s_done:
                break
            if M.n_saved >= soft_cap and S.n_saved >= soft_cap:
                print(
                    f"SOFT-CAP collect M={M.n_saved} S={S.n_saved} "
                    f"runM={info_m_live.get('run_len')} runS={info_s_live.get('run_len')}",
                    flush=True,
                )
                break
            # Higher traffic => longer gaps; PCIe needs more headroom (tuning stutter).
            if mode_set["transport"] == "pcie":
                grab_to = max(12.0, min(30.0, 10.0 + float(traffic) / 5.0))
                abort_n = 16 if traffic < 25 else 20
            else:
                grab_to = max(8.0, min(20.0, 6.0 + float(traffic) / 5.0))
                abort_n = 10 if traffic < 25 else 12
            _abort_env = os.environ.get("SYNC_ACC_ABORT_N", "").strip()
            if _abort_env:
                abort_n = int(_abort_env)
            to_hit = False
            jobs = []
            if not m_done:
                jobs.append(("M", M))
            if not s_done:
                jobs.append(("S", S))
            if jobs:
                if mode_set["transport"] == "pcie":
                    # one thread, round-robin: avoids concurrent qhyccd.dll calls on two PCIe cards
                    results = [(tag, _grab_one(col, min(grab_to, 8.0))) for tag, col in jobs]
                else:
                    with ThreadPoolExecutor(max_workers=len(jobs)) as ex_pool:
                        futs = {ex_pool.submit(_grab_one, col, grab_to): tag for tag, col in jobs}
                        results = [(futs[fut], fut.result()) for fut in as_completed(futs)]
                for tag, exc in results:
                    if exc is not None:
                        print(f"{tag} grab err {exc}", flush=True)
                        if "timeout" in str(exc).lower() or "LiveFrame" in str(exc):
                            to_hit = True
            if to_hit:
                consec_to += 1
            else:
                consec_to = 0
            if consec_to >= abort_n:
                print(f"ABORT collect: {consec_to} consecutive LiveFrame timeouts (grab_to={grab_to:.1f}s)", flush=True)
                break
            if i % 8 == 0:
                miss_m = analyze_seqs(M.seqs, burst_start, burst_end)["missing"]
                miss_s = analyze_seqs(S.seqs, burst_start, burst_end)["missing"]
                print(
                    f"  collect M={M.n_saved}/{n_frames} miss={miss_m} "
                    f"S={S.n_saved}/{n_frames} miss={miss_s} t={time.time()-t0:.1f}s",
                    flush=True,
                )
        print(f"COLLECT M={M.n_saved} S={S.n_saved} t={time.time()-t0:.1f}s", flush=True)

        info_m = analyze_seqs(M.seqs, burst_start, burst_end)
        info_s = analyze_seqs(S.seqs, burst_start, burst_end)
        miss_m = list(info_m["missing"])
        miss_s = list(info_s["missing"])
        expected_n = len(expected_seq_set(burst_start, burst_end) or [])
        result.update(
            {
                "M_count": M.n_saved,
                "S_count": S.n_saved,
                "M_seqs": M.seqs,
                "S_seqs": S.seqs,
                "M_cycle": info_m["cycle"],
                "S_cycle": info_s["cycle"],
                "M_observed_idx": info_m["observed_idx"],
                "S_observed_idx": info_s["observed_idx"],
                "M_missing": miss_m,
                "S_missing": miss_s,
                "M_drop": bool(miss_m),
                "S_drop": bool(miss_s),
                "any_drop": bool(miss_m or miss_s),
                "elapsed_s": round(time.time() - t0, 3),
                "frames": {
                    "M": [asdict(x) for x in M.frames],
                    "S": [asdict(x) for x in S.frames],
                },
            }
        )
        # Prefer a complete expected band in one cycle; n_frames is a soft target.
        complete = info_m["complete"] and info_s["complete"]
        enough = M.n_saved >= min(n_frames, expected_n or n_frames) and S.n_saved >= min(
            n_frames, expected_n or n_frames
        )
        have_seq = bool(M.seqs) and bool(S.seqs)
        result["have_seq"] = have_seq
        result["ok"] = bool(have_seq and complete and enough and not result["any_drop"])

        meta_path = gdir / "meta.jsonl"
        with meta_path.open("w", encoding="utf-8") as f:
            for side in ("M", "S"):
                for rec in (M.frames if side == "M" else S.frames):
                    f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        result["meta_jsonl"] = str(meta_path)
        result["group_dir"] = str(gdir)
    finally:
        close_pair(api, cams, modes, mode_set)
        if pool is not None:
            try:
                pool.close_all()
            except Exception as exc:
                print(f"pool close warn: {exc}", flush=True)
            # Leaving SDK handles open when the process exits wedges the PCIe cards (only a power cycle recovers):
            # release this process' SDK resource too, bounded like the reference script.
            def _rel() -> None:
                try:
                    api.dll.ReleaseQHYCCDResource()
                except Exception:
                    pass

            _t = threading.Thread(target=_rel, daemon=True)
            _t.start()
            _t.join(8.0)
            print("SDK resource released" if not _t.is_alive() else "SDK release hung (8s)", flush=True)
        time.sleep(1.0)
    return result


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def traffic_gate(
    *,
    bin_xy: tuple[int, int] = (1, 1),
    start_traffic: float = 0.0,
    step: float = 5.0,
    max_traffic: float = 60.0,
    n_frames: int = 9,
    burst_start: int = 1,
    burst_end: int = 11,
    need_clean_rounds: int = 2,
    save_images: bool = False,
    exp_us: float = 10_000.0,
    transport: str = "usb",
) -> dict:
    root = data_root()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bin_tag = f"{bin_xy[0]}x{bin_xy[1]}"
    trials = []
    traffic = float(start_traffic)
    clean_streak = 0
    locked = None

    print(
        f"TRAFFIC GATE bin={bin_tag} start={traffic} step={step} "
        f"need_clean={need_clean_rounds} window={burst_start}..{burst_end} expect={sorted(expected_seq_set(burst_start, burst_end) or [])}",
        flush=True,
    )

    while traffic <= max_traffic + 1e-9:
        tag = f"gate_{bin_tag}_t{int(traffic):03d}_r{len(trials)+1}_{stamp}"
        print(f"\n=== GATE trial traffic={traffic} tag={tag} ===", flush=True)
        try:
            r = run_burst(
                bin_xy=bin_xy,
                n_frames=n_frames,
                tag=tag,
                traffic=traffic,
                burst_start=burst_start,
                burst_end=burst_end,
                exp_us=exp_us,
                save_images=save_images,
                transport=transport,
            )
        except Exception as exc:
            r = {
                "tag": tag,
                "traffic": traffic,
                "ok": False,
                "error": str(exc),
                "any_drop": True,
            }
            print(f"GATE ERROR: {exc}", flush=True)
        trials.append(r)
        gate_path = root / "status" / f"traffic_gate_{bin_tag}_t{int(traffic):03d}_{stamp}_n{len(trials)}.json"
        save_json(gate_path, r)
        print(
            f"  -> ok={r.get('ok')} drop={r.get('any_drop')} "
            f"M_seqs={r.get('M_seqs')} S_seqs={r.get('S_seqs')} "
            f"missM={r.get('M_missing')} missS={r.get('S_missing')} file={gate_path.name}",
            flush=True,
        )

        if r.get("ok"):
            clean_streak += 1
        else:
            clean_streak = 0

        if clean_streak >= need_clean_rounds:
            locked = traffic
            break

        if r.get("ok"):
            # same traffic again for streak
            continue
        traffic = round(traffic + step, 3)
        if traffic > max_traffic and clean_streak < need_clean_rounds:
            break

    lock_doc = {
        "bin": list(bin_xy),
        "locked_traffic": locked,
        "burst_start": burst_start,
        "burst_end": burst_end,
        "expected": sorted(expected_seq_set(burst_start, burst_end) or []),
        "need_clean_rounds": need_clean_rounds,
        "trials": [
            {
                "traffic": t.get("traffic"),
                "ok": t.get("ok"),
                "any_drop": t.get("any_drop"),
                "M_missing": t.get("M_missing"),
                "S_missing": t.get("S_missing"),
                "M_seqs": t.get("M_seqs"),
                "S_seqs": t.get("S_seqs"),
                "tag": t.get("tag"),
                "error": t.get("error"),
            }
            for t in trials
        ],
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ocr_status": "not_run",
        "blocker": None if locked is not None else "traffic_not_locked",
    }
    lock_name = f"traffic_lock_{bin_tag}.json"
    lock_path = root / "status" / lock_name
    save_json(lock_path, lock_doc)
    # convenience alias for full-frame
    if bin_xy == (1, 1):
        save_json(root / "status" / "traffic_lock.json", lock_doc)
    print(f"LOCK file {lock_path} locked_traffic={locked}", flush=True)
    return lock_doc


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Sync accuracy runner (external DATA_ROOT)")
    p.add_argument("--gate", action="store_true", help="Phase A traffic drop gate")
    p.add_argument("--bin", type=int, default=1, help="binning NxN")
    p.add_argument("--n-frames", type=int, default=9, help="unique frames to collect per camera")
    p.add_argument("--tag", type=str, default="", help="group tag under groups/")
    p.add_argument("--traffic", type=float, default=0.0)
    p.add_argument("--start-traffic", type=float, default=0.0)
    p.add_argument("--traffic-step", type=float, default=5.0)
    p.add_argument("--max-traffic", type=float, default=60.0)
    p.add_argument("--burst-start", type=int, default=1)
    p.add_argument("--burst-end", type=int, default=11)
    p.add_argument("--need-clean", type=int, default=2)
    p.add_argument("--exp-us", type=float, default=10000.0)
    p.add_argument("--save-images", action="store_true", help="save JPG (default on for capture, off for gate)")
    p.add_argument("--no-save-images", action="store_true")
    p.add_argument(
        "--transport",
        choices=("usb", "pcie"),
        default="usb",
        help="FPGA soft-mode set: usb (patch 32001) or pcie (patch 8192)",
    )
    p.add_argument(
        "--bits",
        type=int,
        choices=(8, 16),
        default=8,
        help="camera bit depth for SoftCamera/CameraSettings (8 or 16)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = data_root()
    print(f"DATA_ROOT={root}", flush=True)
    print(f"CODE_ROOT={CODE_ROOT}", flush=True)
    print(f"TRANSPORT={args.transport}", flush=True)
    bx = int(args.bin)
    if args.gate:
        # Climb uses meta_only by default (header GPS); pass --save-images when OCR needed.
        save_imgs = bool(args.save_images) and not args.no_save_images
        doc = traffic_gate(
            bin_xy=(bx, bx),
            start_traffic=args.start_traffic,
            step=args.traffic_step,
            max_traffic=args.max_traffic,
            n_frames=args.n_frames,
            burst_start=args.burst_start,
            burst_end=args.burst_end,
            need_clean_rounds=args.need_clean,
            save_images=save_imgs,
            exp_us=args.exp_us,
            transport=args.transport,
        )
        print(json.dumps({"locked_traffic": doc.get("locked_traffic"), "blocker": doc.get("blocker")}, ensure_ascii=False), flush=True)
        return 0 if doc.get("locked_traffic") is not None else 2

    tag = args.tag or f"cap_b{bx}_t{int(args.traffic):03d}_n{args.n_frames}"
    save_imgs = not args.no_save_images
    print(f"TRANSPORT_ARG={args.transport}", flush=True)
    print(f"BITS={args.bits}", flush=True)
    r = run_burst(
        bin_xy=(bx, bx),
        n_frames=args.n_frames,
        tag=tag,
        traffic=args.traffic,
        burst_start=args.burst_start,
        burst_end=args.burst_end,
        exp_us=args.exp_us,
        save_images=save_imgs,
        transport=args.transport,
        bits=int(args.bits),
    )
    out = root / "status" / f"capture_{tag}.json"
    save_json(out, r)
    print(json.dumps({k: r.get(k) for k in ("ok", "any_drop", "M_seqs", "S_seqs", "M_missing", "S_missing", "group_dir")}, ensure_ascii=False), flush=True)
    return 0 if r.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
