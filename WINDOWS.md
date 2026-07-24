# Windows 使用说明

## 系统要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 建议从 [python.org](https://www.python.org/downloads/windows/) 安装 64 位 Python

安装 Python 时请勾选 **Add python.exe to PATH**，并保留 Tcl/Tk 组件。

## 双击启动

1. 下载或解压完整项目文件夹。
2. 双击 `start_wb_windows.bat`。
3. 首次运行时，脚本会自动安装 `requirements.txt` 中缺少的依赖，然后启动软件。

如果 Windows 阻止脚本运行，可右键脚本并选择“打开”，或在项目目录的命令提示符中运行：

```bat
start_wb_windows.bat
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

重新安装 Python，并勾选 **Add python.exe to PATH**。安装后关闭并重新打开命令提示符。

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
