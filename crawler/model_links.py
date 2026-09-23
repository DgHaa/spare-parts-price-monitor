"""crawler/model_links.py —— "一机一链"核心模块：为每台机型生成指向该机型实际数据的链接并实测校验。

被两处复用：
  · crawler/run.py     每次抓取结束后自动回填/校验该品牌该国的机型级链接
  · tools/harvest_model_links.py  独立 CLI（全量回填、审计、修复）

硬约束（用户要求：所有抓取对应的链接都要对应到本机实际的链接）：
  1. 只把**实测请求过、且内容里能定位到该机型**的链接标记 verified=1；
  2. 官网确实不提供更细粒度时，如实标注 kind + locator（说明这条链接里怎么定位到本机型），
     绝不为凑"机型级"而拼接猜测 URL（历史教训：support.apple.com/<slug> 实测 29/29 全为软 404）；
  3. 校验不通过 -> verified=0，并把该机型快照的 source_url_kind 标为 brand_entry，
     前端必须显示"非本机型精确链接"。

各品牌形态（均由 tools/probe_*.py 实测得出，见 output/*_probe.json）：
  vivo    model_api            GET  www.vivo.com/{cc}/support/queryPriceByProductId?id={data-id}
  xiaomi  model_api            GET  api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id={id}&callback=cb
  oppo    model_api            GET  sgp-sow-cms.oppo.com/...GetPartPrice?...&productModel={机型名}
  apple   category_api_locator GET  support.apple.com/ols/api/pricing/.../pricing-estimate?locale=..&parent_tag_id=TAG_1754518739895
                               （Apple 只有 iPhone 根级接口：族级/单机型查询均 404；
                                 但本机型全部服务价就在该响应内，locator 给出精确 JSON 路径）
  samsung model_text_fragment  {整表页}#:~:text={机型名}（浏览器文本片段直接滚动+高亮到本机型行）
"""
import asyncio
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import db  # noqa: E402  取证链接类型的单一词汇表来源

DB = ROOT / "spare_parts.db"
OUT = ROOT / "output"

APPLE_API = "https://support.apple.com/ols/api/pricing/products/services/pricing-estimate"
APPLE_ROOT_TAG = "TAG_1754518739895"          # iPhone 根产品（repair 页加载时实测使用）
APPLE_LOCALE = {"de": "de-de", "jp": "ja-jp", "ae": "en-ae", "my": "en-my",
                "cn": "zh-cn", "tr": "tr-tr", "mx": "es-mx"}
MI_CLASS_LIST = "https://api2.service.order.mi.com/repair_price/shop_class_info?keyword=&callback=CALLBACK"
MI_PRICE = "https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id={cid}&callback=cb"

# 取证链接类型常量：**绑定 db 的单一来源**，不再在此另写一份字面量。
# 本模块走直接 SQL 写入（绕过 db.upsert_model 的写入口断言），若在此另定义一份，
# 一旦与 db 漂移就会静默写出非法 kind —— 绑成一处可让漂移在导入期就失败，
# 而不是等 tools/verify_quarterly_run.py 事后巡检才发现。
KIND_MODEL_API = db.MODEL_URL_KIND_API
KIND_CAT_API = db.MODEL_URL_KIND_CATEGORY_API
KIND_TEXT_FRAG = db.MODEL_URL_KIND_TEXT_FRAGMENT
KIND_BRAND = db.MODEL_URL_KIND_BRAND_ENTRY
# 防御：防止 db 侧改名后这里静默变成 None
assert None not in (KIND_MODEL_API, KIND_CAT_API, KIND_TEXT_FRAG, KIND_BRAND), \
    "db.MODEL_URL_KIND_* 常量解析失败，检查 db.py 是否改名"


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def norm(s):
    """机型名归一化：小写、只留字母数字与汉字（跨语言/跨空格比较用）。

    小米官网机型清单有时用英文 'Xiaomi'、有时用中文 '小米'，库内也混用，
    统一把 '小米' 视为 'xiaomi'，避免同一机型因品牌 token 语言不同而漏配。
    """
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower().replace("小米", "xiaomi"))


# 库内小米机型名是完整 SKU（含内存/颜色/容量），官网机型清单只到机型级，
# 因此允许"官网名是库内名前缀"的匹配；但必须确认多出来的尾巴确实是规格/颜色，
# 否则 "Xiaomi 12" 会错配到 "Xiaomi 12S ..."（尾巴以字母开头一律拒绝）。
_SPEC_TAIL = re.compile(r"^(?:\d+(?:gb|g|tb|t)|[\u4e00-\u9fff])")


def match_official_name(m, nmap):
    """把库内机型匹配到官网机型清单条目：全名 → base_model → 受限前缀。

    返回 (hit, matched_by)；hit 为 nmap 的值，未命中返回 (None, None)。
    """
    for key, tag in ((m.get("name"), "name"), (m.get("base_model"), "base_model")):
        if key and norm(key) in nmap:
            return nmap[norm(key)], tag
    nn = norm(m.get("name") or "")
    best = None
    for k in nmap:
        if len(k) >= 6 and nn.startswith(k) and _SPEC_TAIL.match(nn[len(k):]):
            if best is None or len(k) > len(best):
                best = k
    return (nmap[best], "prefix+spec_tail") if best else (None, None)


# ---------------------------------------------------------------- DB 读写
def load_models(brand, country=None):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT m.id, m.name, m.model_key, m.country_code, m.source_url, "
           "m.base_model, m.spec, m.color "
           "FROM models m JOIN brands b ON b.id=m.brand_id WHERE b.name=?")
    args = [brand]
    if country:
        sql += " AND m.country_code=?"
        args.append(country)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY m.country_code, m.name", args)]
    conn.close()
    return rows


def countries_of(brand):
    conn = sqlite3.connect(DB)
    cs = [r[0] for r in conn.execute(
        "SELECT DISTINCT m.country_code FROM models m JOIN brands b ON b.id=m.brand_id "
        "WHERE b.name=? ORDER BY 1", (brand,))]
    conn.close()
    return cs


def write_links(records, dry_run=False):
    """写 models 的机型级链接字段；verified=1 时把该机型全部快照的 source_url 同步为机型级链接。"""
    if dry_run:
        return 0, 0
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    n_m = n_s = 0
    ts = now_iso()
    for r in records:
        # 写边界校验：本模块走直接 SQL，绕过 db.upsert_model 的断言，故在此显式补上。
        # ⚠️ 两列是**两套**词汇表（source_url_kind 多一个 reference_cn），所以分别按
        # 各自的合法集校验 —— 不能因为"同一个 r['kind'] 写两处"就假定它们同义。
        model_kind = db.validate_model_url_kind(r["kind"])
        snap_kind = db.validate_snapshot_url_kind(r["kind"])
        c.execute("""UPDATE models SET model_url=?, model_url_kind=?, model_url_locator=?,
                                       model_url_verified=?, model_url_checked_at=?, model_page_url=?
                     WHERE id=?""",
                  (r["model_url"], model_kind, json.dumps(r["locator"], ensure_ascii=False),
                   1 if r["verified"] else 0, ts, r.get("model_page_url"), r["model_id"]))
        n_m += c.rowcount
        if r["verified"]:
            c.execute("""UPDATE price_snapshots SET source_url=?, source_url_kind=?
                         WHERE part_id IN (SELECT id FROM parts WHERE model_id=?)""",
                      (r["model_url"], snap_kind, r["model_id"]))
            n_s += c.rowcount
        else:
            # 未落实/未通过校验：保留原 source_url，但如实标注为品牌入口级
            c.execute("""UPDATE price_snapshots SET source_url_kind=?
                         WHERE part_id IN (SELECT id FROM parts WHERE model_id=?)""",
                      (KIND_BRAND, r["model_id"]))
    conn.commit()
    conn.close()
    return n_m, n_s


# ---------------------------------------------------------------- 取数助手
async def page_fetch(page, url):
    """在当前页上下文里 fetch（同源场景：vivo / apple）。"""
    return await page.evaluate("""async (u) => {
        try { const r = await fetch(u, {credentials:'include'});
              const t = await r.text();
              return {status: r.status, text: t}; }
        catch(e) { return {status: 0, error: String(e).slice(0,160)}; }
    }""", url)


async def jsonp_fetch(page, url_with_CALLBACK, timeout_ms=20000):
    """JSONP 注入（跨域场景：小米 api2.service.order.mi.com）。URL 里用 CALLBACK 占位。"""
    return await page.evaluate("""async (args) => {
        const [u, to] = args;
        return await new Promise((resolve) => {
            const cb = '__hv' + Math.random().toString(36).slice(2, 9);
            let done = false;
            const finish = (v) => { if (done) return; done = true;
                try { delete window[cb]; s.remove(); } catch(e) {} resolve(v); };
            window[cb] = (d) => finish({ok: true, data: d});
            const s = document.createElement('script');
            s.src = u.replace('CALLBACK', cb);
            s.onerror = () => finish({ok: false, error: 'script load error'});
            document.head.appendChild(s);
            setTimeout(() => finish({ok: false, error: 'timeout'}), to);
        });
    }""", [url_with_CALLBACK, timeout_ms])


async def ctx_get(context, url, referer=None, timeout_ms=15000):
    """浏览器 APIRequestContext（无 CORS 限制，走同一代理与 cookie；用于 OPPO 跨域 API）。"""
    headers = {"Referer": referer} if referer else {}
    try:
        resp = await context.request.get(url, headers=headers, timeout=timeout_ms)
        return {"status": resp.status, "text": await resp.text()}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:140]}"}


def direct_get(url, referer=None, timeout=12):
    """直连 HTTP GET（同步）。用于 OPPO GetPartPrice 这类公开、无 CORS 的接口——
    本环境浏览器代理连不到其新加坡 CDN（请求挂起 30s 超时），直连 curl 却能秒回，
    故验证环节改用直连，避免把“代理连不上”误判成“官网无数据”。"""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": referer or ""}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "text": resp.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:140]}"}


def _enc_oppo_url(u):
    """OPPO GetPartPrice 的 productModel 参数含中文/空格，必须 URL 编码，否则 urllib
    会直接抛 InvalidURL（连请求都发不出去，导致全部机型“未通过”）。先 unquote 再 quote，
    保证无论库里存的是原始还是已编码形式，最终都得到单一编码、可正确请求的 URL。"""
    base, sep, query = u.partition("?")
    if not sep:
        return u
    parts = []
    for seg in query.split("&"):
        k, _, v = seg.partition("=")
        if k == "productModel":
            v = urllib.parse.quote(urllib.parse.unquote(v), safe="")
        parts.append(f"{k}={v}")
    return base + "?" + "&".join(parts)


def _unmatched(m, reason, page_url):
    return {"model_id": m["id"], "name": m["name"], "model_url": None, "kind": None,
            "locator": {"reason": reason}, "verified": 0,
            "model_page_url": page_url, "note": "unmatched"}


# ---------------------------------------------------------------- vivo
async def harvest_vivo(browser, cc, models, max_verify=0):
    page_url = f"https://www.vivo.com/{cc}/support/accessory"
    page = await browser.new_page(locale="")
    out, diag = [], {"page_url": page_url}
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(14000)
        trig = page.locator("#boxSelectModel, .box-select-model").first
        if await trig.count():
            await trig.evaluate("el => el.click()")
            await page.wait_for_timeout(2500)
        opts = await page.evaluate(
            """() => [...document.querySelectorAll('li.select-model-item')].map(e => ({
                 label: (e.innerText || '').trim(),
                 id: e.getAttribute('data-id') || e.dataset.id || null}))""")
        diag["n_options"] = len(opts)
        diag["sample_options"] = opts[:5]
        id_map = {norm(o["label"]): o for o in opts if o.get("id") and o.get("label")}
        diag["n_with_id"] = len(id_map)

        cache, n_ver = {}, 0
        for m in models:
            o = id_map.get(norm(m["name"]))
            if not o:
                out.append(_unmatched(m, "机型下拉中未找到同名选项", page_url))
                continue
            mid = o["id"]
            url = f"https://www.vivo.com/{cc}/support/queryPriceByProductId?id={mid}"
            if mid not in cache and (max_verify == 0 or n_ver < max_verify):
                r = await page_fetch(page, url)
                ok, detail = False, {"status": r.get("status")}
                if r.get("status") == 200:
                    try:
                        j = json.loads(r["text"])
                        lst = (((j.get("data") or {}).get("sparePartVO") or {})
                               .get("sparePartsVoList")) or []
                        ok = bool(j.get("success")) and len(lst) > 0
                        detail["n_parts"] = len(lst)
                    except Exception as e:
                        detail["parse_error"] = str(e)[:90]
                else:
                    detail["error"] = r.get("error")
                cache[mid] = (ok, detail)
                n_ver += 1
            ok, detail = cache.get(mid, (False, {"skipped": True}))
            out.append({"model_id": m["id"], "name": m["name"], "model_url": url,
                        "kind": KIND_MODEL_API,
                        "locator": {"param": "id", "value": mid, "dom_label": o["label"],
                                    "note": "机型 id 取自官网机型下拉的 data-id；该接口只返回本机型备件价",
                                    **detail},
                        "verified": 1 if ok else 0, "model_page_url": page_url})
    finally:
        await page.close()
    return out, diag


# ---------------------------------------------------------------- xiaomi
def find_class_map(obj, acc=None, depth=0):
    """在任意结构里递归找 {机型名 -> class_id}。"""
    acc = acc if acc is not None else {}
    if depth > 8:
        return acc
    if isinstance(obj, dict):
        name = None
        for k in ("name", "class_name", "className", "title", "label", "goods_name", "product_name"):
            if isinstance(obj.get(k), str) and obj[k].strip():
                name = obj[k].strip()
                break
        cid = None
        for k in ("class_id", "classId", "id", "goods_id", "goodsId"):
            v = obj.get(k)
            if isinstance(v, (int, str)) and str(v).strip().isdigit():
                cid = str(v).strip()
                break
        if name and cid:
            acc[name] = cid
        for v in obj.values():
            find_class_map(v, acc, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            find_class_map(v, acc, depth + 1)
    return acc


async def harvest_xiaomi(browser, cc, models, max_verify=0):
    page_url = "https://www.mi.com/service/materialprice"
    page = await browser.new_page(locale="zh-CN")
    out, diag = [], {"page_url": page_url}
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(9000)
        r = await jsonp_fetch(page, MI_CLASS_LIST)
        diag["class_list_ok"] = r.get("ok")
        if not r.get("ok"):
            diag["class_list_error"] = r.get("error")
            return out, diag
        OUT.mkdir(exist_ok=True)
        (OUT / "xiaomi_class_info.json").write_text(
            json.dumps(r["data"], ensure_ascii=False, indent=2), encoding="utf-8")
        cmap = find_class_map(r["data"])
        diag["n_class_entries"] = len(cmap)
        nmap = {norm(k): (k, v) for k, v in cmap.items()}

        n_ver = 0
        matched_by_stat = {}
        for m in models:
            hit, matched_by = match_official_name(m, nmap)
            matched_by_stat[matched_by or "none"] = matched_by_stat.get(matched_by or "none", 0) + 1
            if not hit:
                out.append(_unmatched(m, "官网机型清单接口 shop_class_info 中未找到对应机型"
                                         "（已试全名/基础机型名/规格前缀三级匹配）", page_url))
                continue
            label, cid = hit
            url = MI_PRICE.format(cid=cid)
            ok, detail = False, {"skipped": True}
            if max_verify == 0 or n_ver < max_verify:
                rr = await jsonp_fetch(page, url.replace("callback=cb", "callback=CALLBACK"))
                n_ver += 1
                if rr.get("ok"):
                    d = rr["data"] if isinstance(rr["data"], dict) else {}
                    body = d.get("data") if isinstance(d.get("data"), (dict, list)) else d
                    blob = json.dumps(body, ensure_ascii=False)
                    # 小米该接口成功码为 200（非 0）
                    detail = {"code": d.get("code"), "payload_len": len(blob)}
                    ok = (str(d.get("code")) in ("0", "200")) and len(blob) > 40
                else:
                    detail = {"error": rr.get("error")}
            out.append({"model_id": m["id"], "name": m["name"], "model_url": url,
                        "kind": KIND_MODEL_API,
                        "locator": {"param": "class_id", "value": cid, "official_label": label,
                                    "matched_by": matched_by,
                                    "note": "class_id 取自官网机型清单接口 shop_class_info；"
                                            "该接口只返回本机型物料价"
                                            + ("（库内为完整 SKU 名，已归一到官网机型名 "
                                               f"“{label}”）" if matched_by != "name" else ""),
                                    **detail},
                        "verified": 1 if ok else 0, "model_page_url": page_url})
        diag["matched_by"] = matched_by_stat
    finally:
        await page.close()
    return out, diag


# ---------------------------------------------------------------- apple
async def harvest_apple(browser, cc, models, max_verify=0):
    locale = APPLE_LOCALE.get(cc)
    repair = f"https://support.apple.com/{locale}/iphone/repair" if locale else None
    out, diag = [], {"locale": locale, "page_url": repair}
    if not locale:
        diag["error"] = f"未知语区映射: {cc}"
        return out, diag
    api = f"{APPLE_API}?locale={locale}&pricing_type=OOW&parent_tag_id={APPLE_ROOT_TAG}"
    page = await browser.new_page(locale=locale)
    try:
        await page.goto(repair, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        r = await page_fetch(page, api)
        diag["api"] = api
        diag["api_status"] = r.get("status")
        if r.get("status") != 200:
            diag["error"] = (r.get("text") or r.get("error") or "")[:200]
            return out, diag
        data = json.loads(r["text"])
        index = {}
        for fam in data.get("products", []):
            for ch in (fam.get("childrenProducts") or []):
                svc = ch.get("services") or []
                for nm in (ch.get("product_loc_title"), ch.get("product_eng_title")):
                    if nm:
                        index[norm(nm)] = {
                            "family": fam.get("product_loc_title"),
                            "family_tag": fam.get("product_tag_id"),
                            "model_title": ch.get("product_loc_title"),
                            "model_tag": ch.get("product_tag_id"),
                            "n_services": len(svc),
                            "services": [{"label": s.get("serviceLabel"), "price": s.get("price")}
                                         for s in svc][:8]}
        diag["n_models_in_api"] = len({v["model_tag"] for v in index.values()})
        for m in models:
            hit = index.get(norm(m["name"]))
            if not hit:
                out.append(_unmatched(m, "官方定价接口返回中未找到该机型", repair))
                continue
            out.append({
                "model_id": m["id"], "name": m["name"], "model_url": api, "kind": KIND_CAT_API,
                "locator": {
                    "json_path": f"products[product_tag_id={hit['family_tag']}]"
                                 f".childrenProducts[product_tag_id={hit['model_tag']}].services",
                    "family": hit["family"], "model_tag": hit["model_tag"],
                    "model_title": hit["model_title"], "n_services": hit["n_services"],
                    "services": hit["services"],
                    "note": "Apple 官网仅提供 iPhone 根级定价接口（族级/单机型查询均 404，"
                            "机型 slug 页为软 404）；本机型全部服务价即在此响应内，按 json_path 定位",
                    "picker_hint": f"官网页面在 device 下拉选『{hit['family']}』、"
                                   f"model 下拉选『{hit['model_title']}』"},
                "verified": 1 if hit["n_services"] > 0 else 0, "model_page_url": repair})
    finally:
        await page.close()
    return out, diag


# ---------------------------------------------------------------- samsung
async def harvest_samsung(browser, cc, models, max_verify=0):
    table_url = models[0]["source_url"] if models else None
    out, diag = [], {"table_url": table_url}
    if not table_url:
        return out, diag
    page = await browser.new_page(locale="")
    try:
        await page.goto(table_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(8000)
        # 必须用 textContent：三星价表在折叠面板里，innerText 取不到（实测结论）
        text = await page.evaluate("() => document.body.textContent || ''")
        diag["page_text_len"] = len(text)
        ntext = norm(text)
        for m in models:
            frag = urllib.parse.quote(m["name"], safe="")
            url = f"{table_url}#:~:text={frag}"
            present = norm(m["name"]) in ntext
            out.append({
                "model_id": m["id"], "name": m["name"], "model_url": url, "kind": KIND_TEXT_FRAG,
                "locator": {"text_fragment": m["name"], "model_present_in_page": present,
                            "note": "三星官网为整表静态页（无机型级页面/接口）；"
                                    "链接用浏览器文本片段直接滚动并高亮到本机型所在行"},
                "verified": 1 if present else 0, "model_page_url": table_url})
    finally:
        await page.close()
    return out, diag


# ---------------------------------------------------------------- oppo
async def harvest_oppo(browser, cc, models, max_verify=0):
    """OPPO 的 models.source_url 已是机型级 API（URL 自带 productModel），逐条直连实测校验。

    注意：本环境浏览器代理连不到 OPPO 新加坡 CDN（请求挂起 30s 超时），而接口本身公开、无
    CORS，直连 curl 能秒回真实数据。故校验环节用 direct_get（直连），既快又准，避免把
    “代理连不上”误判成“官网无数据”。"""
    out, diag = [], {}
    referer = f"https://www.oppo.com/{cc}/"
    n_ver = 0
    for m in models:
        raw = m["source_url"] or ""
        if "GetPartPrice" not in raw:
            out.append(_unmatched(m, "models.source_url 非机型级 API", referer))
            continue
        # productModel 含中文/空格，必须编码否则 urllib 直接 InvalidURL（连请求都发不出）
        url = _enc_oppo_url(raw)
        pm = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("productModel", [""])[0]
        ok = False
        detail = {"param": "productModel", "value": pm,
                  "note": "URL 自带机型名，该接口只返回本机型备件价"}
        if max_verify == 0 or n_ver < max_verify:
            for attempt in range(1, 3):
                r = direct_get(url, referer)
                n_ver += 1
                detail["status"] = r.get("status")
                detail["attempt"] = attempt
                if r.get("status") == 200:
                    try:
                        j = json.loads(r["text"])
                        d = j.get("data") if isinstance(j, dict) else None
                        blob = json.dumps(d, ensure_ascii=False) if d is not None else ""
                        detail["payload_len"] = len(blob)
                        if bool(d) and len(blob) > 40:
                            ok = True
                            break
                    except Exception as e:
                        detail["parse_error"] = str(e)[:90]
                elif r.get("status") == 429:
                    detail["error"] = "429 频率限制"
                    await asyncio.sleep(5)          # 限流避让
                else:
                    detail["error"] = (r.get("error") or (r.get("text") or "")[:120])
                await asyncio.sleep(0.5)            # 重试/礼貌间隔
        out.append({"model_id": m["id"], "name": m["name"], "model_url": url,
                    "kind": KIND_MODEL_API,
                    "locator": detail,
                    "verified": 1 if ok else 0, "model_page_url": referer})
        await asyncio.sleep(0.2)  # 机型间礼貌限速
    return out, diag


HARVEST = {"vivo": harvest_vivo, "xiaomi": harvest_xiaomi, "apple": harvest_apple,
           "samsung": harvest_samsung, "oppo": harvest_oppo}


# ---------------------------------------------------------------- 对外入口
async def backfill_links(browser, brand, country=None, max_verify=0, dry_run=False, verbose=True):
    """为 brand[/country] 的全部机型生成并校验机型级链接，写库并返回报告。

    crawler/run.py 在每个品牌/国家抓取成功后调用本函数，使"抓取产出的取证链接"
    直接就是机型级链接；tools/harvest_model_links.py 用同一函数做全量回填与审计。
    """
    fn = HARVEST.get(brand)
    if not fn:
        return {"skipped": f"无 {brand} 机型链接采集器"}
    report = {}
    for cc in ([country] if country else countries_of(brand)):
        models = load_models(brand, cc)
        if not models:
            continue
        if verbose:
            print(f"  [links] {brand}/{cc} 机型 {len(models)} —— 生成并实测机型级链接…", flush=True)
        try:
            recs, diag = await fn(browser, cc, models, max_verify)
        except Exception as e:
            report[f"{brand}/{cc}"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            if verbose:
                print(f"  [links][ERROR] {brand}/{cc}: {type(e).__name__}: {e}", flush=True)
            continue
        ok = sum(1 for r in recs if r["verified"])
        have = sum(1 for r in recs if r.get("model_url"))
        n_m, n_s = write_links(recs, dry_run)
        report[f"{brand}/{cc}"] = {
            "n_models": len(models), "n_with_url": have, "n_verified": ok,
            "kind": recs[0]["kind"] if recs else None,
            "models_updated": n_m, "snapshots_updated": n_s, "diag": diag,
            "unmatched": [r["name"] for r in recs if not r.get("model_url")][:30],
            "unverified": [r["name"] for r in recs if r.get("model_url") and not r["verified"]][:30],
            "sample": [{"name": r["name"], "url": r["model_url"], "locator": r["locator"]}
                       for r in recs[:2]]}
        if verbose:
            print(f"  [links] {brand}/{cc} 链接 {have}/{len(models)}，实测通过 {ok}；"
                  f"写库 models={n_m} snapshots={n_s}"
                  + ("  [dry-run]" if dry_run else ""), flush=True)
    return report


def save_report(report, path=None):
    """合并写入 output/model_links_report.json（审计用）。"""
    OUT.mkdir(exist_ok=True)
    f = Path(path) if path else (OUT / "model_links_report.json")
    prev = {}
    if f.exists():
        try:
            prev = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    prev.update(report)
    f.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
    return f
