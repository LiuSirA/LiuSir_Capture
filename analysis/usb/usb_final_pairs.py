# -*- coding: utf-8 -*-
"""Final USB pairs: fixed-region OCR readings, with gross misreads and unread frames replaced by the camera's own time line.

Per group (>= 8 frames) and per camera a straight line t = a + b*seq is fitted to the readings that agree with each other
(robust slope, then iterative rejection). A reading that is more than TOL ms away from the line, or missing, is replaced by the
line value and flagged 'est'. Single-frame groups (G1, G4) have no time line: their readings are used as read and checked by eye."""
import csv, json, collections, statistics as st
from pathlib import Path
import numpy as np
R = Path("J:/Mywork/Project_LH/sync_acc_runs/20261103_usb"); ROI = R / "reports" / "roi"
OLD = Path("J:/Mywork/Project_LH/sync_acc_runs/20261103_pcie16_r3/reports/final_pairs_all_modes.csv")
OUT = Path(__file__).parent; TOL = 40.0
def tms(s):
    h, m, r = s.split(":"); a, b = r.split("."); return ((int(h) * 60 + int(m)) * 60 + int(a)) * 1000 + int(b)
def fmt(ms):
    ms = int(round(ms)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000); return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
def line(seq, t):
    seq = np.asarray(seq, float); t = np.asarray(t, float)
    sl = []
    for i in range(len(seq)):
        for j in range(i + 5, len(seq), 3):
            sl.append((t[j] - t[i]) / (seq[j] - seq[i]))
    if not sl: return None
    b = float(np.median(sl)); a = float(np.median(t - b * seq)); ok = np.ones(len(seq), bool)
    for _ in range(8):
        if ok.sum() < 5: break
        b, a = np.polyfit(seq[ok], t[ok], 1)
        ok = abs(t - (a + b * seq)) < TOL
    return a, b, ok

def joint_lines(obs, seqs):
    """Short groups (<30 frames): both cameras share one frame period (verified on the 100-frame groups); one slope, one intercept per camera."""
    data = {}
    for ci, cam in enumerate("MS"):
        ss = [s_ for s_ in seqs if s_ in obs]; data[cam] = (np.array(ss, float), np.array([obs[s_][ci] for s_ in ss], float))
    sl = []
    for cam in "MS":
        q, t = data[cam]
        for i in range(len(q)):
            for j in range(i + 3, len(q)):
                sl.append((t[j] - t[i]) / (q[j] - q[i]))
    if len(sl) < 3: return None
    b = float(np.median(sl)); a = {cam: float(np.median(data[cam][1] - b * data[cam][0])) for cam in "MS"}
    ok = {cam: np.ones(len(data[cam][0]), bool) for cam in "MS"}
    for _ in range(8):
        for cam in "MS": ok[cam] = abs(data[cam][1] - (a[cam] + b * data[cam][0])) < TOL
        if sum(v.sum() for v in ok.values()) < 6: break
        num = den = 0.0; means = {}
        for cam in "MS":
            q, t = data[cam][0][ok[cam]], data[cam][1][ok[cam]]
            if len(q) == 0: continue
            qm, tm_ = q.mean(), t.mean(); num += ((q - qm) * (t - tm_)).sum(); den += ((q - qm) ** 2).sum(); means[cam] = (qm, tm_)
        b = num / den
        for cam, (qm, tm_) in means.items(): a[cam] = tm_ - b * qm
    return {cam: (a[cam], b, ok[cam]) for cam in "MS"}
old = collections.defaultdict(dict)
for x in csv.DictReader(open(OLD, encoding="utf-8-sig")):
    if x["mode"].startswith("USB"): old[(x["mode"].split()[1], x["group"])][int(x["seq"])] = (x["utc_master"], x["utc_slave"])
out = []; info = {}
for bit in ("8bit", "16bit"):
    for gdir in sorted((R / bit / "groups").iterdir()):
        g = gdir.name.split("_b")[0]; seqs = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("M_seq*.jpg"))
        roi = {int(k): v for k, v in json.load(open(ROI / f"rot_pairs_{bit}_{g}.json")).items()}
        obs = {}   # seq -> (M ms, S ms) best available reading
        for sq in seqs:
            if sq in roi: obs[sq] = (tms(roi[sq][0]), tms(roi[sq][1]), "rot")
            elif sq in old[(bit, g)]: obs[sq] = (tms(old[(bit, g)][sq][0]), tms(old[(bit, g)][sq][1]), "old")
        lines = {}
        if 8 <= len(seqs) < 30:
            jl = joint_lines(obs, seqs)
            if jl: lines = jl
        elif len(seqs) >= 30:
            for ci, cam in enumerate("MS"):
                ss = [s for s in seqs if s in obs]
                if len(ss) >= 6: lines[cam] = line(ss, [obs[s][ci] for s in ss])
        n_est = 0
        for sq in seqs:
            res = {}
            for ci, cam in enumerate("MS"):
                o = obs.get(sq); ln = lines.get(cam)
                if ln is None:
                    res[cam] = (o[ci], "obs") if o else (None, "none")
                else:
                    a, b, _ = ln; pred = a + b * sq
                    if o and abs(o[ci] - pred) <= TOL: res[cam] = (o[ci], "obs")
                    else: res[cam] = (pred, "est")
            if res["M"][0] is None or res["S"][0] is None: continue
            n_est += (res["M"][1] == "est") + (res["S"][1] == "est")
            out.append(dict(mode=f"USB {bit}", group=g, seq=sq, utc_master=fmt(res["M"][0]), utc_slave=fmt(res["S"][0]),
                            delta=round(res["M"][0] - res["S"][0]), master=res["M"][1], slave=res["S"][1]))
        info[f"{bit}|{g}"] = dict(frames=len(seqs), lines={c: (None if l is None else dict(period=round(l[1], 2), inliers=int(l[2].sum()), n=len(l[2]))) for c, l in lines.items()}, n_est=n_est)

# Single-frame groups (G1_01..G1_10, G4) have no time line. Their clocks were read from the original images (tilt-corrected crops);
# digits with overlapping ghost images can be uncertain by 1-2 refresh periods. 'unres' = still ambiguous (not used in statistics),
# 'cond' = captured under different conditions (16bit G1_01 was re-captured at 14:30 after the camera position had changed).
MANUAL = {
    ("8bit", "G1_02"): ("11:09:31.556", "11:09:31.606"), ("8bit", "G1_03"): ("11:10:23.556", "11:10:23.606"),
    ("8bit", "G1_05"): ("11:12:07.173", "11:12:07.223"), ("8bit", "G1_08"): ("11:14:43.090", "11:14:43.140"),
    ("8bit", "G1_10"): ("11:16:26.940", "11:16:26.990"), ("8bit", "G4"): ("11:19:44.324", "11:19:44.357"),
    ("16bit", "G1_02"): ("11:25:08.441", "11:25:08.507"), ("16bit", "G1_03"): ("11:26:00.291", "11:26:00.351"),
    ("16bit", "G1_04"): ("11:26:52.008", "11:26:52.074"), ("16bit", "G1_06"): ("11:28:35.708", "11:28:35.758"),
    ("16bit", "G1_07"): ("11:29:27.708", "11:29:27.758"), ("16bit", "G1_08"): ("11:30:19.525", "11:30:19.591"),
    ("16bit", "G1_09"): ("11:31:11.591", "11:31:11.658"), ("16bit", "G1_10"): ("11:32:03.208", "11:32:03.273"),
    ("16bit", "G1_05"): ("11:27:43.924", "11:27:43.983"),
    ("16bit", "G4"): ("11:36:01.309", "11:36:01.442"),   # bold layers as read; both clocks overlap with a second layer -> kept as unresolved (UNRES)   # slave between .974 and .991 (digits overlap); midpoint used, +-9 ms
}
UNRES = {("16bit", "G4")}
COND = {("16bit", "G1_01")}
for x in out:
    key = (x["mode"].split()[1], x["group"])
    if key in MANUAL:
        x["utc_master"], x["utc_slave"] = MANUAL[key]; x["delta"] = tms(x["utc_master"]) - tms(x["utc_slave"]); x["master"] = x["slave"] = "man"
    if key in UNRES: x["master"] = x["slave"] = "unres"
    if key in COND: x["master"] = x["slave"] = "cond"
w = csv.DictWriter(open(OUT / "usb_final_pairs.csv", "w", newline="", encoding="utf-8-sig"), fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
json.dump(info, open(OUT / "usb_final_info.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(len(out), "pairs;", sum(1 for x in out if "est" in (x["master"], x["slave"])), "with an estimated side;", sum(1 for x in out if x["master"] == "man"), "read by eye;", sum(1 for x in out if x["master"] == "unres"), "unresolved;", sum(1 for x in out if x["master"] == "cond"), "other conditions")
