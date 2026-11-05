# -*- coding: utf-8 -*-
"""GUI 侧的采图代理：把采图子进程包装成一个像 `SoftCamera` 的对象。

调用方（GUI 的采图路径）不需要知道相机在另一个进程里，方法名与
`qhyccd_highlevel.SoftCamera` 保持一致。寄存器路径仍走 GUI 主进程，
两边各有各的 SDK 资源，因此 `InitQHYCCD` 不会再冲掉同步寄存器。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from capture_ipc import RemoteFrame, ShmChannel, decode, encode, unpack_gps  # noqa: E402

WORKER = HERE / "capture_worker.py"


class CaptureWorkerError(RuntimeError):
    pass


class CaptureWorker:
    """管理采图子进程：起停、请求应答、日志转发。"""

    def __init__(self, project_root: Path, *, log=None, start_timeout_s: float = 60.0):
        self.project_root = Path(project_root)
        self._log = log or (lambda s: None)
        self._start_timeout_s = float(start_timeout_s)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._waiters: dict[int, tuple[threading.Event, dict]] = {}
        self._ready = threading.Event()
        self._reader: threading.Thread | None = None
        self._prev_shm = ShmChannel("preview", owner=False)
        self._data_shm = ShmChannel("data", owner=False)

    # ---------- 进程 ----------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.alive:
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # SDK Init 读 getcwd()/qhyccd.ini：子进程 cwd 对齐 DLL 目录（旁路 ini），
        # 避免 GUI 从其它目录启动时采图侧 debugOutPut 关死、无 QHYCCDGPS。
        dll = Path(env.get("QHYCCD_DLL", "") or "")
        worker_cwd = str(dll.parent) if dll.is_file() else str(self.project_root)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [sys.executable, "-u", str(WORKER), "--project-root", str(self.project_root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=worker_cwd,
            creationflags=creationflags,
        )
        self._log(f"[capture] worker cwd={worker_cwd} (qhyccd.ini for GPS debug)")
        self._ready.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if not self._ready.wait(self._start_timeout_s):
            self.stop()
            raise CaptureWorkerError("采图子进程启动超时")
        self._log("[capture] 采图子进程已就绪（独立 SDK 资源）")

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    self.call("shutdown", timeout_s=8.0)
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        finally:
            self._proc = None
            self._ready.clear()
            # 释放所有阻塞中的调用，避免 GUI 线程卡死。
            with self._lock:
                waiters = list(self._waiters.items())
                self._waiters.clear()
            for _rid, (ev, box) in waiters:
                box["ok"] = False
                box["error"] = "采图子进程已退出"
                ev.set()
            self._prev_shm.close()
            self._data_shm.close()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            try:
                msg = decode(line)
            except Exception:
                msg = None
            if msg is None:
                if line:
                    self._log(line)
                continue
            if msg.get("event") == "ready":
                self._ready.set()
                continue
            rid = msg.get("id")
            with self._lock:
                entry = self._waiters.pop(rid, None)
            if entry is None:
                continue
            ev, box = entry
            box.update(msg)
            ev.set()
        # 子进程结束：叫醒所有等待者
        with self._lock:
            waiters = list(self._waiters.items())
            self._waiters.clear()
        for _rid, (ev, box) in waiters:
            box["ok"] = False
            box["error"] = "采图子进程已退出"
            ev.set()
        self._ready.clear()

    # ---------- 请求 ----------

    def call(self, op: str, args: dict | None = None, *, timeout_s: float = 120.0) -> dict:
        if not self.alive:
            raise CaptureWorkerError("采图子进程未运行")
        ev = threading.Event()
        box: dict = {}
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            self._waiters[rid] = (ev, box)
        payload = encode({"id": rid, "op": op, "args": args or {}})
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            with self._lock:
                self._waiters.pop(rid, None)
            raise CaptureWorkerError(f"发送失败: {exc}") from exc
        if not ev.wait(timeout_s):
            with self._lock:
                self._waiters.pop(rid, None)
            raise CaptureWorkerError(f"操作超时: {op}")
        if not box.get("ok"):
            raise CaptureWorkerError(box.get("error") or f"{op} 失败")
        return box.get("result") or {}

    # ---------- 帧还原 ----------

    def build_frame(self, payload: dict) -> RemoteFrame:
        fr = RemoteFrame(
            width=int(payload.get("width", 0)),
            height=int(payload.get("height", 0)),
            bpp=int(payload.get("bpp", 0)),
            channels=int(payload.get("channels", 1)),
            preview_w=int(payload.get("preview_w", 0)),
            preview_h=int(payload.get("preview_h", 0)),
            gps=unpack_gps(payload.get("gps")),
        )
        if payload.get("preview_shm"):
            fr.preview_gray = self._prev_shm.read(payload["preview_shm"], int(payload.get("preview_nbytes", 0)))
        if payload.get("data_shm"):
            fr.data = self._data_shm.read(payload["data_shm"], int(payload.get("data_nbytes", 0)))
        elif payload.get("head"):
            fr.data = bytes.fromhex(payload["head"])
        return fr


class _RemoteCtx:
    def __init__(self, cam_id: str):
        self.cam_id = cam_id
        self.handle = None


class RemoteCamera:
    """`SoftCamera` 的跨进程替身，只覆盖采图路径用到的接口。

    刻意不提供 `write_fpga` / `read_fpga`：寄存器一律走 GUI 主进程，
    免得又出现两条路径抢同一台相机模式的老问题。
    """

    def __init__(self, worker: CaptureWorker, cam_id: str):
        self._w = worker
        self.ctx = _RemoteCtx(cam_id)
        self._live = False
        self.settings = None
        self._settings_cls = None

    # -- 会话 --

    def _mk_settings(self, d: dict | None):
        if d is None:
            return None
        if self._settings_cls is None:
            from qhyccd_highlevel import CameraSettings
            self._settings_cls = CameraSettings
        import dataclasses
        fields = {f.name for f in dataclasses.fields(self._settings_cls)}
        return self._settings_cls(**{k: v for k, v in d.items() if k in fields})

    def _prepare_session(self, read_mode: int, stream_mode: int) -> None:
        self._w.call("prepare_session", {"read_mode": read_mode, "stream_mode": stream_mode})
        self._live = False

    def refresh_geometry(self, *, read_mode: int, bin_x: int = 1, bin_y: int = 1) -> dict:
        return self._w.call(
            "refresh_geometry", {"read_mode": read_mode, "bin_x": bin_x, "bin_y": bin_y}
        ).get("geo", {})

    def camera_info(self) -> dict:
        return self._w.call("camera_info").get("info", {})

    def list_read_modes(self):
        return [tuple(x) for x in self._w.call("list_read_modes").get("modes", [])]

    def apply_settings(self, settings) -> dict:
        res = self._w.call("apply_settings", {"settings": settings.as_dict()}, timeout_s=180.0)
        self.settings = settings
        self._live = False
        return res.get("geo", {})

    # SoftCamera.apply_runtime_controls 只更新这些字段；代理侧保持一致，避免覆盖 read_mode/ROI/bin。
    _RUNTIME_ATTRS = (
        "bits", "exposure_us", "gain", "offset", "usb_traffic", "speed",
        "ddr_enable", "gps_enable", "gps_slave", "gps_led_cal",
        "gps_pos_a", "gps_pos_b", "gps_pos_width",
    )

    def apply_runtime_controls(self, settings):
        # SetGPSOn 偶发很慢；缩短超时避免 Live 线程干等 120s 表现为整机卡死
        applied = self._w.call(
            "apply_runtime_controls",
            {"settings": settings.as_dict()},
            timeout_s=20.0,
        ).get("applied", [])
        if self.settings is None:
            self.settings = self._mk_settings(settings.as_dict())
        else:
            for attr in self._RUNTIME_ATTRS:
                setattr(self.settings, attr, getattr(settings, attr))
        return applied

    def apply_gps_led_positions(self, *, pos_a=0, pos_b=0, width=40, slave=False, leave_led_cal_on=False):
        return self._w.call("apply_gps_led_positions", {
            "pos_a": pos_a, "pos_b": pos_b, "width": width,
            "slave": slave, "leave_led_cal_on": leave_led_cal_on,
        })

    # -- 出图 --

    def begin_live(self) -> None:
        self._w.call("begin_live", timeout_s=180.0)
        self._live = True

    def stop_live(self) -> None:
        try:
            self._w.call("stop_live", timeout_s=30.0)
        finally:
            self._live = False

    def capture_live_frame(self, *, parse_gps=False, timeout_s=5.0,
                           copy_pixels=None, preview_max_dim=None, want_data=False):
        res = self._w.call("capture_live_frame", {
            "parse_gps": parse_gps, "timeout_s": timeout_s,
            "copy_pixels": copy_pixels, "preview_max_dim": preview_max_dim,
            "want_data": want_data,
        }, timeout_s=max(30.0, timeout_s + 20.0))
        return self._w.build_frame(res["frame"])

    # -- GPS / 低层会话（GUI 的 GPS 功能直接调这些 SoftCamera 方法） --

    def _call(self, name: str, *a, **kw):
        return self._w.call(
            "cam_call", {"name": name, "args": list(a), "kwargs": kw}, timeout_s=180.0
        ).get("result")

    @property
    def _inited(self) -> bool:
        try:
            return bool(self._w.call("cam_attr", {"name": "_inited"}).get("value"))
        except Exception:
            return False

    def init_camera(self):
        return self._call("init_camera")

    def set_stream_mode(self, mode):
        return self._call("set_stream_mode", mode)

    def set_read_mode(self, mode):
        return self._call("set_read_mode", mode)

    def set_gps_enable(self, on):
        return self._call("set_gps_enable", on)

    def try_set_gps_master_slave(self, slave):
        return self._call("try_set_gps_master_slave", slave)

    def set_gps_slave_target_utc(self, *a, **kw):
        return self._call("set_gps_slave_target_utc", *a, **kw)

    def get_param(self, control_id):
        return self._call("get_param", control_id)

    def get_param_range(self, control_id):
        return self._call("get_param_range", control_id)

    def set_param(self, control_id, value):
        """运行中改参数（如 usb_traffic），不重新 Init、不打断 live。"""
        return self._call("set_param", control_id, value)

    def capture_single(self, *, parse_gps=False, timeout_s=30.0):
        res = self._w.call("capture_single", {"parse_gps": parse_gps, "timeout_s": timeout_s},
                           timeout_s=max(60.0, timeout_s + 30.0))
        return self._w.build_frame(res["frame"])


def open_remote_camera(worker: CaptureWorker, camera_name: str | None) -> RemoteCamera:
    worker.start()
    res = worker.call("open", {"camera_name": camera_name}, timeout_s=180.0)
    return RemoteCamera(worker, res.get("cam_id") or (camera_name or ""))


def scan_remote(worker: CaptureWorker) -> list[str]:
    worker.start()
    return list(worker.call("scan", timeout_s=180.0).get("names", []))


class CaptureWorkerPool:
    """多相机同时采图：一台相机一个子进程。

    为什么一台一个进程：同进程内 SDK 对一台相机只有一个对象，`InitQHYCCD`
    还会改相机模式，多台挤在一个进程里会互相踩；而且一台楔死会带塌全部。
    分进程后一台出问题不影响其它台。

    **启动必须串行** —— 实测两个进程同时 `InitQHYCCDResource` 会双双失败并
    打出 `GetOverlappedResult: 87`。因此 `_start_lock` 把「起进程 + 首次
    scan/open」整段串起来，之后各进程取帧互不干扰。
    """

    def __init__(self, project_root: Path, *, log=None):
        self.project_root = Path(project_root)
        self._log = log or (lambda s: None)
        self._workers: dict[str, CaptureWorker] = {}
        self._cams: dict[str, RemoteCamera] = {}
        self._start_lock = threading.Lock()
        self._map_lock = threading.Lock()

    def names(self) -> list[str]:
        """扫描一次相机列表（借用/复用任意一个 worker，串行）。"""
        with self._start_lock:
            if self._workers:
                w = next(iter(self._workers.values()))
            else:
                w = CaptureWorker(self.project_root, log=self._log)
                w.start()
                self._workers["__probe__"] = w
            return list(w.call("scan", timeout_s=180.0).get("names", []))

    def _stop_worker_quiet(self, w: CaptureWorker | None, camera_name: str = "") -> None:
        if w is None:
            return
        try:
            w.stop()
        except Exception as exc:
            self._log(f"[pool] 关闭 {camera_name or 'worker'} 失败: {exc}")

    def _take_dead_locked(self, camera_name: str) -> CaptureWorker | None:
        """若缓存项的 worker 已死，从 map 摘掉并返回待 stop 的 worker（调用方已持 _map_lock）。"""
        cam = self._cams.get(camera_name)
        if cam is None:
            return None
        w = self._workers.get(camera_name)
        if w is not None and w.alive:
            return None
        self._cams.pop(camera_name, None)
        return self._workers.pop(camera_name, None)

    def open(self, camera_name: str) -> RemoteCamera:
        dead = None
        with self._map_lock:
            cam = self._cams.get(camera_name)
            if cam is not None:
                w = self._workers.get(camera_name)
                if w is not None and w.alive:
                    return cam
                dead = self._take_dead_locked(camera_name)
        if dead is not None:
            self._log(f"[pool] {camera_name} 采图子进程已死，清理后重建")
            self._stop_worker_quiet(dead, camera_name)

        with self._start_lock:          # 串行启动，避免并发 Init 冲突
            dead2 = None
            with self._map_lock:
                cam = self._cams.get(camera_name)
                if cam is not None:
                    w = self._workers.get(camera_name)
                    if w is not None and w.alive:
                        return cam
                    dead2 = self._take_dead_locked(camera_name)
            if dead2 is not None:
                self._stop_worker_quiet(dead2, camera_name)
            w = self._workers.pop("__probe__", None) or CaptureWorker(
                self.project_root, log=self._log
            )
            # probe/复用 worker 也可能已死
            if not w.alive:
                self._stop_worker_quiet(w, "__probe__")
                w = CaptureWorker(self.project_root, log=self._log)
            w.start()
            res = w.call("open", {"camera_name": camera_name}, timeout_s=180.0)
            cam = RemoteCamera(w, res.get("cam_id") or camera_name)
            with self._map_lock:
                self._workers[camera_name] = w
                self._cams[camera_name] = cam
            self._log(f"[pool] {camera_name} 已就绪（独立子进程）")
            return cam

    def get(self, camera_name: str) -> RemoteCamera | None:
        dead = None
        with self._map_lock:
            cam = self._cams.get(camera_name)
            if cam is None:
                return None
            w = self._workers.get(camera_name)
            if w is not None and w.alive:
                return cam
            dead = self._take_dead_locked(camera_name)
        if dead is not None:
            self._log(f"[pool] get({camera_name}) 发现死连接，已清理")
            self._stop_worker_quiet(dead, camera_name)
        return None

    def opened(self) -> list[str]:
        with self._map_lock:
            return list(self._cams)

    def close(self, camera_name: str) -> None:
        with self._map_lock:
            w = self._workers.pop(camera_name, None)
            self._cams.pop(camera_name, None)
        if w is not None:
            try:
                w.stop()
            except Exception as exc:
                self._log(f"[pool] 关闭 {camera_name} 失败: {exc}")

    def close_all(self) -> None:
        for name in list(self._workers):
            self.close(name)
        w = self._workers.pop("__probe__", None)
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass
