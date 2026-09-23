"""monitor.py - 接口监控与异常巡检（无 LLM）。

职责：
  1) 扫描 run_logs 中 failed / partial 的运行，且 maintenance_queue 里还没有对应 open 项时，
     补写待修队列（防止 crawler 异常路径漏写）。
  2) 行数骤降检测：同一 brand/country 本季行数 < 上季同项的 50% 时，标记异常（页面改版/反爬信号）。
  3) 打印一份可读的健康摘要。

运行：
  python monitor.py                 # 巡检并补写队列
  python monitor.py --report        # 仅打印健康摘要，不写队列

说明：自愈 Agent（WorkBuddy automation）会轮询 maintenance_queue 的 open 项并自动修复。
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

DROPGUARD = 0.5  # 行数骤降阈值：本季 < 上季 50% 视为异常

# run_logs.status → 巡检标记。用 db.STATUS_* 常量做键，避免与状态集合各写一份而漂移。
# 未知状态**不**再被静默归入 FAIL：巡检是最后一道可见性防线，必须能暴露
# 「库里出现了没见过的状态」（非法写入 / 状态集合已扩展但此处未同步）。
STATUS_FLAGS = {
    db.STATUS_SUCCESS: "OK",
    db.STATUS_RESUMED: "RESUME",
    db.STATUS_UNAVAILABLE: "N/A",
    db.STATUS_SKIPPED: "SKIP",
    db.STATUS_FAILED: "FAIL",
    db.STATUS_PARTIAL: "FAIL",
}

# 需要建待修工单的状态（真失败类）
FAILING_STATUSES = (db.STATUS_FAILED, db.STATUS_PARTIAL)


def latest_run_per_scope():
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM run_logs WHERE id IN (SELECT MAX(id) FROM run_logs GROUP BY brand, country)"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def prev_quarter_rows(brand, country, quarter):
    y, q = int(quarter[:4]), int(quarter[-1])
    pq = f"{y-1}Q4" if q == 1 else f"{y}Q{q-1}"
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.row_factory = sqlite3.Row
    n = conn.execute(
        """SELECT COALESCE(SUM(ps.rows_written),0) n FROM run_logs ps
           WHERE ps.brand=? AND ps.country=? AND ps.quarter=? AND ps.status=?""",
        (brand, country, pq, db.STATUS_SUCCESS)).fetchone()["n"]
    conn.close()
    return n


def scan(write_queue=True, report_only=False):
    runs = latest_run_per_scope()
    print(f"=== 监控巡检 {datetime.now().isoformat(timespec='seconds')} 共 {len(runs)} 个 brand/country ===")
    queued = 0
    for r in runs:
        brand, country, status = r["brand"], r["country"], r["status"]
        # 2026-09-23 状态语义拆分：原先 resumed（本季已抓）与 unavailable（官网不提供）、
        # skipped（未收录）都记同一状态，巡检输出无法区分。现分列四种中性/失败标记。
        flag = STATUS_FLAGS.get(status)
        if flag is None:
            flag = "UNKNOWN"
            print(f"  [warn] 未知 run_logs.status={status!r}（非法写入或状态集合已扩展）："
                  f"合法值 {sorted(db.VALID_STATUSES)}", flush=True)
        print(f"  [{flag}] {brand}/{country} 状态={status} 行数={r['rows_written']} "
              f"{('异常:' + r['anomaly_reason']) if r['anomaly_flag'] else ''}")
        if status in FAILING_STATUSES and r["anomaly_flag"] and write_queue and not report_only:
            iid = db.add_issue(brand, country, r["anomaly_reason"] or f"{brand}/{country} 运行失败")
            if iid:
                queued += 1
                print(f"       -> 补写待修队列 #{iid}")
        # 行数骤降检测（仅在本次成功时比较）
        if status == db.STATUS_SUCCESS and r["rows_written"] > 0:
            prev = prev_quarter_rows(brand, country, r["quarter"])
            if prev and r["rows_written"] < prev * DROPGUARD:
                reason = f"{brand}/{country} 本季行数 {r['rows_written']} 较上季 {prev} 骤降 >{int((1-DROPGUARD)*100)}%"
                print(f"       -> 行数骤降异常：{reason}")
                if write_queue and not report_only:
                    iid = db.add_issue(brand, country, reason)
                    if iid:
                        queued += 1
                        print(f"       -> 补写待修队列 #{iid}")
    if queued:
        print(f"=== 本次新增 {queued} 个待修项 ===")
    else:
        print("=== 无新待修项 ===")
    return queued


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="仅打印健康摘要，不写队列")
    args = ap.parse_args()
    db.init_db()
    scan(write_queue=not args.report, report_only=args.report)


if __name__ == "__main__":
    main()
