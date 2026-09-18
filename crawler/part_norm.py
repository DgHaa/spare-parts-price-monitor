"""备件名归一化引擎（方案 A）。

把各区域官网原文的备件名（中/英/德/日/土）归一化为统一的中文 `canonical_name`，
并拆出 `canonical_spec`（参与分组，如主板 8G+256G、适配器 11V 7.3A）与
`variant`（不参与分组，如颜色/限定版/优惠标识）。

设计要点
--------
- **原文保留**：`parts.name` 不动，归一化结果写到 `parts.canonical_name` 等新列，
  任何一条都能回溯到官网原文。
- **规格与颜色分离**：`（12G 256G）` 是规格 → 进分组键；`(晶钻粉)` 是颜色 → 不进分组键。
  实测 91 组颜色变体中 88 组价格完全相同，剥离颜色近乎无损。
- **像素不参与分组**：`Rear Main Camera（50M）` 与 `后置主摄像头(50M)` 是同一备件，
  手机只有一个主摄，像素差异是机型差异而非备件差异。
- **跨品类归并**：少数条目需要改 part_type（如 `其他/Receiver` → `听筒`），
  否则同品牌跨区域仍无法合并。由别名表的 `ct` 字段声明。

不做的事
--------
- 不在别名表里的名字**不猜**：回退为剥离规格/颜色后的基名，`norm_conf=0` 待人工复核。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ALIAS_PATH = Path(__file__).resolve().parent / "part_alias.json"

# 出现在括号中但属于"备件身份"的词——不剥离，与基名一起构成查表键
ROLE_TOKENS = {
    "组件", "副摄", "主摄", "广角", "超广角", "长焦", "超长焦", "微距", "人像",
    "景深", "潜望长焦", "潜望", "特写长焦", "浮动长焦", "显微", "复古",
    "黑白多光谱", "多光谱", "超光感潜望长焦", "虚化", "深度", "黑白", "胶片",
    "主屏", "外屏", "上", "下", "左", "右", "片耳", "両耳", "含上电池盖",
}

# 尾部营销后缀（vivo 促销/活动机）
FLAVOR_WORD = re.compile(r"\s+(Activity|Etkinlik)$", re.I)

# 折扣/优惠类变体：不是"同价换个颜色"，而是同部件的**另一个价格档**
# （如小米 `内屏组件` ¥4139 vs `内屏（优惠屏）` ¥1240）。混进同一行会严重误导
# 跨国比价，故升级为独立的 canonical 维度。
DISCOUNT_MARK = re.compile(r"优惠|First time repair", re.I)

# 后缀括号组（全角/半角混用、内部允许空格）
SUFFIX_BRACKET = re.compile(r"[（(]\s*([^（()）]*?)\s*[)）]\s*$")
# 前缀方括号角色（日文站：[メインカメラ] 約5,000万画素）
PREFIX_BRACKET = re.compile(r"^\[([^\]]+)\]\s*")

# ---- 规格识别规则（顺序敏感：先像素，再 RAM/ROM，再功率，最后其它白名单）----
RE_MP = re.compile(r"^(\d+(?:\.\d+)?)\s*[MmＭ]$")
RE_MP_JP = re.compile(r"^約?([\d,]+)\s*万画素$")
RE_MP_JP_OKU = re.compile(r"^約?([\d,.]+)\s*億画素$")
RE_RAMROM_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|T|G|MB)\b", re.I)
RE_POWER = re.compile(r"^(\d+(?:\.\d+)?)\s*V\s*(\d+(?:\.\d+)?)\s*A$", re.I)
RE_SPEC_EXTRA = [
    re.compile(r"^\d+\s*W\b.*$"),          # 100W 双口
    re.compile(r"^.*膜$"),                 # TPU膜 / AR膜
    re.compile(r"^第?\d+代$"),
]

_UNIT_BYTES = {"MB": 1, "G": 1000, "GB": 1000, "T": 1000000, "TB": 1000000}


@dataclass
class NormResult:
    canonical: str
    canonical_type: str | None = None   # 非空表示需要改写 part_type
    spec: str = ""
    variant: str = ""
    lang: str = "zh"
    rule: str = "fallback"              # alias | alias_full | fallback
    conf: int = 1
    base_key: str = ""
    extras: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 别名加载
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _alias_index() -> dict[tuple[str, str], dict]:
    raw = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    idx: dict[tuple[str, str], dict] = {}
    for e in raw["entries"]:
        bases = e["base"]
        if isinstance(bases, str):
            bases = [bases]
        for b in bases:
            k = (e["pt"], _key(b))
            if k in idx and idx[k]["canonical"] != e["canonical"]:
                raise ValueError(f"别名冲突 {k}: {idx[k]['canonical']} vs {e['canonical']}")
            idx[k] = {"canonical": e["canonical"], "ct": e.get("ct")}
    return idx


@lru_cache(maxsize=1)
def alias_version() -> str:
    raw = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    return str(raw.get("version", "?"))


def alias_rows() -> list[tuple[str, str, str, str, str]]:
    """展开为 part_alias 表行：(part_type, base_key, canonical, canonical_type, lang_note)。"""
    out = []
    raw = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    for e in raw["entries"]:
        bases = e["base"]
        if isinstance(bases, str):
            bases = [bases]
        for b in bases:
            out.append((e["pt"], _key(b), e["canonical"], e.get("ct") or "", _detect_lang(b)))
    return out


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _key(s: str) -> str:
    """构造查表键：折叠空白、括号统一为全角。"""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = s.replace("(", "（").replace(")", "）")
    return s


def _detect_lang(s: str) -> str:
    if re.search(r"[ぁ-んァ-ヶ]", s):
        return "ja"
    if re.search(r"[ıİşŞğĞçÇöÖüÜ]", s):
        return "tr"
    if re.search(r"[\u4e00-\u9fff]", s):
        return "zh"
    if re.search(r"\b(Schaden|Rückglas|Rückkamera|Displayschaden|Sonstiger|Batterieservice)\b", s, re.I):
        return "de"
    return "en"


def _extract_ramrom(g: str) -> str | None:
    toks = RE_RAMROM_TOKEN.findall(g)
    if not toks:
        return None
    vals = [(float(n), u.upper()) for n, u in toks]
    if len(vals) >= 2:
        vals.sort(key=lambda x: x[0] * _UNIT_BYTES.get(x[1], 1000))
        (rn, ru), (sn, su) = vals[0], vals[-1]
        return f"{_fmt(rn)}{ru[0]}+{_fmt(sn)}{su[0]}"
    n, u = vals[0]
    return f"{_fmt(n)}{u[0]}"


def _fmt(v: float) -> str:
    return str(int(v)) if abs(v - int(v)) < 1e-9 else str(v)


def _mp_spec(g: str) -> str | None:
    """把各种写法的像素归一为 `50M` / `200M` / `2M`。"""
    m = RE_MP.match(g)
    if m:
        return f"{_fmt(float(m.group(1)))}M"
    m = RE_MP_JP.match(g)          # 約5,000万画素 -> 50M
    if m:
        return f"{_fmt(float(m.group(1).replace(',', '')) / 100)}M"
    m = RE_MP_JP_OKU.match(g)      # 約2億画素 -> 200M
    if m:
        return f"{_fmt(float(m.group(1).replace(',', '')) * 100)}M"
    return None


def _classify_group(g: str) -> tuple[str, str]:
    """把一个括号组分类为 ('spec'|'variant', 值)。

    像素**进 spec**：实测 OPPO 中国站用"有无像素后缀"区分廉价件与模组
    （同机型 `后置摄像头` ¥19 vs `后置摄像头（64M）` ¥320），若把像素丢弃会把
    两个不同条目错误合并。像素归一后再比较（5,000万画素 == 50M），
    故 `Rear Main Camera（50M）` 与 `后置主摄像头(50M)` 仍能跨国合并。
    """
    g = g.strip()
    mp = _mp_spec(g)
    if mp:
        return "spec", mp
    rr = _extract_ramrom(g) if re.search(r"\d", g) else None
    if rr and re.search(r"(GB|TB|RAM|ROM|\d\s*[GT]\b)", g, re.I):
        return "spec", rr
    m = RE_POWER.match(g)
    if m:
        return "spec", f"{_fmt(float(m.group(1)))}V {_fmt(float(m.group(2)))}A"
    for pat in RE_SPEC_EXTRA:
        if pat.match(g):
            return "spec", g
    return "variant", g


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def split_name(name: str) -> tuple[str, list[str], list[str], list[str]]:
    """拆解原名 → (基名, 角色词列表, 规格列表, 变体列表)。"""
    s = (name or "").strip()
    s = FLAVOR_WORD.sub("", s).strip()

    prefix_roles: list[str] = []
    m = PREFIX_BRACKET.match(s)
    if m:
        prefix_roles.append(m.group(1).strip())
        s = s[m.end():].strip()

    groups: list[str] = []
    while True:
        m = SUFFIX_BRACKET.search(s)
        if not m:
            break
        groups.insert(0, m.group(1).strip())
        s = s[: m.start()].strip()

    roles: list[str] = []          # 仅"后缀括号"里的角色词（前缀方括号另作基名）
    specs: list[str] = []
    variants: list[str] = []
    for g in groups:
        if g in ROLE_TOKENS:
            roles.append(g)
            continue
        kind, val = _classify_group(g)
        if kind == "spec":
            specs.append(val)
        elif val:
            variants.append(val)

    # 前缀方括号直接作为基名（日文站写法：[メインカメラ] 約5,000万画素）
    if prefix_roles:
        return _key(f"[{prefix_roles[0]}]"), roles, specs, variants
    return _key(s), roles, specs, variants


def normalize(name: str, part_type: str | None = None) -> NormResult:
    """归一化单条备件名。part_type 命中别名表时返回中文 canonical。"""
    base, roles, specs, variants = split_name(name)
    joined_roles = f"（{'+'.join(roles)}）" if roles else ""
    cand_keys = []
    if part_type:
        cand_keys.append((part_type, _key(base) + joined_roles))
    cand_keys.append((part_type, _key(base)))

    idx = _alias_index()
    hit = None
    for k in cand_keys:
        if k in idx:
            hit = idx[k]
            break

    spec = " ".join(specs)
    variant = " ".join(variants)
    discount = bool(DISCOUNT_MARK.search(variant)) or bool(DISCOUNT_MARK.search(base))
    if discount:
        # 折扣档独立成行：canonical 追加（优惠），variant 里不再重复
        variant = " ".join(v for v in variants if not DISCOUNT_MARK.search(v))
    if hit:
        canonical = _mark_discount(hit["canonical"], discount)
        return NormResult(
            canonical=canonical,
            canonical_type=hit["ct"] or None,
            spec=spec,
            variant=variant,
            lang=_detect_lang(name),
            rule="alias",
            conf=1,
            base_key=cand_keys[0][1],
        )
    return NormResult(
        canonical=_mark_discount(base, discount),
        canonical_type=None,
        spec=spec,
        variant=variant,
        lang=_detect_lang(name),
        rule="fallback",
        conf=0,
        base_key=_key(base) + joined_roles,
    )


def _mark_discount(canonical: str, discount: bool) -> str:
    """折扣档加后缀；canonical 里已含"优惠"字样（别名表已标注）的不重复加。"""
    if not discount or "优惠" in canonical:
        return canonical
    return canonical + "（优惠）"


def norm_key_row(name: str, part_type: str | None = None) -> tuple:
    """便于批量回填：返回 (canonical, canonical_type, spec, variant, lang, rule, conf, base_key)。"""
    r = normalize(name, part_type)
    return (r.canonical, r.canonical_type or "", r.spec, r.variant,
            r.lang, r.rule, r.conf, r.base_key)
