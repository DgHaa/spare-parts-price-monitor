"""tools/migrate_oppo_regions_kb.py - 把 OPPO de/tr/mx/my/jp/ae 的 KB 配方迁移到 api_reborn。

依据（tools/probe_oppo_regions.py 实测 2026-09-17）：
  区域  可用节点             OPPO(brandCode=11) 机型数   币种
  de    par-sow-cms          10                          EUR
  tr    sgp-sow-cms          29                          TRY
  mx    sgp-sow-cms          53                          MXN
  my    sgp-sow-cms          124                         MYR
  jp    sgp-sow-cms          26                          JPY
  ae    sgp-sow-cms          99                          AED
旧 api_json 走 sgp /cnw/v1/GetPartPrice，实测其机型表**非区域化**（de/my 列表里出现
「Find X3 Pro 火星探索版」「Find X2 Pro 兰博基尼版」等中国限定版），且各区域价格数值
完全相同（统一基准价表）；REBORN 返回的才是该市场真实在售机型与本地化价格。

用法：python tools/migrate_oppo_regions_kb.py [--dry-run]
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

# KB 位置：优先仓库内副本 references/kb（已随仓库同步），缺失时回退到 skill 目录
_REPO_KB = Path(__file__).resolve().parents[1] / "references" / "kb" / "oppo.json"
KB = _REPO_KB if _REPO_KB.exists() else Path(
    r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/references/kb/oppo.json")
ALL_HOSTS = ["sow-cms.oppo.com", "par-sow-cms.oppo.com", "sgp-sow-cms.oppo.com"]
REG = {
    "de": ("par-sow-cms.oppo.com", "DE", "de", "de-DE", "EUR"),
    "tr": ("sgp-sow-cms.oppo.com", "TR", "tr", "tr-TR", "TRY"),
    "mx": ("sgp-sow-cms.oppo.com", "MX", "mx", "es-MX", "MXN"),
    "my": ("sgp-sow-cms.oppo.com", "MY", "my", "ms-MY", "MYR"),
    "jp": ("sgp-sow-cms.oppo.com", "JP", "jp", "ja-JP", "JPY"),
    "ae": ("sgp-sow-cms.oppo.com", "AE", "ae", "ar-AE", "AED"),
}
COUNTS = {"de": 10, "tr": 29, "mx": 53, "my": 124, "jp": 26, "ae": 99}


def build(cc, host, rg, iso, lang, cur):
    hosts = [host] + [h for h in ALL_HOSTS if h != host]
    return {
        "mode": "api_reborn",
        "api": {
            "base_url": f"https://{host}/oppo-api",
            "candidate_hosts": hosts,
            "product_list": "/basic/v1/getProduct",
            "price_detail": "/basic/v1/getPartPriceNew",
            "region": rg, "region_iso": iso, "iso_language": lang,
            "brand_code": "11",
            "method": "POST",
            "fetch_mode": "http",
            "bypass_proxy": False,
            "credentials": "omit",
            "category_filter": None,
            "model_field": "marketingModelName",
            "model_code_field": "marketingModelCode",
            "deep_link": f"https://support.oppo.com/{cc}/spare-parts-price/#/detail?marketingModelCode={{code}}",
            "model_match_note": "机型名匹配必须精确优先（忽略大小写/空白）再退子串——cn 实测三个含 'pad 5' 的机型（Pad 5 850/11、柔光版 950/9、Pro 999），子串匹配会取错价。",
            "note": (f"OPPO 官网 2026 起备件价改走 REBORN：POST /basic/v1/getProduct"
                     f"（region={rg}/regionIsoCode2={iso}/isoLanguageCode={lang}/sourceRoute=1，"
                     f"brandCode=11 仅取 OPPO 品牌：全表含 OnePlus(12)/realme(13)，须过滤）"
                     f"→ 再 POST /basic/v1/getPartPriceNew(+marketingModelCode) 取价。"
                     f"{cc} 实测 {COUNTS[cc]} 台 OPPO 机型、本地币种 {cur}。"
                     f"必须 POST，GET 返空；区域节点不同（{host}）。"
                     f"旧 /cnw/v1/GetPartPrice 机型表非区域化（含中国限定版）且各区价格数值相同，已弃用。"),
        },
    }


def main(dry_run):
    d = json.loads(KB.read_text(encoding="utf-8"))
    ts = datetime.now().strftime("%Y-%m-%d")
    if not dry_run:
        KB.with_suffix(".json.bak").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    for cc, (host, rg, iso, lang, cur) in REG.items():
        rec = d["countries"][cc][0]
        old_mode = rec.get("query", {}).get("mode")
        rec["query"] = build(cc, host, rg, iso, lang, cur)
        rec["extract"] = {"api": {
            "part_field": "partName", "price_field": "retailPrice",
            "type_field": "lv3ClassificationName", "labor_field": "laborCostAmount",
            "currency_in_json": True, "currency": cur}}
        rec["currency"] = cur
        rec["status"] = "verified"
        rec["updated_at"] = ts
        rec["evidence"]["captured_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M")
        rec["evidence"]["captured_url"] = f"https://support.oppo.com/{cc}/spare-parts-price/"
        rec["notes"] = (f"【2026-09-17 迁移到 REBORN 并实测】{old_mode} → api_reborn。"
                        f"节点 {host}；OPPO 机型 {COUNTS[cc]} 台；币种 {cur}。"
                        f"机型深链 #/detail?marketingModelCode=<code> 已实测可渲染该机型价表（my 抽验通过）。"
                        f"旧 /cnw/v1/ 数据非区域化（de/my 列表含中国限定版、各区价格数值相同），已弃用。")
        print(f"[kb] {cc}: {old_mode} -> api_reborn  host={host}  {COUNTS[cc]} 台  {cur}")
    if dry_run:
        print("[dry-run] 未写文件")
        return
    KB.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[kb] 已写回 {KB}（备份 {KB.with_suffix('.json.bak').name}）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
