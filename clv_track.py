# v5 tracker: scan line-move signals (dT >= 0.06 goals), log them, roll up CLV/ROI
# 1) history backfill (finished matches with open/close odds) -> phase=hist
# 2) matches appearing after go-live -> phase=live (true forward tracking, never rewritten)
# 3) if ODDS_KEY exists (The Odds API free 500/month), also emit same-day live signals
import csv, io, json, math, os, pathlib, time, urllib.request, datetime, traceback

LG = {
    "EPL": "E0", "ITA": "I1", "ESP": "SP1", "FRA1": "F1", "FRA2": "F2",
    "GER1": "D1", "GER2": "D2", "NED": "N1", "ENG2": "E1", "POR": "P1", "SCO": "SC0",
}
SEASON = "2627"
URL = "https://www.football-data.co.uk/mmz4281/{}/{}.csv"
SIG = 0.06                      # watch floor (log only, no bet)
TRADE = 0.15                    # trade threshold: |dT| >= 0.15 goals (CLV turns positive here)
BANDS = [(0.06, 0.10), (0.10, 0.15), (0.15, 99.0)]
LEDGER = pathlib.Path("data/clv_ledger.json")
SPORT_KEYS = {
    "EPL": "soccer_epl", "ITA": "soccer_italy_serie_a", "ESP": "soccer_spain_la_liga",
    "FRA1": "soccer_france_ligue_one", "GER1": "soccer_germany_bundesliga",
    "GER2": "soccer_germany_bundesliga2", "NED": "soccer_netherlands_eredivisie",
    "ENG2": "soccer_efl_champ", "POR": "soccer_portugal_primeira_liga",
    "SCO": "soccer_spl", "FRA2": "soccer_france_ligue_two",
}


def dl(u, timeout=45, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8-sig", "ignore")
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


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
    """returns list of dicts: key, lg, d, h, a, dT, T_o, T_c, open odds, fair over prob at close"""
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
    pick_over = e["pick"] == "OVER"
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


# ---------- optional: same-day live odds (needs ODDS_KEY, skipped otherwise) ----------
PREF = ["pinnacle", "onexbet", "unibet_eu", "betfair", "williamhill", "marathonbet"]


def _get_json(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=30)
    body = json.loads(resp.read().decode("utf-8"))
    hdr = dict(remaining=resp.headers.get("x-requests-remaining"),
               used=resp.headers.get("x-requests-used"))
    return body, hdr


def pick_totals(game):
    """one bookmaker only: prefer sharps, then take the first usable O/U pair on a main line"""
    books = game.get("bookmakers") or []
    books = sorted(books, key=lambda b: PREF.index(b["key"]) if b.get("key") in PREF else 99)
    for bk in books:
        for mk in bk.get("markets") or []:
            if mk.get("key") != "totals":
                continue
            pts = {}
            for o in mk.get("outcomes", []):
                pts.setdefault(o.get("point"), {})[o.get("name")] = o.get("price")
            for pt in (2.5, 3.5, 2.25, 2.75, 1.5, 4.5):
                d = pts.get(pt)
                if d and d.get("Over") and d.get("Under"):
                    return dict(pt=pt, over=d["Over"], under=d["Under"], book=bk["key"])
    return None


def live_signals(today):
    key = os.environ.get("ODDS_KEY", "").strip()
    if not key:
        return dict(status="skip-no-key", note="ODDS_KEY not set, live signals disabled")
    only = [x.strip() for x in os.environ.get("LIVE_LEAGUES", "").split(",") if x.strip()]
    snap_p = pathlib.Path("data/odds_snap.json")
    prev = json.loads(snap_p.read_text()) if snap_p.exists() else {}
    cur, sigs, used, rem = {}, [], 0, None
    for lg, sk in SPORT_KEYS.items():
        if only and lg not in only:
            continue
        try:
            data, hdr = _get_json("https://api.the-odds-api.com/v4/sports/%s/odds/?apiKey=%s"
                                  "&regions=eu&markets=totals&oddsFormat=decimal" % (sk, key))
            used += 1
            if hdr.get("remaining") is not None:
                rem = hdr["remaining"]
        except Exception:
            continue
        for g in data if isinstance(data, list) else []:
            mt = "%s|%s|%s" % (lg, g.get("commence_time"), g.get("home_team"))
            tk = pick_totals(g)
            if not tk:
                continue
            z = dv(tk["over"], tk["under"])
            if not z:
                continue
            T = solve_T(z[0])
            cur[mt] = dict(pt=tk["pt"], T=round(T, 3), over=tk["over"], under=tk["under"],
                           book=tk["book"], home=g.get("home_team"), away=g.get("away_team"),
                           start=g.get("commence_time"))
            first = prev.get(mt)
            if not first or first.get("T") is None or first.get("book") != tk["book"]:
                continue
            dT = T - first["T"]
            if abs(dT) < TRADE:
                continue
            try:
                st = datetime.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
                hrs = (st - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600
            except Exception:
                hrs = 999
            if not (0 < hrs <= 36):
                continue
            sigs.append(dict(lg=lg, h=g.get("home_team"), a=g.get("away_team"),
                             start=g.get("commence_time"), dT=round(dT, 4),
                             pick="OVER" if dT > 0 else "UNDER", line=tk["pt"],
                             odds=tk["over"] if dT > 0 else tk["under"], book=tk["book"],
                             hours_to_ko=round(hrs, 1)))
    if used == 0:
        return dict(status="auth-or-network-fail", requests=0, remaining=None,
                    note="every odds request failed: check ODDS_KEY value and sport keys")
    snap_p.write_text(json.dumps(cur, indent=1))
    return dict(status="ok", requests=used, remaining=rem, n_sig=len(sigs), signals=sigs,
                snap_n=len(cur),
                note="first run only builds snapshot, dT available from second run")


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    hist, last = scan_history()

    led = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    since = led.get("since") or today
    ents = led.get("entries", {})

    # 1) history backfill / new signal logging (pick and odds are frozen once logged)
    for r in hist:
        k = r["key"]
        if k in ents:
            if r["ft"] and "units" not in ents[k]:
                settle(ents[k], r["ft"])
            continue
        if abs(r["dT"]) < SIG or r["ft"] is None:
            continue
        ents[k] = dict(lg=r["lg"], d=r["d"], h=r["h"], a=r["a"],
                       dT=r["dT"], pick="OVER" if r["dT"] > 0 else "UNDER",
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

    # recent / pending signals (data lags, usually empty)
    trade = [e for e in big if e.get("tier") == "trade"]
    watch = [e for e in big if e.get("tier") == "watch"]
    pending = [e for e in ents.values() if "won" not in e]
    recent = [e for e in ents.values() if e["d"] >= (now.date() - datetime.timedelta(days=3)).isoformat()
              and "won" not in e]
    lv = live_signals(today)

    out = dict(fetched=now.strftime("%Y-%m-%d %H:%M UTC"), since=since, today=today,
               data_last_date=last, rule=dict(market="ou2.5", sig=SIG,
                                              desc="bet only when total-goals T moves >=0.06 toward one side, follow the move"),
               all=agg(big), live=agg(live), hist=agg(hst),
               trade=agg(trade), watch=agg(watch), thresholds=dict(watch_min=SIG, trade_min=TRADE),
               by_band=band_rows, by_league=by_lg,
               today_signals=recent, pending_n=len(pending), live_odds=lv,
               note="hist = backfill validation, live = forward tracking after go-live; never mixed")
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
