#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键构建 WinFix：主程序 exe + 卸载程序 exe + 内嵌包 + 安装程序 exe

用法：
    pip install pyinstaller
    python build.py            # 全量构建到 dist/
    python build.py app        # 只构建主程序
    python build.py installer  # 只重建内嵌包与安装程序

说明：安装程序会内嵌 payload.zip（含主程序与卸载程序），
      因此修改主程序后需重新执行全量构建，安装包才会带上新版本。
"""

import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
APP_EXE = "WinFix系统维护工具.exe"
UNINST_EXE = "卸载程序.exe"
INSTALLER_EXE = "WinFix安装程序.exe"
PAYLOAD = os.path.join(ROOT, "payload.zip")

PYI = [sys.executable, "-m", "PyInstaller"]


def run(cmd):
    print("\n$", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"构建失败（退出码 {r.returncode}）：{' '.join(cmd)}")


def build_app():
    run(PYI + ["--onefile", "--windowed", "--name", "WinFix系统维护工具",
               "--clean", "--noconfirm", "winfix.py"])


def build_uninstaller():
    run(PYI + ["--onefile", "--windowed", "--name", "卸载程序",
               "--clean", "--noconfirm", "uninstaller.py"])


def build_payload():
    app = os.path.join(DIST, APP_EXE)
    uninst = os.path.join(DIST, UNINST_EXE)
    for p in (app, uninst):
        if not os.path.isfile(p):
            raise SystemExit(f"缺少文件：{p}，请先构建主程序与卸载程序")
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(app, APP_EXE)
        z.write(uninst, UNINST_EXE)
    size = os.path.getsize(PAYLOAD) / 1024 / 1024
    print(f"payload.zip 已生成：{size:.1f} MB")


def build_installer():
    run(PYI + ["--onefile", "--windowed", "--name", "WinFix安装程序",
               "--add-data", f"{PAYLOAD}{os.pathsep}.", "--clean", "--noconfirm",
               "installer.py"])


def cleanup():
    for d in ("build", "__pycache__"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "app"):
        build_app()
    if what in ("all", "app", "uninstaller"):
        build_uninstaller()
    if what in ("all", "app", "installer"):
        build_payload()
        build_installer()
    cleanup()
    print("\n构建完成，输出目录：", DIST)
    for f in sorted(os.listdir(DIST)):
        p = os.path.join(DIST, f)
        print(f"  {f}  {os.path.getsize(p) / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
