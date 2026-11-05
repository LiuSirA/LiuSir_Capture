# -*- coding: utf-8 -*-
"""PCIe formal data: OCR of the clock region of each camera with a tilt correction (separate regions and angles for the early view,
groups G1-G4, and the late view after the camera position was adjusted, groups G5-G6), merged with the earlier recognition
candidates, then the usual sequence repair."""
import json, sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3\reports\_ocr_work")
import numpy as np, cv2
from PIL import Image
import run_pcie_ocr as P

R = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3"); OUTD = R / "reports" / "roi"; OUTD.mkdir(exist_ok=True)
ROI = {"early": {"M": (800, 340, 1560, 660), "S": (1400, 520, 2100, 780)},
       "late": {"M": (760, 300, 1460, 640), "S": (1500, 240, 2200, 620)}}      # for 2400-wide frames; scaled by width/2400
ANG = {"early": {"M": (-11, -9, -13), "S": (-10, -8, -12)}, "late": {"M": (-11, -9, -13), "S": (-15, -13, -17)}}
WORKERS = 14


def era_of(gid):
    return "late" if gid in ("G5", "G6") else "early"


def job_ocr(job):
    try:
        im = Image.open(job["path"]).convert("L"); k = im.width / 2400.0; a = np.array(im)
        era, cam = job["era"], job["cam"]
        x0, y0, x1, y1 = [int(v * k) for v in ROI[era][cam]]; crop = a[y0:y1, x0:x1]
        out = Counter()
        for ang in ANG[era][cam]:
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
    jobs, groups = [], []
    for gid, tag, binning, traffic, n_expect, bits, prior in P.discover_groups("16bit"):
        gdir = R / "groups" / tag; groups.append((gid, gdir, prior))
        for cam in ("M", "S"):
            for jp in sorted(gdir.glob(f"{cam}_seq*.jpg")):
                sq = int(jp.stem.split("seq")[1])
                jobs.append({"key": f"16bit|{gid}|{cam}|{sq}", "cam": cam, "era": era_of(gid.split("_")[0] if gid.startswith("G1_") else gid), "path": str(jp), "prior": prior})
    print("groups", len(groups), "jobs", len(jobs), flush=True)
    new = {}
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(job_ocr, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result(); new[r["key"]] = r["cand_counts"]
            if i % 100 == 0: print("progress", i, "/", len(jobs), flush=True)
    json.dump(new, open(OUTD / "rot_cache.json", "w"))
    old = {}
    for line in open(R / "reports" / "_ocr_work" / "frame_cache_v6.jsonl", encoding="utf-8"):
        try: o = json.loads(line)
        except Exception: continue
        if o.get("status") == "ok" and o.get("cand_counts"): old[o["key"]] = o
    merged = {}
    for key in set(old) | {k for k, v in new.items() if v}:
        c = Counter((old.get(key) or {}).get("cand_counts") or {}); c.update(new.get(key) or {})
        if c: merged[key] = {"key": key, "cand_counts": dict(c), "status": "ok"}
    summ = []
    for gid, gdir, prior in groups:
        ms = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("M_seq*.jpg")); ss = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("S_seq*.jpg"))
        common = sorted(set(ms) & set(ss))
        rep = P.repair_group(merged, "16bit", gid, prior, common) if len(common) >= 8 else None
        per = {}
        for sq in common:
            if rep is not None: a, b = rep["M"].get(sq), rep["S"].get(sq)
            else: a, b, _ = P.pick_pair((merged.get(f"16bit|{gid}|M|{sq}") or {}).get("cand_counts") or {}, (merged.get(f"16bit|{gid}|S|{sq}") or {}).get("cand_counts") or {})
            if a and b: per[sq] = [a, b, P.total_ms(a) - P.total_ms(b)]
        json.dump(per, open(OUTD / f"rot_pairs_{gid}.json", "w"))
        core = [v[2] for v in per.values() if abs(v[2]) <= 100]
        summ.append({"group": gid, "captured": len(common), "read": len(per), "core": len(core)})
        print(gid, len(common), len(per), len(core), flush=True)
    json.dump(summ, open(OUTD / "rot_summary.json", "w"))
    print("PCIE ROT OCR DONE", flush=True)


if __name__ == "__main__":
    main()
