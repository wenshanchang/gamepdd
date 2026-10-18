# -*- coding: utf-8 -*-
"""
WinFix 卸载程序（独立 exe）

流程：
  1. 原进程：确认（可 --yes 跳过）→ 把自身复制到 %TEMP% → 用临时副本重新启动 → 立即退出
  2. 临时副本（--from-temp）：等原进程退出 → 删除快捷方式、注册表、安装目录 → 清理自身

这样正在运行的卸载程序不会锁住安装目录，能做到无残留。
"""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

APP_DISPLAY = "WinFix 系统维护工具"
APP_EXE = "WinFix系统维护工具.exe"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WinFix"

CREATE_NO_WINDOW = 0x08000000
MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
MB_ICONWARNING = 0x30
MB_ICONINFO = 0x40


def msg(text: str, title: str, flags: int = MB_ICONINFO) -> int:
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def ps(script: str):
    return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, timeout=60,
                          creationflags=CREATE_NO_WINDOW)


def desktop_dir() -> str:
    r = ps("[Environment]::GetFolderPath('Desktop')")
    p = r.stdout.decode("gbk", "replace").strip()
    return p or os.path.join(os.path.expanduser("~"), "Desktop")


def start_menu_dir() -> str:
    r = ps("[Environment]::GetFolderPath('Programs')")
    p = r.stdout.decode("gbk", "replace").strip()
    return p or os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                             "Start Menu", "Programs")


def process_running(exe_name: str) -> bool:
    try:
        r = subprocess.run(["tasklist", "/fi", f"imagename eq {exe_name}", "/nh"],
                           capture_output=True, timeout=20,
                           creationflags=CREATE_NO_WINDOW)
        return exe_name.lower() in r.stdout.decode("gbk", "replace").lower()
    except Exception:
        return False


def read_install_dir():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            v, _ = winreg.QueryValueEx(k, "InstallLocation")
            return v
    except Exception:
        return None


def remove_reg():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except Exception:
        pass


def kill_app():
    subprocess.run(["taskkill", "/F", "/IM", APP_EXE],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    time.sleep(1)


# ---------------------------------------------------------------- 实际删除
def remove_all(install_dir: str) -> list:
    removed = []
    for lnk in (os.path.join(desktop_dir(), f"{APP_DISPLAY}.lnk"),
                os.path.join(start_menu_dir(), f"{APP_DISPLAY}.lnk"),
                os.path.join(start_menu_dir(), "WinFix", "卸载 WinFix.lnk")):
        try:
            if os.path.isfile(lnk):
                os.remove(lnk)
                removed.append(lnk)
        except Exception:
            pass

    sm = os.path.join(start_menu_dir(), "WinFix")
    try:
        shutil.rmtree(sm, ignore_errors=True)
    except Exception:
        pass

    remove_reg()

    for _ in range(4):
        if not install_dir or not os.path.isdir(install_dir):
            break
        try:
            shutil.rmtree(install_dir)
            removed.append(install_dir)
            break
        except Exception:
            time.sleep(1.2)

    # 兼容旧版留下的 .oldXXXX 目录
    parent = os.path.dirname(install_dir.rstrip("\\")) if install_dir else ""
    if parent and os.path.isdir(parent):
        base = os.path.basename(install_dir.rstrip("\\"))
        try:
            for n in os.listdir(parent):
                if n.startswith(base + ".old"):
                    shutil.rmtree(os.path.join(parent, n), ignore_errors=True)
        except Exception:
            pass

    return removed


def self_delete():
    """删除临时副本自身（失败也无所谓，位于 %TEMP%）。"""
    exe = sys.executable if getattr(sys, "frozen", False) else __file__
    try:
        cmd = f'ping -n 3 127.0.0.1 >nul & del /f /q "{exe}"'
        subprocess.Popen(["cmd", "/c", cmd], creationflags=CREATE_NO_WINDOW,
                         close_fds=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 入口
def main():
    argv = sys.argv[1:]
    silent = "--yes" in argv or "/S" in argv
    from_temp = "--from-temp" in argv

    # 取安装目录：优先参数（临时副本用），否则读注册表
    install_dir = None
    for i, a in enumerate(argv):
        if a == "--dir" and i + 1 < len(argv):
            install_dir = argv[i + 1]
    if not install_dir:
        install_dir = read_install_dir()
    if not install_dir:
        install_dir = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__))

    # 临时副本：执行真正的删除
    if from_temp:
        time.sleep(2.0)  # 等原进程退出，释放目录锁
        remove_all(install_dir)
        self_delete()
        return 0

    # 原进程：先确认，再切换到临时副本
    if process_running(APP_EXE):
        if silent:
            kill_app()
        elif msg(f"检测到 {APP_DISPLAY} 正在运行。\n\n是否立即关闭并继续卸载？",
                 "卸载", MB_YESNO | MB_ICONWARNING) == 6:
            kill_app()
        else:
            return 1

    if not silent:
        if msg(f"确定要卸载 {APP_DISPLAY} 吗？\n\n"
               f"安装位置：{install_dir}\n\n"
               f"将删除程序文件、源码目录、快捷方式与注册信息。",
               "卸载确认", MB_YESNO | MB_ICONQUESTION) != 6:
            return 1

    if getattr(sys, "frozen", False):
        # 复制自身到临时目录，再由副本完成删除（避免锁住自己的安装目录）
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               f"WinFix_uninstall_{int(time.time())}.exe")
            shutil.copy2(sys.executable, tmp)
            subprocess.Popen([tmp, "--from-temp", "--yes", "--dir", install_dir],
                             close_fds=True)
            return 0
        except Exception as e:
            # 复制失败则退化为原地删除
            removed = remove_all(install_dir)
            if not silent:
                msg(f"{APP_DISPLAY} 已卸载。\n\n"
                    + ("已删除：\n" + "\n".join(removed) if removed else "无残留。"),
                    "卸载完成")
            return 0

    # 源码方式运行时直接删除
    removed = remove_all(install_dir)
    if not silent:
        msg(f"{APP_DISPLAY} 已卸载。\n\n"
            + ("已删除：\n" + "\n".join(removed) if removed else "无残留。"),
            "卸载完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
