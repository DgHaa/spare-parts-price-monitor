"""tools/repair_oppo_cn_links.py - 修复 oppo/cn 被 backfill_links 覆盖的机型级链接。

背景：api_reborn 模式下 write_rows 已用 KB deep_link 模板把
`https://support.oppo.com/cn/spare-parts-price/#/detail?marketingModelCode=<code>`
写入 model_url / model_page_url（kind=model_page）。但通用 backfill_links 无 api_reborn
采集器，随后把 model_url 清空、model_page_url 覆盖成品牌入口、并把快照
source_url_kind 降级为 brand_entry。

幸运的是快照的 source_url 未被改（仍是深链），故可据此无损还原。
本脚本为一次性修复；crawler/run.py 已改为 api_reborn 不回填，后续不再发生。

用法：python tools/repair_oppo_cn_links.py [--brand oppo] [--country cn] [--dry-run]
"""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "spare_parts.db"
DEEP = "%marketingModelCode%"


def main(brand, country, dry_run):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    bid = cur.execute("select id from brands where name=?", (brand,)).fetchone()
    if not bid:
        print(f"[fail] 无品牌 {brand}")
        return 1
    bid = bid[0]
    ts = datetime.now().isoformat(timespec="seconds")

    n_models = cur.execute(
        """select count(*) from models where brand_id=? and country_code=?""", (bid, country)).fetchone()[0]
    n_snap = cur.execute(
        """select count(*) from price_snapshots ps join parts p on ps.part_id=p.id
           join models m on p.model_id=m.id where m.brand_id=? and m.country_code=?""",
        (bid, country)).fetchone()[0]
    print(f"[in] {brand}/{country}: models={n_models} snapshots={n_snap}")

    deep_models = cur.execute(
        """select count(distinct m.id) from models m join parts p on p.model_id=m.id
           join price_snapshots ps on ps.part_id=p.id
           where m.brand_id=? and m.country_code=? and ps.source_url like ?""",
        (bid, country, DEEP)).fetchone()[0]
    print(f"[check] 快照中含深链的机型: {deep_models}/{n_models}")

    if dry_run:
        print("[dry-run] 未写库")
        return 0

    # 1) 从任一快照取该机型深链，回填 models 的三个链接字段
    cur.execute(
        """update models set
             model_url = (select ps.source_url from price_snapshots ps
                          join parts p on ps.part_id=p.id
                          where p.model_id=models.id and ps.source_url like ? limit 1),
             model_url_kind = 'model_page',
             model_url_locator = null,
             model_url_verified = 1,
             model_url_checked_at = ?,
             model_page_url = (select ps.source_url from price_snapshots ps
                               join parts p on ps.part_id=p.id
                               where p.model_id=models.id and ps.source_url like ? limit 1)
           where brand_id=? and country_code=?""",
        (DEEP, ts, DEEP, bid, country))
    n_m = cur.rowcount

    # 2) 快照 source_url_kind 还原为 model_page
    cur.execute(
        """update price_snapshots set source_url_kind='model_page'
           where part_id in (select p.id from parts p join models m on p.model_id=m.id
                             where m.brand_id=? and m.country_code=?)""",
        (bid, country))
    n_s = cur.rowcount
    c.commit()

    # 3) 复核
    chk = cur.execute(
        """select count(*) from models where brand_id=? and country_code=?
           and model_url_kind='model_page' and model_page_url like ?""",
        (bid, country, DEEP)).fetchone()[0]
    print(f"[done] models 更新={n_m} snapshots 更新={n_s}；复核 model_page 级={chk}/{n_models}")
    c.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="oppo")
    ap.add_argument("--country", default="cn")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    raise SystemExit(main(a.brand, a.country, a.dry_run))
