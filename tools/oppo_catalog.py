#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPPO 区域产品目录抓取器（getProductInfo）。

== 为什么需要它 ==
  OPPO 的 `/basic/v1/getProduct` 只返回**在售精选**机型（de 实测仅 10 台），
  而 `/basic/v1/getProductInfo` 返回**区域产品目录全集**（de 实测 192 台）。
  后者才是"这个区域到底有哪些 OPPO 机型"的权威答案，因此：
    - 机型表发现应改用它（见 references/kb/oppo.json 的 api.product_list）；
    - 判定某个机型行是否为污染残留（不属于该区域）也以它为依据。

  两者返回字段名一致（marketingModelName / marketingModelCode），故取价链路无需改动。
  额外多出：certifiedModels（CPH 全球编码）、productCategoryName / productSeriesName。

== 用法 ==
  python tools/oppo_catalog.py            # 抓取 6 个非CN区域并落盘 + 打印汇总
  python tools/oppo_catalog.py --cc de    # 只抓某个区域
  python tools/oppo_catalog.py --json     # 只输出 JSON 到 stdout（不落盘）

落盘位置：references/catalog/oppo_<cc>.json
"""
import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_PATH = ROOT / "references" / "kb" / "oppo.json"
OUT_DIR = ROOT / "references" / "catalog"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 目录端点。KB 里 product_list 指向 getProduct；本器固定用 getProductInfo，
# 因为"目录全集"语义与该端点绑定。
CATALOG_PATH = "/basic/v1/getProductInfo"


def _api(cc):
    kb = json.load(open(KB_PATH, encoding="utf-8"))
    return kb["countries"][cc][0]["query"]["api"]


def fetch_catalog(cc, to=25):
    """抓取某区域的 getProductInfo 目录，返回机型 dict 列表（原样，含所有字段）。"""
    api = _api(cc)
    host = api["base_url"].split("//")[-1].rstrip("/")
    url = f"https://{host}{CATALOG_PATH}"
    body = {"region": api["region"], "regionIsoCode2": api["region_iso"],
            "brandCode": api.get("brand_code", "11"),
            "isoLanguageCode": api["iso_language"], "sourceRoute": "1"}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Referer": f"https://support.oppo.com/{cc}/spare-parts-price/",
                 "Accept": "application/json, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=to) as r:
        env = json.load(r)
    data = env.get("data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("productList") or data.get("list") or []
    else:
        items = []
    # 只保留可用的机型条目：必须有名字与 code（缺码无法按码取价）
    return [x for x in items
            if isinstance(x, dict) and x.get("marketingModelName") and x.get("marketingModelCode")]


def fetch_all(regions, workers=6):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {cc: ex.submit(fetch_catalog, cc) for cc in regions}
        for cc, fu in futs.items():
            try:
                out[cc] = fu.result()
            except Exception as e:
                print(f"  [warn] {cc} 目录抓取失败: {type(e).__name__}: {str(e)[:80]}",
                      file=sys.stderr)
                out[cc] = []
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc", action="append", help="只抓指定区域（可重复）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 到 stdout，不落盘")
    a = ap.parse_args()

    kb = json.load(open(KB_PATH, encoding="utf-8"))
    regions = a.cc or [cc for cc in kb["countries"] if cc != "cn"]
    catalogs = fetch_all(regions)

    if a.json:
        print(json.dumps(catalogs, ensure_ascii=False, indent=2))
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    print(f"{'区域':<6}{'目录机型':>9}{'含CPH码':>9}{'品类数':>8}")
    for cc, items in catalogs.items():
        p = OUT_DIR / f"oppo_{cc}.json"
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        cph = sum(1 for x in items if x.get("certifiedModels"))
        cats = len({x.get("productCategoryCode") for x in items})
        total += len(items)
        print(f"{cc:<6}{len(items):>9}{cph:>9}{cats:>8}   -> {p.relative_to(ROOT)}")
    print(f"\n合计 {total} 条目录机型，已落盘 {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
