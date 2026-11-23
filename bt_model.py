# 436 场大样本回测：赛前逐场累积 -> λ/μ -> 对照真实盘口（胜平负 / 亚盘 / 大小球2.5 / 总进球三档）
import csv, io, json, math, pathlib, urllib.request, datetime, traceback

LG = {"英超": "E0", "意甲": "I1", "西甲": "SP1", "法乙": "F2"}
SEASON = "2627"
URL = "https://www.football-data.co.uk/mmz4281/{}/{}.csv"
COEF = (1.09, 0.91)
K = 0.95
THR = (0.03, 0.05, 0.08)

def dl(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8-sig", "ignore")

def fl(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except Exception:
        return None

def fs(x):
    try:
        return float(x)
    except Exception:
        return None

def pois(k, l):
    return math.exp(-l) * l ** k / math.factorial(k)

def probs(lam, mu, N=10):
    p1 = px = p2 = 0.0
    tg = [0.0] * 8
    mar = {}
    tot = 0.0
    for i in range(N + 1):
        pi = pois(i, lam)
        for j in range(N + 1):
            p = pi * pois(j, mu)
            tot += p
            s = i + j
            tg[min(s, 7)] += p
            mar[i - j] = mar.get(i - j, 0.0) + p
            if i > j: p1 += p
            elif i == j: px += p
            else: p2 += p
    if tot <= 0:
        return None
    return dict(p1=p1/tot, px=px/tot, p2=p2/tot, tg=[x/tot for x in tg],
                o25=1 - sum(tg[:3])/tot, u25=sum(tg[:3])/tot,
                mar={k: v/tot for k, v in mar.items()})

def norm(*ps):
    s = sum(ps)
    return [p/s for p in ps] if s > 0 else None

def run(code, rd):
    teams, res = {}, []
    def avg(arr, k):
        return (sum(x[k] for x in arr) / len(arr)) if arr else None
    for r in rd:
        if not r.get("FTHG") or not r.get("FTAG"):
            continue
        try:
            hs, as_ = int(float(r["FTHG"])), int(float(r["FTAG"]))
        except Exception:
            continue
        H, A = r.get("HomeTeam"), r.get("AwayTeam")
        if not H or not A:
            continue
        th = teams.setdefault(H, {"H": [], "A": [], "r": []})
        ta = teams.setdefault(A, {"H": [], "A": [], "r": []})
        L = [t for t in teams.values() if t["H"] and t["A"]]
        if len(L) >= 6:
            bh = sum(x[0] for t in L for x in t["H"]) / sum(len(t["H"]) for t in L)
            bhd = sum(x[1] for t in L for x in t["H"]) / sum(len(t["H"]) for t in L)
            ba = sum(x[0] for t in L for x in t["A"]) / sum(len(t["A"]) for t in L)
            bad = sum(x[1] for t in L for x in t["A"]) / sum(len(t["A"]) for t in L)
        else:
            bh = bhd = ba = bad = 1.35
        h_att, h_def = avg(th["H"], 0), avg(th["H"], 1)
        a_att, a_def = avg(ta["A"], 0), avg(ta["A"], 1)
        def fb(v, b):
            return v if v is not None else b
        lam = 0.5 * (fb(h_att, bh) * fb(a_def, bad) / bh) + 0.5 * bh
        mu = 0.5 * (fb(a_att, ba) * fb(h_def, bhd) / ba) + 0.5 * ba
        lam *= COEF[0]; mu *= COEF[1]
        def form(t):
            rs = t["r"][-6:]
            if not rs:
                return 1.0
            pts = sum(3 if x == "W" else (1 if x == "D" else 0) for x in rs)
            return min(1.10, max(0.90, 0.85 + 0.30 * (pts / (3 * len(rs)))))
        st = (form(th) + form(ta)) / 2
        lam = (K * lam + (1 - K) * bh) * st
        mu = (K * mu + (1 - K) * ba) * st
        pr = probs(lam, mu)
        o1 = [fl(r.get(k)) for k in ("B365H", "B365D", "B365A")]
        oo = [fl(r.get("B365>2.5")), fl(r.get("B365<2.5"))]
        ah, ahh, aha = fs(r.get("AHh")), fl(r.get("B365AHH")), fl(r.get("B365AHA"))
        row = dict(lg=code, d=r.get("Date"), h=H, a=A, ft=[hs, as_], lam=round(lam, 3), mu=round(mu, 3),
                   nh=len(th["H"]), na=len(ta["A"]),
                   res=("W" if hs > as_ else ("D" if hs == as_ else "L")))
        if pr:
            row.update(p1=pr["p1"], px=pr["px"], p2=pr["p2"], o25=pr["o25"], g01=pr["tg"][0]+pr["tg"][1],
                       g23=pr["tg"][2]+pr["tg"][3], g46=sum(pr["tg"][4:]))
        if all(o1) and pr:
            row["m1"] = norm(*o1)
        if all(oo) and pr:
            mo = norm(*oo)
            row["mo"] = mo
            row["omkt"] = mo[0]
        if all(o1):
            row["o1"] = o1
        if all(oo):
            row["oo"] = oo
        if ah is not None and ahh and aha:
            row.update(ah=ah, ahh=ahh, aha=aha, mah=norm(ahh, aha))
            if pr:
                m = pr["mar"]
                win = sum(v for k, v in m.items() if k > -ah)
                push = sum(v for k, v in m.items() if k == -ah)
                row["ahh_p"] = win + push / 2
        res.append(row)
        th["H"].append((hs, as_)); ta["A"].append((as_, hs))
        th["r"].append("W" if hs > as_ else ("D" if hs == as_ else "L"))
        ta["r"].append("W" if as_ > hs else ("D" if as_ == hs else "L"))
    return res

def metrics(rows):
    m = {}
    n12 = brier_m = brier_k = acc_m = acc_k = 0.0
    for r in rows:
        if "p1" not in r or "m1" not in r: continue
        ps = (r["p1"], r["px"], r["p2"])
        act = ("W", "D", "L").index(r["res"])
        n12 += 1
        acc_m += 1 if max(range(3), key=lambda i: ps[i]) == act else 0
        acc_k += 1 if max(range(3), key=lambda i: r["m1"][i]) == act else 0
        brier_m += sum((ps[i] - (1 if i == act else 0)) ** 2 for i in range(3))
        brier_k += sum((r["m1"][i] - (1 if i == act else 0)) ** 2 for i in range(3))
    if n12:
        m["1x2"] = dict(n=int(n12), acc_model=round(acc_m/n12, 4), acc_mkt=round(acc_k/n12, 4),
                        brier_model=round(brier_m/n12, 4), brier_mkt=round(brier_k/n12, 4))
    n = hit = 0
    for r in rows:
        if "o25" not in r or "mo" not in r: continue
        over = r["ft"][0] + r["ft"][1] > 2.5
        n += 1; hit += 1 if ((r["o25"] > 0.5) == over) else 0
    if n:
        m["ou25"] = dict(n=int(n), hit=round(hit/n, 4))
        roib = {}
        for th in THR:
            bn = bh2 = 0; u = 0.0
            for r in rows:
                if "o25" not in r or "oo" not in r: continue
                over = r["ft"][0] + r["ft"][1] > 2.5
                for i, (p, od) in enumerate(((r["o25"], r["oo"][0]), (1 - r["o25"], r["oo"][1]))):
                    if p * od - 1 < th: continue
                    bn += 1
                    won = (i == 0) == over
                    bh2 += 1 if won else 0
                    u += (od - 1) if won else -1.0
            roib[f"ev{int(th*100)}"] = dict(bets=bn, hit=bh2, units=round(u, 2),
                                            roi=round(u / bn, 4) if bn else None)
        m["ou25"]["roi"] = roib
    n = hit = 0
    for r in rows:
        if "ahh_p" not in r or "mah" not in r: continue
        mar = r["ft"][0] - r["ft"][1]
        n += 1
        if mar == -r["ah"]:
            hit += 0.5
        else:
            hit += 1 if ((r["ahh_p"] > 0.5) == (mar > -r["ah"])) else 0
    if n:
        m["ah"] = dict(n=int(n), hit=round(hit/n, 4))
        roib = {}
        for th in THR:
            bn = bh2 = 0; u = 0.0
            for r in rows:
                if "ahh_p" not in r: continue
                mar = r["ft"][0] - r["ft"][1]
                for i, (p, od) in enumerate(((r["ahh_p"], r["ahh"]), (1 - r["ahh_p"], r["aha"]))):
                    if p * od - 1 < th: continue
                    if mar == -r["ah"]: continue
                    bn += 1
                    won = (i == 0) == (mar > -r["ah"])
                    bh2 += 1 if won else 0
                    u += (od - 1) if won else -1.0
            roib[f"ev{int(th*100)}"] = dict(bets=bn, hit=bh2, units=round(u, 2),
                                            roi=round(u / bn, 4) if bn else None)
        m["ah"]["roi"] = roib
    n = hit = 0
    for r in rows:
        if "g01" not in r: continue
        t = r["ft"][0] + r["ft"][1]
        k = 0 if t <= 1 else (1 if t <= 3 else 2)
        n += 1
        ps = (r["g01"], r["g23"], r["g46"])
        hit += 1 if max(range(3), key=lambda i: ps[i]) == k else 0
    if n:
        m["tg3"] = dict(n=int(n), hit=round(hit/n, 4))
    return m

def main():
    out = {"fetched": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "leagues": {}, "errors": []}
    allrows = []
    for lg, code in LG.items():
        try:
            rows = run(code, list(csv.DictReader(io.StringIO(dl(URL.format(SEASON, code))))))
            out["leagues"][lg] = len(rows)
            allrows += rows
            print("BT-OK", lg, len(rows))
        except Exception:
            out["errors"].append(lg + ": " + traceback.format_exc()[-300:])
            print("BT-ERR", lg, traceback.format_exc()[-160:])
    out["n_rows"] = len(allrows)
    out["metrics"] = metrics(allrows)
    out["sample"] = allrows[:2]
    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/bt.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out["metrics"], ensure_ascii=False, indent=1))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("BT-TOPFAIL", traceback.format_exc()[-300:])
