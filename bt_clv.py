# 大样本回测 v5：线动 / CLV 研究（开盘价 -> 收盘价）
# 目标：每个盘口都给"方向"结论——热钱往哪边流、跟随/反向能不能赚、收盘价哪一档还定错价
import csv, io, json, math, pathlib, urllib.request, datetime, traceback

LG = {
    "英超": "E0", "意甲": "I1", "西甲": "SP1", "法甲": "F1", "法乙": "F2",
    "德甲": "D1", "德乙": "D2", "荷甲": "N1", "英冠": "E1", "葡超": "P1", "苏超": "SC0",
}
SEASON = "2627"
URL = "https://www.football-data.co.uk/mmz4281/{}/{}.csv"
COEF = (1.09, 0.91)
K = 0.95
THR = 0.08          # 模型下注门槛 EV>=8%
MAT = 6
ZB = 2.0            # 显著性门槛（~95%）


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
            tg[min(i + j, 7)] += p
            mar[i - j] = mar.get(i - j, 0.0) + p
            if i > j:
                p1 += p
            elif i == j:
                px += p
            else:
                p2 += p
    if tot <= 0:
        return None
    d = dict(p1=p1 / tot, px=px / tot, p2=p2 / tot, tg=[x / tot for x in tg],
             o25=1 - sum(tg[:3]) / tot, mar={k: v / tot for k, v in mar.items()})
    d["g"] = (sum(d["tg"][0:2]), sum(d["tg"][2:4]), sum(d["tg"][4:]))
    return d


def dv(*odds):
    """去掉水位：1/赔率 归一化"""
    inv = [1 / o for o in odds if o]
    if len(inv) != len(odds):
        return None
    s = sum(inv)
    return [x / s for x in inv] if s > 0 else None


def p_over(t):
    return 1.0 - math.exp(-t) * (1.0 + t + t * t / 2.0)


def solve_T(target):
    """市场 P(大2.5) -> 总球期望 T"""
    lo, hi = 0.3, 9.0
    for _ in range(45):
        mid = (lo + hi) / 2.0
        if p_over(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def solve_S(T, ah, target):
    """市场 P(主让 ah 赢) -> 净胜球期望 S"""
    lo, hi = max(-T + 0.4, -6.0), min(T - 0.4, 6.0)
    if lo >= hi:
        return 0.0
    for _ in range(32):
        mid = (lo + hi) / 2.0
        lam, mu = (T + mid) / 2.0, (T - mid) / 2.0
        pr = probs(lam, mu)
        if not pr:
            return 0.0
        m = pr["mar"]
        v = sum(x for k, x in m.items() if k > -ah) + sum(x for k, x in m.items() if k == -ah) / 2.0
        if v < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def p_cover(lam, mu, ah):
    pr = probs(lam, mu)
    if not pr:
        return None
    m = pr["mar"]
    return sum(x for k, x in m.items() if k > -ah) + sum(x for k, x in m.items() if k == -ah) / 2.0


def gather(code, rd):
    """逐场：模型 λ/μ（纯模型）+ 开盘/收盘去水概率 + T/S + 三档"""
    teams, out = {}, []
    for r in rd:
        if not (r.get("FTHG") and r.get("FTAG")):
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

        def avg(arr, k):
            return (sum(x[k] for x in arr) / len(arr)) if arr else None

        def fb(v, b):
            return v if v is not None else b

        h_att, h_def = avg(th["H"], 0), avg(th["H"], 1)
        a_att, a_def = avg(ta["A"], 0), avg(ta["A"], 1)
        lam = 0.5 * (fb(h_att, bh) * fb(a_def, bad) / bh) + 0.5 * bh
        mu = 0.5 * (fb(a_att, ba) * fb(h_def, bhd) / ba) + 0.5 * ba
        lam *= COEF[0]
        mu *= COEF[1]

        def form(t):
            rs = t["r"][-6:]
            if not rs:
                return 1.0
            pts = sum(3 if x == "W" else (1 if x == "D" else 0) for x in rs)
            return min(1.10, max(0.90, 0.85 + 0.30 * (pts / (3.0 * len(rs)))))

        st = (form(th) + form(ta)) / 2
        lam = (K * lam + (1 - K) * bh) * st
        mu = (K * mu + (1 - K) * ba) * st

        row = dict(lg=code, d=r.get("Date"), h=H, a=A, ft=[hs, as_],
                   res=("W" if hs > as_ else ("D" if hs == as_ else "L")),
                   lam=lam, mu=mu)

        # 1x2 开盘 / 收盘
        oo = [fl(r.get(k)) for k in ("B365H", "B365D", "B365A")]
        cc = [fl(r.get(k)) for k in ("B365CH", "B365CD", "B365CA")]
        if all(oo) and all(cc):
            a_, b_ = dv(*oo), dv(*cc)
            if a_ and b_:
                row["p1o"], row["pxo"], row["p2o"] = a_
                row["p1c"], row["pxc"], row["p2c"] = b_
                row["o1o"], row["o1c"] = oo, cc

        # 大小球 2.5 开盘 / 收盘
        so = [fl(r.get("B365>2.5")), fl(r.get("B365<2.5"))]
        sc = [fl(r.get("B365C>2.5")), fl(r.get("B365C<2.5"))]
        if all(so) and all(sc):
            a_, b_ = dv(*so), dv(*sc)
            if a_ and b_:
                row["po_o"], row["pu_o"] = a_
                row["po_c"], row["pu_c"] = b_
                row["oo_o"], row["oo_c"] = so, sc
                row["T_o"] = solve_T(a_[0])
                row["T_c"] = solve_T(b_[0])
                row["dT"] = row["T_c"] - row["T_o"]

        # 亚盘 开盘 / 收盘（线可能移动，用 S 对齐）
        ah_o, hho, aho = fs(r.get("AHh")), fl(r.get("B365AHH")), fl(r.get("B365AHA"))
        ah_c, hhc, ahc = fs(r.get("AHCh")), fl(r.get("B365CAHH")), fl(r.get("B365CAHA"))
        if None not in (ah_o, hho, aho):
            z = dv(hho, aho)
            if z:
                row.update(ah_o=ah_o, aho=hho, aha=aho, pah_o=z[0])
                T = row.get("T_o") or (lam + mu)
                row["S_o"] = solve_S(T, ah_o, z[0])
        if None not in (ah_c, hhc, ahc):
            z = dv(hhc, ahc)
            if z:
                row.update(ah_c=ah_c, cahh=hhc, caha=ahc, pah_c=z[0])
                T = row.get("T_c") or (lam + mu)
                row["S_c"] = solve_S(T, ah_c, z[0])
                row["dLine"] = ah_c - ah_o if ah_o is not None else None
                if "S_o" in row:
                    row["dS"] = row["S_c"] - row["S_o"]

        # 三档（开盘 / 收盘的锚定分布）
        for tag in ("o", "c"):
            T, S = row.get("T_" + tag), row.get("S_" + tag)
            if T is None or S is None:
                continue
            l1, m1 = max(0.15, (T + S) / 2.0), max(0.15, (T - S) / 2.0)
            pr = probs(l1, m1)
            if pr:
                row["g" + tag] = pr["g"]
                row["lam" + tag], row["mu" + tag] = l1, m1

        out.append(row)
        th["H"].append((hs, as_))
        ta["A"].append((as_, hs))
        th["r"].append("W" if hs > as_ else ("D" if hs == as_ else "L"))
        ta["r"].append("W" if as_ > hs else ("D" if as_ == hs else "L"))
    return out


def st(vals):
    """均值 + 标准误 + z"""
    v = [x for x in vals if x is not None]
    n = len(v)
    if n < 2:
        return dict(n=n, mean=None, se=None, z=None)
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    se = sd / math.sqrt(n)
    return dict(n=n, mean=round(m, 4), se=round(se, 4), z=round(m / se, 2) if se else None)


def verdict(z, tag_pos, tag_neg):
    if z is None or abs(z) < ZB:
        return "无稳定方向（噪声）"
    return tag_pos if z > 0 else tag_neg


# ---------------- 1x2 ----------------
def an_1x2(rows):
    d1, dx, d2 = [], [], []
    cnt = dict(steamH=0, steamD=0, steamA=0)
    fu, fa = [], []
    hit_o = hit_c = brier_o = brier_c = brier_m = 0.0
    nacc = 0
    for r in rows:
        if "p1o" not in r:
            continue
        dd = (r["p1c"] - r["p1o"], r["pxc"] - r["pxo"], r["p2c"] - r["p2o"])
        d1.append(dd[0] * 100)
        dx.append(dd[1] * 100)
        d2.append(dd[2] * 100)
        k = max(range(3), key=lambda i: dd[i])
        cnt[["steamH", "steamD", "steamA"][k]] += 1
        act = ("W", "D", "L").index(r["res"])
        # 跟随热钱：开盘价买被 steam 的一边
        od = r["o1o"][k]
        fu.append(od - 1 if k == act else -1.0)
        # 反向：买最被看淡的一边
        j = min(range(3), key=lambda i: dd[i])
        fa.append(r["o1o"][j] - 1 if j == act else -1.0)
        po = (r["p1o"], r["pxo"], r["p2o"])
        pc = (r["p1c"], r["pxc"], r["p2c"])
        pm = probs(r["lam"], r["mu"])
        brier_o += sum((po[i] - (1 if i == act else 0)) ** 2 for i in range(3))
        brier_c += sum((pc[i] - (1 if i == act else 0)) ** 2 for i in range(3))
        if pm:
            brier_m += sum(([pm["p1"], pm["px"], pm["p2"]][i] - (1 if i == act else 0)) ** 2 for i in range(3))
        hit_o += 1 if max(range(3), key=lambda i: po[i]) == act else 0
        hit_c += 1 if max(range(3), key=lambda i: pc[i]) == act else 0
        nacc += 1
    out = dict(n=len(fu),
               move_pp=dict(H=st(d1), D=st(dx), A=st(d2)),
               steam_share={k: round(v / max(1, len(fu)), 4) for k, v in cnt.items()},
               follow=st(fu), fade=st(fa),
               acc_open=round(hit_o / nacc, 4) if nacc else None,
               acc_close=round(hit_c / nacc, 4) if nacc else None,
               brier_open=round(brier_o / nacc, 4) if nacc else None,
               brier_close=round(brier_c / nacc, 4) if nacc else None,
               brier_model=round(brier_m / nacc, 4) if nacc else None)
    out["verdict"] = verdict(out["follow"]["z"],
                             "跟随热钱有方向（开盘买被 steam 那边为正）",
                             "热钱是反指标（反向为正）")
    out["verdict_move"] = verdict(out["move_pp"]["H"]["z"],
                                  "热钱平均流向主队（主胜概率被推高）",
                                  "热钱平均流向客队（主胜概率被压低）")
    if out["move_pp"]["D"]["z"] is not None and abs(out["move_pp"]["D"]["z"]) >= ZB:
        out["verdict_move"] += " / 平局概率显著" + ("抬升" if out["move_pp"]["D"]["z"] > 0 else "下滑")
    return out


# ---------------- 大小球 ----------------
def an_ou(rows):
    dT, follow, fade = [], [], []
    hit_o = hit_c = 0.0
    n = 0
    for r in rows:
        if "dT" not in r:
            continue
        dT.append(r["dT"])
        over = r["ft"][0] + r["ft"][1] > 2.5
        side_over = r["dT"] > 0
        fo = r["oo_o"][0] if side_over else r["oo_o"][1]
        follow.append(fo - 1 if side_over == over else -1.0)
        od2 = r["oo_o"][1] if side_over else r["oo_o"][0]
        fade.append(od2 - 1 if (not side_over) == over else -1.0)
        hit_o += 1 if ((r["po_o"] > 0.5) == over) else 0
        hit_c += 1 if ((r["po_c"] > 0.5) == over) else 0
        n += 1
    out = dict(n=n, move_T=st(dT),
               follow=st(follow), fade=st(fade),
               acc_open=round(hit_o / n, 4) if n else None,
               acc_close=round(hit_c / n, 4) if n else None)
    zt = out["move_T"]["z"]
    out["verdict_move"] = ("总球预期被推高（线往大球走）" if (zt or 0) > 0 else "总球预期被压低（线往小球走）")
    out["verdict"] = verdict(out["follow"]["z"],
                             "跟随线动有方向", "线动是反指标")
    return out


# ---------------- 亚盘 ----------------
def an_ah(rows):
    dS, dl, follow, fade = [], [], [], []
    n = 0
    for r in rows:
        if "dS" not in r:
            continue
        dS.append(r["dS"])
        dl.append(r["dLine"] or 0.0)
        mar = r["ft"][0] - r["ft"][1]
        side_home = r["dS"] > 0
        fo = r["aho"] if side_home else r["aha"]
        fade_o = r["aha"] if side_home else r["aho"]
        if mar == -r["ah_o"]:
            follow.append(0.0)
            fade.append(0.0)
        else:
            won = (mar > -r["ah_o"])
            follow.append(fo - 1 if (side_home == won) else -1.0)
            fade.append(fade_o - 1 if ((not side_home) == won) else -1.0)
        n += 1
    out = dict(n=n, move_S=st(dS), move_line=st(dl),
               follow=st(follow), fade=st(fade),
               share_home_steam=round(sum(1 for x in dS if x > 0) / max(1, len(dS)), 4))
    out["verdict_move"] = ("盘口往主队方向抬（主队被压）" if (out["move_S"]["z"] or 0) > ZB
                           else ("盘口往客队方向抬（客队被压）" if (out["move_S"]["z"] or 0) < -ZB
                                 else "净胜球预期基本不动（盘口只微调）"))
    out["verdict"] = verdict(out["follow"]["z"],
                             "跟随盘口移动有方向（开盘买被抬的一边）",
                             "盘口移动是反指标（反向为正）")
    return out


# ---------------- 总进球三档 ----------------
def an_tg3(rows):
    dm = ([], [], [])
    rows2 = [r for r in rows if "go" in r and "gc" in r]
    for r in rows2:
        for i in range(3):
            dm[i].append((r["gc"][i] - r["go"][i]) * 100)
    res = [dict(mean=st(dm[i])["mean"], z=st(dm[i])["z"], se=st(dm[i])["se"]) for i in range(3)]
    steam_hit = steam_n = 0
    steam_imp = 0.0
    real_cnt = [0, 0, 0]
    imp_sum = [0.0, 0.0, 0.0]
    for r in rows2:
        k = 0 if sum(r["ft"]) <= 1 else (1 if sum(r["ft"]) <= 3 else 2)
        real_cnt[k] += 1
        for i in range(3):
            imp_sum[i] += r["gc"][i]
        s = max(range(3), key=lambda i: r["gc"][i] - r["go"][i])
        steam_n += 1
        steam_hit += 1 if s == k else 0
        steam_imp += r["gc"][s]
    n_all = max(1, len(rows2))
    out = dict(n=len(rows2), move_pp=dict(g01=res[0], g23=res[1], g46=res[2]),
               steam_hit=round(steam_hit / max(1, steam_n), 4),
               steam_implied=round(steam_imp / max(1, steam_n), 4),
               realized=[round(c / n_all, 4) for c in real_cnt],
               implied_close=[round(v / n_all, 4) for v in imp_sum])
    steam_gap = out["steam_hit"] - out["steam_implied"]
    out["steam_gap_pp"] = round(steam_gap * 100, 2)
    out["steam_gap_se_pp"] = round(math.sqrt(max(out["steam_implied"] * (1 - out["steam_implied"]), 1e-9)
                                             / max(1, steam_n)) * 100, 2)
    # 被抬最高那一档，胜率比它的收盘隐含低 -> 线动方向不可跟随
    big = max(range(3), key=lambda i: abs(res[i]["mean"] or 0))
    out["big_move"] = ["0-1", "2-3", "4+"][big]
    out["verdict_move"] = ("被抬档=" + out["big_move"] + "（%+.2fpp）" % (res[big]["mean"] or 0))
    gap = [round(out["realized"][i] - out["implied_close"][i], 4) for i in range(3)]
    out["gap_real_minus_close"] = gap
    sg, ss = out["steam_gap_pp"], out["steam_gap_se_pp"]
    out["verdict"] = ("跟随三档线动是反指标（被抬档胜率比隐含低 %.1fpp）" % (-sg) if sg < -ZB * ss
                      else ("跟随三档线动有效" if sg > ZB * ss else "三档线动无稳定方向"))
    return out


# ---------------- 收盘价分档放大镜 ----------------
BINS = [(-99, -0.06), (-0.06, -0.02), (-0.02, 0.02), (0.02, 0.06), (0.06, 99)]


def move_bands(rows):
    """线动幅度分档：多大幅度的移动才值得跟"""
    res = {"ou": [], "x12": [], "ah": []}
    for lo, hi in BINS:
        # 大小球
        n = h = 0
        fu, fa = [], []
        for r in rows:
            if "dT" not in r or not (lo <= r["dT"] < hi):
                continue
            over = r["ft"][0] + r["ft"][1] > 2.5
            side_over = r["dT"] > 0
            n += 1
            h += 1 if (side_over == over) else 0
            fo = r["oo_o"][0] if side_over else r["oo_o"][1]
            fa_od = r["oo_o"][1] if side_over else r["oo_o"][0]
            fu.append(fo - 1 if side_over == over else -1.0)
            fa.append(fa_od - 1 if (not side_over) == over else -1.0)
        if n >= 40:
            z1 = z2 = None
            if n > 1:
                m1 = sum(fu) / n; sd1 = (sum((x - m1) ** 2 for x in fu) / (n - 1)) ** .5
                z1 = round(m1 / (sd1 / n ** .5), 2) if sd1 else None
                m2 = sum(fa) / n; sd2 = (sum((x - m2) ** 2 for x in fa) / (n - 1)) ** .5
                z2 = round(m2 / (sd2 / n ** .5), 2) if sd2 else None
            res["ou"].append(dict(band="ΔT %+.2f~%+.2f" % (lo, hi), n=n,
                                  follow_rate=round(h / n, 4),
                                  follow=round(sum(fu) / n, 4), fz=z1,
                                  fade=round(sum(fa) / n, 4), dz=z2))
        # 胜平负（按主胜概率移动幅度）
        n = h = 0
        fu, fa = [], []
        for r in rows:
            if "p1o" not in r:
                continue
            x = (r["p1c"] - r["p1o"])
            if not (lo <= x < hi):
                continue
            act = ("W", "D", "L").index(r["res"])
            dd = (r["p1c"] - r["p1o"], r["pxc"] - r["pxo"], r["p2c"] - r["p2o"])
            k = max(range(3), key=lambda i: dd[i])
            j = min(range(3), key=lambda i: dd[i])
            n += 1
            h += 1 if k == act else 0
            fu.append(r["o1o"][k] - 1 if k == act else -1.0)
            fa.append(r["o1o"][j] - 1 if j == act else -1.0)
        if n >= 40:
            z1 = z2 = None
            if n > 1:
                m1 = sum(fu) / n; sd1 = (sum((x - m1) ** 2 for x in fu) / (n - 1)) ** .5
                z1 = round(m1 / (sd1 / n ** .5), 2) if sd1 else None
                m2 = sum(fa) / n; sd2 = (sum((x - m2) ** 2 for x in fa) / (n - 1)) ** .5
                z2 = round(m2 / (sd2 / n ** .5), 2) if sd2 else None
            res["x12"].append(dict(band="Δ主胜 %+.2f~%+.2f pp" % (lo * 100, hi * 100), n=n,
                                   steam_hit=round(h / n, 4),
                                   follow=round(sum(fu) / n, 4), fz=z1,
                                   fade=round(sum(fa) / n, 4), dz=z2))
        # 亚盘
        n = h = 0
        fu, fa = [], []
        for r in rows:
            if "dS" not in r or not (lo <= r["dS"] < hi):
                continue
            mar = r["ft"][0] - r["ft"][1]
            side_home = r["dS"] > 0
            if mar == -r["ah_o"]:
                continue
            n += 1
            won = mar > -r["ah_o"]
            h += 1 if (side_home == won) else 0
            fo = r["aho"] if side_home else r["aha"]
            fa_od = r["aha"] if side_home else r["aho"]
            fu.append(fo - 1 if side_home == won else -1.0)
            fa.append(fa_od - 1 if (not side_home) == won else -1.0)
        if n >= 40:
            z1 = None
            if n > 1:
                m1 = sum(fu) / n; sd1 = (sum((x - m1) ** 2 for x in fu) / (n - 1)) ** .5
                z1 = round(m1 / (sd1 / n ** .5), 2) if sd1 else None
            res["ah"].append(dict(band="ΔS %+.2f~%+.2f" % (lo, hi), n=n,
                                  follow_rate=round(h / n, 4),
                                  follow=round(sum(fu) / n, 4), fz=z1,
                                  fade=round(sum(fa) / n, 4)))
    return res


def bands(rows, key, edges):
    """按收盘隐含概率分档，看实际频率 vs 隐含，给方向"""
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        n = h = 0
        imp = 0.0
        u = 0.0
        for r in rows:
            if key == "1x2":
                if "p1c" not in r:
                    continue
                act = ("W", "D", "L").index(r["res"])
                for i, p in enumerate((r["p1c"], r["pxc"], r["p2c"])):
                    if lo <= p < hi:
                        n += 1
                        imp += p
                        won = (i == act)
                        h += 1 if won else 0
                        u += (r["o1c"][i] - 1) if won else -1.0
            elif key == "ou":
                if "po_c" not in r:
                    continue
                over = r["ft"][0] + r["ft"][1] > 2.5
                for i, p in enumerate((r["po_c"], r["pu_c"])):
                    if lo <= p < hi:
                        n += 1
                        imp += p
                        won = ((i == 0) == over)
                        h += 1 if won else 0
                        u += (r["oo_c"][i] - 1) if won else -1.0
            elif key == "ah":
                if "pah_c" not in r:
                    continue
                mar = r["ft"][0] - r["ft"][1]
                if mar == -r["ah_c"]:
                    continue
                for i, p in enumerate((r["pah_c"], 1 - r["pah_c"])):
                    if lo <= p < hi:
                        n += 1
                        imp += p
                        won = ((i == 0) == (mar > -r["ah_c"]))
                        h += 1 if won else 0
                        od = r["cahh"] if i == 0 else r["caha"]
                        u += (od - 1) if won else -1.0
        if n < 30:
            continue
        real, im = h / n, imp / n
        se = math.sqrt(max(im * (1 - im), 1e-9) / n) * 100
        gap = (real - im) * 100
        out.append(dict(band="%d-%d%%" % (lo * 100, hi * 100), n=n,
                        implied=round(im, 4), real=round(real, 4),
                        gap_pp=round(gap, 2), se_pp=round(se, 2),
                        z=round(gap / se, 2) if se else None,
                        roi=round(u / n, 4),
                        verdict=("买这边（实际高于收盘隐含）" if gap > ZB * se
                                 else ("卖这边（实际低于收盘隐含）" if gap < -ZB * se else "定准"))))
    return out


# ---------------- 模型下注的 CLV ----------------
def model_clv(rows, market):
    """模型(纯模型 λ/μ)在开盘价 EV>=8% 触发，看是否打败收盘线"""
    clv, u = [], []
    for r in rows:
        if market == "1x2":
            if "p1c" not in r:
                continue
            pm = probs(r["lam"], r["mu"])
            if not pm:
                continue
            ps = (pm["p1"], pm["px"], pm["p2"])
            for i, p in enumerate(ps):
                if p * r["o1o"][i] - 1 < THR:
                    continue
                act = ("W", "D", "L").index(r["res"])
                clv.append((r["o1o"][i] * (r["p1c"], r["pxc"], r["p2c"])[i] - 1) * 100)
                u.append((r["o1o"][i] - 1) if i == act else -1.0)
        elif market == "ou":
            if "dT" not in r:
                continue
            pr = probs(r["lam"], r["mu"])
            if not pr:
                continue
            over = r["ft"][0] + r["ft"][1] > 2.5
            for i, p in enumerate((pr["o25"], 1 - pr["o25"])):
                if p * r["oo_o"][i] - 1 < THR:
                    continue
                pc = (r["po_c"], r["pu_c"])[i]
                clv.append((r["oo_o"][i] * pc - 1) * 100)
                u.append((r["oo_o"][i] - 1) if ((i == 0) == over) else -1.0)
        else:
            if "dS" not in r or "lamc" not in r:
                continue
            pr = probs(r["lam"], r["mu"])
            if not pr:
                continue
            mar = r["ft"][0] - r["ft"][1]
            if mar == -r["ah_o"]:
                continue
            pm = p_cover(r["lam"], r["mu"], r["ah_o"])
            if pm is None:
                continue
            for i, p in enumerate((pm, 1 - pm)):
                od = r["aho"] if i == 0 else r["aha"]
                if p * od - 1 < THR:
                    continue
                # 收盘公平概率：用收盘 λ/μ 在同一盘口线评估
                pc = p_cover(r["lamc"], r["muc"], r["ah_o"])
                if pc is None:
                    continue
                pc = pc if i == 0 else 1 - pc
                clv.append((od * pc - 1) * 100)
                u.append((od - 1) if ((i == 0) == (mar > -r["ah_o"])) else -1.0)
    out = dict(clv_pct=st(clv), units=st(u))
    out["roi"] = round(sum(u) / len(u), 4) if u else None
    out["hit"] = round(sum(1 for x in u if x > 0) / len(u), 4) if u else None
    z = out["clv_pct"]["z"]
    out["verdict"] = ("模型抢在市场前面（CLV 显著为正）" if (z or 0) > ZB
                      else ("模型落后市场（CLV 显著为负，是跟风）" if (z or 0) < -ZB
                            else "模型与收盘线同步，无领先信息"))
    return out


def main():
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = {"fetched": now, "leagues": {}, "errors": []}
    allrows, per = [], {}
    for lg, code in LG.items():
        try:
            txt = dl(URL.format(SEASON, code))
            rows = gather(code, list(csv.DictReader(io.StringIO(txt))))
            out["leagues"][lg] = len(rows)
            per[lg] = rows
            allrows += rows
            print("CLV-OK", lg, code, len(rows))
        except Exception:
            out["errors"].append(lg + ": " + traceback.format_exc()[-300:])
            print("CLV-ERR", lg, code, traceback.format_exc()[-160:])

    out["n_rows"] = len(allrows)
    out["cov"] = dict(
        x12=sum(1 for r in allrows if "p1o" in r),
        ou=sum(1 for r in allrows if "dT" in r),
        ah=sum(1 for r in allrows if "dS" in r),
        tg3=sum(1 for r in allrows if "go" in r and "gc" in r),
        line_moved=sum(1 for r in allrows if r.get("dLine") not in (None, 0)),
    )

    out["m1"] = an_1x2(allrows)
    out["ou"] = an_ou(allrows)
    out["ah"] = an_ah(allrows)
    out["tg3"] = an_tg3(allrows)

    edges = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    out["bands"] = dict(x12=bands(allrows, "1x2", edges),
                        ou=bands(allrows, "ou", edges),
                        ah=bands(allrows, "ah", edges))
    out["move_bands"] = move_bands(allrows)

    out["model"] = dict(x12=model_clv(allrows, "1x2"),
                        ou=model_clv(allrows, "ou"),
                        ah=model_clv(allrows, "ah"))

    out["per_league"] = {}
    for lg, rows in per.items():
        e = dict(n=len(rows))
        a = an_1x2(rows)
        e["dP1_pp"] = a["move_pp"]["H"]["mean"]
        e["follow_1x2"] = (a["follow"]["mean"], a["follow"]["z"], a["follow"]["n"])
        b = an_ou(rows)
        e["dT"] = b["move_T"]["mean"]
        e["follow_ou"] = (b["follow"]["mean"], b["follow"]["z"], b["follow"]["n"])
        c = an_ah(rows)
        e["dS"] = c["move_S"]["mean"]
        e["follow_ah"] = (c["follow"]["mean"], c["follow"]["z"], c["follow"]["n"])
        e["brier_o"] = a["brier_open"]
        e["brier_c"] = a["brier_close"]
        out["per_league"][lg] = e

    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/clv.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print("\n===== 线动 / CLV 结论 =====")
    print(json.dumps({k: out[k] for k in ("cov", "m1", "ou", "ah", "tg3", "model", "move_bands")},
                     ensure_ascii=False, indent=1))
    print("\n被拒绝的假设：见 model 段 CLV")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("CLV-TOPFAIL", traceback.format_exc()[-500:])
