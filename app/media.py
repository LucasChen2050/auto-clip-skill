"""ffmpeg 相关的底层工具：探测、抽音频、抽帧、生成预览代理。"""
import os
import re
import subprocess
import sys

import numpy as np

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # 兜底：使用系统 ffmpeg
    FFMPEG = "ffmpeg"

_NOWIN = {}
if sys.platform == "win32":
    _NOWIN = {"creationflags": 0x08000000}  # CREATE_NO_WINDOW


def run(args, **kw):
    return subprocess.run([FFMPEG, "-hide_banner", "-nostdin", *args],
                          capture_output=True, **_NOWIN, **kw)


def run_progress(args, total_sec, cb, cwd=None):
    """运行 ffmpeg 并通过 cb(0..1) 回报进度。"""
    p = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats", *args],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="ignore", cwd=cwd, **_NOWIN)
    err_lines = []

    def _drain():
        for line in p.stderr:
            err_lines.append(line)
            if len(err_lines) > 200:
                del err_lines[:100]
    import threading
    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=")[1])
                if total_sec > 0:
                    cb(min(0.999, us / 1e6 / total_sec))
            except ValueError:
                pass
    p.wait()
    th.join(timeout=2)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 出错：\n" + "".join(err_lines[-15:]))


def probe(path):
    """返回 {duration, width, height, fps, has_audio}，宽高为旋转后的显示尺寸。"""
    r = run(["-i", path])
    s = r.stderr.decode("utf-8", "ignore")
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 30.0, "has_audio": " Audio:" in s}
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", s)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", s)
    if m:
        info["width"], info["height"] = int(m[1]), int(m[2])
    m = re.search(r"(\d+(?:\.\d+)?) fps", s)
    if m:
        info["fps"] = float(m[1])
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", s) or re.search(r"rotate\s*:\s*(-?\d+)", s)
    if rot and abs(round(float(rot[1]))) % 180 == 90:
        info["width"], info["height"] = info["height"], info["width"]
    if " Video:" not in s:
        raise RuntimeError("没有识别到视频画面，请确认导入的是视频文件。")
    return info


def load_audio_16k(path):
    r = run(["-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("无法读取音频：" + r.stderr.decode("utf-8", "ignore")[-400:])
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def grab_frame(path, t, out_path, width=None):
    vf = ["-vf", f"scale={width}:-2"] if width else []
    r = run(["-y", "-ss", f"{max(0, t):.3f}", "-i", path, "-frames:v", "1", *vf, "-q:v", "2", out_path])
    if r.returncode != 0 or not os.path.exists(out_path):
        # 某些文件在末尾抽不到帧，退回到开头
        run(["-y", "-i", path, "-frames:v", "1", *vf, "-q:v", "2", out_path])
    return out_path


def make_proxy(src, dst, total, cb):
    """生成 540p 预览视频，供浏览器流畅播放（兼容 HEVC 等格式）。"""
    run_progress(["-y", "-i", src, "-vf", "scale='if(gt(iw,ih),960,540)':-2,fps=30",
                  "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst], total, cb)


def measure_shake(path, duration, max_sec=60, fps=10, width=320):
    """判断镜头是否在晃：只看画面边缘（上方和左右两侧的背景），避开中间的人，用相位相关估计相邻帧的整体平移。

    返回 {"jitter": 抖动幅度（换算到 1080 宽的像素）, "drift": 慢速漂移, "level": "fixed"/"light"/"shaky"}。
    固定机位（手机支架、三脚架）通常 jitter < 1.2。"""
    import numpy as np
    t = min(duration, max_sec)
    start = max(0.0, (duration - t) / 2) if duration > max_sec else 0.0
    proc = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-ss", f"{start:.2f}", "-t", f"{t:.2f}", "-i", path,
                           "-vf", f"fps={fps},scale={width}:-2,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, **_NOWIN)
    raw = proc.stdout
    info = probe(path)
    h = int(round(width * info["height"] / info["width"] / 2)) * 2
    n = len(raw) // (width * h)
    if n < 6:
        return {"jitter": 0.0, "drift": 0.0, "level": "fixed"}
    frames = np.frombuffer(raw[:n * width * h], np.uint8).reshape(n, h, width).astype(np.float32)
    # 背景区域：上方 22%、左右各 18%（中间偏下通常是人）
    regions = [(slice(0, int(h * 0.22)), slice(0, width)),
               (slice(0, int(h * 0.75)), slice(0, int(width * 0.18))),
               (slice(0, int(h * 0.75)), slice(int(width * 0.82), width))]

    def shift(a, b):
        win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
        A = np.fft.fft2((a - a.mean()) * win)
        B = np.fft.fft2((b - b.mean()) * win)
        R = A * np.conj(B)
        R /= np.abs(R) + 1e-6
        c = np.fft.ifft2(R).real
        iy, ix = np.unravel_index(np.argmax(c), c.shape)
        peak = c[iy, ix]

        def sub(cm, c0, cp):
            d = cm - 2 * c0 + cp
            return 0.0 if abs(d) < 1e-9 else 0.5 * (cm - cp) / d
        dy = iy + sub(c[(iy - 1) % c.shape[0], ix], peak, c[(iy + 1) % c.shape[0], ix])
        dx = ix + sub(c[iy, (ix - 1) % c.shape[1]], peak, c[iy, (ix + 1) % c.shape[1]])
        if dy > c.shape[0] / 2:
            dy -= c.shape[0]
        if dx > c.shape[1] / 2:
            dx -= c.shape[1]
        return dx, dy, peak

    steps = []
    for i in range(1, n):
        cand = []
        for ry, rx in regions:
            a, b = frames[i - 1][ry, rx], frames[i][ry, rx]
            if a.std() < 4:  # 纯色墙面，没有纹理，测不准
                continue
            dx, dy, pk = shift(a, b)
            cand.append((pk, dx, dy))
        if cand:
            cand.sort(reverse=True)
            steps.append(np.median([[c[1], c[2]] for c in cand[:2]], axis=0))
        else:
            steps.append(np.zeros(2))
    st = np.array(steps)
    path_ = np.cumsum(st, axis=0)
    k = max(3, fps)  # 1 秒滑动平均：高频部分是抖，低频部分是有意的运镜
    pad = np.pad(path_, ((k, k), (0, 0)), mode="edge")
    smooth = np.array([pad[i:i + 2 * k + 1].mean(axis=0) for i in range(len(path_))])
    scale = 1080 / width
    jitter = float(np.sqrt(((path_ - smooth) ** 2).sum(axis=1)).mean() * scale)
    drift = float(np.abs(smooth[-1] - smooth[0]).max() * scale)
    level = "fixed" if jitter < 1.2 else ("light" if jitter < 3.5 else "shaky")
    return {"jitter": round(jitter, 2), "drift": round(drift, 1), "level": level}
