# -*- coding: utf-8 -*-
"""采图子进程与 GUI 之间的传输层。

为什么要拆进程：QHYCCD SDK 在一个进程里对同一台相机只有一个对象
（`OpenQHYCCD` 两次返回同一句柄），`InitQHYCCD` 会下 `LowLevelA0` 切换
单帧/live 模式并把 `reg39` 写回 1/5。于是"采图"与"寄存器/模式"共用一个
进程时必然互相踩：在 GUI 里切一次相机，主从同步的 `39=0x11 / 39=2` 就没了。

拆成两个进程后各自持有独立的 SDK 资源，互不影响。

控制流：换行分隔的 JSON，一问一答。
像素流：共享内存，不进管道（整帧 9600x6422 有 61MB）。
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from multiprocessing import shared_memory

PROTOCOL_VERSION = 1

# 子进程 stdout 里，协议行以此前缀开头；其余行当作日志转发给 GUI 控制台。
LINE_PREFIX = "\x01CAPIPC\x01"


def encode(msg: dict) -> str:
    return LINE_PREFIX + json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


def decode(line: str) -> dict | None:
    """返回协议消息；不是协议行（即普通日志）时返回 None。"""
    if not line.startswith(LINE_PREFIX):
        return None
    return json.loads(line[len(LINE_PREFIX):])


class ShmChannel:
    """一块按需增长的共享内存。

    worker 侧 `owner=True` 负责创建/释放；GUI 侧按名字附着。
    名字带容量后缀，容量一变名字就变，附着方不会读到过期的块。
    """

    def __init__(self, tag: str, *, owner: bool):
        self.tag = tag
        self.owner = owner
        self.shm: shared_memory.SharedMemory | None = None
        self.name = ""

    def ensure(self, nbytes: int) -> str:
        """owner 侧：保证容量不小于 nbytes，返回块名。"""
        if not self.owner:
            raise RuntimeError("ensure() 只能在 owner 侧调用")
        if self.shm is not None and self.shm.size >= nbytes:
            return self.name
        self.close()
        size = max(int(nbytes), 1 << 20)
        self.shm = shared_memory.SharedMemory(create=True, size=size)
        self.name = self.shm.name
        return self.name

    def write(self, payload: bytes) -> tuple[str, int]:
        name = self.ensure(len(payload))
        assert self.shm is not None
        self.shm.buf[: len(payload)] = payload
        return name, len(payload)

    def read(self, name: str, nbytes: int) -> bytes:
        """附着侧：按名字取出 nbytes 的副本。"""
        if nbytes <= 0:
            return b""
        if self.shm is None or self.name != name:
            self.close()
            self.shm = shared_memory.SharedMemory(name=name)
            self.name = name
        return bytes(self.shm.buf[:nbytes])

    def close(self) -> None:
        if self.shm is not None:
            try:
                self.shm.close()
            except Exception:
                pass
            if self.owner:
                try:
                    self.shm.unlink()
                except Exception:
                    pass
        self.shm = None
        self.name = ""


@dataclass
class RemoteFrame:
    """GUI 侧的帧对象，字段与 `qhyccd_highlevel.FrameResult` 对齐，

    这样 `_prepare_preview_base` / `_queue_preview` 不必改。
    """

    width: int = 0
    height: int = 0
    bpp: int = 0
    channels: int = 1
    data: bytes = b""
    gps: object = None
    preview_w: int = 0
    preview_h: int = 0
    preview_gray: bytes = b""


def pack_gps(gps) -> dict | None:
    if gps is None:
        return None
    out = {}
    # 必须带 height / exposure / lat-lon：GUI 帧序号门禁与预览文案会读这些字段。
    # 旧版漏 height 时 GpsView 无 height → gps_header_issues / format 抛 AttributeError，
    # Capture 开 GPS 后预览整段失败，表现为「帧序号没有显示」。
    for k in (
        "seq_number", "width", "height",
        "latitude", "longitude", "elevation",
        "start_sec", "start_us", "end_sec", "end_us",
        "now_flag", "now_sec", "now_us",
        "pps", "gps_status", "exposure_us", "exposure_ticks",
        "long_lat", "max_clock",
    ):
        if hasattr(gps, k):
            v = getattr(gps, k)
            if isinstance(v, (int, float, str, bool)) or v is None:
                out[k] = v
    return out


class GpsView:
    """GPS 字段的只读还原体，属性访问方式与 SDK 的 GpsInfo 一致。"""

    def __init__(self, d: dict):
        # 缺省字段给 0，避免旧 worker / 残缺 payload 让 GUI 读属性炸掉
        defaults = {
            "seq_number": 0, "width": 0, "height": 0,
            "latitude": 0.0, "longitude": 0.0, "elevation": 0,
            "start_sec": 0, "start_us": 0, "end_sec": 0, "end_us": 0,
            "now_flag": 0, "now_sec": 0, "now_us": 0,
            "pps": 0, "gps_status": 0, "exposure_us": 0.0, "exposure_ticks": 0,
        }
        defaults.update(d or {})
        self.__dict__.update(defaults)

    def __repr__(self) -> str:
        return f"GpsView({self.__dict__})"


def unpack_gps(d: dict | None):
    return None if d is None else GpsView(d)
