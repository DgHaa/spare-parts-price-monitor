"""crawler/samsung_api.py - 三星备件价「服务端 HTTP」直采（无需浏览器/Playwright）。

背景：三星各国家维修估价器是 SPA/交互式页面，无静态价表，无头浏览器抽取 0 机型 → skipped。
但底层价格均由公开 HTTP 接口提供，可直接服务端拉取，稳定且无需 Chromium：

  - 德国(de)：seg.apix.de/smart-repair/v3-graph/ REST
      DeviceType/GetAll -> Series/GetAll/{typeGuid} -> Model/GetAllCodes/{seriesGuid}
      -> Model/{modelGuid}（repairCosts 含各部件 Preis）
  - 马来西亚(my)：Azure 估价 API（my-repair-cost-estimator-api-*-prd.azurewebsites.net）
      /api/all（过滤 subcategoriesId=手机）-> /api/symptoms?productId= -> POST /api/estimate
  - 阿联酋(ae)：Gulf 维修页服务端渲染完整价表（无独立 API，无头被区域门控挡成 0 表格）
      GET /ae/support/repair-prices/ HTML -> 解析内嵌 JSON models 数组(replacement=屏幕 /
      battery=电池 / backCover=后盖) + 折叠屏 HTML 表(Fold/Flip 主屏/外屏)。无需浏览器。

本模块既可被 crawler/run.py 的 crawl_brand_country 在 mode=="samsung_api" 时调用，
也可独立运行：python -m crawler.samsung_api --brand samsung --country de
落库逻辑与 crawler/run.py 的 write_rows 保持一致（同表、同取证字段、同汇率折算）。

注意：此模块只依赖标准库 + db.py，不 import playwright，故在缺失浏览器的环境也能跑通并入库。
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from html import unescape as _unescape
from pathlib import Path

# 项目根（便于 import db）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db import (init_db, fetch_rates, this_quarter, upsert_brand, upsert_country,  # noqa: E402
               upsert_model, upsert_part, insert_snapshot, get_rate_meta,
               normalize_base_model, extract_spec, extract_color, classify_tier,
               log_run, add_issue, model_already_captured)

SKILL_KB = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/references/kb")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Accept": "application/json"}


# ---------------- 通用 HTTP ----------------
def _get_json(url, timeout=25, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"GET {url} 失败: {last}")


def _post_json(url, body, timeout=25, retries=3):
    last = None
    data = json.dumps(body).encode("utf-8")
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers={
                **UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"POST {url} 失败: {last}")


# ---------------- 部件名归一（德/英 -> 中文标准件） ----------------
def _map_de_part(damage_type):
    t = (damage_type or "").lower()
    if "display" in t or "bildschirm" in t:
        return "屏幕"
    if "akku" in t:
        return "电池"
    if "rückseite" in t or "back" in t or "gehäuse" in t or "rumpf" in t:
        return "后盖"
    if "kamera" in t:
        return "摄像头"
    if "anschluss" in t or "taste" in t or "laden" in t or "port" in t:
        return "充电口"
    if "falt" in t:
        return "折叠机构"
    return None  # 保修/软件/升级等非备件价，跳过


def _map_my_part(symptom_name):
    t = (symptom_name or "").lower()
    if "battery" in t:
        return "电池"
    if "backglass" in t or "back glass" in t or "rear" in t:
        return "后盖"
    if "screen" in t or "display" in t or "lcd" in t:
        return "屏幕"
    if "camera" in t:
        return "摄像头"
    if "charge" in t or "port" in t or "usb" in t:
        return "充电口"
    return None  # 通话/重启/无法开机/进水/软件等非备件价，跳过


# ---------------- 德国：seg.apix.de v3-graph REST ----------------
def fetch_de(rec):
    api = rec["query"]["api"]
    base = api["base"].rstrip("/") + "/"
    out = []
    types = _get_json(base + "DeviceType/GetAll")["data"]
    type_guid = None
    for t in types:
        if (t.get("name") or "").lower() == (api.get("phone_type_name", "Smartphones")).lower():
            type_guid = t["guid"]
            break
    if not type_guid:
        raise RuntimeError("DE 未找到 Smartphones 设备类型")
    series = _get_json(base + f"Series/GetAll/{type_guid}")["data"]
    for s in series:
        sguid = s["guid"]
        try:
            models = _get_json(base + f"Model/GetAllCodes/{sguid}")["data"]
        except Exception:  # noqa: BLE001
            continue
        for m in models:
            mguid = m.get("guid")
            mname = m.get("name")
            if not mguid or not mname:
                continue
            try:
                data = _get_json(base + f"Model/{mguid}")["data"]
            except Exception:  # noqa: BLE001
                continue
            rows = []
            for rc in data.get("repairCosts", []):
                dt = rc.get("damageType") or rc.get("groupName") or ""
                part = _map_de_part(dt)
                if not part:
                    continue
                price = None
                for spec in rc.get("repairCostSpecs", []):
                    if (spec.get("name") or "").lower() == "preis":
                        v = spec.get("value")
                        if v not in (None, ""):
                            try:
                                price = float(str(v).replace(",", ""))
                            except ValueError:
                                price = None
                        break
                if price is None or price <= 0:
                    continue
                rows.append({"part": part, "price": price})
            if rows:
                out.append((mname, rows, rec.get("entry", {}).get("expect_url")
                            or rec.get("entry", {}).get("url", "")))
    return out


# ---------------- 马来西亚：Azure 估价 API ----------------
def fetch_my(rec):
    api = rec["query"]["api"]
    base = api["base"].rstrip("/")
    all_ep = api["all_endpoint"]
    sym_ep = api["symptoms_endpoint"]
    est_ep = api["estimate_endpoint"]
    phone_sub = api.get("phone_subcategory_id", 1)
    out = []
    allp = _get_json(base + all_ep)
    phones = [p for p in allp if p.get("subcategoriesId") == phone_sub]
    for p in phones:
        pid = p["id"]
        pname = p.get("name")
        if not pname:
            continue
        try:
            symptoms = _get_json(base + sym_ep + str(pid))
        except Exception:  # noqa: BLE001
            continue
        rows = []
        for sym in symptoms:
            sid = sym.get("id")
            sname = sym.get("name") or ""
            part = _map_my_part(sname)
            if not part:
                continue
            try:
                est = _post_json(base + est_ep, {"productId": pid, "symptomIds": [sid]})
            except Exception:  # noqa: BLE001
                continue
            price = None
            if isinstance(est, list) and est:
                rp = (est[0] or {}).get("repairParts", [{}])[0]
                price = rp.get("minPrice") or rp.get("maxPrice")
            if price is None or price <= 0:
                continue
            rows.append({"part": part, "price": float(price)})
            time.sleep(0.05)  # 限速礼貌
        if rows:
            out.append((pname, rows, rec.get("entry", {}).get("expect_url")
                        or rec.get("entry", {}).get("url", "")))
    return out


# ---------------- 阿联酋(AE)：服务端 HTML 解析（Gulf 维修页，无独立 API） ----------------
_AE_URL = "https://www.samsung.com/ae/support/repair-prices/"


def _get_html(url, timeout=25, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"GET {url} 失败: {last}")


def _ae_price(s):
    """从 'AED 1,845' / '-' 中取出浮点价；缺失/占位返回 None。"""
    if not s:
        return None
    s = s.strip()
    if s in ("-", ""):
        return None
    m = re.search(r"AED\s*([\d,]+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _clean_cell(s):
    return re.sub(r"\s+", " ", _unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _ae_fld(bod, name):
    """从内嵌 JSON 对象体里取出某字段的引号值（无则空串）。"""
    mt = re.search(f'{name}:"([^"]*)"', bod)
    return mt.group(1) if mt else ""


def fetch_ae(rec):
    """服务端直采 AE Gulf 维修价表（无需浏览器，绕开无头区域门控）。

    数据源（均服务端渲染于 /ae/support/repair-prices/）：
      1) 内嵌 JSON `models:[{device, replacement, battery, backCover}]`：
         标准机型 device 以 'Galaxy' 开头 -> replacement=屏幕 / battery=电池 / backCover=后盖；
         折叠屏 device 形如 'Fold6 Main'/'Flip5 Front' -> 归一为 Galaxy Fold6，
         Main/Front 分别对应 屏幕(主屏)/屏幕(外屏)，并携带 battery/backCover。
      2) 折叠屏 HTML <table>（Front 外屏价仅此处有）-> 屏幕(外屏)。
    严格只收手机（Galaxy / Fold / Flip），过滤平板/手表/电视/充电座等非手机条目。
    返回 [(model_name, [{'part','price'}], detail_url), ...]。
    """
    html = _get_html(_AE_URL)
    phones = {}  # model -> {part: price}（首值优先，多源去重）

    def add(model, part, price):
        if price is None:
            return
        d = phones.setdefault(model, {})
        if part not in d:  # 多源/多段只取首个非缺失值
            d[part] = price

    # 1) 内嵌 JSON（跨多个 models 数组，按设备名合并）
    for block in re.findall(r"models:\s*\[([\s\S]*?)\]", html):
        for m in re.finditer(r'\{\s*device:"([^"]+)"([\s\S]*?)\}', block):
            dev, bod = m.group(1).strip(), m.group(2)
            repl = _ae_price(_ae_fld(bod, "replacement"))
            batt = _ae_price(_ae_fld(bod, "battery"))
            back = _ae_price(_ae_fld(bod, "backCover"))
            fm = re.match(r"^(Fold|Flip)(\d+)\s+(Main|Front)$", dev)
            if fm:  # 折叠屏：归一机型名 + 主屏/外屏
                base = f"Galaxy {fm.group(1)}{fm.group(2)}"
                add(base, "屏幕(主屏)" if fm.group(3) == "Main" else "屏幕(外屏)", repl)
                add(base, "电池", batt)
                add(base, "后盖", back)
                continue
            if not dev.startswith("Galaxy"):  # 非手机（平板/手表/电视/充电座等）
                continue
            add(dev, "屏幕", repl)
            add(dev, "电池", batt)
            add(dev, "后盖", back)
    # 2) 折叠屏 HTML 表（补 Front 外屏价；Main 已被 JSON 覆盖，首值优先自动去重）
    for t in re.findall(r"<table[\s\S]*?</table>", html, re.I):
        for row in re.findall(r"<tr[\s\S]*?</tr>", t, re.I):
            cells = [_clean_cell(c) for c in re.findall(r"<t[dh][\s\S]*?</t[dh]>", row, re.I)]
            if len(cells) < 2:
                continue
            fm = re.match(r"^(Fold|Flip)(\d+)\s+(Main|Front)$", cells[0])
            if not fm:
                continue
            price = _ae_price(cells[1])
            if price is None:
                continue
            base = f"Galaxy {fm.group(1)}{fm.group(2)}"
            add(base, "屏幕(主屏)" if fm.group(3) == "Main" else "屏幕(外屏)", price)
    out = [(model, [{"part": k, "price": v} for k, v in parts.items()], _AE_URL)
           for model, parts in phones.items()]
    return out


# ---------------- 落库（镜像 run.py write_rows） ----------------
def _write_model(brand, country, country_name, rec, model_name, rows, detail_url):
    if not rows:
        return 0
    quarter = this_quarter()
    bid = upsert_brand(brand, rec.get("query", {}).get("mode"))
    upsert_country(country, country_name, rec.get("currency", ""), rec.get("locale", ""))
    mid = upsert_model(bid, country, model_name, model_name, detail_url,
                       tier=classify_tier(model_name),
                       base_model=normalize_base_model(model_name),
                       spec=extract_spec(model_name),
                       color=extract_color(model_name))
    cur = rec.get("currency", "")
    rate, rate_source, rate_as_of = get_rate_meta(quarter, cur)
    n = 0
    for r in rows:
        part, price = r["part"], r["price"]
        if price is None:
            continue
        pid = upsert_part(mid, part, None)
        cny = price * rate if rate else None
        insert_snapshot(pid, quarter, price, cur, cny,
                        material_fee=None, labor_fee=None, source_url=detail_url,
                        tax_included=1,
                        labor_note="官网未单列人工费，仅提供含人工的总维修价（来源见取证链接）",
                        labor_source_url=detail_url, has_labor_split=0, is_seed=0,
                        rate_source=rate_source, rate_as_of=rate_as_of,
                        source_url_kind="brand_entry")
        n += 1
    return n


def load_rec(brand, country):
    kb = SKILL_KB / f"{brand}.json"
    data = json.loads(kb.read_text(encoding="utf-8"))
    recs = data.get("countries", {}).get(country, [])
    return recs[0] if recs else None


def crawl_and_write(brand, country, country_name, rec):
    """完整 worker：服务端拉取 -> 落库 -> 写 run_log / 异常入队。返回 (status, rows_total, reason)。"""
    quarter = this_quarter()
    init_db()
    fetch_rates(quarter)
    started = datetime.now().isoformat(timespec="seconds")
    rows_total = 0
    status = "success"
    reason = ""
    anomaly = 0
    try:
        api = rec.get("query", {}).get("api", {})
        atype = api.get("type")
        if atype == "rest_v3graph":
            models_rows = fetch_de(rec)
        elif atype == "azure_estimator":
            models_rows = fetch_my(rec)
        elif atype == "ae_static_table":
            models_rows = fetch_ae(rec)
        else:
            raise RuntimeError(f"未知 samsung_api 类型: {atype}")
        bid = upsert_brand(brand, rec.get("query", {}).get("mode"))
        for mname, rows, detail_url in models_rows:
            if model_already_captured(bid, country, mname, quarter):
                continue  # 断点续跑
            n = _write_model(brand, country, country_name, rec, mname, rows, detail_url)
            rows_total += n
        if rows_total == 0:
            status = "failed"
            anomaly = 1
            reason = "API 未返回任何有效备件价（端点/参数可能变更）"
    except Exception as e:  # noqa: BLE001
        status = "failed"
        anomaly = 1
        reason = f"运行时异常：{str(e)[:240]}"
    finally:
        finished = datetime.now().isoformat(timespec="seconds")
        log_run(brand, country, quarter, started, finished, status, rows_total,
                error_text=reason if status == "failed" else "",
                anomaly_flag=anomaly, anomaly_reason=reason)
        if anomaly:
            add_issue(brand, country, reason)
    return status, rows_total, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="samsung")
    ap.add_argument("--country", required=True)
    args = ap.parse_args()
    rec = load_rec(args.brand, args.country)
    if not rec:
        print(f"[skip] {args.brand}/{args.country} 无 KB 记录", flush=True)
        return
    if rec.get("status") in ("blocked", "unavailable"):
        print(f"[skip] {args.brand}/{args.country} 状态={rec['status']}", flush=True)
        return
    name = {"de": "德国", "my": "马来西亚", "ae": "阿联酋", "tr": "土耳其",
            "jp": "日本"}.get(args.country, args.country)
    status, n, reason = crawl_and_write(args.brand, args.country, name, rec)
    print(f"[{status}] {args.brand}/{args.country} 本季新增 {n} 条价"
          + (f" | {reason}" if reason else ""), flush=True)


if __name__ == "__main__":
    main()
