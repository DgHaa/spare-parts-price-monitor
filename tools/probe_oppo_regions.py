"""tools/probe_oppo_regions.py - 探测 OPPO REBORN 各区域的可用 CDN 节点、机型数与币种。

用途：为把 de/tr/mx/my/jp/ae 迁移到 api_reborn 提供事实依据——
  1) 同一区域在 sow-cms / par-sow-cms / sgp-sow-cms 三个节点上各返回多少机型；
  2) 取一台有价的机型，读出 partPriceList 的币种（retailPriceCurrency）与样例价。

body 结构以官网真实请求为准（Playwright 抓包，cn 实测）：
  {region, regionIsoCode2, isoLanguageCode, sourceRoute}   ← **不含 brandCode**
本脚本按同样结构发（brandCode 只在 --with-brand-code 时附加，用于对比）。

用法：python tools/probe_oppo_regions.py [--regions de,tr] [--with-brand-code]
"""
import argparse
import json
import urllib.request

HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://support.oppo.com/",
       "Accept": "application/json", "Content-Type": "application/json; charset=UTF-8",
       "Origin": "https://support.oppo.com"}
HOSTS = ["sow-cms.oppo.com", "par-sow-cms.oppo.com", "sgp-sow-cms.oppo.com"]
REGIONS = {
    "de": ("DE", "de", "de-DE"),
    "tr": ("TR", "tr", "tr-TR"),
    "mx": ("MX", "mx", "es-MX"),
    "my": ("MY", "my", "ms-MY"),
    "jp": ("JP", "jp", "ja-JP"),
    "ae": ("AE", "ae", "ar-AE"),
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(host, path, body, to=20):
    url = f"https://{host}/oppo-api{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HDR)
    try:
        with OPENER.open(req, timeout=to) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:90]}"}


def main(regions, with_brand_code):
    for cc in regions:
        rg, iso, lang = REGIONS[cc]
        base = {"region": rg, "regionIsoCode2": iso, "isoLanguageCode": lang, "sourceRoute": "1"}
        if with_brand_code:
            base["brandCode"] = "11"
        print(f"\n{'='*74}\n[{cc}] region={rg} iso={iso} lang={lang} brandCode={'有' if with_brand_code else '无'}")
        best = None
        for h in HOSTS:
            j = post(h, "/basic/v1/getProduct", dict(base))
            if j.get("error"):
                print(f"  {h:26} ERR {j['error']}")
                continue
            data = j.get("data") or []
            cats = {}
            for x in data:
                cats[x.get("categoryName")] = cats.get(x.get("categoryName"), 0) + 1
            print(f"  {h:26} code={j.get('code')} models={len(data)} {cats}")
            if data and (best is None or len(data) > best[1]):
                best = (h, len(data), data)
        if not best:
            print(f"  => [{cc}] 三个节点均无数据")
            continue
        h, n, data = best
        # 取前若干台，找一台有价的读币种
        for x in data[:8]:
            code = x.get("marketingModelCode")
            jp = post(h, "/basic/v1/getPartPriceNew", {**base, "marketingModelCode": code})
            pl = ((jp.get("data") or {}).get("partPriceList") or [])
            rows = [c for g in pl for c in (g.get("childList") or [g])]
            if rows:
                cur = rows[0].get("retailPriceCurrency")
                sample = [(r.get("partName"), r.get("retailPrice")) for r in rows[:3]]
                print(f"  => [{cc}] 最佳节点 {h}（{n} 台）；样例 {x.get('marketingModelName')}"
                      f" 货币={cur} 条数={len(rows)} 例={sample}")
                break
        else:
            print(f"  => [{cc}] 最佳节点 {h}（{n} 台）但前 8 台均无价格")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="de,tr,mx,my,jp,ae")
    ap.add_argument("--with-brand-code", action="store_true")
    a = ap.parse_args()
    main([s.strip() for s in a.regions.split(",") if s.strip()], a.with_brand_code)
