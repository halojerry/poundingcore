#!/usr/bin/env python3
"""Skill 一键升级引导脚本 — 旧包（无 updater.py）迁移用。

背景：自动更新机制（updater.py）自 v0.12.0 才随包分发，v0.12.0 之前的
旧包没有 updater，永远收不到新版本提示。本脚本单文件、仅依赖 requests +
标准库，供旧包用户手动下载后放在 skill 包目录运行，一次升级到最新版
（升级成功后新包自带 updater，之后即可自动更新）。

用法（把本文件放到包含 scripts/ 与 VERSION 的 skill 包目录后执行）：
    python3.12 bootstrap_update.py

流程：读 COS manifest → 下载最新包 → sha256 校验 → 备份旧文件（保留 data/）
→ 覆盖 → 失败自动回滚。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import requests

# COS manifest 唯一真源（与 updater.py 保持一致）
MANIFEST_URL = "https://yss-1256275613.cos.ap-guangzhou.myqcloud.com/ozon-skill/manifest.json"
DOWNLOAD_TIMEOUT = 120
_PRESERVE_DIRS = {"data"}


def find_skill_root() -> Path:
    """定位 skill 包根目录（含 scripts/ 与 VERSION）。"""
    here = Path(__file__).resolve()
    # 随 dist 分发：scripts/bootstrap_update.py → 上一级
    if (here.parent.name == "scripts"
            and (here.parent.parent / "VERSION").exists()
            and (here.parent.parent / "scripts").is_dir()):
        return here.parent.parent
    # 手动下载放在包根目录：当前目录含 scripts/ 与 VERSION
    if (Path.cwd() / "scripts").is_dir() and (Path.cwd() / "VERSION").exists():
        return Path.cwd()
    raise SystemExit(
        "未找到 skill 包：请把本文件放到包含 scripts/ 和 VERSION 的目录后运行")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_archive(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        tf.extractall(dest, filter="data")  # 安全解压（拒绝路径穿越）


def main() -> int:
    root = find_skill_root()
    print(f"📁 skill 目录: {root}")

    try:
        resp = requests.get(MANIFEST_URL, timeout=10)
        resp.raise_for_status()
        data = json.loads(resp.text)
    except Exception as exc:
        print(f"❌ 无法获取更新信息（网络或 COS 问题）: {exc}")
        return 1

    if not isinstance(data, dict):
        print("❌ manifest 格式不正确")
        return 1
    version = data.get("version")
    url = data.get("url")
    sha = data.get("sha256")
    if not version or not url or not sha:
        print("❌ manifest 缺少 version/url/sha256")
        return 1

    current = "0.0.0"
    if (root / "VERSION").exists():
        current = (root / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    print(f"📦 当前 v{current} → 最新 v{version}")
    if current == version:
        print("✅ 已是最新版本")
        return 0

    confirm = input(f"是否升级到 v{version}？(y/N) ")
    if confirm.lower() != "y":
        print("已取消")
        return 0

    tmp_dir = Path(tempfile.mkdtemp(prefix="skill-bootstrap-"))
    try:
        archive = tmp_dir / "pkg.tar.gz"
        print(f"⏳ 下载 {url} ...")
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(archive, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        resp.close()

        got = _sha256(archive)
        if got != sha:
            print(f"❌ sha256 校验失败（{got[:16]}… ≠ {sha[:16]}…），已中止")
            return 1

        extract_dir = tmp_dir / "pkg"
        extract_dir.mkdir()
        _extract_archive(archive, extract_dir)
        if len(list(extract_dir.iterdir())) == 1 and next(extract_dir.iterdir()).is_dir():
            pkg_root = next(extract_dir.iterdir())
        else:
            pkg_root = extract_dir

        # 备份旧文件（保留 data/）
        backup = root / "_update_backup"
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True, exist_ok=True)
        for item in root.iterdir():
            if item.name in _PRESERVE_DIRS or item.name == "_update_backup":
                continue
            shutil.move(str(item), str(backup / item.name))

        # 覆盖新包；失败回滚
        try:
            for item in pkg_root.iterdir():
                if item.name in _PRESERVE_DIRS or item.name == "_update_backup":
                    continue
                target = root / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
        except Exception as exc:
            print(f"❌ 覆盖失败，回滚旧版本: {exc}")
            for item in backup.iterdir():
                target = root / item.name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
                shutil.move(str(item), str(root / item.name))
            return 1

        shutil.rmtree(backup, ignore_errors=True)
        print(f"✅ 升级完成: v{current} → v{version}（data/ 已保留）")
        print("   ⚠️ 请重启终端后重新运行命令，让新版本生效")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
