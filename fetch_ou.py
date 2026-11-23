# 大小球 2.5 线真实赔率采集（football-data.co.uk，免费无 key，含 B365 / 市场均值）
# 覆盖：英超 E0、意甲 I1、西甲 SP1、法乙 F2（瑞超/巴甲/美职该站不提供大小球，不采）
import csv, io, json, pathlib, urllib.request, datetime

FILES = {"英超": "E0", "意甲": "I1", "西甲": "SP1", "法乙": "F2"}
SEASONS = ["2627", "2526"]
BASE = "https://www.football-data.co.uk/mmz4281/{}/{}.csv"
COLS = {"b365o": "B365>2.5", "b365u": "B365<2.5", "avgo": "Avg>2.5", "avgu": "Avg<2.5",
        "cb365o": "B365C>2.5", "cb365u": "B365C<2.5", "cavgo": "AvgC>2.5", "cavgu": "AvgC<2.5"}

def dl(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8-sig", "ignore")

def fnum(x):
    try:
        v = float(x)
        return round(v, 3) if v > 0 else None
    except Exception:
        return None

def iso(d):
    try:
        return datetime.datetime.strptime(d.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        return d.strip()

def main():
    out = {"fetched": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
           "season": None, "sources": {}, "errors": [], "rows": []}
    for season in SEASONS:
        got = 0
        for lg, code in FILES.items():
            u = BASE.format(season, code)
            try:
                txt = dl(u)
            except Exception as ex:
                out["errors"].append(f"{lg} {season}: " + str(ex)[:120])
                print("OU-ERR", lg, season, str(ex)[:120])
                continue
            rd = list(csv.DictReader(io.StringIO(txt)))
            if not rd or not any(c in rd[0] for c in ("B365>2.5", "Avg>2.5")):
                out["errors"].append(f"{lg} {season}: 无大小球列")
                print("OU-SKIP", lg, season, "无大小球列")
                continue
            n = 0
            for r in rd:
                if not r.get("HomeTeam") or not r.get("B365>2.5"):
                    continue
                rec = {"lg": lg, "season": season, "date": iso(r.get("Date", "")),
                       "home": r.get("HomeTeam"), "away": r.get("AwayTeam"),
                       "ft": [r.get("FTHG"), r.get("FTAG")], "line": 2.5, "src": "football-data"}
                for k, c in COLS.items():
                    rec[k] = fnum(r.get(c))
                out["rows"].append(rec)
                n += 1
            out["sources"][f"{lg}_{season}"] = {"url": u, "n": n}
            got += n
            print("OU-OK", lg, season, "rows:", n)
        if got:
            out["season"] = season
            break
    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/ou.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    mxd = max([r["date"] for r in out["rows"] if r.get("date")] or ["0"])
    try:
        cut = (datetime.datetime.strptime(mxd, "%Y-%m-%d") - datetime.timedelta(days=21)).strftime("%Y-%m-%d")
    except Exception:
        cut = "0"
    tail = dict(out, rows=[r for r in out["rows"] if r.get("date", "") >= cut])
    pathlib.Path("data/ou_tail.json").write_text(json.dumps(tail, ensure_ascii=False, indent=1))
    print("OU saved:", len(out["rows"]), "tail:", len(tail["rows"]), "errs:", len(out["errors"]))

if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("OU-TOPFAIL", str(ex)[:160])
        pathlib.Path("data").mkdir(exist_ok=True)
        pathlib.Path("data/ou.json").write_text(json.dumps(
            {"fetched": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
             "errors": ["topfail: " + str(ex)[:200]], "rows": []}, ensure_ascii=False))
