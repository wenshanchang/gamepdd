# -*- coding: utf-8 -*-
"""
WinFix 安装程序（自解压 + 在线拉取源码）

安装流程：
  1. 可选：从 GitHub（wenshanchang/gamepdd）下载最新源码到 <安装目录>\\source
  2. 解压内置程序包（主程序 exe + 卸载程序 exe）
  3. 创建桌面 / 开始菜单快捷方式
  4. 写入「设置 → 应用」卸载注册信息（指向卸载程序 exe）

用法：
  WinFix安装程序.exe                          正常安装向导
  WF_INSTALL_DIR=D:\\path WinFix安装程序.exe   静默安装（默认不建快捷方式、不拉源码）
  WF_INSTALL_DIR=... WF_FETCH_SOURCE=1 ...    静默安装并拉取源码
  WinFix安装程序.exe --uninstall               调用已安装的卸载程序

打包：PyInstaller --onefile --windowed --add-data "payload.zip;." installer.py
"""

import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import winreg
import zipfile
from tkinter import filedialog, messagebox, ttk

APP_NAME = "WinFix"
APP_DISPLAY = "WinFix 系统维护工具"
APP_EXE = "WinFix系统维护工具.exe"
UNINST_EXE = "卸载程序.exe"
VERSION = "2.4"
PUBLISHER = "wenshanchang"

REPO_OWNER = "wenshanchang"
REPO_NAME = "gamepdd"
REPO_BRANCH = "main"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
ZIP_URL = f"https://codeload.github.com/{REPO_OWNER}/{REPO_NAME}/zip/refs/heads/{REPO_BRANCH}"

PAYLOAD = "payload.zip"
DEFAULT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "Programs", "WinFix")
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WinFix"

CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- 工具
def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def payload_path() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, PAYLOAD)


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def ps(script: str):
    return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, timeout=60,
                          creationflags=CREATE_NO_WINDOW)


def create_shortcut(lnk_path: str, target: str, workdir: str = ""):
    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{tgt}';"
        "$s.WorkingDirectory='{wd}';"
        "$s.IconLocation='{tgt},0';"
        "$s.Save()"
    ).format(lnk=lnk_path.replace("'", "''"), tgt=target.replace("'", "''"),
             wd=(workdir or os.path.dirname(target)).replace("'", "''"))
    ps(script)
    return os.path.isfile(lnk_path)


def desktop_dir() -> str:
    r = ps("[Environment]::GetFolderPath('Desktop')")
    p = r.stdout.decode("gbk", "replace").strip()
    return p or os.path.join(os.path.expanduser("~"), "Desktop")


def start_menu_dir() -> str:
    r = ps("[Environment]::GetFolderPath('Programs')")
    p = r.stdout.decode("gbk", "replace").strip()
    return p or os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                             "Start Menu", "Programs")


def installed_dir_from_reg():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            v, _ = winreg.QueryValueEx(k, "InstallLocation")
            return v
    except Exception:
        return None


def write_uninstall_reg(install_dir: str, size_bytes: int):
    cmd = f'"{os.path.join(install_dir, UNINST_EXE)}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ,
                          os.path.join(install_dir, APP_EXE))
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, cmd)
        winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ, cmd)
        winreg.SetValueEx(k, "URLInfoAbout", 0, winreg.REG_SZ, REPO_URL)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD,
                          max(1, int(size_bytes / 1024)))


def remove_uninstall_reg():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except Exception:
        pass


# ---------------------------------------------------------------- 在线源码
def http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "WinFix-Installer/" + VERSION,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_source(dest: str, log) -> str:
    """下载 GitHub 仓库源码到 dest，返回简短结果说明。"""
    log(f"从 GitHub 下载源码：{REPO_URL}")
    data = http_get(ZIP_URL)
    if not data.startswith(b"PK"):
        raise RuntimeError("下载内容不是有效 zip（可能被网络拦截）")

    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    count = 0
    remote_version = None
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/", 1)
            rel = parts[1] if len(parts) > 1 else parts[0]
            if not rel or rel.startswith(".git/"):
                continue
            out = os.path.join(dest, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(name) as src, open(out, "wb") as fd:
                shutil.copyfileobj(src, fd)
            count += 1
            if rel.endswith("version.json"):
                try:
                    with z.open(name) as f:
                        remote_version = json.loads(f.read().decode("utf-8")).get("version")
                except Exception:
                    pass

    log(f"✓ 源码已下载：{count} 个文件 → {dest}")
    if remote_version:
        log(f"  仓库版本 v{remote_version}，本地安装程序 v{VERSION}")
        if remote_version != VERSION:
            log("⚠ 版本不一致：仓库可能已更新，建议重新下载安装程序")
    return f"{count} 个文件"


# ---------------------------------------------------------------- 安装向导
class Installer:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} 安装向导")
        root.geometry("660x620")
        root.resizable(False, False)
        root.configure(bg="#f2f3f6")

        z = payload_path()
        self.size = os.path.getsize(z) if os.path.exists(z) else 0
        self._build()
        self.center()

    def center(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.root.geometry(f"+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")

    def _build(self):
        hd = tk.Frame(self.root, bg="#1c2230", height=72)
        hd.pack(fill="x")
        hd.pack_propagate(False)
        tk.Label(hd, text=APP_NAME, bg="#1c2230", fg="#ffffff",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left", padx=18)
        tk.Label(hd, text=f"v{VERSION}", bg="#1c2230", fg="#9aa4b8",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", pady=4)
        tk.Label(hd, text=f"{REPO_OWNER}/{REPO_NAME}", bg="#1c2230", fg="#6cb2ff",
                 font=("Microsoft YaHei UI", 9)).pack(side="right", padx=18)

        body = tk.Frame(self.root, bg="#ffffff")
        body.pack(fill="both", expand=True, padx=14, pady=12)

        tk.Label(body, text="选择安装位置", bg="#ffffff", fg="#1a1d21", anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x", pady=(4, 6))
        row = tk.Frame(body, bg="#ffffff")
        row.pack(fill="x")
        self.dir_var = tk.StringVar(value=DEFAULT_DIR)
        self.dir_ent = tk.Entry(row, textvariable=self.dir_var, relief="solid", bd=1,
                                font=("Microsoft YaHei UI", 9))
        self.dir_ent.pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(row, text="浏览…", command=self.browse, bg="#eef1f6", fg="#3a4049",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 8), cursor="hand2",
                  padx=10, pady=3).pack(side="left", padx=(8, 0))
        tk.Label(body, text="安装到当前用户目录，不需要管理员权限。",
                 bg="#ffffff", fg="#8b929e",
                 font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x", pady=(4, 10))

        tk.Label(body, text="附加任务", bg="#ffffff", fg="#1a1d21", anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x", pady=(0, 4))
        self.c_source = tk.BooleanVar(value=True)
        self.c_desktop = tk.BooleanVar(value=True)
        self.c_start = tk.BooleanVar(value=True)
        self.c_run = tk.BooleanVar(value=True)
        for text, var in [
            (f"从 GitHub 拉取最新源码到 source 目录（需联网）", self.c_source),
            ("创建桌面快捷方式", self.c_desktop),
            ("创建开始菜单快捷方式（含卸载入口）", self.c_start),
            ("安装完成后运行 WinFix", self.c_run),
        ]:
            tk.Checkbutton(body, text=text, variable=var, bg="#ffffff", fg="#1a1d21",
                           font=("Microsoft YaHei UI", 9), anchor="w",
                           activebackground="#ffffff", cursor="hand2").pack(
                fill="x", pady=2)

        self.status = tk.Label(body, text=f"准备安装（内置程序包 {fmt_size(self.size)}）",
                               bg="#ffffff", fg="#3a4049",
                               font=("Microsoft YaHei UI", 9, "bold"), anchor="w")
        self.status.pack(fill="x", pady=(12, 4))
        self.bar = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(0, 8))

        self.logbox = tk.Text(body, height=7, bg="#f7f8fa", fg="#3a4049", relief="flat",
                              bd=0, font=("Consolas", 9), wrap="word",
                              state="disabled", padx=8, pady=6)
        self.logbox.pack(fill="both", expand=True)
        self.logbox.tag_config("ok", foreground="#1a7f37")
        self.logbox.tag_config("warn", foreground="#bf6a02")
        self.log("就绪。点击「开始安装」后，将先从 GitHub 拉取源码（可取消勾选），"
                 "再写入程序文件并创建快捷方式。")

        btns = tk.Frame(self.root, bg="#f2f3f6")
        btns.pack(fill="x", padx=14, pady=(0, 12))
        self.install_btn = tk.Button(btns, text="开始安装", command=self.install,
                                     bg="#2f6feb", fg="#ffffff", relief="flat", bd=0,
                                     font=("Microsoft YaHei UI", 9), cursor="hand2",
                                     padx=16, pady=4)
        self.install_btn.pack(side="right")
        self.cancel_btn = tk.Button(btns, text="取消", command=self.root.destroy,
                                    bg="#eef1f6", fg="#3a4049", relief="flat", bd=0,
                                    font=("Microsoft YaHei UI", 9), cursor="hand2",
                                    padx=14, pady=4)
        self.cancel_btn.pack(side="right", padx=8)

    # ---- 日志与进度
    def log(self, text):
        def _do():
            self.logbox.config(state="normal")
            tag = "warn" if ("⚠" in text or "失败" in text) else (
                "ok" if "✓" in text else None)
            self.logbox.insert("end", text + "\n", tag or ())
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _ui(self, fn):
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def progress(self, pct, status=None):
        def _do():
            self.bar.configure(value=max(0, min(100, pct)))
            if status:
                self.status.configure(text=status)
        self._ui(_do)

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or DEFAULT_DIR)
        if d:
            self.dir_var.set(d)

    # ---- 安装
    def install(self):
        target = os.path.abspath(os.path.expanduser(
            self.dir_var.get().strip() or DEFAULT_DIR))

        if target.upper().startswith(os.environ.get("SystemDrive", "C:") + r"\PROGRAM FILES") \
           or target.upper().startswith(os.environ.get("SystemRoot", r"C:\WINDOWS")):
            if not ctypes.windll.shell32.IsUserAnAdmin():
                if not messagebox.askyesno(
                        "需要管理员权限",
                        "所选位置是系统目录，需要管理员权限。\n\n"
                        "是否改用默认位置（当前用户目录）安装？"):
                    return
                target = DEFAULT_DIR
                self.dir_var.set(target)

        if os.path.exists(os.path.join(target, APP_EXE)):
            if not messagebox.askyesno("已存在安装", "该目录已安装过 WinFix，是否覆盖安装？"):
                return

        self.install_btn.config(state="disabled")
        self.cancel_btn.config(state="disabled")
        self.dir_ent.config(state="disabled")

        def worker():
            try:
                self._do_install(target)
            except Exception as e:
                self.log(f"⚠ 安装失败：{e}")
                self._ui(lambda: messagebox.showerror("安装失败", f"安装过程中出错：\n{e}"))
                self._ui(lambda: self.install_btn.config(state="normal"))
                self._ui(lambda: self.cancel_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _do_install(self, target):
        src = payload_path()
        if not os.path.exists(src):
            raise FileNotFoundError(f"安装包数据缺失：{src}")

        # ---- 1. 在线源码
        source_ok = False
        if self.c_source.get():
            self.progress(4, "正在从 GitHub 拉取源码…")
            try:
                download_source(os.path.join(target, "source"), self.log)
                source_ok = True
            except Exception as e:
                self.log(f"⚠ 源码下载失败（{e}）")
                self.log(f"  将只安装程序本体，可稍后访问 {REPO_URL}")
        else:
            self.log("已跳过源码拉取（仅安装程序本体）")

        # ---- 2. 解压程序
        self.progress(35, "正在写入程序文件…")
        os.makedirs(target, exist_ok=True)
        total = 0
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            for i, name in enumerate(names, 1):
                out = os.path.join(target, name.replace("/", os.sep))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(name) as fsrc, open(out, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1024 * 256)
                total += os.path.getsize(out)
                self.progress(35 + i / max(1, len(names)) * 40, f"正在写入 {name}")
                self.log(f"  写入 {name}（{fmt_size(os.path.getsize(out))}）")

        exe_path = os.path.join(target, APP_EXE)
        uninst_path = os.path.join(target, UNINST_EXE)
        if not os.path.isfile(exe_path):
            raise RuntimeError("程序文件缺失，安装包可能损坏")

        # ---- 3. 快捷方式
        pct = 78
        if self.c_desktop.get():
            self.progress(pct, "创建桌面快捷方式…")
            ok = create_shortcut(os.path.join(desktop_dir(), f"{APP_DISPLAY}.lnk"), exe_path)
            self.log(("✓ " if ok else "⚠ ") + "桌面快捷方式")
            pct += 6
        if self.c_start.get():
            self.progress(pct, "创建开始菜单项…")
            sm = os.path.join(start_menu_dir(), "WinFix")
            ok1 = create_shortcut(os.path.join(sm, f"{APP_DISPLAY}.lnk"), exe_path)
            ok2 = create_shortcut(os.path.join(sm, "卸载 WinFix.lnk"), uninst_path)
            self.log(("✓ " if ok1 else "⚠ ") + "开始菜单快捷方式")
            self.log(("✓ " if ok2 else "⚠ ") + "开始菜单卸载入口")
            pct += 6

        # ---- 4. 注册卸载信息
        self.progress(94, "注册卸载信息…")
        write_uninstall_reg(target, total)
        self.log("✓ 已注册到「设置 → 应用」")
        if not os.path.isfile(uninst_path):
            self.log("⚠ 未找到卸载程序，注册表将无法直接卸载")

        self.progress(100, "安装完成")
        self.log(f"✓ 安装完成，共 {fmt_size(total)}" +
                 ("，源码位于 source 目录" if source_ok else ""))
        time.sleep(0.3)
        self._ui(lambda: self._finish(target, source_ok))

    def _finish(self, target, source_ok):
        for w in self.root.winfo_children():
            w.destroy()

        hd = tk.Frame(self.root, bg="#1c2230", height=72)
        hd.pack(fill="x")
        hd.pack_propagate(False)
        tk.Label(hd, text="安装完成", bg="#1c2230", fg="#ffffff",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left", padx=18)
        tk.Label(hd, text=f"{APP_DISPLAY} v{VERSION}", bg="#1c2230", fg="#9aa4b8",
                 font=("Microsoft YaHei UI", 9)).pack(side="right", padx=18)

        body = tk.Frame(self.root, bg="#ffffff")
        body.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(body, text=f"安装位置：\n{target}", bg="#ffffff", fg="#1a1d21",
                 font=("Microsoft YaHei UI", 9), justify="left",
                 anchor="w").pack(fill="x", pady=(6, 8))

        notes = []
        if source_ok:
            notes.append(f"· 源码已拉取到：{os.path.join(target, 'source')}")
        notes.append(f"· 卸载程序：{os.path.join(target, UNINST_EXE)}")
        if self.c_desktop.get():
            notes.append("· 桌面已创建快捷方式")
        if self.c_start.get():
            notes.append("· 开始菜单已创建程序项与卸载入口")
        notes.append("· 也可在「设置 → 应用」中卸载")
        notes.append("· 建议右键以管理员身份运行，修复功能才完整")
        tk.Label(body, text="\n".join(notes), bg="#ffffff", fg="#3a4049",
                 font=("Microsoft YaHei UI", 9), justify="left",
                 anchor="w").pack(fill="x", pady=(0, 12))

        btns = tk.Frame(self.root, bg="#f2f3f6")
        btns.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(btns, text="完成", command=self.root.destroy, bg="#2f6feb",
                  fg="#ffffff", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 9), cursor="hand2",
                  padx=16, pady=4).pack(side="right")
        if self.c_run.get():
            tk.Button(btns, text="运行 WinFix", command=lambda: self._run_app(target),
                      bg="#0f9b8e", fg="#ffffff", relief="flat", bd=0,
                      font=("Microsoft YaHei UI", 9), cursor="hand2",
                      padx=14, pady=4).pack(side="right", padx=8)

    def _run_app(self, target):
        exe = os.path.join(target, APP_EXE)
        if os.path.exists(exe):
            subprocess.Popen([exe], close_fds=True)
        self.root.destroy()


# ---------------------------------------------------------------- 卸载
def do_uninstall():
    d = installed_dir_from_reg()
    uninst = os.path.join(d, UNINST_EXE) if d else None
    if uninst and os.path.isfile(uninst):
        subprocess.Popen([uninst], close_fds=True)
        return 0
    remove_uninstall_reg()
    ctypes.windll.user32.MessageBoxW(0, "未找到已安装的卸载程序，已清理注册信息。",
                                     "卸载", 0x40)
    return 0


def main():
    if "--uninstall" in sys.argv[1:]:
        return do_uninstall()

    auto_dir = os.environ.get("WF_INSTALL_DIR")
    if auto_dir:
        root = tk.Tk()
        root.withdraw()
        app = Installer(root)
        app.c_source.set(bool(os.environ.get("WF_FETCH_SOURCE")))
        app.c_desktop.set(False)
        app.c_start.set(False)
        app.c_run.set(False)
        app.dir_var.set(os.path.abspath(auto_dir))
        app._do_install(os.path.abspath(auto_dir))
        root.destroy()
        return 0

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Installer(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
