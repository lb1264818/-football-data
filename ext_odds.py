# -*- coding: utf-8 -*-
"""
外部欧赔锚接入层 v1  (ext_odds.py)
==================================
给每日竞彩管道补一个独立于竞彩的锚, 全程走 HTTP, 不需要浏览器渲染:

  1) 竞彩官方计算器接口  -> 当日全部在售场次(五池价格)
  2) betexplorer 服务端接口:
       赛程  /football/<area>/<comp>/            页面 JSON-LD 给 eventId / 开赛时间 / 队名
       赔率  /match-odds/<eventId>/1/<bt>/odds/   bt = 1x2 | ah | ou
     每个盘口取 6~20 家博彩公司的中位数, 再按 1/p 归一化去水
  3) 场次对齐: 先按开赛时间 ±3h 圈候选, 再以「去水 1X2 向量」的 L1 距离做最优指派
     (欧冠 04:00 常有 6 场同开, 只靠时间分不开, 必须用赔率向量)
  4) 用外部锚拟合 Dixon-Coles: 1X2 公平概率 + 竞彩让球线附近的亚盘条件概率 + 2.5 大小球
     -> λh / λa / ρ; 再用「外部公平概率 × 竞彩价 − 1」算五池 EV
  5) 四道闸 + 铁律 A(|ρ|<0.25 才算拟合可信, EV 上限 15%)

闸门口径(写明, 避免以后各版本各说一套):
  G1 λμ对盘口 : |模型总球 − 竞彩总进球池去水隐含总球| ≤ 0.25 球
  G2 总球对线 : |模型 P(≥3球) − 外部 2.5 大小球去水概率| ≤ 5pp
  G3 让球档对齐: |模型让球条件概率 − 外部同线亚盘条件概率| ≤ 8pp
  G4 校准后EV : 5% ≤ EV(折 6% 概率) ≤ 15%

输出: data/ext_odds.json + data/ext_odds_log.txt, 并打印到 CI 日志
"""
import json, math, pathlib, re, sys, time, traceback, urllib.request
import concurrent.futures as cf
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import least_squares, linear_sum_assignment

CST = timezone(timedelta(hours=8))
JC_URL = ("https://webapi.sporttery.cn/gateway/jc/football/"
          "getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu&channel=c")
BE = "https://www.betexplorer.com"
UA_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")


def headers(url):
    """竞彩只认手机 UA + m 站 Referer(桌面 UA 直接 567); betexplorer 认桌面 UA"""
    if "sporttery.cn" in url:
        return {"User-Agent": UA_IPHONE, "Referer": "https://m.sporttery.cn/",
                "Accept": "application/json, text/plain, */*"}
    return {"User-Agent": UA_MAC, "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9", "X-Requested-With": "XMLHttpRequest"}
OUT = pathlib.Path("data/ext_odds.json")
LOG = pathlib.Path("data/ext_odds_log.txt")

COMP = {
    "欧冠": "football/europe/champions-league",
    "欧联": "football/europe/europa-league",
    "欧协": "football/europe/conference-league",
    "英超": "football/england/premier-league",
    "英冠": "football/england/championship",
    "英甲": "football/england/league-one",
    "意甲": "football/italy/serie-a",
    "意乙": "football/italy/serie-b",
    "西甲": "football/spain/laliga",
    "西乙": "football/spain/laliga2",
    "德甲": "football/germany/bundesliga",
    "德乙": "football/germany/2-bundesliga",
    "法甲": "football/france/ligue-1",
    "法乙": "football/france/ligue-2",
    "荷甲": "football/netherlands/eredivisie",
    "葡超": "football/portugal/primeira-liga",
    "苏超": "football/scotland/premiership",
    "巴甲": "football/brazil/serie-a",
    "阿甲": "football/argentina/torneo-betano",
    "美职": "football/usa/mls",
    "日职": "football/japan/j1-league",
    "韩职": "football/south-korea/k-league-1",
    "澳超": "football/australia/a-league",
    "挪超": "football/norway/eliteserien",
    "瑞超": "football/sweden/allsvenskan",
    "丹超": "football/denmark/superliga",
    "比甲": "football/belgium/jupiler-league",
    "土超": "football/turkey/super-lig",
    "瑞士超": "football/switzerland/super-league",
    "奥甲": "football/austria/bundesliga",
    "俄超": "football/russia/premier-league",
    "墨超": "football/mexico/liga-mx",
    "沙特": "football/saudi-arabia/saudi-professional-league",
}
# 亚盘/大小球接口里的线值解析
HCP = re.compile(r"E-\d+-\d+-\d+-([-+]?\d+(?:\.\d+)?)-\d+")
ODDS: dict = {}          # eventId -> 1X2 中位数
N = 11


# ----------------------------------------------------------------- HTTP
def fetch(url, timeout=25, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers(url))
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as e:                    # noqa: BLE001
            last = e
            time.sleep(1.2 * (k + 1))
    raise last


def jget(url):
    return json.loads(fetch(url))


# ------------------------------------------------------------- 竞彩场次
def jc_matches():
    d = jget(JC_URL)
    out = []
    for m in d["value"]["matchInfoList"]:
        for s in m["subMatchList"]:
            if s.get("matchStatus") not in (None, "Selling"):
                continue
            try:
                kick = datetime.strptime(s["matchDate"] + " " + s["matchTime"],
                                         "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            except Exception:
                continue
            gl = s["hhad"]["goalLine"]
            line = float(gl) if str(gl).lstrip("+-").replace(".", "").isdigit() else 0.0
            out.append({
                "num": s["matchNumStr"], "lg": s["leagueAbbName"],
                "home": s["homeTeamAbbName"], "away": s["awayTeamAbbName"],
                "kick": kick, "kick_utc": kick.astimezone(timezone.utc),
                "had": {k: float(s["had"][k]) for k in "hda"},
                "line": line,
                "hhad": {k: float(s["hhad"][k]) for k in "hda"},
                "ttg": {int(k[1:]): float(v) for k, v in s["ttg"].items() if re.match(r"^s[0-7]$", k)},
                "hafu": {k: float(v) for k, v in s["hafu"].items() if re.match(r"^[hda][hda]$", k)},
                "crs": {k: float(v) for k, v in s["crs"].items() if re.match(r"^s\d\ds\d\d$", k)},
                "crs_other": {k: float(s["crs"][k]) for k in ("s1sh", "s1sd", "s1sa")
                              if k in s["crs"]},
            })
    return out


def devig(o):
    inv = [1 / x for x in o]
    s = sum(inv)
    return [x / s for x in inv]


# --------------------------------------------------------------- 赛程页
def ld_events(comp):
    html = fetch(f"{BE}/{comp}/")
    evs = []
    for blk in re.findall(r'<script type="application/ld\+json">(.+?)</script>', html, re.S):
        try:
            o = json.loads(blk)
        except Exception:
            continue
        for obj in (o if isinstance(o, list) else [o]):
            if not isinstance(obj, dict) or obj.get("@type") != "SportsEvent":
                continue
            try:
                st = datetime.fromisoformat(obj["startDate"]).astimezone(timezone.utc)
            except Exception:
                continue
            m = re.search(r"/([^/]+)/([A-Za-z0-9]+)/$", obj.get("url", ""))
            if m:
                evs.append({"utc": st, "name": obj.get("name", ""), "id": m.group(2)})
    return evs


# ----------------------------------------------------------------- 赔率
def odds_rows(event_id, bt):
    try:
        h = jget(f"{BE}/match-odds/{event_id}/1/{bt}/odds/?lang=en")["odds"]
    except Exception:
        return []
    rows = []
    for tr in re.findall(r"<tr .+?</tr>", h, re.S):
        bookie = re.search(r'data-bookie="([^"]+)"', tr)
        odds = [float(x) for x in re.findall(r'data-odd="([\d.]+)"', tr)]
        hcp = HCP.search(tr)
        if odds:
            rows.append((bookie.group(1).strip() if bookie else "?", 
                         float(hcp.group(1)) if hcp else None, odds))
    return rows


def med1x2(rows, min_books=6):
    tri = [r[2][:3] for r in rows if len(r[2]) >= 3]
    if len(tri) < min_books:
        return None
    m = [round(float(np.median([t[i] for t in tri])), 3) for i in range(3)]
    inv = [float(np.mean([1 / t[i] for t in tri])) for i in range(3)]
    s = sum(inv)
    return {"x12": m, "fair": [round(x / s, 4) for x in inv], "n": len(tri)}


def med_line(rows, min_books=6):
    by = {}
    for r in rows:
        if r[1] is None or len(r[2]) < 2:
            continue
        by.setdefault(round(r[1], 2), []).append(r[2][:2])
    out = {}
    for line, ps in by.items():
        if len(ps) < min_books:
            continue
        q1 = float(np.median([p[0] for p in ps]))
        q2 = float(np.median([p[1] for p in ps]))
        s = 1 / q1 + 1 / q2
        out[line] = [round(q1, 2), round(q2, 2), len(ps), round((1 / q1) / s, 4)]
    return out


# ------------------------------------------------------------ Dixon-Coles
def grid(lh, la, rho):
    h = np.arange(N)
    ph = np.array([math.exp(-lh) * lh ** k / math.factorial(k) for k in h])
    pa = np.array([math.exp(-la) * la ** k / math.factorial(k) for k in h])
    M = np.outer(ph, pa)
    for i, j in ((0, 0), (0, 1), (1, 0), (1, 1)):
        M[i, j] *= (1 - rho) if i == j else (1 + rho)
    return M / M.sum()


def hda(M):
    H = sum(M[i, j] for i in range(N) for j in range(N) if i > j)
    D = sum(M[i, j] for i in range(N) for j in range(N) if i == j)
    return H, D, 1 - H - D


def cover(M, line):
    win = lose = push = 0.0
    for i in range(N):
        for j in range(N):
            d = (i - j) + line
            if abs(d) < 1e-9:
                push += M[i, j]
            elif d > 0:
                win += M[i, j]
            else:
                lose += M[i, j]
    dec = win + lose
    return (win / dec if dec > 0 else 0.0), push


def p_over(M):
    return sum(M[i, j] for i in range(N) for j in range(N) if i + j >= 3)


def fit_dc(fair, ah, ou, line):
    def res(p):
        lh, la, rho = p
        if not (0.25 < lh < 7 and 0.25 < la < 7 and -0.281 < rho < 0.281):
            return np.array([9.0] * (3 + len(ah) + (1 if ou else 0)))
        M = grid(lh, la, rho)
        H, D, A = hda(M)
        r = [H - fair[0], D - fair[1], A - fair[2]]
        for ln, tv in ah.items():
            r.append(0.8 * (cover(M, ln)[0] - tv))
        if ou:
            r.append(0.8 * (p_over(M) - ou))
        return np.array(r)

    best = None
    for g in ((1.0, 0.8), (1.6, 1.0), (0.7, 0.6), (2.2, 1.4), (1.2, 1.6), (2.6, 0.7)):
        for r0 in (-0.05, 0.02, 0.1):
            try:
                s = least_squares(res, [g[0], g[1], r0],
                                  bounds=([0.2, 0.2, -0.28], [7, 7, 0.28]), max_nfev=400)
            except Exception:
                continue
            if best is None or s.cost < best.cost:
                best = s
    if best is None:
        return None
    lh, la, rho = best.x
    r = res(best.x)
    return {"lh": float(lh), "la": float(la), "rho": float(rho),
            "M": grid(lh, la, rho), "fit_err": float(max(abs(x) for x in r[:3])),
            "ah_err": float(max(abs(x) / 0.8 for x in r[3:])) if len(r) > 3 else 0.0}


# ----------------------------------------------------------------- 对齐
def assign(subs, events, log):
    """subs/events 同联赛; 以「时间窗 + 1X2 向量距离」求最优指派"""
    n, k = len(subs), len(events)
    if not n or not k:
        return {}
    idx = {e["id"]: j for j, e in enumerate(events)}
    cost = np.full((n, k), 9.0)
    for i, m in enumerate(subs):
        dv = np.array(devig([m["had"][x] for x in "hda"]))
        for j, e in enumerate(events):
            dt = abs((e["utc"] - m["kick_utc"]).total_seconds()) / 3600.0
            if dt > 3.0:
                continue
            o = ODDS.get(e["id"])
            if o:
                d = float(np.abs(dv - np.array(o["fair"])).sum()) + 0.04 * dt
            else:                                  # 无赔率: 退回时间成本, 阈值更低
                d = 0.95 + 0.05 * dt
            cost[i, j] = d
    ri, ci = linear_sum_assignment(cost)
    out = {}
    for i, j in zip(ri, ci):
        if cost[i, j] >= 0.85:
            continue
        e = events[j]
        out[subs[i]["num"]] = e
        log.append("  %s %-8s -> %-42s cost=%.3f Δt=%.2fh"
                   % (subs[i]["num"], subs[i]["home"], e["name"], cost[i, j],
                      abs((e["utc"] - subs[i]["kick_utc"]).total_seconds()) / 3600))
    for m in subs:
        if m["num"] not in out:
            log.append("  %s %-8s !! 未对齐(时间窗内候选过少或赔率差过大)" % (m["num"], m["home"]))
    return out


# ----------------------------------------------------------------- 主流程
def main():
    now = datetime.now(CST)
    log = ["ext_odds @ %s" % now.strftime("%F %H:%M CST")]

    def flush():
        # 每阶段就落盘一次: CI 崩在中途也能看到死在哪一步
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text("\n".join(log), encoding="utf-8")
        except Exception:                          # noqa: BLE001
            pass

    subs = jc_matches()
    log.append("竞彩在售 %d 场" % len(subs))
    flush()

    # 赛程(每联赛一次)
    comp_events, events = {}, []
    for m in subs:
        slug = COMP.get(m["lg"])
        if not slug:
            log.append("  !! 联赛未收录: %s (%s) —— 加进 COMP 即可" % (m["lg"], m["num"]))
            continue
        if slug not in comp_events:
            try:
                comp_events[slug] = ld_events(slug)
                log.append("赛程 %-42s %d 场" % (slug, len(comp_events[slug])))
            except Exception as e:                 # noqa: BLE001
                comp_events[slug] = []
                log.append("  !! 赛程抓取失败 %s: %r" % (slug, e))
    for evs in comp_events.values():
        events.extend(evs)
    flush()

    # 1X2 预取(并发)
    def one(e):
        return e["id"], med1x2(odds_rows(e["id"], "1x2"))
    if events:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for eid, o in ex.map(one, events):
                ODDS[eid] = o
    log.append("1X2 预取 %d 场, 成功 %d" % (len(events), sum(1 for v in ODDS.values() if v)))
    flush()

    log.append("对齐:")
    mapping = {}
    for lg in sorted({m["lg"] for m in subs}):
        slug = COMP.get(lg)
        if slug and comp_events.get(slug):
            mapping.update(assign([m for m in subs if m["lg"] == lg],
                                  comp_events[slug], log))

    rows = []
    for m in subs:
        r = {"num": m["num"], "lg": m["lg"], "home": m["home"], "away": m["away"],
             "kick": m["kick"].strftime("%m-%d %H:%M"), "had": m["had"],
             "line": m["line"], "hhad": m["hhad"]}
        ev = mapping.get(m["num"])
        o12 = ODDS.get(ev["id"]) if ev else None
        if not o12:
            r["status"] = "no_anchor"
            r["reason"] = "未对齐到 betexplorer 场次" if not ev else "该场无 1X2 赔率"
            rows.append(r)
            continue

        ah_all = med_line(odds_rows(ev["id"], "ah"))
        ou_all = med_line(odds_rows(ev["id"], "ou"))
        line = m["line"]
        ah = {k: v[3] for k, v in ah_all.items() if abs(k - line) <= 0.51}
        ou25 = ou_all.get(2.5)
        fit = fit_dc(o12["fair"], ah, ou25[3] if ou25 else None, line)
        if not fit:
            r["status"] = "fit_fail"
            rows.append(r)
            continue

        M, lh, la, rho = fit["M"], fit["lh"], fit["la"], fit["rho"]
        H, D, A = o12["fair"]
        pH = sum(M[i, j] for i in range(N) for j in range(N) if (i - j) + line > 0)
        pD = sum(M[i, j] for i in range(N) for j in range(N) if abs((i - j) + line) < 1e-9)
        pA = 1 - pH - pD
        pj = devig([m["had"][x] for x in "hda"])
        pjh = devig([m["hhad"][x] for x in "hda"])
        ttg = [m["ttg"][k] for k in range(8)]
        invt = [1 / v for v in ttg]
        s_inv = sum(invt)
        pt = [x / s_inv for x in invt]
        et_jc = sum(k * pt[k] for k in range(8))
        et_mod = lh + la
        pt_mod = [sum(M[i, j] for i in range(N) for j in range(N) if i + j == k) for k in range(7)]
        pt_mod.append(1 - sum(pt_mod))
        M1 = grid(lh / 2, la / 2, rho * 0.5)
        hf = {}
        for i1 in range(5):
            for j1 in range(5):
                ht = "h" if i1 > j1 else ("d" if i1 == j1 else "a")
                for i2 in range(N):
                    for j2 in range(N):
                        ft = "h" if i1 + i2 > j1 + j2 else ("d" if i1 + i2 == j1 + j2 else "a")
                        hf[ht + ft] = hf.get(ht + ft, 0.0) + M1[i1, j1] * M1[i2, j2]
        hf = {k: v / sum(hf.values()) for k, v in hf.items()}

        cands = [("胜平负/主胜", H * m["had"]["h"] - 1, m["had"]["h"]),
                 ("胜平负/平", D * m["had"]["d"] - 1, m["had"]["d"]),
                 ("胜平负/客胜", A * m["had"]["a"] - 1, m["had"]["a"]),
                 ("让球/让主", pH * m["hhad"]["h"] - 1, m["hhad"]["h"]),
                 ("让球/让平", pD * m["hhad"]["d"] - 1, m["hhad"]["d"]),
                 ("让球/让客", pA * m["hhad"]["a"] - 1, m["hhad"]["a"])]
        cands += [("总进球/%d球" % k, pt_mod[k] * m["ttg"][k] - 1, m["ttg"][k]) for k in range(8)]
        cands += [("半全场/" + k, hf.get(k, 0) * v - 1, v) for k, v in m["hafu"].items()]
        pd_cov = sum(M[i, j] for i in range(6) for j in range(6)
                     if i == j and ("s%02ds%02d" % (i, j)) in m["crs"])
        ph_cov = sum(M[i, j] for i in range(6) for j in range(6)
                     if i > j and ("s%02ds%02d" % (i, j)) in m["crs"])
        pa_cov = sum(M[i, j] for i in range(6) for j in range(6)
                     if i < j and ("s%02ds%02d" % (i, j)) in m["crs"])
        for k, v in m["crs"].items():
            i, j = int(k[1:3]), int(k[4:6])
            if i < N and j < N:
                cands.append(("比分/%d-%d" % (i, j), M[i, j] * v - 1, v))
        for key, nm, p in (("s1sh", "比分/胜其他", max(H - ph_cov, 0.0)),
                           ("s1sd", "比分/平其他", max(D - pd_cov, 0.0)),
                           ("s1sa", "比分/负其他", max(A - pa_cov, 0.0))):
            if key in m["crs_other"]:
                cands.append((nm, p * m["crs_other"][key] - 1, m["crs_other"][key]))
        cands.sort(key=lambda x: -x[1])
        core = [c for c in cands if c[0].split("/")[0] in ("胜平负", "让球", "总进球")]
        best = (core or cands)[0]
        cal = (best[1] + 1) * 0.94 - 1                       # 概率相对折 6%
        g1 = abs(et_mod - et_jc)
        g2 = abs(p_over(M) - (ou25[3] if ou25 else p_over(M)))
        g3 = abs(cover(M, line)[0] - ah.get(round(line, 2), cover(M, line)[0]))
        hard = (g1 <= 0.25) and (g2 <= 0.05) and (g3 <= 0.08) and abs(rho) < 0.259
        verdict = "trade" if (hard and 5.0 <= cal * 100 <= 15.0) else "watch"
        why = ("4闸全过" if hard else
               ("λμ差%.2f>0.25" % g1 if g1 > 0.25 else
                ("ρ贴界%.2f" % rho if abs(rho) >= 0.249 else
                 ("总球线差%.1fpp" % (g2 * 100) if g2 > 0.05 else
                  "让球档差%.1fpp" % (g3 * 100)))))
        r.update({
            "status": "ok", "event": ev["name"], "event_id": ev["id"],
            "ext": {"x12": o12["x12"], "n": o12["n"], "fair": o12["fair"], "ah": ah,
                    "ou25": ou25[:4] if ou25 else None},
            "model": {"lh": round(lh, 3), "la": round(la, 3), "rho": round(rho, 3),
                      "tot": round(et_mod, 3), "H": H, "D": D, "A": A,
                      "ah": [round(pH, 4), round(pD, 4), round(pA, 4)],
                      "p_over25": round(p_over(M), 4),
                      "ttg": [round(x, 4) for x in pt_mod]},
            "jc": {"devig": [round(x, 4) for x in pj],
                   "devig_hhad": [round(x, 4) for x in pjh], "ttg_implied": round(et_jc, 3)},
            "gates": {"g1": round(g1, 3), "g2": round(g2, 4), "g3": round(g3, 4),
                      "fit_err": round(fit["fit_err"], 4), "ah_err": round(fit["ah_err"], 4)},
            "top5": [[c[0], round(c[1] * 100, 1), c[2]] for c in cands[:5]],
            "core_top5": [[c[0], round(c[1] * 100, 1), c[2]] for c in core[:5]],
            "best": [best[0], round(best[1] * 100, 1), round(min(cal, 0.15) * 100, 1), best[2]],
            "verdict": verdict, "why": why,
        })
        rows.append(r)

    trades = [r for r in rows if r.get("verdict") == "trade"]
    payload = {
        "summary": {
            "date": now.strftime("%F"), "fetched": now.strftime("%F %H:%M CST"),
            "n_jc": len(subs), "n_anchor": sum(1 for r in rows if r["status"] == "ok"),
            "n_trade": len(trades),
            "trades": [[r["num"], r["home"] + " vs " + r["away"], r["best"][0],
                        r["best"][3], r["best"][1], r["best"][2]] for r in trades],
        },
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    LOG.write_text("\n".join(log), encoding="utf-8")

    s = payload["summary"]
    print("\nEXT-ODDS %s | 竞彩 %d 场 | 外部锚 %d 场 | 实战 %d 场"
          % (s["date"], s["n_jc"], s["n_anchor"], s["n_trade"]))
    for r in rows:
        if r.get("status") != "ok":
            print("  %-6s %-8s %s %s" % (r["num"], r["home"], r["status"], r.get("reason", "")))
            continue
        e, mo, b = r["ext"], r["model"], r["best"]
        print("  %-6s %-9s/%-10s ext %.2f/%.2f/%.2f(n=%d) λ%.2f/%.2f ρ%+.2f  最优 %-12s "
              "EV%+6.1f%% cal%+6.1f%%  %s %s"
              % (r["num"], r["home"], r["away"], e["x12"][0], e["x12"][1], e["x12"][2],
                 e["n"], mo["lh"], mo["la"], mo["rho"], b[0], b[1], b[2], r["verdict"], r["why"]))
    for r in trades:
        print("  >>> TRADE %s %s %s @%.2f  EV%+.1f%% cal%+.1f%%"
              % (r["num"], r["home"], r["best"][0], r["best"][3], r["best"][1], r["best"][2]))
    # 一行机器可读摘要(CI 里直接 grep 这一行)
    print("EXT-ODDS-SUMMARY date=%s jc=%d anchor=%d trade=%d %s"
          % (s["date"], s["n_jc"], s["n_anchor"], s["n_trade"],
             " ".join("%s:%s@%.2f(cal%+.1f%%)" % (t[0], t[2], t[3], t[5])
                      for t in s["trades"]) or "-"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                       # noqa: BLE001
        tb = traceback.format_exc()
        stamp = datetime.now(CST).strftime("%F %H:%M CST")
        head = [
            "ext_odds FAILED @ %s" % stamp,
            "python %s" % sys.version.split()[0],
            "exception: %r" % (exc,),
            "-" * 60,
            tb,
        ]
        try:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            # 失败也要落盘: 让 CI 的 commit data 能提交证据, 而不是静默空跑
            LOG.write_text("\n".join(head), encoding="utf-8")
            OUT.write_text(json.dumps({
                "summary": {"date": datetime.now(CST).strftime("%F"),
                            "fetched": stamp, "status": "error",
                            "error": repr(exc),
                            "traceback_tail": tb.strip().splitlines()[-12:]},
                "rows": [],
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            print("[diag] wrote %s + %s" % (LOG, OUT))
        except Exception as e2:                    # noqa: BLE001
            print("[diag] 落盘也失败: %r" % (e2,), file=sys.stderr)
        print("EXT-ODDS FAILED: %r" % (exc,), file=sys.stderr)
        raise
