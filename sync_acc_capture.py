# -*- coding: utf-8 -*-
"""同步精度测试（直连 SDK，绕开 GUI pool）。

流程：开两台相机(8bit/10ms/bin) → begin_live 双机 → 写主从同步寄存器(压住) →
释放(主 35=0,35=1) 主从同时出 burst 帧 → 交替抓取 N 帧(主/从各 N) → 存全幅 JPEG。

全幅保存（供自动定位 OCR）：sync_acc/groups/{tag}/{M,S}_f{idx:03d}.jpg
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "host"))
from qhyccd_highlevel import (  # noqa: E402
    QhyApi, scan_cameras, open_camera_by_name, close_camera,
    SoftCamera, CameraSettings, STREAM_LIVE,
)
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent / "host"
OUT = Path(__file__).resolve().parent / "sync_acc" / "groups"
OUT.mkdir(parents=True, exist_ok=True)
MODES = json.loads((ROOT / "fpga_soft_modes.json").read_text(encoding="utf-8"))["modes"]


def clean(b: bytes) -> str:
    i = b.find(b"\x00")
    return (b[:i] if i > 0 else b).decode("ascii")


def write_steps(ctx, key):
    for st in MODES[key]["steps"]:
        if st["cmd"] == "WRITE_FPGA":
            ctx.write_fpga(st["params"]["index"], st["params"]["value"])


def save_full(fr, tag, idx, gdir):
    """保存全幅 JPEG（8bit 灰度），供自动定位 OCR。"""
    import numpy as np
    W, H = fr.width, fr.height
    data = fr.data
    n = W * H
    if fr.bpp > 8:
        img = (np.frombuffer(data, dtype="<u2", count=n) >> 8).astype("uint8").reshape(H, W)
    else:
        img = np.frombuffer(data, dtype="uint8", count=n).reshape(H, W)
    p = gdir / f"{tag}_f{idx:03d}.jpg"
    Image.fromarray(img).save(p, quality=90)
    return p


def run_group(bin_xy, n_frames, tag, exp_us=10000):
    api = QhyApi(ROOT)
    ids = scan_cameras(api)
    if len(ids) < 2:
        print("NEED 2 cams", flush=True)
        return 1
    mA, mB = clean(ids[0]), clean(ids[1])
    bx, by = bin_xy
    cams = {}
    for tg, nm in (("M", mA), ("S", mB)):
        ctx = open_camera_by_name(api, ids, nm)
        cam = SoftCamera(ctx)
        cam.apply_settings(CameraSettings(
            stream_mode=STREAM_LIVE, read_mode=0, bits=8,
            exposure_us=exp_us, gain=50.0, bin_x=bx, bin_y=by,
        ))
        cam.begin_live()
        cams[tg] = (cam, ctx)
        print(f"{tg} live bin={bx}x{by} {nm}", flush=True)
    time.sleep(1.5)
    # 同步寄存器：主(M) / 从(S)
    write_steps(cams["M"][1], "主从同步_主机(Master)一键设置")
    time.sleep(0.5)
    write_steps(cams["S"][1], "主从同步_从机(Slave)一键设置")
    time.sleep(1.0)
    # 释放（仅主）
    t_rel = time.time()
    write_steps(cams["M"][1], "主从同步_主机触发开拍(释放IDLE)")
    print(f"RELEASE at {t_rel:.3f}", flush=True)
    # 交替抓取 N 帧（全幅 JPEG）
    gdir = OUT / tag
    gdir.mkdir(parents=True, exist_ok=True)
    mcam, sctx = cams["M"], cams["S"]
    saved_m = saved_s = 0
    for i in range(n_frames):
        try:
            fm = mcam[0].capture_live_frame(timeout_s=8.0)
            if fm is not None:
                save_full(fm, "M", i + 1, gdir)
                saved_m += 1
        except Exception as e:
            print(f"M f{i} err {e}", flush=True)
        try:
            fs = sctx[0].capture_live_frame(timeout_s=8.0)
            if fs is not None:
                save_full(fs, "S", i + 1, gdir)
                saved_s += 1
        except Exception as e:
            print(f"S f{i} err {e}", flush=True)
    # 退出同步
    for tg in ("M", "S"):
        write_steps(cams[tg][1], "主从同步_退出同步拍摄(恢复单机默认)")
    for tg in ("M", "S"):
        try:
            cams[tg][0].stop_live()
            close_camera(api, cams[tg][1])
        except Exception:
            pass
    print(f"GROUP {tag} done M={saved_m} S={saved_s}", flush=True)
    return 0


if __name__ == "__main__":
    binx = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    nfr = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    tag = sys.argv[3] if len(sys.argv) > 3 else f"b{binx}_n{nfr}"
    raise SystemExit(run_group((binx, binx), nfr, tag))
