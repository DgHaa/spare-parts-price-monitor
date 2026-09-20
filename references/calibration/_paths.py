#!/usr/bin/env python3
"""标定脚本的共用路径解析 —— 全部相对，仓库可整体搬迁。

目录约定（相对于本文件 references/calibration/_paths.py）：

    <repo_root>/
      references/
        kb/                品牌抓取配方 json
        calibration/       标定脚本（本目录）+ 产出的 *_cal.json
      output/
        evidence/          截图等取证产物

用法（各标定脚本开头）：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _paths import find_chromium, EVIDENCE_DIR, OUTPUT_DIR, KB_DIR

所有产物路径都由此派生，不依赖当前工作目录，也不包含任何机器相关的绝对路径。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../references/calibration
REF_DIR = HERE.parent                           # .../references
KB_DIR = REF_DIR / "kb"                         # 品牌抓取配方
REPO_ROOT = REF_DIR.parent                      # 仓库根
OUTPUT_DIR = REPO_ROOT / "output"
EVIDENCE_DIR = OUTPUT_DIR / "evidence"


def ensure_evidence() -> Path:
    """确保取证目录存在并返回它。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    return EVIDENCE_DIR


def find_chromium():
    """定位可用的 Chromium 可执行文件；找不到就返回 None。

    返回 None 时，Playwright 会用它自己安装的默认 Chromium，
    所以没有匹配到并不会导致脚本失败。

    优先级：
      1. 环境变量 PLAYWRIGHT_CHROMIUM_PATH（显式指定时用这个）
      2. Playwright 默认缓存目录下的 chromium-*/chrome-win64/chrome.exe（取版本号最大的）
      3. None -> 交给 Playwright 自己找
    """
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if env and Path(env).is_file():
        return env

    # Playwright 默认安装位置：%LOCALAPPDATA%/ms-playwright/chromium-<rev>/...
    localapp = os.environ.get("LOCALAPPDATA")
    bases = []
    if localapp:
        bases.append(Path(localapp) / "ms-playwright")
    bases.append(Path.home() / "AppData" / "Local" / "ms-playwright")

    hits = []
    for base in bases:
        if not base.is_dir():
            continue
        for d in base.glob("chromium-*"):
            for rel in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe"):
                cand = d / rel.replace("/", os.sep)
                if cand.is_file():
                    hits.append(cand)
                    break
    if hits:
        # 按修订号降序，优先用新的
        def _rev(p: Path) -> int:
            rev = p.parent.parent.name.split("-")[-1]
            return int(rev) if rev.isdigit() else -1

        return str(max(hits, key=_rev))

    return None


def python_exe() -> str:
    """当前解释器路径（替代原先写死的托管 Python 绝对路径）。"""
    return sys.executable
