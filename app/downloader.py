"""首次运行时下载语音识别模型（约 230MB，只需一次）。依次尝试多个国内可用的镜像。"""
import os
import shutil
import tarfile
import threading
import urllib.request

from engine import MODEL_DIR, model_paths

SV_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
GH_PROXIES = ["", "https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/"]
HF_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

state = {"running": False, "progress": 0.0, "message": "", "error": ""}
_lock = threading.Lock()


def _fetch(url, dst, label, lo, hi):
    tmp = dst + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 koubo-clip"})
    with urllib.request.urlopen(req, timeout=20) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            buf = r.read(1 << 16)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if total:
                state["progress"] = lo + (hi - lo) * got / total
                state["message"] = f"{label} {got / 1e6:.0f}/{total / 1e6:.0f} MB"
    os.replace(tmp, dst)


def _try(urls, dst, label, lo, hi, min_size):
    last = None
    for u in urls:
        try:
            state["message"] = f"{label}：连接 {u.split('/')[2]} …"
            _fetch(u, dst, label, lo, hi)
            if os.path.getsize(dst) >= min_size:
                return True
            last = "文件不完整"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        for p in (dst, dst + ".part"):
            if os.path.exists(p):
                os.remove(p)
    raise RuntimeError(f"{label}下载失败（{last}）")


def _download():
    p = model_paths()
    sv_dir = os.path.dirname(p["asr"])
    os.makedirs(sv_dir, exist_ok=True)
    if not os.path.exists(p["vad"]):
        _try([f"{x}{GH}/asr-models/silero_vad.onnx" for x in GH_PROXIES]
             + ["https://hf-mirror.com/csukuangfj/vad/resolve/main/silero_vad.onnx"],
             p["vad"], "静音检测模型", 0.0, 0.03, 300_000)
    if not os.path.exists(p["tokens"]) or not os.path.exists(p["asr"]):
        try:  # 方式一：hf-mirror 直接下载两个文件
            _try([f"https://hf-mirror.com/{HF_REPO}/resolve/main/tokens.txt"], p["tokens"], "词表", 0.03, 0.05, 100_000)
            _try([f"https://hf-mirror.com/{HF_REPO}/resolve/main/model.int8.onnx"], p["asr"], "语音识别模型", 0.05, 1.0,
                 200_000_000)
        except RuntimeError:  # 方式二：GitHub 压缩包（含多个加速镜像）
            tar = os.path.join(MODEL_DIR, SV_NAME + ".tar.bz2")
            _try([f"{x}{GH}/asr-models/{SV_NAME}.tar.bz2" for x in GH_PROXIES], tar, "语音识别模型", 0.05, 0.95,
                 150_000_000)
            state["message"] = "正在解压…"
            with tarfile.open(tar, "r:bz2") as tf:
                for m in tf.getmembers():
                    name = os.path.basename(m.name)
                    if name in ("model.int8.onnx", "tokens.txt"):
                        final = os.path.join(sv_dir, name)
                        with tf.extractfile(m) as src, open(final + ".part", "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.replace(final + ".part", final)
            os.remove(tar)


def start():
    with _lock:
        if state["running"]:
            return
        state.update(running=True, progress=0.0, message="准备下载…", error="")

    def job():
        try:
            _download()
            state["message"] = "模型已就绪"
            state["progress"] = 1.0
        except Exception as e:  # noqa: BLE001
            state["error"] = (f"{e}。可以手动下载模型放进 models 文件夹，见《使用说明》。")
        finally:
            state["running"] = False
    threading.Thread(target=job, daemon=True).start()
