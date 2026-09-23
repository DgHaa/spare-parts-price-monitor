#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清退 OPPO 旧端点 /cnw/v1/GetPartPrice 遗留的污染机型行（2026-09-23）。

== 为什么 ==
  2026-09-22 清退该端点的**价格行**（13,836 行）时，漏清了它创建的**机型行**。
  残留的 1,123 台机型不属于 de/ae/tr/mx/my/jp 任何区域 —— 它们的 source_url 全部指向
  该旧端点，名单里混着 **186 台"一加 OnePlus"机型**、72 台中国专供版
  （兰博基尼版/火星探索版/柯南限定版）、以及 OPPO 智能电视 K9/R1、OPPO 手环等
  中国市场产品线；含中文名的有 498 台。与 REBORN 真实抓到的机型按(区域+名)比对
  **重叠为 0**。

== 判据为什么可靠 ==
  db.upsert_model 的去重键是 (brand_id, country_code, model_key)，model_key 默认取 name。
  因此：**真机型**被 REBORN 重新发现时会同名复用同一行、并把 source_url 覆写为
  support.oppo.com 的机型级链接；**污染机型**永远不会被发现，source_url 会一直停在旧端点。
  故"source_url 仍是旧端点"= 该机型既不在区域目录里、也抓不到价 —— 正是要清的对象。

  双保险：再排除"本季已有非参考价(真实)快照"的行，避免误删任何真拿到价的数据。

== 用法 ==
  python tools/purge_legacy_oppo_models.py            # 预览（只读，打印将删什么）
  python tools/purge_legacy_oppo_models.py --apply    # 执行（先自动备份 DB）
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402

LEGACY_MARK = "%/cnw/v1/GetPartPrice%"

# 待清退机型：source_url 仍是旧端点，且本季没有任何"真实价"快照（双保险）
_VICTIM_SQL = f"""
SELECT m.id, m.country_code, m.name
FROM models m
JOIN brands b ON b.id = m.brand_id
WHERE b.name = 'oppo'
  AND m.source_url LIKE ?
  AND NOT EXISTS (
        SELECT 1 FROM parts p JOIN price_snapshots ps ON ps.part_id = p.id
        WHERE p.model_id = m.id AND COALESCE(ps.is_reference, 0) = 0)
"""


def plan(c):
    """返回 (victims, n_parts, n_snaps, n_ref_snaps)。只读。"""
    victims = list(c.execute(_VICTIM_SQL, (LEGACY_MARK,)))
    ids = [r[0] for r in victims]
    if not ids:
        return victims, 0, 0, 0
    qs = ",".join("?" * len(ids))
    n_parts = c.execute(f"SELECT COUNT(*) FROM parts WHERE model_id IN ({qs})", ids).fetchone()[0]
    n_snaps = c.execute(
        f"""SELECT COUNT(*) FROM price_snapshots WHERE part_id IN
            (SELECT id FROM parts WHERE model_id IN ({qs}))""", ids).fetchone()[0]
    n_ref = c.execute(
        f"""SELECT COUNT(*) FROM price_snapshots WHERE is_reference = 1 AND part_id IN
            (SELECT id FROM parts WHERE model_id IN ({qs}))""", ids).fetchone()[0]
    return victims, n_parts, n_snaps, n_ref


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认只预览）")
    a = ap.parse_args()

    c = sqlite3.connect(str(db.DB_PATH))
    c.row_factory = sqlite3.Row
    victims, n_parts, n_snaps, n_ref = plan(c)

    print("=" * 74)
    print("OPPO 旧端点污染机型清退" + ("（执行）" if a.apply else "（预览，未改动）"))
    print("=" * 74)
    print(f"  待清退机型行        = {len(victims)}")
    print(f"  级联 parts 行       = {n_parts}")
    print(f"  级联 price_snapshots= {n_snaps}  (其中参考价 is_reference=1: {n_ref})")

    if victims:
        from collections import Counter
        print(f"  按区域分布          = {dict(Counter(r['country_code'] for r in victims))}")
        import re
        cjk = re.compile(r"[\u4e00-\u9fff]")
        n_cjk = sum(1 for r in victims if cjk.search(r["name"] or ""))
        n_op = sum(1 for r in victims
                   if (r["name"] or "").startswith(("一加", "OnePlus")))
        print(f"  其中含中文名        = {n_cjk}")
        print(f"  其中『一加』机型    = {n_op}")
        print("\n  样本(前 12):")
        for r in victims[:12]:
            print(f"     [{r['country_code']}] {r['name']}")

    if not a.apply:
        print("\n预览结束。确认无误后加 --apply 执行。")
        c.close()
        return

    if not victims:
        print("\n没有需要清退的行。")
        c.close()
        return

    # 备份（清退不可逆，必须先备份）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_dir = ROOT / "backups"
    bak_dir.mkdir(exist_ok=True)
    bak = bak_dir / f"spare_parts_before_purge_legacy_models_{ts}.db"
    c.close()
    shutil.copy2(db.DB_PATH, bak)
    print(f"\n已备份 -> {bak.relative_to(ROOT)}")

    c = sqlite3.connect(str(db.DB_PATH))
    c.row_factory = sqlite3.Row
    victims, n_parts, n_snaps, n_ref = plan(c)
    ids = [r[0] for r in victims]
    qs = ",".join("?" * len(ids))
    cur = c.cursor()
    cur.execute(f"""DELETE FROM price_snapshots WHERE part_id IN
                    (SELECT id FROM parts WHERE model_id IN ({qs}))""", ids)
    d_snaps = cur.rowcount
    cur.execute(f"DELETE FROM parts WHERE model_id IN ({qs})", ids)
    d_parts = cur.rowcount
    cur.execute(f"DELETE FROM models WHERE id IN ({qs})", ids)
    d_models = cur.rowcount
    c.commit()

    print(f"已删除: models={d_models}  parts={d_parts}  price_snapshots={d_snaps}")
    # 复核
    left = c.execute(f"""SELECT COUNT(*) FROM models m JOIN brands b ON b.id=m.brand_id
        WHERE b.name='oppo' AND m.source_url LIKE ?""", (LEGACY_MARK,)).fetchone()[0]
    orphan = c.execute("""SELECT COUNT(*) FROM price_snapshots ps WHERE
        NOT EXISTS (SELECT 1 FROM parts p WHERE p.id=ps.part_id)""").fetchone()[0]
    print(f"复核: 剩余旧端点机型={left}（应为 0）  孤儿快照={orphan}（应为 0）")
    c.close()


if __name__ == "__main__":
    main()
