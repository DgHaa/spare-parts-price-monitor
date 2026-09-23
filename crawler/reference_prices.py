#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨区参考价回退（B 方案，2026-09-23）——让"官方本地区无价"的机型持续有可追溯的参考价。

== 为什么需要它 ==
  OPPO 各区域 REBORN `getProduct` 只返回**当前在售**的精选机型（实测 de 仅 10~18 台、
  ae/tr/jp/mx/my 各 26~124 台），已下架老机型在区域官网查无价（实测跨区复用 CN 的
  marketingModelCode 调 `getPartPriceNew` 会被 OPPO 以 `code=10028 "Data does not exist！"`
  拒绝——这是 OPPO 自认"本地无该机型价"，不是我方抓取失败）。
  批量抓取只遍历"实时在售机型表"，故这批老机型**每季都会被反复判为无价**、且永远不会被
  重新访问。若不处理，比价矩阵里它们将长期是空格。

== 做法 ==
  本地官方无价时，借用**同一季度、同品牌、同归一化机型名**的 CN 官方价作"参考价"：
    - 数据来源 = 本季 cn 抓取结果（同一轮 run_all 已落库），**不再发额外网络请求**；
    - 写入 is_reference=1 / reference_region='cn' / source_url_kind='reference_cn'；
    - currency 保持 CNY、price=cny_price，故其折算 CNY 值恒等于 CN 列，
      前端 min/max 跨国价差计算**天然不会**把它算成本地价（不制造假价差）；
    - 前端以灰色「参考·中国」徽标显式区分，绝不冒充本地官方价。

== 幂等 ==
  目标判定是"本季**没有任何快照**"（而非"没有任何 parts"）——这样每个季度都会
  为当季重新补一次参考价，而同一季度内重复运行不会重写（第二次就查不到了）。
  parts 复用同一 (model_id, name) 主键（db.upsert_part），不会逐季堆重复备件行。
"""
import datetime as _dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根，便于 import db
from db import get_conn, insert_snapshot, upsert_part  # noqa: E402

# 需要 CN 参考价兜底的品牌 → 目标区域（其余品牌各区域官方价齐全，无需兜底）。
# OPPO 是唯一实测存在"区域在售表只含精选机型、老机型无本地价"的品牌。
CN_REFERENCE_REGIONS = {
    "oppo": ("de", "ae", "tr", "mx", "my", "jp"),
}
REFERENCE_REGION = "cn"

# 从 CN 备件行复制到目标机型的 parts 列（canonical_* 由 db.upsert_part 按 name 重算，
# 保证与其他写入路径完全一致，故此处不直接搬运）。
_SNAP_COLS = (
    "price", "currency", "cny_price", "material_fee", "labor_fee", "source_url",
    "tax_included", "has_labor_split", "labor_note", "labor_source_url",
    "rate_source", "rate_as_of",
)


def norm(s):
    """机型名归一化：忽略大小写/空白/标点，但**保留 '+'**。

    为什么必须保留 '+'：OPPO 用 '+' 区分**不同机型**（OPPO A6i 5G vs OPPO A6i+ 5G、
    OPPO Find X8s vs Find X8s+、Reno10 Pro 5G vs Reno10 Pro+ 5G …）。实测本季 CN
    价池里有 13 组这样的"若剥掉 '+' 就同名"的机型；一旦剥掉，两台的备件会被并进同一个
    键，参考价就会把 A6i+ 的价格抄给 A6i（张冠李戴）。保留 '+' 后二者键不同，互不干扰。
    括号等其余标点仍忽略——用来兼容「全角（）/半角()」这类**同一机型**的写法差异。
    """
    return re.sub(r"[^a-z0-9\u4e00-\u9fff+]+", "", (s or "").lower().replace("\u00a0", " "))


def _models_missing_quarter(c, brand, cc, quarter):
    """本季尚无任何快照的机型（= 需要参考价兜底的对象）。"""
    return [dict(r) for r in c.execute(
        """SELECT m.id, m.name FROM models m JOIN brands b ON b.id=m.brand_id
           WHERE b.name=? AND m.country_code=?
             AND NOT EXISTS (
               SELECT 1 FROM parts p JOIN price_snapshots ps ON ps.part_id=p.id
               WHERE p.model_id=m.id AND ps.quarter=?)""", (brand, cc, quarter))]


def _cn_pool(c, brand, quarter):
    """本季 CN 真实官方价池：{归一化机型名: [(part_name, part_type, snap_dict), ...]}。

    只取 is_reference=0（真实抓取）的行，避免"参考价再被当参考源"形成链式传递。
    歧义守卫：同一归一化键若对应**多个不同 CN 机型名**（残留的命名变体碰撞），
    说明无法确定该抄谁 —— 整键剔除（宁可留空，绝不抄错），并回传歧义数供日志如实上报。
    返回 (pool, ambiguous_count)。
    """
    rows = c.execute(
        f"""SELECT m.name model_name, p.name part_name, p.part_type,
                   {', '.join('ps.' + k for k in _SNAP_COLS)}
            FROM models m JOIN brands b ON b.id=m.brand_id
            JOIN parts p ON p.model_id=m.id
            JOIN price_snapshots ps ON ps.part_id=p.id
            WHERE b.name=? AND m.country_code=? AND ps.quarter=?
              AND COALESCE(ps.is_reference, 0)=0 AND ps.cny_price IS NOT NULL""",
        (brand, REFERENCE_REGION, quarter)).fetchall()
    by_key = {}
    for r in rows:
        key = norm(r["model_name"])
        if not key:
            continue
        by_key.setdefault(key, {}).setdefault(r["model_name"], []).append(
            (r["part_name"], r["part_type"], {k: r[k] for k in _SNAP_COLS}))
    pool, ambiguous = {}, 0
    for key, per_model in by_key.items():
        if len(per_model) == 1:
            pool[key] = next(iter(per_model.values()))
        else:
            ambiguous += 1
    return pool, ambiguous


def apply_cn_reference(quarter, brand="oppo", regions=None, dry_run=False, conn=None):
    """为 `brand` 在 `regions` 中"本季无价"的机型补 CN 官方参考价。

    返回 stats dict：{"quarter", "brand", "regions": {cc: {"models","snapshots"}},
                      "cn_pool", "unmatched", "skipped_no_cn"}。
    dry_run=True 只统计不写库。
    """
    regions = tuple(regions or CN_REFERENCE_REGIONS.get(brand, ()))
    own = conn is None
    c = conn or get_conn()
    try:
        pool, ambiguous = _cn_pool(c, brand, quarter)
        stats = {"quarter": quarter, "brand": brand, "regions": {},
                 "cn_pool": len(pool), "ambiguous": ambiguous,
                 "unmatched": 0, "skipped_no_cn": []}
        now = _dt.datetime.now().isoformat(timespec="seconds")
        for cc in regions:
            n_models = n_snaps = 0
            for m in _models_missing_quarter(c, brand, cc, quarter):
                pairs = pool.get(norm(m["name"]))
                if not pairs:
                    stats["unmatched"] += 1
                    # 只留前 50 个样本用于排障，避免整季上千台把内存/日志撑爆
                    if len(stats["skipped_no_cn"]) < 50:
                        stats["skipped_no_cn"].append(f"{cc}/{m['name']}")
                    continue
                for part_name, part_type, snap in pairs:
                    if dry_run:
                        n_snaps += 1
                        continue
                    pid = upsert_part(m["id"], part_name, part_type, conn=c)
                    insert_snapshot(
                        pid, quarter, snap["price"], "CNY", snap["cny_price"],
                        snap["material_fee"], snap["labor_fee"],
                        source_url=snap["source_url"], captured_at=now,
                        tax_included=snap["tax_included"],
                        labor_note=snap["labor_note"],
                        labor_source_url=snap["labor_source_url"],
                        has_labor_split=snap["has_labor_split"], is_seed=0,
                        rate_source=snap["rate_source"], rate_as_of=snap["rate_as_of"],
                        source_url_kind="reference_cn", conn=c,
                        is_reference=1, reference_region=REFERENCE_REGION)
                    n_snaps += 1
                n_models += 1
            stats["regions"][cc] = {"models": n_models, "snapshots": n_snaps}
        if not dry_run and own:
            c.commit()
        return stats
    finally:
        if own:
            c.close()


def format_stats(stats):
    """把 stats 渲染成一行行可读文本（供 run_all / CLI 打印）。"""
    lines = [f"[reference] 季度={stats['quarter']} 品牌={stats['brand']} "
             f"CN 参考池={stats['cn_pool']} 台同名可参考"]
    tm = tp = 0
    for cc, d in stats["regions"].items():
        lines.append(f"  {cc:>3}  机型={d['models']:<4} 参考价行={d['snapshots']}")
        tm += d["models"]; tp += d["snapshots"]
    lines.append(f"  合计  机型={tm} 参考价行={tp}"
                 + (f"；另有 {stats['unmatched']} 台 CN 亦无同名价，保持空"
                    if stats["unmatched"] else "")
                 + (f"；{stats['ambiguous']} 组命名歧义已剔除（宁可空不可错）"
                    if stats.get("ambiguous") else ""))
    return "\n".join(lines)
