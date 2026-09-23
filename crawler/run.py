"""crawler/run.py - 季度抓取编排（无 LLM，复用 skill 抓取配方）。

流程：
  1) init_db + 拉汇率快照
  2) 对每个品牌×国别：读 skill KB 配方 -> 导航 -> 跑 executor.run_query
  3) OPPO(api_json) 经 API 自动发现全部机型实现"全量"；其余品牌用种子机型
  4) 逐机型落库(断点续跑：本季已抓的机型跳过)
  5) 支持 --brand/--country 单次调试

运行：python -m crawler.run            # 全量(分批)
      python -m crawler.run --brand oppo --country my
"""
import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根，便于 import db
from crawler.core import launch_browser, open_page, get_proxy  # noqa: E402
from crawler.model_links import backfill_links, save_report  # noqa: E402
from crawler.reference_prices import (apply_cn_reference, format_stats,  # noqa: E402
                                      CN_REFERENCE_REGIONS)
import executor  # 来自 skill scripts（core 已注入 sys.path）  # noqa: E402
from normalize import parse_amount as _parse_amount  # skill scripts，金额解析唯一实现  # noqa: E402
from normalize import parse_json_amount as _parse_json_amount  # 接口 JSON 专用  # noqa: E402
from db import (init_db, upsert_brand, upsert_country, upsert_model,  # noqa: E402
                upsert_part, insert_snapshot, this_quarter, get_rate, get_rate_meta, fetch_rates,
                model_already_captured, captured_model_keys, log_run, add_issue,
                normalize_base_model, extract_spec, extract_color, classify_tier,
                get_conn,
                STATUS_SUCCESS, STATUS_PARTIAL, STATUS_FAILED,
                STATUS_RESUMED, STATUS_UNAVAILABLE, STATUS_SKIPPED,
                summarize_status,
                MODEL_URL_KIND_API, MODEL_URL_KIND_PAGE, MODEL_URL_KIND_BRAND_ENTRY)

# 抓取范围（全量设计）：models=None 表示尽量自动发现全部机型。
# 华为国内外友商 5 家全覆盖（apple / oppo / samsung / vivo / xiaomi）。
# google 已于 2026-09-22 移出 SCOPE：本环境无 Google Web 出口代理，其 7 国恒为 blocked，
# 只会产出 rows_written=0 的 skipped 记录与「需真机/代理」噪音工单，且 DB 中无任何价格数据。
# 如需恢复：把下方 google 块取消注释，并确保本机挂代理后跑
#   python -m crawler.run --brand google --country <cc>
# KB（references/kb/google.json）已保留，7 国配方可直接复用，无需重建。
SCOPE = {
    # OPPO 已新增 cn（中国）：官网 2026 起备件价走新一代 REBORN 接口
    # （POST /basic/v1/getProduct + /basic/v1/getPartPriceNew，按 marketingModelCode 查询），
    # 旧 sgp /cnw/v1/GetPartPrice(productModel=) 端点机型覆盖不全（cn 的 Pad 5 等平板查无数据）。
    "oppo":   {"countries": ["cn", "de", "tr", "mx", "my", "jp", "ae"], "models": None,
               "country_names": {"cn": "中国", "de": "德国", "tr": "土耳其", "mx": "墨西哥",
                                 "my": "马来西亚", "jp": "日本", "ae": "阿联酋"}},
    # vivo/cn（中国）：vivo.com.cn 独立域名，维修价工具在 /service/accessory。
    # ✅ 2026-09-22 修正：中国站确实提供逐机型备件价（选型号→版本→颜色即见）。
    # 机制：product/list 取机型 → skuInfo 取 SKU(skuCode) → query/v2 {productId, skuCode}
    # 取价；skuCode 必填，空则退回全 SKU 默认目录（原误判"品牌级"的根因）。KB 已转 verified。
    # executor 落地于 skill：vivo_cn_support_page / vivo_cn_price_rows（带 skuCode 取价）。
    "vivo":   {"countries": ["cn", "my", "tr", "ae", "de", "jp", "mx"], "models": None,
               "country_names": {"cn": "中国", "my": "马来西亚", "tr": "土耳其", "ae": "阿联酋",
                                 "de": "德国", "jp": "日本", "mx": "墨西哥"}},
    "xiaomi": {"countries": ["cn", "de", "tr", "my", "jp", "ae", "mx"], "models": None,
               "country_names": {"cn": "中国", "de": "德国", "tr": "土耳其", "my": "马来西亚",
                                 "jp": "日本", "ae": "阿联酋", "mx": "墨西哥"}},
    "apple":  {"countries": ["cn", "de", "jp", "ae", "my", "tr", "mx"], "models": None,
               "country_names": {"cn": "中国", "de": "德国", "jp": "日本", "ae": "阿联酋", "my": "马来西亚",
                                 "tr": "土耳其", "mx": "墨西哥"}},
    "samsung":{"countries": ["de", "tr", "my", "jp", "ae", "cn", "mx"], "models": None,
               "country_names": {"de": "德国", "tr": "土耳其", "my": "马来西亚", "jp": "日本", "ae": "阿联酋", "cn": "中国",
                                 "mx": "墨西哥"}},
    # google 已移出（原因见上方注释）。如需恢复，取消下列 3 行注释并确认代理可用：
    # "google": {"countries": ["de", "jp", "ae", "my", "tr", "cn", "mx"], "models": None,
    #            "country_names": {"de": "德国", "jp": "日本", "ae": "阿联酋", "my": "马来西亚",
    #                              "tr": "土耳其", "cn": "中国", "mx": "墨西哥"}},
}


def _ever_succeeded(brand: str, country: str) -> bool:
    """该 brand/country 是否**曾经**成功抓到过数据（存在 status='success' 的 run_log）。

    用途：区分两类"抓不到"——
      · 从未成功过 → 环境性限制（无出口代理 / 官网无公开备件价工具），不是代码缺陷，
                     不建待修工单，只留 run_log（否则自愈 Agent 每 6h 空转一轮）。
      · 曾经成功过 → 疑似"曾可用→退化"的回归，仍建工单提醒（防回归被静默掩盖）。
    """
    try:
        con = get_conn()
        try:
            row = con.execute(
                "SELECT 1 FROM run_logs WHERE brand=? AND country=? AND status=? LIMIT 1",
                (brand, country, STATUS_SUCCESS)).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        # 查不动就当"曾经成功过"——宁可多建一条工单，也不静默吞掉潜在回归。
        return True

# 各品牌×国家报价是否含税（1=含税/含VAT，0=税前）。本项目覆盖市场均为含税消费电子
# 报价（欧盟 VAT、土耳其/中国/马来/阿联酋/墨西哥/日本均含税），个别净价市场（如美国）
# 未纳入范围。如需精确口径请按官网校准。
TAX_INCLUDED = {(b, c): 1 for b, cfg in SCOPE.items() for c in cfg["countries"]}

# Playwright 收尾超时。实测（2026-09-17）：page.close() 只要 0.01s，但
# browser.close() 与 pw.stop() 在本环境**必然卡满超时**（纯白等，优雅关闭从不成功）；
# 进程随后由 main() 的 os._exit(0) 兜底回收，故不必给足 20s。
# 取 5s：一次收尾从 40s（20s×2）降到 10s，且无 Chromium 子进程泄漏（实测计数 0）。
_CLOSE_TIMEOUT = 5


async def _read_sowapi(page, base_global):
    """轮询页面 window.<base_global> / GCSMAPIPATH（SPA 可能延迟挂载）。"""
    for _ in range(10):
        try:
            base = await page.evaluate(f"() => window.{base_global} || window.GCSMAPIPATH || null")
        except Exception:
            base = None
        if base:
            return base
        await page.wait_for_timeout(800)
    return None


async def _ensure_same_origin(page, base):
    """OPPO sgp API 允许带 credentials 的跨域 fetch（de/ae 实测跨域可取回 2307 行）；

    而源站根页 https://sgp-sow-cms.oppo.com/ 的导航常超时(20s)并会销毁执行上下文，
    导致后续 page.evaluate 崩溃(Execution context was destroyed)。故不再主动同源导航，
    直接跨域 fetch 即可（同品牌同 sgp 节点，行为与已验证的 de/ae 一致）。
    """
    return


async def discover_and_price_via_api(page, rec, country):
    """OPPO api_json：解析 API 基址 -> 机型列表 -> 逐机型取价（全量高效）。

    基址解析优先级：①当前消费者页 window.<base_global> ②导航到 origin_url 源站再读
    ③KB 直给 base_url。取到基址后确保同源，再 fetch。任一环节失败均不抛崩，返回 [] 由
    调用方标记 failed。
    """
    api = rec.get("query", {}).get("api", {})
    base_global = api.get("base_global", "SOWAPIPATH")
    area = api.get("area") or "tr"
    language = api.get("language") or area
    # ① 当前页（应为 entry.url 消费者支持页）读全局基址
    base = await _read_sowapi(page, base_global)
    # 沙箱防护：部分区域(如 de) window 暴露的是 par 等区域节点，从沙箱不可达
    # (fetch 超时 AbortError)；此类非 sgp 节点直接丢弃，回退到 KB 写死的 base_url
    # (sgp-sow-cms，沙箱可达且通吃全部区域，area 参数切换)。
    if base and "sgp-sow-cms" not in base and api.get("base_url"):
        print(f"  [info] window 基址 {base} 非 sgp 可达节点，改用 KB base_url", flush=True)
        base = None
    # ② 未取到则导航到源站（sgp-sow-cms）再读
    origin = api.get("origin_url")
    if not base and origin:
        try:
            await page.goto(origin, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            base = await _read_sowapi(page, base_global)
            if base and "sgp-sow-cms" not in base and api.get("base_url"):
                base = None
        except Exception as e:
            print(f"  [warn] 导航源站失败 {origin}: {e}", flush=True)
    # ③ 兜底 base_url（sgp 节点，沙箱可达）
    if not base:
        base = api.get("base_url")
    if not base:
        print("  [warn] 未取到 API 基址（无 base_url 且无 window 全局），跳过", flush=True)
        return []
    await _ensure_same_origin(page, base)
    list_url = base + api["product_list"].format(area=area, language=language)
    products = await executor._page_fetch(page, list_url)
    if isinstance(products, dict) and products.get("error"):
        print(f"  [warn] 机型列表请求失败: {products['error']}", flush=True)
        return []
    plist = products if isinstance(products, list) else (products or {}).get("list", [])
    if not plist:
        print("  [warn] 机型列表为空（端点/参数可能变更或被限流）", flush=True)
        return []
    ex = rec.get("query", {}).get("extract", {}).get("api", {})
    part_f = ex.get("part_field", "partName")
    price_f = ex.get("price_field", "partPrice")
    type_f = ex.get("type_field")
    out = []
    for x in plist:
        nm = x.get("productModel") or x.get("name")
        if not nm:
            continue
        price_url = base + api["price_detail"].format(area=area, language=language, model=nm)
        data = await executor._page_fetch(page, price_url)
        if isinstance(data, dict) and data.get("error"):
            print(f"  [warn] {nm} 取价失败: {data['error']}", flush=True)
            continue
        parts = data if isinstance(data, list) else (data or {}).get("list", (data or {}).get("partList", []))
        rows = []
        for p in parts:
            pf = p.get(price_f)
            rows.append({
                "cells": [nm, (f"{p.get(type_f)} / " if type_f else "") + (p.get(part_f) or ""), str(pf)],
                # 来自接口 JSON，用 JSON 语义解析器（`.` 恒为小数点）——该分支虽已停用
                # （遗留 GetPartPrice），但避免将来重启用时重蹈"3 位小数被当千分位"的覆辙。
                "price": _parse_json_amount(pf) if pf is not None else None,
                "model": nm, "part": p.get(part_f),
            })
            out.append((nm, rows, price_url))
    return out


# 候选节点对冲试打的默认参数（均可用 KB query.api.probe_* 覆盖）
# hedge 取 3.0s 是实测定的：海外命中节点(par-sow-cms) 稳定 1.20~1.37s/次（实测 6 轮，
# 冷热一致），1.5s 会在**正常情况**下误触发对冲、每次白发 2 个请求；3.0s ≈ 2×典型耗时，
# 既不误触发，又能在黑洞节点上把回退代价从最坏 ~84s 压到 3s。
_PROBE_HEDGE_S = 3.0   # 首发节点多久没回来就叠加下一个
# 第 0 个候选（KB 里的**区域专属正主**）单独用更长的宽限期。
# 为什么必须区分（2026-09-23 实测）：OPPO 各区域的数据按 CDN 节点分布 ——
#   par-sow-cms（欧洲专属）返回 de 目录 192 台，doc 取价正常；
#   sow/sgp-sow-cms（全球节点）返回 197 台，但**对 de 取价恒返空**（0/197 台）。
# 而 par 冷启耗时 4.12s > hedge 3s，会被 0.22s 的 sow 抢先胜出 ——
# 结果是"机型列表拿到了、整轮 0 台有价"，且日志看起来一切正常（最危险的静默失效）。
# 正主本来就不该和兜底节点用同一个宽限期。
_PROBE_FIRST_HEDGE_S = 8.0
_PROBE_TRIES = 2       # 探测只重试 2 次：有回退，快速失败优先于单点成功率
_PROBE_TIMEOUT = 10    # 探测单次超时（秒）；取价仍用 executor 默认 4/20


async def _probe_hosts_for_models(hosts, api, cc, page, path_list, path_price):
    """在多个候选 CDN 节点中确定「谁在返回机型列表」——**对冲试打（hedged request）**。

    为什么不能顺序试打：reborn_post 的 http 分支走 _http_post_json(tries=4, to=20)，
    失败之间还 sleep(1.2) 退避，于是一个**黑洞节点**（TCP 通但不回包）最坏要等
    4×20 + 3×1.2 ≈ 84s 才轮到下一个候选；顺序试打撞上两个坏节点 ≈ 3 分钟纯白等。
    （连接被拒是秒级失败，真正要命的是超时型故障——这才是本函数存在的理由。）

    做法（Google hedged request 思路）：
      1) 先只发第 0 个节点；
      2) 若它在 hedge 秒内没回来，**不等它结束**就叠加发第 1 个，以此类推；
      3) 第一个返回有效机型列表的节点胜出，其余任务取消。
    正常情况仍只发 1 个请求、耗时与顺序试打完全一致（不额外增加对端压力）；
    故障时回退代价从「等满 84s」降到 hedge 秒。

    探测调用的 tries/to 另调小（默认 2/10）：探测有候选可回退，快速失败优先；
    真正取价仍保留完整重试（tries=4/to=20），不牺牲数据可靠性。

    返回 (机型列表 或 None, 命中节点 或 None)。
    """
    if not hosts:
        return None, None

    def _num(key, default, cast):
        try:
            return cast(api.get(key) if api.get(key) is not None else default)
        except (TypeError, ValueError):
            return default

    hedge = _num("probe_hedge", _PROBE_HEDGE_S, float)
    first_hedge = _num("probe_first_hedge", _PROBE_FIRST_HEDGE_S, float)
    p_tries = _num("probe_tries", _PROBE_TRIES, int)
    p_to = _num("probe_timeout", _PROBE_TIMEOUT, int)

    async def _ask(h):
        """问一个节点要机型列表 → (节点, 机型列表 或 None, 失败原因 或 None)。"""
        api_h = {**api, "base_url":
                 (h if h.startswith("http") else f"https://{h}").rstrip("/") + "/oppo-api"}
        res = await executor.reborn_post(api_h, cc, page=page,
                                         path_list=path_list, path_price=path_price,
                                         tries=p_tries, to=p_to)
        if isinstance(res, dict) and res.get("error"):
            return h, None, f"机型列表失败: {res['error']}"
        if str(res.get("code")) not in ("1", "200"):
            return h, None, f"code={res.get('code')} msg={res.get('msg')}"
        data = res.get("data") or []
        return (h, data, None) if data else (h, None, "返回 0 机型")

    tasks = []
    idx = 0

    def _spawn():
        nonlocal idx
        tasks.append(asyncio.create_task(_ask(hosts[idx])))
        idx += 1

    winner = None
    try:
        if hedge <= 0:
            while idx < len(hosts):   # hedge=0：不设先发优势，候选同时竞速
                _spawn()
        else:
            _spawn()
        while tasks:
            # 候选已发完就等结果（timeout=None）；还有候选则最多等 hedge 秒。
            # 注意：idx 耗尽时必须用 None，否则 timeout=0 会让本循环空转（忙等）。
            # idx==1 表示"才发了第 0 个（区域正主）"，此时用更长的 first_hedge。
            wait_s = None if idx >= len(hosts) else (first_hedge if idx == 1 else hedge)
            done, _ = await asyncio.wait(
                tasks, timeout=wait_s,
                return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                tasks.remove(t)
                try:
                    h, data, msg = t.result()
                except Exception as e:  # 单个节点的意外异常不该拖垮探测
                    print(f"  [reborn] 候选节点探测异常: {type(e).__name__}: {str(e)[:120]}",
                          flush=True)
                    continue
                if data and winner is None:
                    winner = (h, data)
                elif msg:
                    print(f"  [reborn] {h} {msg}", flush=True)
            if winner:
                break
            if idx < len(hosts):
                if not done:
                    print(f"  [reborn] {hosts[idx - 1]} {wait_s:g}s 内未返回，"
                          f"叠加试打 {hosts[idx]}", flush=True)
                _spawn()
    finally:
        for t in tasks:
            t.cancel()
        if tasks:
            # 收尾被取消的任务：_http_post_json 跑在 to_thread 里，取消只是放弃等待，
            # 其底层线程会自行结束（纯读请求，不写任何文件，无副作用）。
            await asyncio.gather(*tasks, return_exceptions=True)
    if winner:
        return winner[1], winner[0]
    return None, None


_DIRTY_MODEL_RE = re.compile(r"^OPPOX\s*Send\s*To\b", re.I)


def _is_dirty_catalog_entry(it):
    """剔除 REBORN 目录里的脏条目。

    实测 getProductInfo 的目录混入两条无意义记录：'OPPOXSend To Os' / 'OPPOXSend To ES'
    （品类却标为 Mobile phone）。它们不是真实机型，入库后会在比价矩阵显示为幽灵机型。
    """
    return bool(_DIRTY_MODEL_RE.search(it.get("marketingModelName") or ""))


async def discover_and_price_via_reborn(page, rec, country, max_models=None,
                                        skip_models=None, concurrency=None, stats=None):
    """OPPO api_reborn（新一代 REBORN 备件价接口）：全量机型发现 + **并发**逐机型取价。

    与 discover_and_price_via_api 的差别：
      - 只接受 POST（GET 会被网关拒/返回空），故经 executor._page_fetch_post 走浏览器同源 fetch；
      - 机型主键是 marketingModelCode（非型号名），故先 POST 机型表端点拿全量机型，
        再按码逐个 POST getPartPriceNew，避免"按名字查不到"的错配；
        机型表端点由 KB 的 api.product_list 指定 —— 2026-09-23 起主端点指向 getProductInfo
        （**区域产品目录全集**：de 192 台），而非 getProduct（**仅在售精选**：de 10 台）。
        两者字段名一致，故本函数无需区分；切换后 de 的真实可取价机型由 10 增至 24 台。
        **但两者集合互不包含**：getProduct 另有 26 台（非CN）+ 204 台（cn，含 OPPO
        智能电视与一加/realme/真我等集团子品牌）机型是 getProductInfo 没有的，
        故 KB 可配 api.product_lists 指定多个端点，本函数按下文逻辑取并集。
      - 一次返回全品类（手机/平板/音频/穿戴/智能显示/笔记本），平板机型不再漏抓
        （这正是 OPPO Pad 5 在旧端点查无数据、平台抓不全的根因）。
    区域 CDN 节点不同（cn→sow-cms / de→par-sow-cms / 亚太→sgp-sow-cms），
    故按 candidate_hosts **对冲试打**（见 _probe_hosts_for_models），取第一个返回机型数据的节点。
    返回 [(model_name, rows, human_deep_link), ...]；human_deep_link 形如
    https://support.oppo.com/<cc>/spare-parts-price/#/detail?marketingModelCode=<code>
    （点开即该机型价表，已实测可直接渲染）。

    并发（2026-09-17 优化）：
      - 逐台 POST 原为顺序执行（~1.05s/台，275 台约 5 分钟）。现改为 Semaphore 限流
        并发，默认 6（可用 KB 的 api.concurrency 或环境变量 REBORN_CONCURRENCY 覆盖，
        上限 16）。返回顺序与 plist 一致（asyncio.gather 保序），失败单台只跳过不中断。
      - 前置过滤 skip_models：本季已抓到价的机型**不再发请求**（原先断点续跑只跳过写库，
        仍会把 275 台全部重新 POST 一遍）。

    节点探测对冲（2026-09-17 优化）：候选节点由"顺序试打"改"对冲试打"（hedged），
      正常情况仍只发 1 个请求、耗时不变；某节点黑洞时回退代价从最坏 ~84s 降到 1.5s。

    进度回传 stats（可选 dict，供调用方区分"断点续跑全跳过"与"接口真失败"）：
        total（机型表发现的机型总数）/ skipped（因本季已抓而跳过取价的台数）
        / attempted（实际发起取价的台数）/ priced（取到价的台数）
        / errored（请求层失败的台数）/ noprice（请求成功但官方无备件价的台数）
        ——最后两个必须分开：把"官方本就没公布价"当故障，会让每次续跑都误写待修工单。
    """
    api = rec.get("query", {}).get("api", {})
    cc = api.get("region_iso") or country
    hosts = api.get("candidate_hosts") or [api.get("base_url") or ""]
    hosts = [h.strip().replace("https://", "").rstrip("/") for h in hosts if h]
    path_list = api.get("product_list", "/basic/v1/getProduct")
    path_price = api.get("price_detail", "/basic/v1/getPartPriceNew")
    plist, used_host = await _probe_hosts_for_models(
        hosts, api, cc, page, path_list, path_price)
    if not plist:
        print("  [warn] api_reborn: 所有候选节点均未返回机型列表", flush=True)
        return []
    # 端点并集（2026-09-23 修正）：getProductInfo（区域产品目录全集）与
    # getProduct（在售精选）的机型集合**互不包含** —— 实测七个区域都有
    # "仅 getProduct 有"的机型：cn 204 台（OPPO 智能电视 K9、一加/realme/真我
    # ——OPPO 集团子品牌，其在华售后已并入 OPPO）、ae 13 台、mx 9 台
    # （含主力机 Reno13 5G / Reno13 F 5G）、my 2 台、de/jp 各 1 台。
    # 原先只认单端点（2026-09-23 切到 getProductInfo）会让这批机型整季缺席，
    # 且**旧快照不会被刷新**（这正是 labor_fee 修复后 cn 仍残留 1,621 行的原因）。
    # 故凡 KB 配了 product_lists 的区域，逐个补齐并按 marketingModelCode 去重合并。
    extra_paths = [p for p in (api.get("product_lists") or []) if p and p != path_list]
    if extra_paths:
        have = {x.get("marketingModelCode") for x in plist}
        for ep in extra_paths:
            # 附带端点同样走节点对冲：实测不同端点在各 CDN 节点上的可用性不一致
            # （de 的 getProduct 在 sow-cms 返回 0 台、在 par-sow-cms 正常），
            # 直接复用主端点的命中节点会静默漏数据。
            arr, ep_host = await _probe_hosts_for_models(
                hosts, api, cc, page, ep, path_price)
            if not arr:
                print(f"  [warn] 附带端点 {ep}: 所有候选节点均未返回机型（跳过）", flush=True)
                continue
            if isinstance(arr, dict):  # 保险：个别节点可能返回 {productList:[...]}
                arr = arr.get("productList") or arr.get("list") or []
            new = [x for x in arr
                   if x.get("marketingModelCode") and x.get("marketingModelCode") not in have]
            for x in new:
                have.add(x.get("marketingModelCode"))
            plist += new
            print(f"  [reborn] 附带端点 {ep}（{ep_host}）：{len(arr)} 台，"
                  f"补入 {len(new)} 台新机型", flush=True)
    # 品类过滤：兼容两种端点口径 —— getProduct 给 categoryName（本地语言品类名，
    # 各区域字面量不同），getProductInfo 给 productCategoryCode（跨区域稳定，01=手机）。
    cat_filter = api.get("category_filter")
    cat_codes = api.get("category_filter_by_code")
    if cat_filter:
        plist = [x for x in plist
                 if (x.get("categoryName") or x.get("productCategoryName") or "") in cat_filter]
    elif cat_codes:
        plist = [x for x in plist if (x.get("productCategoryCode") or "") in cat_codes]
    # 只保留"有名字 + 有 marketingModelCode"的机型（缺码无法按码取价），
    # 并剔除官方目录里的脏条目（见 _is_dirty_catalog_entry）。
    plist = [x for x in plist
             if x.get("marketingModelName") and x.get("marketingModelCode")
             and not _is_dirty_catalog_entry(x)]
    total = len(plist)
    # 断点续跑前置过滤：本季已抓到价的机型直接不发请求（旧实现只跳过写库，仍全量重发）
    skipped = 0
    if skip_models:
        before = len(plist)
        plist = [x for x in plist if x.get("marketingModelName") not in skip_models]
        skipped = before - len(plist)
    print(f"  [reborn] {used_host} 发现 {total} 个机型（area={cc}）"
          + (f"；本季已抓 {skipped} 台，跳过取价" if skipped else ""), flush=True)
    if max_models:
        plist = plist[:max_models]  # 冒烟测试/调试用，正式抓取不传
    if stats is not None:
        # 必须在下面的早退之前回传：当全部机型都被断点续跑跳过时 plist 为空，
        # 调用方要靠 total/skipped 区分"正常跳过"与"getProduct 真失败"。
        # errored / noprice 用于区分"请求层失败"与"官方本就没公布价"（见 §16.4）。
        stats.update(total=total, skipped=skipped, attempted=len(plist),
                     priced=0, errored=0, noprice=0)
    if not plist:
        return []
    api_h = {**api, "base_url": f"https://{used_host}/oppo-api"}
    # 并发度：KB api.concurrency > 环境变量 REBORN_CONCURRENCY > 默认 6，夹逼到 [1,16]。
    # 取 6 是"提速 5~6 倍"与"不给 OPPO CDN 造成压力/触发限流"的平衡点。
    try:
        conc = int(concurrency or api.get("concurrency")
                   or os.environ.get("REBORN_CONCURRENCY") or 6)
    except (TypeError, ValueError):
        conc = 6
    conc = max(1, min(conc, 16))
    sem = asyncio.Semaphore(conc)
    fin = 0
    t0 = time.perf_counter()

    async def _one(x):
        """取一台机型的备件价；任何失败只丢弃该台，不抛出（不拖垮整轮）。

        两类"没拿到价"必须分开计数（否则调用方分不清「端点坏了」和「官网本就没价」）：
          errored —— 请求层失败（urllib 报错 / 异常），可能是端点变更或限流；
          noprice —— 请求成功但 partPriceList 为空，是官方如实"无备件价"，不是故障。
        """
        nonlocal fin
        nm = x.get("marketingModelName")
        code = x.get("marketingModelCode")
        try:
            async with sem:  # 只在真正发请求期间占坑，解析/打印不占并发额度
                pr = await executor.reborn_post(api_h, cc, code=code, page=page,
                                                path_list=path_list, path_price=path_price)
            if isinstance(pr, dict) and pr.get("error"):
                print(f"    [warn] {nm} 取价失败: {pr['error']}", flush=True)
                if stats is not None:
                    stats["errored"] += 1
                return None
            rows = executor.flatten_reborn_parts(
                ((pr or {}).get("data") or {}).get("partPriceList"))
            if not rows:
                print(f"    {nm}: 官方无备件价（partPriceList 为空，跳过不写库）", flush=True)
                if stats is not None:
                    stats["noprice"] += 1
                return None
            for r in rows:
                r["model"] = nm
            human = (f"https://support.oppo.com/{cc}/spare-parts-price/"
                     f"#/detail?marketingModelCode={code}")
            return (nm, rows, human)
        except Exception as e:  # 兜底：单台异常不影响其余
            print(f"    [warn] {nm} 取价异常: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if stats is not None:
                stats["errored"] += 1
            return None
        finally:
            fin += 1

    results = await asyncio.gather(*(_one(x) for x in plist))
    out = [r for r in results if r]  # gather 保序，故 out 与 plist 顺序一致
    if stats is not None:
        stats["priced"] = len(out)
    el = time.perf_counter() - t0
    print(f"  [reborn] 取价完成 {len(out)}/{len(plist)} 台有价，并发={conc}，"
          f"耗时 {el:.1f}s（{el / max(1, len(plist)):.2f}s/台，串行基准约 1.05s/台）",
          flush=True)
    return out


async def discover_and_price_via_xiaomi(rec, country, max_models=None,
                                        skip_models=None, concurrency=None, stats=None):
    """小米 api（xiaomi_api）：官方 JSONP 接口一次拿分类树 + **并发**逐型号取价。

    取代原 DOM 点选路径（xiaomi_material_table）。原路径的实测问题（issue #53）：
      1) 943 台逐台"点系列→点型号→读 table"，每次预等待 12s，跑到一半 Page crashed；
      2) 页面崩溃会连带拖垮共享 Chromium，污染同批次其他区域；
      3) 946s~1059s 跑完仍 0 行落库，且人工费列从未成功（has_labor_split 全 0）。
    新路径全程服务端 HTTP，不开浏览器：
      GET  {service_api}/repair_price/shop_class_info?keyword=&callback=cb   → 分类树
      GET  {service_api}/repair_price/shop_band_wx_price?class_id=<id>&callback=cb → 价表
    实测 100 台并发 8 用 5.2s、0 报错；943 台外推约 48s。

    官方字段映射：sale_price=保外物料指导价，handwork_cost=保外人工指导价，
    总价=物料+人工（与跨品牌含人工总价口径一致），人工费可如实拆列取证。

    返回 [(model_name, rows, detail_url), ...]；detail_url 为该机型的官方取价接口
    （逐机型精确到本 SKU，故 source_url_kind=model_api）。

    stats（同 REBORN 版语义，供调用方区分四种"空"）：
        total / skipped / attempted / priced / errored / noprice
    """
    q = rec.get("query", {}) or {}
    api = q.get("api", {}) or {}
    service_api = api.get("service_api")
    cats = q.get("categories")
    try:
        tree = await asyncio.to_thread(executor.xiaomi_class_tree, service_api)
    except Exception as e:
        print(f"  [warn] xiaomi_api: 分类树请求失败 {type(e).__name__}: {str(e)[:120]}", flush=True)
        tree = None
    # series_filter：{L1品类: [L2系列关键字]}，只对配置到的 L1 生效。
    # 例：小米「电脑办公」L1 里平板与笔记本混在一起，配 {"电脑办公":["平板","Pad"]} 只收平板。
    cands = executor.xiaomi_iter_models(tree, cats, q.get("series_filter")) if tree else []
    if not cands:
        print("  [warn] xiaomi_api: 分类树为空（接口变更或网络不可达）", flush=True)
        if stats is not None:
            stats.update(total=0, skipped=0, attempted=0, priced=0, errored=1, noprice=0)
        return []
    total = len(cands)
    skipped = 0
    if skip_models:
        before = len(cands)
        cands = [c for c in cands if c["model"] not in skip_models]
        skipped = before - len(cands)
    print(f"  [xiaomi] 分类树发现 {total} 个型号（品类={cats or '全部'}）"
          + (f"；本季已抓 {skipped} 台，跳过取价" if skipped else ""), flush=True)
    if max_models:
        cands = cands[:max_models]
    if stats is not None:
        stats.update(total=total, skipped=skipped, attempted=len(cands),
                     priced=0, errored=0, noprice=0)
    if not cands:
        return []
    # 并发度：KB api.concurrency > 环境变量 XIAOMI_CONCURRENCY > 默认 8，夹逼到 [1,16]。
    # 取 8 是"实测 0 报错"与"不给小米接口压力"的平衡点（100 台/5.2s）。
    try:
        conc = int(concurrency or api.get("concurrency")
                   or os.environ.get("XIAOMI_CONCURRENCY") or 8)
    except (TypeError, ValueError):
        conc = 8
    conc = max(1, min(conc, 16))
    sem = asyncio.Semaphore(conc)
    fin = 0
    t0 = time.perf_counter()

    async def _one(c):
        """取一台型号的价表；任何失败只丢弃该台，不抛出（不拖垮整轮）。

        errored（请求层失败）与 noprice（官方 code=14「该产品暂无相关数据」）
        必须分开计数，否则每次续跑都会把"官网本就没这台机的价"误写成待修工单。
        """
        nonlocal fin
        nm = c["model"]
        try:
            async with sem:  # 只在发请求期间占坑
                rows, err, status = await asyncio.to_thread(
                    executor.xiaomi_price_rows, c["class_id"], nm, service_api)
            if status == "error":
                print(f"    [warn] {nm} 取价失败: {err}", flush=True)
                if stats is not None:
                    stats["errored"] += 1
                return None
            if status == "noprice" or not rows:
                if stats is not None:
                    stats["noprice"] += 1
                return None
            # 取证链接必须带 &callback=cb：该接口是 JSONP，缺 callback 时官方直接返回
            # {"code":-30005,"msg":"jsonp没有传入callback"} —— 那样的链接点开取不到数据，
            # 不能算"可追溯到本机型"的取证链接（首版漏了参数，实测发现后补上）。
            detail = (f"{(service_api or executor.XIAOMI_SERVICE_API).rstrip('/')}"
                      f"/repair_price/shop_band_wx_price?class_id={c['class_id']}&callback=cb")
            return (nm, rows, detail)
        except Exception as e:  # 兜底：单台异常不影响其余
            print(f"    [warn] {nm} 取价异常: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if stats is not None:
                stats["errored"] += 1
            return None
        finally:
            fin += 1

    results = await asyncio.gather(*(_one(c) for c in cands))
    out = [r for r in results if r]
    if stats is not None:
        stats["priced"] = len(out)
    el = time.perf_counter() - t0
    print(f"  [xiaomi] 取价完成 {len(out)}/{len(cands)} 台有价，并发={conc}，"
          f"耗时 {el:.1f}s（{el / max(1, len(cands)):.2f}s/台；原 DOM 点选约 1.1s/台且中途崩溃）",
          flush=True)
    return out


async def discover_and_price_via_vivo(rec, country, max_models=None,
                                      skip_models=None, concurrency=None, stats=None):
    """vivo api（vivo_api）：官方备件页 SSR 出机型 → **并发**打官方取价接口。

    取代原 DOM 点选路径（vivo_parts_grid）。原路径的实测问题（issue #55 及衍生）：
      1) ae 站在 /ae/en 下，DOM 选择器命中失败 → 0 行（issue #55 的直接症状）；
      2) 更严重的是**跨机型串值**：点选后未等表格刷新就抓，读到上一台机型的行。
         实锤：my 站 X300 Pro 的 Display 落库为 270，而 KB 人工校准（2026-08-27，
         references/calibration/vivo_my_cal.png）与官方接口均为 RM1,030；
         且 DB 里 V40 Lite 与 V50 的 Display 同为 270（两台不同机型不可能同价）。
      3) 结论：DOM 路径产出的价位不可信，价格的唯一真源是官方接口。

    新路径全程服务端 HTTP，不开浏览器：
      GET  https://www.vivo.com/{country}/support/accessory
             → globalVar.regionId（ae='ae/en'，my='my'，tr='tr'）
             → <li class="select-model-item" data-id="3687">型号名</li>
      POST https://www.vivo.com/{region_id}/support/queryPriceByProductId  id={data_id}
             → data.sparePartVO.sparePartsVoList[].name/price
    实测 ae 90 台 / 3 台抽样 4.9s，0 异常。

    返回 [(model_name, rows, detail_url), ...]；detail_url 为该机型官方取价接口的
    **GET 形式**（实测 GET/POST 同源同响应，可点开取证），故 source_url_kind=model_api。

    stats 语义同 REBORN/xiaomi 版：total / skipped / attempted / priced / errored / noprice
    """
    q = rec.get("query", {}) or {}
    api = q.get("api", {}) or {}
    try:
        region_id, items = await asyncio.to_thread(executor.vivo_support_page, country)
    except Exception as e:
        print(f"  [warn] vivo_api: 备件页请求失败 {type(e).__name__}: {str(e)[:120]}", flush=True)
        region_id, items = country, []
    if not items:
        print(f"  [warn] vivo_api: {country} 备件页未解析出机型（SSR 结构变更或无此页）", flush=True)
        if stats is not None:
            stats.update(total=0, skipped=0, attempted=0, priced=0, errored=1, noprice=0)
        return []
    cands = [{"data_id": did, "model": nm} for did, nm in items]
    total = len(cands)
    skipped = 0
    if skip_models:
        before = len(cands)
        cands = [c for c in cands if c["model"] not in skip_models]
        skipped = before - len(cands)
    print(f"  [vivo] {country} 备件页发现 {total} 个型号（regionId={region_id}）"
          + (f"；本季已抓 {skipped} 台，跳过取价" if skipped else ""), flush=True)
    if max_models:
        cands = cands[:max_models]
    if stats is not None:
        stats.update(total=total, skipped=skipped, attempted=len(cands),
                     priced=0, errored=0, noprice=0)
    if not cands:
        return []
    # 并发度：KB api.concurrency > 环境变量 VIVO_CONCURRENCY > 默认 6，夹逼到 [1,16]。
    # 比小米(8)保守：vivo 接口单台价表较大，实测 6 并发已 ~1.6s/台且 0 报错。
    try:
        conc = int(concurrency or api.get("concurrency")
                   or os.environ.get("VIVO_CONCURRENCY") or 6)
    except (TypeError, ValueError):
        conc = 6
    conc = max(1, min(conc, 16))
    sem = asyncio.Semaphore(conc)
    t0 = time.perf_counter()
    if region_id == "cn":
        base = "https://www.vivo.com.cn/service/accessory"
    else:
        base = f"https://www.vivo.com/{region_id}/support"

    async def _one(c):
        """取一台型号的价表；任何失败只丢弃该台，不抛出。errored/noprice 分开计数。"""
        nm = c["model"]
        try:
            async with sem:
                rows, err, status = await asyncio.to_thread(
                    executor.vivo_price_rows, region_id, c["data_id"], nm)
            if status == "error":
                print(f"    [warn] {nm} 取价失败: {err}", flush=True)
                if stats is not None:
                    stats["errored"] += 1
                return None
            if status == "noprice" or not rows:
                if stats is not None:
                    stats["noprice"] += 1
                return None
            # 取证链接：该接口 GET/POST 同响应（实测），GET 形式可直接点开，
            # 逐机型精确到本 SKU（data_id 即官方机型 id）。cn 为中国站专用路径，
            # 优先用 vivo_cn_price_rows 回写的可追溯 source_url（含 skuCode）。
            if region_id == "cn":
                detail = (rows[0].get("source_url")
                          if rows and rows[0].get("source_url") else
                          f"{base}?productId={c['data_id']}")
            else:
                detail = f"{base}/queryPriceByProductId?id={c['data_id']}"
            return (nm, rows, detail)
        except Exception as e:
            print(f"    [warn] {nm} 取价异常: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if stats is not None:
                stats["errored"] += 1
            return None

    results = await asyncio.gather(*(_one(c) for c in cands))
    out = [r for r in results if r]
    if stats is not None:
        stats["priced"] = len(out)
    el = time.perf_counter() - t0
    print(f"  [vivo] 取价完成 {len(out)}/{len(cands)} 台有价，并发={conc}，"
          f"耗时 {el:.1f}s（{el / max(1, len(cands)):.2f}s/台）", flush=True)
    return out


def _parse_row(brand, cells, row):
    """把 executor 返回的 row 拆成价格与人工费取证字段。

    executor 各 query 模式会在 row 上尽量携带 material_fee/labor_fee/has_labor_split/
    labor_note/labor_source_url（如小米官网明确单列人工费，直接取证）；未携带时由
    write_rows 统一按『官网未单列人工费』处理，绝不编造（P1-1）。
    """
    price = row.get("price")
    part = row.get("part") or (cells[1] if len(cells) > 1 else "")
    material = row.get("material_fee")
    labor = row.get("labor_fee")
    return (part, price, material, labor,
            row.get("has_labor_split"), row.get("labor_note"), row.get("labor_source_url"))


def write_rows(brand, country, country_name, model_name, rows, rec, detail_url=None):
    """把一台机型的全部价行写入库。

    **单连接一次提交**（2026-09-17 优化）：原实现对每一行都走
    upsert_part / insert_snapshot，而这两个函数在 conn=None 时会各自
    get_conn() + commit()。一台 11 行的机型 ≈ 25 次"建连接 + fsync 提交"，
    实测落库 ~3s/台；807 台要跑近一小时，在小米改走接口（抓取从 1000s 降到 45s）后
    反而成了全链路瓶颈。现改成整个机型共用一个连接、末尾提交一次：
    25 次 → 1 次 fsync，且写库从"逐行可见"变为"每台机型原子"，中途失败不会留下半台数据。

    db 层的 upsert_* / insert_snapshot 本就支持 conn 参数（传了就不自己 commit/close），
    故这里只是把连接透传下去，语义不变。
    """
    if not rows:
        return 0
    quarter = this_quarter()
    cur = rec.get("currency", "")
    # 汇率查询放在建连接之前：CNY 直接返回静态值，不占连接。
    rate, rate_source, rate_as_of = get_rate_meta(quarter, cur)
    base_model = normalize_base_model(model_name)
    spec = extract_spec(model_name)
    color = extract_color(model_name)
    tier = classify_tier(model_name)
    # source_url 优先级：逐机型报价链接(detail_url) > KB 期望跳转页(expect_url) > 入口页(url)
    src = detail_url or rec.get("entry", {}).get("expect_url") or rec.get("entry", {}).get("url", "")
    # source_url_kind：如实标注该链接是否精确到"本机型"。
    #   api_json(OPPO 旧版) 的 detail_url 是逐机型 GetPartPrice 接口 → model_api；
    #   api_reborn(OPPO 新版) 的 detail_url 是 `#/detail?marketingModelCode=<code>` 人读深链
    #   （POST 接口不可直接点开，故以人读页取证）→ model_page；
    #   xiaomi_api(小米) 的 detail_url 是逐机型 shop_band_wx_price?class_id=<id> 接口，
    #   精确到本 SKU → model_api；
    #   vivo_api(vivo) 的 detail_url 是逐机型 queryPriceByProductId?id=<data_id> 接口
    #   （GET 形式实测与 POST 同响应，可点开），精确到本 SKU → model_api；
    #   其余模式此处只有品牌级入口 → 先记 brand_entry，抓取结束后由
    #   crawler.model_links.backfill_links 生成并实测机型级链接再覆盖。
    mode = rec.get("query", {}).get("mode")
    # 链接类型绑定 db 单一来源（勿写回字面量：与 VALID_SNAPSHOT_URL_KINDS 漂移会让
    # 前端"是否精确到本机型"的标注失真，且写入口断言会直接拦下）
    src_kind = (MODEL_URL_KIND_API
                if detail_url and mode in ("api_json", "xiaomi_api", "vivo_api")
                else (MODEL_URL_KIND_PAGE
                      if detail_url and mode == "api_reborn"
                      else MODEL_URL_KIND_BRAND_ENTRY))
    _model_url = detail_url if src_kind in (MODEL_URL_KIND_API,
                                           MODEL_URL_KIND_PAGE) else None
    tax_included = TAX_INCLUDED.get((brand, country), 1)
    conn = get_conn()
    try:
        bid = upsert_brand(brand, mode, conn=conn)
        upsert_country(country, country_name, cur, rec.get("locale", ""), conn=conn)
        mid = upsert_model(bid, country, model_name, model_name, src,
                           tier=tier, base_model=base_model, spec=spec, color=color,
                           model_url=_model_url,
                           model_url_kind=(src_kind if _model_url else None),
                           model_page_url=(detail_url if mode == "api_reborn" else None),
                           conn=conn)
        n = 0
        for r in rows:
            cells = r.get("cells", [])
            part, price, material, labor, has_split, labor_note, labor_src = _parse_row(brand, cells, r)
            if price is None:
                continue
            # 人工费取证：executor 已取证的(小米)直接用；否则统一标注"官网未单列人工费"，绝不编造
            if has_split is None:
                has_split = 0
                labor_note = labor_note or "官网未单列人工费，仅提供含人工的总维修价（来源见取证链接）"
            labor_src = labor_src or src
            pid = upsert_part(mid, part or "", None, conn=conn)
            cny = price * rate if rate else None
            insert_snapshot(pid, quarter, price, cur, cny, material, labor, src,
                            tax_included=tax_included,
                            labor_note=labor_note, labor_source_url=labor_src,
                            has_labor_split=has_split, is_seed=0,
                            rate_source=rate_source, rate_as_of=rate_as_of,
                            source_url_kind=src_kind, conn=conn)
            n += 1
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def detect_antibot(page):
    """粗略识别反爬/拦截页，返回原因字符串或 None。"""
    try:
        txt = (await page.evaluate("() => document.body ? document.body.innerText || '' : ''"))[:2000].lower()
    except Exception:
        return None
    for kw in ["access denied", "just a moment", "robot", "验证码",
               "verify you are human", "cloudflare", "checking your browser"]:
        if kw in txt:
            return f'疑似反爬/拦截页(命中"{kw}")'
    return None


async def discover_vivo_models(page, rec):
    """vivo_parts_grid：下拉自动发现全部机型（返回选项文本列表）。"""
    q = rec.get("query", {})
    await page.wait_for_timeout(int(q.get("pre_wait_ms", 15000)))
    trig = page.locator(q.get("model_trigger", "#boxSelectModel, .box-select-model")).first
    if not await trig.count():
        return []
    try:
        await trig.evaluate("el => el.click()")
    except Exception:
        pass
    opt_sel = q.get("model_option", "li.select-model-item")
    opts = page.locator(opt_sel)
    for _ in range(12):
        if await opts.count() > 0:
            break
        await page.wait_for_timeout(500)
    names = await opts.all_inner_texts()
    return [n.strip() for n in names if n.strip()]


# ---------- 通用下拉/级联机型发现器（全量机型自动发现） ----------
# 这些发现器读取页面上"型号"相关下拉/列表的全部选项文本，文本会被
# executor.run_query 直接用于选型号，因此自动发现出的机型在取价环节即生效。
# 选择器优先取自 KB 的 cascade[].sel；缺失时用通用启发式。本机首次运行
# 若页面改版导致选择器失效，自愈 Agent（WorkBuddy automation）会介入校准。

async def _read_dropdown_options(page, sel):
    """读取一个下拉（原生 <select> 或自定义下拉）的全部选项文本。"""
    el = page.locator(sel).first
    if await el.count() == 0:
        return []
    try:
        is_select = await el.evaluate("e => e.tagName === 'SELECT'")
    except Exception:
        is_select = False
    if is_select:
        try:
            return [t.strip() for t in await el.locator("option").all_inner_texts() if t.strip()]
        except Exception:
            return []
    # 自定义下拉：点开读取子项
    try:
        await el.click()
        await page.wait_for_timeout(600)
    except Exception:
        pass
    opts = await page.locator(sel + " option, " + sel + " li, " + sel + " [role=option]").all_inner_texts()
    return [t.strip() for t in opts if t.strip()]


async def _apply_dropdown(page, sel, text):
    """在下拉里选择指定文本项（原生 select 或自定义下拉）。"""
    el = page.locator(sel).first
    if await el.count() == 0:
        return
    try:
        is_select = await el.evaluate("e => e.tagName === 'SELECT'")
    except Exception:
        is_select = False
    try:
        if is_select:
            await el.select_option(label=text, timeout=5000)
        else:
            await el.click()
            await page.wait_for_timeout(400)
            await page.locator(sel + " li, " + sel + " [role=option]", has_text=text).first.click(timeout=5000)
    except Exception as e:
        print(f"    [warn] 选择下拉项失败 {sel}={text}: {e}", flush=True)
    await page.wait_for_timeout(500)


async def discover_cascade_models(page, rec):
    """form_select_cascade：逐级选前置级 -> 到 model 级读取全部选项（全量型号）。

    适用：三星 de/my（device_type→model）、苹果 de/jp/ae/my（device→model）等。
    """
    cascade = rec.get("query", {}).get("cascade", [])
    if not cascade:
        return []
    model_idx = next((i for i, f in enumerate(cascade) if f.get("field") == "model"), len(cascade) - 1)
    collected, seen = [], set()

    def _filter(level, opts):
        f = cascade[level]
        ex = f.get("example")
        # ⚠️ example 的**首词**就是前缀过滤键：多品类区域（Apple 同区域有
        # iphone/ipad/watch 三条记录）必须逐级改写 example，否则沿用 iPhone 的
        # 会把 iPad/Watch 型号全滤成 0 台（实测 de/ipad 首跑即 0 机型）。
        if ex:
            prefix = ex.split()[0].lower()
            opts = [o for o in opts if o.lower().startswith(prefix)]
        # 排除项（大小写不敏感的子串）：用于剔掉同一品类页里的非维修条目，
        # 如 iPad 页的「iPad-Zubehör / iPad Accessories / iPad アクセサリ」是配件
        # 不是整机，无「预估服务费」表，收进来只会得到无价行的垃圾机型。
        for bad in (f.get("exclude") or []):
            opts = [o for o in opts if bad.lower() not in o.lower()]
        if f.get("field") in ("device_type", "category"):
            # 旧口径残留：本平台曾只抓手机。现为「官方价表里的品类都收」，
            # 此处仅保留显式配置了 exclude 的过滤，不再硬编码只留手机。
            pass
        return [o for o in opts if o]

    async def walk(level):
        if level > model_idx:
            return
        f = cascade[level]
        sel = f.get("sel") or (".model-dropdown" if f.get("field") == "model" else ".device-dropdown")
        opts = _filter(level, await _read_dropdown_options(page, sel))
        if level == model_idx:
            for o in opts:
                if o not in seen:
                    seen.add(o)
                    collected.append(o)
            return
        for o in opts:  # 遍历前置级，收集 model 级全部型号（级联需逐级展开）
            await _apply_dropdown(page, sel, o)
            await walk(level + 1)

    try:
        await walk(0)
    except Exception as e:
        print(f"    [warn] 级联发现中断：{e}", flush=True)
    return collected


async def discover_table_models(page, rec):
    """spare_parts_table：读取页面上型号 <select> 的全部选项（三星 tr/jp/ae）。"""
    sels = await page.locator("select").all()
    collected, seen = [], set()
    skip = {"请选择", "全部", "Select", "All", ""}
    for s in sels:
        try:
            for t in await s.locator("option").all_inner_texts():
                t = t.strip()
                if t and t not in seen and t not in skip:
                    seen.add(t)
                    collected.append(t)
        except Exception:
            continue
    return collected


async def discover_samsung_models(page, rec):
    """samsung_repair_table：从维修费用表首列(友好名)收集全部机型（需该行含至少一列价格）。

    三星 tr/jp/ae 维修页直接给出含全部机型的静态费用表，无型号下拉；价表可能在
    手风琴(accordion)里（折叠时 innerText 为空），故用 executor._read_all_tables_text
    以 textContent 读取。机型名用 executor._samsung_model_of 取友好名（tr 双列取名称），
    并过滤平板/手表等非手机。
    """
    grids = await executor._read_all_tables_text(page)
    phone_re = re.compile(r"\btab\b|\bwatch\b|\bbuds\b|\bbook\b|\bfit\b|\btv\b", re.I)
    collected, seen = [], set()
    for grid in grids:
        if not grid or len(grid) < 2:
            continue
        for r in grid[1:]:
            if not r or len(r) <= 1:
                continue
            m = executor._samsung_model_of(r)
            if not m or m in seen:
                continue
            if phone_re.search(m):
                continue
            if not any(executor.extract_price(c) for c in r[1:]):
                continue
            seen.add(m)
            collected.append(m)
    return collected


async def discover_xiaomi_models(page, rec):
    """xiaomi_material_table：读取价表页渲染的全部机型（每个机型一个 div.type-search-goods 叶子）。

    小米 /service/materialprice 直接渲染全部机型列表，无需先点系列。旧选择器
    `li, .model-item, [class*=model]` + has_text="GB" 会误抓导航/促销卡片
    （如 "RGB-Mini LED" 含 "GB"），导致抽取 0 行。
    """
    q = rec.get("query", {})
    await page.wait_for_timeout(int(q.get("pre_wait_ms", 12000)))
    collected, seen = [], set()
    try:
        leaves = page.locator("div.type-search-goods")
        for txt in await leaves.all_inner_texts():
            t = txt.strip()
            if t and t not in seen:
                seen.add(t)
                collected.append(t)
    except Exception as e:
        print(f"    [warn] 小米机型发现失败: {e}", flush=True)
    return collected


async def discover_dropdown_models(page, rec):
    """google_estimator：单级型号下拉，读取全部选项（Google 需本机代理可达）。"""
    cascade = rec.get("query", {}).get("cascade", [])
    sel = cascade[0].get("sel") if cascade else ".device-dropdown"
    return await _read_dropdown_options(page, sel)


async def discover_models(page, rec):
    """按 KB 模式分发到对应发现器；返回型号文本列表（空=未就绪，调用方跳过）。"""
    mode = rec.get("query", {}).get("mode")
    if mode == "vivo_parts_grid":
        return await discover_vivo_models(page, rec)
    if mode == "form_select_cascade":
        return await discover_cascade_models(page, rec)
    if mode == "spare_parts_table":
        return await discover_table_models(page, rec)
    if mode == "samsung_repair_table":
        return await discover_samsung_models(page, rec)
    if mode == "xiaomi_material_table":
        return await discover_xiaomi_models(page, rec)
    if mode == "google_estimator":
        return await discover_dropdown_models(page, rec)
    return []


async def crawl_brand_country(browser, brand, country, country_name, models_seed, quarter,
                              isolate=False, force=False, rec_in=None,
                              write_log=True):
    """抓取单个 品牌×国家。

    isolate=True：需要浏览器的区域**各起独立浏览器**，不与同批次其他区域共用。
    由 run_all 在区域并发 >1 时传入（见下方开头的说明：共享 Chromium 会被
    单个区域的页面崩溃连带拖垮）。

    force=True：**忽略本季断点续跑跳过**，对全部机型重新取价。
    用于「本季已落库但数据已知有误/陈旧」的区域（如 xiaomi/cn 在 2026-09-03 抓到
    但 09-17 两次刷新都崩在半路、行仍是旧的）。默认 False 时保持原语义：
    本季抓到过的机型不发请求，避免季度内做无谓重复抓取。
    """
    started = datetime.now().isoformat(timespec="seconds")
    rows_total = 0
    status = STATUS_SUCCESS
    err = None
    anomaly = 0
    reason = ""
    # 早退路径（无 KB / blocked / 浏览器起不来 / 页面打不开 / 自动发现 0 机型）会自行写
    # run_log，而下面的 finally 原本**无条件**再写一条 —— 同一区域同一秒出现两条日志，
    # 且后写的 finally 那条会把真实状态**覆盖成 success**（anomaly/reason 全丢），
    # 监控层据此误判"该区域健康"。故用 logged 标记去重（实测见 §16.9）。
    logged = False
    # 多品类区域由 crawl_brand_country_all 逐条调用，此时本函数**不写 run_log**
    # （write_log=False），只返回结果由外层汇总成一条——否则同一区域同一秒出现
    # 多条日志，且后写的那条会把真实状态覆盖成 success（见 §16.9）。
    def _log_run(*a, **k):
        if write_log:
            log_run(*a, **k)
    # rec_in：多品类区域（KB 同 country 有多条 device_category 记录）由外层逐条传入。
    rec = rec_in if rec_in is not None else executor.load_record(brand, country)
    if not rec:
        print(f"[skip] {brand}/{country} 无 KB 记录", flush=True)
        _log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                STATUS_SKIPPED, 0, "无 KB 记录")
        logged = True
        # P2 异常可视化：无 KB / blocked / 0 机型等「抓不到」一律写入待修队列（报错，不静默）
        add_issue(brand, country, "无 KB 抓取配置（未收录该品牌/国家）")
        return (STATUS_SKIPPED, 0, "无 KB 抓取配置（未收录该品牌/国家）")
    # 注意：此处的 "blocked"/"unavailable" 是 **KB 配方的 status**，与 db.STATUS_* 是两套
    # 不同词汇表（只是取值恰好同名），勿改为常量引用——否则会把两个语义耦合在一起。
    if rec.get("status") in ("blocked", "unavailable"):
        st = rec.get("status")
        print(f"[skip] {brand}/{country} 状态={st}（需真机/代理或官网无工具）", flush=True)
        # 2026-09-23 状态语义拆分：本类是**人工研判过的环境结论**（官网无备件价工具 /
        # 需真机代理），与「断点续跑：本季已抓」是两回事，原先都记 skipped，事后无法
        # 区分「正常续跑」与「该区域根本没数据」。现分别记为 unavailable / resumed。
        _log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                STATUS_UNAVAILABLE, 0, st)
        logged = True
        # 2026-09-22：环境性限制不再建工单。KB 标 blocked/unavailable 是**人工研判过的环境结论**
        # （本机无出口代理 / 官网无公开备件价工具），不是抓取代码的缺陷——每 6h 触发的自愈 Agent
        # 对它无能为力，只会反复"诊断→修不了→保留 open"，纯空转。
        # 用 _ever_succeeded 兜住回归：从未成功过 = 环境限制，静默跳过；
        # 曾经成功过 = 疑似"曾可用→退化"，仍建单，避免回归被这次改动掩盖。
        if _ever_succeeded(brand, country):
            add_issue(brand, country,
                      f"KB 状态={st}，但该区域曾有成功记录 → 疑似回归（曾可用→退化），需排查")
        return (STATUS_UNAVAILABLE, 0, st)
    # samsung_api：服务端 HTTP 直采（DE=seg.apix.de REST / MY=Azure 估价 API），无需浏览器/Playwright
    if rec.get("query", {}).get("mode") == "samsung_api":
        # samsung_api 是**同步**实现（urllib + time.sleep），必须丢线程池：直接在协程里
        # 调用会占住事件循环，run_all 的区域级并发会退化成串行——与 executor.reborn_post
        # 里 _http_post_json 必须 to_thread 是同一个坑（见 §15.3）。
        from crawler.samsung_api import crawl_and_write
        # force 必须透传：否则 `--force` 对 samsung 各区域静默失效（续跑判定仍在
        # samsung_api 内部），出现"命令写了 --force、行为却是续跑"的错觉。
        _st, _n, _rs = await asyncio.to_thread(crawl_and_write, brand, country,
                                               country_name, rec, force)
        return (_st, _n, _rs)
    api_cfg = rec.get("query", {}).get("api", {})
    # api_json（OPPO 旧版）优先用消费者支持页 entry.url（更可达且注入 SOWAPIPATH）；
    # 若拿不到同源基址，discover_and_price_via_api 内部会再回退到 origin_url 源站。
    # api_reborn（OPPO 新版 REBORN）同样先停在消费者支持页：POST 走浏览器同源 fetch，
    # 需要一个已加载的 support.oppo.com 上下文，跨域带 credentials 才能被网关接受。
    goto_url = (rec.get("entry", {}).get("url")
                if rec.get("query", {}).get("mode") in ("api_json", "api_reborn") else None)
    # fetch_mode=http：REBORN 由服务端 urllib 直发（executor._http_post_json），
    # 不需要浏览器与同源上下文，代理也在 http 层按 bypass_proxy 处理。
    # 跳过浏览器可避免"直连打开 support.oppo.com 卡 25s"的无谓等待。
    fetch_http = api_cfg.get("fetch_mode") == "http"
    own_pw = own_browser = None
    use_browser = browser
    page = None
    if not fetch_http:
        # 需要浏览器的区域在「区域并发」下**各起一个自己的浏览器**（isolate=True），
        # 不再共用 run_all 的共享实例。原因（实测）：2026-09-17 全量跑中
        # xiaomi/cn 的页面崩溃（Page crashed）**连带把共享 Chromium 的驱动连接弄断**，
        # 紧接着启动的 samsung/jp 立即报 "Connection closed while reading from the driver"
        # 而秒失败——一个区域的事故污染了同批次的其他区域。
        # 隔离后单个区域崩溃只影响自己；Chromium 启动实测仅 ~1.0s，相对区域自身耗时
        # （几十秒到几分钟）可忽略。fetch_mode=http 的区域（OPPO 全 7 区）不开浏览器，
        # 不受影响，仍完全并发。
        own_needed = bool(api_cfg.get("bypass_proxy")) or isolate
        if own_needed:
            try:
                own_pw, own_browser = await launch_browser(
                    force_direct=bool(api_cfg.get("bypass_proxy")))
                use_browser = own_browser
            except Exception as e:
                own_pw = own_browser = None
                use_browser = browser
                if use_browser is None:
                    print(f"  [error] {brand}/{country} 独立浏览器启动失败且无共享实例可回退: {e}",
                          flush=True)
                    _log_run(brand, country, quarter, started,
                            datetime.now().isoformat(timespec="seconds"),
                            STATUS_FAILED, 0, f"浏览器启动失败: {e}")
                    logged = True
                    add_issue(brand, country, f"浏览器启动失败: {e}")
                    return (STATUS_FAILED, 0, f"浏览器启动失败: {e}")
                print(f"  [warn] {brand}/{country} 独立浏览器启动失败，回退共享浏览器: {e}", flush=True)
        try:
            page = await open_page(use_browser, rec, goto_url=goto_url)
        except Exception as e:
            print(f"  [error] {brand}/{country} 打开页面失败: {e}", flush=True)
            _log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                    STATUS_FAILED, 0, f"页面打开失败: {e}")
            logged = True
            add_issue(brand, country, f"页面打开失败（可能网络不可达/被墙）: {e}")
            for _c in ([own_browser.close()] if own_browser else []) + ([own_pw.stop()] if own_pw else []):
                try:
                    await asyncio.wait_for(_c, timeout=_CLOSE_TIMEOUT)
                except Exception:
                    pass
            return (STATUS_FAILED, 0, f"页面打开失败: {e}")
    try:
        mode = rec.get("query", {}).get("mode")
        bid = upsert_brand(brand, mode)
        done = 0
        attempted = 0  # 真正发起抽取（非断点续跑跳过）的机型数
        zero_models = 0  # 发起抽取却 0 行的机型数（部分失败依据）
        if mode in ("api_json", "api_reborn", "xiaomi_api", "vivo_api"):
            # OPPO/小米等：API 自动发现全量机型并批量取价
            api_stats = {}
            if mode == "xiaomi_api":
                # 小米 api（2026-09-17 新增）：官方 JSONP 接口，全程无浏览器。
                # 断点续跑前置过滤同 REBORN：本季已抓到价的型号不再发请求（force 时忽略）。
                skip_models = set() if force else captured_model_keys(bid, country, quarter)
                if skip_models:
                    print(f"  [resume] {brand}/{country} 本季已抓到价 {len(skip_models)} 台，"
                          f"不再重复取价", flush=True)
                discovered = await discover_and_price_via_xiaomi(
                    rec, country, skip_models=skip_models, stats=api_stats)
            elif mode == "vivo_api":
                # vivo api（2026-09-17 新增）：官方 queryPriceByProductId 接口，无浏览器。
                # 断点续跑前置过滤同 REBORN/小米：本季已抓到价的型号不再发请求（force 时忽略）。
                skip_models = set() if force else captured_model_keys(bid, country, quarter)
                if skip_models:
                    print(f"  [resume] {brand}/{country} 本季已抓到价 {len(skip_models)} 台，"
                          f"不再重复取价", flush=True)
                discovered = await discover_and_price_via_vivo(
                    rec, country, skip_models=skip_models, stats=api_stats)
            elif mode == "api_reborn":
                # 断点续跑前置过滤：本季已抓到价的机型**不发请求**。旧实现只靠下面的
                # model_already_captured 跳过写库，但仍会把 275 台全部重新 POST 一遍。
                skip_models = set() if force else captured_model_keys(bid, country, quarter)
                if skip_models:
                    print(f"  [resume] {brand}/{country} 本季已抓到价 {len(skip_models)} 台，"
                          f"不再重复取价", flush=True)
                discovered = await discover_and_price_via_reborn(
                    page, rec, country, skip_models=skip_models, stats=api_stats)
            else:
                discovered = await discover_and_price_via_api(page, rec, country)
            print(f"  [discover] {brand}/{country} API 发现 {len(discovered)} 个机型"
                  + (f"（另有 {api_stats['skipped']} 台本季已抓、跳过取价）"
                     if api_stats.get("skipped") else ""), flush=True)
            if not discovered:
                st = api_stats
                if not st.get("total"):
                    status = STATUS_FAILED; anomaly = 1
                    reason = "API 未返回机型列表：端点/参数可能变更或被限流"
                elif st.get("skipped") == st["total"]:
                    # 全部机型本季已抓 → 断点续跑的**正常跳过**，绝不能当失败写待修队列
                    # （改造前 discover 会返回 10 台再被逐台跳过，故不会走到这里；
                    #  加了前置过滤后 discover 返回空，必须在调用方区分这两种"空"）。
                    # 2026-09-23：独立为 resumed，与"官方无价可抓"（unavailable）区分开。
                    status = STATUS_RESUMED
                    reason = (f"断点续跑：本季 {st['skipped']} 台机型均已抓取，"
                              f"本轮无新增价行")
                elif st.get("errored"):
                    # 请求层确实失败了（urllib 报错/异常）→ 才可能是端点变更或限流
                    status = STATUS_FAILED; anomaly = 1
                    reason = (f"取价请求失败 {st['errored']}/{st.get('attempted', 0)} 台："
                              f"端点/参数可能变更或被限流")
                else:
                    # 请求都成功，只是官方本季没公布这些机型的备件价（OPPO 为
                    # partPriceList 为空；小米为 code=14『该产品暂无相关数据』）。
                    # 这是"没得抓"，不是"抓坏了"—— 若判为 failed 并写待修队列，
                    # 则每次续跑都会误报一条（实测 oppo/cn 首次踩中，见 §16.4）。
                    # 2026-09-23：归入 unavailable（官方无价可抓），不再与"未收录"混记 skipped；
                    # 与 KB 级 unavailable 的差别体现在 reason 文案上（机型级 vs 区域级）。
                    status = STATUS_UNAVAILABLE
                    reason = (f"本轮尝试 {st.get('attempted', 0)} 台机型，"
                              f"官方均未公布备件价（接口正常返回但无价表）")
            for m, rows, detail_url in discovered:
                if not force and model_already_captured(bid, country, m, quarter):
                    continue  # 断点续跑
                attempted += 1
                wrote = write_rows(brand, country, country_name, m, rows, rec, detail_url=detail_url)
                rows_total += wrote
                done += 1
                if wrote == 0:
                    zero_models += 1
                print(f"  [{brand}/{country}] {m}: {wrote} 条价", flush=True)
        else:
            # vivo/xiaomi/apple/samsung/google：先尝试自动发现全量机型（SCOPE models=None 触发）
            models = models_seed or []
            if not models:
                discovered = await discover_models(page, rec)
                if discovered:
                    print(f"  [discover] {brand}/{country} 自动发现 {len(discovered)} 个机型", flush=True)
                    models = discovered
                else:
                    _reason = "自动发现 0 机型（选择器可能需校准 / 页面改版 / 反爬）"
                    print(f"  [warn] {brand}/{country} {_reason}", flush=True)
                    # 2026-09-23 状态语义拆分：本类原先记 status=skipped、anomaly_flag=0，
                    # 但它**会往待修队列写工单**，且正是"静默失联"的最高危信号
                    # （vivo/tr 曾因浏览器并发把 goto 顶过 25s 上限 → 自动发现 0 机型 →
                    #   本季数据整块缺失，而 run_log 看起来只是"跳过"）。
                    # 按 failed + anomaly 如实上报，不再伪装成"正常跳过"。
                    _log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                            STATUS_FAILED, 0, _reason, 1, _reason)
                    logged = True
                    add_issue(brand, country, _reason)
                    await page.close()
                    return (STATUS_FAILED, 0, _reason)
            for m in models:
                if not force and model_already_captured(bid, country, m, quarter):
                    continue
                attempted += 1
                rows = await executor.run_query(page, rec.get("query", {}), m, None, country)
                detail_url = rec.get("entry", {}).get("expect_url") or rec.get("entry", {}).get("url", "")
                wrote = write_rows(brand, country, country_name, m, rows, rec, detail_url=detail_url)
                rows_total += wrote
                done += 1
                if wrote == 0:
                    zero_models += 1
                print(f"  [{brand}/{country}] {m}: {wrote} 条价", flush=True)
                await page.wait_for_timeout(800)  # 限速，礼貌
        print(f"[done] {brand}/{country} 本季新增 {done} 机型 / {rows_total} 条价", flush=True)
        # 异常启发式（诚实上报）：断点续跑全跳过不算异常；其余失败如实标记。
        if status == STATUS_SUCCESS:
            if attempted == 0:
                # 本季机型此前已全部抓取，本轮无新增 —— 不是失败，但也不能误标"成功产出"。
                # 2026-09-23 拆为独立状态 resumed：与 unavailable（官网不提供）语义不同，
                # 混记 skipped 会让覆盖度审计分不清"正常续跑"与"该区域根本没数据"。
                status = STATUS_RESUMED
                reason = "断点续跑：本季机型均已抓取，本轮无新增价行"
            elif rows_total == 0:
                status = STATUS_FAILED
                anomaly = 1
                ab = await detect_antibot(page)
                reason = ab or "尝试抽取 0 行：疑似选择器失效 / 页面改版 / 反爬"
            elif zero_models > 0:
                # 部分机型抽到了价、部分 0 行 —— 部分失败，待修队列会据此建工单
                status = STATUS_PARTIAL
                anomaly = 1
                reason = (f"部分机型抓取失败：{zero_models}/{attempted} 个机型抽取 0 行"
                          f"（其余 {attempted - zero_models} 个成功）；可能部分选择器失效 / SKU 改版")
        # P0 取证链接：每条价格快照的 source_url 必须指向"本机型"的实际数据/页面，
        # 而非品牌级入口页。抓取成功后立即为本品牌/国家全部机型生成机型级链接并
        # 逐条实测校验；校验不通过的如实标 brand_entry（前端显示"非本机型精确链接"），
        # 绝不为凑机型级而拼造 slug（Apple 实测 29/29 软 404，故只用已验证的品类级 API+locator）。
        # 价格抓到但机型级链接一条没生成 / 回填异常 —— 不能静默吞掉，如实标记为 partial 并写待修。
        # api_reborn（OPPO 新版 REBORN）的取证链接已由 write_rows 用 KB deep_link 模板
        # 逐机型生成（https://support.oppo.com/<cc>/spare-parts-price/#/detail?
        # marketingModelCode=<code>，已实测可直接渲染该机型价表），本身就是 model_page
        # 级链接；通用 backfill_links 无对应采集器，会把 model_url/model_page_url 覆盖成
        # 品牌入口、并把 source_url_kind 降级为 brand_entry。故 api_reborn 不参与回填。
        # xiaomi_api 同理：write_rows 已把每台的 model_url 写成该机型专属的官方取价接口
        # （model_api 级），回填只会把它降级。**且小米现在走"无浏览器池"，browser/page 均为
        # None**，回填需要 browser.new_page() → 实测报
        # `AttributeError: 'NoneType' object has no attribute 'new_page'`，
        # 把本该 success 的区域误标成 partial 并写待修工单（2026-09-17 首次跑新路径踩到）。
        # vivo_api 与 xiaomi_api 完全同构（同为 model_api 级 detail_url + 无浏览器池），
        # 一并排除，否则会重演同一个 AttributeError。
        if (status in (STATUS_SUCCESS, STATUS_PARTIAL) and rows_total > 0
                and mode not in ("api_reborn", "xiaomi_api", "vivo_api")):
            try:
                rep = await backfill_links(use_browser or browser, brand, country)
                if rep:
                    save_report(rep)
                    # backfill_links 返回的是 {"<brand>/<country>": {...}} 的逐国家报告，
                    # 计数键为 n_models / n_verified（不是 total / verified）——此前读错键，
                    # 导致"0/N 机型级链接通过校验"的异常永远不触发（静默通过）。
                    sub = rep.get(f"{brand}/{country}") or {}
                    n_total = sub.get("n_models", 0) or 0
                    n_ok = sub.get("n_verified", 0) or 0
                    if n_total and n_ok == 0:
                        status = STATUS_PARTIAL if status == STATUS_SUCCESS else status
                        anomaly = 1
                        r2 = (f"机型级取证链接生成失败：0/{n_total} 通过校验"
                              f"（价格已抓，但无法定位到本机型官方链接）")
                        reason = (reason + "；" if reason else "") + r2
            except Exception as e:
                print(f"  [links][warn] {brand}/{country} 机型级链接回填异常："
                      f"{type(e).__name__}: {str(e)[:160]}", flush=True)
                status = STATUS_PARTIAL if status == STATUS_SUCCESS else status
                anomaly = 1
                r2 = f"机型级取证链接回填异常：{type(e).__name__}: {str(e)[:160]}"
                reason = (reason + "；" if reason else "") + r2
    except Exception as e:
        status = STATUS_FAILED
        anomaly = 1
        err = str(e)[:500]
        reason = f"运行时异常：{str(e)[:200]}"
        print(f"[error] {brand}/{country}: {err}", flush=True)
    finally:
        finished = datetime.now().isoformat(timespec="seconds")
        if not logged:  # 早退路径已写过日志则跳过，避免双写掩盖真实状态（见开头 logged 说明）
            _log_run(brand, country, quarter, started, finished, status, rows_total, err or "", anomaly, reason)
        if anomaly and write_log:
            iid = add_issue(brand, country, reason)
            if iid:
                print(f"  [queue] 已写入待修队列 #{iid}：{reason}", flush=True)
        # 强制关闭页面：Playwright page.close() 偶发卡死，加超时保护，避免子进程不退出。
        # 实测 page.close() 只 0.01s（不卡），但 browser.close() / pw.stop() 在本环境**必然卡满超时**，
        # 故统一用 _CLOSE_TIMEOUT（5s）而不是 20s：区域并发下每个浏览器类区域都自建浏览器，
        # 20s×2 = 40s 白等会被放大到每个区域。详见 §16.5 与 _CLOSE_TIMEOUT 的注释。
        if page is not None:
            try:
                await asyncio.wait_for(page.close(), timeout=_CLOSE_TIMEOUT)
            except Exception:
                pass
        # 收尾本记录专用的浏览器（bypass_proxy / 区域并发隔离）——不关会残留子进程，
        # 导致 crawl 子进程不退出、服务端任务状态卡 running。
        if own_browser is not None:
            try:
                await asyncio.wait_for(own_browser.close(), timeout=_CLOSE_TIMEOUT)
            except Exception:
                pass
        if own_pw is not None:
            try:
                await asyncio.wait_for(own_pw.stop(), timeout=_CLOSE_TIMEOUT)
            except Exception:
                pass

    return (status, rows_total, reason)


async def crawl_brand_country_all(browser, brand, country, country_name, models_seed, quarter,
                                  isolate=False, force=False):
    """抓取单个 品牌×国家的**全部品类**（KB 里该国家的所有记录）。

    KB 的 `countries[country]` 本就是数组、可用 `device_category` 区分品类。
    Apple 把维修价按品类拆成了 /iphone/repair、/ipad/repair、/watch/repair 三个
    **结构完全同构**的页面（device-dropdown / model-dropdown / 价格标题均一致），
    只取第一条就永远只有 iPhone。

    - 记录数 <= 1：直接走 crawl_brand_country，**行为与改造前完全一致**（不额外写日志）。
    - 记录数 > 1：逐条抓取，最后**汇总成一条 run_log**。
      ⚠️ 不这么做的话，同区域同秒会写出多条日志，而 run_log 按 MAX(id) 只取最后一条，
      前面品类的失败会被静默吞掉（与 §16.9 的"finally 覆盖成 success"是同一类坑）。
    """
    recs = executor.load_records(brand, country)
    if len(recs) <= 1:
        return await crawl_brand_country(browser, brand, country, country_name, models_seed,
                                        quarter, isolate=isolate, force=force)
    started = datetime.now().isoformat(timespec="seconds")
    rows_total, statuses, reasons = 0, [], []
    for r in recs:
        cat = r.get("device_category", "phone")
        print(f"  [multi-category] {brand}/{country} 品类={cat}", flush=True)
        st, n, reason = await crawl_brand_country(
            browser, brand, country, country_name, models_seed, quarter,
            isolate=isolate, force=force, rec_in=r, write_log=False)
        rows_total += (n or 0)
        statuses.append(st or STATUS_SUCCESS)
        if reason:
            reasons.append(f"{cat}: {reason}")
    # 汇总优先级（2026-09-23 随状态语义拆分更新）：
    #   failed > partial > success > resumed > unavailable > skipped
    # 即"有坏消息先报坏消息；有好消息就报好消息；都没有才报中性的续跑/不可用"。
    # 混记 resumed+unavailable 时取 resumed（说明该区域确有数据在跑，信息量更大）。
    # 实现已提取到 db.summarize_status（由 db.STATUS_PRIORITY 单源驱动），
    # 避免此处 if/elif 与状态常量集合各写一份而漂移。
    status = summarize_status(statuses)
    reason = "；".join(reasons)
    anomaly = 1 if status in (STATUS_FAILED, STATUS_PARTIAL) else 0
    log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
            status, rows_total, "", anomaly, reason)
    if anomaly:
        iid = add_issue(brand, country, reason)
        if iid:
            print(f"  [queue] 已写入待修队列 #{iid}：{reason}", flush=True)
    return (status, rows_total, reason)


def _job_needs_browser(brand, country):
    """该 品牌×国家 是否需要浏览器（无浏览器=http 直发或 samsung_api 服务端采集）。

    用于把区域并发**分成两个池**：无浏览器区域可放心并发，浏览器区域默认串行
    （见 run_all 的说明：并发会让部分官网导航超时，进而"自动发现 0 机型"）。
    读 KB 失败时保守认为是浏览器区域。
    """
    try:
        rec = executor.load_record(brand, country)
    except Exception:
        return True
    # 同上：KB 配方 status，非 run_logs.status（见 _job_needs_browser 开头注释）。
    if not rec or rec.get("status") in ("blocked", "unavailable"):
        return False
    q = rec.get("query") or {}
    if q.get("mode") == "samsung_api":
        return False
    return (q.get("api") or {}).get("fetch_mode") != "http"


async def run_all(only_brand=None, only_country=None, concurrency=None, force=False):
    """季度全量抓取。

    区域级并发（2026-09-17 优化）：原先对 SCOPE 的 25 个 brand×country **顺序** await，
    OPPO 7 个区域全量串行约 8 分钟（机型级并发上轮已做，但区域之间仍是排队）。

    **分成两个并发池**（这是实测逼出来的，不是拍脑袋）：

      - **无浏览器池**（OPPO 7 区 + samsung_api 3 区）：默认并发 **2**
        （`RUN_ALL_CONCURRENCY` / `--concurrency`，夹逼 [1,8]）。
        OPPO 区域内部还有机型级并发（默认 6），2 区域 → 峰值 12 个在飞请求；
        实测 OPPO CDN 在并发 8 附近饱和（c=8→4.45×、c=12→4.06×），12 属安全带上沿。
      - **浏览器池**（vivo/xiaomi/apple/samsung_repair_table/google）：默认并发 **1（串行）**
        （`RUN_ALL_BROWSER_CONCURRENCY`，夹逼 [1,4]）。

    为什么浏览器区域默认串行——实测证据：并发运行时 vivo/tr 报
    `Page.goto: Timeout 25000ms exceeded` → `自动发现 0 机型`（本季数据缺失），
    而同一区域单独跑能正常发现 56 个机型。并发（多 Chromium + 同一出口代理）
    把部分官网的 `domcontentloaded` 顶过了硬编码的 25s 上限。
    这类失败**不会报错、只会静默 0 机型**，比慢更危险；且浏览器区域本来就是长尾
    （vivo/my 单区域 306s），并发收益有限。要放开就显式设 RUN_ALL_BROWSER_CONCURRENCY>1，
    此时每个浏览器区域会**各起独立 Chromium**（isolate），避免崩溃连坐（见 crawl_brand_country）。
    """
    init_db()
    fetch_rates(this_quarter())
    proxy = get_proxy()
    quarter = this_quarter()
    jobs = [(b, c, _job_needs_browser(b, c)) for b, cfg in SCOPE.items()
            if not (only_brand and b != only_brand)
            for c in cfg["countries"]
            if not (only_country and c != only_country)]
    try:
        rconc = int(concurrency or os.environ.get("RUN_ALL_CONCURRENCY") or 2)
    except (TypeError, ValueError):
        rconc = 2
    rconc = max(1, min(rconc, 8))
    try:
        bconc = int(os.environ.get("RUN_ALL_BROWSER_CONCURRENCY") or 1)
    except (TypeError, ValueError):
        bconc = 1
    bconc = max(1, min(bconc, 4))
    n_br = sum(1 for _, _, nb in jobs if nb)
    print(f"[start] 季度={quarter} 代理={'on' if proxy else 'off'} "
          f"区域={len(jobs)}（浏览器 {n_br}） 并发=无浏览器 {rconc} / 浏览器 {bconc}", flush=True)
    if not jobs:
        print("[finish] 无可抓区域（检查 --brand/--country 过滤）", flush=True)
        return
    # 浏览器实例策略：
    #   bconc>1（浏览器区域也并发）→ **不**预建共享浏览器，每个浏览器区域自建（isolate，
    #     见 crawl_brand_country），避免"单区域页面崩溃连带拖垮同批次其他区域"；
    #   bconc==1（默认，浏览器区域串行）→ 预建一个共享浏览器（与改造前一致，省启动）；
    #   本批没有任何浏览器区域（如 --brand oppo）→ 一个都不建，纯 http 跑完。
    pw = browser = None
    if bconc <= 1 and n_br > 0:
        pw, browser = await launch_browser()
    http_sem = asyncio.Semaphore(rconc)
    browser_sem = asyncio.Semaphore(bconc)

    async def _one(brand, country, needs_browser):
        cfg = SCOPE[brand]
        # 两个池：无浏览器区域之间并发；浏览器区域之间按 bconc 限流。
        # 两者互不阻塞（共享同一个事件循环，http 请求在 to_thread 里跑，不占浏览器槽）。
        sem = browser_sem if needs_browser else http_sem
        async with sem:  # 只在真正抓取期间占坑，排队时不建页面/不发请求
            t0 = time.perf_counter()
            print(f"  [region] ▶ {brand}/{country}"
                  f"{'（浏览器）' if needs_browser else ''}", flush=True)
            try:
                await crawl_brand_country_all(browser, brand, country,
                                              cfg["country_names"].get(country, country),
                                              cfg["models"], quarter,
                                              isolate=(needs_browser and bconc > 1),
                                              force=force)
            finally:
                print(f"  [region] ■ {brand}/{country} "
                      f"耗时 {time.perf_counter() - t0:.1f}s", flush=True)

    try:
        # return_exceptions=True：单个区域的意外异常（crawl_brand_country 之外的，
        # 如 load_record 抛错）不得中断整批；异常在此汇总打印。
        results = await asyncio.gather(*(_one(b, c, nb) for b, c, nb in jobs),
                                       return_exceptions=True)
        for (b, c, _nb), r in zip(jobs, results):
            if isinstance(r, BaseException):
                print(f"  [error] {b}/{c} 区域任务异常：{type(r).__name__}: {str(r)[:200]}",
                      flush=True)
    finally:
        # 强制关闭共享浏览器与 Playwright driver（仅串行模式会创建，见上方策略说明）。
        # 二者优雅关闭在本环境**必然卡死**，故加超时保护并忽略异常，确保 run_all 能正常返回
        # （否则 asyncio.run 不结束、进程不退出，服务端任务状态会卡在 running）。
        # ⚠️ 不要图快改成"两者并发 await"：实测会**死锁**（外层 60s 兜底才救回来）——
        #    pw.stop() 会拆掉 browser.close() 正在等待的那条驱动连接，两个 future 都不落地，
        #    连 wait_for 的取消都没法推进。只能顺序收尾。
        if browser is not None and pw is not None:
            for _closer in (browser.close(), pw.stop()):
                try:
                    await asyncio.wait_for(_closer, timeout=_CLOSE_TIMEOUT)
                except Exception:
                    pass
    # CN 参考价回退（_apply_reference_fallback）已**默认停用**（2026-09-23）。
    # 原因：其前提被证伪 —— 原先认为"这些机型是当地在售机型、只是官方没公布价"，
    # 实测那 1,123 台全部是旧端点 /cnw/v1/GetPartPrice 污染的机型行（含 186 台一加机型），
    # 根本不属于这些区域，已由 tools/purge_legacy_oppo_models.py 清退；
    # 机型表也改由 getProductInfo（区域产品目录全集）定源。
    # 若将来确要为"在当地产品目录中、但官方未公布价"的机型补参考价，须先用
    # references/catalog/oppo_<cc>.json 收窄适用面，**切勿**再以"本季无快照"为判据
    # （那会把"当地根本没有的机型"也算进来）。
    print("[finish] 抓取结束，数据已落 spare_parts.db", flush=True)


def _apply_reference_fallback(quarter, only_brand=None, only_country=None):
    """【已停用 · DEPRECATED 2026-09-23】给"本季无价"机型补 CN 官方参考价。

    ⚠️ 不要再直接调用。其判据（"本季无任何快照的机型"）已被证明会命中**污染机型**：
    当时那 1,123 台的成因并非"当地机型官方未定价"，而是旧端点 /cnw/v1/GetPartPrice
    残留的中国市场机型行（含 186 台一加机型），根本不属于这些区域，现已清退。
    保留函数体仅供将来按**目录收窄**后复用（见下方注释），当前 run_all 已不再调用。
    """
    for brand, regions in CN_REFERENCE_REGIONS.items():
        if only_brand and brand != only_brand:
            continue
        ccs = [cc for cc in regions if not (only_country and cc != only_country)]
        if not ccs:
            continue
        try:
            st = apply_cn_reference(quarter, brand=brand, regions=ccs)
            print(format_stats(st), flush=True)
            for cc, d in st["regions"].items():
                if d["models"]:
                    print(f"  [reference] {brand}/{cc}: 补 {d['models']} 台机型 / "
                          f"{d['snapshots']} 条 CN 参考价（is_reference=1，不参与本地价差）",
                          flush=True)
        except Exception as e:
            print(f"  [reference] {brand} 参考价兜底失败（不影响抓取结果）："
                  f"{type(e).__name__}: {str(e)[:150]}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--country")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="区域级并发度（默认 2，也可用环境变量 RUN_ALL_CONCURRENCY）")
    ap.add_argument("--force", action="store_true",
                    help="忽略本季断点续跑跳过，对全部机型重新取价（用于刷新已知陈旧/损坏的本季数据）")
    args = ap.parse_args()
    try:
        asyncio.run(run_all(args.brand, args.country, args.concurrency, force=args.force))
    except Exception as e:
        print(f"[fatal] {type(e).__name__}: {str(e)[:300]}", flush=True)
    finally:
        # 强制退出：os._exit 立即终止进程并回收子进程/Playwright driver，
        # 兜底防止任何优雅关闭残留导致进程不退出（进而服务端 crawl 任务状态一直 running）
        os._exit(0)


if __name__ == "__main__":
    main()
