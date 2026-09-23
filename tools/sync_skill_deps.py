#!/usr/bin/env python3
"""保持「仓库 ↔ skill 镜像」一致：方向明确、默认只读、拒绝覆盖更新的文件。

⚠️ 本项目有三类外部依赖，**真源方向各不相同**——混淆会造成「改了半天不生效」：

  | 文件 | 运行时真正的源 | 谁负责同步 | 方向 |
  |---|---|---|---|
  | `references/kb/*.json`          | **repo**（2026-09-23 实测：vendor/ 优先，KB_DIR 解析到仓库） | `tools/sync_kb.py` | repo → skill |
  | `vendor/executor.py`            | **repo**（core.py 把 vendor/ 插 sys.path 最前） | 本工具 | repo → skill |
  | `vendor/normalize.py`           | **repo**（同上）                            | 本工具 | repo → skill |
  | `references/calibration/*`      | **repo**（_paths.py 全相对路径，仓库为家）   | 本工具 | repo → skill |

  实测判据：`from crawler.run import run_all; import executor` →
  `vendor/executor.py` + `<仓库>/references/kb`。故**四类文件在仓库内运行时
  都以仓库为真源**，改动立即生效；sync 系列工具统一只服务于「保持 skill 镜像/
  脱离仓库时可用」，不再是「不同步就不生效」。

  KB 仍刻意**不**由本工具处理：两个工具管同一批文件，混用是历史事故来源
  （曾把项目里新增的 apple/cn 等 KB 条目用 skill 旧副本覆盖回 [skip]）。

用法：
  python tools/sync_skill_deps.py            # 默认只读：报告漂移与方向（有漂移 exit 1）
  python tools/sync_skill_deps.py --check    # 同默认（兼容旧写法）
  python tools/sync_skill_deps.py --apply    # 部署 repo → skill（刷新 skill 镜像）
  python tools/sync_skill_deps.py --pull     # 拉取 skill → repo（仅当确知 skill 侧是新版）
  python tools/sync_skill_deps.py --force    # 允许覆盖「比源更新」的目标（默认拒绝）

安全阀（本工具最重要的设计）：若目标文件比源文件**更新**，默认**拒绝复制**并 exit 2。
这通常意味着有人在**不生效的那一侧**改了代码——典型场景：自愈维护 Agent 按旧 prompt
去改 skill 的 `scripts/executor.py`，而运行时的真源是仓库 `vendor/executor.py`，
于是「改了、没报错、也没生效」。此时请先 Read 两侧内容确认，再用 --pull / --force 定方向。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price")

# (真源目录, 镜像目录, 文件名 glob 列表, 说明)
JOBS = [
    (ROOT / "vendor", SKILL / "scripts",
     ["normalize.py", "executor.py"],
     "抓取引擎 + 金额解析（core.py 中 vendor/ 优先 → 仓库生效）"),
    (ROOT / "references" / "calibration", SKILL / "references" / "calibration",
     ["_paths.py", "*_calibrate.py", "*.json"],
     "标定脚本与标定数据（_paths.py 全相对路径 → 仓库为家）"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else "-"


def newer_of(a: Path, b: Path) -> Path | None:
    """返回 mtime 较新的那个；a/b 均存在才有意义。"""
    if not (a.exists() and b.exists()):
        return None
    return a if a.stat().st_mtime_ns > b.stat().st_mtime_ns else b


def main() -> int:
    ap = argparse.ArgumentParser(
        description="仓库 ↔ skill 镜像同步（方向：repo → skill）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只报告漂移，不写盘（默认行为）")
    ap.add_argument("--apply", action="store_true", help="部署：仓库 → skill")
    ap.add_argument("--pull", action="store_true", help="拉取：skill → 仓库（谨慎）")
    ap.add_argument("--force", action="store_true", help="覆盖比源更新的目标（默认拒绝）")
    args = ap.parse_args()

    if args.apply and args.pull:
        print("[ERR] --apply 与 --pull 互斥，请二选一")
        return 2

    if not SKILL.exists():
        print(f"[SKIP] skill 目录不存在：{SKILL}")
        print("       （本机未安装 skill 时属正常，仓库副本可独立运行）")
        return 0

    # 1) 收集并分类漂移
    same: list[str] = []
    only_repo: list[tuple[Path, Path]] = []
    only_skill: list[tuple[Path, Path]] = []
    differ: list[tuple[Path, Path, str]] = []   # (repo, skill, 谁更新)

    for src_dir, dst_dir, globs, desc in JOBS:
        for g in globs:
            for repo_file in sorted(src_dir.glob(g)):
                skill_file = dst_dir / repo_file.name
                if not skill_file.exists():
                    only_repo.append((repo_file, skill_file))
                elif sha(repo_file) == sha(skill_file):
                    same.append(str(repo_file.relative_to(ROOT)))
                else:
                    n = newer_of(repo_file, skill_file)
                    differ.append((repo_file, skill_file,
                                   "仓库较新" if n == repo_file else "skill 较新"))

    # 全部名字一并列出（含只在 skill 侧的）
    for src_dir, dst_dir, globs, desc in JOBS:
        for g in globs:
            for skill_file in sorted(dst_dir.glob(g)):
                if not (src_dir / skill_file.name).exists():
                    only_skill.append((src_dir / skill_file.name, skill_file))

    total_drift = len(only_repo) + len(only_skill) + len(differ)

    # 2) 只读模式
    if not (args.apply or args.pull):
        if total_drift == 0:
            print(f"[OK] 仓库与 skill 镜像一致（{len(same)} 个文件），无漂移")
            return 0
        print(f"[DRIFT] 共 {total_drift} 处不一致：")
        for repo_file, skill_file in only_repo:
            print(f"    仓库独有   {repo_file.relative_to(ROOT)}   （skill 侧缺失）")
        for _, skill_file in only_skill:
            print(f"    skill 独有 {skill_file.name}   （仓库缺失，通常为截图等产物）")
        for repo_file, skill_file, who in differ:
            print(f"    内容不同   {repo_file.relative_to(ROOT)}   [{who}]  "
                  f"repo={sha(repo_file)} skill={sha(skill_file)}")
        print("\n  部署到 skill：python tools/sync_skill_deps.py --apply")
        print("  拉回仓库    ：python tools/sync_skill_deps.py --pull   （谨慎）")
        return 1

    # 3) 写盘模式（带安全阀）
    direction = "repo -> skill" if args.apply else "skill -> repo"
    print(f"[{direction}] 开始同步（{'允许强制覆盖' if args.force else '拒绝覆盖更新的目标'}）")

    refused: list[str] = []
    copied = 0

    if args.apply:
        pairs = [(r, s) for r, s in only_repo] + [(r, s) for r, s, _ in differ]
        for src, dst in pairs:
            if dst.exists() and not args.force and dst.stat().st_mtime_ns > src.stat().st_mtime_ns:
                refused.append(f"{src.name}（skill 侧更新：{dst}）")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            print(f"    [copy] {src.relative_to(ROOT)}  ->  skill")
    else:
        pairs = [(r, s) for r, s in only_skill] + [(r, s) for r, s, _ in differ]
        for src, dst in pairs:          # src=skill 侧, dst=仓库侧
            if dst.exists() and not args.force and dst.stat().st_mtime_ns > src.stat().st_mtime_ns:
                refused.append(f"{dst.name}（仓库侧更新：{dst.relative_to(ROOT)}）")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            print(f"    [copy] skill  ->  {dst.relative_to(ROOT)}")

    print(f"\n[完成] 已复制 {copied} 个文件")

    if refused:
        print(f"[拒绝] {len(refused)} 个文件因「目标比源更新」未覆盖：")
        for line in refused:
            print(f"    - {line}")
        print("    这通常意味着一侧的修改落在了**不生效的那份**上。")
        print("    请先 Read 两侧内容确认哪边为准，再用 --pull / --apply --force 定方向。")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
