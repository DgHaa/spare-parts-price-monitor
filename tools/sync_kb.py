#!/usr/bin/env python3
"""部署 项目 references/kb -> skill references/kb，并检测双向漂移。

== 为什么需要这个工具（已踩过的坑）==

  crawler/run.py 通过 crawler/core.py 把 skill 的 scripts/ 目录注入 sys.path，
  然后 `import executor` 解析到 **skill** 里的 scripts/executor.py。
  而 executor.load_record 的 KB_DIR = <skill>/references/kb
  （executor.py 位于 skill 包内，parent.parent 即 skill 根）。

  因此：
    - 项目 references/kb/*.json  = git 源（你编辑、提交的地方）
    - skill references/kb/*.json = 运行时真正被读取的副本

  只改项目 KB 而不同步到 skill，crawler.run 会因 load_record 读不到而
  静默 [skip] 该 品牌×国家（vivo/cn 曾因此被误 skip，排查良久）。

  本工具把「项目 -> skill」的部署固化下来，并提供 --check 漂移检测，
  在跑 crawler.run 之前先确认 KB 已部署，杜绝静默跳过。

== 与 tools/sync_skill_deps.py 的分工 ==

  两者管的是**不同文件**，方向也**相反**，请勿混用（2026-09-22 起明确划分）：

    本工具          ：references/kb/*.json          仓库 -> skill（KB 是 skill 生效）
    sync_skill_deps ：vendor/executor.py            仓库 -> skill（executor 是仓库生效）
                      vendor/normalize.py           仓库 -> skill
                      references/calibration/*      仓库 -> skill

  即：**KB 的真源是仓库、但要部署到 skill 才生效**；executor/normalize/标定脚本的真源
  也是仓库、且**仓库副本直接生效**（core.py 把 vendor/ 插在 sys.path 最前）。
  两者的共同点：都请在**仓库**改，改完再同步镜像。

  sync_skill_deps.py 默认只读，且会**拒绝覆盖比源更新的文件**——若它报 exit 2，
  通常说明有人改在了不生效的那一侧（如改了 skill 的 scripts/executor.py）。

用法：
  python tools/sync_kb.py --check   # 只检测 drift，不写盘（推荐跑 crawler.run 前先跑）
  python tools/sync_kb.py           # 部署项目 KB -> skill（仅复制有差异的文件）
  python tools/sync_kb.py --all     # 全量覆盖（含 skill 比项目新的文件；谨慎，不删 skill 独有）
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO_KB = Path(__file__).resolve().parents[1] / "references" / "kb"
SKILL_KB = Path(r"C:/Users/Dong/.workbuddy/skills/spare-parts-price/references/kb")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告漂移，不写盘")
    ap.add_argument("--all", action="store_true",
                    help="全量覆盖（含 skill 比项目新的文件）；不会删除 skill 独有文件")
    ap.add_argument("--force", action="store_true",
                    help="对 conflict-skill-newer 也强制用 repo 覆盖（会丢弃 skill 较新编辑，谨慎）")
    args = ap.parse_args()

    if not SKILL_KB.exists():
        print(f"[SKIP] skill KB 目录不存在：{SKILL_KB}（本机未安装 skill 时属正常）")
        return 0

    REPO_KB.mkdir(parents=True, exist_ok=True)
    repo_files = {p.name: p for p in REPO_KB.glob("*.json")}
    skill_files = {p.name: p for p in SKILL_KB.glob("*.json")}
    names = sorted(set(repo_files) | set(skill_files))

    drift: list[tuple[str, str, str, str]] = []
    for n in names:
        rp, sp = repo_files.get(n), skill_files.get(n)
        rs, ss = sha(rp), sha(sp)
        if rs == ss:
            continue
        if rp and not sp:
            drift.append(("add", n, rs, ss))             # repo 独有 -> 部署
        elif sp and not rp:
            drift.append(("only-in-skill", n, rs, ss))    # skill 独有 -> 不动
        else:
            # 两边都存在且内容不同：以 mtime 判定哪边较新，避免把 skill 较新的
            # 合法编辑覆盖回旧副本（google.json 曾出现 skill 比 repo 新）。
            rp_mt = rp.stat().st_mtime
            sp_mt = sp.stat().st_mtime
            if rp_mt >= sp_mt:
                drift.append(("deploy", n, rs, ss))       # repo 较新/相等 -> 部署
            else:
                drift.append(("conflict-skill-newer", n, rs, ss))  # skill 较新 -> 需人工核对

    if not drift:
        print("[OK] 项目 KB 与 skill KB 一致，无需同步")
        return 0

    if args.check:
        print(f"[DRIFT] {len(drift)} 个文件存在差异：")
        for kind, n, rs, ss in drift:
            print(f"   {kind:22} {n}  repo={rs} skill={ss}")
        print("（--check 模式未写盘；去掉 --check 即部署 repo -> skill）")
        print("  conflict-skill-newer 表示 skill 比 repo 新，默认不覆盖，需人工核对/--force")
        return 1

    SKILL_KB.mkdir(parents=True, exist_ok=True)
    done = 0
    for kind, n, rs, ss in drift:
        if kind in ("add", "deploy"):
            shutil.copy2(repo_files[n], SKILL_KB / n)
            print(f"[DEPLOY] {n}  repo={rs} -> skill")
            done += 1
        elif kind == "conflict-skill-newer":
            if args.force:
                shutil.copy2(repo_files[n], SKILL_KB / n)
                print(f"[FORCE ] {n}  repo={rs} -> skill（--force 覆盖较新的 skill 副本！）")
                done += 1
            else:
                print(f"[CONFLICT] {n}  skill 比 repo 新（skill={ss} repo={rs}）；未覆盖，请人工核对")
        else:  # only-in-skill：默认保留，避免误删 skill 独有 KB
            print(f"[WARN-only-skill] {n}  skill={ss} 但项目无此文件；未处理（勿误删 skill 独有 KB）")
    print(f"[DONE] 已部署 {done} 个文件到 skill KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
