# -*- coding: utf-8 -*-
"""采图子进程：独占一套 QHYCCD SDK 资源，只管扫描 / 配置 / 出图。

GUI 主进程只做寄存器读写（0xB9 / 0xBC），绝不调 `InitQHYCCD`。
两边各自 `InitQHYCCDResource` + `ScanQHYCCD`，互不干扰 —— 这就是
"采图单独一套扫描机制"能落地的唯一方式（同进程内 SDK 是单例，
对同一台相机 `OpenQHYCCD` 两次返回的是同一个句柄）。

用法（由 capture_proxy 拉起，不手工运行）：
    python capture_worker.py --project-root <path>
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from capture_ipc import ShmChannel, decode, encode, pack_gps  # noqa: E402


def _reply(msg: dict) -> None:
    sys.__stdout__.write(encode(msg) + "\n")
    sys.__stdout__.flush()


def _log(text: str) -> None:
    """普通日志：不带协议前缀，由 GUI 原样转发到控制台。"""
    sys.__stdout__.write(str(text).rstrip("\n") + "\n")
    sys.__stdout__.flush()


class Worker:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.api = None
        self.ids: list[bytes] = []
        self.names: list[str] = []
        self.cam = None
        self.prev_shm = ShmChannel("preview", owner=True)
        self.data_shm = ShmChannel("data", owner=True)
        self._settings_cls = None

    # ---------- SDK 生命周期 ----------

    def _ensure_api(self):
        if self.api is not None:
            return self.api
        from qhyccd_api import QhyApi, init_qhy_resource_with_retry
        from qhyccd_highlevel import CameraSettings, _ensure_highlevel_bindings

        self._settings_cls = CameraSettings
        api = QhyApi(self.project_root)
        _ensure_highlevel_bindings(api)
        init_qhy_resource_with_retry(api)
        self.api = api
        return api

    def op_scan(self, _args: dict) -> dict:
        from qhyccd_api import list_camera_names, scan_cameras

        api = self._ensure_api()
        self.ids = scan_cameras(api)
        self.names = list_camera_names(self.ids)
        return {"names": list(self.names)}

    def op_open(self, args: dict) -> dict:
        from qhyccd_api import open_camera_by_name
        from qhyccd_highlevel import SoftCamera

        name = args.get("camera_name") or None
        api = self._ensure_api()
        if not self.ids:
            self.op_scan({})
        if self.cam is not None:
            if name and self.cam.ctx.cam_id == name:
                return {"cam_id": self.cam.ctx.cam_id, "reused": True}
            self.op_close({})
        ctx = open_camera_by_name(api, self.ids, name)
        self.cam = SoftCamera(ctx)
        _log(f"[capture] opened {ctx.cam_id}")
        return {"cam_id": ctx.cam_id, "reused": False}

    def op_close(self, _args: dict) -> dict:
        from qhyccd_highlevel import close_soft_camera

        if self.cam is not None:
            try:
                if getattr(self.cam, "_live", False):
                    self.cam.stop_live()
            except Exception as exc:
                _log(f"[capture] stop_live on close: {exc}")
            try:
                close_soft_camera(self.cam)
            except Exception as exc:
                _log(f"[capture] close failed: {exc}")
            self.cam = None
        return {"closed": True}

    # ---------- 配置 ----------

    def _settings(self, d: dict):
        cls = self._settings_cls
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})

    def _need_cam(self):
        if self.cam is None:
            raise RuntimeError("采图会话未打开")
        return self.cam

    def op_prepare_session(self, args: dict) -> dict:
        cam = self._need_cam()
        cam._prepare_session(int(args["read_mode"]), int(args["stream_mode"]))
        return {"ok": True}

    def op_refresh_geometry(self, args: dict) -> dict:
        cam = self._need_cam()
        geo = cam.refresh_geometry(
            read_mode=int(args["read_mode"]),
            bin_x=int(args.get("bin_x", 1)),
            bin_y=int(args.get("bin_y", 1)),
        )
        return {"geo": geo}

    def op_camera_info(self, _args: dict) -> dict:
        return {"info": self._need_cam().camera_info()}

    def op_list_read_modes(self, _args: dict) -> dict:
        return {"modes": [list(x) for x in self._need_cam().list_read_modes()]}

    def op_apply_settings(self, args: dict) -> dict:
        cam = self._need_cam()
        geo = cam.apply_settings(self._settings(args["settings"]))
        return {"geo": geo}

    def op_apply_runtime_controls(self, args: dict) -> dict:
        cam = self._need_cam()
        return {"applied": list(cam.apply_runtime_controls(self._settings(args["settings"])) or [])}

    def op_apply_gps_led_positions(self, args: dict) -> dict:
        cam = self._need_cam()
        cam.apply_gps_led_positions(
            pos_a=int(args.get("pos_a", 0)),
            pos_b=int(args.get("pos_b", 0)),
            width=int(args.get("width", 40)),
            slave=bool(args.get("slave", False)),
            leave_led_cal_on=bool(args.get("leave_led_cal_on", False)),
        )
        return {"ok": True}

    def op_set_gps_slave_utc(self, args: dict) -> dict:
        cam = self._need_cam()
        fn = getattr(cam, "set_gps_slave_utc", None)
        if fn is None:
            raise RuntimeError("SoftCamera 没有 set_gps_slave_utc")
        return {"result": fn(*args.get("pos_args", []), **args.get("kw_args", {}))}

    # ---------- 出图 ----------

    def op_begin_live(self, _args: dict) -> dict:
        self._need_cam().begin_live()
        return {"ok": True}

    def op_stop_live(self, _args: dict) -> dict:
        cam = self._need_cam()
        try:
            cam.stop_live()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _frame_payload(self, fr, want_data: bool) -> dict:
        """像素走共享内存，控制流只带元数据。"""
        out = {
            "width": int(getattr(fr, "width", 0) or 0),
            "height": int(getattr(fr, "height", 0) or 0),
            "bpp": int(getattr(fr, "bpp", 0) or 0),
            "channels": int(getattr(fr, "channels", 1) or 1),
            "preview_w": int(getattr(fr, "preview_w", 0) or 0),
            "preview_h": int(getattr(fr, "preview_h", 0) or 0),
            "gps": pack_gps(getattr(fr, "gps", None)),
        }
        prev = getattr(fr, "preview_gray", b"") or b""
        if prev:
            name, n = self.prev_shm.write(bytes(prev))
            out["preview_shm"] = name
            out["preview_nbytes"] = n
        data = getattr(fr, "data", b"") or b""
        if want_data and data:
            name, n = self.data_shm.write(bytes(data))
            out["data_shm"] = name
            out["data_nbytes"] = n
        elif data:
            # 即便不要整帧，也带上帧头，GUI 侧解析 GPS 用得上。
            out["head"] = bytes(data[:64]).hex()
        return out

    def op_capture_live_frame(self, args: dict) -> dict:
        cam = self._need_cam()
        kw = {
            "parse_gps": bool(args.get("parse_gps", False)),
            "timeout_s": float(args.get("timeout_s", 5.0)),
        }
        if args.get("copy_pixels") is not None:
            kw["copy_pixels"] = bool(args["copy_pixels"])
        if args.get("preview_max_dim"):
            kw["preview_max_dim"] = int(args["preview_max_dim"])
        try:
            fr = cam.capture_live_frame(**kw)
        except TypeError:
            kw.pop("copy_pixels", None)
            kw.pop("preview_max_dim", None)
            fr = cam.capture_live_frame(**kw)
        # copy_pixels=True 时默认把整帧塞进 data_shm，避免 GUI 只拿到 64B 帧头却去存盘
        want = bool(args.get("want_data", False)) or bool(args.get("copy_pixels", False))
        return {"frame": self._frame_payload(fr, want)}

    def op_capture_single(self, args: dict) -> dict:
        cam = self._need_cam()
        fr = cam.capture_single(
            parse_gps=bool(args.get("parse_gps", False)),
            timeout_s=float(args.get("timeout_s", 30.0)),
        )
        return {"frame": self._frame_payload(fr, True)}

    # 允许 GUI 通过代理调用的相机方法白名单。
    # 刻意不含 write_fpga / read_fpga —— 寄存器一律走 GUI 主进程，
    # 免得又出现两条路径抢同一台相机的老问题。
    ALLOWED_CALLS = {
        "init_camera",
        "set_stream_mode",
        "set_read_mode",
        "set_gps_enable",
        "try_set_gps_master_slave",
        "set_gps_slave_target_utc",
        "apply_gps_led_positions",
        "get_param",          # 只读
        "get_param_range",    # 只读
        "set_param",          # 运行中改 usb_traffic 等，不重新 Init
    }

    def op_cam_call(self, args: dict) -> dict:
        name = str(args.get("name", ""))
        if name not in self.ALLOWED_CALLS:
            raise RuntimeError(f"不在白名单里的相机方法: {name}")
        fn = getattr(self._need_cam(), name, None)
        if fn is None:
            raise RuntimeError(f"SoftCamera 没有方法 {name}")
        out = fn(*(args.get("args") or []), **(args.get("kwargs") or {}))
        if not isinstance(out, (str, int, float, bool, list, dict, type(None))):
            out = repr(out)
        return {"result": out}

    def op_cam_attr(self, args: dict) -> dict:
        cam = self._need_cam()
        return {"value": bool(getattr(cam, str(args.get("name", "")), False))}

    def op_state(self, _args: dict) -> dict:
        cam = self.cam
        return {
            "cam_id": None if cam is None else cam.ctx.cam_id,
            "live": bool(getattr(cam, "_live", False)) if cam else False,
            "settings": (cam.settings.as_dict() if cam is not None and getattr(cam, "settings", None) else None),
        }

    def op_shutdown(self, _args: dict) -> dict:
        # 显式 shutdown 与 serve() 的 finally 都会走到这里，必须可重入。
        if getattr(self, "_down", False):
            return {"bye": True, "already": True}
        self._down = True
        self.op_close({})
        self.prev_shm.close()
        self.data_shm.close()
        if self.api is not None:
            try:
                self.api.dll.ReleaseQHYCCDResource()
            except Exception:
                pass
            self.api = None
        return {"bye": True}

    # ---------- 主循环 ----------

    def serve(self) -> int:
        """stdin 一旦关闭（GUI 崩溃/被杀）也必须收好相机。

        漏掉这一步的代价是实测过的：句柄不关就退出，相机会整台掉出 USB
        总线，只能物理断电才能回来（软件端口复位 IOCTL 返回 err=31）。
        """
        try:
            return self._serve_loop()
        finally:
            try:
                self.op_shutdown({})
            except Exception:
                _log("[capture] 退出清理失败:\n" + traceback.format_exc(limit=4))

    def _serve_loop(self) -> int:
        ops = {n[3:]: getattr(self, n) for n in dir(self) if n.startswith("op_")}
        _reply({"event": "ready", "pid": os.getpid()})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = decode(line)
            except Exception as exc:
                _log(f"[capture] 无法解析请求: {exc}")
                continue
            if req is None:
                continue
            rid = req.get("id")
            op = req.get("op", "")
            fn = ops.get(op)
            if fn is None:
                _reply({"id": rid, "ok": False, "error": f"未知操作: {op}"})
                continue
            try:
                _reply({"id": rid, "ok": True, "result": fn(req.get("args") or {})})
            except Exception as exc:
                _reply({
                    "id": rid,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc(limit=6),
                })
            if op == "shutdown":
                break
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    a = ap.parse_args()
    # 与 SoftGui 同一套优先级：deps（旁路 qhyccd.ini debug）> AllInOne。
    # 若父进程已设 QHYCCD_DLL，则沿用，保证主/子进程同一 DLL。
    deps = Path(__file__).resolve().parent.parent / "remoteupdate_deps" / "qhyccd.dll"
    allinone = Path(r"C:\Program Files\QHYCCD\AllInOne\sdk\x64\qhyccd.dll")
    if not os.environ.get("QHYCCD_DLL"):
        if deps.is_file():
            os.environ["QHYCCD_DLL"] = str(deps)
        elif allinone.is_file():
            os.environ["QHYCCD_DLL"] = str(allinone)
    # 确认 CWD 有 ini（父进程通常已把 cwd 设到 DLL 目录）
    dll = Path(os.environ.get("QHYCCD_DLL", "") or "")
    ini = Path.cwd() / "qhyccd.ini"
    if dll.is_file() and not ini.is_file():
        src = dll.with_name("qhyccd.ini")
        if src.is_file():
            try:
                ini.write_text(src.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
            except Exception:
                pass
    print(
        f"[capture_worker] DLL={dll} cwd={Path.cwd()} ini_exists={ini.is_file()}",
        flush=True,
    )
    # 必须 append 而不是 insert(0)：FPGA_Tools 里也有一份 qhyccd_highlevel，
    # 它的 capture_live_frame 没有 copy_pixels / preview_max_dim。抢到前面就会
    # 退化成整帧拷贝且不生成预览图，GUI 侧拿不到可显示的数据。host/ 必须优先。
    fpga_tools = r"J:\Mywork\Project_LH\git\FPGA_Tools"
    if fpga_tools not in sys.path:
        sys.path.append(fpga_tools)
    return Worker(Path(a.project_root)).serve()


if __name__ == "__main__":
    raise SystemExit(main())
