# 打包成免安装 exe

本文记录把 `screen_recorder.py` 打成「不需要 Python」的发布包的完整步骤，以及单文件 / 文件夹两种方案的实测对比。

## 1. 环境

- Windows 10 / 11
- 系统 Python 3.9+，**必须带 tkinter**（python.org 官方安装包默认带；Windows Store 版和部分精简版可能缺 Tcl）
- PyInstaller（`pip install pyinstaller`）

验证 tkinter 是否可用：

```bat
python -c "import tkinter; tkinter.Tk(); print('ok')"
```

报 `Can't find a usable init.tcl` 就说明这个 Python 缺 Tcl 库，换官方安装版。

## 2. 准备素材

| 文件 | 用途 | 来源 |
| --- | --- | --- |
| `ffmpeg.exe` | 内置录制内核 | 建议用 gyan.dev 的 essentials 静态版（约 79 MB，含 `gdigrab` / `libx264` / `qsv`） |
| `app.ico` | 程序图标 | 见第 5 节 |

把 `ffmpeg.exe` 放到项目根目录。构建后再复制进产物目录，**不要用 `--add-data` 塞进单文件**，否则每次启动都要解压 79 MB（见第 3 节）。

## 3. 两种打包方案

### 3.1 单文件（`--onefile`）—— 不推荐

```bat
pyinstaller --noconfirm --clean --onefile --windowed --name ScreenRecorder ^
  --icon app.ico --add-data "ffmpeg.exe;." screen_recorder.py
```

产物 38.2 MB，`--cli --duration 3` 能正常出 3 秒 1080p30 的 MP4，但**每次启动都要把 79 MB 的 ffmpeg 解压到 `_MEIPASS`**：

| 启动 | 实测 |
| --- | --- |
| 冷启动 | 10.26 s |
| 热启动 | 7.2 ～ 7.7 s |

7 秒以上的首屏时间不可接受，放弃。

### 3.2 文件夹版（`--onedir`）—— 采用

```bat
pyinstaller --noconfirm --clean --onedir --windowed --name ScreenRecorder ^
  --icon app.ico screen_recorder.py
copy ffmpeg.exe dist\ScreenRecorder\
```

产物结构：

```
dist\ScreenRecorder\
  ScreenRecorder.exe    1.8 MB   启动器
  ffmpeg.exe            79 MB    录制内核（程序按同目录优先查找）
  _internal\            ~21 MB  Python 运行时 + Tcl/Tk
```

合计 102.1 MB。启动实测：

| 阶段 | 耗时 |
| --- | --- |
| 首次启动（Defender 扫描新 exe） | 2.89 s |
| 之后每次启动 | 1.36 s |
| 老版本（启动时同步探测麦克风 + 硬件编码） | 4.38 s |

差距来自「后台探测」：界面先出来，麦克风枚举和硬件编码器测试放到后台线程，完成后用 `root.after` 回填下拉项。

## 4. 产物自检

```bat
:: 1. 命令行录 3 秒，确认内置 ffmpeg 能被找到
dist\ScreenRecorder\ScreenRecorder.exe --cli --duration 3 -o %TEMP%\exe_test.mp4

:: 2. 用 ffprobe 核对容器与参数
ffprobe -hide_banner -show_format -show_streams %TEMP%\exe_test.mp4

:: 3. 冒烟测试界面能否构建
dist\ScreenRecorder\ScreenRecorder.exe --smoke
```

期望：`1920x1080`、`h264`、`yuv420p`、`30 fps`、时长接近 3.00 s。

> `--windowed` 的 exe 没有控制台，`--cli` 模式的文件照常产出，但看不到日志输出。需要看日志就做一份控制台版：

```bat
pyinstaller --noconfirm --clean --onedir --console --name ScreenRecorder-console screen_recorder.py
```

## 5. 图标

`app.ico` 需要包含多尺寸，否则任务栏/资源管理器里会糊。用 Pillow 生成：

```python
from PIL import Image, ImageDraw

img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([6, 6, 250, 250], radius=54, fill=(30, 31, 36, 255),
                    outline=(86, 90, 104, 255), width=4)
d.ellipse([64, 64, 192, 192], fill=(255, 59, 48, 255))
img.save("app.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
```

## 6. 分发

```powershell
Compress-Archive -Path dist\ScreenRecorder -DestinationPath ScreenRecorder-win64-portable.zip
```

用 `zipfile` 也行（能控制条目名编码，避免中文文件名乱码）：

```python
import zipfile
from pathlib import Path

src = Path("dist/ScreenRecorder")
with zipfile.ZipFile("ScreenRecorder-win64-portable.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            z.write(p, (Path("ScreenRecorder") / p.relative_to(src)).as_posix())
```

zip 约 38 MB。收件人解压即用，无需 Python、无需 FFmpeg。

## 7. 常见问题

**exe 双击没反应**
`--windowed` 没有控制台。程序会把异常写入 exe 同目录的 `screen_recorder_error.log` 并弹 MessageBox，先看日志。

**打包后日志写不了**
`__file__` 在 PyInstaller 里指向 `_MEIPASS` 临时目录，退出即删。日志路径要用 `sys.executable` 的目录（本项目的 `app_dir()` 已处理）。

**exe 找不到 ffmpeg**
查找顺序是 `--ffmpeg` → `SCREEN_RECORDER_FFMPEG` → exe 同目录 → 旧版 `_MEIPASS` → PATH。把 `ffmpeg.exe` 放在 exe 旁边最省事。

**体积能不能再小**
79 MB 的 ffmpeg 是主要体积。换成 `ffmpeg-minimal` 类精简构建能降到 20 MB 左右，但要确认裁剪版仍带 `gdigrab`、`libx264` 和你要用的硬件编码器，风险是录到一半才发现不支持。
