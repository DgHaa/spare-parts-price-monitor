#!/usr/bin/env python3
"""test_probe_hedge.py — 候选节点「对冲试打」的行为验证（不联网，全部用假节点）。

验证 crawler.run._probe_hosts_for_models 的四条关键行为：
  ① 正常情况（首发节点很快返回）：只发 1 个请求，耗时与顺序试打一致 —— 不能因为
     改成对冲就白白多发请求、给对端加压；
  ② 首发节点黑洞（TCP 通但不回包，模拟超时型故障）：不等它，hedge 秒后叠加下一个，
     总耗时 ≈ hedge + 次节点耗时，而不是等满 _http_post_json 的最坏 84s；
  ③ 全部候选都失败：如实返回 (None, None)，且每种失败原因都打印出来（可诊断）；
  ④ probe_hedge=0：不设先发优势，所有候选同时起跑。

用法：python tools/test_probe_hedge.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根
# 导入顺序：crawler.core 会注入 skill scripts 目录，之后才能 import executor
from crawler.core import launch_browser  # noqa: E402,F401
import crawler.run as run  # noqa: E402

HEDGE = run._PROBE_HEDGE_S  # 用实现里的默认值，避免测试与代码两处默认漂移


class FakeClock:
    """记录每个节点的调用时刻与并发峰值，用于断言"谁在什么时候被打了"。"""

    def __init__(self, plan):
        # plan: {host: {"delay": 秒, "resp": 返回体 或 "hang": True}}
        self.plan = plan
        self.calls = []          # [(host, t_rel)]
        self.inflight = 0
        self.peak = 0
        self.t0 = None

    async def post(self, api, cc, code=None, page=None, path_list=None, path_price=None,
                   tries=None, to=None, **kw):
        host = api["base_url"].split("//")[-1].split("/")[0]
        p = self.plan.get(host, {})
        self.calls.append((host, round(time.perf_counter() - self.t0, 2)))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if p.get("hang"):
                await asyncio.sleep(p.get("delay", 30))       # 黑洞：永不返回有效数据
                return {"code": 0, "msg": "hang"}
            await asyncio.sleep(p.get("delay", 0.05))
            return p.get("resp", {"code": 1, "data": [{"marketingModelName": "X",
                                                       "marketingModelCode": "1"}]})
        finally:
            self.inflight -= 1


def _api(hosts, **over):
    api = {"base_url": hosts[0], "candidate_hosts": hosts, "region": "cn",
           "region_iso": "cn", "fetch_mode": "http"}
    api.update(over)
    return api


async def _case(name, hosts, plan, expect, **api_over):
    clock = FakeClock(plan)
    orig = run.executor.reborn_post
    run.executor.reborn_post = clock.post
    clock.t0 = time.perf_counter()
    try:
        data, host = await run._probe_hosts_for_models(
            hosts, _api(hosts, **api_over), "cn", None, "/basic/v1/getProduct",
            "/basic/v1/getPartPriceNew")
    finally:
        run.executor.reborn_post = orig
        el = time.perf_counter() - clock.t0

    got = {"win_host": host, "has_data": bool(data), "elapsed": el,
           "n_calls": len(clock.calls), "peak": clock.peak}
    ok = True
    why = []
    for k, v in expect.items():
        if k == "elapsed_lt":
            if not el < v:
                ok = False
                why.append(f"耗时 {el:.2f}s 未 < {v}s")
        elif k == "elapsed_ge":
            if not el >= v:
                ok = False
                why.append(f"耗时 {el:.2f}s 未 >= {v}s")
        elif got.get(k) != v:
            ok = False
            why.append(f"{k}={got.get(k)!r} 期望 {v!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"      胜出节点={host}  有数据={bool(data)}  耗时={el:.2f}s  "
          f"请求数={len(clock.calls)}  并发峰值={clock.peak}")
    print(f"      调用序列={clock.calls}")
    if why:
        print(f"      ✗ {'; '.join(why)}")
    return ok


async def main():
    hosts3 = ["h0.blackhole", "h1.good", "h2.never"]
    ok = True

    # ① 首发即成功：只能发 1 个请求
    ok &= await _case(
        "首发节点正常 → 只发 1 个请求（不给对端加班）",
        ["h0.good", "h1.spare"],
        {"h0.good": {"delay": 0.05}},
        {"win_host": "h0.good", "has_data": True, "n_calls": 1, "elapsed_lt": 0.6})

    # ② 首发黑洞：hedge 秒后叠加，总耗时 ≈ hedge + 次节点耗时
    ok &= await _case(
        "首发节点黑洞 → hedge 后叠加次节点（不等满超时）",
        hosts3,
        {"h0.blackhole": {"hang": True, "delay": 30},
         "h1.good": {"delay": 0.2},
         "h2.never": {"hang": True, "delay": 30}},
        {"win_host": "h1.good", "has_data": True, "elapsed_ge": HEDGE,
         "elapsed_lt": HEDGE + 1.2})

    # ③ 首个快速报错 + 第二个返回 0 机型 + 第三个 code!=1 → 全失败且原因可诊断
    ok &= await _case(
        "全部候选失败 → 返回空并逐条打印失败原因",
        hosts3,
        {"h0.blackhole": {"resp": {"error": "URLError: timed out"}},
         "h1.good": {"resp": {"code": 1, "data": []}},
         "h2.never": {"resp": {"code": 500, "msg": "gateway"}}},
        {"win_host": None, "has_data": False, "n_calls": 3, "elapsed_lt": 3.0})

    # ④ hedge=0：不设先发优势，候选同时起跑（峰值并发=候选数）
    ok &= await _case(
        "probe_hedge=0 → 所有候选同时竞速",
        ["h0.a", "h1.b", "h2.c"],
        {"h0.a": {"delay": 0.3}, "h1.b": {"delay": 0.3}, "h2.c": {"delay": 0.3}},
        {"has_data": True, "n_calls": 3, "peak": 3, "elapsed_lt": 0.8},
        probe_hedge=0)

    print()
    print("=== 汇总 ===")
    print("全部通过 ✓" if ok else "存在失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
