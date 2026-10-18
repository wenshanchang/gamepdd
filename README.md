# WinFix 系统维护工具

Windows 系统维护小工具：**DLL / 系统文件修复、电脑异常检测、内存优化、垃圾清理**，
带图形界面，单文件 exe，绿色免安装。

仓库同时包含**安装程序**与**独立卸载程序**的源码，安装时可以选择从本仓库拉取最新源码。

---

## 功能

| 模块 | 说明 |
| --- | --- |
| DLL / 系统文件修复 | `快速修复`：`sfc /scannow`；`深度修复`：`DISM /RestoreHealth` + `sfc`；`窗口运行`：在独立控制台看系统原生进度。支持随时停止 |
| 电脑异常检测 | 关键系统 DLL 完整性、WinSxS、C 盘空间、启动项数量、内存/进程负载、近 24 小时系统错误事件 |
| 内存优化 | 回收进程工作集释放物理内存，展示优化前后对比与内存占用 Top 5 进程 |
| 垃圾清理 | 临时文件、Windows 更新缓存、错误转储、Prefetch、缩略图、回收站、Edge/Chrome 缓存，只删安全项，被占用自动跳过 |

界面为**左侧操作、右侧日志**布局：日志带时间戳与颜色标记，可一键保存为文件。

> SFC / DISM 本身耗时较长（5~20 分钟）且中途几乎无输出，这是 Windows 的行为。
> 工具会显示计时、稀疏心跳与动画进度条；想要看到真实百分比请用「窗口运行」。

---

## 目录结构

```
winfix.py        主程序（Tkinter GUI，仅依赖 Python 标准库）
installer.py     安装程序（安装向导 + 在线拉取源码 + 注册卸载信息）
uninstaller.py   独立卸载程序（删除快捷方式 / 注册表 / 安装目录）
build.py         一键构建脚本（主程序 → 卸载程序 → 内嵌包 → 安装程序）
version.json     版本清单，安装程序据此提示版本一致性
```

## 直接运行源码

需要 Python 3.9+（标准库即可，无需第三方依赖）：

```bash
python winfix.py
```

修复类功能建议以管理员身份运行，否则 SFC 无法修复受保护文件。

## 构建

```bash
pip install pyinstaller
python build.py            # 全量构建，产物在 dist/
python build.py app        # 只构建主程序
python build.py installer  # 只重建内嵌包与安装程序
```

产物：

- `dist/WinFix系统维护工具.exe` 主程序（约 13 MB）
- `dist/卸载程序.exe` 独立卸载程序
- `dist/WinFix安装程序.exe` 安装包（内嵌上述两个 exe，约 25 MB）

## 安装

运行 `WinFix安装程序.exe`：

1. 选择安装位置（默认 `%LOCALAPPDATA%\Programs\WinFix`，无需管理员权限）
2. 可选「从 GitHub 拉取最新源码到 source 目录」（默认勾选，需联网）
3. 可选创建桌面 / 开始菜单快捷方式（默认勾选）
4. 完成后可从桌面或开始菜单启动，卸载入口在开始菜单 `WinFix\卸载 WinFix`
   或「设置 → 应用」

命令行：

```bash
WinFix安装程序.exe --uninstall                     # 调用已安装的卸载程序
set WF_INSTALL_DIR=D:\Apps\WinFix && WinFix安装程序.exe   # 静默安装
```

## 说明与边界

- 安装时拉取的是**源代码**，供查看与二次开发；实际运行的是随安装包内置的 exe
  （用户机器不一定装 Python，直接跑源码不可靠）。
- 源码目录下会附带 `version.json`，安装程序会比对版本并提示仓库是否更新。
- 清理与修复均使用 Windows 自带机制，不修改系统核心配置；但任何系统级操作都有风险，
  请自行评估并在重要数据已备份的前提下使用。

## 许可

MIT
