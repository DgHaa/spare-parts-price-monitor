#!/usr/bin/env python3
"""守卫：抽取器的「价格行币种白名单」必须覆盖各区域官网实际渲染的币种形态。

== 为什么需要这个工具（2026-09-22 apple/cn 事故）==

  `executor.run_query` 的 form_select_cascade 分支用一条正则 `price_re` 判断
  "这一行是不是价格行"；heading 没命中的兜底分支也依赖它。Apple 中国站价格渲染为
  `RMB 969`（**既不是 `¥` 也不是 `CNY`**），而白名单当时只有
  `AED|CNY|EUR|JPY|MYR|RM|TRY`，漏了 `RMB` → apple/cn **139 台机型全部 0 行**
  （run_log=failed），但结构完全同构的 apple/mx、apple/de 正常 —— 极易被误判成
  "中国站无价/被墙"。

  该类 bug 的共性是：**新增区域时忘了把该站点的币种渲染形态加进白名单**。
  本守卫把「币种 -> 官网渲染样本」与 `price_re` 对齐，缺一个就报错，防复发。

== 只读 ==
  不修改任何文件；解析的是**运行时真正加载的那份** executor.py
  （与 crawler/core.py 同序：仓库 vendor/ 优先，skill 目录回退）。

用法：
  python tools/check_extractor_currency.py            # 校验；有缺口则退出码 1
  python tools/check_extractor_currency.py --selftest # 自检：确认守卫能抓出历史坏正则
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_EXECUTOR = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/scripts/executor.py")
VENDOR_EXECUTOR = ROOT / "vendor" / "executor.py"
KB_DIR = ROOT / "references" / "kb"

# 币种 -> 该币种在各官网上**实际观测到**的价格行渲染样本。
# ⚠️ 语义是「**全部**样本都必须被 price_re 命中」，不是"命中任一即可"——
#    只要有一种真实渲染形态漏匹配，抽取就会静默丢行（apple/cn 事故正是如此）。
#    所以只填**官网真实出现过**的形态，别把"可能的写法"塞进来自我安慰：
#    Apple 中国站渲染的是 `RMB 969`，`¥968`/`CNY 968` 在本项目里从未出现，
#    填进去会让守卫在缺 RMB 时误判 PASS。
SAMPLES: dict[str, list[str]] = {
    "CNY": ["RMB 969", "RMB 1,298"],   # Apple 中国实际用 RMB（非 ¥/CNY）
    "EUR": ["€899"],
    "JPY": ["10,800円"],
    "AED": ["AED 1,845"],
    "MYR": ["RM 1,099"],
    "MXN": ["$1,299"],                 # Apple 墨西哥用 $ 号
    "USD": ["$99"],
    "TRY": ["TRY 1.099"],
}

# 历史坏正则（缺 RMB）：供 --selftest 验证守卫确实能抓到该缺口。
LEGACY_BAD_RE = r"[€$¥£]|円|\b(?:AED|CNY|EUR|JPY|MYR|RM|TRY)(?![A-Za-z])"

PRICE_RE_PAT = re.compile(
    r"price_re\s*=\s*re\.compile\(\s*r([\"'])(?P<pat>.*?)\1\s*,\s*re\.I\s*\)", re.S
)


def resolve_executor() -> Path:
    """与 crawler/core.py 同序：vendor/ 优先，skill 回退。"""
    if VENDOR_EXECUTOR.exists():
        return VENDOR_EXECUTOR
    return SKILL_EXECUTOR


def extract_price_re(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    m = PRICE_RE_PAT.search(src)
    if not m:
        raise SystemExit(
            f"[FAIL] 在 {path} 里找不到 `price_re = re.compile(r\"...\", re.I)` 的定义。\n"
            "       抽取器可能被重构/改名 —— 请同步更新本守卫（这是有意让缺口显性化）。"
        )
    return m.group("pat")


def currencies_in_kb() -> dict[str, list[str]]:
    """KB 里出现的币种 -> 用到它的 brand/country 列表。"""
    import json

    found: dict[str, list[str]] = {}
    for f in sorted(KB_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        brand = data.get("brand") or f.stem
        for country, recs in (data.get("countries") or {}).items():
            for rec in recs:
                cur = rec.get("currency")
                if not cur:
                    continue
                found.setdefault(cur.upper(), []).append(f"{brand}/{country}")
    return found


def check(pattern: str) -> tuple[bool, list[tuple[str, bool, str]]]:
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        print(f"[FAIL] price_re 无法编译：{e}")
        return False, []
    rows: list[tuple[str, bool, str]] = []
    for cur, samples in SAMPLES.items():
        misses = [s for s in samples if not rx.search(s)]
        # 该币种的**每一种**实测渲染形态都必须命中，否则视为缺口。
        rows.append((cur, not misses, misses[0] if misses else samples[0]))
    return all(ok for _, ok, _ in rows), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="用历史坏正则跑一遍，确认守卫能报出 RMB 缺口")
    args = ap.parse_args()

    path = resolve_executor()
    print(f"[source] 运行时抽取器 = {path}")
    pattern = LEGACY_BAD_RE if args.selftest else extract_price_re(path)
    if args.selftest:
        print("[selftest] 使用历史坏正则（缺 RMB），期望 RMB 行 FAIL")
    print(f"[pattern] {pattern}\n")

    ok_all, rows = check(pattern)
    print(f"{'币种':6}{'覆盖':6}样本")
    for cur, ok, sample in rows:
        label = "全部命中" if ok else f"漏匹配：{sample}"
        print(f"{cur:6}{'PASS' if ok else 'FAIL':6}{label}")

    present = currencies_in_kb()
    print(f"\n[KB] 已配置币种：{', '.join(sorted(present))}")
    relevant = {c: v for c, v in present.items() if c in SAMPLES}
    missing = sorted(c for c in relevant if not dict((r[0], r[1]) for r in rows)[c])
    uncovered_kb = sorted(c for c in present if c not in SAMPLES)

    if missing:
        print(f"[FAIL] KB 用到但白名单未覆盖：「{', '.join(missing)}」")
        for c in missing:
            print(f"        {c} 出现在: {', '.join(sorted(set(present[c])))}")
        print("        修法：把该站真实渲染形态加进 executor.py 的 price_re，并更新本守卫 SAMPLES。")
    if uncovered_kb:
        print(f"[WARN] KB 币种 {', '.join(uncovered_kb)} 在守卫里没有样本（请补充 SAMPLES 核实）")

    if args.selftest:
        rmb_failed = not dict((r[0], r[1]) for r in rows)["CNY"]
        print(f"\n[selftest] RMB/CNY 被判定为缺口 = {rmb_failed}（{'符合预期' if rmb_failed else '异常：守卫抓不到！'}）")
        print(f"[selftest] 旧正则整体结果 = {'FAIL（符合预期）' if not ok_all else 'PASS（异常！）'}")
        return 0 if (rmb_failed and not ok_all) else 1

    if not ok_all or missing:
        print("\n[FAIL] 币种白名单存在缺口，见上。")
        return 1
    print("\n[OK] price_re 覆盖全部已配置币种的官网渲染形态。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
