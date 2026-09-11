# Photo Culler（摄影选片）

一个为 Windows 摄影工作流制作的轻量选片工具。它只负责浏览、标记与复制保留原片，不会改动、移动或重命名来源照片。

## 操作

1. 运行 `Photo Culler.exe`，选择包含照片的文件夹。
2. 用 `[` / `]` 切换照片；按 `Space` 标记或取消“保留”。同名的 `DNG + JPG/JPEG` 会自动合并为一个选片项目，优先显示 JPG。
3. 下方缩略图中的黄色星号表示已保留；可勾选“只看保留”。
4. 按 `E` 或点击“导出保留照片”，选择输出文件夹。软件会复制原始文件，并尽量保留原始拍摄时间等文件属性；被保留的 `DNG + JPG/JPEG` 绑定组会一起导出。

右侧控制栏与预览区域之间的分隔线可以用鼠标拖动，以调整控制栏宽度；软件会记住下次启动时的宽度。

## 放大查看

- 鼠标放在大图上滚动滚轮，可围绕鼠标位置放大或缩小。
- `Z` 在“适合屏幕”和 `100%` 原始像素间切换；`1` 直接切至 `100%`。
- `+` / `-` 可逐级缩放，最高 `400%`；放大后按住鼠标左键拖动可查看其他区域。
- 选到下一张照片时自动回到“适合屏幕”。RAW+JPG 绑定组一律以 JPG 做预览和放大检查，RAW 保持绑定，仅在导出时一并复制。
- 拖动时程序会先移动已准备好的画面缓冲，停止操作后再补上原始像素的高质量显示；因此交互响应与最终清晰度分开处理。

对于 RAW+JPG 绑定组，第一次按 `Space` 默认选择两张；顶部“模式”按钮或按 `F` 会按“RAW+JPG → 仅 JPG → 仅 RAW → RAW+JPG”的顺序循环。模式与保留状态相互独立：修改模式不会自动保留/取消，取消保留也不会重置模式。导出时只复制当前模式指定的原文件，选片记录会自动保存。

“全不保留”（`Ctrl+Shift+X`）会在确认后取消当前文件夹所有保留状态，但保留各组的 RAW/JPG 模式；“重置模式”（`Ctrl+Shift+M`）会在确认后把所有绑定组模式恢复为 RAW+JPG，但不改变保留状态。

## 格式

支持 JPG/JPEG、PNG、TIFF/TIF 与 DNG。DNG 优先读取相机内嵌预览图；缺少内嵌预览时会解码 RAW，因此首次显示可能比 JPG 稍慢。导出 DNG 时复制的是未经修改的原始 DNG 文件。

## 选片记录

每个照片文件夹的“保留”标记会自动保存到当前 Windows 用户的本地应用数据目录；源照片文件夹不会被写入选片记录。重新打开同一文件夹可继续选片。

## JPG 预载

打开文件夹后，软件会在后台将其中全部 JPG/JPEG 解码进内存，并在状态栏显示进度。RAW+JPG 绑定组只预载 JPG；RAW 不会被解码进内存，但导出保留项目时仍会与 JPG 一同复制。预载完成后，再用左右方向键浏览 JPG 不需要再次读取磁盘。

## 构建（开发用）

在此目录建立虚拟环境后安装 `requirements.txt` 和 PyInstaller。由于 Windows Python 的 Tk 目录结构可能不同，先把 Python 的 `tcl` 目录复制成项目旁的 `tk_runtime\tcl`，并把 `tcl86t.dll`、`tk86t.dll` 放进 `tk_runtime\bin`，再运行：

```powershell
$env:TCL_LIBRARY = (Resolve-Path '..\tk_runtime\tcl\tcl8.6')
$env:TK_LIBRARY = (Resolve-Path '..\tk_runtime\tcl\tk8.6')
pyinstaller --noconfirm --clean --onefile --windowed --name "Photo Culler" --collect-all rawpy --hidden-import tkinter --hidden-import _tkinter --add-data "..\tk_runtime\tcl;tcl" --add-binary "..\tk_runtime\bin\tcl86t.dll;bin" --add-binary "..\tk_runtime\bin\tk86t.dll;bin" app.py
```
