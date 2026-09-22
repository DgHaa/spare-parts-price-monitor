"""tools/archive_ws_dup_models.py - 归档「仅空白字符不同」的重复机型行（可逆清理）。

背景（2026-09-22 自愈发现）：
  三星德站（samsung/de）的底层 REST（seg.apix.de/smart-repair/v3-graph/）对**同一台机器**
  存在两个条目，名字只差一个 NBSP：
      'Galaxy Tab A9\\xa0(Wi-Fi) - SM-X110'   (guid 1023, 屏幕 158 / 摄像头 109 / 电池 82)
      'Galaxy Tab A9 (Wi-Fi) - SM-X110'      (guid 717,  屏幕 145 / 摄像头 99  / 电池 83)
  两条的 `modelCode` 都是 **SM-X110N**（官方自己认定同一 SKU），但价表不同。
  而 `models` 的唯一键是 (brand_id, country_code, model_key)，`model_key` 直接取官方原文名
  → 同一台平板在库里/前端出现**两行、价格还互相冲突**。

  抓取侧已修：`crawler/samsung_api.py` 新增 `_norm_ws()` + `dedupe_model_rows()`，
  在**所有区域共用**的发现落点按"空白归一后同名"去重（保留命名最规范的那条），
  官方重名条目不会再落成两行。本工具负责**清理已经落库的历史重复行**。

判定规则（与抓取侧去重保持一致，保证幂等）：
  1. 分组键 = `_norm_ws(name).casefold()`（NBSP/窄空格 → 空格、多空格并一、去首尾、忽略大小写）。
     **只归一空白，不删空格、不改大小写** —— 因此不会把 '小米9 SE' 与 '小米9SE' 这类
     官方目录自带的两个不同条目并掉（那批是等价行，清理它们不可幂等，见
     tools/archive_xiaomi_ws_dupes.py 的说明）。
  2. 组内保留「原文名本身就已归一」的那条（无 NBSP 等格式噪声）→ 它正是抓取侧今后
     会继续写入的 model_key，故清理后不会被下一次抓取重新建行（幂等）。
     若组内多条都不规范，则保留 id 最小者。
  3. 其余连同 parts / price_snapshots 一起移入 *_legacy_archive 表。价格差异不隐藏：
     `--dry-run` 会逐条打印被归档行的价表，请据实记入 KB notes。

全程先整库备份，可 --restore 还原。

用法：
  python tools/archive_ws_dup_models.py --brand samsung --country de --dry-run
  python tools/archive_ws_dup_models.py --brand samsung --country de --execute
  python tools/archive_ws_dup_models.py --brand samsung --country de --status
  python tools/archive_ws_dup_models.py --restore
"""
import argparse
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
T_M, T_P, T_S = "models_legacy_archive", "parts_legacy_archive", "price_snapshots_legacy_archive"

WS = re.compile(r"\s+", re.UNICODE)


def _norm_ws(name):
    """格式性空白归一：NBSP 等 → 空格、多空格并一、去首尾（与 samsung_api._norm_ws 同口径）。"""
    s = unicodedata.normalize("NFKC", name or "")
    return WS.sub(" ", s).replace("\u00a0", " ").strip()


def _plan(cur, brand, country):
    """返回 (keep_ids, loser_ids)。"""
    row = cur.execute("select id from brands where name=?", (brand,)).fetchone()
    if not row:
        return [], []
    bid = row[0]
    rows = cur.execute(
        "select id, name from models where brand_id=? and country_code=? order by id",
        (bid, country)).fetchall()
    groups = {}
    for mid, name in rows:
        groups.setdefault(_norm_ws(name).casefold(), []).append((mid, name))
    keep_ids, loser_ids = [], []
    for key, grp in groups.items():
        if len(grp) < 2:
            continue
        # 优先保留"原文名==归一化名"的那条（抓取侧今后继续写入的 key）
        clean = [g for g in grp if g[1] == _norm_ws(g[1])]
        keep = min(clean or grp, key=lambda g: g[0])
        keep_ids.append(keep[0])
        loser_ids.extend(g[0] for g in grp if g[0] != keep[0])
    return keep_ids, loser_ids


def _rows_of(cur, model_id):
    return cur.execute(
        """select p.name, s.price, s.currency, s.captured_at from parts p
             left join price_snapshots s on s.part_id=p.id
            where p.model_id=? order by p.name""", (model_id,)).fetchall()


def _counts(cur, loser_ids):
    if not loser_ids:
        return 0, 0, 0
    qs = ",".join("?" * len(loser_ids))
    n_p = cur.execute(f"select count(*) from parts where model_id in ({qs})",
                      loser_ids).fetchone()[0]
    n_s = cur.execute(
        f"select count(*) from price_snapshots where part_id in "
        f"(select id from parts where model_id in ({qs}))", loser_ids).fetchone()[0]
    return len(loser_ids), n_p, n_s


def _has_archive(cur):
    return cur.execute("select count(*) from sqlite_master where type='table' and name=?",
                       (T_M,)).fetchone()[0] > 0


def status(cur, brand, country):
    if _has_archive(cur):
        print(f"[archive] {T_M}={cur.execute(f'select count(*) from {T_M}').fetchone()[0]} "
              f"{T_P}={cur.execute(f'select count(*) from {T_P}').fetchone()[0]} "
              f"{T_S}={cur.execute(f'select count(*) from {T_S}').fetchone()[0]}")
    else:
        print("[archive] 尚无归档表")
    row = cur.execute("select id from brands where name=?", (brand,)).fetchone()
    if row:
        n = cur.execute("select count(*) from models where brand_id=? and country_code=?",
                        (row[0], country)).fetchone()[0]
        _, losers = _plan(cur, brand, country)
        print(f"  主表 {brand}/{country}: 机型 {n} 行，其中空白重复待归档 {len(losers)} 行")


def backup():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = DB.parent / f"{DB.name}.bak-wsdup-{ts}"
    shutil.copy2(DB, dst)
    print(f"[backup] {dst.name}")
    return dst


def execute(cur, brand, country):
    keep_ids, loser_ids = _plan(cur, brand, country)
    if not loser_ids:
        print("[execute] 没有需要归档的重复行")
        return 0, 0, 0
    for t, src in ((T_M, "models"), (T_P, "parts"), (T_S, "price_snapshots")):
        cur.execute(f"create table if not exists {t} as select * from {src} where 0")
    n_m, n_p, n_s = _counts(cur, loser_ids)
    qs = ",".join("?" * len(loser_ids))
    cur.execute(f"insert into {T_M} select * from models where id in ({qs})", loser_ids)
    cur.execute(f"insert into {T_P} select * from parts where model_id in ({qs})", loser_ids)
    cur.execute(f"insert into {T_S} select * from price_snapshots where part_id in "
                f"(select id from parts where model_id in ({qs}))", loser_ids)
    cur.execute(f"delete from price_snapshots where part_id in "
                f"(select id from parts where model_id in ({qs}))", loser_ids)
    cur.execute(f"delete from parts where model_id in ({qs})", loser_ids)
    cur.execute(f"delete from models where id in ({qs})", loser_ids)
    print(f"[execute] 归档并移出主表：models={n_m} parts={n_p} snapshots={n_s}；"
          f"保留 rows={keep_ids}")
    return n_m, n_p, n_s


def restore(cur):
    for t, src in ((T_S, "price_snapshots"), (T_P, "parts"), (T_M, "models")):
        cur.execute(f"insert or ignore into {src} select * from {t}")
    return (cur.execute(f"select count(*) from {T_M}").fetchone()[0],
            cur.execute(f"select count(*) from {T_P}").fetchone()[0],
            cur.execute(f"select count(*) from {T_S}").fetchone()[0])


def main(mode, brand, country):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    if mode == "status":
        status(cur, brand, country)
    elif mode == "dry-run":
        keep_ids, loser_ids = _plan(cur, brand, country)
        n_m, n_p, n_s = _counts(cur, loser_ids)
        print(f"[dry-run] {brand}/{country} 空白重复组 {len(keep_ids)} 组，"
              f"待归档 models={n_m} parts={n_p} snapshots={n_s}")
        for k in keep_ids:
            keep = cur.execute("select id,name from models where id=?", (k,)).fetchone()
            print(f"  保留 id={keep[0]} {keep[1]!r}")
            for nm, pr, cur_, cap in _rows_of(cur, k):
                print(f"       {nm}: {pr} {cur_} @{cap}")
        for lo in loser_ids:
            loser = cur.execute("select id,name from models where id=?", (lo,)).fetchone()
            print(f"  归档 id={loser[0]} {loser[1]!r}（价格差异须记入 KB notes）")
            for nm, pr, cur_, cap in _rows_of(cur, lo):
                print(f"       {nm}: {pr} {cur_} @{cap}")
        print("[dry-run] 未改库（执行请加 --execute）")
    elif mode == "execute":
        backup()
        execute(cur, brand, country)
        c.commit()
        status(cur, brand, country)
    elif mode == "restore":
        n_m, n_p, n_s = restore(cur)
        c.commit()
        print(f"[restore] 已从归档还原（归档表内计数 models={n_m} parts={n_p} snapshots={n_s}）")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for m in ("dry-run", "execute", "status", "restore"):
        g.add_argument(f"--{m}", action="store_true")
    ap.add_argument("--brand", default="samsung")
    ap.add_argument("--country", default="de")
    a = ap.parse_args()
    main(next(m for m in ("dry-run", "execute", "status", "restore")
              if getattr(a, m.replace("-", "_"))), a.brand, a.country)
