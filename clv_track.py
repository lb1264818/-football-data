# v5 跟踪器：只做一件事——扫「线动 ΔT >= 0.06 球」的信号，记成跟踪账本，滚动算 CLV/ROI
# 1) 历史回填（football-data 有开盘/收盘价的已完赛比赛）→ phase=hist
# 2) 上线后新出现的比赛 → phase=live（真实跟踪，不回溯改写）
# 3) 若仓库 Secrets 里有 ODDS_KEY（The Odds API 免费 500 次/月），再输出当日实时盘口信号
import csv, io, json, math, os, pathlib, urllib.request, datetime, traceback

LG = {
    "英超": "E0", "意甲": "I1", "西甲": "SP1", "法甲": "F1", "法乙": "F2",
    "德甲": "D1", "德乙": "D2", "荷甲": "N1", "英冠": "E1", "葡超": "P1", "苏超": "SC0",
}
SEASON = "2627"
URL = "https://www.football-data.co.uk/mmz4281/{}/{}.csv"
SIG = 0.06                      # 观察下限（先记账，不下注）
TRADE = 0.15                    # 实战门槛：|ΔT| >= 0.15 球才跟（实测 CLV 由负转正的位置）
BANDS = [(0.06, 0.10), (0.10, 0.15), (0.15, 99.0)]
LEDGER = pathlib.Path("data/clv_ledger.json")
SPORT_KEYS = {
    "英超": "soccer_epl", "意甲": "soccer_italy_serie_a", "西甲": "soccer_spain_la_liga",
    "法甲": "soccer_france_ligue_one", "德甲": "soccer_germany_bundesliga",
    "德乙": "soccer_germany_bundesliga2", "荷甲": "soccer_netherlands_eredivisie",
    "英冠": "soccer_efl_champ", "葡超": "soccer_portugal_primeira_liga",
    "苏超": "soccer_spl", "法乙": "soccer_france_ligue_two",
}


def dl(u, timeout=45):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8-sig", "ignore")


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


def dv(*odds):
    inv = [1 / o for o in odds if o]
    if len(inv) != len(odds):
        return None
    s = sum(inv)
    return [x / s for x in inv] if s > 0 else None


def p_over(t):
    return 1.0 - math.exp(-t) * (1.0 + t + t * t / 2.0)


def solve_T(po):
    lo, hi = 0.3, 9.0
    for _ in range(42):
        mid = (lo + hi) / 2.0
        if p_over(mid) < po:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def d_to_iso(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date().isoformat()
        except Exception:
            pass
    return None


def scan_history():
    """返回 [(key, lg, date, home, away, dT, T_o, T_c, over_odds_open, under_odds_open, fair_over_close)]"""
    out, last = [], None
    for lg, code in LG.items():
        try:
            txt = dl(URL.format(SEASON, code))
        except Exception:
            print("HIST-ERR", lg, traceback.format_exc()[-120:])
            continue
        for r in csv.DictReader(io.StringIO(txt)):
            d = d_to_iso(r.get("Date") or "")
            if not d:
                continue
            if last is None or d > last:
                last = d
            so = [fl(r.get("B365>2.5")), fl(r.get("B365<2.5"))]
            sc = [fl(r.get("B365C>2.5")), fl(r.get("B365C<2.5"))]
            if not (all(so) and all(sc)):
                continue
            a, b = dv(*so), dv(*sc)
            if not (a and b):
                continue
            T_o, T_c = solve_T(a[0]), solve_T(b[0])
            hs, as_ = fs(r.get("FTHG")), fs(r.get("FTAG"))
            out.append(dict(key="%s|%s|%s|%s" % (lg, d, r.get("HomeTeam"), r.get("AwayTeam")),
                            lg=lg, d=d, h=r.get("HomeTeam"), a=r.get("AwayTeam"),
                            dT=round(T_c - T_o, 4), T_o=round(T_o, 3), T_c=round(T_c, 3),
                            od_over=so[0], od_under=so[1], fair_over_close=b[0],
                            ft=None if (hs is None or as_ is None) else [int(hs), int(as_)]))
    return out, last


def settle(e, ft):
    tot = ft[0] + ft[1]
    over_won = tot > 2.5
    pick_over = e["pick"] == "大"
    e["ft"] = ft
    e["tot"] = tot
    won = pick_over == over_won
    e["won"] = won
    e["units"] = round((e["odds"] - 1) if won else -1.0, 3)
    fair = e["fair_close"] if pick_over else (1 - e["fair_close"])
    e["clv_pct"] = round((e["odds"] * fair - 1) * 100, 2)
    e.setdefault("closing_line", e.get("T_c"))
    return e


def agg(entries):
    if not entries:
        return dict(n=0)
    n = len(entries)
    hit = sum(1 for e in entries if e.get("won"))
    u = sum(e.get("units", 0) for e in entries)
    clv = [e["clv_pct"] for e in entries if "clv_pct" in e]
    us = [e.get("units", 0) for e in entries]
    su = (sum((x - u / n) ** 2 for x in us) / (n - 1)) ** .5 if n > 1 else 0
    z_roi = round((u / n) / (su / n ** .5), 2) if su else None
    sc = (sum((x - sum(clv) / len(clv)) ** 2 for x in clv) / (len(clv) - 1)) ** .5 if len(clv) > 1 else 0
    z_clv = round((sum(clv) / len(clv)) / (sc / len(clv) ** .5), 2) if sc else None
    return dict(n=n, hit=round(hit / n, 4), units=round(u, 2), roi=round(u / n, 4), z_roi=z_roi,
                clv_mean=round(sum(clv) / len(clv), 2) if clv else None,
                clv_pos=round(sum(1 for x in clv if x > 0) / len(clv), 4) if clv else None,
                z_clv=z_clv)


# ---------- 可选：当日实时盘口（需要 ODDS_KEY，未配则跳过） ----------
def live_signals(today):
    key = os.environ.get("ODDS_KEY", "").strip()
    if not key:
        return dict(status="skip-no-key", note="未配置 ODDS_KEY，实时盘口信号未启用")
    snap_p = pathlib.Path("data/odds_snap.json")
    prev = json.loads(snap_p.read_text()) if snap_p.exists() else {}
    cur, sigs, used = {}, [], 0
    for lg, sk in SPORT_KEYS.items():
        try:
            u = ("https://api.the-odds-api.com/v4/sports/%s/odds/?apiKey=%s"
                 "&regions=eu&markets=totals&oddsFormat=decimal" % (sk, key))
            data = json.loads(dl(u, 30))
            used += 1
        except Exception:
            continue
        for g in data if isinstance(data, list) else []:
            mt = "%s|%s|%s" % (lg, g.get("commence_time"), g.get("home_team"))
            tots = []
            for bk in g.get("bookmakers", []):
                for mk in bk.get("markets", []):
                    if mk.get("key") != "totals":
                        continue
                    for oc in mk.get("outcomes", []):
                        tots.append((oc.get("name"), oc.get("point"), oc.get("price")))
            # 取 2.5 线的 over/under 一对价
            pair = {}
            for nm, pt, pr in tots:
                if pt in (2.5, 3.5) and nm in ("Over", "Under"):
                    pair.setdefault(pt, {})[nm] = pr
            if not pair:
                continue
            for pt, d in pair.items():
                if not ("Over" in d and "Under" in d):
                    continue
                z = dv(d["Over"], d["Under"])
                if not z:
                    continue
                T = solve_T(z[0])
                cur[mt] = dict(pt=pt, T=round(T, 3), over=d["Over"], under=d["Under"],
                               home=g.get("home_team"), away=g.get("away_team"),
                               start=g.get("commence_time"))
                first = prev.get(mt)
                if first and first.get("T") is not None:
                    dT = T - first["T"]
                    if abs(dT) < SIG:
                        continue
                    # 只在开赛前 36 小时内给信号
                    try:
                        st = datetime.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
                        hrs = (st - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
                    except Exception:
                        hrs = 999
                    if 0 < hrs <= 36:
                        sigs.append(dict(lg=lg, home=g.get("home_team"), away=g.get("away_team"),
                                         start=g.get("commence_time"), dT=round(dT, 4),
                                         pick="大" if dT > 0 else "小",
                                         line=pt, odds=d["Over"] if dT > 0 else d["Under"],
                                         hours_to_ko=round(hrs, 1)))
    snap_p.write_text(json.dumps(cur, indent=1))
    return dict(status="ok", requests=used, n_sig=len(sigs), signals=sigs,
                note="首次运行只建快照，第二次起才有 ΔT")


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    hist, last = scan_history()

    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    since = led.get("since") or today
    ents = led.get("entries", {})

    # 1) 历史回填 / 新信号登记（登记后不再改写下注方向与赔率）
    for r in hist:
        k = r["key"]
        if k in ents:
            if r["ft"] and "units" not in ents[k]:
                settle(ents[k], r["ft"])
            continue
        if abs(r["dT"]) < SIG or r["ft"] is None:
            continue
        ents[k] = dict(lg=r["lg"], d=r["d"], h=r["h"], a=r["a"],
                       dT=r["dT"], pick="大" if r["dT"] > 0 else "小",
                       odds=r["od_over"] if r["dT"] > 0 else r["od_under"],
                       fair_close=r["fair_over_close"], closing_line=r["T_c"],
                       tier=("trade" if abs(r["dT"]) >= TRADE else "watch"),
                       phase="live" if r["d"] >= since else "hist", logged=today)
        settle(ents[k], r["ft"])

    live = [e for e in ents.values() if e["phase"] == "live" and "won" in e]
    hst = [e for e in ents.values() if e["phase"] == "hist" and "won" in e]
    big = [e for e in list(ents.values()) if "won" in e]

    band_rows = []
    for lo, hi in BANDS:
        sel = [e for e in big if lo <= abs(e["dT"]) < hi]
        a = agg(sel)
        band_rows.append(dict(band="%.2f~%.2f" % (lo, min(hi, 9.99)), **a))
    by_lg = {}
    for lg in LG:
        a = agg([e for e in big if e["lg"] == lg])
        if a.get("n"):
            by_lg[lg] = a

    # 今日/近期信号（历史数据滞后，通常为空）
    trade = [e for e in big if e.get("tier") == "trade"]
    watch = [e for e in big if e.get("tier") == "watch"]
    pending = [e for e in ents.values() if "won" not in e]
    recent = [e for e in ents.values() if e["d"] >= (now.date() - datetime.timedelta(days=3)).isoformat()
              and "won" not in e]
    lv = live_signals(today)

    out = dict(fetched=now.strftime("%Y-%m-%d %H:%M UTC"), since=since, today=today,
               data_last_date=last, rule=dict(market="ou2.5", sig=SIG,
                                              desc="总球期望 T 从开盘到收盘上调/下调 >=0.06 球才下单，跟线动方向"),
               all=agg(big), live=agg(live), hist=agg(hst),
               trade=agg(trade), watch=agg(watch), thresholds=dict(watch_min=SIG, trade_min=TRADE),
               by_band=band_rows, by_league=by_lg,
               today_signals=recent, pending_n=len(pending), live_odds=lv,
               note="hist 为回填验证，live 为上线后真实跟踪；两者分开统计，不混算")
    LEDGER.parent.mkdir(exist_ok=True)
    LEDGER.write_text(json.dumps(dict(since=since, updated=out["fetched"], entries=ents), ensure_ascii=False, indent=1))
    pathlib.Path("data/clv_track.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print("CLVTRACK-N", out["all"].get("n"), "TRADE", out["trade"].get("n"),
          "WATCH", out["watch"].get("n"), "LIVE", out["live"].get("n"),
          "HIST", out["hist"].get("n"), "LASTDATA", last)
    print("CLVTRACK-TODAY", len(recent), "LIVEODDS", lv.get("status"))
    print(json.dumps({k: out[k] for k in ("trade", "watch", "by_band")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("CLVTRACK-TOPFAIL", traceback.format_exc()[-400:])
