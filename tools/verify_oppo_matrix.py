#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPPO 比价矩阵健康度回归测试（2026-09-23 覆盖修正后）。

背景：旧端点 /cnw/v1/GetPartPrice 残留的 1,123 个污染机型条目（含 186 台一加机型、
中国专供版、智能电视/手环等）已清退，机型表改由 getProductInfo（区域产品目录全集）定源。
本脚本在浏览器里断言修正结果没有回退：

  1) 矩阵正常渲染（.heat 表格存在）；
  2) **无幽灵机型**：不得出现「一加/OnePlus」「智能电视」「手环」「兰博基尼」等
     不属于这些区域的机型名；
  3) **参考价已清零**：不得出现 .ref-cell / sup.ref（CN 参考价方案的前提已被证伪并停用）；
  4) 单元格小字不得出现「参考·中国」；
  5) **明细弹窗**可打开，且物料/人工费按**当地货币**标注（不得标成 ¥）——
     2026-09-23 的「人工费¥50,000」正是在这里被用户发现的；
  6) DB 层人工费量级正常（各币种均未超上限）——防「×1000」类解析事故回退。

前置：后端已启动 python api/server.py (127.0.0.1:8000)。
用法：python tools/verify_oppo_matrix.py [--brand oppo] [--model "OPPO Reno14"]
                                    [--shot out.png] [--result-log logs_x.txt]
"""
import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "references" / "calibration"))
from _paths import find_chromium  # noqa: E402

URL = "http://127.0.0.1:8000/#matrix"
BRAND, MODEL = "oppo", "OPPO Reno14"

# 「幽灵机型」的权威判据在 **DB 层**（source_url），不在页面文本 —— 见 db_legacy_residue()。
# 早期版本用整页文本搜关键词，把下列**合法数据**误报为幽灵机型，已弃用该做法：
#   · 「一加 / OnePlus / 智能电视」：OPPO **中国**官网的备件价页本身就含这些
#     （一加在华售后服务已并入 OPPO），来源是 REBORN，属真实数据；
#   · 「手环」：还大量出现在小米中国的机型名里；
#   · 「兰博基尼」：小米 Redmi K70 至尊冠军版的联名款。


def db_legacy_residue():
    """DB 层：仍为旧端点来源的 OPPO 机型行数（应为 0）。"""
    import sqlite3
    c = sqlite3.connect(str(ROOT / "spare_parts.db"))
    n = c.execute("""SELECT COUNT(*) FROM models m JOIN brands b ON b.id=m.brand_id
                     WHERE b.name='oppo'
                       AND m.source_url LIKE '%/cnw/v1/GetPartPrice%'""").fetchone()[0]
    c.close()
    return n


# 人工费量级上限（**当地货币**，不是 CNY）。依据 2026-09-23 修复后 OPPO 的实测值域
# （CNY 50 / MYR 50 / AED 40~80 / MXN 240~650 / TRY 1350）留足余量。
# 该阈值能可靠抓出被"×1000"污染的旧数据（MYR 50000 / AED 40000 /
# MXN 650000 / TRY 1350000 / CNY 50000），又不误伤真实的高人工费定价。
LABOR_CAP = {"CNY": 2000, "MYR": 1000, "AED": 1000, "MXN": 8000, "TRY": 20000}


def db_labor_fee_anomalies(brand="oppo"):
    """DB 层：人工费量级异常的备件行（防 2026-09-23 那类"×1000"事故回退）。

    事故回顾：OPPO REBORN 的 laborCostAmount 是固定 3 位小数的 JSON 字符串
    （实测 "50.000"），被交给面向**网页文本**的 parse_amount 后，其
    "单个分隔符且尾 3 位 = 千分位"启发式把 50.000 读成 50000 ——
    my/cn/ae/mx/tr 全区域人工费放大 1000 倍。修复为 parse_json_amount
    （`vendor/normalize.py`，回归测试见 tools/test_amount_parse.py）。

    为什么按币种设阈值而非"统一上限"：labor_fee 存的是**原币**金额，
    TRY 的量级天然比 CNY 高两个数量级，一刀切必然误报。
    """
    import sqlite3
    c = sqlite3.connect(str(ROOT / "spare_parts.db"))
    rows = c.execute("""SELECT m.country_code cc, ps.currency cur, ps.labor_fee lf,
                               COUNT(*) n
        FROM price_snapshots ps
        JOIN parts p ON p.id=ps.part_id
        JOIN models m ON m.id=p.model_id
        JOIN brands b ON b.id=m.brand_id
        WHERE b.name=? AND ps.labor_fee IS NOT NULL AND ps.labor_fee > 0
        GROUP BY cc, cur, lf ORDER BY ps.labor_fee DESC""", (brand,)).fetchall()
    c.close()
    bad = []
    for cc, cur, lf, n in rows:
        cap = LABOR_CAP.get(cur)
        if cap and lf > cap:
            bad.append(f"{cc}/{cur} 人工费 {lf:g}（上限 {cap}，{n} 行）")
    return bad


def kb_unavailable(brand):
    """该品牌「官方不提供备件价」的区域（KB recipe status = unavailable/unverified）。"""
    import json
    p = ROOT / "references" / "kb" / f"{brand}.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for cc, arr in (d.get("countries") or {}).items():
        for rec in (arr or []):
            if (rec.get("status") or rec.get("query", {}).get("status")) in ("unavailable", "unverified"):
                out.append(cc)
                break
    return out


def api_note_fails():
    """端到端断言：API 暴露的每个「官方不提供备件价」区域，**必须带非空说明**。

    为什么必须测这一层（2026-09-23）：KB 里说明文字的键名不统一 —— recipe 级用复数
    `notes`（各品牌主流写法），只有 query/api 级才用单数 `note`。`api/server.py`
    原先只认单数，导致 apple/tr、vivo/jp 等区域的说明在页面上**渲染为空**，
    "官方无数据"就退化成一句没有理由的空白横幅。此处直接打 API 复现前端取数路径，
    防止该回退逻辑再次退化。
    """
    import json as _json
    import urllib.request
    problems = []
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/brands", timeout=20) as r:
            brands = _json.load(r)
    except Exception as e:
        return [f"取 /api/brands 失败：{type(e).__name__}: {str(e)[:90]}"]
    total = 0
    for b in brands:
        for u in (b.get("source_unavailable") or []):
            total += 1
            if not (u.get("note") or "").strip():
                problems.append(f"{b['name']}/{u['country']} 标为 {u['status']} "
                                f"但说明为空（界面会只剩无理由的空白横幅）")
    if total == 0:
        problems.append("API 未返回任何『官方不提供备件价』区域（预期 10 个）")
    else:
        print(f"[kb] API 暴露「官方不提供备件价」区域 {total} 个，说明文字均非空")
    return problems


def _assert(info, fails, expect_no_src=0):
    if not info.get("tableRendered"):
        fails.append("比价矩阵未渲染（.heat 表格缺失）")
    residue = db_legacy_residue()
    if residue:
        fails.append(f"旧端点污染机型残留 {residue} 台（应为 0）")
    labor_bad = db_labor_fee_anomalies()
    if labor_bad:
        fails.append("人工费量级异常（疑似再被解析放大）：" + "；".join(labor_bad[:4]))
    # 有"官方不提供备件价"的区域时，必须把原因写在页面上 ——
    # 否则用户会把空白列误读成"抓取失败"（这正是 2026-09-23 体检要澄清的）
    if expect_no_src and not info.get("unavailableNoteRendered"):
        fails.append(f"该品牌有 {expect_no_src} 个『官方不提供备件价』的区域，"
                     f"但页面未渲染说明横幅")
    if expect_no_src:
        titles = info.get("noSrcTitles") or []
        if len(titles) != expect_no_src:
            fails.append(f"说明横幅里的区域数 {len(titles)} ≠ KB 的 {expect_no_src} 个")
        blank = [t["cc"] for t in titles if not t["tip"] or "未记录原因" in t["tip"]]
        if blank:
            fails.append(f"以下区域的说明为空（界面只剩无理由的空白横幅）: {blank}")
    if info.get("refCellCount", 0) != 0:
        fails.append(f"参考价单元格应为 0（方案已停用），实际 {info['refCellCount']}")
    # 明细弹窗必须能打开，且物料/人工费按**当地货币**标注（那是原币金额，
    # 与同列的「CNY 折算」不是一回事）——2026-09-23 的「人工费¥50,000」即此处标错。
    if not info.get("detailOpened"):
        fails.append(f"点击价格单元格未弹出明细弹窗（{str(info.get('detailText'))[:80]}）")
    else:
        dt = info.get("detailText") or ""
        for bad in ("人工费¥", "物料¥", "人工¥"):
            if bad in dt:
                fails.append(f"明细弹窗把原币金额标成人民币符号：出现「{bad}」")
    if info.get("refBadgeCount", 0) != 0:
        fails.append(f"参考价徽标应为 0，实际 {info['refBadgeCount']}")
    if info.get("hasRefText"):
        fails.append("单元格内仍出现「参考·中国」文案")
    if not info.get("cellCount"):
        fails.append("矩阵没有任何价格单元格")

    if fails:
        print("\n[FAIL]")
        for f in fails:
            print("  -", f)
        return 1
    extra = "；已渲染『官方不提供备件价』区域说明" if expect_no_src else ""
    print(f"\n[PASS] 矩阵渲染正常（{info['cellCount']} 个价格单元格）；"
          f"旧端点污染机型残留 0 台；参考价单元格/徽标 0 个；"
          f"人工费量级正常（各币种均未超上限）；明细弹窗币种标注正确{extra}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=BRAND)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--shot", default=None)
    ap.add_argument("--result-log", default="logs_oppo_matrix_result.txt")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    fails = []
    fails += api_note_fails()
    with sync_playwright() as p:
        print("[1/5] 启动无头 Chromium…", flush=True)
        b = p.chromium.launch(headless=True, executable_path=find_chromium(),
                              args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1680, "height": 1200})
        pg.on("pageerror", lambda e: fails.append(f"页面 JS 异常: {e}"))
        print("[2/5] 打开比价矩阵…", flush=True)
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector("#mc-b", timeout=20000)
        pg.select_option("#mc-b", a.brand)
        pg.wait_for_timeout(1200)
        pg.fill("#mc-search", a.model)
        pg.wait_for_timeout(1500)
        pg.click(f'#mc-list .combo-item[data-m="{a.model}"]')
        pg.wait_for_timeout(3500)
        print("[3/5] 读取矩阵…", flush=True)
        info = pg.evaluate("""() => {
            const tables = [...document.querySelectorAll('table.heat')];
            const body = document.body.innerText || '';
            const hints = [...document.querySelectorAll('.hint')].map(e => e.innerText || '').join(' ');
            return {
                tableRendered: tables.length > 0,
                cellCount: document.querySelectorAll('td.cell-click, td.muted').length,
                refCellCount: document.querySelectorAll('td.ref-cell').length,
                refBadgeCount: document.querySelectorAll('sup.ref').length,
                hasRefText: body.includes('参考·中国'),
                unavailableNoteRendered: hints.includes('官方不提供') && hints.includes('不是抓取失败'),
                // 「官方不提供」区域必须**逐条**带非空原因（悬停可见）。
                // 只渲染一个没有理由的空横幅等于没说明 —— 2026-09-23 修的就是这个：
                // KB 用复数 notes、读取端只认单数 note，导致 vivo/de、vivo/jp 的说明为空。
                noSrcTitles: [...document.querySelectorAll('.no-src')]
                    .map(e => ({cc: (e.textContent||'').trim(), tip: (e.title||'').trim()})),
            };
        }""")
        if a.shot:
            pg.screenshot(path=a.shot)
        # 明细弹窗 —— 用户实际盯着看的那一屏（2026-09-23 的「人工费¥50,000」就是在这里
        # 被发现的）。点开第一个价格单元格，检查物料/人工是否按**当地货币**标注。
        print("[4/5] 打开明细弹窗…", flush=True)
        try:
            pg.click("td.cell-click", timeout=8000)
            pg.wait_for_selector(".modal", timeout=8000)
            pg.wait_for_timeout(600)
            info.update(pg.evaluate("""() => {
                const m = document.querySelector('.modal');
                return {detailOpened: !!m, detailText: m ? (m.innerText || '').slice(0, 2000) : ''};
            }"""))
        except Exception as e:  # noqa: BLE001
            info.update(detailOpened=False,
                        detailText=f"{type(e).__name__}: {str(e)[:90]}")
        # 先落结果再关浏览器：本机残留 chrome-headless-shell 会让 b.close() 长时间阻塞，
        # 若把结果打印放在 close 之后，断言结果将永远刷不出来。
        print("结果:", info, flush=True)
        Path(a.result_log).write_text(repr(info), encoding="utf-8")
        print("[5/5] 关闭浏览器…", flush=True)
        try:
            b.close()
        except Exception as e:
            print(f"  (浏览器关闭异常，忽略): {e}", flush=True)

    return _assert(info, fails, expect_no_src=len(kb_unavailable(a.brand)))


if __name__ == "__main__":
    sys.exit(main())
