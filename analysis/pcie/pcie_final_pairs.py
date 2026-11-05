# -*- coding: utf-8 -*-
"""Final PCIe pairs: tilt-corrected readings (merged with the earlier recognition candidates), frames that are more than 40 ms off
the camera's own time line or have no reading replaced by the line value (flag 'est'), readings corrected after checking the
original images ('chk') and single-frame groups read by eye ('man')."""
import csv, json, collections
from pathlib import Path
import numpy as np

R = Path("J:/Mywork/Project_LH/sync_acc_runs/20261103_pcie16_r3"); ROI = R / "reports" / "roi"
OLD = R / "reports" / "final_pairs_all_modes.csv"
OUT = Path(__file__).parent; TOL = 40.0


def tms(s):
    h, m, r = s.split(":"); a, b = r.split("."); return ((int(h) * 60 + int(m)) * 60 + int(a)) * 1000 + int(b)


def fmt(ms):
    ms = int(round(ms)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000); return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def line(seq, t):
    seq = np.asarray(seq, float); t = np.asarray(t, float); sl = []
    for i in range(len(seq)):
        for j in range(i + 5, len(seq), 3): sl.append((t[j] - t[i]) / (seq[j] - seq[i]))
    if not sl: return None
    b = float(np.median(sl)); a = float(np.median(t - b * seq)); ok = np.ones(len(seq), bool)
    for _ in range(8):
        if ok.sum() < 5: break
        b, a = np.polyfit(seq[ok], t[ok], 1); ok = abs(t - (a + b * seq)) < TOL
    return a, b, ok


def joint_lines(obs, seqs):
    """Short groups (<30 frames): both cameras share one frame period; one slope, one intercept per camera."""
    data = {}
    for ci, cam in enumerate("MS"):
        ss = [s_ for s_ in seqs if s_ in obs]; data[cam] = (np.array(ss, float), np.array([obs[s_][ci] for s_ in ss], float))
    sl = []
    for cam in "MS":
        q, t = data[cam]
        for i in range(len(q)):
            for j in range(i + 3, len(q)): sl.append((t[j] - t[i]) / (q[j] - q[i]))
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


# Readings checked against the original images (see the report, Section 3.5): {(group, seq): {"M": time or None, "S": ...}};
# None = could not be read from the image, the time line gives an estimate.
CHK = json.load(open(OUT / "pcie_chk.json", encoding="utf-8")) if (OUT / "pcie_chk.json").exists() else {}
CHK = {tuple([k.split("|")[0], int(k.split("|")[1])]): v for k, v in CHK.items()}
# Single-frame groups (G1_01..G1_10, G4): readings from the original images, {group: (master, slave)}; UNRES = not decided.
MANUAL = json.load(open(OUT / "pcie_manual.json", encoding="utf-8")) if (OUT / "pcie_manual.json").exists() else {}
UNRES = set(json.load(open(OUT / "pcie_unres.json", encoding="utf-8"))) if (OUT / "pcie_unres.json").exists() else set()

old = collections.defaultdict(dict)
for x in csv.DictReader(open(OLD, encoding="utf-8-sig")):
    if x["mode"] == "PCIe 16bit": old[x["group"]][int(x["seq"])] = (x["utc_master"], x["utc_slave"])
st = json.load(open(R / "status" / "_sync_test_status.json", encoding="utf-8"))["groups"]
out, info, flagged = [], {}, []
for g, v in st.items():
    gdir = Path(v["group_dir"]); seqs = sorted(int(p.stem.split("seq")[1]) for p in gdir.glob("M_seq*.jpg"))
    roi = {int(k): x for k, x in json.load(open(ROI / f"rot_pairs_{g}.json")).items()}
    obs = {}
    for sq in seqs:
        if sq in roi: obs[sq] = (tms(roi[sq][0]), tms(roi[sq][1]), "rot")
        elif sq in old[g]: obs[sq] = (tms(old[g][sq][0]), tms(old[g][sq][1]), "old")
    chk_sides = {}
    for (cg, sq), d in CHK.items():
        if cg == g and sq in seqs:
            cur = obs.get(sq)
            for cam, val in d.items():
                if val is None:
                    chk_sides[(sq, cam)] = None
                    if cur is not None: obs.pop(sq, None); cur = None
                else:
                    m, s_, _src = cur if cur else (None, None, "old")
                    m, s_ = (tms(val), s_) if cam == "M" else (m, tms(val))
                    cur = (m, s_, "chk"); obs[sq] = cur; chk_sides[(sq, cam)] = tms(val)
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
            if chk_sides.get((sq, cam)): res[cam] = (chk_sides[(sq, cam)], "chk")
            elif ln is None: res[cam] = (o[ci], "obs") if o else (None, "none")
            else:
                a, b, _ = ln; pred = a + b * sq
                if o and abs(o[ci] - pred) <= TOL:
                    res[cam] = (o[ci], "obs")
                    if abs(o[ci] - pred) >= 16: flagged.append((g, sq, cam, round(float(o[ci] - pred), 1)))
                else: res[cam] = (pred, "est")
        if res["M"][0] is None or res["S"][0] is None: continue
        n_est += (res["M"][1] == "est") + (res["S"][1] == "est")
        out.append(dict(group=g, seq=sq, utc_master=fmt(res["M"][0]), utc_slave=fmt(res["S"][0]),
                        delta=round(res["M"][0] - res["S"][0]), master=res["M"][1], slave=res["S"][1]))
    info[g] = dict(frames=len(seqs), lines={c: (None if l is None else dict(period=round(l[1], 2), inliers=int(l[2].sum()), n=len(l[2]))) for c, l in lines.items()}, n_est=n_est)
for x in out:
    if x["group"] in MANUAL:
        m_, s_ = MANUAL[x["group"]]; x["utc_master"], x["utc_slave"] = m_, s_; x["delta"] = tms(m_) - tms(s_); x["master"] = x["slave"] = "man"
    if x["group"] in UNRES: x["master"] = x["slave"] = "unres"
w = csv.DictWriter(open(OUT / "pcie_final_pairs.csv", "w", newline="", encoding="utf-8-sig"), fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
json.dump(dict(info=info, flagged=flagged), open(OUT / "pcie_final_info.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(len(out), "pairs;", sum(1 for x in out if "est" in (x["master"], x["slave"])), "with an estimated side;", len(flagged), "flagged readings (>=16 ms off the line);",
      sum(1 for x in out if x["master"] == "man"), "read by eye")
