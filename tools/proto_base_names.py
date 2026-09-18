"""原型：把全部备件名收敛为「基名 + 规格 + 变体」，用于设计别名表。

只用于一次性分析，不是最终模块。
"""
import re
import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

# 角色词：出现在括号里时属于"备件身份"的一部分，不剥离
ROLE = {
    "组件", "副摄", "主摄", "广角", "超广角", "长焦", "超长焦", "微距", "人像",
    "景深", "潜望长焦", "潜望", "特写长焦", "浮动长焦", "显微", "复古",
    "黑白多光谱", "多光谱", "超光感潜望长焦", "主摄像头", "虚化", "深度", "黑白",
    "胶片", "后置", "前置", "内屏", "外屏", "主屏", "副板", "含上电池盖",
    "上", "下", "左", "右", "左右", "片耳", "有线充版", "无线充版",
}
# 非角色但需要保留语义的限定词（这些会和基名一起进别名表）
KEEPISH = {"组件"}

BRACKET = re.compile(r"[（(]\s*([^（()）]*?)\s*[)）]\s*$")
FLAVOR_WORD = re.compile(r"\s+(Activity|Etkinlik)$", re.I)


def split_groups(name):
    """从尾部反复剥离括号组，返回 (base, groups[])。"""
    groups = []
    s = name.strip()
    s = FLAVOR_WORD.sub("", s)          # vivo 促销后缀
    while True:
        m = BRACKET.search(s)
        if not m:
            break
        inner = m.group(1).strip()
        s = s[: m.start()].strip()
        groups.insert(0, inner)
    return s, groups


def main():
    c = sqlite3.connect("spare_parts.db")
    rows = c.execute(
        "SELECT p.part_type, p.name, COUNT(*) n FROM parts p "
        "JOIN models m ON m.id=p.model_id JOIN brands b ON b.id=m.brand_id "
        "GROUP BY p.part_type, p.name"
    ).fetchall()
    c.close()

    bases = Counter()
    kept_role = Counter()
    detail = {}
    for pt, name, n in rows:
        base, groups = split_groups(name)
        roles = [g for g in groups if g in ROLE]
        if roles:
            kept_role[(pt, base, tuple(roles))] += n
        bases[(pt, base)] += n
        detail.setdefault((pt, base), []).append((name, groups, n))

    print("原始 (part_type,name) 组合: %d" % len(rows))
    print("剥离后 (part_type,base) 组合: %d" % len(bases))
    print("含角色括号、需特判的组: %d" % len(kept_role))
    print()
    print("===== 剥离后基名清单（按 part_type） =====")
    cur = None
    for (pt, base), n in sorted(bases.items()):
        if pt != cur:
            print("\n-- %s --" % pt)
            cur = pt
        print("   %6d  %s" % (n, base))
    print()
    print("===== 含角色括号、需在别名表中特判的 =====")
    for (pt, base, roles), n in sorted(kept_role.items()):
        print("   %6d  [%s] %s | 角色=%s" % (n, pt, base, "+".join(roles)))


if __name__ == "__main__":
    main()
