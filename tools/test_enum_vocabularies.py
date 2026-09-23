"""test_enum_vocabularies.py - 各枚举列词汇表的隔离回归测试。

背景：2026-09-23 给 run_logs.status 做完"应用层枚举 + 写入口断言 + 巡检白名单"后，
发现库里还有一批**同病**的枚举列：自由文本、无 CHECK 约束、且都被多处读取，
其中若干消费方是**静默**的（不报错、只是结果悄悄变错）。本测试把这批列的护栏
全部钉住，全部写入**临时库**，绝不碰真实数据。

覆盖的列：
  models.category            写错 → 在 COALESCE 兜底外多出一个分组，静默拆散比价矩阵
  models.tier                写错 → 档位下拉框多出一个假选项
  models.model_url_kind      写错 → "这条价格是否精确到本机型"的标注失真（NULL 合法）
  price_snapshots.source_url_kind  同上，且与 model_url_kind 是**两套**词汇表
  maintenance_queue.status   写错 → 未解决工单计数归零
  brands.recipe_mode         写错 → _job_needs_browser 误判，影响并发池划分
  exchange_rates.rate_source 前缀模式（static / live:<endpoint>）
  price_snapshots.rate_source 同上前缀模式

用法：python tools/test_enum_vocabularies.py      # 全绿 exit 0，否则 exit 1
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor"))

import db  # noqa: E402

FAILS: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def expect_raise(name, fn, *a, **k):
    """断言调用抛 ValueError；返回异常文本供进一步的提示语断言。"""
    try:
        fn(*a, **k)
    except ValueError as e:
        print(f"  ✓ {name}: 已拦截 → {e}")
        return str(e)
    print(f"  ✗ {name}: 期望抛 ValueError，但未抛")
    FAILS.append(name)
    return ""


def count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="spm_enum_")) / "t.db"
    db.DB_PATH = tmp
    db.init_db()
    print(f"临时库：{tmp}\n")

    con = sqlite3.connect(str(db.DB_PATH))
    con.row_factory = sqlite3.Row

    # ---- 1) 词汇表集合 ------------------------------------------------------
    print("[1] 词汇表集合")
    check("VALID_CATEGORIES", set(db.VALID_CATEGORIES),
          {"phone", "tablet", "watch", "earbuds", "wearable", "other"})
    check("DEFAULT_CATEGORY 在合法集内", db.DEFAULT_CATEGORY in db.VALID_CATEGORIES, True)
    check("VALID_TIERS", set(db.VALID_TIERS), {"旗舰", "高端", "中端", "入门"})
    check("VALID_MODEL_URL_KINDS 数量", len(db.VALID_MODEL_URL_KINDS), 5)
    check("VALID_SNAPSHOT_URL_KINDS 数量", len(db.VALID_SNAPSHOT_URL_KINDS), 6)
    check("VALID_ISSUE_STATUSES", set(db.VALID_ISSUE_STATUSES),
          {"open", "resolved", "wont_fix"})
    check("VALID_RECIPE_MODES 数量", len(db.VALID_RECIPE_MODES), 5)

    # ---- 2) 两套 kind 词汇表必须相互独立 ------------------------------------
    # 这是本次最容易犯的错：把 models.model_url_kind 与 price_snapshots.source_url_kind
    # 合并成一个集合，于是"往 models 新增一种 kind"会静默把 price_snapshots 也放开。
    print("\n[2] 两套 kind 词汇表相互独立（不可共用同一集合对象）")
    check("非同一对象", db.VALID_MODEL_URL_KINDS is db.VALID_SNAPSHOT_URL_KINDS, False)
    check("快照列多一个 reference_cn",
          db.VALID_SNAPSHOT_URL_KINDS - db.VALID_MODEL_URL_KINDS, {"reference_cn"})
    check("模型列独有的值应为空",
          db.VALID_MODEL_URL_KINDS - db.VALID_SNAPSHOT_URL_KINDS, set())

    # ---- 3) 校验器：合法放行 / 非法拦截 / 纠错提示质量 -----------------------
    print("\n[3] 校验器行为")
    for fn, good in [
        (db.validate_category, ["phone", "tablet", "watch", "earbuds", "wearable", "other"]),
        (db.validate_tier, ["旗舰", "高端", "中端", "入门"]),
        (db.validate_model_url_kind, [None, "model_api", "model_text_fragment",
                                      "model_page", "category_api_locator", "brand_entry"]),
        (db.validate_snapshot_url_kind, [None, "model_api", "reference_cn"]),
        (db.validate_issue_status, ["open", "resolved", "wont_fix"]),
        (db.validate_recipe_mode, [None, "api_reborn", "form_select_cascade",
                                   "samsung_api", "vivo_api", "xiaomi_api"]),
        (db.validate_rate_source, [None, "static", "live:open.er-api.com",
                                   "live:example.org"]),
    ]:
        label = fn.__name__
        try:
            for v in good:
                fn(v)
            check(f"{label} 合法值全部放行", True, True)
        except ValueError as e:
            check(f"{label} 合法值全部放行", f"误拒: {e}", True)

    # 非法值必须拦下
    expect_raise("category='Phone'（大小写）", db.validate_category, "Phone")
    expect_raise("category=''", db.validate_category, "")
    expect_raise("category=None", db.validate_category, None)
    expect_raise("tier='顶级'", db.validate_tier, "顶级")
    expect_raise("model_url_kind='model_apii'", db.validate_model_url_kind, "model_apii")
    expect_raise("model_url_kind=None 之外的非法类型", db.validate_model_url_kind, 5)
    expect_raise("issue_status='closed'", db.validate_issue_status, "closed")
    expect_raise("recipe_mode='api'", db.validate_recipe_mode, "api")
    expect_raise("rate_source='live'（缺冒号+端点）", db.validate_rate_source, "live")
    expect_raise("rate_source='live:'（空前缀体）", db.validate_rate_source, "live:")

    # 纠错提示质量：只对"真·拼写错误"提示，中文短词不误导
    e = expect_raise("category='phonee'", db.validate_category, "phonee")
    check("  提示含 'phone'", "phone" in e, True)
    e = expect_raise("tier='旗舰版'", db.validate_tier, "旗舰版")
    check("  提示含 '旗舰'", "旗舰" in e, True)
    e = expect_raise("tier='顶级'（无近似值，不应乱提示）", db.validate_tier, "顶级")
    check("  不应给出误导性纠错提示", "是否想写" in e, False)

    # ---- 4) 写入入口拦截（且不落库） ---------------------------------------
    print("\n[4] 写入入口拦截 + 不落库")
    bid = db.upsert_brand("__enum__", recipe_mode="api_reborn")
    db.upsert_country("cn", "中国", "CNY", "zh-CN")

    n0 = count(con, "models")
    expect_raise("upsert_model(category='Phone')", db.upsert_model,
                 bid, "cn", "__enum_phone__", category="Phone")
    check("  非法 category 未落库", count(con, "models"), n0)
    expect_raise("upsert_model(tier='顶级')", db.upsert_model,
                 bid, "cn", "__enum_tier__", tier="顶级")
    check("  非法 tier 未落库", count(con, "models"), n0)
    expect_raise("upsert_model(model_url_kind='page')", db.upsert_model,
                 bid, "cn", "__enum_kind__", model_url_kind="page")
    check("  非法 model_url_kind 未落库", count(con, "models"), n0)

    mid = db.upsert_model(bid, "cn", "__enum_ok__", category="tablet",
                          tier="高端", model_url_kind="model_page")
    check("合法 upsert_model 正常落库",
          con.execute("SELECT category||'/'||tier||'/'||model_url_kind FROM models WHERE id=?",
                      (mid,)).fetchone()[0],
          "tablet/高端/model_page")

    n0 = count(con, "brands")
    expect_raise("upsert_brand(recipe_mode='api')", db.upsert_brand,
                 "__enum_bad__", recipe_mode="api")
    check("  非法 recipe_mode 未落库", count(con, "brands"), n0)

    n0 = count(con, "exchange_rates")
    expect_raise("set_rate(source='live')", db.set_rate, "2026Q3", "CNY", 1.0, "live")
    check("  非法 rate_source 未落库", count(con, "exchange_rates"), n0)
    # 正向：合法来源必须能落库（否则下面的"污染-检测"用例会因表为空而空转）
    db.set_rate("2026Q3", "CNY", 1.0, db.RATE_SOURCE_STATIC)
    db.set_rate("2026Q3", "USD", 7.1, db.RATE_SOURCE_LIVE_PREFIX + "example.org")
    check("合法 set_rate 落库（static + live: 前缀）", count(con, "exchange_rates"), n0 + 2)
    check("  live:<endpoint> 前缀被接受",
          con.execute("SELECT rate_source FROM exchange_rates WHERE currency='USD'"
                      ).fetchone()[0], "live:example.org")

    pid = db.upsert_part(mid, "屏幕")
    n0 = count(con, "price_snapshots")
    expect_raise("insert_snapshot(source_url_kind='page')", db.insert_snapshot,
                 pid, "2026Q3", 100.0, "CNY", 100.0, source_url_kind="page")
    check("  非法 source_url_kind 未落库", count(con, "price_snapshots"), n0)
    expect_raise("insert_snapshot(rate_source='live')", db.insert_snapshot,
                 pid, "2026Q3", 100.0, "CNY", 100.0, rate_source="live")
    check("  非法 rate_source 未落库", count(con, "price_snapshots"), n0)
    db.insert_snapshot(pid, "2026Q3", 100.0, "CNY", 100.0,
                       source_url_kind="model_api", rate_source="live:example.org")
    check("合法 insert_snapshot 正常落库", count(con, "price_snapshots"), n0 + 1)

    # ---- 5) 工单状态：add_issue / resolve_issue 用常量 ----------------------
    print("\n[5] 工单状态生命周期")
    iid = db.add_issue("__enum__", "cn", "测试工单")
    check("add_issue 落 status=open",
          con.execute("SELECT status FROM maintenance_queue WHERE id=?", (iid,)).fetchone()[0],
          db.ISSUE_OPEN)
    check("open_issues() 能查到", any(i["id"] == iid for i in db.open_issues()), True)
    db.resolve_issue(iid, diagnosis="d", proposed_fix="f")
    check("resolve_issue 落 status=resolved",
          con.execute("SELECT status FROM maintenance_queue WHERE id=?", (iid,)).fetchone()[0],
          db.ISSUE_RESOLVED)
    check("open_issues() 不再返回", any(i["id"] == iid for i in db.open_issues()), False)

    # ---- 6) 巡检白名单：能抓非法、不误报合法 -------------------------------
    print("\n[6] 巡检白名单 + API 侧非法值统计")
    import verify_quarterly_run as V  # noqa: E402
    rep_ok = V.Report()
    V.check_enum_vocabularies(con, rep_ok)
    check("合法数据巡检无误报",
          any(c == "ENUM_INVALID" for _, c, _ in rep_ok.rows), False)

    con.execute("UPDATE models SET category='Phone' WHERE id=?", (mid,))
    con.execute("UPDATE brands SET recipe_mode='api' WHERE name='__enum__'")
    con.execute("UPDATE maintenance_queue SET status='closed' WHERE id=?", (iid,))
    con.execute("UPDATE exchange_rates SET rate_source='live' WHERE currency='CNY'")
    con.commit()
    rep_bad = V.Report()
    V.check_enum_vocabularies(con, rep_bad)
    bad_codes = [c for _, c, _ in rep_bad.rows if c == "ENUM_INVALID"]
    check("巡检捕获 4 处非法值", len(bad_codes), 4)
    check("巡检判为 ERROR（非仅 WARN）", rep_bad.has_error(), True)

    # API 侧 _invalid_enum_counts 必须与巡检同口径地发现问题
    import api.server as S  # noqa: E402
    counts = S._invalid_enum_counts(con)
    check("API 侧统计 models.category", counts["models.category"] >= 1, True)
    check("API 侧统计 maintenance_queue.status",
          counts["maintenance_queue.status"] >= 1, True)
    check("API 侧统计 brands.recipe_mode", counts["brands.recipe_mode"] >= 1, True)
    check("API 侧统计 exchange_rates.rate_source",
          counts["exchange_rates.rate_source"] >= 1, True)
    check("API 侧未受影响列应为 0", counts["models.tier"], 0)

    # 复原后可再次通过（证明巡检不是一次性状态）
    con.execute("UPDATE models SET category='tablet' WHERE id=?", (mid,))
    con.execute("UPDATE brands SET recipe_mode='api_reborn' WHERE name='__enum__'")
    con.execute("UPDATE maintenance_queue SET status='open' WHERE id=?", (iid,))
    con.execute("UPDATE exchange_rates SET rate_source='static' WHERE currency='CNY'")
    con.commit()
    rep_fix = V.Report()
    V.check_enum_vocabularies(con, rep_fix)
    check("复原后巡检重新通过",
          any(c == "ENUM_INVALID" for _, c, _ in rep_fix.rows), False)

    # ---- 7) 第三轮：推导型词汇表（部件品类 / 币种 / 语种） -------------------
    # 本轮的关键手法：词表**不手写**，而是从唯一的生成器推导，杜绝"加了规则忘了改常量"。
    # 下面的断言独立重推一遍，证明集合确实来自生成器而非抄写。
    print("\n[7] 推导型词汇表（part_type / currency / lang）")

    # 7.1 部件品类：从 _MANUAL_CATEGORY_MAP 独立重推
    rederived = {lab for _rx, lab in db._MANUAL_CATEGORY_MAP} | {db.PART_TYPE_OTHER}
    check("VALID_PART_TYPES == 从 _MANUAL_CATEGORY_MAP 重推的结果",
          set(db.VALID_PART_TYPES), rederived)
    check("  含兜底类 '其他'", db.PART_TYPE_OTHER in db.VALID_PART_TYPES, True)
    check("  数量 = 映射表标签数 + 1", len(db.VALID_PART_TYPES), len(rederived))
    # 反向：加了规则就必须自动进白名单（本断言在"手抄常量"的实现下会失败）
    check("  _MANUAL_CATEGORY_MAP 的每个标签都在白名单内",
          all(lab in db.VALID_PART_TYPES for _rx, lab in db._MANUAL_CATEGORY_MAP), True)
    # 品类不会与 models.category（产品品类）混为一谈
    check("  与 VALID_CATEGORIES 无交集（两套词汇表）",
          set(db.VALID_PART_TYPES) & set(db.VALID_CATEGORIES), set())

    # 7.2 币种：必须等于汇率表的键（能折算才合法）
    check("VALID_CURRENCIES == frozenset(STATIC_RATES)",
          set(db.VALID_CURRENCIES), set(db.STATIC_RATES))
    check("  每个币种都能取到汇率（无 None）",
          all(db.STATIC_RATES[c] is not None for c in db.VALID_CURRENCIES), True)
    # 反向：新增汇率必须自动进白名单
    check("  STATIC_RATES 的每个键都在白名单内",
          all(c in db.VALID_CURRENCIES for c in db.STATIC_RATES), True)

    # 7.3 语种：白名单必须与 part_norm._detect_lang 的返回分支一一对应
    from crawler import part_norm as _pn  # noqa: E402
    probes = {"ja": "画面の損傷", "tr": "şarj cihazı", "zh": "电池",
              "de": "Displayschaden", "en": "Battery Replacement"}
    got_langs = {exp: _pn._detect_lang(s) for exp, s in probes.items()}
    check("_detect_lang 探测结果与预期一致",
          got_langs, {k: k for k in probes})
    check("  _detect_lang 的全部返回值 ⊆ VALID_LANGS",
          set(got_langs.values()) <= set(db.VALID_LANGS), True)
    check("  VALID_LANGS 无多余项（每个值都能被真实探测到）",
          set(db.VALID_LANGS) - set(got_langs.values()), set())

    # 7.3b ⚠️ 已知缺陷（本测试在加固语种时发现，尚未修复——属行为变更，待确认后另做）
    # _detect_lang 的土耳其字符类 [ıİşŞğĞçÇöÖüÜ] 排在德语词表**之前**，而德语
    # Rückglas / Rückkamera 含 ü，被抢先判成 tr。实测生产库 de 国家下 84 行受影响。
    # 注意影响面有限：normalize() 的查表键是 (part_type, key)，**不含 lang**，
    # 故归一化本身不受影响，仅是 lang 标签错误（不参与比价）。
    # 这里把它钉住：一旦有人修复，本断言会失败并提醒同步更新报告与巡检。
    check("已知缺陷：含变音符的德语词被误判为 tr（见 reports/enum_columns_round2）",
          _pn._detect_lang("Rückglas"), "tr")
    check("  对照：不含变音符的德语词判定正确",
          _pn._detect_lang("Displayschaden"), "de")

    # 7.4 写入口拦截：非法 part_type / currency 必须被拦下且不落库
    n_parts0 = count(con, "parts")
    expect_raise("upsert_part(part_type='屏暮')", db.upsert_part, mid, "__t1__", "屏暮")
    check("  非法 part_type 未落库", count(con, "parts"), n_parts0)
    expect_raise("upsert_part(part_type='Screen')", db.upsert_part, mid, "__t2__", "Screen")
    check("  非法 part_type（英文）未落库", count(con, "parts"), n_parts0)
    db.upsert_part(mid, "__t3__", "其他")
    check("  合法 part_type='其他' 正常落库", count(con, "parts"), n_parts0 + 1)

    n_c0 = count(con, "countries")
    expect_raise("upsert_country(currency='EURO')", db.upsert_country, "__xx__", "X", "EURO")
    check("  非法 currency 未落库", count(con, "countries"), n_c0)
    expect_raise("upsert_country(currency='')", db.upsert_country, "__xx__", "X", "")
    check("  空 currency 也未落库", count(con, "countries"), n_c0)
    db.upsert_country("__xx__", "X", "EUR")
    check("  合法 currency='EUR' 正常落库", count(con, "countries"), n_c0 + 1)

    # 7.5 巡检必须捕获这两类非法值
    con.execute("UPDATE parts SET part_type='屏暮' WHERE name='__t3__'")
    con.execute("UPDATE countries SET currency='EURO' WHERE code='__xx__'")
    con.commit()
    rep7 = V.Report()
    V.check_enum_vocabularies(con, rep7)
    msgs7 = [m for _sev, c, m in rep7.rows if c == "ENUM_INVALID"]
    check("巡检捕获 parts.part_type 非法值",
          any("parts.part_type" in m for m in msgs7), True)
    check("巡检捕获 countries.currency 非法值",
          any("countries.currency" in m for m in msgs7), True)
    con.execute("UPDATE parts SET part_type='其他' WHERE name='__t3__'")
    con.execute("UPDATE countries SET currency='EUR' WHERE code='__xx__'")
    con.commit()
    rep7b = V.Report()
    V.check_enum_vocabularies(con, rep7b)
    check("复原后巡检重新通过（第三轮）",
          any(c == "ENUM_INVALID" for _sev, c, _m in rep7b.rows), False)

    # ---- 8) 静态 lint：引用了 db.X 就必须 import db --------------------------
    # 本轮真实踩到：把 run.py 里的字面量换成 db.MODEL_URL_KIND_* 时，忘了该文件用的是
    # `from db import (...)`，没有 `import db` —— 模块能正常 import（该表达式在函数体内，
    # 不在导入期求值），只在**跑那条抓取路径时**才 NameError。静态检查比跑一遍抓取便宜得多。
    print("\n[8] 静态 lint：db.X 引用与 import db 必须配套")
    import ast  # noqa: E402

    def _uses_db(tree):
        return any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                   and n.value.id == "db" for n in ast.walk(tree))

    def _imports_db(tree):
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                if any(a.name == "db" or a.asname == "db" for a in n.names):
                    return True
            if (isinstance(n, ast.ImportFrom) and n.module == "db"
                    and any(a.name == "db" for a in n.names)):
                return True
        return False

    offenders = []
    for p in sorted(ROOT.rglob("*.py")):
        sp = str(p)
        if any(x in sp for x in (".git", "vendor", "node_modules", "_archive")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        if _uses_db(tree) and not _imports_db(tree):
            offenders.append(sp.replace(str(ROOT), ".").replace("\\", "/"))
    check("无『引用 db.X 但未 import db』的模块", offenders, [])

    # 8.1 直接验证 run.py 的链接类型常量确实解析到 db 的值
    # （本轮改动的正是这里；若 import 列表漏了名字，这里会 AttributeError）
    from crawler import run as _run  # noqa: E402
    check("run.MODEL_URL_KIND_API == db.MODEL_URL_KIND_API",
          _run.MODEL_URL_KIND_API, db.MODEL_URL_KIND_API)
    check("run.MODEL_URL_KIND_PAGE == db.MODEL_URL_KIND_PAGE",
          _run.MODEL_URL_KIND_PAGE, db.MODEL_URL_KIND_PAGE)
    check("run.MODEL_URL_KIND_BRAND_ENTRY == db.MODEL_URL_KIND_BRAND_ENTRY",
          _run.MODEL_URL_KIND_BRAND_ENTRY, db.MODEL_URL_KIND_BRAND_ENTRY)
    check("  三者在快照词汇表内（否则写入口会拦下真实抓取）",
          {_run.MODEL_URL_KIND_API, _run.MODEL_URL_KIND_PAGE,
           _run.MODEL_URL_KIND_BRAND_ENTRY} <= set(db.VALID_SNAPSHOT_URL_KINDS), True)

    con.close()
    shutil.rmtree(tmp.parent, ignore_errors=True)

    print("\n" + "=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项未通过 ✗ → {FAILS}")
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
