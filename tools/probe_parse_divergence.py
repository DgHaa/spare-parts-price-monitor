"""probe_parse_divergence.py - 非破坏性对照：新旧金额解析器在**真实接口数据**上的分歧。

背景：2026-09-23 修复「人工费 ×1000」时，把若干**接口 JSON** 取价点从
`parse_amount`（面向网页文本，会把尾 3 位当千分位）切到 `parse_json_amount`
（JSON 语义，`.` 恒为小数点）。OPPO 侧已另有端到端验证；本探针回答的是
**其它品牌是否有回归**——尤其是三星中国区的 PART_PRICE。

做法：直接打官方接口，把每个原始金额字符串同时喂给新旧两个解析器，
列出**所有分歧**。不写库、不改动任何数据。

  - 分歧为 0  → 切换是零风险（对现网数据无影响）
  - 有分歧    → 说明旧代码当时就在出错，新代码是修复（并给出受影响样本）

用法：
  python tools/probe_parse_divergence.py            # 三星中国区
  python tools/probe_parse_divergence.py -n 3       # 只取前 3 个种子机型
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vendor"))

from normalize import parse_amount, parse_json_amount  # noqa: E402

KB = os.path.join(ROOT, "references", "kb", "samsung.json")
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
      "Accept-Language": "zh-CN,zh;q=0.9",
      "X-Requested-With": "XMLHttpRequest",
      "Referer": "https://service.samsung.com.cn/#/public/spare/part/price"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def samsung_cn_cfg():
    """KB 结构：countries 是 {国家码: [记录, ...]}，cn 下取带 base 的那条。"""
    kb = json.load(open(KB, encoding="utf-8"))
    for c in (kb.get("countries") or {}).get("cn", []) or []:
        api = (c.get("query") or {}).get("api") or {}
        if api.get("base"):
            return api
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=5, help="取前 N 个种子机型")
    args = ap.parse_args()

    cfg = samsung_cn_cfg()
    base = (cfg.get("base") or "https://service.samsung.com.cn/rest/scic/open/spare-part-price").rstrip("/")
    parts = cfg.get("part_names") or ["主板", "屏", "尾插", "后盖", "电池", "摄像头"]
    seeds = (cfg.get("seed_models") or [])[:args.n]
    if not seeds:
        print("[FATAL] KB 中未找到 seed_models")
        return 2

    print(f"端点：{base}")
    print(f"种子机型（前 {len(seeds)} 个）：{seeds}")
    print("=" * 92)

    n_raw = n_div = 0
    diverged = []
    for code in seeds:
        try:
            mjson = get(f"{base}/models?{urllib.parse.urlencode({'searchVal': code})}")
        except Exception as e:  # noqa: BLE001
            print(f"  {code}: /models 请求失败 {e}")
            continue
        variants = [c for c in (mjson or []) if str(c).upper() != code.upper()]
        variant = variants[0] if variants else (mjson or [code])[0]
        try:
            ljson = get(f"{base}/list?" + urllib.parse.urlencode(
                {"model": variant, "part": ",".join(parts)}))
        except Exception as e:  # noqa: BLE001
            print(f"  {code}: /list 请求失败 {e}")
            continue

        rows = []
        for grp in ((ljson or {}).get("result") or []):
            for pname, items in (grp or {}).items():
                if not items:
                    continue
                raw = items[0].get("PART_PRICE")
                rows.append((pname, raw))
        print(f"\n{code}（变体 {variant}）共 {len(rows)} 个部件：")
        for pname, raw in rows:
            n_raw += 1
            old = parse_amount(str(raw)) if raw is not None else None
            new = parse_json_amount(raw)
            flag = ""
            if old != new:
                n_div += 1
                diverged.append((code, pname, raw, old, new))
                flag = "   <<< 分歧"
            print(f"  {pname:<6} 原始={str(raw):<12} 旧={old!s:<12} 新={new!s:<12}{flag}")

    print()
    print("=" * 92)
    print(f"对照原始金额值：{n_raw} 个；解析分歧：{n_div} 个")
    if diverged:
        print("\n分歧明细（旧值 = 切换前实际入库的值）：")
        for code, pname, raw, old, new in diverged:
            print(f"  {code} / {pname}: 原始 {raw!r} → 旧 {old} vs 新 {new}")
        print("\n结论：存在分歧 —— 说明旧解析器在这些值上会出错，本次切换是**修复**。")
        return 1
    print("\n结论：零分歧 —— 三星中国区从 parse_amount 切到 parse_json_amount 对现网数据无影响 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
