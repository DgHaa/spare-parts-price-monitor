"""run_quarterly.py - 季度抓取入口（由 Windows 计划任务 / 手动调用）。

流程：python -m crawler.run 全量抓取 -> 计算本季 vs 上季异动 -> 推送（企微/邮件，受 config.push.json 控制）。
"""
import asyncio
from crawler.run import run_all
from push import push_quarterly_alerts


def main():
    print("=== 季度抓取开始 ===")
    asyncio.run(run_all())
    print("=== 季度抓取结束，计算异动并推送 ===")
    try:
        push_quarterly_alerts()
    except Exception as e:
        print(f"=== 推送阶段跳过（错误）: {e} ===", flush=True)
    print("=== 全部完成 ===")


if __name__ == "__main__":
    main()
