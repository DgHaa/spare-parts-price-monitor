"""tools/archive_xiaomi_ws_dupes.py - 归档小米「仅空白字符不同」的重复机型行（可逆清理）。

背景（2026-09-17）：
  xiaomi/cn 的抓取从「DOM 点选」改为「官方 JSONP 接口」后，机型名改由接口的
  `shop_class_info` 提供，而官方标题里偶有**连续双空格**（如
  "Xiaomi 15S Pro  16GB+512GB 龙鳞纤维版"）。旧 DOM 抓取拿到的是单空格版本。
  而 models 的唯一键是 (brand_id, country_code, model_key)，model_key 直接取原文名，
  于是同一台真实机型产生了两行 —— 实测 xiaomi/cn 出现 77 组重复（1014 行 →
  空白归一化后仅 937 台真实机型）。其余品牌 0 组重复（它们的抓取文本稳定）。

  ⚠️ 这批重复是「同一 SKU 的新旧两份记录」，不是两台不同机器。留着会让前端
  出现同名机型、机型计数虚高，且旧那份的价格是 09-03 的陈旧数据。

判定规则（同名组内）：
  保留「最新价行 captured_at 最大」的那一行（= 本轮接口刷新的新行），其余移入归档表。
  再按 class_id 一致为条件，把旧行的取证元数据（model_url_locator /
  model_url_verified / model_page_url）**合并到保留行**——旧行的 verified=1 是针对
  同一个 class_id 的、货真价实的实测结论，直接丢弃会白丢证据。

  旧行连同其 parts / price_snapshots 一起归档（陈旧价行随之下线），主表只留新行。
  全程先整库备份，可 --restore 还原。

用法：
  python tools/archive_xiaomi_ws_dupes.py --dry-run
  python tools/archive_xiaomi_ws_dupes.py --execute
  python tools/archive_xiaomi_ws_dupes.py --status
  python tools/archive_xiaomi_ws_dupes.py --restore
"""
import argparse
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
BRAND = "xiaomi"
COUNTRY = "cn"
T_M, T_P, T_S = "models_legacy_archive", "parts_legacy_archive", "price_snapshots_legacy_archive"

WS = re.compile(r"\s+")


def _norm(s):
    return WS.sub("", s or "")


def _class_id(url):
    """从取价链接里抠出 class_id，用于判断两行的取证链接是否指向同一 SKU。"""
    if not url:
        return None
    m = re.search(r"class_id=(\d+)", url)
    return m.group(1) if m else None


def _plan(cur):
    """返回 (keep_ids, [loser_ids], merges)。merges 记录要合并到保留行的取证元数据。

    归档判据：同一空白归一化名组内，**价行日期**（captured_at 的 YYYY-MM-DD）严格
    早于组内最新者的行才归档。只按"空白相同"归档是错的 —— 小米官方目录自身就有 6 组
    只差一个空格的重名条目（"小米9 SE…" vs "小米9SE…"，class_id 不同、价表完全相同），
    它们是官方目录里的重复条目、且是**同一次抓取**产生的等价行；若把它们也归档，
    下次抓取会按缺失的标题重新建行，清理就不可幂等。

    用"日期"而非"精确到秒的时间戳"比较，是因为同一次抓取里不同机型的 captured_at
    会差 1~2 秒（实测 "小米9 SE 深空灰 128GB" 两行是 19:58:53 / 19:58:54），
    按秒比较会把这组同源等价行误判成"旧的该归档"。按天比较对秒级抖动免疫，
    又能正确区分"09-03 旧抓取"与"09-17 新抓取"。
    """
    bid = cur.execute("select id from brands where name=?", (BRAND,)).fetchone()
    if not bid:
        return None, [], []
    bid = bid[0]
    rows = cur.execute(
        """select m.id, m.name, m.model_url, m.model_url_locator, m.model_url_verified,
                  m.model_page_url,
                  (select max(ps.captured_at) from price_snapshots ps
                     join parts p on p.id = ps.part_id
                    where p.model_id = m.id) as latest
             from models m where m.brand_id=? and m.country_code=?""",
        (bid, COUNTRY)).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(_norm(r[1]), []).append(r)
    keep_ids, loser_ids, merges = [], [], []
    for key, grp in groups.items():
        if len(grp) < 2:
            continue
        newest_day = max((r[6] or "")[:10] for r in grp)
        # 只把"价行日期严格更旧"的移出；同一天的一律保留（含官方目录自带的同名条目）
        losers = [r for r in grp if (r[6] or "")[:10] < newest_day]
        if not losers:
            continue
        keep = max((r for r in grp if r not in losers), key=lambda r: (r[6] or "", r[0]))
        keep_ids.append(keep[0])
        kc = _class_id(keep[2])
        for lo in losers:
            loser_ids.append(lo[0])
            # class_id 一致才合并取证元数据（否则可能是不同 SKU，合并会造假证据）
            if kc and kc == _class_id(lo[2]):
                merges.append({
                    "model_id": keep[0],
                    "model_url_locator": lo[3],
                    "model_url_verified": lo[4],
                    "model_page_url": lo[5],
                    "from_model_id": lo[0],
                })
    return keep_ids, loser_ids, merges


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


def status(cur):
    if _has_archive(cur):
        print(f"[archive] {T_M}={cur.execute(f'select count(*) from {T_M}').fetchone()[0]} "
              f"{T_P}={cur.execute(f'select count(*) from {T_P}').fetchone()[0]} "
              f"{T_S}={cur.execute(f'select count(*) from {T_S}').fetchone()[0]}")
    else:
        print("[archive] 尚无归档表")
    bid = cur.execute("select id from brands where name=?", (BRAND,)).fetchone()
    if bid:
        n = cur.execute("select count(*) from models where brand_id=? and country_code=?",
                        (bid[0], COUNTRY)).fetchone()[0]
        _, losers, _ = _plan(cur)
        print(f"  主表 {BRAND}/{COUNTRY}: 机型 {n} 行，其中空白重复待归档 {len(losers)} 行")


def backup():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = DB.with_suffix(f".db.bak-{ts}")
    shutil.copy2(DB, dst)
    print(f"[backup] {dst.name}")
    return dst


def execute(cur):
    keep_ids, loser_ids, merges = _plan(cur)
    if not loser_ids:
        print("[execute] 没有需要归档的重复行")
        return 0, 0, 0, 0
    for t, src in ((T_M, "models"), (T_P, "parts"), (T_S, "price_snapshots")):
        cur.execute(f"create table if not exists {t} as select * from {src} where 0")
    n_m, n_p, n_s = _counts(cur, loser_ids)
    qs = ",".join("?" * len(loser_ids))
    # 1) 先把旧行的取证元数据合并进保留行（仅 class_id 一致时，见 _plan）
    n_merge = 0
    for mg in merges:
        cur.execute(
            """update models set
                 model_url_locator=coalesce(model_url_locator, ?),
                 model_url_verified=coalesce(model_url_verified, ?),
                 model_page_url=coalesce(model_page_url, ?)
               where id=?""",
            (mg["model_url_locator"], mg["model_url_verified"], mg["model_page_url"],
             mg["model_id"]))
        n_merge += cur.rowcount
    # 2) 归档旧行（连同 parts / snapshots），再从主表移除
    cur.execute(f"insert into {T_M} select * from models where id in ({qs})", loser_ids)
    cur.execute(f"insert into {T_P} select * from parts where model_id in ({qs})", loser_ids)
    cur.execute(f"insert into {T_S} select * from price_snapshots where part_id in "
                f"(select id from parts where model_id in ({qs}))", loser_ids)
    cur.execute(f"delete from price_snapshots where part_id in "
                f"(select id from parts where model_id in ({qs}))", loser_ids)
    cur.execute(f"delete from parts where model_id in ({qs})", loser_ids)
    cur.execute(f"delete from models where id in ({qs})", loser_ids)
    print(f"[execute] 归档并移出主表：models={n_m} parts={n_p} snapshots={n_s}；"
          f"取证元数据合并到保留行 {n_merge} 处")
    return n_m, n_p, n_s, n_merge


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
        keep_ids, loser_ids, merges = _plan(cur)
        n_m, n_p, n_s = _counts(cur, loser_ids)
        print(f"[dry-run] 重复组 {len(keep_ids)} 组，待归档 models={n_m} parts={n_p} "
              f"snapshots={n_s}，可合并取证元数据 {len(merges)} 处")
        if loser_ids:
            bid = cur.execute("select id from brands where name=?", (BRAND,)).fetchone()[0]
            before = cur.execute("select count(*) from models where brand_id=? and country_code=?",
                                 (bid, COUNTRY)).fetchone()[0]
            print(f"[dry-run] 主表机型 {before} → {before - n_m} 行")
        print("[dry-run] 未改库（执行请加 --execute）")
    elif mode == "execute":
        backup()
        execute(cur)
        c.commit()
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
    main(next(m for m in ("dry-run", "execute", "status", "restore")
              if getattr(a, m.replace("-", "_"))))
