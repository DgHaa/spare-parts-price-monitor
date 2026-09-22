#!/usr/bin/env python3
"""把 spare-parts-price skill 的外部依赖同步进仓库，保持自包含。

仓库原本硬依赖仓库外的三处文件：
  - ~/.workbuddy/skills/spare-parts-price/references/kb/*.json   （各品牌抓取配方 KB）
  - ~/.workbuddy/skills/spare-parts-price/references/calibration/ （标定数据/脚本）
  - ~/.workbuddy/skills/spare-parts-price/scripts/normalize.py    （金额解析唯一实现）

本工具把它们拷进仓库的 references/ 与 vendor/。
运行时代码已改为「仓库副本优先、skill 目录回退」，所以拷完即生效。

用法：
    python tools/sync_skill_deps.py            # 拷贝并打印差异
    python tools/sync_skill_deps.py --check    # 只检查是否有漂移，不写盘

⚠️ 方向警告（与 tools/sync_kb.py 相反）：
    本工具是 skill -> 项目（把 skill 依赖 vendoring 进仓库，让仓库自包含）。
    而 crawler/run.py 运行时实际读取的是 **skill** 目录的 references/kb
    （executor.load_record 的 KB_DIR 指向 skill 包内）。
    所以「改了项目 KB 想让抓取生效」应跑 tools/sync_kb.py（项目 -> skill）。
    切勿在本工具部署后随手跑 sync_skill_deps.py：它会用 skill 旧副本覆盖
    你在项目里新增/修改的 KB 条目（例如 apple/cn、vivo/cn、samsung/cn 曾因此
    被静默 [skip]）。本工具只应在「以 skill 为源、刷新仓库快照」时单向使用。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price")

# (源目录, 目标目录, 文件名 glob 列表)
JOBS = [
    (SKILL / "references" / "kb", ROOT / "references" / "kb", ["*.json"]),
    (SKILL / "references" / "calibration", ROOT / "references" / "calibration",
     ["*.json", "*_calibrate.py"]),
    (SKILL / "scripts", ROOT / "vendor", ["normalize.py"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告漂移，不写盘")
    args = ap.parse_args()

    if not SKILL.exists():
        print(f"[SKIP] skill 目录不存在：{SKILL}（本机未安装 skill 时属正常，仓库副本可独立运行）")
        return 0

    drift: list[str] = []
    for src_dir, dst_dir, globs in JOBS:
        if not src_dir.exists():
            print(f"[SKIP] 源目录缺失：{src_dir}")
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for g in globs:
            for src in sorted(src_dir.glob(g)):
                dst = dst_dir / src.name
                same = dst.exists() and sha(src) == sha(dst)
                if same:
                    continue
                drift.append(f"{dst.relative_to(ROOT)}  <-  {src}")
                if not args.check:
                    shutil.copy2(src, dst)

    if not drift:
        print("[OK] 仓库副本与 skill 目录一致，无漂移")
        return 0

    verb = "将更新" if args.check else "已更新"
    print(f"[{verb}] {len(drift)} 个文件：")
    for line in drift:
        print("   ", line)
    if args.check:
        print("（--check 模式，未写盘。去掉 --check 即执行同步）")
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
