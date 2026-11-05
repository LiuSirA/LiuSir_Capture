# -*- coding: utf-8 -*-
"""11/04 USB repeat tests (7 groups, 16 bit full frame, camera position adjusted): tilt-corrected clock-region OCR of all frames,
then the usual sequence repair. Saves the candidates and the repaired pairs per group."""
import json, sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3\reports\_ocr_work")
import numpy as np, cv2
from PIL import Image
import run_pcie_ocr as P

DATA = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261104_usb_session_test")
SHIFT_MS = -8 * 3600 * 1000 + 60 * 1000   # screen clock = UTC+8, folder time stamp = UTC-8; frames ~60 s after the stamp
ROI = {"M": (900, 480, 1700, 800), "S": (1450, 420, 2200, 720)}   # for 2400-wide frames; scaled by width/2400
ANG = {"M": (0, 3, 6), "S": (0, -2, 2)}
WORKERS = 14


def prior_of(tag):
    hh, mm, ss = int(tag[-6:-4]), int(tag[-4:-2]), int(tag[-2:])
    return ((hh * 3600 + mm * 60 + ss) * 1000 + SHIFT_MS) % 86400000


def job_ocr(job):
    try:
        im = Image.open(job["path"]).convert("L"); k = im.width / 2400.0; a = np.array(im)
        x0, y0, x1, y1 = [int(v * k) for v in ROI[job["cam"]]]; crop = a[y0:y1, x0:x1]
        out = Counter()
        for ang in ANG[job["cam"]]:
            M = cv2.getRotationMatrix2D((crop.shape[1] / 2, crop.shape[0] / 2), ang, 1.0)
            rot = cv2.warpAffine(crop, M, (crop.shape[1], crop.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            thr = float(np.percentile(rot, 99.0)); ys, xs = np.where(rot >= max(thr * 0.5, 6))
            if len(ys) < 50: continue
            t = rot[max(0, ys.min() - 8):ys.max() + 9, max(0, xs.min() - 12):xs.max() + 13]
            out.update(P.ocr_crop_candidates(t, job["prior"], deep=False))
        return {"key": job["key"], "cand_counts": dict(out)}
    except Exception as e:
        return {"key": job["key"], "cand_counts": {}, "err": str(e)}


def main():
    groups = sorted(p for p in (DATA / "groups").iterdir() if p.is_dir())
    jobs = []
    for g in groups:
        pr = prior_of(g.name)
        for cam in ("M", "S"):
            for jp in sorted(g.glob(f"{cam}_seq*.jpg")):
                sq = int(jp.stem.split("seq")[1])
                jobs.append({"key": f"{g.name}|{cam}|{sq}", "cam": cam, "path": str(jp), "prior": pr})
    print("groups", len(groups), "jobs", len(jobs), flush=True)
    cache = {}
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(job_ocr, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result(); cache[r["key"]] = r["cand_counts"]
            if i % 100 == 0: print("progress", i, "/", len(jobs), flush=True)
    json.dump(cache, open(DATA / "rot_cache.json", "w"))
    merged = {k: {"key": k, "cand_counts": v, "status": "ok"} for k, v in cache.items() if v}
    summ = []
    for g in groups:
        pr = prior_of(g.name)
        ms = sorted(int(p.stem.split("seq")[1]) for p in g.glob("M_seq*.jpg")); ss = sorted(int(p.stem.split("seq")[1]) for p in g.glob("S_seq*.jpg"))
        common = sorted(set(ms) & set(ss))
        mk = {f"usb|{g.name}|{c}|{s}": merged[f"{g.name}|{c}|{s}"] for c in "MS" for s in common if f"{g.name}|{c}|{s}" in merged}
        rep = P.repair_group(mk, "usb", g.name, pr, common) if len(common) >= 8 else None
        per = {}
        for sq in common:
            a, b = (rep["M"].get(sq), rep["S"].get(sq)) if rep is not None else (None, None)
            if a and b: per[sq] = [a, b, P.total_ms(a) - P.total_ms(b)]
        json.dump(per, open(DATA / f"rot_pairs_{g.name}.json", "w"))
        core = [v[2] for v in per.values() if abs(v[2]) <= 100]
        summ.append({"group": g.name, "captured": len(common), "read": len(per), "core": len(core)})
        print(g.name, len(common), len(per), len(core), flush=True)
    json.dump(summ, open(DATA / "rot_summary.json", "w"))
    print("REPEAT ROT OCR DONE", flush=True)


if __name__ == "__main__":
    main()
