#!/usr/bin/env python3
import sys
from pathlib import Path as _ToolsPath
_tools_root = _ToolsPath(__file__).resolve().parents[1]
if str(_tools_root) not in sys.path:
    sys.path.insert(0, str(_tools_root))

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time


class Color:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


def enable_ansi_on_windows():
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def cprint(msg: str, color: str = ""):
    if color:
        print(f"{color}{msg}{Color.RESET}")
    else:
        print(msg)


def pick_one(prompt: str, options: list[str], default: int | None = None) -> int:
    if not options:
        raise RuntimeError(f"{prompt}: no options available")
    for i, x in enumerate(options, start=1):
        mark = " (default)" if default is not None and i == default else ""
        print(f"  {i}. {x}{mark}")
    ask = "Input index"
    if default is not None:
        ask += f" [default {default}]"
    ask += ": "
    s = input(ask).strip()
    if s == "" and default is not None:
        return default - 1
    if not s.isdigit():
        raise RuntimeError("invalid index input")
    idx = int(s) - 1
    if idx < 0 or idx >= len(options):
        raise RuntimeError("index out of range")
    return idx


class QhyApi:
    def __init__(self, project_root: Path):
        self.dll = self._load_qhyccd_dll(Path(project_root))
        self._bind()

    def _load_qhyccd_dll(self, project_root: Path):
        from fpga_paths import search_roots

        project_root = Path(project_root)
        script_dir = Path(__file__).resolve().parent
        env_dll = os.environ.get("QHYCCD_DLL", "").strip()
        env_dll_path = Path(env_dll) if env_dll else None
        deps_dirs = [root / "remoteupdate_deps" for root in search_roots()]

        if hasattr(os, "add_dll_directory"):
            extra = []
            if env_dll_path is not None:
                extra.append(env_dll_path.parent)
            for p in [*extra, *deps_dirs, project_root, script_dir]:
                if Path(p).exists():
                    try:
                        os.add_dll_directory(str(p))
                    except Exception:
                        pass

        candidates = []
        if env_dll_path is not None:
            candidates.append(env_dll_path)
        candidates.extend(d / "qhyccd.dll" for d in deps_dirs)
        candidates.extend(
            [
                script_dir / "qhyccd.dll",
                project_root / "qhyccd.dll",
            ]
        )
        for dll_path in candidates:
            if dll_path.exists():
                try:
                    return ctypes.WinDLL(str(dll_path))
                except OSError as exc:
                    raise RuntimeError(
                        f"failed to load {dll_path}. "
                        f"This usually means dependent runtime DLLs are missing (not only qhyccd.dll). "
                        f"Original error: {exc}"
                    ) from exc

        try:
            return ctypes.WinDLL("qhyccd.dll")
        except OSError as exc:
            searched = ", ".join(str(x) for x in candidates)
            raise RuntimeError(
                "qhyccd.dll not found. Place qhyccd.dll and its dependent DLLs in one of: "
                f"{searched}. Original error: {exc}"
            ) from exc

    def _bind(self):
        self.dll.InitQHYCCDResource.restype = wintypes.DWORD
        self.dll.ReleaseQHYCCDResource.restype = wintypes.DWORD
        self.dll.ScanQHYCCD.restype = wintypes.DWORD

        self.dll.GetQHYCCDId.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte)]
        self.dll.GetQHYCCDId.restype = wintypes.DWORD

        self.dll.OpenQHYCCD.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self.dll.OpenQHYCCD.restype = wintypes.HANDLE

        self.dll.CloseQHYCCD.argtypes = [wintypes.HANDLE]
        self.dll.CloseQHYCCD.restype = wintypes.DWORD

        self.dll.SetQHYCCDStreamMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.dll.SetQHYCCDStreamMode.restype = wintypes.DWORD

        self.dll.QHYCCDVendRequestWrite.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ubyte,
            wintypes.WORD,
            wintypes.WORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        self.dll.QHYCCDVendRequestWrite.restype = wintypes.DWORD

        self.dll.QHYCCDVendRequestRead.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ubyte,
            wintypes.WORD,
            wintypes.WORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        self.dll.QHYCCDVendRequestRead.restype = wintypes.DWORD

    def vend_write(self, handle, req: int, value: int, index: int, payload: bytes):
        n = max(1, len(payload))
        buf = (ctypes.c_ubyte * n)()
        for i, b in enumerate(payload):
            buf[i] = b
        retry = 2
        last = 0
        for i in range(retry + 1):
            ret = self.dll.QHYCCDVendRequestWrite(handle, req, value, index, len(payload), buf)
            last = ret
            if ret == 0:
                return
            if ret == 65535 and i < retry:
                time.sleep(0.15 * (i + 1))
                continue
            break
        raise RuntimeError(f"vend write failed req=0x{req:02X}, ret={last}")

    def vend_read(self, handle, req: int, value: int, index: int, length: int) -> bytes:
        retry = 2
        last = 0
        for i in range(retry + 1):
            buf = (ctypes.c_ubyte * length)()
            ret = self.dll.QHYCCDVendRequestRead(handle, req, value, index, length, buf)
            last = ret
            if ret == 0:
                return bytes(buf)
            if ret == 65535 and i < retry:
                time.sleep(0.15 * (i + 1))
                continue
            break
        raise RuntimeError(f"vend read failed req=0x{req:02X}, ret={last}")

    def vend_read_cmd(self, handle, req: int, value: int, index: int, payload: bytes) -> bytes:
        n = max(1, len(payload))
        buf = (ctypes.c_ubyte * n)()
        for i, b in enumerate(payload):
            buf[i] = b
        ret = self.dll.QHYCCDVendRequestRead(handle, req, value, index, n, buf)
        if ret != 0:
            raise RuntimeError(f"vend read-cmd failed req=0x{req:02X}, ret={ret}")
        return bytes(buf)


@dataclass
class CameraCtx:
    api: QhyApi
    handle: wintypes.HANDLE
    cam_id: str
    cam_id_raw: bytes

    def write_fpga(self, index: int, value: int):
        self.api.vend_write(self.handle, 0xB9, value & 0xFFFF, index & 0xFFFF, bytes([0]))

    def read_fpga(self, index: int) -> int:
        return self.api.vend_read(self.handle, 0xBC, 0, index & 0xFFFF, 1)[0]

    def write_fpga2(self, index: int, value: int):
        self.api.vend_write(self.handle, 0xBD, value & 0xFFFF, index & 0xFFFF, bytes([0]))

    def write_fpga3(self, index: int, value: int):
        self.api.vend_write(self.handle, 0xBD, value & 0xFFFF, index & 0xFFFF, bytes([1]))

    def read_fpga2(self, index: int) -> int:
        return self.api.vend_read(self.handle, 0xBC, 1, index & 0xFFFF, 2)[0]

    def _fpga_rw(self, bus: int):
        if bus == 1:
            return self.read_fpga, self.write_fpga
        if bus == 2:
            return self.read_fpga2, self.write_fpga2
        raise ValueError(f"FPGA bus must be 1 or 2, got {bus}")

    def read_fpga_be(self, start: int, nbytes: int, *, bus: int = 1) -> int:
        """Read nbytes as a big-endian integer (index 0 = MSB)."""
        if nbytes < 1:
            raise ValueError("nbytes must be >= 1")
        read, _ = self._fpga_rw(bus)
        value = 0
        for i in range(nbytes):
            value = (value << 8) | (read(start + i) & 0xFF)
        return value

    def write_fpga_be(self, start: int, nbytes: int, value: int, *, bus: int = 1) -> None:
        """Write nbytes big-endian (index 0 = MSB). Matches Eris 154-158 XTRIG ticks."""
        if nbytes < 1:
            raise ValueError("nbytes must be >= 1")
        _, write = self._fpga_rw(bus)
        for i in range(nbytes):
            shift = 8 * (nbytes - 1 - i)
            write(start + i, (int(value) >> shift) & 0xFF)

    def write_cmos(self, index: int, value: int):
        self.api.vend_write(self.handle, 0xB8, 0x0000, index & 0xFFFF, bytes([value & 0xFF]))

    def write_e0(self, address: int, payload64: bytes):
        if len(payload64) != 64:
            raise RuntimeError("write_e0 requires 64-byte payload")
        w_index = (address // 65536) & 0xFFFF
        w_value = address & 0xFFFF
        self.api.vend_write(self.handle, 0xE0, w_value, w_index, payload64)

    def write_e2(self, address: int, payload64: bytes):
        if len(payload64) != 64:
            raise RuntimeError("write_e2 requires 64-byte payload")
        w_index = (address // 65536) & 0xFFFF
        w_value = address & 0xFFFF
        self.api.vend_write(self.handle, 0xE2, w_value, w_index, payload64)

    def read_e1(self, address: int) -> bytes:
        w_index = (address // 65536) & 0xFFFF
        w_value = address & 0xFFFF
        return self.api.vend_read(self.handle, 0xE1, w_value, w_index, 64)

    def read_e3(self, address: int) -> bytes:
        w_index = (address // 65536) & 0xFFFF
        w_value = address & 0xFFFF
        return self.api.vend_read(self.handle, 0xE3, w_value, w_index, 64)


def read_fpga_version(ctx: CameraCtx, *, bus: int = 1) -> dict:
    """READ_FPGA 200-204 / 207 (bus=1 Eris) or READ_FPGA2 200-203 (bus=2 MAX10)."""
    read, _ = ctx._fpga_rw(bus)
    info = {
        "year": read(200) & 0xFF,
        "month": read(201) & 0xFF,
        "day": read(202) & 0xFF,
        "subversion1": read(203) & 0xFF,
        "board": read(204) & 0xFF,
        "bus": bus,
    }
    if bus == 1:
        info["subversion2"] = read(207) & 0xFF
    else:
        info["subversion"] = info["subversion1"]
    return info


def init_qhy_resource_with_retry(api: QhyApi, retries: int = 3):
    last_ret = None
    last_exc = None
    for i in range(retries):
        try:
            try:
                api.dll.ReleaseQHYCCDResource()
            except Exception:
                pass
            time.sleep(1)
            ret = int(api.dll.InitQHYCCDResource())
            time.sleep(1)
            if ret == 0:
                # Optional: amplify SDK debug stream when qhyccd.ini debugOutPut=true
                en = getattr(api.dll, "EnableQHYCCDMessage", None)
                if callable(en):
                    try:
                        en.argtypes = [ctypes.c_bool]
                        en.restype = None
                        en(True)
                    except Exception:
                        try:
                            en(1)
                        except Exception:
                            pass
                return
            last_ret = ret
        except Exception as exc:
            last_exc = exc
        time.sleep(0.4 * (i + 1))
    if last_exc is not None:
        raise RuntimeError(f"InitQHYCCDResource failed after retries: {last_exc}")
    raise RuntimeError(f"InitQHYCCDResource failed after retries, ret={last_ret}")


def scan_cameras(api: QhyApi) -> list[bytes]:
    init_qhy_resource_with_retry(api, retries=3)
    total = int(api.dll.ScanQHYCCD())
    ids: list[bytes] = []
    for i in range(total):
        buf = (ctypes.c_ubyte * 100)()
        ret = api.dll.GetQHYCCDId(i, buf)
        if ret == 0:
            ids.append(bytes(buf))
    return ids


def decode_camera_name(raw_id: bytes) -> str:
    return raw_id.split(b"\x00", 1)[0].decode("ascii", errors="ignore")


def list_camera_names(ids: list[bytes]) -> list[str]:
    return [decode_camera_name(x) for x in ids]


def open_selected_camera(api: QhyApi, ids: list[bytes]) -> CameraCtx:
    if not ids:
        raise RuntimeError("no camera detected")
    names = list_camera_names(ids)
    if len(names) == 1:
        idx = 0
    else:
        cprint("Detected multiple cameras, select one:", Color.CYAN)
        idx = pick_one("camera", names, default=1)
    handle = api.dll.OpenQHYCCD((ctypes.c_ubyte * len(ids[idx]))(*ids[idx]))
    if not handle:
        raise RuntimeError("OpenQHYCCD failed")
    ret = api.dll.SetQHYCCDStreamMode(handle, 0)
    if ret != 0:
        raise RuntimeError(f"SetQHYCCDStreamMode failed: {ret}")
    return CameraCtx(api=api, handle=handle, cam_id=names[idx], cam_id_raw=ids[idx])


def open_camera_by_name(api: QhyApi, ids: list[bytes], camera_name: str | None) -> CameraCtx:
    if not ids:
        raise RuntimeError("no camera detected")
    if not camera_name:
        return open_selected_camera(api, ids)
    names = list_camera_names(ids)
    try:
        idx = names.index(camera_name)
    except ValueError as exc:
        raise RuntimeError(f"camera not found: {camera_name}") from exc
    handle = api.dll.OpenQHYCCD((ctypes.c_ubyte * len(ids[idx]))(*ids[idx]))
    if not handle:
        raise RuntimeError("OpenQHYCCD failed")
    ret = api.dll.SetQHYCCDStreamMode(handle, 0)
    if ret != 0:
        raise RuntimeError(f"SetQHYCCDStreamMode failed: {ret}")
    return CameraCtx(api=api, handle=handle, cam_id=names[idx], cam_id_raw=ids[idx])


def reopen_camera(ctx: CameraCtx):
    if ctx.handle:
        ctx.api.dll.CloseQHYCCD(ctx.handle)
        ctx.handle = None
    time.sleep(0.8)
    raw = ctx.cam_id_raw
    handle = ctx.api.dll.OpenQHYCCD((ctypes.c_ubyte * len(raw))(*raw))
    if not handle:
        raise RuntimeError("reopen camera failed after firmware switch")
    ret = ctx.api.dll.SetQHYCCDStreamMode(handle, 0)
    if ret != 0:
        raise RuntimeError(f"SetQHYCCDStreamMode failed after reopen: {ret}")
    ctx.handle = handle


def close_camera(api: QhyApi, ctx: CameraCtx | None):
    if ctx and ctx.handle:
        api.dll.CloseQHYCCD(ctx.handle)
    api.dll.ReleaseQHYCCDResource()
