"""清空小米/cn 本季 brand_entry（官方 code14 无数据）机型的价，使平台如实显示"无数据"。

背景：这 135 台机型 mi.com 官方维修价接口返回 code14「暂无相关数据」，无官方价可回填；
其现有价为初始爬取的官网渲染表价（brand_entry，未核实）。用户选择清空 → 平台显示"官方未发布/无数据"。

动作（仅针对这 135 台，已确认无其他季度已核实价）：
  - 删除这些机型的全部 price_snapshots 与 parts；
  - models 保留，model_url 仍指向 mi.com 价目页，model_url_kind='brand_entry'、verified=0，
    并在 model_url_locator 注明「官方接口 code14 暂无相关数据」。

破坏性操作前请先备份 spare_parts.db。用法:
  python -u tools/clear_xiaomi_gap.py           # DRY-RUN
  python -u tools/clear_xiaomi_gap.py --apply    # 执行
"""
import sqlite3, json, sys, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
Q = "2026Q3"


def gap_model_ids(c):
    return [r["id"] for r in c.execute("""SELECT DISTINCT m.id FROM models m JOIN brands b ON b.id=m.brand_id
        JOIN parts pt ON pt.model_id=m.id JOIN price_snapshots ps ON ps.part_id=pt.id
        WHERE b.name='xiaomi' AND m.country_code='cn' AND ps.quarter=? AND ps.source_url_kind='brand_entry'""", (Q,))]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(); dry = not args.apply
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    ids = gap_model_ids(c)
    ph = ",".join("?" * len(ids))

    n_snap = c.execute(f"SELECT COUNT(*) n FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id "
                       f"WHERE pt.model_id IN ({ph})", ids).fetchone()["n"]
    n_parts = c.execute(f"SELECT COUNT(*) n FROM parts WHERE model_id IN ({ph})", ids).fetchone()["n"]
    print(f"[{'DRY-RUN' if dry else 'APPLY'}] 目标机型 {len(ids)} 台，将删快照 {n_snap} 条 / 部件 {n_parts} 条", flush=True)

    if dry:
        c.close()
        return

    c.execute(f"DELETE FROM price_snapshots WHERE part_id IN (SELECT id FROM parts WHERE model_id IN ({ph}))", ids)
    c.execute(f"DELETE FROM parts WHERE model_id IN ({ph})", ids)
    note = json.dumps({"reason": "官方维修价接口返回 code14 暂无相关数据，无官方价可回填；"
                                "原渲染表价已按用户选择清空，平台显示无数据",
                       "official_api": "shop_band_wx_price?class_id={cid} -> code=14 暂无相关数据"},
                      ensure_ascii=False)
    c.execute(f"""UPDATE models SET model_url_kind='brand_entry', model_url_verified=0,
                   model_url_locator=?, model_url_checked_at=CURRENT_TIMESTAMP WHERE id IN ({ph})""",
              [note, *ids])
    c.commit()
    # 复核
    left = c.execute(f"""SELECT COUNT(*) n FROM price_snapshots ps JOIN parts pt ON pt.id=ps.part_id
        WHERE pt.model_id IN ({ph})""", ids).fetchone()["n"]
    print(f"执行完成。剩余快照 {left} 条（应为 0）。机型 {len(ids)} 台已标 verified=0。", flush=True)
    c.close()


if __name__ == "__main__":
    main()
