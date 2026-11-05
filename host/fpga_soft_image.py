#!/usr/bin/env python3
"""RAW frame decode + pixel analysis for soft capture CLI/GUI.

powered by liusir
"""

from __future__ import annotations

import sys
from pathlib import Path as _ToolsPath
_tools_root = _ToolsPath(__file__).resolve().parents[1]
if str(_tools_root) not in sys.path:
    sys.path.insert(0, str(_tools_root))

from base64 import b64encode
import json
import math
from pathlib import Path
from typing import Iterable

from qhyccd_highlevel import FrameResult

_NP = None
_NP_TRIED = False


def _numpy():
    """Lazy numpy import; preview path stays usable without it."""
    global _NP, _NP_TRIED
    if not _NP_TRIED:
        _NP_TRIED = True
        try:
            import numpy as np  # type: ignore

            _NP = np
        except Exception:
            _NP = None
    return _NP


def frame_pixel_count(frame: FrameResult) -> int:
    bpp = max(8, frame.bpp)
    bps = (bpp + 7) // 8
    return frame.width * frame.height * max(1, frame.channels) * bps


def decode_mono_samples(frame: FrameResult, *, max_samples: int | None = None) -> list[int]:
    """Decode mono frame to integer sample list (8 or 16 bpp)."""
    w, h = frame.width, frame.height
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid frame size {w}x{h}")
    total = w * h
    data = frame.data
    np = _numpy()
    if np is not None:
        if frame.bpp <= 8:
            arr = np.frombuffer(data, dtype=np.uint8, count=min(total, len(data)))
        else:
            n16 = min(total, len(data) // 2)
            arr = np.frombuffer(data, dtype="<u2", count=n16)
        if max_samples is not None and arr.size > max_samples:
            step = max(1, arr.size // max_samples)
            arr = arr[::step][:max_samples]
        return arr.astype(np.int32, copy=False).tolist()

    if frame.bpp <= 8:
        n = min(total, len(data))
        out = list(data[:n])
    else:
        n = min(total, len(data) // 2)
        mv = memoryview(data[: n * 2]).cast("H")
        out = list(mv)
    if max_samples is not None and len(out) > max_samples:
        step = max(1, len(out) // max_samples)
        out = out[::step][:max_samples]
    return out


def _gray8_full(frame: FrameResult):
    """Return a 2D uint8 view/array of the frame (high byte if 16-bit)."""
    w, h = frame.width, frame.height
    n = w * h
    data = frame.data
    np = _numpy()
    if np is not None:
        if frame.bpp <= 8:
            img = np.frombuffer(data, dtype=np.uint8, count=min(n, len(data)))
            if img.size < n:
                pad = np.zeros(n, dtype=np.uint8)
                pad[: img.size] = img
                img = pad
            return img.reshape(h, w)
        n16 = min(n, len(data) // 2)
        img16 = np.frombuffer(data, dtype="<u2", count=n16)
        if img16.size < n:
            pad = np.zeros(n, dtype=np.uint16)
            pad[: img16.size] = img16
            img16 = pad
        return (img16.reshape(h, w) >> 8).astype(np.uint8)
    if frame.bpp <= 8:
        buf = bytearray(n)
        buf[: min(n, len(data))] = data[: min(n, len(data))]
        return w, h, bytes(buf)
    n16 = min(n, len(data) // 2)
    out = bytearray(n)
    src = memoryview(data)
    for i in range(n16):
        out[i] = src[i * 2 + 1]
    return w, h, bytes(out)


def downscale_gray8(frame: FrameResult, *, max_dim: int = 800) -> tuple[int, int, bytes]:
    """Return (w,h) and 8-bit grayscale bytes for GUI preview.

    Subsample first (especially 16-bit full frames) so live preview does not
    convert tens of megapixels on every grab.
    """
    w, h = frame.width, frame.height
    if w <= 0 or h <= 0:
        return 0, 0, b""
    scale = max(w, h) / float(max_dim)
    if scale < 1.0:
        scale = 1.0
    dw = max(1, int(w / scale))
    dh = max(1, int(h / scale))
    np = _numpy()
    data = frame.data
    n = w * h
    if np is not None:
        ys = xs = None
        if dw != w or dh != h:
            ys = (np.linspace(0, h - 1, dh)).astype(np.intp)
            xs = (np.linspace(0, w - 1, dw)).astype(np.intp)
        if frame.bpp <= 8:
            img = np.frombuffer(data, dtype=np.uint8, count=min(n, len(data)))
            if img.size < n:
                pad = np.zeros(n, dtype=np.uint8)
                pad[: img.size] = img
                img = pad
            img = img.reshape(h, w)
            if ys is None:
                return dw, dh, img.tobytes()
            return dw, dh, img[ys[:, None], xs].tobytes()
        n16 = min(n, len(data) // 2)
        img16 = np.frombuffer(data, dtype="<u2", count=n16)
        if img16.size < n:
            pad = np.zeros(n, dtype=np.uint16)
            pad[: img16.size] = img16
            img16 = pad
        img16 = img16.reshape(h, w)
        if ys is None:
            return dw, dh, (img16 >> 8).astype(np.uint8).tobytes()
        return dw, dh, (img16[ys[:, None], xs] >> 8).astype(np.uint8).tobytes()
    gray = _gray8_full(frame)
    _gw, _gh, src = gray if isinstance(gray, tuple) else (w, h, gray.tobytes() if hasattr(gray, "tobytes") else gray)
    if dw == w and dh == h:
        return dw, dh, src if isinstance(src, (bytes, bytearray)) else bytes(src)
    out = bytearray(dw * dh)
    for y in range(dh):
        sy = min(h - 1, int(y * scale))
        row_off = sy * w
        dst = y * dw
        for x in range(dw):
            sx = min(w - 1, int(x * scale))
            out[dst + x] = src[row_off + sx]
    return dw, dh, bytes(out)


def downscale_gray8_cbuf(buf, width: int, height: int, bpp: int, *, max_dim: int = 256) -> tuple[int, int, bytes]:
    """Subsample 8/16-bit pixels from a ctypes frame buffer without copying the full frame."""
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return 0, 0, b""
    scale = max(w, h) / float(max_dim)
    if scale < 1.0:
        scale = 1.0
    dw = max(1, int(w / scale))
    dh = max(1, int(h / scale))
    bpp = int(bpp)
    elem = 2 if bpp > 8 else 1
    nbytes = w * h * elem
    np = _numpy()
    if np is not None:
        raw = np.ctypeslib.as_array(buf)
        raw = raw[:nbytes]
        ys = (np.linspace(0, h - 1, dh)).astype(np.intp)
        xs = (np.linspace(0, w - 1, dw)).astype(np.intp)
        if bpp > 8:
            img16 = raw.view("<u2")[: w * h].reshape(h, w)
            return dw, dh, (img16[ys[:, None], xs] >> 8).astype(np.uint8).tobytes()
        img = raw[: w * h].reshape(h, w)
        return dw, dh, img[ys[:, None], xs].tobytes()
    out = bytearray(dw * dh)
    for y in range(dh):
        sy = min(h - 1, int(y * scale))
        dst = y * dw
        row = sy * w
        if bpp > 8:
            for x in range(dw):
                sx = min(w - 1, int(x * scale))
                out[dst + x] = buf[(row + sx) * 2 + 1]
        else:
            for x in range(dw):
                sx = min(w - 1, int(x * scale))
                out[dst + x] = buf[row + sx]
    return dw, dh, bytes(out)


def adjust_gray8(
    gray: bytes,
    *,
    brightness: float = 0.0,
    contrast: float = 1.0,
    black: int = 0,
    white: int = 255,
) -> bytes:
    """Display-only grayscale remap for preview.

    1) Stretch input [black, white] → [0, 255]
    2) Apply contrast around mid-gray, then brightness offset
    """
    lo = max(0, min(255, int(black)))
    hi = max(0, min(255, int(white)))
    if hi <= lo:
        hi = min(255, lo + 1)
    span = float(hi - lo)
    c = float(contrast)
    b = float(brightness)
    lut = bytearray(256)
    for i in range(256):
        # levels
        t = (i - lo) / span
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        v = t * 255.0
        # contrast + brightness
        v = (v - 128.0) * c + 128.0 + b
        if v < 0.0:
            v = 0.0
        elif v > 255.0:
            v = 255.0
        lut[i] = int(v)
    np = _numpy()
    if np is not None:
        src = np.frombuffer(gray, dtype=np.uint8)
        table = np.frombuffer(lut, dtype=np.uint8)
        return table[src].tobytes()
    return bytes(lut[v] for v in gray)


def gray8_percentile_levels(gray: bytes, *, low_pct: float = 1.0, high_pct: float = 99.0) -> tuple[int, int]:
    """Return (black, white) from histogram percentiles for auto stretch."""
    if not gray:
        return 0, 255
    np = _numpy()
    if np is not None:
        hist_np = np.bincount(np.frombuffer(gray, dtype=np.uint8), minlength=256)
        hist = hist_np.tolist()
    else:
        hist = [0] * 256
        for v in gray:
            hist[v] += 1
    total = len(gray)
    lo_need = max(0.0, min(100.0, float(low_pct))) / 100.0 * total
    hi_need = max(0.0, min(100.0, float(high_pct))) / 100.0 * total
    black = 0
    white = 255
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= lo_need:
            black = i
            break
    acc = 0
    for i, c in enumerate(hist):
        acc += c
        if acc >= hi_need:
            white = i
            break
    if white <= black:
        white = min(255, black + 1)
    return black, white


def scale_gray8(gray: bytes, width: int, height: int, zoom: float, *, max_dim: int = 2400) -> tuple[int, int, bytes]:
    """Nearest-neighbor scale of an 8-bit grayscale buffer. zoom>0; result capped by max_dim."""
    if width <= 0 or height <= 0 or not gray:
        return 0, 0, b""
    z = float(zoom)
    if z <= 0:
        z = 1.0
    nw = max(1, int(round(width * z)))
    nh = max(1, int(round(height * z)))
    # Cap to keep PhotoImage responsive
    if max(nw, nh) > max_dim:
        s = max(nw, nh) / float(max_dim)
        nw = max(1, int(nw / s))
        nh = max(1, int(nh / s))
    if nw == width and nh == height:
        return width, height, gray
    np = _numpy()
    if np is not None:
        img = np.frombuffer(gray, dtype=np.uint8, count=width * height).reshape(height, width)
        ys = (np.linspace(0, height - 1, nh)).astype(np.intp)
        xs = (np.linspace(0, width - 1, nw)).astype(np.intp)
        return nw, nh, img[ys[:, None], xs].tobytes()
    out = bytearray(nw * nh)
    x_ratio = width / float(nw)
    y_ratio = height / float(nh)
    for y in range(nh):
        sy = min(height - 1, int(y * y_ratio))
        src_row = sy * width
        dst_row = y * nw
        for x in range(nw):
            sx = min(width - 1, int(x * x_ratio))
            out[dst_row + x] = gray[src_row + sx]
    return nw, nh, bytes(out)


def fit_zoom_for_size(width: int, height: int, fit_max: int = 640) -> float:
    """Zoom factor so max(w,h)*zoom <= fit_max."""
    if width <= 0 or height <= 0:
        return 1.0
    m = max(width, height)
    if m <= fit_max:
        return 1.0
    return fit_max / float(m)


def crop_gray8_center(
    gray: bytes, width: int, height: int, crop_w: int, crop_h: int
) -> tuple[int, int, bytes]:
    """从中心裁出 crop_w×crop_h；若目标更大则原样返回。"""
    crop_w = max(1, int(crop_w))
    crop_h = max(1, int(crop_h))
    if width <= 0 or height <= 0 or not gray:
        return 0, 0, b""
    if crop_w >= width and crop_h >= height:
        return width, height, gray
    cw = min(crop_w, width)
    ch = min(crop_h, height)
    x0 = max(0, (width - cw) // 2)
    y0 = max(0, (height - ch) // 2)
    np = _numpy()
    if np is not None:
        img = np.frombuffer(gray, dtype=np.uint8, count=width * height).reshape(height, width)
        return cw, ch, img[y0 : y0 + ch, x0 : x0 + cw].tobytes()
    out = bytearray(cw * ch)
    for y in range(ch):
        src = (y0 + y) * width + x0
        dst = y * cw
        out[dst : dst + cw] = gray[src : src + cw]
    return cw, ch, bytes(out)


def gray8_quick_stats(gray: bytes) -> tuple[int, int, float, float]:
    """Return (min, max, mean, std) for an 8-bit buffer."""
    if not gray:
        return 0, 0, 0.0, 0.0
    np = _numpy()
    if np is not None:
        a = np.frombuffer(gray, dtype=np.uint8)
        return int(a.min()), int(a.max()), float(a.mean()), float(a.std())
    n = len(gray)
    vmin = 255
    vmax = 0
    total = 0
    for v in gray:
        if v < vmin:
            vmin = v
        if v > vmax:
            vmax = v
        total += v
    mean = total / n
    var = 0.0
    for v in gray:
        d = v - mean
        var += d * d
    return vmin, vmax, mean, math.sqrt(var / n)


def gray8_to_pgm_data(gray: bytes, width: int, height: int) -> bytes:
    """Binary P5 PGM (grayscale) for Tk PhotoImage — 1/3 the size of RGB PPM."""
    n = width * height
    src = gray[:n]
    if len(src) < n:
        src = bytes(src) + bytes(n - len(src))
    return f"P5 {width} {height} 255\n".encode("ascii") + src


def render_preview_gray8(
    gray: bytes,
    width: int,
    height: int,
    *,
    brightness: float = 0.0,
    contrast: float = 1.0,
    black: int = 0,
    white: int = 255,
    zoom: float = 1.0,
    max_dim: int = 1200,
) -> tuple[int, int, bytes]:
    """Adjust + scale on a worker thread; UI only builds PhotoImage."""
    adjusted = adjust_gray8(
        gray,
        brightness=brightness,
        contrast=contrast,
        black=black,
        white=white,
    )
    return scale_gray8(adjusted, width, height, zoom, max_dim=max_dim)


def gray8_to_ppm_data(gray: bytes, width: int, height: int) -> bytes:
    """Binary P6 PPM (RGB) fallback if PGM is rejected."""
    n = width * height
    src = gray[:n]
    np = _numpy()
    if np is not None:
        g = np.frombuffer(src, dtype=np.uint8, count=len(src))
        if g.size < n:
            pad = np.zeros(n, dtype=np.uint8)
            pad[: g.size] = g
            g = pad
        rgb = np.repeat(g[:, None], 3, axis=1).reshape(-1).tobytes()
    else:
        rgb = bytes(b for v in src for b in (v, v, v))
        if len(src) < n:
            rgb += b"\x00\x00\x00" * (n - len(src))
    return f"P6 {width} {height} 255\n".encode("ascii") + rgb


_PIL_TK = None


def _pil_tk():
    global _PIL_TK
    if _PIL_TK is False:
        return None
    if _PIL_TK is not None:
        return _PIL_TK
    try:
        from PIL import Image, ImageTk  # type: ignore

        _PIL_TK = (Image, ImageTk)
        return _PIL_TK
    except Exception:
        _PIL_TK = False
        return None


def gray8_to_tk_photo(gray: bytes, width: int, height: int):
    """Build a Tk PhotoImage without per-pixel put() (that freezes the GUI)."""
    import tkinter as tk

    if width <= 0 or height <= 0 or not gray:
        return tk.PhotoImage(width=1, height=1)
    pil = _pil_tk()
    if pil is not None:
        Image, ImageTk = pil
        im = Image.frombytes("L", (width, height), gray[: width * height])
        return ImageTk.PhotoImage(im)
    try:
        return tk.PhotoImage(data=b64encode(gray8_to_pgm_data(gray, width, height)))
    except Exception:
        return tk.PhotoImage(data=b64encode(gray8_to_ppm_data(gray, width, height)))


def _roi_indices(w: int, h: int, roi: tuple[int, int, int, int] | None) -> Iterable[int]:
    if roi is None:
        return range(w * h)
    x0, y0, rw, rh = roi
    x1 = min(w, x0 + rw)
    y1 = min(h, y0 + rh)
    for y in range(max(0, y0), max(0, y1)):
        base = y * w
        for x in range(max(0, x0), max(0, x1)):
            yield base + x


def analyze_frame(
    frame: FrameResult,
    *,
    roi: tuple[int, int, int, int] | None = None,
    histogram_bins: int = 16,
    max_pixels: int | None = None,
) -> dict:
    w, h = frame.width, frame.height
    np = _numpy()
    if np is not None:
        n = w * h
        if frame.bpp <= 8:
            arr = np.frombuffer(frame.data, dtype=np.uint8, count=min(n, len(frame.data)))
            if arr.size < n:
                pad = np.zeros(n, dtype=np.uint8)
                pad[: arr.size] = arr
                arr = pad
        else:
            n16 = min(n, len(frame.data) // 2)
            arr = np.frombuffer(frame.data, dtype="<u2", count=n16)
            if arr.size < n:
                pad = np.zeros(n, dtype=np.uint16)
                pad[: arr.size] = arr
                arr = pad
        img = arr.reshape(h, w)
        if roi is not None:
            x0, y0, rw, rh = roi
            x1 = min(w, max(0, x0) + max(0, rw))
            y1 = min(h, max(0, y0) + max(0, rh))
            img = img[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
        vals_np = img.reshape(-1)
        if vals_np.size == 0:
            raise ValueError("ROI produced no pixels")
        if max_pixels is not None and vals_np.size > max_pixels:
            step = max(1, vals_np.size // max_pixels)
            vals_np = vals_np[::step][:max_pixels]
        n_pix = int(vals_np.size)
        vmin = int(vals_np.min())
        vmax = int(vals_np.max())
        mean = float(vals_np.mean())
        std = float(vals_np.std())
        hist: dict[str, int] = {}
        if histogram_bins > 0:
            span = max(1, vmax - vmin + 1)
            bin_w = max(1, span // histogram_bins)
            b = np.minimum(histogram_bins - 1, (vals_np.astype(np.int64) - vmin) // bin_w)
            counts = np.bincount(b, minlength=histogram_bins)
            for i, c in enumerate(counts.tolist()):
                if c:
                    hist[f"bin{i}"] = int(c)
        return {
            "width": w,
            "height": h,
            "bpp": frame.bpp,
            "channels": frame.channels,
            "roi": list(roi) if roi else None,
            "pixels": n_pix,
            "min": vmin,
            "max": vmax,
            "mean": mean,
            "std": std,
            "histogram_bins": histogram_bins,
            "histogram": hist,
            "gps": frame.gps.as_dict() if frame.gps else None,
        }

    samples = decode_mono_samples(frame)
    if not samples:
        raise ValueError("empty frame")
    idxs = list(_roi_indices(w, h, roi))
    vals = [samples[i] for i in idxs if i < len(samples)]
    if max_pixels is not None and len(vals) > max_pixels:
        step = max(1, len(vals) // max_pixels)
        vals = vals[::step][:max_pixels]
    if not vals:
        raise ValueError("ROI produced no pixels")
    n = len(vals)
    s = sum(vals)
    mean = s / n
    var = sum((v - mean) ** 2 for v in vals) / n
    vmin = min(vals)
    vmax = max(vals)
    hist: dict[str, int] = {}
    if histogram_bins > 0:
        span = max(1, vmax - vmin + 1)
        bin_w = max(1, span // histogram_bins)
        for v in vals:
            b = min(histogram_bins - 1, (v - vmin) // bin_w)
            key = f"bin{b}"
            hist[key] = hist.get(key, 0) + 1
    return {
        "width": w,
        "height": h,
        "bpp": frame.bpp,
        "channels": frame.channels,
        "roi": list(roi) if roi else None,
        "pixels": n,
        "min": vmin,
        "max": vmax,
        "mean": mean,
        "std": math.sqrt(var),
        "histogram_bins": histogram_bins,
        "histogram": hist,
        "gps": frame.gps.as_dict() if frame.gps else None,
    }


def load_frame_from_raw(path: Path, meta_path: Path | None = None) -> FrameResult:
    data = path.read_bytes()
    w = h = bpp = channels = 0
    gps = None
    if meta_path and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        w = int(meta.get("width", 0))
        h = int(meta.get("height", 0))
        bpp = int(meta.get("bpp", 16))
        channels = int(meta.get("channels", 1))
    elif path.with_suffix(path.suffix + ".meta.json").is_file():
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        return load_frame_from_raw(path, meta_path)
    else:
        raise ValueError("meta JSON required (.meta.json alongside raw)")
    return FrameResult(width=w, height=h, bpp=bpp, channels=channels, data=data, gps=gps)


def print_analysis_report(report: dict) -> None:
    print(f"Frame: {report['width']}x{report['height']} bpp={report['bpp']} ch={report['channels']}")
    if report.get("roi"):
        print(f"ROI: {report['roi']}")
    print(
        f"Pixels={report['pixels']} min={report['min']} max={report['max']} "
        f"mean={report['mean']:.2f} std={report['std']:.2f}"
    )
    if report.get("histogram"):
        print("Histogram:")
        for k in sorted(report["histogram"], key=lambda x: int(x.replace("bin", ""))):
            print(f"  {k}: {report['histogram'][k]}")
    if report.get("gps"):
        print("GPS header:")
        print(json.dumps(report["gps"], ensure_ascii=False, indent=2))
