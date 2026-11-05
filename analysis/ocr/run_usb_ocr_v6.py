# -*- coding: utf-8 -*-
"""USB 20261103_usb dual-bit OCR v6.1 wash+dark — ProcessPool parallel + frame cache resume."""
from __future__ import annotations
import csv, json, os, re, statistics, time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytesseract

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

PARENT = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261103_usb")
OUT = PARENT / "reports" / "_ocr_work"
CACHE = OUT / "frame_cache_v6.jsonl"
CROPS = OUT / "crops"
BIT_PHASES = ("8bit", "16bit")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}$")
WORKERS = 8


def discover_groups(bit: str):
    rows = []
    for p in sorted((PARENT / bit / "groups").iterdir()):
        if not p.is_dir():
            continue
        m = re.match(r"^(G1_\d+|G[2-6])_b(\d)_t(\d+)_n(\d+)_bits(\d+)_(\d+)$", p.name)
        if not m:
            continue
        gid, binn, traffic, n_expect, bits, hhmmss = m.groups()
        prior = int(hhmmss[0:2]) * 3600000 + int(hhmmss[2:4]) * 60000 + int(hhmmss[4:6]) * 1000
        rows.append((gid, p.name, "1x1" if binn == "1" else "3x3", int(traffic), int(n_expect), int(bits), prior))
    return rows


def total_ms(text: str) -> int:
    h, m, rest = text.split(":")
    ss, mmm = rest.split(".")
    return int(h) * 3600000 + int(m) * 60000 + int(ss) * 1000 + int(mmm)


def valid_time(t: str) -> bool:
    if not TIME_RE.match(t):
        return False
    hh, mm, rest = t.split(":")
    ss, _ = rest.split(".")
    return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and 0 <= int(ss) <= 59


def repair_candidates(raw: str):
    raw = raw.strip().replace(" ", "").replace(",", ".").replace("\n", "")
    seeds = [raw] + [raw[:i] + raw[i + 1 :] for i in range(len(raw))]
    m = re.match(r"^(\d):(\d{2}):(\d{2})\.(\d{3})$", raw)
    if m:
        d, mm, ss, mmm = m.groups()
        for tens in "012":
            seeds.append(f"{tens}{d}:{mm}:{ss}.{mmm}")
    m = re.match(r"^:(\d{2}):(\d{2})\.(\d{3})$", raw)
    if m:
        mm, ss, mmm = m.groups()
        for hh in range(24):
            seeds.append(f"{hh:02d}:{mm}:{ss}.{mmm}")
    m = re.match(r"^(\d{2}:\d{2}:\d{2})\.(\d{1,2})$", raw)
    if m:
        ms_s = m.group(2)
        seeds.append(f"{m.group(1)}.{int(ms_s) * (10 ** (3 - len(ms_s))):03d}")
    m = re.match(r"^(\d{2})(\d{2})(\d{2})(\d{3})$", raw)
    if m:
        seeds.append(f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}")
    m = re.match(r"^(\d{1,2}):(\d{2})\.(\d{2})(\d{3})$", raw)
    if m:
        h, mm, ss, mmm = m.groups()
        seeds.append(f"{int(h):02d}:{mm}:{ss}.{mmm}")
        if len(h) == 1:
            for tens in "012":
                seeds.append(f"{tens}{h}:{mm}:{ss}.{mmm}")
    return [t for t in seeds if valid_time(t)]


def dist_prior(t, prior):
    d = abs(total_ms(t) - prior)
    return min(d, 86400000 - d)



def find_bbox_wash(a, delta=12):
    H, W = a.shape
    med = float(np.median(a))
    bright = a >= (med + delta)
    row_n = bright.sum(axis=1)
    thr = max(3, int(0.01 * W))
    ridx = np.where(row_n > thr)[0]
    if len(ridx) == 0:
        ridx = np.where(row_n > 0)[0]
        if len(ridx) == 0:
            return None
    groups = []
    s = p = int(ridx[0])
    for r in ridx[1:]:
        r = int(r)
        if r - p > 8:
            groups.append((s, p, int(row_n[s : p + 1].sum())))
            s = r
        p = r
    groups.append((s, p, int(row_n[s : p + 1].sum())))
    groups2 = [g for g in groups if (g[1] - g[0]) >= 8] or [(int(ridx[0]), int(ridx[-1]), int(row_n[ridx].sum()))]
    y0, y1, _ = max(groups2, key=lambda g: g[2])
    band = bright[y0 : y1 + 1]
    colhas = band.sum(axis=0) > 0
    runs, i = [], 0
    while i < W:
        if colhas[i]:
            j = i
            while j < W and colhas[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    if not runs:
        return None
    gap = max(10, int(0.05 * W))
    merged = [list(runs[0])]
    for r in runs[1:]:
        if r[0] - merged[-1][1] <= gap:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    x0, x1 = max(merged, key=lambda r: r[1] - r[0])
    return max(0, y0 - 6), min(H - 1, y1 + 6), max(0, x0 - 10), min(W - 1, x1 + 10)


def find_bbox_dark(a, thr=30):
    H, W = a.shape
    light = a >= thr
    rows = np.where(light.sum(axis=1) > W * 0.005)[0]
    cols = np.where(light.sum(axis=0) > H * 0.005)[0]
    if not (len(rows) and len(cols)):
        return None
    return max(0, int(rows[0]) - 5), min(H - 1, int(rows[-1]) + 5), max(0, int(cols[0]) - 10), min(W - 1, int(cols[-1]) + 10)


def deskew_crop(a_full: np.ndarray):
    H, W = a_full.shape
    upper = a_full[: int(H * 0.55)].copy()
    x0cut = int(W * 0.12)
    work = upper[:, x0cut:]
    best = None
    for pct in (99.7, 99.5, 99.2, 99.0, 98.5, 98.0, 97.5):
        thr = float(np.percentile(work, pct))
        if thr >= 250:
            continue
        thr = max(thr, 28.0)
        mask = (work >= thr).astype(np.uint8) * 255
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), 1)
        ys, xs = np.where(mask > 0)
        if len(ys) < 25:
            continue
        pts = np.column_stack([xs.astype(np.float32), ys.astype(np.float32)])
        rect = cv2.minAreaRect(pts)
        (cx, cy), (w, h), angle = rect
        if w < h:
            w, h = h, w
            angle = angle + 90
        aspect = w / max(h, 1)
        if aspect < 2.5 or w < 70:
            continue
        a2 = ((angle + 45) % 90) - 45
        score = aspect * np.sqrt(len(ys)) / (1 + abs(a2))
        if best is None or score > best[0]:
            best = (score, cx + x0cut, cy, w, h, angle)
    if best is None:
        return None
    _, cx, cy, w, h, angle = best
    M = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    rotated = cv2.warpAffine(
        upper, M, (upper.shape[1], upper.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    ww, hh = w * 1.25, h * 1.6
    x0 = max(0, int(cx - ww / 2)); x1 = min(rotated.shape[1] - 1, int(cx + ww / 2))
    y0 = max(0, int(cy - hh / 2)); y1 = min(rotated.shape[0] - 1, int(cy + hh / 2))
    crop = rotated[y0 : y1 + 1, x0 : x1 + 1]
    return crop if crop.size >= 100 else None


def ocr_crop_candidates(crop: np.ndarray, prior: int, deep: bool = False):
    cands = []
    # deep kept lean on purpose (speed); avoid 60s/frame blowups
    stretches = ((2, 98), (5, 95), (1, 99)) if deep else ((2, 98), (5, 95))
    scales = (3,) 
    psms = (7, 6, 13) if deep else (7, 6)
    for lo, hi in stretches:
        l, h = np.percentile(crop, lo), np.percentile(crop, hi)
        st = np.clip((crop.astype(float) - l) / max(1e-3, h - l) * 255, 0, 255).astype(np.uint8)
        for ink_bright in (True, False):
            arr = ((st > 128) * 255).astype(np.uint8) if ink_bright else ((st < 128) * 255).astype(np.uint8)
            for src in (arr, 255 - arr):
                th, tw = src.shape
                if th < 5 or tw < 20:
                    continue
                for scale in scales:
                    im = Image.fromarray(src).resize((tw * scale, th * scale), Image.LANCZOS)
                    for psm in psms:
                        raw = (
                            pytesseract.image_to_string(
                                im, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789.:"
                            )
                            .strip()
                            .replace(" ", "")
                            .replace(",", ".")
                            .replace("\n", "")
                        )
                        cands.extend(repair_candidates(raw))
    near = [t for t in cands if dist_prior(t, prior) <= 120000]
    return near


def get_crop_for_mode(a: np.ndarray):
    H, W = a.shape
    upper = a[: H // 2]
    med_u = float(np.median(upper))
    med = float(np.median(a))
    # classify by UPPER half — bottom glare often inflates full-frame median
    mode = "wash" if med_u > 80 else "dark"
    crops = []
    # always prefer deskew first (works for tilted monitor clock)
    dc = deskew_crop(a)
    if dc is not None:
        crops.append(dc)
    if mode == "dark":
        bbox = find_bbox_dark(a, 30) or find_bbox_dark(a, 20)
        if bbox:
            y0, y1, x0, x1 = bbox
            crops.append(a[y0 : y1 + 1, x0 : x1 + 1])
    else:
        for d in (12, 8):
            bbox = find_bbox_wash(a, d)
            if bbox:
                y0, y1, x0, x1 = bbox
                crops.append(a[y0 : y1 + 1, x0 : x1 + 1])
    crops.append(a[int(H * 0.05) : int(H * 0.45), int(W * 0.15) : int(W * 0.9)])
    return crops, mode



def wash_ur_fast_ocr(a: np.ndarray, prior: int):
    """Upper-right ROI + small positive rotations for 3x3 wash frames."""
    H, W = a.shape
    ur = a[int(H * 0.02) : int(H * 0.42), int(W * 0.32) : int(W * 0.98)]
    cands = []
    for ang in (12, 15, 10, 8, 18, 20, -8, -10):
        M = cv2.getRotationMatrix2D((ur.shape[1] / 2, ur.shape[0] / 2), ang, 1.0)
        rot = cv2.warpAffine(ur, M, (ur.shape[1], ur.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        med = float(np.median(rot))
        for delta in (12, 18, 25):
            mask = (rot >= med + delta).astype(np.uint8) * 255
            if mask.sum() < 40:
                continue
            ys, xs = np.where(mask > 0)
            crop = rot[max(0, ys.min() - 4) : ys.max() + 5, max(0, xs.min() - 8) : xs.max() + 9]
            if crop.size < 100:
                continue
            for lo, hi in ((5, 95), (2, 98)):
                l, h = np.percentile(crop, lo), np.percentile(crop, hi)
                st = np.clip((crop.astype(float) - l) / max(1e-3, h - l) * 255, 0, 255).astype(np.uint8)
                for src in (((st > 128) * 255).astype(np.uint8), ((st < 128) * 255).astype(np.uint8)):
                    th, tw = src.shape
                    if th < 5 or tw < 20:
                        continue
                    im = Image.fromarray(src).resize((tw * 3, th * 3), Image.LANCZOS)
                    raw = (
                        pytesseract.image_to_string(im, config="--psm 7 -c tessedit_char_whitelist=0123456789.:")
                        .strip()
                        .replace(" ", "")
                        .replace(",", ".")
                        .replace("\n", "")
                    )
                    near = [t for t in repair_candidates(raw) if dist_prior(t, prior) <= 120000]
                    if near:
                        cands.extend(near)
                        # early success
                        if len(cands) >= 2:
                            return cands
    return cands


def ocr_one_frame(job: dict) -> dict:
    """Top-level worker: must be picklable."""
    path = job["path"]
    prior = job["prior"]
    t0 = time.time()
    try:
        a = np.array(Image.open(path).convert("L"))
        crops, mode = get_crop_for_mode(a)
        near = []
        best_crop = None
        # wash / high-median: try UR rotate path first (fast & accurate for 3x3)
        if mode == "wash" or float(np.median(a)) > 100:
            near = wash_ur_fast_ocr(a, prior)
        if not near:
            for crop in crops:
                if crop is None or getattr(crop, "size", 0) < 100:
                    continue
                if best_crop is None:
                    best_crop = crop
                near = ocr_crop_candidates(crop, prior, deep=False)
                if near:
                    break
        if not near and best_crop is not None:
            near = ocr_crop_candidates(best_crop, prior, deep=True)
        if not near and mode != "wash":
            near = wash_ur_fast_ocr(a, prior)
        cnt = Counter(near)
        return {
            "key": job["key"],
            "bit": job["bit"],
            "group": job["group"],
            "folder": job["folder"],
            "tag": job["tag"],
            "seq": job["seq"],
            "path": path,
            "cand_counts": dict(cnt),
            "n_unique": len(cnt),
            "top": cnt.most_common(1)[0][0] if cnt else "",
            "status": "ok" if cnt else "ocr_reject",
            "elapsed": time.time() - t0,
            "mode": mode,
            "error": "",
        }
    except Exception as e:
        return {
            "key": job["key"],
            "bit": job["bit"],
            "group": job["group"],
            "folder": job["folder"],
            "tag": job["tag"],
            "seq": job["seq"],
            "path": path,
            "cand_counts": {},
            "n_unique": 0,
            "top": "",
            "status": f"err:{e}",
            "elapsed": time.time() - t0,
            "mode": "?",
            "error": str(e),
        }


def pick_pair(m_counts: dict, s_counts: dict):
    if not m_counts or not s_counts:
        return None, None, None
    best = None
    for m, mv in m_counts.items():
        for s, sv in s_counts.items():
            d = total_ms(m) - total_ms(s)
            score = -abs(d) * 10 + mv + sv
            if abs(d) <= 100:
                score += 50
            if abs(d) <= 50:
                score += 20
            if best is None or score > best[0]:
                best = (score, m, s, d)
    return best[1], best[2], best[3]


def load_cache():
    cache = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            if o.get("status") == "ok" and o.get("cand_counts"):
                cache[o["key"]] = o
    return cache


def append_cache(row: dict):
    with CACHE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def list_jobs(cache: dict):
    jobs = []
    for bit in BIT_PHASES:
        for gid, folder, binning, traffic, n_expect, bits, prior in discover_groups(bit):
            gdir = PARENT / bit / "groups" / folder
            for tag in ("M", "S"):
                for jp in sorted(gdir.glob(f"{tag}_seq*.jpg")):
                    seq = int(jp.stem.split("seq")[1])
                    key = f"{bit}|{gid}|{tag}|{seq}"
                    if key in cache:
                        continue
                    jobs.append({
                        "key": key,
                        "bit": bit,
                        "group": gid,
                        "folder": folder,
                        "tag": tag,
                        "seq": seq,
                        "path": str(jp),
                        "prior": prior,
                        "binning": binning,
                        "traffic": traffic,
                        "n_expect": n_expect,
                        "bits": bits,
                    })
    return jobs


def stats_of(vals):
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": statistics.mean(vals),
        "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        "median": statistics.median(vals),
    }


def med_str(st):
    return f"{st['median']:.1f}" if st else "N/A"


def fmt_stats(s):
    if not s:
        return "N/A"
    return f"n={s['n']} median={s['median']:.1f} mean={s['mean']:.1f} std={s['std']:.1f} min={s['min']:.1f} max={s['max']:.1f}"


def build_summaries(cache: dict):
    # meta from discover
    all_frames_rows = []
    all_pair_rows = []
    group_summaries = []
    for bit in BIT_PHASES:
        bit_groups = discover_groups(bit)
        bit_summaries = []
        for gid, folder, binning, traffic, n_expect, bits, prior in bit_groups:
            gdir = PARENT / bit / "groups" / folder
            m_seqs = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("M_seq*.jpg"))
            s_seqs = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("S_seq*.jpg"))
            common = sorted(set(m_seqs) & set(s_seqs))
            only_m = sorted(set(m_seqs) - set(s_seqs))
            only_s = sorted(set(s_seqs) - set(m_seqs))
            for tag, seqs in (("M", m_seqs), ("S", s_seqs)):
                for seq in seqs:
                    key = f"{bit}|{gid}|{tag}|{seq}"
                    o = cache.get(key)
                    all_frames_rows.append({
                        "bit": bit, "group": gid, "folder": folder, "tag": tag, "seq": seq,
                        "utc_top": (o or {}).get("top", ""),
                        "n_cands": (o or {}).get("n_unique", 0),
                        "status": (o or {}).get("status", "missing"),
                        "path": str(gdir / f"{tag}_seq{seq:04d}.jpg"),
                    })
            accepted = []
            for seq in common:
                mk = f"{bit}|{gid}|M|{seq}"; sk = f"{bit}|{gid}|S|{seq}"
                mo, so = cache.get(mk), cache.get(sk)
                m_counts = (mo or {}).get("cand_counts") or {}
                s_counts = (so or {}).get("cand_counts") or {}
                mt, st, d = pick_pair(m_counts, s_counts)
                row = {
                    "bit": bit, "group": gid, "folder": folder, "seq": seq,
                    "m_utc": mt or "", "s_utc": st or "",
                    "m_votes": m_counts.get(mt, 0) if mt else 0,
                    "s_votes": s_counts.get(st, 0) if st else 0,
                    "delta_ms": d if d is not None else "",
                    "accepted": d is not None,
                }
                all_pair_rows.append(row)
                if row["accepted"]:
                    accepted.append(row)
            d_all = [r["delta_ms"] for r in accepted]
            d_core = [v for v in d_all if abs(v) <= 100]
            d_tight = [v for v in d_all if abs(v) <= 50]
            contamination = []
            if accepted:
                med_m = statistics.median([total_ms(r["m_utc"]) for r in accepted])
                for r in accepted:
                    mm, sm = total_ms(r["m_utc"]), total_ms(r["s_utc"])
                    flags = []
                    if abs(mm - med_m) > 2000:
                        flags.append("M_utc_far_from_group_median")
                    if abs(sm - med_m) > 2000:
                        flags.append("S_utc_far_from_group_median")
                    if abs(r["delta_ms"]) > 100:
                        flags.append("abs_delta_gt_100")
                    if dist_prior(r["m_utc"], prior) > 120000:
                        flags.append("M_far_from_folder_time")
                    if flags:
                        contamination.append({"seq": r["seq"], "delta_ms": r["delta_ms"], "m_utc": r["m_utc"], "s_utc": r["s_utc"], "flags": flags})
            summary = {
                "bit": bit, "group": gid, "folder": folder, "bin": binning, "traffic": traffic,
                "n_expect": n_expect, "bits": bits, "n_M": len(m_seqs), "n_S": len(s_seqs),
                "n_common_seq": len(common), "only_M": only_m, "only_S": only_s,
                "ocr_ok_M_on_common": sum(1 for s in common if (cache.get(f"{bit}|{gid}|M|{s}") or {}).get("cand_counts")),
                "ocr_ok_S_on_common": sum(1 for s in common if (cache.get(f"{bit}|{gid}|S|{s}") or {}).get("cand_counts")),
                "ocr_pair_success": len(accepted),
                "ocr_pair_rate": (len(accepted) / len(common)) if common else None,
                "stats_all": stats_of(d_all), "stats_core_abs100": stats_of(d_core), "stats_tight_abs50": stats_of(d_tight),
                "outliers_abs_gt100": [{"seq": r["seq"], "delta_ms": r["delta_ms"], "m_utc": r["m_utc"], "s_utc": r["s_utc"]} for r in accepted if abs(r["delta_ms"]) > 100],
                "contamination_flags": contamination,
                "diagnostic_note": (
                    f"NONCOMPLIANT: GPS seq mismatch M={m_seqs} S={s_seqs}"
                    if len(common) == 0 and m_seqs and s_seqs else ""
                ),
            }
            bit_summaries.append(summary)
            group_summaries.append(summary)
            print(f"SUM {bit}/{gid} common={len(common)} pairs={len(accepted)} core[{fmt_stats(summary['stats_core_abs100'])}]", flush=True)

        g1_subs = [s for s in bit_summaries if s["group"].startswith("G1_")]
        if g1_subs:
            d_all, d_core, d_tight, outliers, contam, only_m, only_s = [], [], [], [], [], [], []
            for s in g1_subs:
                only_m.extend([f"{s['group']}:{x}" for x in s["only_M"]])
                only_s.extend([f"{s['group']}:{x}" for x in s["only_S"]])
                outliers.extend([{**o, "sub": s["group"]} for o in s["outliers_abs_gt100"]])
                contam.extend([{**c, "sub": s["group"]} for c in s["contamination_flags"]])
            for r in all_pair_rows:
                if r["bit"] == bit and str(r["group"]).startswith("G1_") and r["accepted"]:
                    d_all.append(r["delta_ms"])
                    if abs(r["delta_ms"]) <= 100:
                        d_core.append(r["delta_ms"])
                    if abs(r["delta_ms"]) <= 50:
                        d_tight.append(r["delta_ms"])
            agg = {
                "bit": bit, "group": "G1_agg", "folder": "G1_01..G1_10", "bin": "1x1", "traffic": 55,
                "n_expect": 10, "bits": 8 if bit == "8bit" else 16,
                "n_M": sum(s["n_M"] for s in g1_subs), "n_S": sum(s["n_S"] for s in g1_subs),
                "n_common_seq": sum(s["n_common_seq"] for s in g1_subs), "only_M": only_m, "only_S": only_s,
                "ocr_ok_M_on_common": sum(s["ocr_ok_M_on_common"] for s in g1_subs),
                "ocr_ok_S_on_common": sum(s["ocr_ok_S_on_common"] for s in g1_subs),
                "ocr_pair_success": sum(s["ocr_pair_success"] for s in g1_subs), "ocr_pair_rate": None,
                "stats_all": stats_of(d_all), "stats_core_abs100": stats_of(d_core), "stats_tight_abs50": stats_of(d_tight),
                "outliers_abs_gt100": outliers, "contamination_flags": contam,
                "diagnostic_note": f"aggregated from {[s['group'] for s in g1_subs]}",
            }
            group_summaries.append(agg)

        bit_out = OUT / bit
        bit_out.mkdir(parents=True, exist_ok=True)
        frames_bit = [r for r in all_frames_rows if r["bit"] == bit]
        pairs_bit = [r for r in all_pair_rows if r["bit"] == bit]
        if frames_bit:
            with (bit_out / "frames.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(frames_bit[0].keys())); w.writeheader(); w.writerows(frames_bit)
        if pairs_bit:
            with (bit_out / "pairs.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(pairs_bit[0].keys())); w.writeheader(); w.writerows(pairs_bit)
        (bit_out / f"summary_{bit}.json").write_text(json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"), "bit": bit,
            "delta_definition": "delta_ms = UTC_M - UTC_S",
            "pairing": "GPS seq 1:1 + min-|delta| among OCR cands",
            "groups": [s for s in group_summaries if s["bit"] == bit],
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return group_summaries, all_pair_rows


def write_report(all_summaries, all_pairs, t0, workers):
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = PARENT / "reports" / f"sync_acc_ocr_8bit16bit_{stamp}.md"
    by = {(s["bit"], s["group"]): s for s in all_summaries}
    keys = ["G1_agg", "G2", "G3", "G4", "G5", "G6"]
    labels = {"G1_agg":"1x1 × n=1×10 (G1)","G2":"1x1 × n=10","G3":"1x1 × n=100","G4":"3x3 × n=1","G5":"3x3 × n=10","G6":"3x3 × n=100"}
    lines = [
        "# USB 双位深重测 同步精度 OCR 分析报告", "",
        "> 对照笔记 **02787256《相机同步拍摄精度测试计划》**",
        f"> 数据根：`{PARENT}`",
        f"> 分析时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）",
        "> 方法：**GPS abs seq 1:1** → 去斜时钟 ROI OCR → 候选中选最小\\|Δ\\| → **Δms = UTC_主 − UTC_从**",
        f"> OCR v6.1 wash+dark：ProcessPoolExecutor workers={workers}；帧级缓存 `{CACHE.name}` 可断点续跑。",
        "> 重测加固：drain + 同 abs-seq 新换旧；门禁 1x1=55 / 3x3=30。",
        "", "---", "", "## 0. 结论摘要", "",
    ]
    for bit in BIT_PHASES:
        lines += [f"### {bit}", "",
                  "| 组 | bin | traffic | n期望 | 共同seq | OCR成功对 | median全量 | median\\|Δ\\|≤100 | median\\|Δ\\|≤50 |",
                  "| -- | --- | ------- | ----- | ------- | --------- | ---------- | ---------------- | --------------- |"]
        for s in all_summaries:
            if s["bit"] != bit or (s["group"].startswith("G1_") and s["group"] != "G1_agg"):
                continue
            lines.append(
                f"| {s['group']} | {s['bin']} | {s['traffic']} | {s['n_expect']} | {s['n_common_seq']} | {s['ocr_pair_success']} | "
                f"{med_str(s.get('stats_all'))} | {med_str(s.get('stats_core_abs100'))} | {med_str(s.get('stats_tight_abs50'))} |"
            )
        lines.append("")
    lines += ["### 8bit vs 16bit 核心对比（median core\\|Δ\\|≤100）", "",
              "| 模式 | 8bit median | 8bit std | 8bit n | 16bit median | 16bit std | 16bit n |",
              "| ---- | ----------- | -------- | ------ | ------------ | --------- | ------- |"]
    for k in keys:
        a, b = by.get(("8bit", k)), by.get(("16bit", k))
        def cells(s):
            st = s.get("stats_core_abs100") if s else None
            return (f"{st['median']:.1f}", f"{st['std']:.1f}", str(st["n"])) if st else ("N/A", "N/A", "0")
        a1, a2, a3 = cells(a); b1, b2, b3 = cells(b)
        lines.append(f"| {labels[k]} | {a1} | {a2} | {a3} | {b1} | {b2} | {b3} |")
    lines += ["", "### 与 20261102 OCR 终版对比", "",
              "| 模式 | 20261102 core median | 本批 8bit | 本批 16bit | 备注 |",
              "| ---- | -------------------- | --------- | ---------- | ---- |"]
    hist = {"G1_agg":("-17.0","历史单次 G1"),"G2":("-17.0",""),"G3":("-17.0",""),"G4":("N/A (seq错位)","主26/从10"),"G5":("-18.5",""),"G6":("-16.0","")}
    for k in keys:
        a, b = by.get(("8bit", k)), by.get(("16bit", k)); h, note = hist[k]
        lines.append(f"| {k} | {h} | {med_str(a.get('stats_core_abs100') if a else None)} | {med_str(b.get('stats_core_abs100') if b else None)} | {note} |")
    lines += ["", "### 丢配 / 污染概览", "",
              "| bit | 组 | 共同seq | onlyM | onlyS | \\|Δ\\|>100 | 疑似污染旗标数 | 诊断 |",
              "| --- | -- | ------- | ----- | ----- | --------- | -------------- | ---- |"]
    for s in all_summaries:
        if s["group"].startswith("G1_") and s["group"] != "G1_agg":
            continue
        lines.append(
            f"| {s['bit']} | {s['group']} | {s['n_common_seq']} | {s['only_M'] or '[]'} | {s['only_S'] or '[]'} | "
            f"{len(s.get('outliers_abs_gt100') or [])} | {len(s.get('contamination_flags') or [])} | {s.get('diagnostic_note') or '-'} |"
        )
    lines += ["", "**Δms** = t_M − t_S。核心以 median\\|Δ\\|≤100 为准。", "", "---", "",
              "## 1. OCR 管线", "",
              f"- ProcessPool workers={workers}；去斜 ROI + Tesseract + prior±120s + 主从候选最小\\|Δ\\|配对",
              f"- 帧缓存：`{CACHE}`", "", "---", "", "## 2. 分组明细", ""]
    for bit in BIT_PHASES:
        lines += [f"## 2.{'1' if bit=='8bit' else '2'} {bit}", ""]
        for s in all_summaries:
            if s["bit"] != bit:
                continue
            lines += [f"### {bit} / {s['group']} — `{s['folder']}`（{s['bin']}, t={s['traffic']}, n={s['n_expect']}）", "",
                      f"- 落盘：M={s['n_M']} / S={s['n_S']}；共同 GPS seq：**{s['n_common_seq']}**",
                      f"- OCR：主 {s['ocr_ok_M_on_common']}/{s['n_common_seq']}，从 {s['ocr_ok_S_on_common']}/{s['n_common_seq']}；成对 **{s['ocr_pair_success']}**"]
            if s.get("diagnostic_note"):
                lines.append(f"- ⚠ {s['diagnostic_note']}")
            if s["only_M"]:
                lines.append(f"- 仅主机 seq：{s['only_M']}")
            if s["only_S"]:
                lines.append(f"- 仅从机 seq：{s['only_S']}")
            lines += ["", "| 集合 | n | min | max | mean | std | median |", "| ---- | - | --- | --- | ---- | --- | ------ |"]
            for title, key in (("全量接受对","stats_all"), ("\\|Δ\\|≤100 核心","stats_core_abs100"), ("\\|Δ\\|≤50 紧簇","stats_tight_abs50")):
                st = s.get(key)
                if st:
                    lines.append(f"| {title} | {st['n']} | {st['min']:.1f} | {st['max']:.1f} | {st['mean']:.1f} | {st['std']:.1f} | **{st['median']:.1f}** |")
                else:
                    lines.append(f"| {title} | 0 | — | — | — | — | — |")
            if s.get("outliers_abs_gt100"):
                lines += ["", "野值（\\|Δ\\| > 100）：", "", "| seq | UTC_主 | UTC_从 | Δms |", "| --- | ------ | ------ | --- |"]
                for o in s["outliers_abs_gt100"][:40]:
                    seq = f"{o['sub']}:{o['seq']}" if "sub" in o else o["seq"]
                    lines.append(f"| {seq} | {o['m_utc']} | {o['s_utc']} | {o['delta_ms']} |")
            gpairs = [r for r in all_pairs if r["bit"] == bit and r["group"] == s["group"] and r["accepted"]]
            if not gpairs and s["group"] == "G1_agg":
                gpairs = [r for r in all_pairs if r["bit"] == bit and str(r["group"]).startswith("G1_") and r["accepted"]]
            if gpairs:
                lines += ["", "成对样例（最多 12 行）：", "", "| group | seq | UTC_主 | UTC_从 | Δms |", "| ----- | --- | ------ | ------ | --- |"]
                for r in gpairs[:12]:
                    lines.append(f"| {r['group']} | {r['seq']} | {r['m_utc']} | {r['s_utc']} | {r['delta_ms']} |")
                if len(gpairs) > 12:
                    lines.append(f"| … | （其余 {len(gpairs)-12} 对见 pairs.csv） | | | |")
            lines.append("")
    lines += ["---", "", "## 3. 解读要点", "",
              "- GPS seq 落盘复核；G4 共同 seq>0 则相对 20261102 错位有改善。",
              "- 核心结论以 **median\\|Δ\\|≤100** 为准；与历史 −16～−18ms 同量级即认为可用。", "",
              "---", "", "## 4. 产出文件", "",
              "| 文件 | 说明 |", "| ---- | ---- |",
              f"| `{report_path}` | 本报告 |",
              f"| `{OUT/'8bit'/'summary_8bit.json'}` | 8bit 摘要 |",
              f"| `{OUT/'16bit'/'summary_16bit.json'}` | 16bit 摘要 |",
              f"| `{CACHE}` | 帧级 OCR 缓存 |", "",
              f"*分析总耗时约 {time.time()-t0:.0f}s（workers={workers}）。*", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    (OUT / "latest_report_path.txt").write_text(str(report_path), encoding="utf-8")
    print("REPORT", report_path, flush=True)
    return report_path


def main():
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    CROPS.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    print(f"CACHE loaded {len(cache)} frames; workers={WORKERS}", flush=True)
    print("SPEED_MODE lean-ocr", flush=True)
    jobs = list_jobs(cache)
    print(f"JOBS remaining {len(jobs)}", flush=True)

    if jobs:
        done = 0
        with ProcessPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(ocr_one_frame, job): job for job in jobs}
            for fut in as_completed(futs):
                row = fut.result()
                # store compact
                store = {
                    "key": row["key"], "bit": row["bit"], "group": row["group"], "folder": row["folder"],
                    "tag": row["tag"], "seq": row["seq"], "path": row["path"],
                    "cand_counts": row["cand_counts"], "n_unique": row["n_unique"],
                    "top": row["top"], "status": row["status"], "elapsed": row["elapsed"], "error": row.get("error", ""),
                }
                append_cache(store)
                if row["status"] == "ok":
                    cache[row["key"]] = store
                done += 1
                if done % 10 == 0 or done == len(jobs):
                    print(f"PROGRESS {done}/{len(jobs)} last={row['key']} top={row['top']} {row['elapsed']:.1f}s status={row['status']}", flush=True)

    # reload cache fully
    cache = load_cache()
    print(f"CACHE final {len(cache)}", flush=True)
    summaries, pairs = build_summaries(cache)
    write_report(summaries, pairs, t0, WORKERS)
    print("DONE_ALL", flush=True)


if __name__ == "__main__":
    main()
