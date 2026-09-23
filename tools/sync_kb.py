#!/usr/bin/env python3
"""部署 项目 references/kb -> skill references/kb，并检测双向漂移。

== 运行时到底读哪份 KB（2026-09-23 实测校正，此前文档写反了）==

  结论：**crawler.run 读的是仓库 KB**，不是 skill KB。

  实测（判据是 import 结果，不是推理）：
      $ python -c "from crawler.run import run_all; import executor; print(executor.__file__, executor.KB_DIR)"
      C:\\...\\spare-parts-monitor\\vendor\\executor.py
      C:\\...\\spare-parts-monitor\\references\\kb

  原因：crawler/core.py 按 (SKILL_CALIB, SKILL_SCRIPTS, VENDOR) 逆序 insert(0)，
  最终 **vendor/ 排在最前**（源码注释即「仓库内已同步的副本优先」），
  故 `import executor` 命中仓库 vendor/executor.py，
  其 KB_DIR = <仓库>/references/kb。

  ⚠️ 本工具旧版文档称「import executor 解析到 skill 的 scripts/executor.py，
  不同步到 skill 就会被静默 [skip]」——那是 vendor/ 提权**之前**的状态，
  现已不成立。历史事故（vivo/cn 被误 skip）也属于那个时期。

  那么本工具还有什么用？——**保持 skill 作为独立副本可用**：
    - 当仓库 vendor/ 缺失时，core.py 回退到 SKILL_SCRIPTS，
      此时 KB_DIR 才解析到 <skill>/references/kb，读的就是本工具部署的副本；
    - 该 skill 被**其它项目**单独调用时（不经过本仓库），走的是它自己的 scripts/ + KB。

  即：KB 的真源始终是仓库；部署是为了「脱离仓库时仍然一致」，
  **不再**是为了让 crawler.run 生效。

== 与 tools/sync_skill_deps.py 的分工 ==
  （两者管的文件不重叠，可以放心各自运行；旧文档所称「方向相反」的说法同样已被上述
   实测校正——在仓库内运行时，vendor/executor.py、vendor/normalize.py 与
   references/kb/*.json **全部**以仓库为真源。sync 系列工具统一服务于「镜像 skill」。）

  sync_skill_deps.py 默认只读，且会**拒绝覆盖比源更新的文件**——若它报 exit 2，
  通常说明有人改在了不生效的那一侧（如改了 skill 的 scripts/executor.py）。

用法：
  python tools/sync_kb.py --check   # 只检测 drift，不写盘
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
