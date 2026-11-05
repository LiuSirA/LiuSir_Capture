# -*- coding: utf-8 -*-
"""Fixed-ROI OCR for today's USB groups (cameras now static): per-camera clock crop, then the same repair pipeline."""
import json, sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3\reports\_ocr_work")
import numpy as np
from PIL import Image
import run_pcie_ocr as P

DATA = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261104_usb_session_test")
SHIFT_MS = -8 * 3600 * 1000 + 60 * 1000
WORKERS = 14
ROI = {"M": (1020, 1540, 560, 710), "S": (1570, 2090, 500, 630)}   # x0,x1,y0,y1 in 2400x1606 frame


def prior_of(tag):
    hh, mm, ss = int(tag[-6:-4]), int(tag[-4:-2]), int(tag[-2:])
    return ((hh * 3600 + mm * 60 + ss) * 1000 + SHIFT_MS) % 86400000


def roi_job(job):
    try:
        a = np.array(Image.open(job["path"]).convert("L"))
        x0, x1, y0, y1 = ROI[job["tag"]]
        crop = a[y0:y1, x0:x1]
        near = P.ocr_crop_candidates(crop, job["prior"], deep=False)
        if not near:
            near = P.ocr_crop_candidates(crop, job["prior"], deep=True)
        cnt = Counter(near)
        st = "ok" if cnt else "roi_reject"
        if not cnt:
            r = P.ocr_one_frame(job)
            return r
        return {"key": job["key"], "cand_counts": dict(cnt), "status": st, "tag": job["tag"], "seq": job["seq"]}
    except Exception as e:
        return {"key": job["key"], "cand_counts": {}, "status": "err:%s" % e}


def main():
    prefixes = sys.argv[1:]
    groups = sorted(p for p in (DATA / "groups").iterdir() if p.is_dir() and any(p.name.startswith(x) for x in prefixes))
    jobs, meta = [], {}
    for g in groups:
        pr = prior_of(g.name); meta[g.name] = pr
        for cam in ("M", "S"):
            for jp in sorted(g.glob(f"{cam}_seq*.jpg")):
                sq = int(jp.stem.split("seq")[1])
                jobs.append({"key": f"usb|{g.name}|{cam}|{sq}", "bit": "usb", "group": g.name, "folder": g.name,
                             "tag": cam, "seq": sq, "path": str(jp), "prior": pr})
    print("groups", len(groups), "jobs", len(jobs), flush=True)
    cache = {}
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(roi_job, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r.get("status") in ("ok",) and r.get("cand_counts"):
                cache[r["key"]] = r
            if i % 100 == 0:
                print("progress", i, "/", len(jobs), flush=True)
    out = []
    for g in groups:
        ms = sorted(int(p.stem.split("seq")[1]) for p in g.glob("M_seq*.jpg"))
        ss = sorted(int(p.stem.split("seq")[1]) for p in g.glob("S_seq*.jpg"))
        common = sorted(set(ms) & set(ss))
        nM = sum(1 for s in ms if f"usb|{g.name}|M|{s}" in cache)
        nS = sum(1 for s in ss if f"usb|{g.name}|S|{s}" in cache)
        rep = P.repair_group(cache, "usb", g.name, meta[g.name], common) if len(common) >= 8 else None
        d, per = [], {}
        for sq in common:
            if rep is not None:
                a, b = rep["M"].get(sq), rep["S"].get(sq)
                if a and b:
                    v = P.total_ms(a) - P.total_ms(b); d.append(v); per[sq] = [a, b, v]
        core = [v for v in d if abs(v) <= 100]
        arr = np.array(core, dtype=float)
        row = {"group": g.name, "n_common": len(common), "ocr_M": nM, "ocr_S": nS, "n_pairs": len(d), "n_core": len(core),
               "median": float(np.median(arr)) if len(arr) else None, "std": float(arr.std()) if len(arr) > 1 else None,
               "n_gt100": len(d) - len(core)}
        out.append(row)
        json.dump(per, open(DATA / f"roi_pairs_{g.name}.json", "w"))
        print(row, flush=True)
    (DATA / "roi_summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("ROI ANALYSIS DONE", flush=True)


if __name__ == "__main__":
    main()
