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
import urllib.parse
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
               log_run, add_issue, model_already_captured, guess_category)

# KB 位置：优先仓库内副本 references/kb（已随仓库同步），缺失时回退到 skill 目录
_REPO_KB = Path(__file__).resolve().parents[1] / "references" / "kb"
SKILL_KB = _REPO_KB if (_REPO_KB / "samsung.json").exists() else Path(
    r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/references/kb")
# 金额解析统一走 skill 的 normalize.parse_amount：各国 `.`/`,` 含义相反
# （德语 `488,99`=488.99，美式 `1,299`=1299），重复实现过 `replace(",","")`
# 导致德语小数逗号被当千分位吃掉、价格放大 100 倍。此处不再自行解析。
SKILL_SCRIPTS = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/scripts")
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))
_VENDOR = Path(__file__).resolve().parents[1] / "vendor"  # 仓库内副本优先
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
from normalize import parse_amount as _parse_amount  # noqa: E402  # 仅用于「本地化文本」金额
from normalize import parse_json_amount as _parse_json_amount  # noqa: E402
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


# ---------------- 机型名空白归一 / 官方重名条目去重 ----------------
_WS_RE = None


def _norm_ws(name):
    """把机型名里的**格式性空白**归一：NBSP/窄空格/制表/换行 → 普通空格，多空格并一个，去首尾。

    **只动空白，不动任何可见字符**（不删空格、不改大小写）——因此只在"官方把同一个名字
    写成不同空白"时才会把两条并成一条，绝不会把语义不同的机型并到一起。

    存在的意义：`models.model_key` 取官方原文名，官方源里同一台机器偶有 NBSP 版本
    （实测 `Galaxy Tab A9\\xa0(Wi-Fi) - SM-X110` 与 `Galaxy Tab A9 (Wi-Fi) - SM-X110`
    同 `modelCode=SM-X110N`、两条价表还不一样），直接用原文名做唯一键会写出
    **重复机型 + 冲突价格**，前端看起来就是同一台平板出现两行不同价。
    """
    global _WS_RE
    if _WS_RE is None:
        import re as _re
        _WS_RE = _re.compile(r"[\s\u00a0\u2007\u2009\u202f\u2002\u2003]+")
    return _WS_RE.sub(" ", (name or "")).strip()


def dedupe_model_rows(models_rows, brand="", country=""):
    """按 `_norm_ws(name)` 去掉**官方重名条目**，同名只保留一条。

    保留规则（确定性、不看价格、不被贵/便宜引导）：
      1. 优先保留"原文名本身就已归一"的那条（无 NBSP 等格式噪声）——它是规范记录；
      2. 否则保留**价行更多**的那条（信息更全）；
      3. 再否则保留 API 返回顺序里的第一条。

    丢弃的条目会打到 stderr（`[dupe] ...`），保证不静默丢数据。
    """
    best = {}
    order = []
    for item in models_rows:
        name = item[0]
        k = _norm_ws(name).casefold()
        cand = (0 if name == _norm_ws(name) else 1, -len(item[1] or []))
        if k not in best:
            best[k] = (cand, item)
            order.append(k)
        elif cand < best[k][0]:
            print(f"  [dupe] {brand}/{country} 官方重名条目归一后同名，"
                  f"丢弃 {best[k][1][0]!r}（保留 {name!r}，价表更全/命名更规范）", flush=True)
            best[k] = (cand, item)
        else:
            print(f"  [dupe] {brand}/{country} 官方重名条目归一后同名，"
                  f"丢弃 {name!r}（保留 {best[k][1][0]!r}）", flush=True)
    return [best[k][1] for k in order]


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
    # Galaxy Ring（subcategoriesId=10）专属：Cradle=充电盒、Ring=戒指本体。
    # 部件名与 tr/jp 的 Ring 表对齐（充电器 / 主机），便于跨区域比价。
    if "cradle" in t:
        return "充电器"
    if "ring" in t:
        return "主机"
    return None  # 通话/重启/无法开机/进水/软件等非备件价，跳过


# ---------------- 德国：seg.apix.de v3-graph REST ----------------
# 德站 apix 接口的设备类型 → 品类。接口实际返回 4 类：
#   Smartphones(2) / Tablets(3) / Wearables(4) / Notebooks(5)
# 按 2026-09-18 全品类口径收前三类；Notebooks 与小米"电脑办公只收平板"同理排除
# （笔记本不是手机备件比价的参照系，纳入只会淹没大盘）。
_DE_TYPE_CATEGORY = {"smartphones": "phone", "tablets": "tablet", "wearables": "wearable"}
_DE_TYPES_DEFAULT = ["Smartphones", "Tablets", "Wearables"]


def fetch_de(rec):
    """德站：DeviceType/GetAll → Series/GetAll → Model/GetAllCodes → Model/{guid}。

    2026-09-18 起**收多个设备类型**（原实现只取 Smartphones 一类）。
    """
    api = rec["query"]["api"]
    base = api["base"].rstrip("/") + "/"
    out = []
    types = _get_json(base + "DeviceType/GetAll")["data"]
    want = [str(w).lower() for w in (api.get("device_types") or _DE_TYPES_DEFAULT)]
    plan = []          # [(guid, category)]
    for t in types:
        nm = (t.get("name") or "").strip()
        if nm.lower() in want and t.get("guid") is not None:
            plan.append((t["guid"], _DE_TYPE_CATEGORY.get(nm.lower(), "phone")))
    if not plan:
        raise RuntimeError(f"DE 未找到任何目标设备类型（期望 {want}）")
    for type_guid, type_cat in plan:
        try:
            series = _get_json(base + f"Series/GetAll/{type_guid}")["data"]
        except Exception:  # noqa: BLE001
            continue
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
                                # 这里**故意**用 parse_amount 而非 parse_json_amount：
                                # value 是德国本地化的文本金额（千分位写 `.`，实测如
                                # '245'/'129'，若出现 '1.234' 意为 1234 欧）。JSON 语义
                                # 解析器会把 '1.234' 读成 1.234 —— 差 1000 倍。
                                price = _parse_amount(v)
                            break
                    if price is None or price <= 0:
                        continue
                    rows.append({"part": part, "price": price})
                if rows:
                    # 机型名能判品类就用机型名（Watch→watch / Buds→earbuds 比类型更细），
                    # 判成 phone 但类型本身不是手机时以类型兜底
                    cat = guess_category(mname)
                    if cat == "phone" and type_cat != "phone":
                        cat = type_cat
                    for r in rows:
                        r["category"] = cat
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
    # 大马站 /api/all 的 subcategoriesId：1=手机(79) / 2=电视(183) / 3=冰箱(57) /
    #   4=洗衣机(11) / 9=空调(44) / 10=Galaxy Ring(1)。
    # 按全品类口径只补 **10=Galaxy Ring**（穿戴）；电视/冰洗/空调不是手机备件比价的参照系，不收。
    subs = {phone_sub} | set(api.get("extra_subcategory_ids") or [])
    out = []
    allp = _get_json(base + all_ep)
    phones = [p for p in allp if p.get("subcategoriesId") in subs]
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
            for r in rows:
                r["category"] = guess_category(pname)
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
    # 来源是网页文本（含 'AED ' 前缀），保持 parse_amount 的语区推断；
    # 若写成 'AED 1.845'（阿联酋用 `.` 作千分位）也能正确读成 1845。
    return _parse_amount(m.group(1))


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

    ⚠️ 这里的"过滤"只有一个条件：`device` 必须**以 'Galaxy' 开头**。
    它挡掉的是电视/充电座这类不以 Galaxy 命名的条目；**Galaxy Tab / Galaxy Watch
    都以 Galaxy 开头，是会进来的**（实测 ae 库内 30 台平板即由此而来）。
    这正是全品类口径想要的：官方价表里出现的品类都收，只是每行标 `category` 分组比价。
    ⚠️ 因此"ae 库里没有手表"应理解为**官方该页没列手表**，不是被我们过滤掉了
    （与 my 站同理：官方不提供 ≠ 我们过滤）。
    返回 [(model_name, [{'part','price','category'}], detail_url), ...]。
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
            # 只挡不以 Galaxy 命名的条目（电视/充电座/配件等）。
            # 注意：Galaxy Tab / Galaxy Watch **会通过**——这是全品类口径的预期行为，别"修"掉。
            if not dev.startswith("Galaxy"):
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
    # 阿站价表里本来就含 Galaxy Tab 平板（30 台），按全品类口径保留并标品类
    out = [(model, [{"part": k, "price": v, "category": guess_category(model)}
                    for k, v in parts.items()], _AE_URL)
           for model, parts in phones.items()]
    return out


# ---------------- 土耳其(TR)：服务端 HTML 表解析（同 AE 套路） ----------------
_TR_URL = "https://www.samsung.com/tr/support/repair-prices/"

# 列名 → 统一部件名。土站把"屏幕"按维修方式拆成多列，逐列如实保留，
# 不合并（合并会掩盖"原厂模组 vs 经济屏"的价差，这正是比价要看的信息）。
_TR_COLS = {
    "Ekran - Modül Onarımı": "屏幕",            # 模组（原厂屏）维修
    "Ekran - Eko Onarım": "屏幕(Eko)",          # 经济屏
    "Ekran - Eko-Onarım": "屏幕(Eko)",          # 同上，表#2 少一个连字符
    "Çerçeve - Eko Ekran Onarımı": "屏幕(含边框Eko)",
    "Dış Ekran Onarımı": "屏幕(外屏)",          # 折叠屏外屏
    "Çerçeve Onarımı": "中框",
    # table#5 Galaxy Ring（穿戴）。2026-09-18 起按"官方价表里出现的品类全收"口径纳入，
    # 部件名与 JP 的 Ring 表保持同一套（主机/充电器），便于跨区域对齐。
    "Yüzük": "主机",              # 戒指本体
    "Şarj Kutusu": "充电器",        # 充电盒
}

_PHONE_HINT = re.compile(r"^Galaxy\s+(?![R|B][i|u]|Ring|Watch|Tab|Buds)", re.I)


def _tr_price(s):
    """'₺14.300' / '-' → float（土站用 `.` 作千分位，交给 normalize.parse_amount 判）。"""
    if not s:
        return None
    s = s.strip()
    if s in ("-", "", "—"):
        return None
    m = re.search(r"([\d][\d.,]*)", s)
    if not m:
        return None
    return _parse_amount(m.group(1))


def fetch_tr(rec):
    """服务端直采 TR 维修价表（无需浏览器）。

    数据源：https://www.samsung.com/tr/support/repair-prices/ 服务端渲染的 6 张 <table>。
      table#0~#3：机型级维修价（Model Kodu / Model Adı / 各"屏幕维修方式"列 / 中框），
                  表头按表略有差异（table#3 只有一列屏幕），故**按表头文字**映射列而非按下标。
      table#4   ：['Seri','Ücret'] = **系列级统一费用**（如 "Galaxy S Serisi ₺3.100"），
                  **不是机型级备件价** —— 旧 DOM 口径正是把这张表当成机型表抓，
                  于是库里出现"机型名=Galaxy S Serisi、部件名=Ücret"的垃圾行。必须排除。
                  （注意 #4 表头是 Seri/Ücret，不满足 Model Kodu/Model Adı，天然被过滤。）
      table#5   ：Galaxy Ring 穿戴（Model Kodu/Model Adı + Yüzük/Şarj Kutusu）。
                  2026-09-18 起按"官方价表里出现的品类全收"口径**纳入**（此前按只收手机排除）。

    型号命名用官方 'Model Adı'（如 'Galaxy S24 Ultra'），与其余区域一致；
    'Model Kodu'（SM-S928）记录在取证链接的锚点上以便回溯。
    返回 [(model_name, [{'part','price'}], detail_url), ...]。
    """
    html = _get_html(_TR_URL)
    phones = {}

    def add(model, part, price):
        if price is None:
            return
        d = phones.setdefault(model, {})
        if part not in d:            # 同机型多行/多表只取首个非缺失值
            d[part] = price

    for t in re.findall(r"<table[\s\S]*?</table>", html, re.I):
        rows = re.findall(r"<tr[\s\S]*?</tr>", t, re.I)
        if not rows:
            continue
        header = [_clean_cell(c) for c in re.findall(r"<t[dh][\s\S]*?</t[dh]>", rows[0], re.I)]
        if len(header) < 3 or header[0] != "Model Kodu" or header[1] != "Model Adı":
            continue  # 含 table#4(Seri/Ücret) 与 table#5(Yüzük/Şarj Kutusu)
        cols = [(i, _TR_COLS[h]) for i, h in enumerate(header) if h in _TR_COLS]
        if not cols:
            continue
        for row in rows[1:]:
            cells = [_clean_cell(c) for c in re.findall(r"<t[dh][\s\S]*?</t[dh]>", row, re.I)]
            if len(cells) < len(header):
                continue
            code, model = cells[0].strip(), cells[1].strip()
            if not model:
                continue
            # 折叠屏才把主屏叫"主屏"，直板机就叫"屏幕"（与 AE 的 屏幕(主屏)/屏幕(外屏) 口径对齐）
            fold = bool(re.search(r"\bZ\s*(Fold|Flip)\b", model, re.I))
            for idx, part in cols:
                name = part
                if part == "屏幕" and fold:
                    name = "屏幕(主屏)"
                add(model, name, _tr_price(cells[idx]))
    out = [(m, [{"part": k, "price": v, "category": guess_category(m)} for k, v in parts.items()],
            _TR_URL)
           for m, parts in phones.items() if parts]
    return out


# ---------------- 日本(JP)：服务端 HTML 表解析（同 TR 套路） ----------------
_JP_URL = ("https://www.samsung.com/jp/support/mobile-devices/"
           "please-tell-us-about-the-repair-cost-of-the-galaxy-device/")

# 列名 → 统一部件名。沿用库内 jp 既有命名，避免与历史数据对不上。
# 覆盖全部 6 张表：手机/平板/耳机/手表/Fit/Ring 各有自己的列。
# ⚠️ 手表表的「メイン基板交換」**没有容量括号**，与手机表的「メイン基板交換 （～256GB）」
#    是两个不同 key，不会互相覆盖——这正是全品类口径下必须保留的区别。
_JP_COLS = {
    "ディスプレイ交換": "屏幕",
    "バッテリー交換": "电池",
    "メイン基板交換 （～256GB）": "主板（～256GB）",
    "メイン基板交換 （512GB）": "主板（512GB）",
    "メイン基板交換 （1TB）": "主板（1TB）",
    "メイン基板交換": "主板",              # 手表
    "充電器交換": "充电器",                # 耳机(Buds) / 戒指(Ring)
    "イヤホン交換（片耳）": "耳机（单耳）",        # 耳机
    "イヤホン交換（両耳）": "耳机（双耳）",        # 耳机
    "バンド交換": "表带",                 # Galaxy Fit
    "本体交換": "主机",                  # Galaxy Fit / Galaxy Ring
}

# 一页 6 张表，官方自己的品类划分（按表索引定位，再用品类交叉校验）：
#   #0 手机 / #1 平板 Tab / #2 耳机 Buds / #3 手表 Watch / #4 Galaxy Fit / #5 Galaxy Ring
# 口径（2026-09-18 用户确认）：**官方价表里出现的品类全收**——平板、手表、耳机、手环、戒指都要，
# 与 OPPO 既有口径一致（OPPO 官方页本来就含 Pad/Watch/Buds/手环/智能电视）。
# 但跨品类比价无意义（"平板屏幕" ≠ "手机屏幕"），故每行都标 category，比价矩阵按品类分组。
# ⚠️ 不能靠表头区分品类：#1/#3 的列名与手机表几乎完全相同（ディスプレイ交換/バッテリー交換/メイン基板交換）。
# ⚠️ 也不能写 `\bWatch\b`：Galaxy Watch4 的 h 与 4 都是词字符、中间无单词边界（2026-09-18 实测漏网）。
_JP_TABLE_CATEGORY = ["phone", "tablet", "earbuds", "watch", "wearable", "wearable"]

# 一格挤多个机型名的拆分锚点：日站把同价的不同版本写进同一格，如
# "Galaxy Tab S6 Lite (Wi-Fi) Galaxy Tab S6 Lite 2024 (Wi-Fi)" → 拆成两台。
_JP_SPLIT_ANCHOR = re.compile(r"(?<=[)\s])(?=(?:Galaxy|SM-)\s)")


def _split_jp_models(cell):
    """拆分挤在同一格里的多个机型名。

    日站把"同价的不同版本"写进同一格，例如：
      "Galaxy Tab S6 Lite (Wi-Fi) Galaxy Tab S6 Lite 2024 (Wi-Fi)"
    整串当机型名落库会得到一条谁都对不上的垃圾机型，必须拆成两台（共享同一行价格）。
    """
    parts = [p.strip() for p in _JP_SPLIT_ANCHOR.split(cell) if p.strip()]
    return parts or [cell.strip()]


def fetch_jp(rec):
    """服务端直采 JP 维修价表（无需浏览器）。

    数据源：_JP_URL 服务端渲染的 6 张 <table>，全部表头均以 'Model' 开头：
      table#0 手机   （ディスプレイ/バッテリー/メイン基板 ×3 容量档）
      table#1 平板   （Tab）      table#2 耳机（Buds）   table#3 手表（Watch）
      table#4 Galaxy Fit          table#5 Galaxy Ring
    **6 张全收**（2026-09-18 口径：官方价表里出现的品类都要）。

    价格形如 '¥14,630 ～'（'～' 表示"起价"，官网本身就是区间下界），'-' 表示无此规格。
    原 `samsung_repair_table`（Playwright 点选）已失效：KB 里的 entry URL 会跳到本页，
    随后 `Page.goto` 25s 超时 + 目标元素不可见 → 自动发现 0 机型（issue #57）。

    返回 [(model_name, [{'part','price','category'}], detail_url), ...]。
    """
    html = _get_html(_JP_URL)
    bucket = {}   # (category, model) -> {part: price}

    def add(cat, model, part, price):
        if price is None:
            return
        d = bucket.setdefault((cat, model), {})
        if part not in d:
            d[part] = price

    for ti, t in enumerate(re.findall(r"<table[\s\S]*?</table>", html, re.I)):
        rows = re.findall(r"<tr[\s\S]*?</tr>", t, re.I)
        if not rows:
            continue
        header = [_clean_cell(c) for c in re.findall(r"<t[dh][\s\S]*?</t[dh]>", rows[0], re.I)]
        if not header or header[0] != "Model":
            continue
        # 表头里可能夹带全角空格，统一归一后再映射
        cols = [(i, _JP_COLS[h.replace("\u3000", " ").strip()])
                for i, h in enumerate(header)
                if h.replace("\u3000", " ").strip() in _JP_COLS]
        if not cols:
            continue
        tbl_cat = _JP_TABLE_CATEGORY[ti] if ti < len(_JP_TABLE_CATEGORY) else "other"
        for row in rows[1:]:
            cells = [_clean_cell(c) for c in re.findall(r"<t[dh][\s\S]*?</t[dh]>", row, re.I)]
            if len(cells) < len(header):
                continue
            raw = (cells[0] or "").strip()
            if not raw:
                continue
            for model in _split_jp_models(raw):
                # 机型名优先判品类；判成 phone 但表本身不是手机表时，以表索引兜底
                cat = guess_category(model)
                if cat == "phone" and tbl_cat != "phone":
                    cat = tbl_cat
                for idx, part in cols:
                    add(cat, model, part, _tr_price(cells[idx]))
    out = [(m, [{"part": k, "price": v, "category": c} for k, v in parts.items()], _JP_URL)
           for (c, m), parts in bucket.items() if parts]
    return out


# ---------------- 中国(cn)：服务端 HTTP 直采 /rest/scic/open/spare-part-price/list ----------------
# 逆向所得（2026-09-21，Playwright 抓 SPA + 读 build.2890 切片确认）：
#   机型候选：GET {base}/models?searchVal=<型号码> -> [机型码, 颜色变体码...]
#            （如 SM-S9210 返回自身 + 25 个颜色变体；基类码直接查价返回空，必须用颜色变体码）
#   价表：GET {base}/list?model=<颜色变体码>&part=<中文部件名逗号拼接>
#        -> result=[{部件名:[{MODEL_CODE,PART_CODE,PART_DESC,PART_PRICE,WAERS:CNY}...]}]
#   部件名（中文）固定 6 类：主板/屏/尾插/后盖/电池/摄像头（来自 LEGO 码表
#        csui.prod-type.phone.parts，part 参数即这些中文名的逗号拼接）
#   价格 PART_PRICE 已是 CNY（WAERS=CNY），无独立人工费字段 -> 官网单列总维修价。
# 仅依赖标准库，无需浏览器；与其它 fetch_* 同口径返回 [(model_name, rows, detail_url), ...]。
_CN_BASE_DEFAULT = "https://service.samsung.com.cn/rest/scic/open/spare-part-price"
_CN_PARTS_DEFAULT = ["主板", "屏", "尾插", "后盖", "电池", "摄像头"]
_CN_CAT_MAP_DEFAULT = {"主板": "主板", "屏": "屏幕", "尾插": "充电口",
                       "后盖": "后盖", "电池": "电池", "摄像头": "摄像头"}
# 颜色变体间备件码一致，取首个变体为代表即可（避免 25× 行膨胀），价格与颜色无关。


def _cn_get(base, path, params=None, retries=3):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA["User-Agent"],
                "Accept-Language": "zh-CN,zh;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://service.samsung.com.cn/#/public/spare/part/price"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"GET {url} 失败: {last}")


def fetch_cn(rec):
    """服务端直采 三星中国包外零配件价（无需浏览器）。"""
    api = rec.get("query", {}).get("api", {})
    base = (api.get("base") or _CN_BASE_DEFAULT).rstrip("/")
    parts = api.get("part_names") or _CN_PARTS_DEFAULT
    cat_map = api.get("category_map") or _CN_CAT_MAP_DEFAULT
    seeds = api.get("seed_models") or []
    names = api.get("model_names") or {}
    part_q = ",".join(parts)
    out = []
    for code in seeds:
        try:
            mjson = _cn_get(base, "/models", {"searchVal": code})
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(mjson, list) or not mjson:
            continue
        # 基类码直接查价返回空 -> 取首个颜色变体码。
        # 注意 /models 偶发把基类码原样小写返回（如 SM-s9110），需忽略大小写剔除，
        # 否则会拿无效基类码去查价而得到 0 行（S23/S23+ 因此缺失）。
        variants = [c for c in mjson if c.upper() != code.upper()]
        variant = variants[0] if variants else mjson[0]
        try:
            ljson = _cn_get(base, "/list", {"model": variant, "part": part_q})
        except Exception:  # noqa: BLE001
            continue
        res = (ljson or {}).get("result") or []
        if not res:
            continue
        rows = []
        for grp in res:
            for pname, items in grp.items():
                if not items:
                    continue
                it = items[0]  # 同类目取首个（主件）为代表
                # PART_PRICE 是接口 JSON 里的**标准数值字符串**（实测 '2380.0'/'922.13'，
                # `.` 恒为小数点），用 JSON 语义解析器，避免将来出现 3 位小数时被误判。
                price = _parse_json_amount(it.get("PART_PRICE"))
                if not price or price <= 0:
                    continue
                rows.append({"part": cat_map.get(pname, pname), "price": price})
        if rows:
            mname = names.get(code, code)
            detail = (f"{base}/list?model={urllib.parse.quote(variant)}"
                      f"&part={urllib.parse.quote(part_q)}")
            for r in rows:
                r["category"] = guess_category(mname)
            out.append((mname, rows, detail))
    return out


# ---------------- 落库（镜像 run.py write_rows） ----------------
def _write_model(brand, country, country_name, rec, model_name, rows, detail_url):
    if not rows:
        return 0
    quarter = this_quarter()
    bid = upsert_brand(brand, rec.get("query", {}).get("mode"))
    upsert_country(country, country_name, rec.get("currency", ""), rec.get("locale", ""))
    # 品类：抓链能从源头确定就显式传（如日站按表分区），否则 upsert_model 用机型名兜底推断
    cat = next((r.get("category") for r in rows if r.get("category")), None)
    mid = upsert_model(bid, country, model_name, model_name, detail_url,
                       tier=classify_tier(model_name),
                       base_model=normalize_base_model(model_name),
                       spec=extract_spec(model_name),
                       color=extract_color(model_name), category=cat)
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


def crawl_and_write(brand, country, country_name, rec, force=False):
    """完整 worker：服务端拉取 -> 落库 -> 写 run_log / 异常入队。返回 (status, rows_total, reason)。

    **空态必须分四种**（与 crawler/run.py 同口径，此处曾是未同步的副本）：
      total==0                → failed（接口没返回机型，端点/参数可能变更）
      attempted==0 & total>0  → skipped（断点续跑：本季机型均已抓取，正常跳过，**不能报警**）
      errored                 → failed（请求层失败）
      attempted>0 & rows==0   → 官方未公布价格，skipped（非故障）
    旧实现只判 `rows_total == 0 → failed`，于是在"续跑全跳过"时写出一条假告警
    （2026-09-17 samsung/tr 复跑实测踩中：status=failed、anomaly=1，而数据其实完好）。
    """
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
        elif atype == "tr_static_table":
            models_rows = fetch_tr(rec)
        elif atype == "jp_static_table":
            models_rows = fetch_jp(rec)
        elif atype == "cn_price_list":
            models_rows = fetch_cn(rec)
        else:
            raise RuntimeError(f"未知 samsung_api 类型: {atype}")
        # 官方源偶有"同一台机器两个条目、名字只差一个 NBSP"（真实案例：德站
        # Galaxy Tab A9 (Wi-Fi) - SM-X110 与 ...A9\xa0(Wi-Fi)...，modelCode 都是 SM-X110N，
        # 两条价表还不一样）。不去重会落成两台机型 + 两组冲突价。
        # 去重放在**所有区域共用**的这里，任何区域出现该现象都被挡住。
        raw_total = len(models_rows)
        models_rows = dedupe_model_rows(models_rows, brand, country)
        if len(models_rows) != raw_total:
            print(f"  [dupe] {brand}/{country} 官方重名条目去重：{raw_total} -> {len(models_rows)}", flush=True)
        bid = upsert_brand(brand, rec.get("query", {}).get("mode"))
        total = len(models_rows)
        attempted = 0
        for mname, rows, detail_url in models_rows:
            if not force and model_already_captured(bid, country, mname, quarter):
                continue  # 断点续跑
            attempted += 1
            n = _write_model(brand, country, country_name, rec, mname, rows, detail_url)
            rows_total += n
        if total == 0:
            status = "failed"
            anomaly = 1
            reason = "服务端未解析到任何机型（页面结构变更 / 区域不可达）"
        elif attempted == 0:
            status = "skipped"
            reason = f"断点续跑：本季 {total} 台机型均已抓取，本轮无新增价行"
        elif rows_total == 0:
            status = "failed"
            anomaly = 1
            reason = f"尝试 {attempted} 台机型但 0 条价（页面结构变更或价格列解析失败）"
        if status == "skipped":
            print(f"  [resume] {brand}/{country} 本季已抓 {total} 台，不再重复取价", flush=True)
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
    ap.add_argument("--force", action="store_true", help="忽略本季断点续跑，原地刷新")
    args = ap.parse_args()
    rec = load_rec(args.brand, args.country)
    if not rec:
        print(f"[skip] {args.brand}/{args.country} 无 KB 记录", flush=True)
        return
    if rec.get("status") in ("blocked", "unavailable"):
        print(f"[skip] {args.brand}/{args.country} 状态={rec['status']}", flush=True)
        return
    name = {"de": "德国", "my": "马来西亚", "ae": "阿联酋", "tr": "土耳其",
            "jp": "日本", "cn": "中国"}.get(args.country, args.country)
    status, n, reason = crawl_and_write(args.brand, args.country, name, rec, force=args.force)
    print(f"[{status}] {args.brand}/{args.country} 本季新增 {n} 条价"
          + (f" | {reason}" if reason else ""), flush=True)


if __name__ == "__main__":
    main()
