"""启动全自动化剪辑 Skill：开启本地服务并自动打开浏览器。"""
import os
import socket
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))


def free_port(start=8765):
    for port in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本，当前是", sys.version.split()[0])
        print("请到 https://www.python.org/downloads/ 安装 Python 3.12")
        input("按回车键退出")
        return
    import uvicorn
    from server import app
    cloud = os.environ.get("KOUBO_CLOUD") == "1"
    host = os.environ.get("KOUBO_HOST", "127.0.0.1")
    port = int(os.environ.get("KOUBO_PORT", "0")) or free_port()
    url = f"http://127.0.0.1:{port}"
    if cloud:
        print(f"[在线版] 监听 {host}:{port}，数据目录：{os.environ.get('KOUBO_DATA') or os.path.join(ROOT, 'data')}")
        uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*",
                    timeout_keep_alive=75)
        return
    print("\n" + "=" * 52)
    brand = "全自动化剪辑 Skill @灵鋆智能"
    try:
        import json
        with open(os.path.join(ROOT, "edition.json"), encoding="utf-8") as f:
            e = json.load(f)
        brand = (e.get("brand", brand) + " " + e.get("by", "")).strip()
    except Exception:  # noqa: BLE001
        pass
    print("  " + brand + " 已启动")
    print(f"  浏览器打开：{url}")
    print("  使用期间请不要关闭这个窗口")
    print("=" * 52 + "\n")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
