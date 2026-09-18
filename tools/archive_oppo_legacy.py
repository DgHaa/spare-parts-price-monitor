"""tools/archive_oppo_legacy.py - 归档 OPPO 非区域化的旧 api_json 机型（可逆清理）。

背景：de/tr/mx/my/jp/ae 的旧 `/cnw/v1/GetPartPrice` 数据**非区域化**——机型表里混有
「Find X3 Pro 火星探索版」「Find X2 Pro 兰博基尼版」等中国限定版，且各区域价格数值
完全相同。这些行以 `model_url_kind='model_api'` 标记（REBORN 行为 `model_page`）。

本脚本把这批 legacy 行**移入归档表**（不是删除），并先复制整库备份，故完全可逆：
  python tools/archive_oppo_legacy.py --dry-run    # 只统计，不改库
  python tools/archive_oppo_legacy.py --execute    # 备份 + 归档（从主表移除）
  python tools/archive_oppo_legacy.py --status     # 查看归档现状
  python tools/archive_oppo_legacy.py --restore    # 从归档表还原回主表

归档后建议重跑这 6 个区域，使重叠机型改用 REBORN 价（断点续跑曾跳过它们）。
"""
import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
COUNTRIES = ("de", "tr", "mx", "my", "jp", "ae")
T_M, T_P, T_S = "models_legacy_archive", "parts_legacy_archive", "price_snapshots_legacy_archive"


def _bid(cur):
    r = cur.execute("select id from brands where name='oppo'").fetchone()
    return r[0] if r else None


def _sel_models(bid):
    qs = ",".join("?" * len(COUNTRIES))
    return (f"select id from models where brand_id=? and country_code in ({qs}) "
            f"and model_url_kind='model_api'"), (bid, *COUNTRIES)


def _sel_parts():
    return f"select id from parts where model_id in (select id from {T_M})"


def _sel_snaps():
    return f"select id from price_snapshots where part_id in (select id from {T_P})"


def counts(cur):
    bid = _bid(cur)
    if bid is None:
        return {}
    mq, mp = _sel_models(bid)
    m = len(cur.execute(mq, mp).fetchall())
    return {"models": m}


def status(cur):
    have = {t: cur.execute(
        "select count(*) from sqlite_master where type='table' and name=?", (t,)).fetchone()[0]
        for t in (T_M, T_P, T_S)}
    if have[T_M]:
        print(f"[archive] {T_M}={cur.execute(f'select count(*) from {T_M}').fetchone()[0]} "
              f"{T_P}={cur.execute(f'select count(*) from {T_P}').fetchone()[0]} "
              f"{T_S}={cur.execute(f'select count(*) from {T_S}').fetchone()[0]}")
    else:
        print("[archive] 尚无归档表")
    bid = _bid(cur)
    if bid is not None:
        qs = ",".join("?" * len(COUNTRIES))
        for cc in COUNTRIES:
            d = dict(cur.execute(
                "select model_url_kind,count(*) from models where brand_id=? and country_code=? group by 1",
                (bid, cc)).fetchall())
            print(f"  主表 {cc}: {d}")


def backup():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = DB.with_suffix(f".db.bak-{ts}")
    shutil.copy2(DB, dst)
    print(f"[backup] {dst.name}")
    return dst


def archive(cur):
    bid = _bid(cur)
    mq, mp = _sel_models(bid)
    for t, src in ((T_M, "models"), (T_P, "parts"), (T_S, "price_snapshots")):
        cur.execute(f"create table if not exists {t} as select * from {src} where 0")
    cur.execute(f"insert into {T_M} select * from models where id in ({mq})", mp)
    n_m = cur.rowcount
    cur.execute(f"insert into {T_P} select * from parts where id in ({_sel_parts()})")
    n_p = cur.rowcount
    cur.execute(f"insert into {T_S} select * from price_snapshots where id in ({_sel_snaps()})")
    n_s = cur.rowcount
    cur.execute(f"delete from price_snapshots where id in ({_sel_snaps()})")
    cur.execute(f"delete from parts where id in ({_sel_parts()})")
    cur.execute(f"delete from models where id in ({mq})", mp)
    return n_m, n_p, n_s


def restore(cur):
    for t, src in ((T_S, "price_snapshots"), (T_P, "parts"), (T_M, "models")):
        cur.execute(f"insert or ignore into {src} select * from {t}")
    return (cur.execute(f"select count(*) from {T_M}").fetchone()[0],
            cur.execute(f"select count(*) from {T_P}").fetchone()[0],
            cur.execute(f"select count(*) from {T_S}").fetchone()[0])


def main(mode):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    if mode == "status":
        status(cur)
    elif mode == "dry-run":
        bid = _bid(cur)
        mq, mp = _sel_models(bid)
        n_m = len(cur.execute(mq, mp).fetchall())
        n_p = len(cur.execute(f"select id from parts where model_id in ({mq})", mp).fetchall())
        n_s = len(cur.execute(
            f"select id from price_snapshots where part_id in "
            f"(select id from parts where model_id in ({mq}))", mp).fetchall())
        print(f"[dry-run] 待归档 legacy(model_api)：models={n_m} parts={n_p} snapshots={n_s}")
        print("[dry-run] 未改库（执行请加 --execute）")
    elif mode == "execute":
        backup()
        n_m, n_p, n_s = archive(cur)
        c.commit()
        print(f"[execute] 已归档并移出主表：models={n_m} parts={n_p} snapshots={n_s}")
        status(cur)
    elif mode == "restore":
        n_m, n_p, n_s = restore(cur)
        c.commit()
        print(f"[restore] 已从归档还原（表内计数 models={n_m} parts={n_p} snapshots={n_s}）")
        status(cur)
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for m in ("dry-run", "execute", "status", "restore"):
        g.add_argument(f"--{m}", action="store_true")
    a = ap.parse_args()
    main(next(m for m in ("dry-run", "execute", "status", "restore") if getattr(a, m.replace("-", "_"))))
