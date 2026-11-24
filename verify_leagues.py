# 联赛接入前校验：thesportsdb 联赛 ID 实测 + football-data 覆盖探测，只输出结论
import json, os, pathlib, urllib.parse, urllib.request, datetime, traceback

# 密钥不再写死在仓库(公开仓库=明文泄露), 由 CI 的 secrets.SPORTSDB_KEY 注入
KEY = os.environ.get("SPORTSDB_KEY", "").strip()
TS = "https://www.thesportsdb.com/api/v1/json/{}/{}"
TARGET = {
 "日职": ("Japan", ["J1 League", "J.League", "J1"]),
 "韩职": ("South Korea", ["K League 1"]),
 "澳超": ("Australia", ["A-League"]),
 "挪超": ("Norway", ["Eliteserien"]),
 "德甲": ("Germany", ["Bundesliga"]),
 "德乙": ("Germany", ["2. Bundesliga"]),
 "荷甲": ("Netherlands", ["Eredivisie"]),
 "荷乙": ("Netherlands", ["Eerste Divisie"]),
 "葡超": ("Portugal", ["Primeira Liga"]),
 "苏超": ("Scotland", ["Scottish Premiership"]),
 "英冠": ("England", ["Championship"]),
 "法甲": ("France", ["Ligue 1"]),
}
FD = ["JPN", "NOR", "KOR", "AUS", "D2", "N2", "N1", "D1", "E1", "P1", "SC0", "F1"]

def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8-sig", "ignore")

def ts_ids(country):
    j = json.loads(get(TS.format(KEY, "search_all_leagues.php") + f"?c={urllib.parse.quote(country)}&s=Soccer"))
    return [{"id": x.get("idLeague"), "name": x.get("strLeague")} for x in (j.get("countries") or [])]

def ts_events(lid, season):
    j = json.loads(get(TS.format(KEY, "eventsseason.php") + f"?id={lid}&s={season}"))
    ev = j.get("events") or []
    teams = []
    for e in ev:
        t = e.get("strHomeTeam")
        if t and t not in teams:
            teams.append(t)
    return len(ev), teams[:6]

def main():
    if not KEY:
        print("VERIFY-SKIP: SPORTSDB_KEY 未设置")
        return
    out = {"fetched": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
           "ts": {}, "fd": {}, "errors": []}
    cache = {}
    for lg, (country, keys) in TARGET.items():
        try:
            if country not in cache:
                cache[country] = ts_ids(country)
            cand = [c for c in cache[country]
                    if any(k.lower() in (c["name"] or "").lower() for k in keys)]
            if not cand:
                out["ts"][lg] = {"found": False,
                                 "near": [c["name"] for c in cache[country]][:12]}
                print("TS-MISS", lg, "候选:", len(cache[country]))
                continue
            lid = cand[0]["id"]; nm = cand[0]["name"]
            for season in ("2026-2027", "2026"):
                try:
                    n, teams = ts_events(lid, season)
                except Exception as ex:
                    n, teams = -1, [str(ex)[:60]]
                if n > 0:
                    out["ts"][lg] = {"found": True, "id": lid, "name": nm, "season": season,
                                     "events": n, "teams": teams, "ok": True}
                    print("TS-OK", lg, lid, nm, season, n)
                    break
            else:
                out["ts"][lg] = {"found": True, "id": lid, "name": nm, "events": 0, "ok": False}
                print("TS-EMPTY", lg, lid, nm)
        except Exception:
            out["ts"][lg] = {"found": False, "err": traceback.format_exc()[-160:]}
            print("TS-ERR", lg, traceback.format_exc()[-120:])
    for code in FD:
        for kind, u in (("main", f"https://www.football-data.co.uk/mmz4281/2627/{code}.csv"),
                        ("new", f"https://www.football-data.co.uk/new/{code}.csv")):
            try:
                txt = get(u)
            except Exception:
                continue
            head = txt.splitlines()[0].split(",")
            has_ou = "B365>2.5" in head or "Avg>2.5" in head
            has_1x2 = any(c in head for c in ("B365H", "PSCH", "B365CH"))
            dt = [l.split(",")[3] for l in txt.splitlines()[1:] if l.count(",") > 5]
            out["fd"][code] = {"kind": kind, "ou": has_ou, "x12": has_1x2,
                               "rows": len(dt), "last": dt[-1] if dt else None}
            print("FD", code, kind, "OU" if has_ou else "no-OU", "rows", len(dt))
            break
    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/idcheck.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("VERIFY saved. ts:", len(out["ts"]), "fd:", len(out["fd"]))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("VERIFY-TOPFAIL", traceback.format_exc()[-300:])
