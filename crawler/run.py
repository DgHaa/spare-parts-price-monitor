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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根，便于 import db
from crawler.core import launch_browser, open_page, get_proxy  # noqa: E402
from crawler.model_links import backfill_links, save_report  # noqa: E402
import executor  # 来自 skill scripts（core 已注入 sys.path）  # noqa: E402
from db import (init_db, upsert_brand, upsert_country, upsert_model,  # noqa: E402
                upsert_part, insert_snapshot, this_quarter, get_rate, get_rate_meta, fetch_rates,
                model_already_captured, log_run, add_issue,
                normalize_base_model, extract_spec, extract_color, classify_tier)

# 抓取范围（全量设计）：models=None 表示尽量自动发现全部机型。
# 华为国内外友商 6 家全覆盖；google 在本环境被墙(blocked)，其余在本机+代理可跑。
SCOPE = {
    "oppo":   {"countries": ["de", "tr", "mx", "my", "jp", "ae"], "models": None,
               "country_names": {"de": "德国", "tr": "土耳其", "mx": "墨西哥", "my": "马来西亚", "jp": "日本", "ae": "阿联酋"}},
    "vivo":   {"countries": ["my", "tr", "ae"], "models": None,
               "country_names": {"my": "马来西亚", "tr": "土耳其", "ae": "阿联酋"}},
    "xiaomi": {"countries": ["cn"], "models": None,
               "country_names": {"cn": "中国"}},
    "apple":  {"countries": ["de", "jp", "ae", "my"], "models": None,
               "country_names": {"de": "德国", "jp": "日本", "ae": "阿联酋", "my": "马来西亚"}},
    "samsung":{"countries": ["de", "tr", "my", "jp", "ae"], "models": None,
               "country_names": {"de": "德国", "tr": "土耳其", "my": "马来西亚", "jp": "日本", "ae": "阿联酋"}},
    "google": {"countries": ["de", "jp", "ae", "my", "tr"], "models": None,
               "country_names": {"de": "德国", "jp": "日本", "ae": "阿联酋", "my": "马来西亚", "tr": "土耳其"}},
}

# 各品牌×国家报价是否含税（1=含税/含VAT，0=税前）。本项目覆盖市场均为含税消费电子
# 报价（欧盟 VAT、土耳其/中国/马来/阿联酋/墨西哥/日本均含税），个别净价市场（如美国）
# 未纳入范围。如需精确口径请按官网校准。
TAX_INCLUDED = {(b, c): 1 for b, cfg in SCOPE.items() for c in cfg["countries"]}


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
                "price": float(str(pf).replace(",", "")) if pf is not None else None,
                "model": nm, "part": p.get(part_f),
            })
        out.append((nm, rows, price_url))
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
    if not rows:
        return 0
    quarter = this_quarter()
    bid = upsert_brand(brand, rec.get("query", {}).get("mode"))
    upsert_country(country, country_name, rec.get("currency", ""), rec.get("locale", ""))
    # 归一化：基础机型(去规格/颜色) + 规格/SKU + 颜色 + 档位，四者一并落库，支撑公平比价
    base_model = normalize_base_model(model_name)
    spec = extract_spec(model_name)
    color = extract_color(model_name)
    tier = classify_tier(model_name)
    # source_url 优先级：逐机型报价链接(detail_url) > KB 期望跳转页(expect_url) > 入口页(url)
    src = detail_url or rec.get("entry", {}).get("expect_url") or rec.get("entry", {}).get("url", "")
    # source_url_kind：如实标注该链接是否精确到"本机型"。api_json(OPPO) 的 detail_url 是
    # 逐机型 GetPartPrice 接口 → model_api；其余模式此处只有品牌级入口 → 先记 brand_entry，
    # 抓取结束后由 crawler.model_links.backfill_links 生成并实测机型级链接再覆盖。
    src_kind = ("model_api" if detail_url and rec.get("query", {}).get("mode") == "api_json"
                else "brand_entry")
    # api_json(OPPO) 的 detail_url 本身就是"本机型"接口 URL，抓取当场即可落 model_url；
    # 其余品牌传 None，由 backfill_links 回填（upsert_model 对 None 用 COALESCE 保留原值）
    mid = upsert_model(bid, country, model_name, model_name, src,
                       tier=tier, base_model=base_model, spec=spec, color=color,
                       model_url=(detail_url if src_kind == "model_api" else None),
                       model_url_kind=("model_api" if src_kind == "model_api" else None))
    cur = rec.get("currency", "")
    rate, rate_source, rate_as_of = get_rate_meta(quarter, cur)
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
        pid = upsert_part(mid, part or "", None)
        cny = price * rate if rate else None
        insert_snapshot(pid, quarter, price, cur, cny, material, labor, src,
                        tax_included=TAX_INCLUDED.get((brand, country), 1),
                        labor_note=labor_note, labor_source_url=labor_src,
                        has_labor_split=has_split, is_seed=0,
                        rate_source=rate_source, rate_as_of=rate_as_of,
                        source_url_kind=src_kind)
        n += 1
    return n


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
        if ex:  # 仅保留 example 前缀（如 "iPhone 16" -> "iPhone"），避免抓到平板/手表
            prefix = ex.split()[0].lower()
            opts = [o for o in opts if o.lower().startswith(prefix)]
        if f.get("field") in ("device_type", "category"):  # 本平台只抓手机备件
            opts = [o for o in opts if ("手机" in o or "phone" in o.lower())]
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


async def crawl_brand_country(browser, brand, country, country_name, models_seed, quarter):
    started = datetime.now().isoformat(timespec="seconds")
    rows_total = 0
    status = "success"
    err = None
    anomaly = 0
    reason = ""
    rec = executor.load_record(brand, country)
    if not rec:
        print(f"[skip] {brand}/{country} 无 KB 记录", flush=True)
        log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                "skipped", 0, "无 KB 记录")
        # P2 异常可视化：无 KB / blocked / 0 机型等「抓不到」一律写入待修队列（报错，不静默）
        add_issue(brand, country, "无 KB 抓取配置（未收录该品牌/国家）")
        return
    if rec.get("status") in ("blocked", "unavailable"):
        st = rec.get("status")
        print(f"[skip] {brand}/{country} 状态={st}（需真机/代理或官网无工具）", flush=True)
        log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                "skipped", 0, st)
        add_issue(brand, country, f"KB 状态={st}：需真机/代理或官网无工具，本环境无法抓取")
        return
    # samsung_api：服务端 HTTP 直采（DE=seg.apix.de REST / MY=Azure 估价 API），无需浏览器/Playwright
    if rec.get("query", {}).get("mode") == "samsung_api":
        from crawler.samsung_api import crawl_and_write
        crawl_and_write(brand, country, country_name, rec)
        return
    api_cfg = rec.get("query", {}).get("api", {})
    # api_json（OPPO）优先用消费者支持页 entry.url（更可达且注入 SOWAPIPATH）；
    # 若拿不到同源基址，discover_and_price_via_api 内部会再回退到 origin_url 源站。
    goto_url = rec.get("entry", {}).get("url") if rec.get("query", {}).get("mode") == "api_json" else None
    try:
        page = await open_page(browser, rec, goto_url=goto_url)
    except Exception as e:
        print(f"  [error] {brand}/{country} 打开页面失败: {e}", flush=True)
        log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                "failed", 0, f"页面打开失败: {e}")
        add_issue(brand, country, f"页面打开失败（可能网络不可达/被墙）: {e}")
        return
    try:
        mode = rec.get("query", {}).get("mode")
        bid = upsert_brand(brand, mode)
        done = 0
        attempted = 0  # 真正发起抽取（非断点续跑跳过）的机型数
        zero_models = 0  # 发起抽取却 0 行的机型数（部分失败依据）
        if mode == "api_json":
            # OPPO 等：API 自动发现全量机型并批量取价
            discovered = await discover_and_price_via_api(page, rec, country)
            print(f"  [discover] {brand}/{country} API 发现 {len(discovered)} 个机型", flush=True)
            if not discovered:
                status = "failed"; anomaly = 1
                reason = "API 未返回机型列表：端点/参数可能变更或被限流"
            for m, rows, detail_url in discovered:
                if model_already_captured(bid, country, m, quarter):
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
                    print(f"  [warn] {brand}/{country} 自动发现 0 机型，跳过（选择器可能需校准）", flush=True)
                    log_run(brand, country, quarter, started, datetime.now().isoformat(timespec="seconds"),
                            "skipped", 0, "自动发现 0 机型（选择器可能需校准）")
                    add_issue(brand, country, "自动发现 0 机型（选择器可能需校准 / 页面改版 / 反爬）")
                    await page.close()
                    return
            for m in models:
                if model_already_captured(bid, country, m, quarter):
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
        if status == "success":
            if attempted == 0:
                # 本季机型此前已全部抓取，本轮无新增 —— 不是失败，但也不能误标"成功产出"
                status = "skipped"
                reason = "断点续跑：本季机型均已抓取，本轮无新增价行"
            elif rows_total == 0:
                status = "failed"
                anomaly = 1
                ab = await detect_antibot(page)
                reason = ab or "尝试抽取 0 行：疑似选择器失效 / 页面改版 / 反爬"
            elif zero_models > 0:
                # 部分机型抽到了价、部分 0 行 —— 部分失败，待修队列会据此建工单
                status = "partial"
                anomaly = 1
                reason = (f"部分机型抓取失败：{zero_models}/{attempted} 个机型抽取 0 行"
                          f"（其余 {attempted - zero_models} 个成功）；可能部分选择器失效 / SKU 改版")
        # P0 取证链接：每条价格快照的 source_url 必须指向"本机型"的实际数据/页面，
        # 而非品牌级入口页。抓取成功后立即为本品牌/国家全部机型生成机型级链接并
        # 逐条实测校验；校验不通过的如实标 brand_entry（前端显示"非本机型精确链接"），
        # 绝不为凑机型级而拼造 slug（Apple 实测 29/29 软 404，故只用已验证的品类级 API+locator）。
        # 价格抓到但机型级链接一条没生成 / 回填异常 —— 不能静默吞掉，如实标记为 partial 并写待修。
        if status in ("success", "partial") and rows_total > 0:
            try:
                rep = await backfill_links(browser, brand, country)
                if rep:
                    save_report(rep)
                    n_total = rep.get("total", 0) or 0
                    n_ok = rep.get("verified", 0) or 0
                    if n_total and n_ok == 0:
                        status = "partial" if status == "success" else status
                        anomaly = 1
                        r2 = (f"机型级取证链接生成失败：0/{n_total} 通过校验"
                              f"（价格已抓，但无法定位到本机型官方链接）")
                        reason = (reason + "；" if reason else "") + r2
            except Exception as e:
                print(f"  [links][warn] {brand}/{country} 机型级链接回填异常："
                      f"{type(e).__name__}: {str(e)[:160]}", flush=True)
                status = "partial" if status == "success" else status
                anomaly = 1
                r2 = f"机型级取证链接回填异常：{type(e).__name__}: {str(e)[:160]}"
                reason = (reason + "；" if reason else "") + r2
    except Exception as e:
        status = "failed"
        anomaly = 1
        err = str(e)[:500]
        reason = f"运行时异常：{str(e)[:200]}"
        print(f"[error] {brand}/{country}: {err}", flush=True)
    finally:
        finished = datetime.now().isoformat(timespec="seconds")
        log_run(brand, country, quarter, started, finished, status, rows_total, err or "", anomaly, reason)
        if anomaly:
            iid = add_issue(brand, country, reason)
            if iid:
                print(f"  [queue] 已写入待修队列 #{iid}：{reason}", flush=True)
        # 强制关闭页面：Playwright page.close() 偶发卡死，加超时保护，避免子进程不退出
        try:
            await asyncio.wait_for(page.close(), timeout=20)
        except Exception:
            pass


async def run_all(only_brand=None, only_country=None):
    init_db()
    fetch_rates(this_quarter())
    proxy = get_proxy()
    print(f"[start] 季度={this_quarter()} 代理={'on' if proxy else 'off'}", flush=True)
    pw, browser = await launch_browser()
    quarter = this_quarter()
    try:
        for brand, cfg in SCOPE.items():
            if only_brand and brand != only_brand:
                continue
            for country in cfg["countries"]:
                if only_country and country != only_country:
                    continue
                await crawl_brand_country(browser, brand, country,
                                          cfg["country_names"].get(country, country),
                                          cfg["models"], quarter)
    finally:
        # 强制关闭浏览器与 Playwright driver：二者优雅关闭偶发卡死，加超时保护并忽略异常，
        # 确保 run_all 能正常返回（否则 asyncio.run 不结束、进程不退出，服务端任务状态卡 running）
        for _closer in (browser.close(), pw.stop()):
            try:
                await asyncio.wait_for(_closer, timeout=20)
            except Exception:
                pass
    print("[finish] 抓取结束，数据已落 spare_parts.db", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--country")
    args = ap.parse_args()
    try:
        asyncio.run(run_all(args.brand, args.country))
    except Exception as e:
        print(f"[fatal] {type(e).__name__}: {str(e)[:300]}", flush=True)
    finally:
        # 强制退出：os._exit 立即终止进程并回收子进程/Playwright driver，
        # 兜底防止任何优雅关闭残留导致进程不退出（进而服务端 crawl 任务状态一直 running）
        os._exit(0)


if __name__ == "__main__":
    main()
