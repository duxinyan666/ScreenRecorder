#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地录屏工具（Windows）
======================

基于 FFmpeg 的 gdigrab 桌面采集，输出标准 MP4（H.264 + AAC）。

特点
----
* 整屏 / 主显示器 / 鼠标拖拽框选区域录制
* 帧率 15 / 24 / 30 / 60 可选
* 编码器可选软件编码（libx264，兼容性最好）或 Intel QSV 硬件加速（省 CPU）
* 可选同时录制麦克风（AAC），自动音视频合流
* 停止时向 ffmpeg 发送 q，正常写入 MP4 索引，文件不会损坏
* 图形界面 + 命令行两种用法（命令行可用于自动化、定时任务）

用法
----
    python screen_recorder.py                    # 打开图形界面
    python screen_recorder.py --list-devices     # 列出麦克风设备
    python screen_recorder.py --list-screens     # 列出显示器与虚拟桌面
    python screen_recorder.py --cli --duration 10 -o demo.mp4 --fps 30
    python screen_recorder.py --cli --region 100,100,800,600 --audio "麦克风阵列..." -o area.mp4

依赖：Python 3.9+（自带 tkinter）、FFmpeg（含 gdigrab、libx264）
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, Optional

APP_TITLE = "本地录屏"
APP_SUBTITLE = "FFmpeg gdigrab · 输出 MP4"

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

CREATE_NO_WINDOW = 0x08000000

QUALITY_PRESETS = {
    "高质量（体积大）": 18,
    "标准（推荐）": 20,
    "较小体积": 24,
    "最小体积": 28,
}


# ----------------------------------------------------------------------------
# 基础环境
# ----------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    """让 Tk 与 gdigrab 使用同一套物理像素坐标（高分屏必须）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def virtual_screen() -> tuple[int, int, int, int]:
    """返回虚拟桌面 (x, y, width, height)，单位物理像素。"""
    try:
        u = ctypes.windll.user32
        return (
            int(u.GetSystemMetrics(SM_XVIRTUALSCREEN)),
            int(u.GetSystemMetrics(SM_YVIRTUALSCREEN)),
            int(u.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
            int(u.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
        )
    except Exception:
        return (0, 0, 1920, 1080)


def decode_output(data: bytes) -> str:
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "replace")


def run_quiet(cmd: list[str], timeout: float = 8.0) -> tuple[int, str]:
    """静默执行外部命令，返回 (退出码, 合并输出)。"""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
        return p.returncode, decode_output(p.stdout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ----------------------------------------------------------------------------
# FFmpeg 发现与能力探测
# ----------------------------------------------------------------------------
@dataclass
class FFmpegInfo:
    path: str
    version: str = ""
    encoders: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)

    @property
    def ffprobe(self) -> str:
        p = Path(self.path).with_name("ffprobe.exe")
        return str(p) if p.exists() else ""

    def supports(self, encoder: str) -> bool:
        return encoder in self.encoders


def app_dir() -> Path:
    """程序所在目录：PyInstaller 打包后指向 exe 所在目录，否则指向脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _candidate_ffmpeg(explicit: Optional[str] = None) -> Iterable[str]:
    if explicit:
        yield explicit
    env = os.environ.get("SCREEN_RECORDER_FFMPEG")
    if env:
        yield env
    here = app_dir()
    for rel in ("ffmpeg.exe", os.path.join("ffmpeg", "bin", "ffmpeg.exe"),
                os.path.join("bin", "ffmpeg.exe")):
        p = here / rel
        if p.exists():
            yield str(p)
    # 单文件 exe 内置的 ffmpeg 会被解压到临时目录 _MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "ffmpeg.exe"
        if p.exists():
            yield str(p)
    which = shutil.which("ffmpeg")
    if which:
        yield which
    patterns = [
        r"C:\Users\*\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\*\ffmpeg.exe",
    ]
    for pat in patterns:
        for hit in glob.glob(pat, recursive=True):
            yield hit


def probe_ffmpeg(path: str) -> Optional[FFmpegInfo]:
    code, out = run_quiet([path, "-hide_banner", "-version"], timeout=8)
    if code != 0 or "ffmpeg version" not in out:
        return None
    info = FFmpegInfo(path=path)
    m = re.search(r"ffmpeg version ([^\s]+)", out)
    info.version = m.group(1) if m else "unknown"

    code, out = run_quiet([path, "-hide_banner", "-encoders"], timeout=8)
    if code == 0:
        info.encoders = set(re.findall(r"^\s*V[.A-Z]*\s+(\S+)", out, re.MULTILINE))
    code, out = run_quiet([path, "-hide_banner", "-devices"], timeout=8)
    if code == 0:
        info.devices = set(re.findall(r"^\s*D[.A-Z]*\s+(\S+)", out, re.MULTILINE))
    return info


def find_ffmpeg(explicit: Optional[str] = None, prefer_qsv: bool = False) -> Optional[FFmpegInfo]:
    """按优先级找到可用的 ffmpeg；要求支持 gdigrab + libx264。"""
    fallback: Optional[FFmpegInfo] = None
    for cand in _candidate_ffmpeg(explicit):
        try:
            if not Path(cand).exists() and not shutil.which(cand):
                continue
        except OSError:
            continue
        info = probe_ffmpeg(cand)
        if not info or "gdigrab" not in info.devices or not info.supports("libx264"):
            continue
        if prefer_qsv and info.supports("h264_qsv"):
            return info
        if fallback is None:
            fallback = info
    return fallback


def list_audio_devices(ffmpeg_path: str) -> list[str]:
    _, out = run_quiet(
        [ffmpeg_path, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
        timeout=8,
    )
    return re.findall(r'\] "([^"]+)" \(audio\)', out)


def list_video_devices(ffmpeg_path: str) -> list[str]:
    _, out = run_quiet(
        [ffmpeg_path, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
        timeout=8,
    )
    return re.findall(r'\] "([^"]+)" \(video\)', out)


# ----------------------------------------------------------------------------
# 录制配置与命令构建
# ----------------------------------------------------------------------------
@dataclass
class RecordConfig:
    output: str
    fps: int = 30
    region: Optional[tuple[int, int, int, int]] = None   # x, y, w, h（虚拟桌面坐标系）
    audio_device: Optional[str] = None
    encoder: str = "libx264"
    crf: int = 20
    draw_mouse: bool = True
    max_seconds: Optional[float] = None
    faststart: bool = True


def _vsync_filter_args() -> list[str]:
    return []


def encoder_args(encoder: str, crf: int, fps: int) -> list[str]:
    gop = str(max(1, fps * 2))
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(crf), "-preset", "veryfast",
                "-g", gop, "-pix_fmt", "nv12"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(crf),
                "-g", gop, "-pix_fmt", "yuv420p"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", str(crf),
                "-qp_p", str(crf), "-g", gop]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-g", gop, "-pix_fmt", "yuv420p"]


_ENC_CACHE: dict[tuple[str, str], bool] = {}


def encoder_works(ffmpeg_path: str, encoder: str) -> bool:
    """真机探测硬件编码器能否初始化（编译进去不等于驱动可用）。"""
    key = (ffmpeg_path, encoder)
    if key in _ENC_CACHE:
        return _ENC_CACHE[key]
    cmd = list([ffmpeg_path, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc2=size=256x144:rate=30:duration=0.2"])
    cmd += encoder_args(encoder, 22, 30)
    cmd += ["-f", "null", "-"]
    code, _ = run_quiet(cmd, timeout=15)
    _ENC_CACHE[key] = code == 0
    return _ENC_CACHE[key]


def build_command(ffmpeg_path: str, cfg: RecordConfig) -> list[str]:
    cmd: list[str] = [ffmpeg_path, "-hide_banner", "-loglevel", "info", "-y"]

    # 视频输入：Windows 桌面采集
    # thread_queue_size 给采集线程独立缓冲，避免与音频读取相互阻塞导致掉帧
    cmd += ["-f", "gdigrab", "-framerate", str(cfg.fps),
            "-thread_queue_size", "512",
            "-draw_mouse", "1" if cfg.draw_mouse else "0"]
    if cfg.region:
        x, y, w, h = cfg.region
        cmd += ["-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}"]
    cmd += ["-i", "desktop"]

    # 音频输入（可选）
    if cfg.audio_device:
        name = cfg.audio_device
        if "," in name or '"' in name:
            name = '"%s"' % name.replace('"', "")
        cmd += ["-f", "dshow", "-thread_queue_size", "1024", "-i", f"audio={name}"]

    cmd += encoder_args(cfg.encoder, cfg.crf, cfg.fps)

    if cfg.audio_device:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]

    if cfg.max_seconds:
        cmd += ["-t", f"{cfg.max_seconds:.2f}"]

    if cfg.faststart:
        cmd += ["-movflags", "+faststart"]

    cmd += ["-f", "mp4", cfg.output]
    return cmd


TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
FRAME_RE = re.compile(r"frame=\s*(\d+)")
FPS_RE = re.compile(r"fps=\s*([\d.]+)")
SIZE_RE = re.compile(r"size=\s*([\d.]+\s*\w+)")
SPEED_RE = re.compile(r"speed=\s*([\d.]+x)")


class Recorder:
    """封装 ffmpeg 子进程：启动、读取进度、优雅停止。"""

    def __init__(self, ffmpeg_path: str, cfg: RecordConfig,
                 on_log: Optional[Callable[[str], None]] = None) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.cfg = cfg
        self.on_log = on_log
        self.proc: Optional[subprocess.Popen] = None
        self.error: str = ""
        self.progress: dict[str, str] = {}
        self._lines: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self.started_at: float = 0.0
        self._stop_flag = threading.Event()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        out_path = Path(self.cfg.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_command(self.ffmpeg_path, self.cfg)
        if self.on_log:
            self.on_log(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE，避免闪出控制台窗口
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            startupinfo=startupinfo,
        )
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self.proc and self.proc.stderr
        stderr = self.proc.stderr
        buf = b""
        tail: list[str] = []

        def handle(raw_line: bytes) -> None:
            text = decode_output(raw_line).strip()
            if not text:
                return
            tail.append(text)
            if len(tail) > 60:
                tail.pop(0)
            self._lines = list(tail)
            self._parse(text)

        while True:
            try:
                chunk = stderr.read1(4096)   # type: ignore[attr-defined]
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            # ffmpeg 的进度用 \r 覆盖同一行，必须按 \r 和 \n 一起切分
            parts = re.split(rb"[\r\n]+", buf)
            buf = parts.pop() if parts else b""
            for part in parts:
                handle(part)
        if buf.strip():
            handle(buf)
        self.error = "\n".join(
            l for l in tail if "error" in l.lower() or "invalid" in l.lower()
            or "failed" in l.lower()) or "\n".join(tail[-8:])

    def _parse(self, line: str) -> None:
        m = TIME_RE.search(line)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            total = h * 3600 + mi * 60 + s
            self.progress["time"] = self._fmt(total)
            self.progress["seconds"] = f"{total:.2f}"
        m = FRAME_RE.search(line)
        if m:
            self.progress["frame"] = m.group(1)
        m = FPS_RE.search(line)
        if m:
            self.progress["fps"] = m.group(1)
        m = SIZE_RE.search(line)
        if m:
            self.progress["size"] = m.group(1).strip()
        m = SPEED_RE.search(line)
        if m:
            self.progress["speed"] = m.group(1)

    @staticmethod
    def _fmt(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self.proc.poll() if self.proc else None

    # -- 停止 -------------------------------------------------------------
    def stop(self, timeout: float = 30.0) -> Optional[int]:
        """发送 q 让 ffmpeg 写完 MP4 索引；超时则终止进程。"""
        if not self.proc:
            return None
        if self._stop_flag.is_set():
            return self.returncode()
        self._stop_flag.set()
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        if self._thread:
            self._thread.join(timeout=3)
        return self.returncode()


def probe_output(ffmpeg: FFmpegInfo, path: str) -> str:
    """用 ffprobe 校验产物，返回一行描述；失败返回空串。"""
    if not Path(path).exists() or Path(path).stat().st_size == 0:
        return ""
    if not ffmpeg.ffprobe:
        return ""
    cmd = [ffmpeg.ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    code, out = run_quiet(cmd, timeout=15)
    if code != 0:
        return ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return ""
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    parts: list[str] = []
    if fmt.get("duration"):
        parts.append(f"时长 {_fmt_sec(float(fmt['duration']))}")
    if video:
        parts.append(f"{video.get('width')}×{video.get('height')} {video.get('codec_name')}")
    if audio:
        parts.append(f"音轨 {audio.get('codec_name')}")
    if fmt.get("size"):
        parts.append(f"{float(fmt['size']) / 1048576:.2f} MB")
    return " · ".join(parts)


def _fmt_sec(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------
def default_output_dir() -> Path:
    home = Path.home()
    for name in ("Videos", "视频"):
        p = home / name
        if p.exists():
            return p / "录屏"
    return home / "录屏"


def cmd_list_screens() -> None:
    enable_dpi_awareness()
    vx, vy, vw, vh = virtual_screen()
    print(f"虚拟桌面: x={vx} y={vy} {vw}x{vh}")
    try:
        import ctypes.wintypes as wt
        class RECT(ctypes.Structure):
            _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                        ("right", wt.LONG), ("bottom", wt.LONG)]
        MONITORINFOF_PRIMARY = 1

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wt.DWORD)]

        monitors: list[MONITORINFO] = []

        @ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)
        def _cb(hmon, hdc, lprc, data):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                monitors.append(mi)
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
        for i, mi in enumerate(monitors, 1):
            r = mi.rcMonitor
            primary = " (主)" if mi.dwFlags & MONITORINFOF_PRIMARY else ""
            print(f"显示器 {i}{primary}: x={r.left} y={r.top} "
                  f"{r.right - r.left}x{r.bottom - r.top}")
    except Exception as exc:
        print(f"枚举显示器失败: {exc}")


def cmd_list_devices(ffmpeg: FFmpegInfo) -> None:
    print(f"FFmpeg: {ffmpeg.path} ({ffmpeg.version})")
    marks = []
    for enc, label in (("libx264", "软件编码"), ("h264_qsv", "Intel QSV"),
                       ("h264_nvenc", "NVIDIA NVENC"), ("h264_amf", "AMD AMF")):
        if not ffmpeg.supports(enc):
            continue
        ok = encoder_works(ffmpeg.path, enc)
        marks.append(f"{enc}({label})={'可用' if ok else '不可用'}")
    print("编码器: " + "  ".join(marks) if marks else "编码器: 未检测到 H.264 编码器")
    audio = list_audio_devices(ffmpeg.path)
    print("\n音频输入设备（麦克风）:")
    if audio:
        for i, name in enumerate(audio, 1):
            print(f"  {i}. {name}")
    else:
        print("  （未找到，将只录制画面）")
    video = list_video_devices(ffmpeg.path)
    if video:
        print("\n视频输入设备（摄像头，本工具暂不使用）:")
        for name in video:
            print(f"  - {name}")


def cmd_record(args: argparse.Namespace) -> int:
    ffmpeg = find_ffmpeg(args.ffmpeg, prefer_qsv=(args.encoder == "h264_qsv"))
    if not ffmpeg:
        print("错误：未找到可用的 FFmpeg（需支持 gdigrab 与 libx264）。", file=sys.stderr)
        return 2

    if not ffmpeg.supports(args.encoder):
        print(f"提示：当前 FFmpeg 不支持 {args.encoder}，改用 libx264。")
        args.encoder = "libx264"

    output = args.output or str(
        default_output_dir() / f"录屏_{datetime.now():%Y%m%d_%H%M%S}.mp4")
    if not output.lower().endswith(".mp4"):
        output += ".mp4"

    region = None
    if args.region:
        try:
            parts = [int(x) for x in re.split(r"[,x\s]+", args.region.strip()) if x]
            if len(parts) != 4:
                raise ValueError
            region = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            print("错误：--region 格式应为 x,y,宽,高，例如 100,100,800,600", file=sys.stderr)
            return 2
        vx, vy, vw, vh = virtual_screen()
        region = (max(vx, region[0]), max(vy, region[1]),
                  min(region[2], vw - (max(vx, region[0]) - vx)),
                  min(region[3], vh - (max(vy, region[1]) - vy)))

    cfg = RecordConfig(
        output=output, fps=args.fps, region=region,
        audio_device=args.audio or None,
        encoder=args.encoder, crf=args.crf,
        draw_mouse=not args.no_mouse, max_seconds=args.duration,
    )
    rec = Recorder(ffmpeg.path, cfg, on_log=lambda line: print("[ffmpeg] " + line))
    print(f"开始录制 -> {output}")
    if cfg.max_seconds:
        print(f"预计时长 {cfg.max_seconds:.0f} 秒，Ctrl+C 可提前结束。")
    rec.start()

    try:
        while rec.is_running():
            time.sleep(0.5)
            if cfg.max_seconds and rec.elapsed() >= cfg.max_seconds + 1.5:
                break
            if rec.elapsed() > 0:
                p = rec.progress
                sys.stdout.write(f"\r  录制中 {p.get('time', '00:00:00')}  "
                                 f"帧 {p.get('frame', '-')}  {p.get('size', '')}   ")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n收到中断，正在结束录制…")

    print("\n正在封装 MP4…")
    rec.stop()
    if rec.returncode() not in (0, None):
        print(f"警告：ffmpeg 退出码 {rec.returncode()}，详见上方日志。")

    size = Path(output).stat().st_size if Path(output).exists() else 0
    if size <= 0:
        print("失败：没有生成有效的视频文件。", file=sys.stderr)
        if rec.error:
            print(rec.error[-1500:], file=sys.stderr)
        return 1
    detail = probe_output(ffmpeg, output)
    print(f"已保存：{output}  ({size / 1048576:.2f} MB)")
    if detail:
        print(f"文件信息：{detail}")
    return 0


# ----------------------------------------------------------------------------
# 图形界面
# ----------------------------------------------------------------------------
class RegionSelector:
    """全屏半透明框选窗口，拖拽鼠标得到区域（物理像素坐标）。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.result: Optional[tuple[int, int, int, int]] = None
        vx, vy, vw, vh = virtual_screen()
        # Tk 的 geometry 不支持负偏移，出现副屏在左/上方时退化为只覆盖主屏
        self.ox = max(0, vx)
        self.oy = max(0, vy)
        self.w = vw if vx >= 0 else vw + vx
        self.h = vh if vy >= 0 else vh + vy

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.25)
        self.top.configure(bg="black")
        self.top.geometry(f"{self.w}x{self.h}+{self.ox}+{self.oy}")

        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.w // 2, 60,
            text="拖动鼠标框选录制区域    ·    Esc 取消",
            fill="#ffd166", font=("Microsoft YaHei UI", 16))
        self.start: Optional[tuple[int, int]] = None
        self.rect_id: Optional[int] = None
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.top.bind("<Escape>", lambda _e: self._cancel())
        self.top.focus_force()
        self.top.grab_set()
        root.wait_window(self.top)

    def _on_press(self, e: tk.Event) -> None:
        self.start = (e.x, e.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=2)

    def _on_drag(self, e: tk.Event) -> None:
        if self.start and self.rect_id:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], e.x, e.y)

    def _on_release(self, e: tk.Event) -> None:
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        # 偶数宽高，避免部分编码器要求 4:2:0 对齐时报错
        w -= w % 2
        h -= h % 2
        if w < 16 or h < 16:
            self._cancel()
            return
        x = max(0, min(x, self.w - w))
        y = max(0, min(y, self.h - h))
        self.result = (x + self.ox, y + self.oy, w, h)
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()


class ScreenRecorderApp:
    def __init__(self, root: tk.Tk, ffmpeg: FFmpegInfo) -> None:
        self.root = root
        self.ffmpeg = ffmpeg
        self.rec: Optional[Recorder] = None
        self.mode = "countdown"  # idle / countdown / recording
        self.region: Optional[tuple[int, int, int, int]] = None
        self._countdown_left = 0
        self._timer_job: Optional[str] = None
        self._normal_geometry: Optional[str] = None
        self.last_output: Optional[str] = None

        self.vx, self.vy, self.vw, self.vh = virtual_screen()
        self.audio_devices: list[str] = []
        self._enc_values = ["libx264（软件编码，兼容最好）"]

        self._build_ui()
        self._refresh_summary()
        # 编码器能力与麦克风枚举较慢，放到后台，先让窗口出现
        self.root.after(120, self._start_probe)

    # -- 界面搭建 ---------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title(f"{APP_TITLE} · ScreenRecorder")
        self.root.configure(bg="#1e1f24")
        self.root.minsize(660, 640)
        win_h = min(740, max(600, int(self.vh * 0.88)))
        self.root.geometry(f"700x{win_h}+{max(0, self.vx + 40)}+{max(0, self.vy + 40)}")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg, panel, fg, accent = "#1e1f24", "#26272e", "#e8e9ee", "#ff3b30"
        style.configure(".", background=bg, foreground=fg,
                        fieldbackground="#31323a", bordercolor="#3a3b44",
                        focuscolor="#4c8dff", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabelframe", background=panel, bordercolor="#3a3b44")
        style.configure("TLabelframe.Label", background=panel, foreground="#9aa0ae",
                        font=("Microsoft YaHei UI", 10))
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Panel.TLabel", background=panel, foreground=fg)
        style.configure("Hint.TLabel", background=panel, foreground="#7d8494",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=bg, foreground="#ffffff",
                        font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Sub.TLabel", background=bg, foreground="#8b92a1",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Time.TLabel", background=bg, foreground="#ffffff",
                        font=("Consolas", 22, "bold"))
        style.configure("TCheckbutton", background=panel, foreground=fg)
        style.configure("TButton", padding=(12, 7))
        style.configure("Accent.TButton", background="#3b3d47", foreground="#ffffff", padding=(14, 10))
        style.map("Accent.TButton", background=[("active", "#4a4c58")])
        style.configure("Record.TButton", background=accent, foreground="#ffffff",
                        font=("Microsoft YaHei UI", 12, "bold"), padding=(18, 12))
        style.map("Record.TButton", background=[("active", "#e0342a"), ("disabled", "#5a3a38")])
        style.configure("Stop.TButton", background="#3a3b44", foreground="#ffffff",
                        font=("Microsoft YaHei UI", 12, "bold"), padding=(18, 12))
        style.map("Stop.TButton", background=[("active", "#4a4c58")])
        style.configure("TCombobox", font=("Microsoft YaHei UI", 10),
                        padding=(6, 5), arrowsize=14,
                        fieldbackground="#31323a", background="#31323a",
                        foreground="#e8e9ee", bordercolor="#3a3b44",
                        lightcolor="#3a3b44", darkcolor="#3a3b44")
        # readonly 状态必须单独映射，否则 ttk 用浅色 field 并裁切中文行高
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#31323a"), ("disabled", "#2a2b32")],
                  foreground=[("readonly", "#e8e9ee"), ("disabled", "#6d7380")],
                  selectbackground=[("readonly", "#31323a")],
                  selectforeground=[("readonly", "#e8e9ee")])
        style.configure("TEntry", padding=(6, 5), fieldbackground="#31323a",
                        foreground="#e8e9ee", insertcolor="#e8e9ee",
                        bordercolor="#3a3b44", lightcolor="#3a3b44", darkcolor="#3a3b44")

        outer = ttk.Frame(self.root, padding=(18, 14, 18, 14))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        self.head_frame = ttk.Frame(outer)
        self.head_frame.grid(row=0, column=0, sticky="ew")
        self.head_frame.columnconfigure(0, weight=1)
        ttk.Label(self.head_frame, text=APP_TITLE, style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(self.head_frame, text=APP_SUBTITLE, style="Sub.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 0))

        # ---- 录制参数 ----
        self.opt_frame = ttk.LabelFrame(outer, text="录制参数", padding=(14, 10))
        self.opt_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.opt_frame.columnconfigure(1, weight=1)
        self.opt_frame.columnconfigure(2, weight=1)

        ttk.Label(self.opt_frame, text="区域", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", pady=4)
        self.area_var = tk.StringVar(value="整个桌面（所有显示器）")
        self.area_box = ttk.Combobox(self.opt_frame, textvariable=self.area_var,
                                     state="readonly", values=[
                                         "整个桌面（所有显示器）", "主显示器", "自定义区域"],
                                     width=30)
        self.area_box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 8))
        self.area_box.bind("<<ComboboxSelected>>", lambda _e: self._on_area_change())
        self.pick_btn = ttk.Button(self.opt_frame, text="框选区域…", command=self._pick_region)
        self.pick_btn.grid(row=0, column=3, sticky="e", padx=(0, 2))

        self.area_hint = ttk.Label(self.opt_frame, text="", style="Hint.TLabel")
        self.area_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Label(self.opt_frame, text="帧率", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", pady=4)
        self.fps_var = tk.StringVar(value="30")
        ttk.Combobox(self.opt_frame, textvariable=self.fps_var, state="readonly",
                     values=["15", "24", "30", "60"], width=6).grid(
            row=2, column=1, sticky="w", padx=(8, 8))

        ttk.Label(self.opt_frame, text="画质", style="Panel.TLabel").grid(
            row=2, column=2, sticky="e", padx=(10, 0))
        self.quality_var = tk.StringVar(value="标准（推荐）")
        ttk.Combobox(self.opt_frame, textvariable=self.quality_var, state="readonly",
                     values=list(QUALITY_PRESETS), width=16).grid(
            row=2, column=3, sticky="ew", padx=(8, 2))

        ttk.Label(self.opt_frame, text="编码", style="Panel.TLabel").grid(
            row=3, column=0, sticky="w", pady=4)
        self.encoder_var = tk.StringVar()
        self.encoder_var.set(self._enc_values[0])
        self.encoder_box = ttk.Combobox(self.opt_frame, textvariable=self.encoder_var,
                                        state="readonly", values=self._enc_values, width=38)
        self.encoder_box.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(8, 2))

        ttk.Label(self.opt_frame, text="麦克风", style="Panel.TLabel").grid(
            row=4, column=0, sticky="w", pady=4)
        self.audio_items = ["不录音（仅画面）"]
        self.audio_var = tk.StringVar(value=self.audio_items[0])
        self.audio_box = ttk.Combobox(self.opt_frame, textvariable=self.audio_var,
                                      state="readonly", values=self.audio_items, width=46)
        self.audio_box.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 2))

        self.mouse_var = tk.BooleanVar(value=True)
        self.open_after_var = tk.BooleanVar(value=False)
        chk = ttk.Frame(self.opt_frame)
        chk.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(chk, text="录制鼠标指针", variable=self.mouse_var).pack(side="left")
        ttk.Checkbutton(chk, text="完成后打开文件夹", variable=self.open_after_var).pack(
            side="left", padx=(18, 0))

        # ---- 保存位置 ----
        self.save_frame = ttk.LabelFrame(outer, text="保存位置", padding=(14, 10))
        self.save_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.save_frame.columnconfigure(0, weight=1)
        self.dir_var = tk.StringVar(value=str(default_output_dir()))
        ttk.Entry(self.save_frame, textvariable=self.dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(self.save_frame, text="浏览…", command=self._browse_dir).grid(
            row=0, column=1, padx=(8, 0))
        self.name_hint = ttk.Label(self.save_frame, text="", style="Hint.TLabel")
        self.name_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ---- 底部控制 ----
        self.ctrl_frame = ttk.Frame(outer)
        self.ctrl_frame.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        self.ctrl_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="就绪")
        self.status_lbl = ttk.Label(self.ctrl_frame, textvariable=self.status_var,
                                    style="Sub.TLabel")
        self.status_lbl.grid(row=0, column=0, sticky="w")
        self.time_lbl = ttk.Label(self.ctrl_frame, text="00:00:00", style="Time.TLabel")
        self.time_lbl.grid(row=1, column=0, sticky="w", pady=(2, 6))

        self.buttons = ttk.Frame(self.ctrl_frame)
        self.buttons.grid(row=2, column=0, sticky="ew")
        self.buttons.columnconfigure(0, weight=1)
        self.buttons.columnconfigure(1, weight=1)
        self.main_btn = ttk.Button(self.buttons, text="开始录制", style="Record.TButton",
                                   command=self._on_main_button)
        self.main_btn.grid(row=0, column=0, sticky="ew")
        self.open_btn = ttk.Button(self.buttons, text="打开输出目录", command=self._open_dir)
        self.open_btn.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self.open_file_btn = ttk.Button(self.buttons, text="播放上次录像",
                                        command=self._open_last, state="disabled")
        self.open_file_btn.grid(row=0, column=2, sticky="ew", padx=(10, 0))

        self.log_var = tk.StringVar(value="")
        self.log_lbl = ttk.Label(self.ctrl_frame, textvariable=self.log_var,
                                 style="Sub.TLabel", wraplength=600)
        self.log_lbl.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        self.root.bind("<Escape>", lambda _e: self._on_escape())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._close_when_done = False

    # -- 交互 -------------------------------------------------------------
    def _start_probe(self) -> None:
        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self) -> None:
        """后台探测麦克风与硬件编码器，避免拖慢首屏。"""
        devices = list_audio_devices(self.ffmpeg.path)
        extras: list[str] = []
        for enc, label in (("h264_qsv", "Intel 硬件加速"),
                           ("h264_nvenc", "NVIDIA 硬件加速"),
                           ("h264_amf", "AMD 硬件加速")):
            if self.ffmpeg.supports(enc) and encoder_works(self.ffmpeg.path, enc):
                extras.append(f"{enc}（{label}）")
        try:
            self.root.after(0, lambda: self._apply_probe(devices, extras))
        except RuntimeError:
            pass

    def _apply_probe(self, devices: list[str], extras: list[str]) -> None:
        self.audio_devices = devices
        self.audio_items = ["不录音（仅画面）"] + devices
        self.audio_box.configure(values=self.audio_items)
        if self.audio_var.get() not in self.audio_items:
            self.audio_var.set(self.audio_items[0])
        if extras:
            self._enc_values.extend(extras)
            self.encoder_box.configure(values=self._enc_values)

    def _on_close(self) -> None:
        """录制中直接关窗会让 ffmpeg 变成孤儿进程，必须优雅收尾。"""
        if self.mode == "recording" and self.rec:
            if not messagebox.askyesno(APP_TITLE, "正在录制中，停止并保存后退出？"):
                return
            self._close_when_done = True
            self._stop_recording()
            return
        if self.mode == "countdown":
            self._cancel_countdown()
        self.root.destroy()
    def _on_area_change(self) -> None:
        self._refresh_summary()

    def _pick_region(self) -> None:
        if self.mode != "idle":
            return
        self.root.withdraw()
        self.root.update_idletasks()
        time.sleep(0.15)
        selector = RegionSelector(self.root)
        self.root.deiconify()
        if selector.result:
            self.region = selector.result
            self.area_var.set("自定义区域")
        self._refresh_summary()

    def _browse_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if d:
            self.dir_var.set(d)
            self._refresh_summary()

    def _current_region(self) -> Optional[tuple[int, int, int, int]]:
        mode = self.area_var.get()
        if mode == "主显示器":
            return (0, 0, self.vw, self.vh)
        if mode == "自定义区域" and self.region:
            return self.region
        return None

    def _refresh_summary(self) -> None:
        mode = self.area_var.get()
        if mode == "整个桌面（所有显示器）":
            text = f"将录制全部显示器：{self.vw}×{self.vh}（虚拟桌面原点 {self.vx},{self.vy}）"
        elif mode == "主显示器":
            text = f"将录制主显示器：{self.vw}×{self.vh}"
        else:
            if self.region:
                x, y, w, h = self.region
                text = f"将录制区域：{w}×{h} @ ({x}, {y})"
            else:
                text = "尚未框选区域，点击「框选区域…」拖动鼠标选择"
        self.area_hint.configure(text=text)
        self.pick_btn.configure(state="normal" if self.mode == "idle" else "disabled")
        out = Path(self.dir_var.get() or ".") / f"录屏_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        self.name_hint.configure(text=f"文件名：{out.name}")

    def _encoder_name(self) -> str:
        return self.encoder_var.get().split("（")[0]

    def _on_main_button(self) -> None:
        if self.mode == "idle":
            self._start_countdown()
        elif self.mode == "countdown":
            self._cancel_countdown()
        else:
            self._stop_recording()

    def _on_escape(self) -> None:
        if self.mode == "recording":
            self._stop_recording()
        elif self.mode == "countdown":
            self._cancel_countdown()

    def _build_config(self) -> Optional[RecordConfig]:
        if self.area_var.get() == "自定义区域" and not self.region:
            messagebox.showinfo(APP_TITLE, "请先点击「框选区域…」选择要录制的区域。")
            return None
        try:
            fps = int(self.fps_var.get())
        except ValueError:
            fps = 30
        crf = QUALITY_PRESETS.get(self.quality_var.get(), 20)
        audio = self.audio_var.get()
        audio = None if audio.startswith("不录音") else audio
        out_dir = Path(self.dir_var.get().strip() or str(default_output_dir()))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"无法创建输出目录：\n{out_dir}\n{exc}")
            return None
        if not os.access(out_dir, os.W_OK):
            messagebox.showerror(APP_TITLE, f"输出目录不可写：\n{out_dir}")
            return None
        output = out_dir / f"录屏_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        return RecordConfig(
            output=str(output), fps=fps, region=self._current_region(),
            audio_device=audio, encoder=self._encoder_name(), crf=crf,
            draw_mouse=self.mouse_var.get(),
        )

    def _start_countdown(self) -> None:
        cfg = self._build_config()
        if not cfg:
            return
        self.pending_cfg = cfg
        self.mode = "countdown"
        self._countdown_left = 3
        self.main_btn.configure(text="取消", style="Stop.TButton")
        self._set_options_enabled(False)
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        if self.mode != "countdown":
            return
        if self._countdown_left <= 0:
            self._begin_recording()
            return
        self.status_var.set(f"{self._countdown_left} 秒后开始录制…")
        self.time_lbl.configure(text=f"00:00:0{self._countdown_left}")
        self._countdown_left -= 1
        self._timer_job = self.root.after(1000, self._tick_countdown)

    def _cancel_countdown(self) -> None:
        if self._timer_job:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None
        self.mode = "idle"
        self.status_var.set("已取消")
        self.time_lbl.configure(text="00:00:00")
        self.main_btn.configure(text="开始录制", style="Record.TButton")
        self._set_options_enabled(True)
        self._refresh_summary()

    def _begin_recording(self) -> None:
        cfg = self.pending_cfg
        self.mode = "recording"
        self.main_btn.configure(text="停止录制  (Esc)", style="Stop.TButton")
        self._enter_compact()
        self.rec = Recorder(self.ffmpeg.path, cfg, on_log=self._on_log)
        try:
            self.rec.start()
        except OSError as exc:
            self._exit_compact()
            self.mode = "idle"
            self.main_btn.configure(text="开始录制", style="Record.TButton")
            self._set_options_enabled(True)
            messagebox.showerror(APP_TITLE, f"启动 FFmpeg 失败：\n{exc}")
            return
        self.last_output = cfg.output
        self.status_var.set("正在录制")
        self.next_tick = time.time() + 0.2
        self._poll()

    def _on_log(self, line: str) -> None:
        # 仅保留最后一条命令，完整日志已在 Recorder 内部缓存
        pass

    def _poll(self) -> None:
        if self.mode != "recording" or not self.rec:
            return
        rc = self.rec.returncode()
        if rc is not None and not self.rec.is_running():
            # 进程已自行退出（例如设备被占用）
            self._finish_recording(auto_stopped=True)
            return
        p = self.rec.progress
        self.time_lbl.configure(text=p.get("time", "00:00:00"))
        extra = []
        if p.get("fps"):
            extra.append(f"{p['fps']} fps")
        if p.get("size"):
            extra.append(p["size"])
        if p.get("speed"):
            extra.append(p["speed"])
        self.status_var.set("正在录制" + ("  ·  " + "  ".join(extra) if extra else ""))
        self._timer_job = self.root.after(400, self._poll)

    def _stop_recording(self) -> None:
        if not self.rec:
            return
        if self._timer_job:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None
        self.status_var.set("正在封装 MP4…")
        self.main_btn.configure(state="disabled")
        self.root.update_idletasks()

        def work() -> None:
            self.rec.stop()
            self.root.after(0, self._finish_recording)

        threading.Thread(target=work, daemon=True).start()

    def _finish_recording(self, auto_stopped: bool = False) -> None:
        rec, self.rec = self.rec, None
        self.mode = "idle"
        self._exit_compact()
        self.main_btn.configure(text="开始录制", style="Record.TButton", state="normal")
        self._set_options_enabled(True)
        self._refresh_summary()

        out = Path(self.last_output) if self.last_output else None
        if self._timer_job:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None

        if out and out.exists() and out.stat().st_size > 0:
            detail = probe_output(self.ffmpeg, str(out))
            self.status_var.set("已保存：" + out.name)
            self.log_var.set(detail or f"文件大小 {out.stat().st_size / 1048576:.2f} MB")
            self.open_file_btn.configure(state="normal")
            self.time_lbl.configure(text="00:00:00")
            if self.open_after_var.get():
                os.startfile(str(out.parent))
            if auto_stopped:
                tail = (rec.error or "")[-600:]
                messagebox.showwarning(
                    APP_TITLE,
                    "录制进程提前结束，已保存现有片段。\n" + (tail if tail else ""))
        else:
            self.status_var.set("录制失败，未生成文件")
            self.log_var.set((rec.error or "")[-600:] if rec else "")
            self.time_lbl.configure(text="00:00:00")
            messagebox.showerror(APP_TITLE, "没有生成有效的视频文件，请检查 FFmpeg 输出与设备占用情况。")

        if self._close_when_done:
            self.root.destroy()

    # -- 紧凑模式 ---------------------------------------------------------
    def _enter_compact(self) -> None:
        self._normal_geometry = self.root.geometry()
        self.head_frame.grid_remove()
        self.opt_frame.grid_remove()
        self.save_frame.grid_remove()
        self.open_btn.grid_remove()
        self.open_file_btn.grid_remove()
        self.log_lbl.grid_remove()
        self.root.attributes("-topmost", True)
        self.root.minsize(1, 1)
        w, h = 400, 185
        x = max(0, self.vx + self.vw - w - 18)
        y = max(0, self.vy + self.vh - h - 66)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.update_idletasks()

    def _exit_compact(self) -> None:
        self.root.attributes("-topmost", False)
        self.root.minsize(660, 640)
        self.head_frame.grid()
        self.opt_frame.grid()
        self.save_frame.grid()
        self.open_btn.grid()
        self.open_file_btn.grid()
        self.log_lbl.grid()
        if self._normal_geometry:
            self.root.geometry(self._normal_geometry)

    def _set_options_enabled(self, enabled: bool) -> None:
        combo_state = "readonly" if enabled else "disabled"
        for widget in self._option_widgets():
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state=combo_state)
                elif isinstance(widget, ttk.Button):
                    widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _option_widgets(self) -> Iterable[tk.Widget]:
        stack = list(self.opt_frame.winfo_children())
        while stack:
            w = stack.pop(0)
            yield w
            stack.extend(w.winfo_children())

    def _open_dir(self) -> None:
        d = Path(self.dir_var.get() or default_output_dir())
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))

    def _open_last(self) -> None:
        if self.last_output and Path(self.last_output).exists():
            os.startfile(self.last_output)


def cmd_gui(args: argparse.Namespace) -> int:
    enable_dpi_awareness()
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少 FFmpeg",
            "未找到可用的 FFmpeg。\n\n"
            "请安装 FFmpeg（需包含 gdigrab 与 libx264），\n"
            "或把 ffmpeg.exe 放到程序目录下，\n"
            "或用 --ffmpeg 指定路径、设置环境变量 SCREEN_RECORDER_FFMPEG。")
        return 2
    root = tk.Tk()
    app = ScreenRecorderApp(root, ffmpeg)
    try:
        root.mainloop()
    finally:
        # 兜底：异常退出时别留下孤儿 ffmpeg 进程和半截文件
        if app.rec and app.rec.is_running():
            app.rec.stop(timeout=10)
    return 0


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="本地录屏工具：采集 Windows 桌面并输出 MP4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python screen_recorder.py --cli --duration 10 -o demo.mp4\n"
               "  python screen_recorder.py --cli --region 100,100,800,600 -o area.mp4",
    )
    ap.add_argument("--cli", action="store_true", help="命令行录制（不打开界面）")
    ap.add_argument("-o", "--output", help="输出 mp4 路径")
    ap.add_argument("--fps", type=int, default=30, choices=[15, 24, 30, 60])
    ap.add_argument("--region", help="录制区域 x,y,宽,高（虚拟桌面坐标）")
    ap.add_argument("--audio", help="麦克风设备名；省略则不录音")
    ap.add_argument("--encoder", default="libx264",
                    choices=["libx264", "h264_qsv", "h264_nvenc", "h264_amf"])
    ap.add_argument("--crf", type=int, default=20, help="画质 18(高)~28(小体积)")
    ap.add_argument("--duration", type=float, help="最长录制秒数；省略则手动停止")
    ap.add_argument("--no-mouse", action="store_true", help="不录制鼠标指针")
    ap.add_argument("--list-devices", action="store_true", help="列出麦克风与支持的编码器")
    ap.add_argument("--list-screens", action="store_true", help="列出显示器几何信息")
    ap.add_argument("--ffmpeg", help="指定 ffmpeg.exe 路径")
    ap.add_argument("--smoke", action="store_true", help="仅构建界面并退出（自检用）")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    # pythonw 启动时 stdout / stderr 为 None，任何 print 都会崩
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    args = build_parser().parse_args(argv)

    if args.list_screens:
        cmd_list_screens()
        return 0

    if args.list_devices:
        enable_dpi_awareness()
        ffmpeg = find_ffmpeg(args.ffmpeg, prefer_qsv=True)
        if not ffmpeg:
            print("未找到可用的 FFmpeg。", file=sys.stderr)
            return 2
        cmd_list_devices(ffmpeg)
        return 0

    if args.smoke:
        enable_dpi_awareness()
        ffmpeg = find_ffmpeg(args.ffmpeg)
        if not ffmpeg:
            print("未找到可用的 FFmpeg。", file=sys.stderr)
            return 2
        root = tk.Tk()
        root.withdraw()
        ScreenRecorderApp(root, ffmpeg)
        root.update_idletasks()
        root.destroy()
        print("GUI 构建成功")
        return 0

    if args.cli or (args.output and not sys.stdout.isatty()):
        return cmd_record(args)

    return cmd_gui(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # pythonw 启动时没有控制台，必须留下可诊断的痕迹
        import traceback
        tb = traceback.format_exc()
        try:
            log_file = app_dir() / "screen_recorder_error.log"
            log_file.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{tb}",
                                encoding="utf-8")
        except OSError:
            pass
        if sys.stderr is not None:
            print(tb, file=sys.stderr)
        try:
            ctypes.windll.user32.MessageBoxW(
                0, tb[-1200:], "本地录屏启动失败", 0x10)
        except Exception:
            pass
        raise SystemExit(3)
