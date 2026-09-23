#!/usr/bin/env python3
"""test_amount_parse.py — 金额解析「分工」的回归测试（不联网，纯函数）。

== 为什么需要这个测试（2026-09-23 事故）==
OPPO REBORN 接口返回的 laborCostAmount 是**固定 3 位小数**的 JSON 字符串
（实测 MYR "50.000" / TRY "1350.000" / AED "40.000" / MXN "650.000"），
却被交给了面向**网页文本**的 parse_amount()。后者的启发式
「单个分隔符且尾 3 位 = 千分位」把 "50.000" 读成 **50000** ——
全区域人工费放大 1000 倍（my 1,053 行、cn 2,182 行、ae/mx/tr 合计 1,982 行）。

根因不是某个函数写错了，而是**两套不同语境的解析器被混用**：
  parse_amount      —— 网页可见文本。`1.234,56`（德式）与 `1,234.56`（美式）
                       都合法，必须按语区推断谁是小数点，故**必须保留**
                       "尾 3 位 = 千分位"的推断（否则德国站 `8.200` 会读成 8.2）。
  parse_json_amount —— 接口 JSON。JSON 数值语法规定小数点只能是 `.`、逗号只能是
                       千分位，因此**不做任何推断**，`50.000` 就是 50。

本测试同时锁住两侧：新函数必须对，旧函数必须保持它原本的（对文本正确的）行为 ——
包括那条"看起来像 bug"的 `parse_amount("50.000") == 50000`，它是**预期**行为，
正因如此 JSON 才必须走另一个函数。谁要是把它"顺手修好"，德语区文本解析会立刻坏掉。

用法：python tools/test_amount_parse.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor"))
from normalize import parse_amount, parse_json_amount  # noqa: E402

FAILS = []


def check(fn, raw, want, label, note=""):
    try:
        got = fn(raw)
    except Exception as e:  # noqa: BLE001
        got = f"{type(e).__name__}: {e}"
    ok = (got == want) or (isinstance(got, float) and isinstance(want, float)
                           and abs(got - want) < 1e-9)
    print(f"  {'✓' if ok else '✗'} {label:<10} {fn.__name__}({raw!r}) = {got!r}"
          + ("" if ok else f"   期望 {want!r}") + (f"   {note}" if note else ""))
    if not ok:
        FAILS.append(f"{fn.__name__}({raw!r}) = {got!r}，期望 {want!r}")


def main():
    print("=" * 88)
    print("A) parse_json_amount —— 修复本次事故（接口 3 位小数的人工费）")
    print("=" * 88)
    for raw, want in [("50.000", 50.0), ("1350.000", 1350.0),
                      ("650.000", 650.0), ("240.000", 240.0),
                      ("40.000", 40.0), ("80.000", 80.0), ("0.000", 0.0)]:
        check(parse_json_amount, raw, want, "REBORN人工费",
              "← 旧实现读成 ×1000" if float(raw) != 0 else "0 值不受影响")

    print()
    print("=" * 88)
    print("B) parse_json_amount —— 不得误伤常规 JSON 价（2 位小数 / 千分位）")
    print("=" * 88)
    for raw, want in [("4499.00", 4499.0), ("89.00", 89.0), ("127.26", 127.26),
                      ("2380.0", 2380.0), ("176000.00", 176000.0),
                      ("1,234.56", 1234.56), ("2,999.00", 2999.0), ("50", 50.0)]:
        check(parse_json_amount, raw, want, "OPPO零售价")

    print()
    print("=" * 88)
    print("C) parse_amount —— 网页文本语义必须原样保留（德式/美式/千分位）")
    print("=" * 88)
    for raw, want, note in [("488,99", 488.99, "Apple 德国站小数逗号（曾放大 100 倍）"),
                            ("1.234,56", 1234.56, "德式"),
                            ("1,234.56", 1234.56, "美式"),
                            ("8.200", 8200.0, "德式千分位：不可退化成 8.2"),
                            ("79,5", 79.5, "德式一位小数"),
                            ("₺14.300", 14300.0, "土站千分位")]:
        check(parse_amount, raw, want, "网页文本", note)

    print()
    print("=" * 88)
    print("D) 关键：parse_amount 对 '50.000' 的行为是**预期**的（固化，勿「修好」）")
    print("=" * 88)
    print("  说明：`50.000` 在德语文本语境里确实可读作 50000（点=千分位）。")
    print("       这个歧义无法在纯文本层消除 —— 所以 JSON 走另一个函数，")
    print("       而不是把 parse_amount 改得对 JSON 友好（那会毁掉德语区解析）。")
    check(parse_amount, "50.000", 50000.0, "文本语义")
    check(parse_amount, "1350.000", 1350000.0, "文本语义")

    print()
    print("=" * 88)
    print("E) 边界：None / 空串 / 非数字 / 原生数值类型")
    print("=" * 88)
    for raw in (None, "", "   ", "abc", "—", "-"):
        check(parse_json_amount, raw, None, "不可解析")
    check(parse_json_amount, 50.0, 50.0, "float 直通")
    check(parse_json_amount, 50, 50.0, "int 直通")
    check(parse_json_amount, " ¥1,234.50 ", 1234.5, "带符号噪声")

    print()
    print("=" * 88)
    print(f"结果：{'全部通过 ✓' if not FAILS else f'存在 {len(FAILS)} 个失败 ✗'}")
    for f in FAILS:
        print("   -", f)
    print("=" * 88)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
