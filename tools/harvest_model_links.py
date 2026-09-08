"""tools/harvest_model_links.py —— 机型级取证链接的全量回填 / 校验 / 审计 CLI。

本脚本只是薄壳：采集与校验逻辑全部在 crawler/model_links.py（与抓取链路 crawler/run.py
共用同一份实现），避免两份代码分叉——历史教训：曾因 CLI 里单独写了一份小米校验逻辑，
把成功码误判成 0（小米实际返回 200），导致 344 条已可用链接被全判为"未通过"。

用法：
  python tools/harvest_model_links.py --brand vivo                 # 该品牌全部国家
  python tools/harvest_model_links.py --brand xiaomi --country cn   # 指定国家
  python tools/harvest_model_links.py --all                         # 全品牌
  python tools/harvest_model_links.py --brand apple --dry-run       # 只跑不写库
  python tools/harvest_model_links.py --brand oppo --max-verify 20  # 限量实测（抽查）

产物：output/model_links_report.json（合并写入，含 unmatched / unverified / 样例 locator）
      审计视图另见 API：GET /api/model_links?brand=&country=&only_bad=1
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.core import launch_browser                       # noqa: E402
from crawler.model_links import HARVEST, backfill_links, save_report  # noqa: E402


async def main():
    ap = argparse.ArgumentParser(description="为每台机型生成并实测机型级取证链接")
    ap.add_argument("--brand", help="vivo | xiaomi | apple | samsung | oppo")
    ap.add_argument("--country", help="国家码，如 cn/de/my；省略=该品牌全部国家")
    ap.add_argument("--all", action="store_true", help="全部品牌")
    ap.add_argument("--dry-run", action="store_true", help="只采集与校验，不写库")
    ap.add_argument("--max-verify", type=int, default=0,
                    help="每国最多实测校验条数，0=全量（默认）")
    args = ap.parse_args()

    brands = list(HARVEST) if args.all else ([args.brand] if args.brand else [])
    if not brands:
        ap.error("需要 --brand 或 --all")

    pw, browser = await launch_browser()
    report = {}
    try:
        for brand in brands:
            if brand not in HARVEST:
                print(f"[skip] 无 {brand} 机型链接采集器", flush=True)
                continue
            print(f"\n===== {brand} =====", flush=True)
            report.update(await backfill_links(
                browser, brand, args.country,
                max_verify=args.max_verify, dry_run=args.dry_run) or {})
    finally:
        await browser.close()
        await pw.stop()

    if report:
        print(f"\n[saved] {save_report(report)}", flush=True)
    # 汇总一屏，方便直接判断"是否每台机型都有本机型链接"
    print("\n%-14s %6s %8s %8s  %s" % ("品牌/国家", "机型", "有链接", "已校验", "kind"))
    for k, v in sorted(report.items()):
        if "error" in v:
            print("%-14s  ERROR: %s" % (k, v["error"]))
            continue
        print("%-14s %6d %8d %8d  %s" % (k, v["n_models"], v["n_with_url"],
                                         v["n_verified"], v.get("kind")))


if __name__ == "__main__":
    asyncio.run(main())
