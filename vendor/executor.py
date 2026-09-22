#!/usr/bin/env python3
"""executor.py - 按 KB steps 用 Playwright 执行竞品备件价格抓取并取证。

依赖:
    pip install playwright && playwright install chromium

用法:
    python executor.py --brand oppo --country tr --model "Find X7" --part 电池
    python executor.py --csv queries.csv --out output/report.md

KB 未命中时（返回 status=kb_miss）由调用方走 Explorer 探索流程。
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.stderr.write("ERROR: playwright 未安装。请先运行: pip install playwright && playwright install chromium\n")
    sys.exit(2)

KB_DIR = Path(__file__).resolve().parent.parent / "references" / "kb"
EVIDENCE_DIR = Path("output/evidence")
OUT_DIR = Path("output")
GOOGLE_LOCS = KB_DIR / "google_locators.json"

# 金额解析统一走 normalize.parse_amount：各国 `.`/`,` 含义相反，重复实现过
# `replace(",", "")` 导致德语小数逗号被吃掉、价格放大 100 倍。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize import parse_amount as _parse_amount  # noqa: E402


def load_record(brand, country, category="phone"):
    p = KB_DIR / f"{brand}.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    recs = data.get("countries", {}).get(country, [])
    for r in recs:
        if r.get("device_category", "phone") == category:
            return r
    return recs[0] if recs else None


def load_records(brand, country):
    """取该 品牌×国家 的**全部** KB 记录（数组），按 device_category 区分品类。

    同一个国家可能有多个官方价页（Apple 把维修价按品类分成了
    /iphone/repair、/ipad/repair、/watch/repair 三个**结构完全同构**的页面），
    此时一条记录不够用，需要全品类都抓。无 KB / 无该国家时返回空列表。
    """
    p = KB_DIR / f"{brand}.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data.get("countries", {}).get(country, []) or [])


def loc(page, sel):
    s = (sel or "").strip()
    if s.startswith("text="):
        return page.get_by_text(s[5:], exact=False)
    if s.startswith("css="):
        return page.locator(s[4:])
    return page.locator(s)


async def run_step(page, step, evidence_dir, shots):
    act = step.get("act")
    sel = step.get("sel")
    if act == "goto":
        # commit 比 domcontentloaded 更稳：SPA 常因长轮询/资源卡在 load 事件
        await page.goto(step["url"], wait_until="commit")
        await page.wait_for_timeout(4000)
    elif act == "hover":
        await loc(page, sel).first.hover()
    elif act == "click":
        try:
            await loc(page, sel).first.click(timeout=step.get("timeout", 8000))
        except Exception:
            if step.get("alt"):
                await loc(page, step["alt"]).first.click(timeout=step.get("timeout", 8000))
    elif act == "select":
        await page.locator(step["sel"]).select_option(label=step.get("value"))
    elif act == "type":
        await page.locator(step["sel"]).fill(step.get("text", ""))
    elif act == "wait":
        until = step.get("until", "")
        to = step.get("timeout", 8000)
        if until.startswith("url_contains:"):
            token = until.split(":", 1)[1]
            # 容忍轮询：SPA 常不触发新导航，当前 URL 可能已满足；超时也不致命
            matched = False
            for _ in range(max(1, to // 200)):
                if token in page.url:
                    matched = True
                    break
                await page.wait_for_timeout(200)
            if not matched:
                sys.stderr.write(f"[warn] wait url_contains:{token} 超时，当前 URL={page.url}\n")
        elif until.startswith("selector_visible:"):
            await page.locator(until.split(":", 1)[1]).first.wait_for(state="visible", timeout=to)
        elif until.startswith("text_appears:"):
            await page.get_by_text(until.split(":", 1)[1]).first.wait_for(state="visible", timeout=to)
    elif act == "screenshot":
        path = evidence_dir / step.get("as", f"shot_{len(shots)}.png")
        await page.screenshot(path=str(path), full_page=False)
        shots.append(str(path))
    # extract 步骤在 run_query 中统一处理


async def click_option(page, text):
    for sel in [f"text={text}", f"option:has-text('{text}')",
                f"td:has-text('{text}')", f"button:has-text('{text}')"]:
        try:
            await page.locator(sel).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


async def select_option_robust(page, sel, val):
    """优先用原生 select_option（label/index），失败回退 click_option。

    用于替代裸 click_option：当目标为 <SELECT> 时用 select_option 精确选值，
    避免 click_option 的文本模糊匹配误点其它元素（回归风险点）。仅在 select_option
    不可用（非 SELECT 自定义控件）或失败时回退到文本点击。
    """
    el = page.locator(sel).first
    if await el.count() == 0:
        return False
    try:
        is_select = await el.evaluate("e => e.tagName === 'SELECT'")
    except Exception:
        is_select = False
    if is_select:
        try:
            await el.select_option(label=val.replace("\xa0", " "), timeout=5000)
            return True
        except Exception:
            pass
        try:
            await el.select_option(index=0, timeout=5000)
            return True
        except Exception:
            pass
    # 非 SELECT 或 select_option 失败：回退文本点击
    return await click_option(page, val)


def extract_price(text):
    """从文本抽金额。

    数字→float 一律交给 normalize.parse_amount 处理——各国 `.`/`,` 含义相反，
    曾因无条件删逗号把 Apple 德国站 `€488,99` 读成 48899（放大 100 倍）。
    统一后原先的 TRY 专用分支（`₺8.200`）也被规则自然覆盖，无需特例。
    """
    if not text:
        return None
    # 1) 前缀：货币符号或 2-3 字母币代码（RM / TRY / AED / EUR / MYR ...）
    m = re.search(r"(?:[€¥£$₺]|[A-Z]{2,3})\s*([\d][\d.,]*)", text)
    if not m:
        # 2) 后缀：数字 + 可选空格 + 币代码（14495TRY / 380.00 RM）
        m = re.search(r"([\d][\d.,]*)\s*[A-Z]{2,3}", text)
    if not m:
        # 3) 兜底：首个数字（无币符的纯数字价表，如小米物料价）
        m = re.search(r"([\d][\d.,]*)", text)
    return _parse_amount(m.group(1)) if m else None


async def read_table(page, query):
    tbl = page.locator(query.get("table_locator", "table")).first
    rows = []
    trs = await tbl.locator("tbody tr, tr").all()
    for tr in trs:
        cells = await tr.locator("td").all_inner_texts()
        if not cells:
            continue
        row = {"cells": [c.strip() for c in cells]}
        row["price"] = extract_price(" ".join(cells))
        rows.append(row)
    return rows


async def _read_all_tables_text(page):
    """读取页面全部 <table> 的单元格文本（用 textContent，绕过折叠手风琴 innerText 为空的问题）。

    返回 list[grid]，grid = list[row]，row = list[cell_text]。仅保留 >=2 行的表。
    三星等维修费用页把价表放在手风琴(accordion)组件里，折叠时 innerText 为空，
    textContent 才能取到真实数据。
    """
    return await page.evaluate("""() => {
        const out = [];
        for (const t of document.querySelectorAll('table')) {
            const rows = [...t.querySelectorAll('tr')];
            if (rows.length < 2) continue;
            const grid = rows.map(r => [...r.querySelectorAll('th,td')].map(
                c => (c.textContent || '').replace(/\\s+/g, ' ').trim()));
            out.push(grid);
        }
        return out;
    }""")


# 三星维修费用表头 -> 中文部件名（跨国比价统一口径，与 oppo/vivo/apple 一致）
_PART_MAP = [
    (re.compile(r"screen|ekran|display|ディスプレイ|画面|屏幕", re.I), "屏幕"),
    (re.compile(r"battery|pil|バッテリー|電池|电池", re.I), "电池"),
    (re.compile(r"back\s*cover|arka\s*kapak|バックカバー|背面|后盖|back\s*glass", re.I), "后盖"),
    (re.compile(r"main\s*board|anakart|メイン基板|主板|logic\s*board", re.I), "主板"),
    (re.compile(r"camera|kamera|カメラ|摄像头", re.I), "摄像头"),
    (re.compile(r"charg|şarj|充電|充电", re.I), "充电口"),
    (re.compile(r"speaker|hoparlör|スピーカー|扬声器|听筒", re.I), "扬声器"),
]


def _canon_part(header):
    """把本地化表头（如 'Screen Replacement' / 'バッテリー交換' / 'メイン基板交換（512GB）'）
    归一化为中文部件名；保留括号里的规格变体（如 '主板（512GB）'）以区分同部件多列。"""
    for rx, lab in _PART_MAP:
        if rx.search(header or ""):
            m = re.search(r"[（(].*?[)）]", header or "")
            return lab + (m.group(0) if m else "")
    return (header or "").strip()


def _samsung_model_of(r):
    """取机型展示名：tr 双列（代码+名称）取名称列；jp 单列取首列。

    通用规则：若第二列存在、非价格且含字母，则视为友好名称（如 'Galaxy S21 5G'），
    否则用首列（如 jp 的 'Galaxy A25 5G'，其二列是价格）。
    """
    r0 = (r[0] or "").strip()
    if len(r) > 2:
        r1 = (r[1] or "").strip()
        if r1 and not extract_price(r1) and re.search(r"[A-Za-z]", r1):
            return r1
    return r0


async def samsung_repair_table(page, query, model, part):
    """三星维修费用表（tr/jp/ae 等）：读取页面全部 <table>（textContent，绕过手风琴折叠）。

    表头首列（可能为『代码+名称』双列，如 tr 的 Model Kodu/Model Adı）为机型列，
    其余列为部件价；按表头正则归一化为中文部件名，并过滤平板/手表等非手机。
    model 给定时只返回该机型行；model=None 时返回全部（discover_samsung_models 用）。
    币种为 TRY 时 '₺8.200' 的 '.' 是千分位（extract_price 已特殊处理 → 8200）。
    """
    grids = await _read_all_tables_text(page)
    model_re = re.compile(r"model|kodu|ad[ıi]|名|机型|型号|modell|mod[eè]le|модель", re.I)
    phone_re = re.compile(r"\btab\b|\bwatch\b|\bbuds\b|\bbook\b|\bfit\b|\btv\b", re.I)
    out = []
    for grid in grids:
        if not grid or len(grid) < 2:
            continue
        header = grid[0]
        # 机型列 = 从首列起、连续命中 model 正则的最右列
        model_col = -1
        for j, h in enumerate(header):
            if model_re.search(h or ""):
                model_col = j
            else:
                break
        if model_col < 0:
            model_col = 0
        for r in grid[1:]:
            if not r or len(r) <= model_col:
                continue
            m = _samsung_model_of(r)
            if not m:
                continue
            if phone_re.search(m):
                continue
            if model and model.lower() not in m.lower():
                continue
            for j in range(model_col + 1, len(r)):
                cell = (r[j] or "").strip()
                price = extract_price(cell)
                if price is None:
                    continue
                h = (header[j] if j < len(header) else "") or ""
                part_label = _canon_part(h) if h else f"col{j}"
                out.append({"cells": [m, part_label, cell],
                            "price": price, "model": m, "part": part_label})
    return out


async def _page_fetch(page, url, timeout_ms=15000):
    """在浏览器同源上下文里 fetch JSON（绕过 CDN 对 curl 的 302 拦截）。

    带 AbortSignal 超时：失败/超时返回 {error: ...} 而非抛异常，便于调用方逐条跳过而非整轮崩。
    """
    # 注意：Playwright 1.62 的 Page.evaluate(expression, arg=None) 只接受单个值型 arg，
    # 不支持把 url 与 timeout 当两个位置参数传入（旧版 timeout 位置参数已移除）。
    # 因此把 url 与 timeout 打包成单一数组 arg，在 JS 内解构；超时仍由 AbortController 实现。
    return await page.evaluate(
        """async (args) => {
            const [u, t] = args;
            try {
                const ctrl = new AbortController();
                const id = setTimeout(() => ctrl.abort(), t);
                const r = await fetch(u, {credentials: 'include', signal: ctrl.signal});
                clearTimeout(id);
                if (!r.ok) return {error: 'HTTP ' + r.status};
                const j = await r.json();
                return j.data || j;
            } catch (e) { return {error: String(e)}; }
        }""", [url, timeout_ms])


async def _page_fetch_post(page, url, payload, timeout_ms=20000, credentials="include"):
    """在浏览器同源上下文里 POST JSON（OPPO 新一代 REBORN /basic/v1/* 只接受 POST）。

    与 _page_fetch 的区别：① 方法为 POST + Content-Type: application/json；
    ② 返回**完整信封**（{code,msg,data}），不预先解包 data —— 因为调用方需要先用
    code 判断业务是否成功（如"机型不存在"会以 code!=1 返回），再取 data.partPriceList。
    失败/超时返回 {error: ...} 而非抛异常，便于逐条跳过而非整轮崩。

    credentials：cross-origin fetch 是否携带 cookie（omit/include）。
    OPPO 中国实测**带 cookie 与不带 cookie 返回的是两套价**（带 cookie：屏幕 950、9 条；
    不带：屏幕 850、11 条，与官网人读页一致），故 cn 配方显式声明 omit 以求与官网一致。
    """
    return await page.evaluate(
        """async (args) => {
            const [u, p, t, cred] = args;
            try {
                const ctrl = new AbortController();
                const id = setTimeout(() => ctrl.abort(), t);
                const r = await fetch(u, {
                    method: 'POST',
                    credentials: cred,
                    headers: {'Content-Type': 'application/json; charset=utf8'},
                    body: JSON.stringify(p),
                    signal: ctrl.signal
                });
                clearTimeout(id);
                if (!r.ok) return {error: 'HTTP ' + r.status};
                return await r.json();
            } catch (e) { return {error: String(e)}; }
        }""", [url, payload, timeout_ms, credentials])


def _http_post_json(url, body, referer, tries=4, to=20, bypass_proxy=False):
    """服务端直发 POST JSON（绕过浏览器）。用于 REBORN 且 fetch_mode=http 的场景。

    为什么需要它：REBORN 的 getProduct/getPartPriceNew 需要 POST；用服务端 http 直发
    比浏览器同源 fetch 更省资源（无需启动 Chromium、无需同源上下文），且已实测返回
    数据与官网人读页完全一致（OPPO Pad 5：屏幕 850、共 11 条）。

    更正：此前曾把「浏览器 fetch 得 950/9、服务端得 850/11」归因于 UA/代理分流，实为
    误判——950/9 是『OPPO Pad 5 柔光版』的真实价，850/11 是『OPPO Pad 5』的真实价，
    两者是不同机型；代理与直连返回一致。故本函数不再假设代理会改变价格。

    bypass_proxy=True 时建一个"无代理"opener（默认 False）。仅为确有按出口 IP 分流
    行为的站点保留该开关。
    """
    import urllib.request
    import time
    last = None
    if bypass_proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    for _ in range(tries):
        try:
            hdr = {"User-Agent": "Mozilla/5.0", "Referer": referer,
                   "Accept": "application/json",
                   "Content-Type": "application/json; charset=utf8",
                   "Origin": "https://support.oppo.com"}
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdr)
            with opener.open(req, timeout=to) as r:
                return {"_http_status": r.status, **(json.loads(r.read().decode("utf-8", "replace")))}
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(1.2)
    return {"error": last}


def _http_get_text(url, referer, tries=4, to=20):
    """服务端直发 GET，返回**原始文本**（JSONP 必须保留原文，故此处不解析）。

    与 _http_post_json 同族：都是为了「不启动 Chromium 也能取官方数据」。
    小米 api2.service.order.mi.com 是 JSONP 接口，强制要 callback 参数，所以
    不能用 json.loads 直接读。重试与超时语义和 POST 版保持一致。
    """
    import urllib.request
    import time
    last = None
    for _ in range(max(1, int(tries or 1))):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
                "Referer": referer,
                "Accept": "*/*",
            })
            with urllib.request.build_opener().open(req, timeout=to) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.6)
    raise RuntimeError(last or "http get failed")


def _jsonp_parse(text):
    """剥掉 JSONP 外壳返回 dict。cb({...}); / mi_message_callback({...}); 两种都吃。

    小米不加 callback 时返回 {"code":-30005,"msg":"jsonp没有传入callback"}，
    所以调用方一律带 callback 再走本函数解析。解析失败返回 None（由调用方判为
    「请求层失败」而非「官方无价」，两者必须区分，见 run.py 的四态三分类）。
    """
    if not text:
        return None
    t = text.strip()
    i, j = t.find("("), t.rfind(")")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(t[i + 1:j])
    except Exception:
        return None


# 小米备件价页（www.mi.com/service/materialprice）背后的官方 JSONP 接口。
# 页面本身把 $DATA（机型→备件下拉树）内联在 HTML 的 `var $DATA = {...}` 里，
# 但**不含价格**；价格由选中型号后请求 shop_band_wx_price?class_id=<id> 得到。
# class_id 是 shop_class_info 树里 level=3 叶子节点（具体到内存/颜色/容量）的 id。
XIAOMI_SERVICE_API = "https://api2.service.order.mi.com"
XIAOMI_REFERER = "https://www.mi.com/service/materialprice"


def xiaomi_class_tree(service_api=None, tries=3, to=20):
    """取小米分类树（shop_class_info）：L1 品类 -> L2 系列 -> L3 具体型号（带 id）。

    一次请求即可拿到全站 3671 个 L3 叶子，无需浏览器、无需翻页/点击。
    """
    api = (service_api or XIAOMI_SERVICE_API).rstrip("/")
    raw = _http_get_text(f"{api}/repair_price/shop_class_info?keyword=&callback=cb",
                         XIAOMI_REFERER, tries=tries, to=to)
    return _jsonp_parse(raw)


def xiaomi_iter_models(tree, categories=None, series_filter=None):
    """从分类树里摊平出 L3 型号，可按 L1 品类名过滤，并可只对指定 L1 再按 L2 系列名过滤。

    返回 [{"class_id":int, "model":str, "category":str, "series":str}, ...]。
    categories=None 表示全部品类；给定列表时只取标题命中的 L1。

    series_filter（可选）：{L1品类名: [L2系列名关键字]}，**只对配置到的 L1 生效**，
    用于"一个大类里混了多个品类、只想要其中一部分"的场景。例：小米「电脑办公」L1 里
    既有平板（小米平板7 / Redmi Pad Pro）又有笔记本（RedmiBook / 小米笔记本），
    配 {"电脑办公": ["平板", "Pad"]} 即可只收平板、排除笔记本。
    """
    out = []
    if not tree:
        return out
    for l1 in (tree.get("data") or {}).get("shop_class_info") or []:
        cat = (l1.get("title") or "").strip()
        if categories and cat not in categories:
            continue
        keys = (series_filter or {}).get(cat)
        for l2 in l1.get("children") or []:
            series = (l2.get("title") or "").strip()
            if keys and not any(k in series for k in keys):
                continue  # 该 L1 配了系列白名单而本系列不命中 → 跳过（如笔记本）
            for l3 in l2.get("children") or []:
                if l3.get("status") not in (None, 1):
                    continue  # status=0 为已下架
                cid, title = l3.get("id"), (l3.get("title") or "").strip()
                if cid and title:
                    out.append({"class_id": int(cid), "model": title,
                                "category": cat, "series": series})
    return out


def xiaomi_price_rows(class_id, model, service_api=None, tries=3, to=20):
    """按 class_id 取单机型官方价表，产出与 xiaomi_material_table 同构的行。

    官方字段（实测 shop_band_wx_price 返回）：
      materials[].shop_material_class_name  备件名称
      materials[].sale_price                保外物料指导价（元）
      materials[].handwork_cost             保外人工指导价（元）
    总价 = 物料 + 人工，与跨品牌口径（含人工总价）一致。

    返回 (rows, err, status)：
      status='ok'      取到价表
      status='noprice' 官方明确该型号暂无数据（code=14），**不是抓取失败**
      status='error'   请求层失败（网络/解析），需要重试或如实上报
    """
    api = (service_api or XIAOMI_SERVICE_API).rstrip("/")
    url = f"{api}/repair_price/shop_band_wx_price?class_id={int(class_id)}&callback=cb"
    try:
        payload = _jsonp_parse(_http_get_text(url, XIAOMI_REFERER, tries=tries, to=to))
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}", "error"
    if payload is None:
        return [], "JSONP 解析失败（返回体非预期结构）", "error"
    if payload.get("code") != 200:
        # code=14「该产品暂无相关数据」= 官方本就没有这台机的价，属"没得抓"而非"抓坏了"
        return [], f"code={payload.get('code')} {payload.get('msg') or ''}".strip(), "noprice"
    materials = (payload.get("data") or {}).get("materials") or []
    if not materials:
        return [], "code=200 但 materials 为空", "noprice"
    rows = []
    for m in materials:
        part_name = (m.get("shop_material_class_name") or "").strip()
        material = _amount_or_none(m.get("sale_price"))
        labor = _amount_or_none(m.get("handwork_cost"))
        if not part_name or (material is None and labor is None):
            continue
        total = (material or 0) + (labor or 0)
        if labor is None:
            labor_note = "官网该机型价表未单列人工费（仅列保外物料指导价）"
            has_split = 0
        else:
            labor_note = (f"官网《保外人工指导价（元）》列明确单列：{int(labor)} 元（币种 CNY）；"
                          f"同表《保外物料指导价（元）》列：{int(material or 0)} 元。"
                          f"保外维修费 = 物料 + 人工 = {int(total)} 元。")
            has_split = 1
        rows.append({
            "cells": [model or "", part_name,
                      str(int(material)) if material is not None else "—",
                      str(int(labor)) if labor is not None else "—"],
            "price": total or None,
            "material_fee": material,
            "labor_fee": labor,
            "has_labor_split": has_split,
            "labor_note": labor_note,
            "labor_source_url": url,
            "model": model, "part": part_name,
        })
    if not rows:
        return [], "价表存在但无可解析行", "noprice"
    return rows, None, "ok"


def _amount_or_none(v):
    """官方金额字段可能是 '1120' / 1120 / '0' / '' / None —— 统一成 float 或 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if float(v) > 0 else None
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f > 0 else None


# ---------------------------------------------------------------- vivo 官方接口
# vivo support 备件页的机型清单**已 SSR 内联**在 HTML 里：
#   <li class="select-model-item" data-id="3687">T1 Pro 5G</li>
# 价格不是 DOM 直出，而是点选后 POST 官方接口取的（见 bundle support/parts/index.pack_*.js）：
#   searchMaint: globalVar.path + '/' + globalVar.regionId + '/support/queryPriceByProductId'
# 故整条链路可完全不用浏览器：GET 页面拿 data-id → POST 接口拿价表。
#
# ⚠️ regionId 不等于国家码：ae 是 'ae/en'（带语言段，页面实际在 /ae/en/ 下，KB 里的
#    /ae/support/accessory 会 301 过去），my/tr 才是 'my'/'tr'。故 regionId 一律从页面
#    的 globalVar.regionId 现读，不按国家码拼。
# ⚠️ 显示价优先级 materialPrice > promotionPrice > price —— 与页面 getPartsDom 一致。
#    实测 ae/my 的响应里 materialPrice/promotionPrice 均为 null，只有 price 有值。
_VIVO_ITEM_RE = re.compile(
    r'<li class="select-model-item[^"]*"[^>]*data-id="(\d+)"[^>]*>([^<]*)</li>')
_VIVO_REGION_RE = re.compile(r"regionId:\s*['\"]([^'\"]+)")


def vivo_support_page(country, tries=3, to=25):
    """取 vivo 某国备件页，返回 (region_id, [(data_id, model_name), ...])。

    region_id 从页面 globalVar.regionId 现读（ae='ae/en'，my='my'，tr='tr'）。
    cn 是独立域名 vivo.com.cn，走专用接口，见 vivo_cn_support_page。
    """
    if country == "cn":
        return vivo_cn_support_page(tries=tries, to=to)
    url = f"https://www.vivo.com/{country}/support/accessory"
    html = _http_get_text(url, url, tries=tries, to=to)
    m = _VIVO_REGION_RE.search(html or "")
    items = _VIVO_ITEM_RE.findall(html or "")
    seen, out = set(), []
    for did, name in items:
        name = (name or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append((did, name))
    return (m.group(1) if m else country), out


def vivo_price_rows(region_id, data_id, model, tries=3, to=25):
    """POST queryPriceByProductId 取该机型价表，产出与 vivo_parts_grid 同构的行。

    返回 (rows, err, status)：ok / noprice（官方无此机型价）/ error（请求层失败）。
    vivo 官网每件只给一个总价，**未单列物料/人工拆分**，故不设 has_labor_split，
    由 write_rows 统一按「官网未单列人工费」标注（与 DOM 路径口径一致）。

    region_id=='cn' 时改走 vivo.com.cn 的中国专用接口（vivo_cn_price_rows）。
    """
    if region_id == "cn":
        return vivo_cn_price_rows(data_id, model, tries=tries, to=to)
    import urllib.parse
    import urllib.request
    import time
    referer = f"https://www.vivo.com/{region_id}/support/accessory"
    url = f"https://www.vivo.com/{region_id}/support/queryPriceByProductId"
    last = None
    payload = None
    for _ in range(max(1, int(tries or 1))):
        try:
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode({"id": int(data_id)}).encode(),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
                         "Referer": referer, "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
            with urllib.request.build_opener().open(req, timeout=to) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.5)
    if payload is None:
        return [], last or "请求失败", "error"
    if not payload.get("success"):
        return [], f"success=false msg={payload.get('msg')}", "error"
    sp = ((payload.get("data") or {}).get("sparePartVO")) or {}
    lst = sp.get("sparePartsVoList") or []
    if not lst:
        # 官方返回成功但没有价表 = 该机型未公布备件价（noprice，非故障）
        return [], "success=true 但 sparePartsVoList 为空", "noprice"
    currency = (sp.get("spareCompany") or "").strip()
    rows = []
    for it in lst:
        name = (it.get("name") or "").strip()
        price = None
        for k in ("materialPrice", "promotionPrice", "price"):
            price = _amount_or_none(it.get(k))
            if price is not None:
                break
        if not name or price is None:
            continue
        rows.append({
            "cells": [model or "", name, str(int(price)) if price == int(price) else str(price), "—"],
            "price": price,
            "model": model, "part": name,
            "vivo_currency_note": currency,
        })
    if not rows:
        return [], "价表存在但无可解析行", "noprice"
    return rows, None, "ok"


# ---------------------------------------------------------------- vivo 中国（vivo.com.cn）
# vivo 中国是独立域名 vivo.com.cn，维修价工具在 /service/accessory（不是 /support/accessory）：
#   GET  https://www.vivo.com.cn/service/accessory                 （仅 warm，机型清单走接口）
#   POST https://www.vivo.com.cn/service/accessory/product/list
#        -> data[]: {seriesName, seriesId, products:[{id, code, name, categoryId, ...}]}
#   POST https://www.vivo.com.cn/service/accessory/query/v2        body {productId:<variant id>}
#        -> data[]: {categoryCode, categoryName,
#                    items:"[{partItemId,itemId,componentName,price,promotionPrice,
#                            manualFee,priceType,remark,...}]"}
#   GET  https://cust.vivo.com.cn/commonservice/repair-component/product/sku?productId=<机型id>
#        -> data[]: {skuCode, ram(版本), color(颜色), img, ...}   （某机型的全部 SKU）
# ⚠️ 关键（2026-09-22 修正）：query/v2 必须带 skuCode，否则只返回该机型**全 SKU 的默认目录**
#   （看似"品牌级"、与机型无关，实为未指定 SKU 的兜底全量）。带 skuCode（取自 skuInfo 首个
#   SKU）后返回的是**真实逐机型备件价**（屏幕/电池/后盖/主板/摄像头…，priceType==1 为标准件）。
#   备件价按机型口径稳定（与版本/颜色基本无关），故每机型取首个 SKU 作代表即可，无需 422×N 展开。
#   用户纠正（原 unavailable 误判）：vivo 中国官网确实提供逐机型备件价，选型号→版本→颜色即见。
_VIVO_CN_PAGE = "https://www.vivo.com.cn/service/accessory"
_VIVO_CN_LIST = "https://www.vivo.com.cn/service/accessory/product/list"
_VIVO_CN_QUERY = "https://www.vivo.com.cn/service/accessory/query/v2"
_VIVO_CN_SKU = "https://cust.vivo.com.cn/commonservice/repair-component/product/sku"
_VIVO_CN_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": _VIVO_CN_PAGE,
}


def vivo_cn_support_page(tries=3, to=25):
    """vivo 中国机型清单：POST product/list，展平 家族→变体。

    返回 (region_id, [(data_id, model_name), ...])；region_id 固定 'cn'。
    """
    try:  # warm（拿合法 Referer，可选，失败不影响取数）
        _http_get_text(_VIVO_CN_PAGE, _VIVO_CN_PAGE, tries=1, to=to)
    except Exception:
        pass
    import urllib.request
    import time
    last = None
    payload = None
    for _ in range(max(1, int(tries or 1))):
        try:
            req = urllib.request.Request(_VIVO_CN_LIST, data=b"", headers=_VIVO_CN_HDR)
            with urllib.request.build_opener().open(req, timeout=to) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.5)
    if payload is None:
        raise RuntimeError(f"vivo_cn product/list 失败: {last}")
    # vivo cn 用 retCode="200" + retMsg="OK"（非 succ）
    if str(payload.get("retCode")) != "200" and payload.get("retCode") not in ("OK", "succ", 200):
        raise RuntimeError(f"vivo_cn product/list retCode={payload.get('retCode')}")
    out, seen = [], set()
    for fam in (payload.get("data") or []):
        for v in (fam.get("products") or []):
            vid = v.get("id")
            name = (v.get("name") or "").strip()
            if not vid or not name:
                continue
            key = (vid, name)
            if key in seen:
                continue
            seen.add(key)
            out.append((str(vid), name))
    return "cn", out


def vivo_cn_price_rows(data_id, model, tries=3, to=25):
    """vivo 中国取价：先 GET skuInfo 取该机型 SKU 列表（版本/颜色 + skuCode），
    再用首个 SKU 的 skuCode 调 POST query/v2 {productId, skuCode} 取**逐机型**备件价。
    返回 (rows, err, status)。

    ⚠️ 必须带 skuCode：空 skuCode 只返回该机型全 SKU 的默认目录（看似"品牌级"）。
    仅取 priceType=='1' 的标准部件（优惠换/安心换等营销方案 priceType=4/5 跳过），
    每个类目只保留「与类目同名」的 canonical 部件（屏幕/电池/后盖/主板/…）。
    price 优先 promotionPrice（官方透明价/活动价），无则 price；
    manualFee 作为人工费单列（has_labor_split=1）。
    """
    import urllib.parse
    import urllib.request
    import time
    data_id_i = int(data_id)
    # 1) skuInfo -> 首个 SKU 的 skuCode（代表该机型的版本+颜色）
    sku_code, sku_desc = "", ""
    sku_payload = None
    for _ in range(max(1, int(tries or 1))):
        try:
            req = urllib.request.Request(
                f"{_VIVO_CN_SKU}?productId={data_id_i}", headers=_VIVO_CN_HDR)
            with urllib.request.build_opener().open(req, timeout=to) as r:
                sku_payload = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception as e:
            time.sleep(0.5)
    skulist = (sku_payload or {}).get("data") or []
    if skulist:
        s0 = skulist[0]
        sku_code = str(s0.get("skuCode") or "")
        sku_desc = "/".join(x for x in (str(s0.get("ram") or ""), str(s0.get("color") or "")) if x)
    # 2) query/v2 带 skuCode
    last = None
    payload = None
    for _ in range(max(1, int(tries or 1))):
        try:
            req = urllib.request.Request(
                _VIVO_CN_QUERY,
                data=urllib.parse.urlencode(
                    {"productId": data_id_i, "skuCode": sku_code}).encode(),
                headers=_VIVO_CN_HDR)
            with urllib.request.build_opener().open(req, timeout=to) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            time.sleep(0.5)
    if payload is None:
        return [], last or "请求失败", "error"
    if str(payload.get("retCode")) != "200" and payload.get("retCode") not in ("OK", "succ", 200):
        return [], f"retCode={payload.get('retCode')}", "error"
    rows = []
    for cat in (payload.get("data") or []):
        cat_name = (cat.get("categoryName") or "").strip()
        items = cat.get("items")
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        std = [it for it in (items or []) if str(it.get("priceType")) == "1"]
        if not std:
            continue
        # canonical：与类目同名的部件优先，否则取首个标准部件
        cand = [it for it in std if (it.get("componentName") or "").strip() == cat_name] or std[:1]
        for it in cand:
            name = (it.get("componentName") or "").strip()
            promo = _amount_or_none(it.get("promotionPrice"))
            base = _amount_or_none(it.get("price"))
            price = promo if promo is not None else base
            manual = _amount_or_none(it.get("manualFee"))
            if not name or price is None:
                continue
            rows.append({
                "cells": [model or "", name,
                          str(int(price)) if price == int(price) else str(price), "—"],
                "price": price, "model": model, "part": name,
                "vivo_currency_note": "CNY",
                "material_fee": price, "labor_fee": manual,
                "has_labor_split": 1 if manual is not None else 0,
                "labor_note": "vivo 中国官网单列人工费(manualFee)" if manual is not None else None,
                "labor_source_url": _VIVO_CN_PAGE,
                "sku_code": sku_code, "sku_desc": sku_desc,
                "source_url": (f"{_VIVO_CN_QUERY}?productId={data_id_i}"
                               f"&skuCode={urllib.parse.quote(sku_code)}"),
            })
    if not rows:
        return [], "标准部件价表为空（该机型无 priceType=1 备件/无可用 SKU）", "noprice"
    return rows, None, "ok"


def _vivo_api_for_model(model, country):
    """按型号名在备件页 SSR 列表里定位 data_id 再取价（供单机型 CLI 用）。

    批量抓取走 run.py 的 discover_and_price_via_vivo（一次拿页面，并发取价），
    本函数只服务 `executor.py --brand vivo --country ae --model <全称>` 这种单点用法。
    """
    try:
        region_id, items = vivo_support_page(country)
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}", "error"
    if not items:
        return [], "备件页未解析出机型列表（SSR 结构变更？）", "error"
    low = (model or "").lower().strip()
    hit = next((x for x in items if x[1].lower().strip() == low), None)
    if not hit:
        hit = next((x for x in items if low and low in x[1].lower()), None)
    if not hit:
        return [], f"备件页机型列表中未找到「{model}」", "noprice"
    return vivo_price_rows(region_id, hit[0], hit[1])


async def vivo_api(page, query, model, part, country="de"):
    """vivo_api：服务端直发 vivo 官方 queryPriceByProductId 接口取价（**不需要浏览器**）。

    与 vivo_parts_grid（DOM 点选）的分工：后者在 /ae/en 等页面选择器命中失败返回 0 行
    （issue #55），且实测存在**跨机型串值**——DOM 竞态读到上一台机型的表格，
    导致 X300 Pro 的「屏幕」被写成 270 而官网接口读数是 1030（见 calibration）。
    接口路径来自页面 bundle：searchMaint 打到
    globalVar.path + '/' + globalVar.regionId + '/support/queryPriceByProductId'。
    故本模式不再依赖 DOM，价格的唯一真源是官方接口。

    同步 urllib 必须丢线程池（asyncio.to_thread），否则会占住事件循环，
    让 run_all 的区域级并发退化成串行（同 §15.3 / xiaomi_api 的坑）。
    """
    rows, err, status = await asyncio.to_thread(_vivo_api_for_model, model, country)
    if status != "ok":
        return [{"cells": ["(待回填) vivo 接口未取到价表", model or "", "—"], "price": None,
                 "model": model, "has_labor_split": 0,
                 "labor_note": f"vivo 官方接口未返回该机型价表：{err}", "labor_source_url": None}]
    return rows


async def xiaomi_api(page, query, model, part):
    """xiaomi_api：服务端直发小米官方 JSONP 接口取价（**不需要浏览器**，page 忽略）。

    与 xiaomi_material_table（DOM 点选）的分工：后者是 2026-08 校准时的可行路径，
    但 943 台逐台点选 × 12s 预等待，实测跑到一半 Page crashed（见 issue #53），
    且人工费列从未成功落库（has_labor_split 全 0）。本模式改为打官方 JSONP：
    一次拿分类树，再按 class_id 取价表，实测 943 台约 48s、0 报错。

    同步 urllib 必须丢线程池（asyncio.to_thread），否则会占住事件循环，
    让 run_all 的区域级并发退化成串行（同 §15.3 的坑）。
    """
    q = query or {}
    cats = q.get("categories")
    api = (q.get("api") or {}).get("service_api")
    rows, err, status = await asyncio.to_thread(
        _xiaomi_api_for_model, model, cats, api)
    if status != "ok":
        return [{"cells": ["(待回填) 小米接口未取到价表", model or "", "—"], "price": None,
                 "model": model, "has_labor_split": 0,
                 "labor_note": f"小米官方接口未返回该机型价表：{err}", "labor_source_url": None}]
    return rows


def _xiaomi_api_for_model(model, categories=None, service_api=None):
    """按型号名在分类树里定位 class_id 再取价（供单机型 CLI 用）。

    批量抓取走 run.py 的 discover_and_price_via_xiaomi（一次拿树、并发取价），
    本函数只服务 `executor.py --brand xiaomi --country cn --model <全称>` 这种单点用法。
    """
    try:
        tree = xiaomi_class_tree(service_api)
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}", "error"
    if not tree:
        return [], "分类树请求失败或解析失败", "error"
    cands = xiaomi_iter_models(tree, categories)
    hit = next((c for c in cands if c["model"] == model), None)
    if not hit:
        hit = next((c for c in cands if model and model in c["model"]), None)
    if not hit:
        return [], f"分类树中未找到机型「{model}」", "noprice"
    return xiaomi_price_rows(hit["class_id"], hit["model"], service_api)


async def reborn_post(api, cc, code=None, page=None, fetch_mode=None, path_list=None,
                      path_price=None, tries=None, to=None):
    """发一次 REBORN POST（getProduct 或 getPartPriceNew），返回完整信封 dict。

    fetch_mode='http'：服务端 urllib 直发（最稳，且取官方人读价）；
    否则：浏览器同源 fetch（executor._page_fetch_post）。page 在 http 模式下忽略。

    tries/to（仅 http 模式生效，None=沿用 _http_post_json 默认 tries=4/to=20）：
      探测「有没有这个端点」与「取这台机型的价」对可靠性的要求不同——
      前者有候选节点可回退（run.py 做对冲试打），故用更短的 tries/to 快速失败；
      后者是最终数据来源，保留完整重试。调用方按用途显式传入，避免两处默认值漂移。
    """
    region = api.get("region") or cc
    body = {"region": region, "regionIsoCode2": api.get("region_iso") or cc,
            "brandCode": api.get("brand_code", "11"),
            "isoLanguageCode": api.get("iso_language") or cc, "sourceRoute": "1"}
    if code:
        body["marketingModelCode"] = code
    host = (api.get("base_url") or "").rstrip("/")
    path = (path_price if code else path_list) or (
        api.get("price_detail", "/basic/v1/getPartPriceNew") if code
        else api.get("product_list", "/basic/v1/getProduct"))
    # base_url 可写 "sow-cms.oppo.com/oppo-api" 或完整 "https://..." 两种形式
    full = host + path if host.startswith("https://") else f"https://{host}{path}"
    cred = api.get("credentials", "include")
    if fetch_mode == "http" or api.get("fetch_mode") == "http":
        # 必须丢线程池：_http_post_json 是 urllib 同步阻塞调用，直接在协程里跑会占住
        # 事件循环，调用方即便用 asyncio.gather 并发，实际仍是串行（实测无提速）。
        # 走 asyncio.to_thread 后并发才真正生效（见 crawler/run.py
        # discover_and_price_via_reborn 的 Semaphore 限流）。
        return await asyncio.to_thread(
            _http_post_json, full, body,
            api.get("referer") or "https://support.oppo.com/cn/spare-parts-price/",
            tries if tries else 4, to if to else 20,
            bypass_proxy=bool(api.get("bypass_proxy")))
    return await _page_fetch_post(page, full, body, 20000, cred)


def flatten_reborn_parts(groups):
    """把 REBORN getPartPriceNew 的 partPriceList 拍平成逐备件行。

    结构：partPriceList[] = 分类组（groupName/groupCode，如"屏幕"/"主板"），
    组内 childList[] = 具体备件（partName/retailPrice/laborCostAmount/lv3ClassificationName）。
    部分组无 childList（叶子即自身）。报价口径：官网"预估价格 = 人工费 + 备件费"，
    故 retailPrice 记 material_fee、laborCostAmount 记 labor_fee，且 has_labor_split=1
    （官网明确单列人工费，非本项目推算）。
    """
    rows = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        gname = g.get("groupName") or g.get("lv3ClassificationName") or ""
        children = g.get("childList") or [g]
        for c in children:
            if not isinstance(c, dict):
                continue
            part = c.get("partName") or c.get("lv3ClassificationName") or gname
            retail = c.get("retailPrice")
            disc = c.get("discountRetailPrice")
            eff = retail if retail is not None else disc
            labor = c.get("laborCostAmount")
            cur = c.get("retailPriceCurrency") or ""
            def _num(v):
                return _parse_amount(v)
            rows.append({
                "cells": [None, (f"{gname} / " if gname and gname != part else "") + (part or ""),
                          str(eff)],
                "price": _num(eff),
                "part": part,
                "material_fee": _num(retail),
                "labor_fee": _num(labor),
                "has_labor_split": 1 if labor is not None else 0,
                "labor_note": (f"官网单列人工费；备件价 {retail} {cur}，"
                               f"预估总价 = 备件费 + 人工费") if labor is not None else None,
                "raw": c,
            })
    return rows


async def vivo_parts_grid(page, query, model, part):
    """vivo support 页确定性回放：SPA 自定义下拉选机型 -> 渲染备件价网格。

    KB query 字段：
      pre_wait_ms   进页后等待 SPA 渲染(默认 15000)
      model_trigger 触发器选择器(默认 '#boxSelectModel, .box-select-model')
      model_option  选项 li 选择器(默认 'li.select-model-item')
      extract.row_locator / part_locator / price_locator
    返回 rows(cells=[机型, 部件, 价], 含 model/part/price)。
    """
    await page.wait_for_timeout(int(query.get("pre_wait_ms", 15000)))
    trig = page.locator(query.get("model_trigger", "#boxSelectModel, .box-select-model")).first
    if not await trig.count():
        return [{"cells": ["ERROR: vivo 机型触发器不存在（该区域可能无备件价工具）"], "price": None}]
    # JS 点击：部分区域选项 aria-hidden，Playwright 普通 click 会超时，必须 el.click()
    await trig.evaluate("el => el.click()")
    opt_sel = query.get("model_option", "li.select-model-item")
    opts = page.locator(opt_sel)
    for _ in range(12):  # 轮询选项列表（惰性渲染）
        if await opts.count() > 0:
            break
        await page.wait_for_timeout(500)
    if not await opts.count():
        return [{"cells": ["ERROR: vivo 机型选项未渲染"], "price": None}]
    chosen = await page.evaluate(
        """(args) => {
            const [model, sel] = args;
            const lis = [...document.querySelectorAll(sel)];
            let el = model ? lis.find(x => (x.innerText||'').includes(model)) : null;
            if (!el) el = lis[0];
            if (!el) return null;
            const t = el.innerText.trim();
            el.click();
            return t;
        }""", [model, opt_sel])
    if not chosen:
        return [{"cells": ["ERROR: vivo 未找到可选机型"], "price": None}]
    ex = query.get("extract", {})
    data = []
    for _ in range(20):  # 轮询价格行渲染
        data = await page.evaluate(
            """(args) => {
                const [rowSel, partSel, priceSel] = args;
                const out = [];
                for (const r of document.querySelectorAll(rowSel)) {
                    const nm = r.querySelector(partSel)?.innerText?.trim();
                    const pr = r.querySelector(priceSel)?.innerText?.trim();
                    if (nm && pr) out.push({part: nm, price: pr});
                }
                return out;
            }""", [ex.get("row_locator", "li.container-box-item"),
                   ex.get("part_locator", ".item-left-name"),
                   ex.get("price_locator", ".item-right-price")])
        if data:
            break
        await page.wait_for_timeout(500)
    if not data:
        return [{"cells": [chosen, "(无价行)", "—"], "price": None, "model": chosen}]
    rows = []
    for d in data:
        rows.append({"cells": [chosen, d["part"], d["price"]],
                     "price": extract_price(d["price"]),
                     "model": chosen, "part": d["part"]})
    return rows


async def xiaomi_material_table(page, query, model, part):
    """小米 materialprice 页确定性回放：点系列展开 -> 点具体型号 -> 读 HTML 价表。

    KB query 字段：
      pre_wait_ms 进页等待(默认 12000)
      series      系列名(如 'Xiaomi 13 Pro')；缺省取 model
      extract.table_locator(默认 'table')
      extract.cols 列映射(默认 part=0, material_price=1, labor_price=2)
    model 参数须为页面完整型号名(含内存/颜色/容量)。
    价表列：备件名称 / 保外物料指导价(元) / 保外人工指导价(元)。

    人工费取证(P1-1)：逐行拆出『保外人工指导价』列金额，labor_note 写官网原文说明
    （含金额与币种），labor_source_url 写该价表页 URL；官网明确单列，绝不编造。
    总价 = 物料 + 人工（与跨品牌口径一致：其它品牌给的是含人工总价）。
    """
    await page.wait_for_timeout(int(query.get("pre_wait_ms", 12000)))
    series = query.get("series") or (model or "")
    if series:
        s_el = page.get_by_text(series, exact=False).first
        if await s_el.count():
            try:
                await s_el.click(timeout=8000)
                await page.wait_for_timeout(2000)
            except Exception as e:
                sys.stderr.write(f"[xiaomi] series click warn: {e}\n")
    if model:
        v_el = page.get_by_text(model, exact=False).first
        if await v_el.count():
            try:
                await v_el.click(timeout=8000)
                await page.wait_for_timeout(4000)
            except Exception as e:
                sys.stderr.write(f"[xiaomi] variant click warn: {e}\n")
    tbl = page.locator(query.get("extract", {}).get("table_locator", "table")).first
    ex = query.get("extract", {})
    cols = ex.get("cols", {"part": 0, "material_price": 1, "labor_price": 2})
    p_i, m_i, l_i = cols.get("part", 0), cols.get("material_price", 1), cols.get("labor_price", 2)
    rows = []
    if await tbl.count():
        for tr in await tbl.locator("tbody tr, tr").all():
            cells = await tr.locator("td").all_inner_texts()
            if not cells:
                continue
            part_name = cells[p_i].strip() if len(cells) > p_i else ""
            material = extract_price(cells[m_i]) if len(cells) > m_i else None
            labor = extract_price(cells[l_i]) if len(cells) > l_i else None
            if material is None and labor is None:
                continue  # 表头/空行
            total = (material or 0) + (labor or 0)
            if labor is not None:
                labor_note = (f"官网《保外人工指导价（元）》列明确单列：{int(labor)} 元（币种 CNY）；"
                              f"同表《保外物料指导价（元）》列：{int(material)} 元。"
                              f"保外维修费 = 物料 + 人工 = {int(total)} 元。")
                has_split = 1
            else:
                labor_note = "官网价表仅列保外物料指导价，未单列人工费（人工费含于官方授权服务，未公开拆分）"
                has_split = 0
            rows.append({
                "cells": [model or "", part_name,
                          str(int(material)) if material is not None else "—",
                          str(int(labor)) if labor is not None else "—"],
                "price": total or None,
                "material_fee": material,
                "labor_fee": labor,
                "has_labor_split": has_split,
                "labor_note": labor_note,
                "labor_source_url": page.url,
                "model": model, "part": part_name,
            })
    if not rows:
        return [{"cells": ["(待回填) 小米价表未渲染", model or "", "—"], "price": None, "model": model,
                 "has_labor_split": 0,
                 "labor_note": "官网未单列人工费（价表未渲染，无法取证）",
                 "labor_source_url": page.url}]
    return rows


async def google_estimator(page, query, model, part, country="de"):
    """Google repair-cost-estimator 确定性回放(store.google.com/<国>/repair-cost-estimator?hl=<语>)。

    校准后(用户真实 IP 跑 google_calibrate.py)会写回 references/kb/google_locators.json，
    本函数优先使用其中的精确定位器；缺失时回退通用金额配对（与校准脚本一致）。
    选 Pixel 设备 -> 读维修一口价(含零件+人工，非拆零件价)。
    """
    locs = {}
    needs_imei = False
    if GOOGLE_LOCS.exists():
        try:
            _l = json.loads(GOOGLE_LOCS.read_text(encoding="utf-8"))
            locs = _l.get("regions", {}).get(country) or _l.get("default") or {}
            needs_imei = bool(_l.get("needs_imei", False))
        except Exception:
            locs = {}
    # 1) 选机型（设备选择器存在时）
    dev_sel = locs.get("device_selector") or query.get(
        "device_selector", ".repair-device-select, [data-test='device'], select")
    dev = page.locator(dev_sel).first
    if await dev.count():
        try:
            if await dev.evaluate("e => e.tagName") == "SELECT":
                opts = await dev.locator("option").all_inner_texts()
                pick = next((o for o in opts if "pixel" in (o or "").lower()), (opts[0] if opts else None))
                if pick:
                    await dev.select_option(label=pick)
            else:
                await dev.click(timeout=6000)
                px = page.locator("li:has-text('Pixel'), [role='option']:has-text('Pixel')").first
                if await px.count():
                    await px.click(timeout=6000)
        except Exception as e:
            sys.stderr.write(f"[google] device select warn: {e}\n")
    await page.wait_for_timeout(3000)
    if needs_imei:
        return [{"cells": ["(需 IMEI/序列号) Google 估价需输入设备标识，无公开可抓价表",
                           model or "Pixel", "—"], "price": None, "model": model or "Pixel"}]
    # 2) 精确定位器优先（校准脚本写回）
    if locs.get("price_container") and locs.get("row_selector"):
        rows = await page.evaluate(
            """(args) => {
                const [container, rowSel, labelSel, priceSel] = args;
                const root = document.querySelector(container) || document.body;
                const out = [];
                for (const r of root.querySelectorAll(rowSel)) {
                    const lab = labelSel ? (r.querySelector(labelSel)?.innerText?.trim() || '') : '';
                    const pr = priceSel ? (r.querySelector(priceSel)?.innerText?.trim() || r.innerText.trim()) : r.innerText.trim();
                    if (lab || pr) out.push({label: lab, price: pr});
                }
                return out;
            }""", [locs["price_container"], locs["row_selector"],
                   locs.get("label_selector", ""), locs.get("price_selector", "")])
        if rows:
            res = []
            for r in rows:
                res.append({"cells": [model or "Pixel", r.get("label", ""), r.get("price", "")],
                            "price": extract_price(r.get("price", "") or ""),
                            "model": model or "Pixel", "part": r.get("label", "")})
            return res
    # 3) 回退：通用金额配对（与校准脚本一致）
    data = await page.evaluate("""() => {
        const priceRe = /[€$¥£AEDRM]\\s?[\\d.,]+|[\\d.,]+\\s?(円|€|$|¥|£|AED|RM)|[\\d,]+\\s?円/i;
        const lines = (document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
        const rows = [];
        for (let i=0;i<lines.length;i++){
            const l = lines[i];
            if (priceRe.test(l) && l.length < 40){
                rows.push({label: (i>0 && !priceRe.test(lines[i-1])) ? lines[i-1] : '', price: l});
            }
        }
        return {price_rows: rows, title: document.title};
    }""")
    rows = []
    for r in data.get("price_rows", []):
        rows.append({"cells": [model or "Pixel", r.get("label", ""), r.get("price", "")],
                     "price": extract_price(r.get("price", "") or ""),
                     "model": model or "Pixel", "part": r.get("label", "")})
    if not rows:
        return [{"cells": ["(待实跑回填) Google 估价页未取到价；请用 references/calibration/google_calibrate.py 在真实 IP 补完定位器",
                           model or "Pixel", "—"], "price": None, "model": model or "Pixel"}]
    return rows


# ── 已知不可信端点：硬阻断 ────────────────────────────────────────────────
# 有些端点**服务端忽略区域参数**：无论请求哪个 area，都返回同一份价格表。上层若按
# 「请求的 area」把它标成本地币种，再乘当地汇率折 CNY，就会把同一个原始数字放大成
# 数十倍的虚假跨国价差，且**不报错**——静默产出一整库看似正常的脏数据。
#
# 2026-09-22 实测：OPPO 遗留端点 sgp-sow-cms.oppo.com/oppo-server/cnw/v1/GetPartPrice
# 对 ae/de/jp/mx/my/tr 返回逐字节相同的中国大陆 CNY 价目表（6 国 1844 个公共
# (机型,备件) 键的价格集合指纹全部相等 = ece41d8aadf54a83；响应体里也没有任何币种
# 字段）。后果：2690 CNY 被写成 2690 EUR 与 2690 TRY，折 CNY 后 20982 vs 564.9，
# 同一数字被汇率放大 37 倍，前台显示「+3614%」的假价差。
# 已据此清退 13836 行污染数据（见 tools/purge_oppo_area_param_pollution.py）。
# 保留本阻断，防止配置回填后再次静默污染。
_BLOCKED_ENDPOINTS = (
    ("/cnw/v1/GetPartPrice?",
     "OPPO 遗留端点：服务端忽略 area 参数，各国均返回中国大陆 CNY 价目表"),
)

# 这些 key 的值是「说明文本」而非「要请求的 URL」；文案里出现端点名属正常的历史沿革
# 记录（如 KB 的 note 明确写着「旧端点已弃用」），扫进去会误伤。
_PROSE_KEYS = frozenset({"note", "notes", "model_match_note", "labor_note",
                         "screenshot", "evidence_note", "remark"})


def blocked_endpoint_hit(query):
    """扫描配置中所有「作为数据来源的 URL」，命中已知不可信端点则返回 (pattern, reason)。

    只扫 URL 语义的字符串，跳过 note/notes 等纯说明字段（见 _PROSE_KEYS）。
    """
    hits = []

    def _walk(o, in_prose=False):
        if isinstance(o, dict):
            for k, v in o.items():
                _walk(v, in_prose or k in _PROSE_KEYS)
        elif isinstance(o, list):
            for v in o:
                _walk(v, in_prose)
        elif isinstance(o, str) and not in_prose:
            hits.append(o)

    _walk(query)
    for s in hits:
        for pat, reason in _BLOCKED_ENDPOINTS:
            if pat in s:
                return pat, reason
    return None


async def run_query(page, query, model, part, country="de"):
    # 数据来源可信度闸门：先于一切抓取动作。命中即拒绝执行，避免静默写入脏数据。
    hit = blocked_endpoint_hit(query)
    if hit:
        pat, reason = hit
        return [{"cells": [f"BLOCKED: 配置指向已知不可信端点 {pat} —— {reason}；"
                           f"请改用信源（OPPO 走 api_reborn），详见 "
                           f"tools/purge_oppo_area_param_pollution.py"],
                 "price": None, "model": model}]
    mode = query.get("mode")
    if page is not None:
        await page.wait_for_timeout(1200)
    if mode == "api_json":
        api = query.get("api", {})
        base_global = api.get("base_global", "SOWAPIPATH")
        area = api.get("area") or "tr"
        language = api.get("language") or area
        # 等 SPA 把全局 API 基址挂上 window
        await page.wait_for_timeout(2500)
        base = await page.evaluate(f"() => window.{base_global} || window.GCSMAPIPATH || null")
        if not base:
            return [{"cells": [f"ERROR: 页面未暴露全局变量 {base_global}（未进入正确站点？）"], "price": None}]
        # 1) 机型列表
        list_url = base + api["product_list"].format(area=area, language=language)
        products = await _page_fetch(page, list_url)
        if isinstance(products, dict) and products.get("error"):
            return [{"cells": [f"机型列表错误: {products['error']}"], "price": None}]
        plist = products if isinstance(products, list) else products.get("list", [])
        # 2) 解析目标机型
        target = None
        if model:
            for x in plist:
                nm = (x.get("productModel") or x.get("name") or "")
                if model.lower() in nm.lower():
                    target = x
                    break
        if not target:
            flt = api.get("list_filter", "")
            if flt:
                for x in plist:
                    if "手机" in (x.get("productTypeName") or x.get("typeName") or ""):
                        target = x
                        break
            target = target or (plist[0] if plist else None)
        if not target:
            return [{"cells": ["ERROR: 未从 API 取到任何机型"], "price": None}]
        mdl = target.get("productModel") or target.get("name")
        # 3) 备件价
        price_url = base + api["price_detail"].format(area=area, language=language, model=mdl)
        data = await _page_fetch(page, price_url)
        if isinstance(data, dict) and data.get("error"):
            return [{"cells": [f"取价错误: {data['error']}"], "price": None}]
        parts = data if isinstance(data, list) else data.get("list", data.get("partList", []))
        ex = query.get("extract", {}).get("api", {})
        part_f = ex.get("part_field", "partName")
        price_f = ex.get("price_field", "partPrice")
        type_f = ex.get("type_field")
        rows = []
        for x in parts:
            pf = x.get(price_f)
            rows.append({
                "cells": [mdl,
                          (f"{x.get(type_f)} / " if type_f else "") + (x.get(part_f) or ""),
                          str(pf)],
                "price": _parse_amount(pf) if pf is not None else None,
                "model": mdl, "part": x.get(part_f), "raw": x,
            })
        return rows
    if mode == "api_reborn":
        # OPPO 新一代备件价接口（2026 起官网 /spare-parts-price/ 实际调用，取代遗留
        # sgp /cnw/v1/GetPartPrice）：
        #   POST {base}/basic/v1/getProduct      -> 全量机型（含 marketingModelCode）
        #   POST {base}/basic/v1/getPartPriceNew -> 该机型备件价（partPriceList[].childList[]）
        # 遗留端点按 productModel 名查价，机型覆盖不全（如 cn 的 OPPO Pad 5 在旧端点
        # 无记录、新端点有价），故新区域一律走本模式；区域由 api.region/region_iso 决定，
        # 不同区域落在不同 CDN 节点（cn→sow-cms / de→par-sow-cms / 亚太→sgp-sow-cms）。
        api = query.get("api", {})
        base = (api.get("base_url") or "").rstrip("/")
        if not base:
            return [{"cells": ["ERROR: api_reborn 缺少 api.base_url"], "price": None}]
        cc = api.get("region_iso") or country
        lst = await reborn_post(api, cc, page=page)
        if isinstance(lst, dict) and lst.get("error"):
            return [{"cells": [f"机型列表错误: {lst['error']}"], "price": None}]
        if str(lst.get("code")) not in ("1", "200"):
            return [{"cells": [f"机型列表返回异常: code={lst.get('code')} msg={lst.get('msg')}"],
                     "price": None}]
        plist = lst.get("data") or []
        cat_filter = api.get("category_filter")  # 如 ["手机","平板"]；缺省=不限品类
        if cat_filter:
            plist = [x for x in plist if (x.get("categoryName") or "") in cat_filter]
        target = None
        if model:
            low = model.lower().strip()
            # 优先精确匹配（大小写/前后空白忽略），避免 "OPPO Pad 5" 误命中
            # "OPPO Pad 5 柔光版"/"OPPO Pad 5 Pro" 等变体而取到错误机型价。
            for x in plist:
                if (x.get("marketingModelName") or "").lower().strip() == low:
                    target = x
                    break
            if not target:
                for x in plist:
                    if low in (x.get("marketingModelName") or "").lower():
                        target = x
                        break
        target = target or (plist[0] if plist else None)
        if not target:
            return [{"cells": ["ERROR: 未从 getProduct 取到任何机型"], "price": None}]
        mdl = target.get("marketingModelName")
        pr = await reborn_post(api, cc, code=target.get("marketingModelCode"), page=page)
        if isinstance(pr, dict) and pr.get("error"):
            return [{"cells": [f"取价错误: {pr['error']}"], "price": None}]
        data = pr.get("data") or {}
        rows = flatten_reborn_parts(data.get("partPriceList"))
        for r in rows:
            r["cells"][0] = mdl
            r["model"] = mdl
        if not rows:
            return [{"cells": [f"ERROR: {mdl} 官方无备件价记录（partPriceList 为空）"],
                     "price": None, "model": mdl}]
        return rows
    if mode == "spare_parts_table":
        if query.get("select_model") and model:
            await select_option_robust(page, query.get("select_model_sel", ".model-dropdown"), model)
            await page.wait_for_timeout(1200)
        return await read_table(page, query)
    if mode == "form_select_cascade":
        cascade = query.get("cascade", [])
        # 级联下拉需要先选 device/series，再选 model。KB 只传 model，需反推 device。
        device_val = None
        if model:
            dev_sel = next((c.get("sel") for c in cascade if c.get("field") == "device"), ".device-dropdown")
            dev_el = page.locator(dev_sel).first
            dev_opts = []
            if await dev_el.count():
                try:
                    dev_opts = [t.strip() for t in await dev_el.locator("option").all_inner_texts()]
                except Exception:
                    pass
            # 在 device 下拉选项中找与 model 最长公共前缀/包含的项
            nm = model.replace("\xa0", " ")
            best = None
            best_score = 0
            for o in dev_opts:
                lo = o.lower()
                lnm = nm.lower()
                if lnm.startswith(lo) or lo in lnm:
                    score = len(lo)
                    if score > best_score:
                        best_score = score
                        best = o
            if best:
                device_val = best
            else:
                # 兜底：如 "iPhone 12 Pro Max" -> "iPhone 12"
                m = re.search(r"([A-Za-z]+\s+\d+[A-Za-z]?)", model)
                if m:
                    device_val = m.group(1)
                else:
                    device_val = model.split()[0] if model.split() else model
        async def _select_field(sel, val):
            el = page.locator(sel).first
            if await el.count() == 0:
                return False
            try:
                is_select = await el.evaluate("e => e.tagName === 'SELECT'")
            except Exception:
                is_select = False
            if is_select:
                try:
                    opts = [t.strip() for t in await el.locator("option").all_inner_texts()]
                except Exception:
                    opts = []
                # 候选值：归一化不间断空格，并尝试 e 系列尾缀回退
                cands = [val]
                nv = val.replace("\xa0", " ")
                if nv != val:
                    cands.append(nv)
                # iPhone 16e/17e -> iPhone 16/17
                if re.search(r"\d+[a-zA-Z]$", val):
                    cands.append(re.sub(r"\d+[a-zA-Z]$", lambda m2: m2.group(0)[:-1], val))
                # 回退到词根：iPhone 12 Pro Max -> iPhone 12
                parts = val.split()
                if len(parts) >= 2:
                    cands.append(" ".join(parts[:2]))
                for v in cands:
                    try:
                        await el.select_option(label=v, timeout=5000)
                        return True
                    except Exception:
                        pass
                # 模糊包含匹配（按文本或按 index）
                for v in cands:
                    for idx, o in enumerate(opts):
                        if v in o or o.replace("\xa0", " ") == v:
                            try:
                                await el.select_option(index=idx, timeout=5000)
                                return True
                            except Exception:
                                pass
                            try:
                                await el.select_option(label=o, timeout=5000)
                                return True
                            except Exception:
                                pass
            else:
                if await click_option(page, val):
                    return True
            return False
        for c in cascade:
            field = c.get("field")
            sel = c.get("sel") or (".model-dropdown" if field == "model" else ".device-dropdown")
            val = None
            if field in ("model", "series", "category"):
                val = model
            elif field == "device":
                val = device_val
            if not val:
                continue
            ok = await _select_field(sel, val)
            if not ok:
                print(f"[warn] form_select_cascade 未能选择 {field}={val} (sel={sel})", flush=True)
            await page.wait_for_timeout(1200)
        await page.wait_for_timeout(1000)
        # 解析成本区：标题后的 "标签 + 金额" 多行
        heading = query.get("price_section_heading", "Estimated service cost")
        txt = await page.locator("body").first.inner_text()
        lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
        # ⚠️ 币种代码必须带**词首边界**，否则会命中单词里的字母片段：
        # 实测 `RM`（马来西亚林吉特）在 re.I 下匹配了 "Hermès" 里的 "rm"，
        # 于是 apple/jp 手表页的下拉选项「Apple Watch Hermès Series 10 …」被当成价格行，
        # extract_price 再抽出其中的机型序号 → 产出「部件名=机型名、价格=10 JPY」的垃圾行
        # （Series 10→10.0 / Ultra 3→2.0，共 170 行，仅 watch 页有 Hermès 机型故只中招它）。
        # `(?![A-Za-z])` 保证 "RM1,099" 仍能命中（后面是数字而不是字母）。
        # ⚠️ 必须含 RMB（Apple 中国站价格渲染为 "RMB 648"，既非 ¥ 也非 CNY）：
        # 缺 RMB 会导致 apple/cn 全部机型 fallback 也匹配不到 → 0 行（2026-09-22 实测 139 台全 0）。
        # RMB 须排在 RM 之前，且沿用 `(?![A-Za-z])` 词尾边界（"RMB 648" 后是空格可命中；
        # "Hermès" 里的 rm 无词首边界，仍不会误命中）。
        price_re = re.compile(r"[€$¥£]|円|\b(?:AED|CNY|EUR|JPY|MYR|RMB|RM|TRY)(?![A-Za-z])", re.I)
        rows = []
        active = False
        for i, ln in enumerate(lines):
            if not active and heading and re.search(re.escape(heading), ln, re.I):
                active = True
                continue
            if not active:
                continue
            if price_re.search(ln) and re.search(r"[\d][\d.,]*", ln):
                label = lines[i - 1] if i > 0 and not price_re.search(lines[i - 1]) else ""
                rows.append({"cells": [label, ln], "price": extract_price(ln), "part": label})
            # 遇到后续大段说明文字或 Get service 即停止
            if ln.lower() in ("get service",) or (rows and len(ln) > 200):
                break
        if not rows:
            # 兜底：从全页按 "上一行标签 + 当前行价格" 提取所有价格行
            for i, ln in enumerate(lines):
                if price_re.search(ln) and re.search(r"[\d][\d.,]*", ln):
                    label = lines[i - 1] if i > 0 and not price_re.search(lines[i - 1]) else ""
                    rows.append({"cells": [label, ln], "price": extract_price(ln), "part": label})
        return rows
    if mode == "service_fee_expand":
        if query.get("select_model") and model:
            await select_option_robust(page, query.get("select_model_sel", ".model-dropdown"), model)
            await page.wait_for_timeout(1000)
        trig = query.get("expand_trigger", {})
        try:
            await loc(page, trig.get("sel", "text=了解更多")).first.click(timeout=6000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        for m in query.get("modules", []):
            await select_option_robust(page, query.get("expand_module_sel", ".module-dropdown"), m)
            await page.wait_for_timeout(800)
        return await read_table(page, query)
    if mode == "vivo_parts_grid":
        return await vivo_parts_grid(page, query, model, part)
    if mode == "vivo_api":
        return await vivo_api(page, query, model, part, country)
    if mode == "xiaomi_material_table":
        return await xiaomi_material_table(page, query, model, part)
    if mode == "xiaomi_api":
        return await xiaomi_api(page, query, model, part)
    if mode == "google_estimator":
        return await google_estimator(page, query, model, part, country)
    if mode == "samsung_repair_table":
        return await samsung_repair_table(page, query, model, part)
    return []


async def fetch_price(brand, country, model=None, part=None, category="phone"):
    rec = load_record(brand, country, category)
    if not rec:
        return {"status": "kb_miss", "brand": brand, "country": country}
    if rec.get("status") != "verified":
        pass  # 允许跑，但标记为待校准
    if rec.get("status") in ("blocked", "unavailable"):
        if rec.get("status") == "blocked":
            note = ("该区域在本环境不可达（沙箱网络限制）。请在本机真实出口 IP 运行 executor "
                    "或 references/calibration/google_calibrate.py 补完定位器后转正。")
            extra = {"explorer_script": rec.get("query", {}).get("explorer_script")}
        else:
            note = "该区域官网未提供公开备件价查询工具（已实测不可达），无需运行。"
            extra = {}
        return {"status": rec.get("status"), "brand": brand, "country": country,
                "note": note, **extra}
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    shots = []
    today = datetime.now().strftime("%Y%m%d")
    async with async_playwright() as p:
        # --no-sandbox: 容器/沙箱环境下 Chromium 缺它会在导航时被 SIGKILL，静默吞掉全部输出
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(locale=rec.get("locale", ""))
        await page.context.set_extra_http_headers({})  # 可在此加 UA / 限速
        try:
            entry = rec.get("entry", {})
            entry_steps = entry.get("steps", [])
            if entry.get("url") and not any(s.get("act") == "goto" for s in entry_steps):
                # KB 未声明 goto 步骤时默认导航到 entry.url；校准脚本均显式 goto，
                # 若缺失会让页面停在 about:blank（vivo/小米/Google/Samsung 的 KB 即如此）。
                await page.goto(entry["url"], wait_until="commit")
                await page.wait_for_timeout(4000)
            for step in entry_steps:
                await run_step(page, step, EVIDENCE_DIR, shots)
            await page.wait_for_timeout(800)
            rows = await run_query(page, rec.get("query", {}), model, part, country)
            await page.screenshot(path=str(EVIDENCE_DIR / f"{brand}_{country}_{today}.png"))
            shot = str(EVIDENCE_DIR / f"{brand}_{country}_{today}.png")
            final_url = page.url
        except Exception as e:
            await browser.close()
            return {"status": "error", "brand": brand, "country": country,
                    "error": str(e), "shots": shots}
        await browser.close()
    result = {
        "status": "ok" if rec.get("reviewed") else "pending_review",
        "brand": brand, "country": country, "currency": rec.get("currency"),
        "model": model, "part": part, "rows": rows,
        "screenshot": shot, "url": final_url if "final_url" in dir() else rec["entry"].get("expect_url"),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "notes": rec.get("notes", ""),
    }
    return result


def render_markdown(result):
    lines = ["# 竞品备件价格取证报告", ""]
    if result.get("status") == "kb_miss":
        lines.append(f"- KB 未命中：{result['brand']} / {result['country']}，需走 Explorer 探索。")
        return "\n".join(lines)
    if result.get("status") == "error":
        lines.append(f"- ⚠️ 抓取失败：{result.get('brand')} / {result.get('country')}")
        lines.append(f"- 错误：{result.get('error', '未知')}")
        if result.get("shots"):
            lines.append(f"- 已截图：{', '.join(result['shots'])}")
        return "\n".join(lines)
    lines.append(f"- 品牌：**{result.get('brand')}** ｜ 国家：**{result.get('country')}** ｜ 币种：{result.get('currency', '')}")
    lines.append(f"- 截图：{result.get('screenshot')}")
    lines.append(f"- 取证链接：{result.get('url')}")
    lines.append(f"- 采集时间：{result.get('captured_at')}")
    lines.append("")
    lines.append("| 型号 | 部件 | 本地价 | 币种 | 截图 |")
    lines.append("|------|------|--------|------|------|")
    for r in result.get("rows", []):
        cells = r.get("cells", [])
        model = cells[0] if cells else (result.get("model") or "")
        part = cells[1] if len(cells) > 1 else (result.get("part") or "")
        price = r.get("price")
        lines.append(f"| {model} | {part} | {price} | {result.get('currency')} | [截图]({result.get('screenshot')}) |")
    lines.append("")
    lines.append(f"> 备注：{result.get('notes','')}")
    lines.append("")
    lines.append("> 据官网公开页面整理，仅供内部竞品比价参考。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--country")
    ap.add_argument("--model")
    ap.add_argument("--part")
    ap.add_argument("--category", default="phone")
    ap.add_argument("--csv")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.csv:
        import csv
        results = []
        with open(args.csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                results.append(asyncio.run(fetch_price(
                    row["brand"], row["country"], row.get("model"), row.get("part"))))
        md = "\n\n".join(render_markdown(r) for r in results)
    else:
        if not (args.brand and args.country):
            sys.stderr.write("ERROR: 需提供 --brand 与 --country，或 --csv\n")
            sys.exit(1)
        result = asyncio.run(fetch_price(args.brand, args.country, args.model, args.part, args.category))
        md = render_markdown(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.out:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"report -> {args.out}")


if __name__ == "__main__":
    main()
