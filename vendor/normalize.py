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
    实测语料中不存在这种写法。
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
