"""tools/probe_vivo_api.py - 探明并验证 vivo 备件价的官方接口（不启浏览器）。

背景（2026-09-17，issue #55）：
  vivo/ae 的 DOM 点选路径（executor.vivo_parts_grid）连续 4 次「尝试抽取 0 行」，
  但同配置的 my/tr 正常。查页面 bundle 发现价格根本不是 DOM 直出，而是点选后
  POST 官方接口拿的：

      // index.pack_<hash>.js
      selectProductAjax: $.ajax({ url: ajaxUrl.searchMaint, data: {id: e}, type: "POST" })
      // public_<hash>.js
      searchMaint: globalVar.path + '/' + globalVar.regionId + '/support/queryPriceByProductId'

  而机型清单**已 SSR 内联**在 HTML 里（`<li class="select-model-item" data-id="3687">T1 Pro 5G</li>`），
  `data-id` 就是要传给接口的 id。故整条链路可以完全不用浏览器。

关键区域差异：`globalVar.regionId` 在 ae 是 **`'ae/en'`**（带语言段），my/tr 是 `'my'`/`'tr'`
—— 这也解释了为何同一套 DOM 选择器在 ae 上失效：页面实际在 `/ae/en/` 下，
KB 里写的 `/ae/support/accessory` 会 301 过去，交互链路对不上。

字段（响应 `data.sparePartVO`）：
  spareCompany           币种（ae=AED / my=MYR / tr=TRY）
  sparePartsVoList[]     name / price / promotionPrice / materialPrice
  页面显示价优先级：materialPrice > promotionPrice > price（见 getPartsDom/getPartsDomOld）

用法：
  python tools/probe_vivo_api.py --country ae            # 探明 + 抽样
  python tools/probe_vivo_api.py --country my --compare   # 与库内 DOM 结果逐条比对
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

ITEM_RE = re.compile(
    r'<li class="select-model-item[^"]*"[^>]*data-id="(\d+)"[^>]*>([^<]*)</li>')
REGION_RE = re.compile(r"regionId:\s*['\"]([^'\"]+)")


def _get(url, referer, tries=3, to=25):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.build_opener().open(req, timeout=to) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.6)
    raise RuntimeError(last or "get failed")


def _post(url, data, referer, to=25):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    with urllib.request.build_opener().open(req, timeout=to) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_page(country):
    """取各国备件页 HTML，返回 (region_id, [(data_id, model_name), ...])。"""
    url = f"https://www.vivo.com/{country}/support/accessory"
    h = _get(url, url)
    rid = REGION_RE.search(h)
    return (rid.group(1) if rid else country), ITEM_RE.findall(h)


def parse_price(it, query_by_crm=True):
    """按页面 getPartsDom 的优先级取显示价：materialPrice > promotionPrice > price。"""
    for k in ("materialPrice", "promotionPrice", "price"):
        v = it.get(k)
        if v in (None, ""):
            continue
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            continue
    return None


def fetch_prices(region_id, data_id, referer):
    """返回 (currency, [(part_name, price), ...])。"""
    url = f"https://www.vivo.com/{region_id}/support/queryPriceByProductId"
    j = _post(url, {"id": data_id}, referer)
    if not j.get("success"):
        return None, None, f"success=false msg={j.get('msg')}"
    sp = ((j.get("data") or {}).get("sparePartVO")) or {}
    cur = sp.get("spareCompany")
    qbc = sp.get("queryByCrm")
    out = []
    for it in (sp.get("sparePartsVoList") or []):
        name = (it.get("name") or "").strip()
        p = parse_price(it, qbc)
        if name and p is not None:
            out.append((name, p))
    return cur, out, None


def db_prices(country):
    """库内现有（DOM 路径抓到的）价格，按 机型名 → {备件名: 价}。"""
    c = sqlite3.connect(DB)
    rows = c.execute(
        """SELECT m.name, p.name, ps.price FROM price_snapshots ps
           JOIN parts p ON p.id = ps.part_id
           JOIN models m ON m.id = p.model_id
           JOIN brands b ON b.id = m.brand_id
           WHERE b.name='vivo' AND m.country_code=?""", (country,)).fetchall()
    c.close()
    out = {}
    for mn, pn, pr in rows:
        out.setdefault(mn, {})[pn] = pr
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", required=True)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--compare", action="store_true",
                    help="与库内 DOM 路径抓到的价格逐条比对")
    a = ap.parse_args()

    rid, items = fetch_page(a.country)
    print(f"[page] vivo/{a.country} regionId='{rid}'  SSR 机型数={len(items)}")
    sample = items[:a.limit]
    ref = f"https://www.vivo.com/{a.country}/support/accessory"
    api_map, errs = {}, 0
    t0 = time.time()
    for did, name in sample:
        try:
            cur, rows, err = fetch_prices(rid, did, ref)
            if err:
                errs += 1
                print(f"  {name:26s} 接口异常: {err}")
                continue
            api_map[name] = {p: v for p, v in rows}
            curs = cur or "--"
            print(f"  {name:26s} {curs:4s} 价行={len(rows)}"
                  + (f"  例: {rows[0][0]}={rows[0][1]}" if rows else "  （官方无价）"))
        except Exception as e:
            errs += 1
            print(f"  {name:26s} 请求失败 {type(e).__name__}: {str(e)[:80]}")
        time.sleep(0.3)
    el = time.time() - t0
    print(f"[api] 抽样 {len(sample)} 台，{el:.1f}s，异常 {errs} 台")

    if a.compare:
        d = db_prices(a.country)
        print("\n[compare] 与库内 DOM 结果逐条比对（同机型同名备件）")
        same = diff = only_api = only_db = 0
        for mn, api_parts in api_map.items():
            dbp = d.get(mn)
            if dbp is None:
                only_api += 1
                continue
            for pn, v in api_parts.items():
                if pn not in dbp:
                    only_db += 1
                    continue
                if abs((dbp[pn] or 0) - v) < 0.01:
                    same += 1
                else:
                    diff += 1
                    print(f"  ✗ {mn} / {pn}: 库={dbp[pn]} 接口={v}")
        print(f"  一致 {same}，不一致 {diff}，仅库有 {only_db}，仅接口有(机型未入库) {only_api}")
        print("  结论：" + ("接口与库内 DOM 结果一致 ✅" if diff == 0
                            else f"存在 {diff} 处不一致，需人工核对 ⚠️"))


if __name__ == "__main__":
    sys.exit(main())
