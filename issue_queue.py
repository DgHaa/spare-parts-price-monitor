"""issue_queue.py - 待修队列命令行（供自愈 Agent 与人工使用）。

原名 queue.py —— 2026-09-17 更名为 issue_queue.py：项目根目录在 sys.path 上，
名为 queue.py 的模块会**遮蔽 Python 标准库的 queue**，导致 concurrent.futures /
asyncio.to_thread / multiprocessing 等一切走线程池的代码报
`AttributeError: module 'queue' has no attribute 'SimpleQueue'`（REBORN 并发抓取
改造时实际踩到）。文件名占位冲突，改名是唯一根治办法。

自愈 Agent（WorkBuddy automation）修复抓取脚本并重跑验证通过后，调用本工具把工单标记 resolved：
  python issue_queue.py list-open
  python issue_queue.py resolve <id> "<diagnosis>" "<proposed_fix>" [resolved_by]
  python issue_queue.py report            # 同 list-open 的 JSON 形式（便于程序解析）
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list-open":
        items = db.open_issues()
        for it in items:
            print(f"#{it['id']} {it['brand']}/{it['country']}  {it['issue_summary']}  ({it['detected_at']})")
        print(f"共 {len(items)} 个 open 项")
        return
    if args[0] == "report":
        print(json.dumps(db.open_issues(), ensure_ascii=False))
        return
    if args[0] == "resolve":
        if len(args) < 2:
            print("usage: issue_queue.py resolve <id> \"<diagnosis>\" \"<proposed_fix>\" [resolved_by]")
            return
        iid = int(args[1])
        diagnosis = args[2] if len(args) > 2 else ""
        fix = args[3] if len(args) > 3 else ""
        who = args[4] if len(args) > 4 else "agent"
        db.resolve_issue(iid, diagnosis, fix, who)
        print(f"已标记 #{iid} 为 resolved（by {who}）")
        return
    print("unknown command")


if __name__ == "__main__":
    main()
