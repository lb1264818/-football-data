# 大样本回测 v4：把市场信息灌进 λ/μ（总球锚定大小球线、净胜球锚定让球盘），再看残差是否还有优势
import csv, io, json, math, pathlib, urllib.request, datetime, traceback

LG = {
    "英超": "E0", "意甲": "I1", "西甲": "SP1", "法甲": "F1", "法乙": "F2",
    "德甲": "D1", "德乙": "D2", "荷甲": "N1", "英冠": "E1", "葡超": "P1", "苏超": "SC0",
}
SEASON = "2627"
URL = "https://www.football-data.co.uk/mmz4281/{}/{}.csv"
COEF = (1.09, 0.91)
K = 0.95
THR = (0.03, 0.05, 0.08)
MAT = 6
WG = (0.0, 0.25, 0.5, 0.75, 1.0)


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
            if i > j:
                p1 += p
            elif i == j:
                px += p
            else:
                p2 += p
    if tot <= 0:
        return None
    return dict(p1=p1 / tot, px=px / tot, p2=p2 / tot, tg=[x / tot for x in tg],
                o25=1 - sum(tg[:3]) / tot, u25=sum(tg[:3]) / tot,
                mar={k: v / tot for k, v in mar.items()})


def norm(*ps):
    inv = [1 / p for p in ps if p]
    s = sum(inv)
    return [x / s for x in inv] if s > 0 else None


def p_over(t):
    return 1.0 - math.exp(-t) * (1.0 + t + t * t / 2.0)


def solve_T(target):
    """反解：市场 P(大2.5) -> 总球期望 T"""
    lo, hi = 0.3, 9.0
    for _ in range(45):
        mid = (lo + hi) / 2.0
        if p_over(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def solve_S(T, ah, target):
    """反解：市场 P(主让球赢) -> 净胜球期望 S"""
    lo = max(-T + 0.4, -6.0)
    hi = min(T - 0.4, 6.0)
    if lo >= hi:
        return 0.0
    for _ in range(32):
        mid = (lo + hi) / 2.0
        lam = (T + mid) / 2.0
        mu = (T - mid) / 2.0
        pr = probs(lam, mu)
        if not pr:
            return 0.0
        m = pr["mar"]
        w1 = sum(v for k, v in m.items() if k > -ah)
        pu = sum(v for k, v in m.items() if k == -ah)
        val = w1 + pu / 2.0
        if val < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


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
        lam *= COEF[0]
        mu *= COEF[1]

        def form(t):
            rs = t["r"][-6:]
            if not rs:
                return 1.0
            pts = sum(3 if x == "W" else (1 if x == "D" else 0) for x in rs)
            r2 = 0.85 + 0.30 * (pts / (3.0 * len(rs)))
            return min(1.10, max(0.90, r2))

        st = (form(th) + form(ta)) / 2
        lam = (K * lam + (1 - K) * bh) * st
        mu = (K * mu + (1 - K) * ba) * st

        o1 = [fl(r.get(k)) for k in ("B365H", "B365D", "B365A")]
        oo = [fl(r.get("B365>2.5")), fl(r.get("B365<2.5"))]
        ah, ahh, aha = fs(r.get("AHh")), fl(r.get("B365AHH")), fl(r.get("B365AHA"))

        m1 = norm(*o1) if all(o1) else None
        mo = norm(*oo) if all(oo) else None
        mah = norm(ahh, aha) if (ahh and aha) else None

        tm = solve_T(mo[0]) if mo else None
        base_t = tm if tm is not None else (lam + mu)
        sm = solve_S(base_t, ah, mah[0]) if (ah is not None and mah) else None

        row = dict(lg=code, d=r.get("Date"), h=H, a=A, ft=[hs, as_],
                   lam=round(lam, 3), mu=round(mu, 3), tm=tm, sm=sm,
                   nh=len(th["H"]) + len(th["A"]), na=len(ta["H"]) + len(ta["A"]),
                   res=("W" if hs > as_ else ("D" if hs == as_ else "L")))
        if m1:
            row["m1"] = m1
            row["o1"] = o1
        if mo:
            row["mo"] = mo
            row["oo"] = oo
        if ah is not None and ahh and aha:
            row.update(ah=ah, ahh=ahh, aha=aha, mah=mah)
        res.append(row)
        th["H"].append((hs, as_))
        ta["A"].append((as_, hs))
        th["r"].append("W" if hs > as_ else ("D" if hs == as_ else "L"))
        ta["r"].append("W" if as_ > hs else ("D" if as_ == hs else "L"))
    return res


def recompute(rows, w):
    """w=0 纯模型；w=1 完全锚定市场"""
    out = []
    for r in rows:
        t0 = r["lam"] + r["mu"]
        s0 = r["lam"] - r["mu"]
        t = t0 if r.get("tm") is None else (1 - w) * t0 + w * r["tm"]
        s = s0 if r.get("sm") is None else (1 - w) * s0 + w * r["sm"]
        lam = max(0.15, (t + s) / 2.0)
        mu = max(0.15, (t - s) / 2.0)
        pr = probs(lam, mu)
        if not pr:
            continue
        nr = dict(r)
        nr["lam2"] = round(lam, 3)
        nr["mu2"] = round(mu, 3)
        nr.update(p1=pr["p1"], px=pr["px"], p2=pr["p2"], o25=pr["o25"],
                  g01=pr["tg"][0] + pr["tg"][1],
                  g23=pr["tg"][2] + pr["tg"][3], g46=sum(pr["tg"][4:]))
        if r.get("ah") is not None:
            m = pr["mar"]
            ah = r["ah"]
            win = sum(v for k, v in m.items() if k > -ah)
            push = sum(v for k, v in m.items() if k == -ah)
            nr["ahh_p"] = win + push / 2.0
        out.append(nr)
    return out


def sides_of(r, key):
    if key == "ou":
        return [(r["o25"], r["oo"][0]), (1 - r["o25"], r["oo"][1])]
    return [(r["ahh_p"], r["ahh"]), (1 - r["ahh_p"], r["aha"])]


def back_roi(rows, key, th, reverse=False):
    bn = hit = 0
    u = 0.0
    for r in rows:
        if key == "ou":
            if "o25" not in r or "oo" not in r:
                continue
            over = r["ft"][0] + r["ft"][1] > 2.5
        else:
            if "ahh_p" not in r:
                continue
            mar = r["ft"][0] - r["ft"][1]
            if mar == -r["ah"]:
                continue
        sides = sides_of(r, key)
        for i, (p, od) in enumerate(sides):
            if p * od - 1 < th:
                continue
            j = 1 - i if reverse else i
            if key == "ou":
                od2 = r["oo"][j]
                won = (j == 0) == over
            else:
                od2 = r["ahh"] if j == 0 else r["aha"]
                won = (j == 0) == (mar > -r["ah"])
            bn += 1
            hit += 1 if won else 0
            u += (od2 - 1) if won else -1.0
    return dict(bets=bn, hit=hit, units=round(u, 2), roi=round(u / bn, 4) if bn else None)


def metrics(rows, rev=True):
    m = {}
    n12 = brier_m = brier_k = acc_m = acc_k = 0.0
    for r in rows:
        if "p1" not in r or "m1" not in r:
            continue
        ps = (r["p1"], r["px"], r["p2"])
        act = ("W", "D", "L").index(r["res"])
        n12 += 1
        acc_m += 1 if max(range(3), key=lambda i: ps[i]) == act else 0
        acc_k += 1 if max(range(3), key=lambda i: r["m1"][i]) == act else 0
        brier_m += sum((ps[i] - (1 if i == act else 0)) ** 2 for i in range(3))
        brier_k += sum((r["m1"][i] - (1 if i == act else 0)) ** 2 for i in range(3))
    if n12:
        m["1x2"] = dict(n=int(n12), acc_model=round(acc_m / n12, 4),
                        acc_mkt=round(acc_k / n12, 4),
                        brier_model=round(brier_m / n12, 4),
                        brier_mkt=round(brier_k / n12, 4))

    n = hit = 0
    for r in rows:
        if "o25" not in r or "mo" not in r:
            continue
        over = r["ft"][0] + r["ft"][1] > 2.5
        n += 1
        hit += 1 if ((r["o25"] > 0.5) == over) else 0
    if n:
        m["ou25"] = dict(n=int(n), hit=round(hit / n, 4))
        roi = {}
        revb = {}
        for th in THR:
            k = "ev" + str(int(th * 100))
            roi[k] = back_roi(rows, "ou", th, reverse=False)
            if rev:
                revb[k] = back_roi(rows, "ou", th, reverse=True)
        m["ou25"]["roi"] = roi
        if rev:
            m["ou25"]["rev"] = revb

    n = hit = 0
    for r in rows:
        if "ahh_p" not in r:
            continue
        mar = r["ft"][0] - r["ft"][1]
        n += 1
        if mar == -r["ah"]:
            hit += 0.5
        else:
            hit += 1 if ((r["ahh_p"] > 0.5) == (mar > -r["ah"])) else 0
    if n:
        m["ah"] = dict(n=int(n), hit=round(hit / n, 4))
        roi = {}
        revb = {}
        for th in THR:
            k = "ev" + str(int(th * 100))
            roi[k] = back_roi(rows, "ah", th, reverse=False)
            if rev:
                revb[k] = back_roi(rows, "ah", th, reverse=True)
        m["ah"]["roi"] = roi
        if rev:
            m["ah"]["rev"] = revb

    n = hit = 0
    for r in rows:
        if "g01" not in r:
            continue
        t = r["ft"][0] + r["ft"][1]
        k = 0 if t <= 1 else (1 if t <= 3 else 2)
        n += 1
        ps = (r["g01"], r["g23"], r["g46"])
        hit += 1 if max(range(3), key=lambda i: ps[i]) == k else 0
    if n:
        m["tg3"] = dict(n=int(n), hit=round(hit / n, 4))
    return m


def light(rows):
    m = metrics(rows, rev=False)
    e = {}
    if "1x2" in m:
        k = m["1x2"]
        e["1x2"] = dict(n=k["n"], am=k["acc_model"], ak=k["acc_mkt"],
                        bm=k["brier_model"], bk=k["brier_mkt"])
    if "ou25" in m:
        e["ou25"] = dict(n=m["ou25"]["n"], hit=m["ou25"]["hit"], ev8=m["ou25"]["roi"]["ev8"])
    if "ah" in m:
        e["ah"] = dict(n=m["ah"]["n"], hit=m["ah"]["hit"], ev8=m["ah"]["roi"]["ev8"])
    if "tg3" in m:
        e["tg3"] = m["tg3"]
    return e


def main():
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = {"fetched": now, "leagues": {}, "errors": []}
    allrows, per = [], {}
    for lg, code in LG.items():
        try:
            txt = dl(URL.format(SEASON, code))
            rows = run(code, list(csv.DictReader(io.StringIO(txt))))
            out["leagues"][lg] = len(rows)
            per[lg] = rows
            allrows += rows
            print("BT-OK", lg, code, len(rows))
        except Exception:
            out["errors"].append(lg + ": " + traceback.format_exc()[-300:])
            print("BT-ERR", lg, code, traceback.format_exc()[-160:])

    out["n_rows"] = len(allrows)
    out["anchor_n"] = dict(
        tm=sum(1 for r in allrows if r.get("tm") is not None),
        sm=sum(1 for r in allrows if r.get("sm") is not None),
        ah=sum(1 for r in allrows if r.get("ah") is not None),
    )

    r0 = recompute(allrows, 0.0)
    out["metrics"] = metrics(r0, rev=True)
    mature = [r for r in allrows if r.get("nh", 0) >= MAT and r.get("na", 0) >= MAT]
    out["n_mature"] = len(mature)
    out["metrics_mature"] = metrics(recompute(mature, 0.0), rev=True)

    grid = {}
    for w in WG:
        grid["%.2f" % w] = light(recompute(allrows, w))
    out["grid"] = grid

    best_w, best = 0.0, -99
    for w in WG:
        a = grid["%.2f" % w].get("ah", {}).get("ev8", {})
        b = a.get("bets", 0)
        v = a.get("roi")
        if v is None or b < 150:
            continue
        if v > best:
            best, best_w = v, w
    out["best_w"] = best_w
    out["per_league_best"] = {lg: light(recompute(rows, best_w)) for lg, rows in per.items()}
    out["per_league_raw"] = {lg: light(recompute(rows, 0.0)) for lg, rows in per.items()}

    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/bt.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({"grid": out["grid"], "anchor_n": out["anchor_n"],
                      "best_w": out["best_w"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("BT-TOPFAIL", traceback.format_exc()[-400:])
