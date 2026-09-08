"""queue.py - 待修队列命令行（供自愈 Agent 与人工使用）。

自愈 Agent（WorkBuddy automation）修复抓取脚本并重跑验证通过后，调用本工具把工单标记 resolved：
  python queue.py list-open
  python queue.py resolve <id> "<diagnosis>" "<proposed_fix>" [resolved_by]
  python queue.py report            # 同 list-open 的 JSON 形式（便于程序解析）
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
            print("usage: queue.py resolve <id> \"<diagnosis>\" \"<proposed_fix>\" [resolved_by]")
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
