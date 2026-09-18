"""tools/verify_oppo_deeplink.py - 验证 OPPO 各区域"机型深链"是否真能渲染该机型价表。

背景：cn 的 `#/detail?marketingModelCode=<code>` 深链已实测可直接渲染价表；
但早前对 my 的观察称"深链不会预选机型"。迁移 de/tr/mx/my/jp/ae 到 api_reborn 前，
必须确认各区域深链可用（否则 model_page_url 不能写深链，只能退品牌入口）。

用法：python tools/verify_oppo_deeplink.py --country my --model "OPPO A6c"
"""
import argparse
import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.core import launch_browser  # noqa: E402

HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://support.oppo.com/",
       "Accept": "application/json", "Content-Type": "application/json; charset=UTF-8",
       "Origin": "https://support.oppo.com"}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REGIONS = {"cn": ("sow-cms.oppo.com", "CN", "cn", "zh-CN"),
           "de": ("par-sow-cms.oppo.com", "DE", "de", "de-DE"),
           "tr": ("sgp-sow-cms.oppo.com", "TR", "tr", "tr-TR"),
           "mx": ("sgp-sow-cms.oppo.com", "MX", "mx", "es-MX"),
           "my": ("sgp-sow-cms.oppo.com", "MY", "my", "ms-MY"),
           "jp": ("sgp-sow-cms.oppo.com", "JP", "jp", "ja-JP"),
           "ae": ("sgp-sow-cms.oppo.com", "AE", "ae", "ar-AE")}


def post(host, path, body):
    req = urllib.request.Request(f"https://{host}/oppo-api{path}",
                                data=json.dumps(body).encode(), headers=HDR)
    with OP.open(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


async def main(cc, model):
    host, rg, iso, lang = REGIONS[cc]
    base = {"region": rg, "regionIsoCode2": iso, "isoLanguageCode": lang,
            "sourceRoute": "1", "brandCode": "11"}
    j = post(host, "/basic/v1/getProduct", dict(base))
    data = j.get("data") or []
    print(f"[{cc}] 机型 {len(data)} 台")
    code = None
    for x in data:
        if (x.get("marketingModelName") or "") == model:
            code = x["marketingModelCode"]
            break
    if not code:
        print(f"[fail] 未找到机型 {model}")
        return 1
    # 先确认该 code 有价
    jp = post(host, "/basic/v1/getPartPriceNew", {**base, "marketingModelCode": code})
    rows = [c for g in ((jp.get("data") or {}).get("partPriceList") or [])
            for c in (g.get("childList") or [g])]
    print(f"[{cc}] {model} code={code} API 取价 {len(rows)} 条")

    url = f"https://support.oppo.com/{cc}/spare-parts-price/#/detail?marketingModelCode={code}"
    print(f"[{cc}] 深链: {url}")
    pw, b = await launch_browser()
    p = await b.new_page()
    try:
        await p.goto(url, wait_until="domcontentloaded", timeout=30000)
        await p.wait_for_timeout(9000)
        txt = await p.evaluate("() => document.body ? document.body.innerText : ''")
        hits = [ln.strip() for ln in txt.splitlines()
                if re.search(r"RM\s?\d|Screen|Battery|Mainboard|屏幕|电池|主板|¥|€|\$", ln)][:14]
        print(f"[{cc}] 渲染行数={len(txt.splitlines())} 匹配价/部件行={len(hits)}")
        for h in hits:
            print("   ", h)
        print(f"[{cc}] 结论: {'深链可用（渲染出价表）' if hits else '深链未渲染价表 → 需退品牌入口'}")
    finally:
        await p.close()
        await b.close()
        await pw.stop()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="my")
    ap.add_argument("--model", default="OPPO A6c")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.country, a.model)))
