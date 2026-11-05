# -*- coding: utf-8 -*-
"""Final pairs and statistics of the 11/04 USB repeat tests: tilt-corrected readings, time-line estimates for frames more than 40 ms
off the line or without a reading, and readings corrected after checking the original images (repeat_chk.json)."""
import csv, json, sys
from pathlib import Path
import numpy as np

DATA = Path(r"J:\Mywork\Project_LH\sync_acc_runs\20261104_usb_session_test")
OUT = Path(__file__).parent
TOL = 40.0


def tms(s):
    h, m, r = s.split(":"); a, b = r.split("."); return ((int(h) * 60 + int(m)) * 60 + int(a)) * 1000 + int(b)


def fmt(ms):
    ms = int(round(ms)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000); return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def line(seq, t, slope=None):
    """Robust straight line t = a + b*seq. With `slope` given (shared frame period) only the intercept is fitted."""
    seq = np.asarray(seq, float); t = np.asarray(t, float); sl = []
    for i in range(len(seq)):
        for j in range(i + 5, len(seq), 3): sl.append((t[j] - t[i]) / (seq[j] - seq[i]))
    b = float(np.median(sl)) if slope is None else float(slope)
    a = float(np.median(t - b * seq)); ok = np.ones(len(seq), bool)
    for _ in range(8):
        if ok.sum() < 5: break
        if slope is None: b, a = np.polyfit(seq[ok], t[ok], 1)
        else: a = float(np.mean(t[ok] - b * seq[ok]))
        ok = abs(t - (a + b * seq)) < TOL
    return a, b, ok


def main():
    chk = {}
    cp = DATA / "repeat_chk.json"
    if cp.exists(): chk = json.load(open(cp, encoding="utf-8"))
    out, flagged, info = [], [], {}
    groups = sorted(p for p in (DATA / "groups").iterdir() if p.is_dir())
    slopes = []
    for g in groups:
        rp0 = {int(k): v for k, v in json.load(open(DATA / f"rot_pairs_{g.name}.json")).items()}
        ss0 = sorted(rp0)
        for ci in (0, 1): slopes.append(line(ss0, [tms(rp0[s_][ci]) for s_ in ss0])[1])
    B0 = float(np.median(slopes))   # the same frame period in all seven groups (about 800.2 ms)
    print("shared frame period %.2f ms (per group/camera slopes %.2f ... %.2f)" % (B0, min(slopes), max(slopes)))
    for g in groups:
        seqs = sorted(int(p.stem.split("seq")[1]) for p in g.glob("M_seq*.jpg"))
        rp = {int(k): v for k, v in json.load(open(DATA / f"rot_pairs_{g.name}.json")).items()}
        obs = {sq: (tms(rp[sq][0]), tms(rp[sq][1])) for sq in seqs if sq in rp}
        # second reading source: the earlier fixed-region recognition (no tilt correction); per frame the candidate that fits the
        # time lines of both cameras better is kept
        ro = {int(k): v for k, v in json.load(open(DATA / f"roi_pairs_{g.name}.json")).items()}
        alt = {sq: (tms(ro[sq][0]), tms(ro[sq][1])) for sq in seqs if sq in ro}
        for _ in range(3):
            ln = {}
            for ci, cam in enumerate("MS"):
                ss = [s_ for s_ in seqs if s_ in obs]; ln[cam] = line(ss, [obs[s_][ci] for s_ in ss], slope=B0)
            def cost(c, sq):
                return max(abs(c[0] - (ln["M"][0] + B0 * sq)), abs(c[1] - (ln["S"][0] + B0 * sq)))
            for sq in seqs:
                cands = [c for c in (obs.get(sq), alt.get(sq)) if c is not None]
                if cands: obs[sq] = min(cands, key=lambda c: cost(c, sq))
        for key, d in chk.items():
            cg, sq = key.split("|"); sq = int(sq)
            if cg != g.name: continue
            cur = list(obs.get(sq, (None, None)))
            for cam, val in d.items():
                i = "MS".index(cam)
                if val is None: obs.pop(sq, None); cur = [None, None]
                else: cur[i] = tms(val)
            if cur[0] is not None and cur[1] is not None: obs[sq] = tuple(cur)
        lines = {}
        for ci, cam in enumerate("MS"):
            ss = [s for s in seqs if s in obs]; lines[cam] = line(ss, [obs[s][ci] for s in ss], slope=B0)
        n_est = 0
        for sq in seqs:
            res = {}
            for ci, cam in enumerate("MS"):
                a, b, _ = lines[cam]; pred = a + b * sq; o = obs.get(sq)
                ck = chk.get(f"{g.name}|{sq}", {})
                if cam in ck and ck[cam] is not None: res[cam] = (tms(ck[cam]), "chk")
                elif o and abs(o[ci] - pred) <= TOL:
                    res[cam] = (o[ci], "obs")
                    if abs(o[ci] - pred) >= 16: flagged.append((g.name, sq, cam, round(float(o[ci] - pred), 1)))
                else: res[cam] = (pred, "est")
            n_est += (res["M"][1] == "est") + (res["S"][1] == "est")
            out.append(dict(group=g.name, seq=sq, utc_master=fmt(res["M"][0]), utc_slave=fmt(res["S"][0]),
                            delta=round(res["M"][0] - res["S"][0]), master=res["M"][1], slave=res["S"][1]))
        info[g.name] = dict(frames=len(seqs), period=round(B0, 2), n_est_sides=n_est)
    w = csv.DictWriter(open(OUT / "usb_repeat_final_pairs.csv", "w", newline="", encoding="utf-8-sig"), fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
    json.dump(dict(info=info, flagged=flagged), open(OUT / "usb_repeat_final_info.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(out), "pairs;", sum(1 for x in out if "est" in (x["master"], x["slave"])), "with an estimated side;", len(flagged), "readings flagged (>=16 ms off the line)")


if __name__ == "__main__":
    main()
