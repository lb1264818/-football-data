# 竞彩 总进球/大小球 赔率采集（独立脚本，只存真实返回，失败不报致命错误）
import json, gzip, re, pathlib, urllib.request, datetime, os

SRC = [
 ("ttg_calc", "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?poolCode=ttg&channel=c"),
 ("hhad_calc", "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?poolCode=hhad&channel=c"),
 ("result", "https://webapi.sporttery.cn/gateway/jc/football/getMatchResultV1.qry?matchPage=1&isFix=0&pcOrWap=1"),
 ("ttg_wap", "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?poolCode=ttg&channel=wap"),
]
REOU = re.compile(r"ttg|jqs|total|goals|over|under|daxiao", re.I)

def jget(u):
    req = urllib.request.Request(u, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
        "Referer": "https://www.sporttery.cn/", "Accept": "application/json,*/*"})
    b = urllib.request.urlopen(req, timeout=45).read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return json.loads(b.decode("utf-8", "ignore"))

def walk(o, hits):
    if isinstance(o, dict):
        if "matchNumStr" in o or "matchNum" in o:
            hits.append(o)
        for v in o.values():
            walk(v, hits)
    elif isinstance(o, list):
        for v in o:
            walk(v, hits)

def rec(m, src):
    r = {"src": src}
    for k, v in m.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (str, int, float)):
            r[k] = v
        elif isinstance(v, (dict, list)) and REOU.search(k):
            r[k] = v
    return r

def main():
    out = {"fetched": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
           "sources": {}, "errors": [], "matches": []}
    for nm, u in SRC:
        try:
            j = jget(u)
        except Exception as ex:
            out["errors"].append(nm + ": " + str(ex)[:140])
            print("OU-ERR", nm, str(ex)[:140])
            continue
        hits = []
        walk(j, hits)
        out["sources"][nm] = {"url": u, "n": len(hits),
                              "keys": sorted(hits[0].keys()) if hits else []}
        for m in hits:
            out["matches"].append(rec(m, nm))
        print("OU-OK", nm, "matches:", len(hits))
    seen = {}
    for r in out["matches"]:
        k = (str(r.get("matchNumStr") or r.get("matchNum")),
             str(r.get("homeTeamAbbName") or r.get("homeTeamName") or ""))
        if k not in seen or r["src"] == "ttg_calc":
            seen[k] = r
    out["matches"] = list(seen.values())
    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/ou.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("OU saved:", len(out["matches"]), "errs:", len(out["errors"]))

if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("OU-TOPFAIL", str(ex)[:160])
        pathlib.Path("data").mkdir(exist_ok=True)
        pathlib.Path("data/ou.json").write_text(json.dumps(
            {"fetched": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
             "errors": ["topfail: " + str(ex)[:200]], "matches": []}, ensure_ascii=False))
