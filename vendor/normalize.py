#!/usr/bin/env python3
"""normalize.py - 将本地价折算人民币(CNY)。

策略: 优先调用实时汇率 API(open.er-api.com，免 key)；失败则用内置快照表(标注日期)。
币种消歧: '$' 结合 country 判定 USD/CAD/AUD，不只看符号。

用法:
    python normalize.py --amount "189,00" --currency EUR --date 2026-08-27
    python normalize.py --amount 799 --currency USD --country us
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime

STATIC_RATES = {  # 1 单位外币 = ? CNY（示例快照 2026-08-27，运行时以 API 为准）
    "EUR": 7.85, "TRY": 0.21, "JPY": 0.048, "AED": 1.96,
    "MYR": 1.52, "MXN": 0.39, "USD": 7.20, "CNY": 1.0,
}

# $ 符号按国家歧义
DOLLAR_COUNTRY = {"us": "USD", "ca": "CAD", "au": "AUD", "sg": "USD"}


def resolve_currency(currency, country=None):
    c = (currency or "").strip().upper()
    if c in ("$", "DOLLAR"):
        return DOLLAR_COUNTRY.get((country or "").lower(), "USD")
    return c


def fetch_rate(currency):
    try:
        url = f"https://open.er-api.com/v6/latest/{currency}"
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
        return float(d["rates"]["CNY"]), "api"
    except Exception:
        return STATIC_RATES.get(currency), "static"


def parse_amount(s):
    """把各国金额写法解析为 float。

    **难点**：`.` 与 `,` 在不同地区含义相反，早期实现无条件 `replace(",", "")`
    （假定逗号=千分位）会把德语的小数逗号吃掉，让价格**放大 100 倍**：
    Apple 德国站 `€488,99` 被读成 48899（真实 488.99）。

    规则（按可靠性排序）：
      1. 两种分隔符都出现 → **最后出现的那个是小数点**，另一个是千分位。
         `1.234,56` → 1234.56（德）  `1,234.56` → 1234.56（美）
      2. 只有一种分隔符：
         - 出现多次 → 必是千分位（`1.234.567`）。
         - 只出现一次 → 小数位恰好 3 位则视为千分位（`1,234`→1234、`8.200`→8200），
           否则视为小数点（`488,99`→488.99、`79,5`→79.5）。
           价格小数极少超过 2 位，故该推断安全。
      3. 无分隔符 → 直读。

    已知取舍：形如 `1,234` 若真表示 1.234（三位小数价）会被读成 1234。
    网页文本里实测不存在这种写法；但**接口 JSON 里恰恰存在**（OPPO REBORN 的
    laborCostAmount 恒为 3 位小数，如 MYR "50.000"）—— 结构化 JSON 请改用
    parse_json_amount()。本函数只用于从**网页可见文本**里抠金额。

    ⚠️ 2026-09-23 事故：OPPO 各区域人工费用本函数解析，"50.000" 被读成 50000，
    全区域人工费放大 1000 倍（见 parse_json_amount 的说明）。
    """
    if s is None:
        return None
    s = str(s)
    # 少数地区（法/部分欧洲）用空格作千分位：`2 999,00`。仅当空格后正好 3 位数字时
    # 才合并，避免误伤 "iPhone 12 500" 这类"型号+数字"文本。
    s = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))", "", s)
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    num = m.group(0).rstrip(".,")
    has_dot, has_comma = "." in num, "," in num
    if has_dot and has_comma:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        thou = "," if dec == "." else "."
        num = num.replace(thou, "").replace(dec, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        if num.count(sep) > 1:                       # 千分位重复出现
            num = num.replace(sep, "")
        else:
            head, _, tail = num.partition(sep)
            num = num.replace(sep, "") if len(tail) == 3 else f"{head}.{tail}"
    try:
        return float(num)
    except ValueError:
        return None


def parse_json_amount(v):
    """解析**结构化 JSON** 里的金额字段（如 OPPO REBORN 的 "50.000" / "4499.00"）。

    与 parse_amount 的分工（切勿混用）：
      parse_amount      —— 面向**网页可见文本**。`1.234,56` 与 `1,234.56` 都合法，
                           需按语区推断谁是小数点，故带"单个分隔符且尾 3 位 = 千分位"
                           的启发式（`8.200` → 8200）。
      parse_json_amount —— 面向**接口 JSON**。JSON 数值语法规定小数点只能是 `.`，
                           逗号只可能是千分位，故无需任何推断：`,` 一律删除，
                           `.` 一律作小数点。

    为什么必须分开：OPPO REBORN 的 laborCostAmount 是**固定 3 位小数**的字符串
    （MYR "50.000"、TRY "1350.000"），交给 parse_amount 会被"尾 3 位 = 千分位"
    误判，人工费整体放大 1000 倍（2026-09-23 实测并修复）。

    与 _amount_or_none 的分工：那个是"直读 float + 丢掉 <=0"，用于小米/vivo 的
    单值字段；本函数保留 0、并容忍千分位与货币符号噪声，返回 None 表示无法解析。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    try:
        return float(m.group(0).rstrip(".,").replace(",", ""))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", required=True)
    ap.add_argument("--currency", required=True)
    ap.add_argument("--country")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    cur = resolve_currency(args.currency, args.country)
    amount = parse_amount(args.amount)
    if amount is None:
        sys.stderr.write("ERROR: 无法解析金额\n")
        sys.exit(1)
    rate, source = fetch_rate(cur)
    cny = amount * rate if rate else None
    out = {
        "amount": amount, "currency": cur, "rate_to_cny": rate,
        "cny": round(cny, 2) if cny is not None else None,
        "source": source, "date": args.date,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
