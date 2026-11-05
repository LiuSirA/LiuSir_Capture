# -*- coding: utf-8 -*-
import csv, json, re, statistics as st, collections, hashlib
from pathlib import Path
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"]=["Microsoft YaHei","SimHei","DejaVu Sans"]; plt.rcParams["axes.unicode_minus"]=False
R=Path("J:/Mywork/Project_LH/sync_acc_runs/20261103_pcie16_r3"); OUT=Path(__file__).parent
TICK=1000/60
st_=json.load(open(R/"status/_sync_test_status.json",encoding="utf-8"))["groups"]
rows=list(csv.DictReader(open(Path(__file__).resolve().parents[2]/"results"/"pcie_pairs.csv",encoding="utf-8-sig")))
for x in rows: x["delta_ms(master-slave)"]=x["delta"]
def tms(s):
    h,m,r=s.split(":"); sec,ms=r.split("."); return ((int(h)*60+int(m))*60+int(sec))*1000+int(ms)
by=collections.defaultdict(list)
for x in rows: by[x["group"]].append(x)
def gk(g): return "G1" if g.startswith("G1_") else g
# ---------- error analysis on "normal" data: |delta|<=100 ----------
res={}; per_pair=[]
for g,v in by.items():
    for x in v:
        d=float(x["delta_ms(master-slave)"]); per_pair.append((gk(g),g,int(x["seq"]),d,x["utc_master"],x["utc_slave"]))
groups=["G1","G2","G3","G4","G5","G6"]
srcs={}
for g in ["G1","G2","G3","G4","G5","G6"]:
    xs=[x for x in rows if gk(x["group"])==g]
    srcs[g]=dict(captured=len(xs),**{k:sum(1 for x in xs if (("est" if "est" in (x["master"],x["slave"]) else ("chk" if "chk" in (x["master"],x["slave"]) else x["master"]))==k)) for k in ("obs","chk","man","est")})
def stats(d):
    a=np.array(d,float)
    return dict(n=len(a),median=float(np.median(a)),mean=float(a.mean()),std=float(a.std(ddof=1)) if len(a)>1 else None,
      mad=float(np.median(abs(a-np.median(a)))*1.4826) if len(a)>1 else None,p5=float(np.percentile(a,5)),p95=float(np.percentile(a,95)),mn=float(a.min()),mx=float(a.max()))
total_all={}; normal={}
for g in groups:
    allv=[p[3] for p in per_pair if p[0]==g]
    nm=[d for d in allv if abs(d)<=100]
    total_all[g]=len(allv); normal[g]=nm
tab={g:stats(normal[g]) for g in groups if normal[g]}
early=normal["G1"]+normal["G2"]+normal["G3"]+normal["G4"]; late=normal["G5"]+normal["G6"]
tab["early(G1-G4)"]=stats(early); tab["late(G5-G6)"]=stats(late)
# tick histogram
def tickhist(d):
    c=collections.Counter(int(round(x/TICK)) for x in d); return dict(sorted(c.items()))
th={"early":tickhist(early),"late":tickhist(late)}
# within-group drift (G3,G6): slope ms/frame on normal pairs
drift={}
for g in ("G3","G6"):
    pts=[(p[2],p[3]) for p in per_pair if p[0]==g and abs(p[3])<=100]
    xs=np.array([a for a,b in pts],float); ys=np.array([b for a,b in pts],float)
    A=np.polyfit(xs,ys,1); drift[g]=dict(slope=float(A[0]),n=len(xs),total_over_100=float(A[0]*100))
# ---------- cadence & frame loss ----------
cad={}
for g in ("G2","G3","G5","G6"):
    v=sorted([x for x in by[g] if x["master"]=="obs" and x["slave"]=="obs"],key=lambda x:int(x["seq"]))
    c={}
    for cam,k in (("M","utc_master"),("S","utc_slave")):
        iv=[tms(b[k])-tms(a[k]) for a,b in zip(v,v[1:]) if int(b["seq"])-int(a["seq"])==1]
        if iv:
            a_=np.array(iv); med=float(np.median(a_))
            c[cam]=dict(n=len(iv),med=med,mad=float(np.median(abs(a_-med))*1.4826),w2=float(np.mean(abs(a_-med)<=34)),big=int(np.sum(abs(a_-med)>200)),gaps_2x=int(np.sum(a_>900)))
        allv={int(x["seq"]):x for x in by[g]}; kk=sorted(allv)
        if len(kk)>1 and cam in c: c[cam]["span_period"]=(tms(allv[kk[-1]][k])-tms(allv[kk[0]][k]))/(kk[-1]-kk[0])
    cad[g]=c
fl={}
for g,s in st_.items():
    d=Path(s["group_dir"]); n=s["n"]
    for cam in "MS":
        seqs=sorted(int(p.stem.split("seq")[1]) for p in d.glob(f"{cam}_seq*.jpg"))
        exp=list(range(2,n+2)); miss=[q for q in exp if q not in seqs]; extra=[q for q in seqs if q not in exp]
        # file duplicates by hash within camera
        hs=collections.Counter(hashlib.md5(p.read_bytes()).hexdigest() for p in d.glob(f"{cam}_seq*.jpg"))
        dup=sum(c-1 for c in hs.values() if c>1)
        sizes=set()
        fl.setdefault(gk(g),{})[ (g,cam) ]=dict(n_files=len(seqs),expected=n,missing=miss,extra=extra,dup=dup)
# metadata sizes + fp duplicates + gps seq == filename seq
meta_check={}
for g,s in st_.items():
    d=Path(s["group_dir"]); mp=d/"meta.jsonl"
    if not mp.exists(): continue
    L=[json.loads(l) for l in mp.read_text(encoding="utf-8").splitlines() if l.strip()]
    wh=collections.Counter((x["width"],x["height"]) for x in L)
    mism=sum(1 for x in L if x["gps"] and x["gps"].get("seq_number")!=x["seq"])
    fp=collections.Counter((x["tag"],x["fp"]) for x in L); dups=sum(c-1 for c in fp.values() if c>1)
    meta_check[g]=dict(n=len(L),wh={str(k):v for k,v in wh.items()},gps_seq_mismatch=mism,fp_dups=dups)
# attempts per group (folders)
attempts=collections.defaultdict(list)
for p in sorted((R/"groups").iterdir()):
    if p.is_dir() and not p.name.startswith("_"):
        m=re.match(r"^(G1_\d+|G[2-6])_",p.name)
        if m:
            nM=len(list(p.glob("M_seq*.jpg"))); nS=len(list(p.glob("S_seq*.jpg")))
            attempts[m.group(1)].append((p.name,nM,nS))
# ---------- figures ----------
fig,ax=plt.subplots(1,2,figsize=(10,3.6))
for a,(k,v) in zip(ax,[("早期 G1–G4（11/03）",early),("后期 G5–G6（11/04）",late)]):
    a.hist(v,bins=np.arange(-110,111,TICK/2),color="#4477aa"); a.set_title(f"{k}  n={len(v)}"); a.set_xlabel("主−从 Δ (ms)"); a.set_ylabel("帧对数")
    for t in range(-6,7): a.axvline(t*TICK,color="#bbbbbb",lw=.5,zorder=0)
plt.tight_layout(); plt.savefig(OUT/"fig_pcie_hist.png",dpi=130); plt.close()
fig,ax=plt.subplots(1,2,figsize=(10,3.6))
for a,g in zip(ax,("G3","G6")):
    pts=sorted((p[2],p[3]) for p in per_pair if p[0]==g and abs(p[3])<=100)
    a.scatter([x for x,_ in pts],[y for _,y in pts],s=10,color="#4477aa"); a.axhline(np.median([y for _,y in pts]),color="#cc3311",lw=1)
    a.set_title(f"{g} Δ 随帧序号（中位数红线；斜率 {drift[g]['slope']:+.3f} ms/帧）"); a.set_xlabel("帧序号"); a.set_ylabel("Δ (ms)")
plt.tight_layout(); plt.savefig(OUT/"fig_pcie_drift.png",dpi=130); plt.close()
json.dump(dict(tab=tab,th=th,drift=drift,cad=cad,fl={k:{str(a):b for a,b in v.items()} for k,v in fl.items()},meta=meta_check,
  attempts=attempts,srcs=srcs,total_all=total_all,n_normal={g:len(normal[g]) for g in groups}),open(OUT/"pcie_pure_results.json","w",encoding="utf-8"),ensure_ascii=False,indent=1,default=str)
print(json.dumps(dict(tab=tab,drift=drift,cad=cad,th=th,total=total_all,nn={g:len(normal[g]) for g in groups}),ensure_ascii=False,indent=0))
print(json.dumps(meta_check,ensure_ascii=False)); 
for g,v in fl.items():
    print(g,[(k[1],x["n_files"],x["expected"],x["missing"],x["dup"]) for k,x in v.items()])
print({k:v for k,v in attempts.items()})
