# Windows 使用说明

## 系统要求

- Windows 10 或 Windows 11
- 可使用 WinGet 或 PowerShell 联网安装软件

依赖安装脚本会优先复用已有的 Python 3.10 或更高版本；未找到时会自动安装 Python 3.12。

## 双击启动

1. 下载或解压完整项目文件夹。
2. 首次使用时双击 `WINDOWS-1-安装依赖.bat`，等待 Python 和项目依赖安装完成。
3. 双击 `WINDOWS-2-启动WB分析.bat` 启动软件。

如果 Windows 阻止脚本运行，可右键脚本并选择“打开”，或在项目目录的命令提示符中运行：

```bat
WINDOWS-1-安装依赖.bat
WINDOWS-2-启动WB分析.bat
```

## 手动启动

在项目目录打开 PowerShell 或命令提示符：

```bat
py -3 -m pip install -r requirements.txt
py -3 run_wb.py
```

如果系统没有 Python Launcher，将 `py -3` 改为 `python`。

## Windows 操作

- `Ctrl+O`：为当前内参或目的角色选择图像。
- `Ctrl+S`：导出 CSV。
- 鼠标滚轮：缩放图像。
- 右键识别框：重命名或删除。
- `Delete` 或 `Backspace`：删除所选识别框。

## 常见问题

### 提示找不到 Python

重新运行 `WINDOWS-1-安装依赖.bat`。若自动安装失败，可从 [python.org](https://www.python.org/downloads/windows/) 手动安装，并勾选 **Add python.exe to PATH**。

### 提示缺少 Tkinter

使用 python.org 的 Windows 安装程序重新安装 Python，并确保 Tcl/Tk 组件未被取消。

### 依赖安装失败

在项目目录运行：

```bat
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements.txt
```

### 高分辨率或小屏幕显示不完整

软件会根据当前屏幕尺寸自动调整首次窗口大小，结果表仍可使用横向和纵向滚动条浏览。
