import json, os, urllib.request, pathlib, datetime

KEY = os.environ["SPORTSDB_KEY"]
API = "https://www.thesportsdb.com/api/v1/json/{}/eventsseason.php?id={}&s={}"

# 键: (联赛ID, 赛季, 中文名)  赛季格式不符时脚本会自动换一个再试
LEAGUES = {
 "seriea": ("4332", "2026-2027", "意甲"),
 "epl":    ("4328", "2026-2027", "英超"),
 "laliga": ("4335", "2026-2027", "西甲"),
 "ligue2": ("4401", "2026-2027", "法乙"),
 "brasil": ("4351", "2026", "巴甲"),
 "swe":    ("4347", "2026", "瑞超"),
 "mls":    ("4346", "2026", "美职"),
}
ALT = {"2026-2027": "2026", "2026": "2026-2027"}

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

def agg(events):
    teams = {}
    for e in events:
        if e.get("intHomeScore") in (None, "") or e.get("intAwayScore") in (None, ""):
            continue
        hs, as_ = int(e["intHomeScore"]), int(e["intAwayScore"])
        for team, gf, ga, side, opp in ((e["strHomeTeam"], hs, as_, "H", e["strAwayTeam"]),
                                        (e["strAwayTeam"], as_, hs, "A", e["strHomeTeam"])):
            t = teams.setdefault(team, {"H": [], "A": [], "list": []})
            t[side].append((gf, ga))
            t["list"].append({"d": e.get("dateEvent"), "r": e.get("intRound"), "side": side,
                              "opp": opp, "gf": gf, "ga": ga,
                              "res": "W" if gf > ga else ("L" if gf < ga else "D")})
    out = {}
    for name, t in teams.items():
        def m(arr, drop_extreme=False):
            if drop_extreme:
                arr = [x for x in arr if x[0] < 4 and x[1] < 4]
            n = len(arr)
            return {"n": n,
                    "gf": round(sum(x[0] for x in arr) / n, 2) if n else None,
                    "ga": round(sum(x[1] for x in arr) / n, 2) if n else None}
        h, a = m(t["H"]), m(t["A"])
        ht, at = m(t["H"], True), m(t["A"], True)
        lst = sorted(t["list"], key=lambda x: x["d"] or "")
        form = lst[-6:]
        pts = sum(3 if x["res"] == "W" else (1 if x["res"] == "D" else 0) for x in form)
        out[name] = {
            "home_n": h["n"], "home_gf": h["gf"], "home_ga": h["ga"],
            "away_n": a["n"], "away_gf": a["gf"], "away_ga": a["ga"],
            "home_gf_x": ht["gf"], "home_ga_x": ht["ga"],
            "away_gf_x": at["gf"], "away_ga_x": at["ga"],
            "total_n": h["n"] + a["n"],
            "form6": "".join(x["res"] for x in form), "form6_pts": pts,
            "recent": lst[-10:],
        }
    return out

pathlib.Path("data").mkdir(exist_ok=True)
allout = {}
for k, (lid, season, cn) in LEAGUES.items():
    data = {}
    for s in (season, ALT.get(season, season)):
        try:
            j = get(API.format(KEY, lid, s))
        except Exception as ex:
            data = {"error": str(ex)}; break
        if j.get("events"):
            data = {"league": cn, "season": s, "ft": len([e for e in j["events"]
                     if e.get("intHomeScore") not in (None, "")]), "teams": agg(j["events"])}
            break
        data = {"league": cn, "season": s, "ft": 0, "teams": {}, "note": "no events"}
    allout[k] = data
    pathlib.Path(f"data/{k}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(k, data.get("ft"), "teams:", len(data.get("teams", {})))

allout["_meta"] = {"updated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
pathlib.Path("data/all.json").write_text(json.dumps(allout, ensure_ascii=False))
print("done")
