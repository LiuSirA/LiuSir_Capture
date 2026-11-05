# -*- coding: utf-8 -*-
"""USB 11/03 data: OCR of the clock region with a fixed tilt correction per camera (master text tilts ~15 deg, slave ~-8 deg),
merged with the earlier OCR candidates, then the usual sequence repair. Saves the new candidates for reuse."""
import json, sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, r"J:\Mywork\Project_LH\sync_acc_runs\20261103_pcie16_r3\reports\_ocr_work")
sys.path.insert(0, r"J:\Mywork\Project_LH\sync_acc_runs\20261103_usb\reports\_ocr_work")
import numpy as np, cv2
from PIL import Image
import run_pcie_ocr as P
import run_usb_ocr_v6 as U

R = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261103_usb"); OUTD = R / "reports" / "roi"
ROI = {"M": (1060, 90, 1860, 470), "S": (1340, 400, 2060, 680)}
ANG = {"M": (15, 13, 17), "S": (-8, -6, -10)}
WORKERS = 14


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
    jobs, groups = [], []
    for bit in ("8bit", "16bit"):
        for gid, folder, binning, traffic, n_expect, bits, prior in U.discover_groups(bit):
            gdir = R / bit / "groups" / folder; groups.append((bit, gid, gdir, prior + 30000))
            for cam in ("M", "S"):
                for jp in sorted(gdir.glob(f"{cam}_seq*.jpg")):
                    sq = int(jp.stem.split("seq")[1])
                    jobs.append({"key": f"{bit}|{gid}|{cam}|{sq}", "cam": cam, "path": str(jp), "prior": prior + 30000})
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
    for bit, gid, gdir, prior in groups:
        ms = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("M_seq*.jpg")); ss = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("S_seq*.jpg"))
        common = sorted(set(ms) & set(ss))
        rep = P.repair_group(merged, bit, gid, prior, common) if len(common) >= 8 else None
        per = {}
        for sq in common:
            if rep is not None: a, b = rep["M"].get(sq), rep["S"].get(sq)
            else: a, b, _ = P.pick_pair((merged.get(f"{bit}|{gid}|M|{sq}") or {}).get("cand_counts") or {}, (merged.get(f"{bit}|{gid}|S|{sq}") or {}).get("cand_counts") or {})
            if a and b: per[sq] = [a, b, P.total_ms(a) - P.total_ms(b)]
        json.dump(per, open(OUTD / f"rot_pairs_{bit}_{gid}.json", "w"))
        core = [v[2] for v in per.values() if abs(v[2]) <= 100]
        summ.append({"bit": bit, "group": gid, "captured": len(common), "read": len(per), "core": len(core)})
        print(bit, gid, len(common), len(per), len(core), flush=True)
    json.dump(summ, open(OUTD / "rot_summary.json", "w"))
    print("USB ROT OCR DONE", flush=True)


if __name__ == "__main__":
    main()
