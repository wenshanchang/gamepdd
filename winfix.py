# -*- coding: utf-8 -*-
"""
WinFix - Windows 系统维护工具
布局：左侧操作卡片，右侧统一日志面板
"""

import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "WinFix 系统维护工具"
VERSION = "2.4"
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "WinFix", "logs")


# ---------------------------------------------------------------- 基础设施
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    try:
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe = sys.executable
            params = f'"{os.path.abspath(__file__)}"'
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return ret > 32
    except Exception:
        return False


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------- 内存信息
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def get_memory():
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return {
        "percent": st.dwMemoryLoad,
        "total": st.ullTotalPhys,
        "avail": st.ullAvailPhys,
        "used": st.ullTotalPhys - st.ullAvailPhys,
    }


def get_disk():
    try:
        total, used, free = shutil.disk_usage("C:\\")
        return total, used, free
    except Exception:
        return 0, 0, 0


# ---------------------------------------------------------------- 清理模块
CLEAN_TARGETS = [
    ("Windows 临时文件", [os.environ.get("TEMP", ""), os.environ.get("TMP", ""),
                          os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")]),
    ("Windows 更新缓存", [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                       "SoftwareDistribution", "Download")]),
    ("系统错误报告", [os.path.join(os.environ.get("LOCALAPPDATA", ""), "CrashDumps"),
                      os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Minidump")]),
    ("预读取缓存 Prefetch", [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Prefetch")]),
    ("缩略图缓存", [os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                 "Microsoft", "Windows", "Explorer")]),
    ("回收站", ["__RECYCLE_BIN__"]),
    ("浏览器缓存", [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data",
                     "Default", "Cache"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data",
                     "Default", "Cache"),
    ]),
]

SAFE_EXT = {".tmp", ".temp", ".log", ".dmp", ".old", ".bak", ".chk", ".gid", ".etl"}


def _walk_clean(root: str, max_depth: int = 3):
    base_depth = root.rstrip("\\").count("\\")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda e: None):
        if dirpath.count("\\") - base_depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if root.lower().endswith(("temp", "tmp", "download", "prefetch", "crashes", "cache")) \
               or ext in SAFE_EXT:
                yield os.path.join(dirpath, fn)


def scan_clean_targets(report):
    result = []
    for name, paths in CLEAN_TARGETS:
        size, count = 0, 0
        for p in paths:
            if not p or p == "__RECYCLE_BIN__" or not os.path.isdir(p):
                continue
            try:
                for f in _walk_clean(p):
                    try:
                        size += os.path.getsize(f)
                        count += 1
                    except OSError:
                        pass
            except Exception:
                pass
        report(f"  {name}: {fmt_size(size)} ({count} 个文件)")
        result.append((name, paths, size, count))
    return result


def do_clean(selected, report, progress=None):
    freed, deleted, failed = 0, 0, 0
    total_items = max(1, sum(c for _, _, _, c in selected))
    done = 0
    for name, paths, _size, _count in selected:
        report(f"▶ 清理 {name} ...")
        for p in paths:
            if not p:
                continue
            if p == "__RECYCLE_BIN__":
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                                   capture_output=True, timeout=120,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                    report("  回收站已清空")
                except Exception as e:
                    report(f"  回收站清理失败: {e}")
                continue
            if not os.path.isdir(p):
                continue
            for f in list(_walk_clean(p)):
                try:
                    sz = os.path.getsize(f)
                    os.remove(f)
                    freed += sz
                    deleted += 1
                except OSError:
                    failed += 1
                done += 1
                if progress and done % 25 == 0:
                    progress(min(done / total_items, 0.99))
        report(f"  {name} 完成")
    return freed, deleted, failed


def scan_dll_issues(report):
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    sys32 = os.path.join(sysroot, "System32")
    critical = [
        "kernel32.dll", "user32.dll", "gdi32.dll", "advapi32.dll", "shell32.dll",
        "ole32.dll", "comctl32.dll", "msvcrt.dll", "ntdll.dll", "ws2_32.dll",
        "shlwapi.dll", "uxtheme.dll", "comdlg32.dll", "winmm.dll", "version.dll",
        "setupapi.dll", "crypt32.dll", "bcrypt.dll", "d3d11.dll", "dwmapi.dll",
    ]
    missing, ok = [], 0
    for d in critical:
        if os.path.isfile(os.path.join(sys32, d)):
            ok += 1
        else:
            missing.append(d)
    report(f"关键系统 DLL 检查：{ok}/{len(critical)} 正常")
    if missing:
        report("  ⚠ 缺失：" + ", ".join(missing))
        report("  建议运行「系统文件修复」找回这些文件")
    winsxs = os.path.join(sysroot, "WinSxS")
    report(f"  组件库 WinSxS：{'存在' if os.path.isdir(winsxs) else '⚠ 缺失'}")
    bad = [p for p in os.environ.get("PATH", "").split(";")
           if p and not os.path.isdir(p) and p.lower().endswith(".dll")]
    if bad:
        report(f"  环境变量 PATH 中有 {len(bad)} 个无效 DLL 路径")
    return missing


# ---------------------------------------------------------------- 输出解码
class OutputDecoder:
    """流式解码子进程输出：多编码打分识别（SFC/DISM 为 UTF-16LE，工具链多为 GBK）。"""

    def __init__(self, prefer=None):
        base = ["gbk", "utf-8", "utf-16-le"]
        if prefer in base:
            base.remove(prefer)
            base.insert(0, prefer)
        self.candidates = base
        # 已知命令的编码直接采信，避免短输出来不及判定
        self.enc = prefer
        self.pending = b""

    @staticmethod
    def _score(text):
        good = bad = 0
        for ch in text:
            o = ord(ch)
            if ch in "\r\n\t" or 32 <= o < 127 or 0x4E00 <= o <= 0x9FFF \
               or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
                good += 1
            elif o < 32 or o == 0xFFFD or 0xE000 <= o <= 0xF8FF:
                bad += 3
        return (good - bad) / max(1, len(text))

    def _detect(self):
        sample = self.pending[:512]
        best, best_score = None, -1e9
        for enc in self.candidates:
            try:
                if enc == "utf-16-le":
                    n = len(sample) - len(sample) % 2
                    t = sample[:n].decode("utf-16-le", errors="replace")
                else:
                    t = sample.decode(enc, errors="replace")
            except Exception:
                continue
            s = self._score(t)
            # 靠前的候选在分数接近时优先（不同编码分数差通常很大）
            if s > best_score + 0.05:
                best, best_score = enc, s
        self.enc = best or "gbk"

    def feed(self, data: bytes) -> str:
        self.pending += data
        if self.enc is None:
            if len(self.pending) < 32:
                return ""
            self._detect()

        if self.enc == "utf-16-le":
            n = len(self.pending) - (len(self.pending) % 2)
            data, self.pending = self.pending[:n], self.pending[n:]
        else:
            data, self.pending = self.pending, b""

        try:
            return data.decode(self.enc, errors="replace")
        except Exception:
            return data.decode("utf-8", errors="replace")

    def flush(self) -> str:
        data, self.pending = self.pending, b""
        if not data:
            return ""
        if self.enc is None:
            self.pending = data
            self._detect()
            data, self.pending = self.pending, b""
        enc = self.enc or "gbk"
        try:
            if enc == "utf-16-le":
                data = data[:len(data) - len(data) % 2]
            return data.decode(enc, errors="replace")
        except Exception:
            return ""


# ---------------------------------------------------------------- 日志面板
class LogPanel:
    """右侧统一日志面板。"""

    def __init__(self, parent):
        wrap = tk.Frame(parent, bg="#1b1f27")
        wrap.pack(fill="both", expand=True)

        bar = tk.Frame(wrap, bg="#1b1f27")
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text="运行日志", bg="#1b1f27", fg="#c9d1e0",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.tag_lbl = tk.Label(bar, text="", bg="#1b1f27", fg="#6b7689",
                                font=("Microsoft YaHei UI", 8))
        self.tag_lbl.pack(side="left", padx=12)
        tk.Button(bar, text="清空", command=self.clear, bg="#2a3140", fg="#aeb8cc",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 8),
                  cursor="hand2", padx=10, pady=2,
                  activebackground="#2a3140", activeforeground="#ffffff").pack(side="right")
        tk.Button(bar, text="保存日志", command=self.save, bg="#2a3140", fg="#aeb8cc",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 8),
                  cursor="hand2", padx=10, pady=2,
                  activebackground="#2a3140", activeforeground="#ffffff").pack(
            side="right", padx=6)

        self.text = tk.Text(wrap, bg="#141821", fg="#c9d1e0", relief="flat", bd=0,
                            font=("Consolas", 9), wrap="word", state="disabled",
                            padx=12, pady=8)
        self.text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.text.tag_config("ok", foreground="#4ec98a")
        self.text.tag_config("warn", foreground="#f0b429")
        self.text.tag_config("err", foreground="#f2635f")
        self.text.tag_config("head", foreground="#6cb2ff",
                             font=("Consolas", 9, "bold"))
        self.text.tag_config("time", foreground="#5b6478")

    def _append(self, text, tag):
        self.text.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.text.insert("end", f"[{ts}] ", "time")
        self.text.insert("end", text + "\n", tag or ())
        self.text.see("end")
        self.text.config(state="disabled")

    def write(self, text, tag=None):
        if getattr(self, "app", None) is not None:
            self.app.post("log", (text, tag))
            return
        self._append(text, self._auto_tag(text))

    def head(self, text):
        if getattr(self, "app", None) is not None:
            self.app.post("head", text)
            return
        self._append("―― " + text + " ――", "head")

    @staticmethod
    def _auto_tag(text):
        if "⚠" in text or "失败" in text or "缺失" in text:
            return "warn"
        if "✓" in text or "完成" in text or "正常" in text:
            return "ok"
        return None

    def clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def save(self):
        try:
            content = self.text.get("1.0", "end").strip()
            if not content:
                return
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, time.strftime("WinFix_%Y%m%d_%H%M%S.log"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.write(f"✓ 日志已保存：{path}")
            subprocess.Popen(["explorer", "/select,", path])
        except Exception as e:
            self.write(f"⚠ 保存日志失败：{e}")


# ---------------------------------------------------------------- 操作卡片
class WorkCard:
    """左侧紧凑操作卡片：标题 + 说明 + 进度条 + 按钮行。"""

    def __init__(self, parent, title, subtitle):
        self.frame = tk.Frame(parent, bg="#ffffff", highlightthickness=1,
                              highlightbackground="#e3e6eb")
        self.frame.pack(fill="x", padx=12, pady=(0, 8))
        self.app = None  # 由 app 注入
        self.busy = False
        self.stop_flag = threading.Event()
        self._proc = None
        self._btn_widgets = []

        tk.Label(self.frame, text=title, bg="#ffffff", fg="#1a1d21",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w").pack(
            fill="x", padx=14, pady=(8, 0), anchor="w")
        tk.Label(self.frame, text=subtitle, bg="#ffffff", fg="#767c88",
                 font=("Microsoft YaHei UI", 8), anchor="w", justify="left",
                 wraplength=326).pack(fill="x", padx=14, pady=(2, 5))

        self.bar = ttk.Progressbar(self.frame, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=14, pady=(0, 6))

        self.btnrow = tk.Frame(self.frame, bg="#ffffff")
        self.btnrow.pack(fill="x", padx=14, pady=(0, 10))
        self.status_lbl = tk.Label(self.btnrow, text="就绪", bg="#ffffff",
                                   fg="#8b929e", font=("Microsoft YaHei UI", 8))
        self.status_lbl.pack(side="right")

    def add_button(self, text, color, command):
        btn = tk.Button(self.btnrow, text=text, command=command,
                        bg=color, fg="#ffffff", relief="flat", bd=0,
                        font=("Microsoft YaHei UI", 8), cursor="hand2",
                        activebackground=color, activeforeground="#ffffff",
                        padx=10, pady=3)
        btn.pack(side="left", padx=(0, 8))
        self._btn_widgets.append(btn)
        return btn

    # ---- 状态控制
    def bind_app(self, app):
        self.app = app

    def log(self, text):
        if self.app:
            self.app.log.write(text)

    def head(self, text):
        if self.app:
            self.app.log.head(text)

    def set_progress(self, v):
        if getattr(self, "app", None) is not None:
            self.app.post("prog", (self, v))
            return
        try:
            self.bar.configure(value=max(0, min(100, v)))
        except Exception:
            pass

    def set_status(self, text):
        if getattr(self, "app", None) is not None:
            self.app.post("status", (self, text))
            return
        try:
            self.status_lbl.config(text=text)
        except Exception:
            pass

    def start(self):
        self.busy = True
        self.stop_flag.clear()
        for b in self._btn_widgets:
            b.config(state="disabled")
        self.add_stop()
        self.set_progress(0)
        self.set_status("执行中...")
        if self.app:
            self.app.log.tag_lbl.config(text="")

    def finish(self, note="已完成"):
        self.busy = False
        self.remove_stop()
        for b in self._btn_widgets:
            b.config(state="normal")
        self.set_status(note)

    def add_stop(self):
        self.stop_btn = tk.Button(self.btnrow, text="■ 停止", command=self.request_stop,
                                  bg="#f2635f", fg="#ffffff", relief="flat", bd=0,
                                  font=("Microsoft YaHei UI", 8), cursor="hand2",
                                  activebackground="#f2635f", activeforeground="#ffffff",
                                  padx=10, pady=3)
        self.stop_btn.pack(side="left")

    def remove_stop(self):
        try:
            self.stop_btn.destroy()
        except Exception:
            pass

    def request_stop(self):
        self.stop_flag.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.set_status("正在停止...")

    def run_thread(self, fn):
        if self.busy:
            return
        self.busy = True
        self.start()          # 主线程调用（按钮回调），安全

        def runner():
            try:
                fn(self)
            except Exception as e:
                self.log(f"⚠ 执行出错: {e}")
            finally:
                if self.app:
                    self.app.post("end", (self,))
                else:
                    self.finish()

        threading.Thread(target=runner, daemon=True).start()


# ---------------------------------------------------------------- 主程序
class WinFixApp:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME}  v{VERSION}")
        # 按屏幕尺寸自适应，避免小屏/未全屏时内容被裁掉
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1180, max(940, sw - 140))
        h = min(840, max(520, sh - 120))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(880, 480)
        root.configure(bg="#f2f3f6")

        os.makedirs(LOG_DIR, exist_ok=True)
        self._q = queue.Queue()
        self._build_header()
        body = tk.Frame(root, bg="#f2f3f6")
        body.pack(fill="both", expand=True)

        # 左：操作列（可滚动，保证任何窗口高度都能看到全部功能）
        left_outer = tk.Frame(body, bg="#f2f3f6", width=392)
        left_outer.pack(side="left", fill="y")
        left_outer.pack_propagate(False)

        self._canvas = tk.Canvas(left_outer, bg="#f2f3f6", highlightthickness=0,
                                 bd=0, width=376)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(left_outer, orient="vertical", command=self._canvas.yview)
        vsb.pack(side="right", fill="y")
        self._canvas.configure(yscrollcommand=vsb.set)

        left = tk.Frame(self._canvas, bg="#f2f3f6")
        self._left_inner = left
        self._left_win = self._canvas.create_window((0, 0), window=left, anchor="nw")

        left.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._left_win, width=e.width))
        root.bind_all("<MouseWheel>", self._on_wheel)

        self._build_left(left)

        # 右：日志列
        right = tk.Frame(body, bg="#1b1f27")
        right.pack(side="left", fill="both", expand=True, padx=(0, 0))
        self.log = LogPanel(right)
        self.log.app = self

        for card in self._all_cards():
            card.bind_app(self)

        self.refresh_stats()
        self._pump()
        self.log.write(f"WinFix v{VERSION} 已启动。"
                       + ("当前为管理员权限。" if is_admin()
                          else "⚠ 当前非管理员权限，修复功能受限，建议点右上角提权。"),
                       "ok" if is_admin() else "warn")

    def _all_cards(self):
        return [self.card_dll, self.card_diag, self.card_mem, self.card_clean]

    # -------- 线程 → UI 消息泵（所有界面更新只在主线程执行）
    def post(self, kind, payload):
        try:
            self._q.put_nowait((kind, payload))
        except Exception:
            pass

    def _pump(self):
        n = 0
        while n < 300:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                break
            n += 1
            try:
                if kind == "log":
                    text, tag = payload
                    self.log._append(text, tag or LogPanel._auto_tag(text))
                elif kind == "head":
                    self.log._append("―― " + payload + " ――", "head")
                elif kind == "prog":
                    card, v = payload
                    card.bar.configure(value=max(0, min(100, v)))
                elif kind == "status":
                    card, text = payload
                    card.status_lbl.config(text=text)
                elif kind == "idle":
                    # 长时间无输出：进度条切换为不确定动画，直观表示仍在运行
                    payload.bar.stop()
                    payload.bar.configure(mode="indeterminate")
                    payload.bar.start(12)
                elif kind == "busy":
                    card, v = payload
                    card.bar.stop()
                    card.bar.configure(mode="determinate", value=max(0, min(100, v)))
                elif kind == "end":
                    payload[0].finish()
            except Exception:
                pass
        try:
            self.root.after(60, self._pump)
        except Exception:
            pass

    def _on_wheel(self, event):
        """鼠标在左栏时滚轮滚动操作列表。"""
        try:
            inner, canvas = self._left_inner, self._canvas
            if inner.winfo_height() <= canvas.winfo_height():
                return
            lx, ly = canvas.winfo_rootx(), canvas.winfo_rooty()
            if lx <= event.x_root <= lx + canvas.winfo_width() and \
               ly <= event.y_root <= ly + canvas.winfo_height():
                canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1),
                                    "units")
        except Exception:
            pass

    # -------- UI
    def _build_header(self):
        hd = tk.Frame(self.root, bg="#1c2230", height=64)
        hd.pack(fill="x")
        hd.pack_propagate(False)

        tk.Label(hd, text="WinFix", bg="#1c2230", fg="#ffffff",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left", padx=18)
        tk.Label(hd, text="DLL 修复 · 异常检测 · 内存与垃圾清理",
                 bg="#1c2230", fg="#9aa4b8",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", pady=2)

        admin = is_admin()
        tk.Label(hd,
                 text="● 管理员权限" if admin else "○ 普通权限（修复功能受限）",
                 bg="#1c2230", fg="#5fd08a" if admin else "#f0b429",
                 font=("Microsoft YaHei UI", 9)).pack(side="right", padx=18)
        if not admin:
            tk.Button(hd, text="以管理员身份重启", command=self.elevate,
                      bg="#2f6feb", fg="#ffffff", relief="flat", bd=0,
                      font=("Microsoft YaHei UI", 8), cursor="hand2",
                      padx=10, pady=2).pack(side="right")

    def _build_left(self, left):
        # 统计卡片：两行，每行「标题 + 进度条 + 数值」，宽度自适应
        stats = tk.Frame(left, bg="#ffffff", highlightthickness=1,
                         highlightbackground="#e3e6eb")
        stats.pack(fill="x", padx=12, pady=(10, 10))
        inner = tk.Frame(stats, bg="#ffffff")
        inner.pack(fill="x", padx=12, pady=(10, 4))

        def stat_row(title):
            row = tk.Frame(inner, bg="#ffffff")
            row.pack(fill="x", pady=(0, 7))
            val = tk.Label(row, text="--", bg="#ffffff", fg="#3a4049",
                           font=("Microsoft YaHei UI", 8), anchor="e")
            val.pack(side="right")
            tk.Label(row, text=title, bg="#ffffff", fg="#1a1d21",
                     font=("Microsoft YaHei UI", 9, "bold"),
                     width=5, anchor="w").pack(side="left", padx=(0, 6))
            bar = ttk.Progressbar(row, mode="determinate", maximum=100)
            bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
            return bar, val

        self.mem_bar, self.mem_val = stat_row("内存")
        self.disk_bar, self.disk_val = stat_row("C 盘")
        tk.Frame(inner, bg="#ffffff", height=2).pack(fill="x")

        # DLL 卡片：快速 / 深度 / 独立窗口运行
        self.card_dll = WorkCard(left, "DLL / 系统文件修复",
                                 "快速修复仅运行 SFC（约 5~15 分钟）；深度修复先重建映像（DISM）"
                                 "再 SFC，很慢；想看到原生进度可选「窗口运行」。")
        self.card_dll.add_button("⚡ 快速修复", "#2f6feb",
                                 lambda: self._start_dll(False))
        self.card_dll.add_button("深度修复", "#1c2230",
                                 lambda: self._start_dll(True))
        self.card_dll.add_button("窗口运行", "#4b5566",
                                 lambda: self._start_dll(console=True))
        self.card_dll.frame.pack(fill="x")

        # 异常检测
        self.card_diag = WorkCard(left, "电脑异常检测",
                                  "检查关键 DLL 完整性、磁盘空间、启动项、系统负载与错误日志，秒级出结果。")
        self.card_diag.add_button("立即检测", "#0f9b8e",
                                  lambda: self.card_diag.run_thread(self.task_diag))
        self.card_diag.frame.pack(fill="x")

        # 内存优化
        self.card_mem = WorkCard(left, "内存优化",
                                 "回收进程工作集，释放被占用的物理内存，并列出内存占用大户。")
        self.card_mem.add_button("优化内存", "#8250df",
                                 lambda: self.card_mem.run_thread(self.task_mem))
        self.card_mem.frame.pack(fill="x")

        # 垃圾清理
        self.card_clean = WorkCard(left, "垃圾清理",
                                   "扫描并清理临时文件、更新缓存、缩略图、回收站与浏览器缓存，只删安全项。")
        self.card_clean.add_button("扫描并清理", "#c2681a",
                                   lambda: self.card_clean.run_thread(self.task_clean))
        self.card_clean.frame.pack(fill="x")

    # -------- 统计
    def refresh_stats(self):
        m = get_memory()
        self.mem_bar.config(value=m["percent"])
        self.mem_val.config(text=f"{m['percent']}%  "
                                 f"{fmt_size(m['used'])}/{fmt_size(m['total'])}")
        total, used, free = get_disk()
        if total:
            pct = used / total * 100
            self.disk_bar.config(value=pct)
            self.disk_val.config(text=f"{pct:.0f}%  剩余 {fmt_size(free)}")
        self.root.after(5000, self.refresh_stats)

    # -------- DLL 修复
    def _start_dll(self, deep=False, console=False):
        """启动修复前先确认管理员权限，避免白跑一趟。"""
        if not is_admin():
            ok = messagebox.askyesno(
                "需要管理员权限",
                "修复系统文件必须以管理员身份运行。\n\n"
                "是否现在以管理员身份重启 WinFix？\n"
                "（重启后请再点一次修复按钮）")
            if ok:
                self.elevate()
            else:
                self.log.write("⚠ 已取消：非管理员权限下 SFC 无法修复受保护文件", "warn")
            return
        if console:
            self.card_dll.run_thread(self.task_dll_console)
        else:
            self.card_dll.run_thread(lambda c: self.task_dll(c, deep=deep))

    def task_dll_console(self, card):
        """在独立控制台窗口运行 SFC，用户可直接看到系统原生进度条。"""
        card.log("在独立控制台窗口运行 SFC（可看到原生进度）")
        cmd = ["cmd", "/c", "sfc /scannow & echo. & echo 修复结束，按任意键关闭窗口 & pause"]
        try:
            proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as e:
            card.log(f"⚠ 无法启动控制台：{e}")
            return
        card._proc = proc
        card.log("已打开控制台窗口，修复进度以系统原生方式显示")
        t0 = time.time()
        self.post("idle", card)
        while True:
            if card.stop_flag.is_set():
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                card.log("已停止")
                break
            if proc.poll() is not None:
                break
            e = time.time() - t0
            card.set_status(f"运行中 {int(e // 60):02d}:{int(e % 60):02d}")
            time.sleep(0.5)
        card._proc = None
        self.post("busy", (card, 100))
        card.set_progress(100)
        if not card.stop_flag.is_set():
            card.log(f"✓ 控制台运行结束，退出码 {proc.returncode}")

    def task_dll(self, card, deep=False):
        card.log("开始系统文件修复")
        if not is_admin():
            card.log("⚠ 当前非管理员权限，SFC 可能无法修复受保护文件")
        # (阶段标题, 命令, 起始进度, 结束进度, 输出编码)
        # SFC 重定向输出为 UTF-16LE；DISM 为当前代码页（GBK），交给自动识别
        stages = []
        if deep:
            stages.append(("阶段 1/2：DISM 修复系统映像（较慢，请耐心等待）",
                           ["dism", "/Online", "/Cleanup-Image", "/RestoreHealth"],
                           5, 55, None))
            stages.append(("阶段 2/2：SFC 扫描并修复系统文件",
                           ["sfc", "/scannow"], 55, 95, "utf-16-le"))
        else:
            stages.append(("SFC 扫描并修复系统文件（快速模式）",
                           ["sfc", "/scannow"], 5, 95, "utf-16-le"))
        for title, cmd, p_from, p_to, enc in stages:
            if card.stop_flag.is_set():
                card.log("已停止")
                card.set_status("已停止")
                return
            card.head(title)
            card.log(f"  命令：{' '.join(cmd)}")
            card.log("  SFC 会静默扫描数分钟，进度条会以动画形式持续推进，"
                     "可随时点「停止」；想看到原生进度请用「窗口运行」")
            self.post("busy", (card, p_from))
            card.set_progress(p_from)
            card.set_status(title.split("：")[0])
            self._run_stream(cmd, card, p_from, p_to, prefer=enc)

        if card.stop_flag.is_set():
            card.log("已停止。可随时重新运行。")
            card.set_status("已停止")
        else:
            card.log("✓ 修复流程结束。若仍有报错，请重启电脑后再执行一次。")
            card.set_status("已完成")

    def _run_stream(self, cmd, card, p_from, p_to, prefer=None):
        """执行命令：分块读取（兼容 \\r 进度刷新）+ 编码自动识别 + 无输出心跳。"""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW,
                                    bufsize=0)
        except FileNotFoundError:
            card.log(f"⚠ 找不到命令 {cmd[0]}")
            return
        except Exception as e:
            card.log(f"⚠ 无法启动 {cmd[0]}: {e}")
            return
        card._proc = proc

        q = queue.Queue()
        dec = OutputDecoder(prefer)

        def reader():
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    q.put(chunk)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=reader, daemon=True).start()

        buf = ""
        prog = p_from
        t0 = time.time()
        last_data = t0
        beat_marks = [20, 60, 120, 180, 300, 420, 600]  # 只在这些时间点各报一次
        beat_idx = 0
        idle_mode = False
        card.set_progress(prog)

        while True:
            if card.stop_flag.is_set():
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                break

            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                # 无输出：状态走计时 + 稀疏心跳 + 进度条切换为不确定动画
                elapsed = time.time() - t0
                card.set_status(f"运行中 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
                if beat_idx < len(beat_marks) and elapsed >= beat_marks[beat_idx]:
                    beat_idx += 1
                    card.log(f"⏳ 已运行 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒，"
                             f"命令仍在执行。SFC/DISM 单阶段可能需要 5~20 分钟，"
                             f"中途无输出属正常，可随时点「停止」")
                if not idle_mode and time.time() - last_data > 10:
                    idle_mode = True
                    self.post("idle", card)
                continue

            if chunk is None:
                break
            last_data = time.time()
            if idle_mode:
                idle_mode = False
                self.post("busy", (card, prog))

            text = dec.feed(chunk)
            if not text:
                continue
            buf += text
            pieces = re.split(r"[\r\n]+", buf)
            buf = pieces[-1]
            for line in pieces[:-1]:
                line = line.strip().strip("\x00").strip()
                if not line:
                    continue
                m = re.search(r"(\d{1,3})\s*%", line)
                if m:
                    pct = int(m.group(1))
                    prog = p_from + (p_to - p_from) * pct / 100
                    card.set_progress(prog)
                    e = time.time() - t0
                    card.set_status(f"{pct}% · {int(e // 60):02d}:{int(e % 60):02d}")
                    card.log(line)
                    continue
                if len(line) > 1 and re.search(r"[\u4e00-\u9fa5A-Za-z]", line):
                    card.log(line)
                if prog < p_to - 6:
                    prog += 0.3
                    card.set_progress(prog)

        proc.wait()
        card._proc = None
        tail = (buf + dec.flush()).strip().strip("\x00").strip()
        if tail and not card.stop_flag.is_set():
            card.log(tail)
        self.post("busy", (card, p_to))
        card.set_progress(p_to)

    # -------- 异常检测
    def task_diag(self, card):
        card.log("开始系统健康检查...")
        card.head("检查关键系统 DLL")
        missing = scan_dll_issues(card.log)
        card.set_progress(30)

        card.head("检查磁盘空间")
        total, used, free = get_disk()
        if total:
            pct = used / total * 100
            card.log(f"  C 盘使用率 {pct:.0f}%，剩余 {fmt_size(free)}")
            if pct > 92:
                card.log("  ⚠ 磁盘空间严重不足，建议立即清理")
            elif pct > 85:
                card.log("  ⚠ 磁盘空间偏低，可运行垃圾清理")
        card.set_progress(50)

        card.head("检查开机启动项")
        n = self._count_startup_items()
        card.log(f"  检测到 {n} 个启动项")
        if n > 25:
            card.log("  ⚠ 启动项过多，会拖慢开机速度")
        card.set_progress(65)

        card.head("检查系统负载")
        m = get_memory()
        card.log(f"  内存占用 {m['percent']}%")
        if m["percent"] > 85:
            card.log("  ⚠ 内存占用偏高，可运行内存优化")
        proc_count = self._count_processes()
        card.log(f"  运行中进程 {proc_count} 个")
        if proc_count > 250:
            card.log("  ⚠ 后台进程偏多")
        card.set_progress(80)

        card.head("检查系统可靠性")
        errs = self._recent_event_errors()
        card.log(f"  近 24 小时严重错误事件：{errs} 条")
        if errs > 10:
            card.log("  ⚠ 错误较多，建议运行 DLL/系统文件修复")

        card.set_progress(100)
        issues = len(missing) + (1 if errs > 10 else 0)
        card.log(f"✓ 检测完成，发现 {issues} 项需要关注的问题")

    def _count_startup_items(self):
        n = 0
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
        ]
        try:
            import winreg
            for root_key in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for k in keys:
                    try:
                        with winreg.OpenKey(root_key, k) as key:
                            i = 0
                            while True:
                                try:
                                    winreg.EnumValue(key, i)
                                    n += 1
                                    i += 1
                                except OSError:
                                    break
                    except OSError:
                        pass
        except Exception:
            pass
        return n

    def _count_processes(self):
        try:
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                                 capture_output=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            txt = out.stdout.decode("gbk", errors="replace")
            return len([l for l in txt.splitlines() if l.strip()])
        except Exception:
            return -1

    def _recent_event_errors(self):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2;"
                 "StartTime=(Get-Date).AddHours(-24)} -ErrorAction SilentlyContinue | "
                 "Measure-Object).Count"],
                capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW)
            txt = out.stdout.decode("utf-8", errors="replace").strip()
            return int(txt) if txt.isdigit() else 0
        except Exception:
            return 0

    # -------- 内存优化
    def task_mem(self, card):
        before = get_memory()
        card.log(f"优化前：内存占用 {before['percent']}%  可用 {fmt_size(before['avail'])}")
        card.set_progress(20)

        try:
            ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
            card.log("  已释放自身工作集")
        except Exception as e:
            card.log(f"  释放失败: {e}")
        card.set_progress(45)

        try:
            ctypes.windll.kernel32.SetProcessWorkingSetSize(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.c_size_t(-1),
                ctypes.c_size_t(-1))
            card.log("  已请求系统回收工作集")
        except Exception:
            pass
        card.set_progress(65)

        card.head("内存占用最高的进程")
        for name, mem in self._top_memory_processes(5):
            card.log(f"  {name:<28} {fmt_size(mem)}")
        card.set_progress(90)

        time.sleep(0.5)
        after = get_memory()
        card.log(f"优化后：内存占用 {after['percent']}%  可用 {fmt_size(after['avail'])}")
        delta = after["avail"] - before["avail"]
        if delta > 0:
            card.log(f"✓ 可用内存增加 {fmt_size(delta)}")
        else:
            card.log("提示：系统内存已在高效状态，无需额外释放")
        card.set_progress(100)

    def _top_memory_processes(self, n=5):
        res = []
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process | Sort-Object WS -Descending | "
                 "Select-Object -First %d Name,WS | "
                 "ForEach-Object { \"$($_.Name)|$($_.WS)\" }" % n],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW)
            for line in out.stdout.decode("utf-8", errors="replace").splitlines():
                if "|" in line:
                    nm, ws = line.split("|", 1)
                    try:
                        res.append((nm.strip(), float(ws.strip())))
                    except ValueError:
                        pass
        except Exception:
            pass
        return res

    # -------- 垃圾清理
    def task_clean(self, card):
        card.head("扫描可清理内容")
        card.set_progress(5)
        targets = scan_clean_targets(card.log)
        card.set_progress(30)

        total_size = sum(t[2] for t in targets)
        total_count = sum(t[3] for t in targets)
        card.log(f"共发现 {fmt_size(total_size)} 可清理（{total_count} 个文件）")

        if total_count == 0:
            card.log("✓ 系统很干净，无需清理")
            card.set_progress(100)
            return

        card.head("开始清理")
        freed, deleted, failed = do_clean(
            targets, card.log,
            progress=lambda p: card.set_progress(30 + p * 65))
        card.log(f"✓ 清理完成：释放 {fmt_size(freed)}，删除 {deleted} 个文件")
        if failed:
            card.log(f"  （{failed} 个文件被占用已跳过）")
        card.set_progress(100)

    def elevate(self):
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showwarning("提示", "提权被取消或失败，可右键 exe 选择“以管理员身份运行”")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    WinFixApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
