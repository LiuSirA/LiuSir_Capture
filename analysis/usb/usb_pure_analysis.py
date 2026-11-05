# -*- coding: utf-8 -*-
"""USB master/slave sync test: error analysis and frame-loss analysis (8 bit and 16 bit)."""
import csv, json, collections, hashlib, statistics as st
from pathlib import Path
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"]=["Microsoft YaHei","SimHei","DejaVu Sans"]; plt.rcParams["axes.unicode_minus"]=False
R=Path("J:/Mywork/Project_LH/sync_acc_runs/20261103_usb"); PAIRS=Path("J:/Mywork/Project_LH/sync_acc_runs/20261103_pcie16_r3/reports/final_pairs_all_modes.csv")
OUT=Path(__file__).parent; TICK=1000/60
def gk(g): return "G1" if g.startswith("G1_") else g
def tms(s):
    h,m,r=s.split(":"); a,b=r.split("."); return ((int(h)*60+int(m))*60+int(a))*1000+int(b)
rows=list(csv.DictReader(open(PAIRS,encoding="utf-8-sig")))
res={}
def stats(d):
    a=np.array(d,float)
    return dict(n=len(a),median=float(np.median(a)),mean=float(a.mean()),std=float(a.std(ddof=1)) if len(a)>1 else None,
        mad=float(np.median(abs(a-np.median(a)))*1.4826) if len(a)>1 else None,p5=float(np.percentile(a,5)),p95=float(np.percentile(a,95)),mn=float(a.min()),mx=float(a.max()))
for bit in ("8bit","16bit"):
    mode=f"USB {bit}"; v=[x for x in rows if x["mode"]==mode]
    status=json.load(open(R/bit/"status/_sync_test_status.json",encoding="utf-8"))["groups"]
    by=collections.defaultdict(list)
    for x in v: by[x["group"]].append(x)
    per={}; allpairs=0
    for g in ["G1","G2","G3","G4","G5","G6"]:
        ds=[float(x["delta_ms(master-slave)"]) for k,xs in by.items() if gk(k)==g for x in xs]
        allpairs+=len(ds); nm=[d for d in ds if abs(d)<=100]
        cap={"G1":10,"G2":10,"G3":100,"G4":1,"G5":10,"G6":100}[g]
        per[g]=dict(captured=cap,read=len(ds),valid=len(nm),excluded=len(ds)-len(nm),stats=stats(nm) if nm else None,normal=nm)
    full=sum((per[g]["normal"] for g in ("G1","G2","G3")),[]); b3=sum((per[g]["normal"] for g in ("G4","G5","G6")),[])
    tot=full+b3
    hist={"full":dict(sorted(collections.Counter(int(round(x/TICK)) for x in full).items())),"bin3":dict(sorted(collections.Counter(int(round(x/TICK)) for x in b3).items()))}
    drift={}
    for g in ("G3","G6"):
        pts=sorted((int(x["seq"]),float(x["delta_ms(master-slave)"])) for k,xs in by.items() if gk(k)==g for x in xs if abs(float(x["delta_ms(master-slave)"]))<=100)
        xs_=np.array([a for a,b in pts],float); ys_=np.array([b for a,b in pts],float)
        A=np.polyfit(xs_,ys_,1); drift[g]=dict(n=len(pts),slope=float(A[0]),total_100=float(A[0]*100))
    # frame-loss checks
    fl={}; meta={}
    for g,s in status.items():
        n=s["n"]
        # the status entry can be stale (16bit G1_01 was re-captured); use the one group folder present
        cands=sorted((R/bit/"groups").glob(f"{g}_b*")); assert len(cands)==1,(g,cands)
        d=cands[0]
        seqs={}
        for cam in "MS":
            seqs[cam]=sorted(int(p.stem.split("seq")[1]) for p in d.glob(f"{cam}_seq*.jpg"))
        exp=list(range(2,n+2))
        fl[g]=dict(M=len(seqs["M"]),S=len(seqs["S"]),expected=n,gaps_M=[q for q in exp if q not in seqs["M"]],gaps_S=[q for q in exp if q not in seqs["S"]],
            extra=[q for q in seqs["M"]+seqs["S"] if q not in exp],any_drop=s.get("any_drop"),ok=s.get("ok"))
        hs=collections.Counter(hashlib.md5(p.read_bytes()).hexdigest() for p in d.glob("[MS]_seq*.jpg"))
        L=[json.loads(l) for l in (d/"meta.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        meta[g]=dict(n=len(L),dups=sum(c-1 for c in hs.values() if c>1),gps_mismatch=sum(1 for x in L if x.get("gps") and x["gps"].get("seq_number")!=x["seq"]),
            sizes=sorted({f'{x["width"]}x{x["height"]}' for x in L}))
    # cadence from pairs
    cad={}
    for g in ("G2","G3","G5","G6"):
        vv=sorted([x for k,xs in by.items() if gk(k)==g for x in xs],key=lambda x:int(x["seq"]))
        iv={}
        for cam,k in (("M","utc_master"),("S","utc_slave")):
            a=[tms(b[k])-tms(c[k]) for c,b in zip(vv,vv[1:]) if int(b["seq"])-int(c["seq"])==1]
            if a:
                a=np.array(a); med=np.median(a)
                iv[cam]=dict(n=len(a),med=float(med),mad=float(np.median(abs(a-med))*1.4826),w2=float(np.mean(abs(a-med)<=34)),big=int(np.sum(abs(a-med)>200)))
        kk=sorted(int(x["seq"]) for x in vv)
        if len(kk)>1:
            for cam,k in (("M","utc_master"),("S","utc_slave")):
                d0={int(x["seq"]):x for x in vv}
                if cam in iv: iv[cam]["span_period"]=(tms(d0[kk[-1]][k])-tms(d0[kk[0]][k]))/(kk[-1]-kk[0])
        cad[g]=iv
    res[bit]=dict(per={g:{k:v for k,v in p.items() if k!="normal"} for g,p in per.items()},all=stats(tot),full=stats(full),bin3=stats(b3),
        n_all_pairs=allpairs,hist=hist,drift=drift,fl=fl,meta=meta,cad=cad)
    fig,ax=plt.subplots(1,2,figsize=(10,3.6))
    for a,(k,vals) in zip(ax,[("Full frame / 全幅",full),("Bin 3x3",b3)]):
        a.hist(vals,bins=np.arange(-110,111,TICK/2),color="#4477aa"); a.set_title(f"USB {bit}  {k}  n={len(vals)}"); a.set_xlabel("master - slave Δ (ms)"); a.set_ylabel("pairs")
        for t in range(-6,7): a.axvline(t*TICK,color="#bbbbbb",lw=.5,zorder=0)
    plt.tight_layout(); plt.savefig(OUT/f"fig_usb_{bit}_hist.png",dpi=130); plt.close()
json.dump(res,open(OUT/"usb_pure_results.json","w",encoding="utf-8"),ensure_ascii=False,indent=1)
for bit in res:
    r=res[bit]; print("==",bit,"all pairs",r["n_all_pairs"],"valid",r["all"]["n"],"median",r["all"]["median"],"mad",r["all"]["mad"])
    for g,p in r["per"].items(): print(g,p["captured"],p["read"],p["valid"],p["stats"] and (round(p["stats"]["median"],1),round(p["stats"]["std"] or 0,1),round(p["stats"]["mad"] or 0,1)))
    print("hist",r["hist"]); print("drift",r["drift"]); print("cad",{g:v for g,v in r["cad"].items()})
    print("fl bad:",[(g,x) for g,x in r["fl"].items() if x["M"]!=x["expected"] or x["S"]!=x["expected"] or x["gaps_M"] or x["gaps_S"] or x["extra"] or x["any_drop"]])
    print("meta dups/mismatch:",sum(m["dups"] for m in r["meta"].values()),sum(m["gps_mismatch"] for m in r["meta"].values()),sorted({s for m in r["meta"].values() for s in m["sizes"]}))
