#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网互传 (LAN Transfer)
=========================
在同一局域网内，其他人用手机扫描二维码打开网页，即可向本机发送文字和文件。
本机实时接收，文字记入 history.jsonl，文件保存到 received/files/ 目录。

依赖: qrcode  (仅用于生成二维码，可选)
    pip install qrcode

用法:
    python lan_transfer.py [端口] [--no-browser] [--windowed | --console] [--data-dir 路径]
      --no-browser   不自动打开浏览器
      --windowed     托盘模式（无黑框，托盘图标 + 开机自启开关）
      --console      控制台模式（默认，窗口化打包下自动检测为托盘模式）
      --data-dir D   指定接收数据存放目录（默认：用户目录/LanTransfer/received）

端口: 默认 12666；被占用时在其之上（12667~13666）随机取一个可用端口。
"""

import os
import re
import sys
import json
import time
import gzip
import zlib
import socket
import shutil
import ipaddress
import mimetypes
import random
import threading
import webbrowser
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote
import urllib.request
import urllib.error

# 允许在标准输出里打印中文 / 二维码字符
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import qrcode
    import qrcode.image.svg
    HAS_QR = True
except Exception:
    HAS_QR = False


# ---------- 网页标题抓取（供链接消息展示） ----------
# 代理策略：
#   * 公网地址 → 走系统代理（Windows 读注册表 / 其他读 *_proxy 环境变量），
#     部分站点（尤其境外）必须经代理才能访问；
#   * 本机 / 局域网地址 → 直连，避免被代理拦截，也避免内网地址泄漏给外部代理；
#   * 首选通道失败时，自动换另一种通道重试一次（代理挂了仍可直连，反之亦然）。
# 若系统代理是 PAC 自动配置脚本（urllib 读不到具体地址），可用环境变量
# LAN_TRANSFER_PROXY 显式指定，例如 http://127.0.0.1:7890 。

_LOCAL_HOST_SUFFIX = (".local", ".internal", ".localhost")


def _is_local_host(host):
    """是否为本机 / 内网地址：这类地址不应经过外部代理。"""
    host = (host or "").strip("[]").lower()
    if host in ("", "localhost", "::1", "0.0.0.0") or host.endswith(_LOCAL_HOST_SUFFIX):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def _build_opener(direct):
    """direct=True 强制直连；否则走系统代理或 LAN_TRANSFER_PROXY 指定的代理。"""
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    explicit = (os.environ.get("LAN_TRANSFER_PROXY") or "").strip()
    if explicit:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": explicit, "https": explicit})
        )
    # 不传 ProxyHandler → urllib 自动使用系统代理（并遵循 no_proxy）
    return urllib.request.build_opener()


def _decode_body(data, content_encoding):
    """部分 CDN 会忽略 Accept-Encoding 强制压缩，这里按需解压。"""
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(data)
        except Exception:
            pass
    elif "deflate" in enc:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(data, wbits)
            except Exception:
                continue
    return data


def _parse_title(data):
    """从 HTML 中提取 <title>，兼容 <meta charset> 指定的非 UTF-8 编码。"""
    sample = data[:8192].decode("utf-8", "replace")
    m = re.search(r'charset=["\']?\s*([\w-]+)', sample, re.IGNORECASE)
    enc = m.group(1).lower() if m else "utf-8"
    try:
        text = data.decode(enc)
    except Exception:
        text = data.decode("utf-8", "replace")
    mt = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if not mt:
        return None
    title = re.sub(r"\s+", " ", mt.group(1)).strip()
    return title or None


def _net_error(e, direct):
    tag = "直连" if direct else "代理"
    reason = getattr(e, "reason", e)
    s = str(reason)
    low = s.lower()
    if "timed out" in low or isinstance(reason, socket.timeout):
        return tag + "连接超时"
    if "refused" in low or "拒绝" in s:
        return tag + "连接被拒绝"
    return tag + "连接失败：" + s[:60]


def fetch_web_title_ex(url):
    """抓取网页标题，返回 (title, error)；成功时 error 为 None。"""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return None, "链接格式不正确"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Encoding": "identity",  # 明确不要压缩，避免拿到无法解析的压缩内容
        })
        # 本机/内网先直连，公网先走代理；失败后换另一种通道再试一次
        prefer_direct = _is_local_host(p.hostname or "")
        last_err = "网络请求失败"
        for direct in (prefer_direct, not prefer_direct):
            try:
                with _build_opener(direct).open(req, timeout=5) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if ctype and not any(k in ctype for k in ("html", "xml", "text")):
                        return None, "目标不是网页内容"
                    data = b""
                    while len(data) < 1_000_000:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        data += chunk
                        # 只在读到 </title> 后提前结束；仅遇到 <title> 就断会截断标题
                        if b"</title" in data[-2000:].lower():
                            break
                    data = _decode_body(data, resp.headers.get("Content-Encoding"))
                title = _parse_title(data)
                if title:
                    return title, None
                return None, "页面未包含 <title> 标签"
            except urllib.error.HTTPError as e:
                # 403/404 等状态码换通道通常结果相同，不再重试
                return None, "HTTP %d" % e.code
            except Exception as e:
                last_err = _net_error(e, direct)
                continue
        return None, last_err
    except Exception:
        return None, "解析失败"


def fetch_web_title(url):
    """兼容旧调用：只返回标题（异步发送、/api/title 使用）。"""
    return fetch_web_title_ex(url)[0]


HOST = "0.0.0.0"
PORT = 12666

# 兼容 PyInstaller 打包：冻结后，只读资源（index.html）放解包临时目录 _MEIPASS
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    DATA_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = APP_DIR

# 接收数据默认存放在「用户主目录 / LanTransfer」下，与 exe 位置解耦：
# 这样换机器 / 重装 exe 不会丢数据，也不会在 exe 旁边留下一堆文件。
# 可通过 --data-dir 参数覆盖（见 main()），便于开发/测试时用独立目录。
RECEIVED_DIR = os.path.join(os.path.expanduser("~"), "LanTransfer", "received")
FILES_DIR = os.path.join(RECEIVED_DIR, "files")
HISTORY_FILE = os.path.join(RECEIVED_DIR, "history.jsonl")
INDEX_FILE = os.path.join(DATA_DIR, "index.html")

# ---- 接收记录（内存镜像 + jsonl 持久化）----
items = []          # list[dict]，按接收先后排序
lock = threading.Lock()
id_counter = [0]
REV = 0             # 数据版本号：任何变更（含标题异步回填）都 +1，供前端增量刷新

# 收到新内容时的通知回调，由 main/run_windowed 注入（用于托盘气泡 / 提示音）
NOTIFIER = None


def _describe_item(it):
    if it.get("type") == "text":
        text = it.get("text", "")
        return "收到文字: %s" % (text[:40] + ("…" if len(text) > 40 else ""))
    size = it.get("size") or 0
    if size < 1024:
        s = "%dB" % size
    elif size < 1024 * 1024:
        s = "%.1fKB" % (size / 1024)
    else:
        s = "%.1fMB" % (size / 1024 / 1024)
    return "收到文件: %s (%s)" % (it.get("name", "文件"), s)


def _play_sound():
    if sys.platform.startswith("win"):
        try:
            import winsound
            winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            pass


def _notify(it):
    fn = NOTIFIER
    if fn is not None:
        try:
            fn(it)
        except Exception:
            pass


def ensure_dirs():
    os.makedirs(FILES_DIR, exist_ok=True)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                    items.append(it)
                    id_counter[0] = max(id_counter[0], int(it.get("id", 0)))
                except Exception:
                    pass
    except Exception:
        pass


def _rewrite_history():
    """把内存中的全部记录原子地写回 history.jsonl（用于标题回填等更新场景）。"""
    try:
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def record(it):
    global REV
    with lock:
        items.append(it)
        REV += 1
        _rewrite_history()
    _notify(it)


def fetch_title_and_update(it, text):
    """后台线程：收到单一链接后异步抓取网页标题并回填记录，再通知前端刷新。"""
    global REV
    title = fetch_web_title(text)
    if not title:
        return
    with lock:
        it["url_title"] = title
        REV += 1
        _rewrite_history()


def safe_filename(name):
    name = unquote(name).replace("\\", "/").split("/")[-1]
    name = name.strip()
    # 去掉 Windows 非法字符与控制字符，保留中文 / emoji
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "file"
    if len(name) > 120:
        name = name[:120]
    return name


def get_lan_ip():
    """尽量拿到本机在局域网中的 IPv4 地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # 回退：枚举网卡
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def make_qr_svg(url):
    if not HAS_QR:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240">'
                '<rect width="100%" height="100%" fill="#fff"/>'
                '<text x="50%" y="50%" text-anchor="middle" font-size="16" fill="#333">二维码不可用</text>'
                '</svg>')
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    s = img.to_string()
    if isinstance(s, bytes):
        s = s.decode("utf-8")
    return s


def print_qr(url):
    if not HAS_QR:
        print("  (未安装 qrcode，无法打印二维码，请先: pip install qrcode)")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        qr.print_ascii(tty=tty, invert=False)
    except Exception:
        # 兜底：任何异常都不影响服务启动，网页里的二维码仍可用
        print("  (终端无法绘制二维码，请查看自动打开的网页二维码)")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "LanTransfer/1.0"

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            html = self._read_index()
            if html is None:
                self._send(404, "index.html 缺失", "text/plain; charset=utf-8")
            else:
                self._send(200, html, "text/html; charset=utf-8")
        elif path == "/qr.svg":
            self._send(200, make_qr_svg(self.server.lan_url), "image/svg+xml")
        elif path == "/api/items":
            with lock:
                data = json.dumps({"items": list(items), "rev": REV}, ensure_ascii=False)
            self._send(200, data)
        elif path.startswith("/files/"):
            self._serve_file(path[len("/files/"):])
        elif path == "/api/title":
            qs = parse_qs(parsed.query)
            u = (qs.get("url") or [""])[0]
            title = fetch_web_title(u) if u else None
            self._json(200, {"url": u, "title": title})
        else:
            self._json(404, {"error": "not found"})

    def _read_index(self):
        if not os.path.exists(INDEX_FILE):
            return None
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return f.read()

    def _serve_file(self, rel):
        name = os.path.basename(unquote(rel).replace("\\", "/"))
        full = os.path.join(FILES_DIR, name)
        if not os.path.isfile(full):
            self._json(404, {"error": "file not found"})
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(full)
        except Exception:
            size = 0
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % quote(name))
        self.end_headers()
        with open(full, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    # ---------- POST ----------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/send":
            self._handle_text()
        elif path == "/api/upload":
            self._handle_upload(parsed)
        elif path == "/api/clear":
            self._handle_clear()
        elif path == "/api/open_folder":
            self._handle_open_folder()
        elif path == "/api/resolve_title":
            self._handle_resolve_title()
        else:
            self._json(404, {"error": "not found"})

    def _handle_text(self):
        try:
            obj = json.loads(self._read_body().decode("utf-8"))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        text = (obj.get("text") or "").strip()
        if not text:
            self._json(400, {"error": "内容为空"})
            return
        # 先落库并立即响应（url_title 暂为空），标题抓取放到后台线程处理，
        # 抓到后再回填记录，接收端轮询会自动同步展示。
        is_single_url = bool(re.match(r"^https?://\S+$", text, re.IGNORECASE))
        with lock:
            id_counter[0] += 1
            it = {
                "id": id_counter[0],
                "type": "text",
                "name": "文字消息",
                "size": len(text.encode("utf-8")),
                "text": text,
                "url_title": None,
                "time": int(time.time() * 1000),
                "from": self.client_address[0],
            }
        record(it)
        if is_single_url:
            t = threading.Thread(target=fetch_title_and_update, args=(it, text), daemon=True)
            t.start()
        self._json(200, {"ok": True, "id": it["id"]})

    def _handle_upload(self, parsed):
        qs = parse_qs(parsed.query)
        filename = safe_filename(qs.get("filename", ["file"])[0])
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json(400, {"error": "empty file"})
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        stored = "%s_%s" % (ts, filename)
        full = os.path.join(FILES_DIR, stored)
        try:
            remaining = length
            with open(full, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:
            self._json(500, {"error": "保存失败: %s" % e})
            return
        with lock:
            id_counter[0] += 1
            it = {
                "id": id_counter[0],
                "type": "file",
                "name": filename,
                "stored": stored,
                "size": length,
                "time": int(time.time() * 1000),
                "from": self.client_address[0],
            }
        record(it)
        self._json(200, {"ok": True, "id": it["id"], "stored": stored})

    def _handle_clear(self):
        global REV
        with lock:
            items.clear()
            id_counter[0] = 0
            REV += 1
            _rewrite_history()
        self._json(200, {"ok": True})

    def _handle_resolve_title(self):
        """手动为某条链接消息解析网页标题并持久化（用于发送时异步抓取失败的情况）。"""
        global REV
        try:
            obj = json.loads(self._read_body().decode("utf-8"))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        item_id = obj.get("id")
        if item_id is None:
            self._json(400, {"error": "缺少 id"})
            return
        # 在锁内取文本并校验是否为单一链接；抓取在锁外进行，避免阻塞其他请求
        with lock:
            it = next((x for x in items if x.get("id") == item_id), None)
            if it is None:
                self._json(404, {"error": "记录不存在"})
                return
            if it.get("type") != "text":
                self._json(400, {"error": "非链接消息"})
                return
            text = it.get("text", "")
            if not re.match(r"^https?://\S+$", text, re.IGNORECASE):
                self._json(400, {"error": "非链接消息"})
                return
        title, err = fetch_web_title_ex(text)
        # 只有成功才落库；失败保留按钮供重试，并把原因回传给前端提示
        if title:
            with lock:
                for x in items:
                    if x.get("id") == item_id:
                        x["url_title"] = title
                        REV += 1
                        _rewrite_history()
                        break
        resp = {"ok": True, "id": item_id, "title": title}
        if not title and err:
            resp["error"] = err
        self._json(200, resp)

    def _handle_open_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(FILES_DIR)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", FILES_DIR])
            else:
                subprocess.Popen(["xdg-open", FILES_DIR])
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(500, {"error": str(e)})


def parse_args(argv):
    port = PORT
    open_browser = True
    windowed = None  # None = 自动检测
    data_dir = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-browser":
            open_browser = False
        elif a in ("--windowed", "--tray"):
            windowed = True
        elif a == "--console":
            windowed = False
        elif a in ("--data-dir", "-d"):
            if i + 1 < len(argv):
                data_dir = argv[i + 1]
                i += 1
            else:
                print("[警告] --data-dir 需要跟一个目录路径，已忽略。")
        else:
            rest.append(a)
        i += 1
    if rest:
        try:
            port = int(rest[0])
        except Exception:
            pass
    if windowed is None:
        # 自动检测：无可用控制台（窗口化打包）时默认托盘模式
        windowed = (sys.stdout is None or not hasattr(sys.stdout, "reconfigure"))
    return port, open_browser, windowed, data_dir


def _redirect_stdio():
    """窗口化打包时 stdout/stderr 为 None 或 NullWriter，统一重定向到日志文件。"""
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        return
    log_path = os.path.join(APP_DIR, "lan_transfer.log")
    try:
        f = open(log_path, "a", encoding="utf-8")
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def _open_folder_dir():
    try:
        if sys.platform.startswith("win"):
            os.startfile(FILES_DIR)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", FILES_DIR])
        else:
            subprocess.Popen(["xdg-open", FILES_DIR])
    except Exception:
        pass


def _clipboard_win(text):
    """Windows 剪贴板写入，成功返回 True。"""
    import ctypes
    from ctypes import wintypes
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            return False
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        user32.CloseClipboard()
        return True
    except Exception:
        return False


def _set_clipboard(text):
    """把文本写入系统剪贴板，带 clip 命令兜底。"""
    if sys.platform.startswith("win"):
        if _clipboard_win(text):
            return
        try:
            subprocess.run(["clip"], input=text.encode("utf-8"), check=False)
        except Exception:
            pass
    else:
        try:
            subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE).communicate(text.encode("utf-8"))
        except Exception:
            pass


def make_icon_image(size=64):
    """生成一个简单的「上/下箭头」托盘图标（无需外部图片文件）。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=(79, 110, 247, 255))
    cx = size // 2
    white = (255, 255, 255, 255)
    # 上箭头（发送）
    d.polygon([(cx, 10), (cx - 10, 24), (cx - 4, 24), (cx - 4, 38), (cx + 4, 38), (cx + 4, 24), (cx + 10, 24)], fill=white)
    # 下箭头（接收）
    d.polygon([(cx, size - 10), (cx - 10, size - 24), (cx - 4, size - 24), (cx - 4, size - 38), (cx + 4, size - 38), (cx + 4, size - 24), (cx + 10, size - 24)], fill=white)
    return img


# ---- 开机自启（Windows 注册表 Run 键）----
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "LanTransfer"


def _autostart_command():
    if FROZEN:
        return '"%s" --no-browser' % sys.executable
    return '"%s" "%s" --windowed --no-browser' % (sys.executable, os.path.abspath(__file__))


def _autostart_enabled():
    if not sys.platform.startswith("win"):
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _set_autostart(enable):
    if not sys.platform.startswith("win"):
        return
    import winreg
    try:
        if enable:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def run_windowed(httpd, lan_url, open_browser, port_changed=False, requested_port=0, port=0):
    """托盘模式：服务在后台线程运行，托盘图标提供操作菜单。"""
    global NOTIFIER
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:
        print("托盘组件不可用，退回前台运行: %s" % e)
        NOTIFIER = lambda it: _play_sound()
        if open_browser:
            webbrowser.open(lan_url + "/?view=receive")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    icon_holder = {}

    def open_page(icon, item):
        webbrowser.open(lan_url + "/?view=receive")

    def open_folder(icon, item):
        _open_folder_dir()

    def copy_link(icon, item):
        _set_clipboard(lan_url)

    def toggle_autostart(icon, item):
        _set_autostart(not _autostart_enabled())

    def quit_app(icon, item):
        try:
            httpd.shutdown()
        except Exception:
            pass
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开接收页面", open_page, default=True),
        pystray.MenuItem("复制链接", copy_link),
        pystray.MenuItem("打开接收文件夹", open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda item: _autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("LanTransfer", make_icon_image(), "局域网互传 - %s" % lan_url, menu)
    icon_holder["icon"] = icon

    # 收到新内容 → 托盘气泡 + 提示音
    def on_receive(it):
        ic = icon_holder.get("icon")
        if ic is not None:
            try:
                ic.notify("局域网互传", _describe_item(it))
            except Exception:
                pass
        _play_sound()

    NOTIFIER = on_receive

    def setup(icon):
        icon.visible = True
        if open_browser:
            webbrowser.open(lan_url + "/?view=receive")
        try:
            msg = "扫码地址: %s" % lan_url
            if port_changed:
                msg = "端口 %d 被占用，已改用 %d\n%s" % (requested_port, port, msg)
            icon.notify("局域网互传已启动", msg)
        except Exception:
            pass

    try:
        icon.run(setup=setup)
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


MAX_PORT = 65535
PORT_SPAN = 1000   # 默认端口被占用时，在其之上随机探测的范围
PORT_TRIES = 80    # 随机探测次数，未命中再顺序兜底


def _port_in_use(p):
    """探测端口是否已被占用。

    Windows 下 SO_REUSEADDR 允许多个 socket 绑定同一端口（Linux 不允许），
    因此不能只靠 Server() 是否抛异常来判断，先用普通 socket 探一次，
    避免两个本程序实例静默绑到同一端口。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((HOST, p))
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _try_bind(p):
    """尝试绑定单个端口，成功返回 Server 对象，被占用返回 None。"""
    if _port_in_use(p):
        return None
    try:
        return Server((HOST, p), Handler)
    except OSError:
        return None


def bind_server(start_port, span=PORT_SPAN, tries=PORT_TRIES):
    """绑定端口：优先 start_port，被占用时在其之上随机取一个可用端口。

    返回 (httpd, 实际端口)；范围内全部被占用则返回 (None, start_port)。
    """
    httpd = _try_bind(start_port)
    if httpd is not None:
        return httpd, start_port

    lo, hi = start_port + 1, min(MAX_PORT, start_port + span)
    if lo > hi:
        lo, hi = start_port + 1, MAX_PORT
    # 先随机试若干次：端口不可预测，绝大多数情况一次即中
    for _ in range(tries):
        p = random.randint(lo, hi)
        httpd = _try_bind(p)
        if httpd is not None:
            return httpd, p
    # 极端情况（随机范围几乎全被占用）：顺序扫描兜底
    for p in range(lo, hi + 1):
        httpd = _try_bind(p)
        if httpd is not None:
            return httpd, p
    return None, start_port


def main():
    global RECEIVED_DIR, FILES_DIR, HISTORY_FILE
    port, open_browser, windowed, data_dir = parse_args(sys.argv[1:])
    if data_dir:
        RECEIVED_DIR = os.path.abspath(os.path.expanduser(data_dir))
        FILES_DIR = os.path.join(RECEIVED_DIR, "files")
        HISTORY_FILE = os.path.join(RECEIVED_DIR, "history.jsonl")
    if windowed:
        _redirect_stdio()
    ensure_dirs()
    load_history()

    # 优先用指定/默认端口，被占用则在其之上随机取一个可用端口
    requested_port = port
    httpd, port = bind_server(port)
    if httpd is None:
        print("[错误] 端口 %d 及 %d~%d 范围均被占用，请手动指定端口。"
              % (requested_port, requested_port + 1, min(MAX_PORT, requested_port + PORT_SPAN)))
        sys.exit(1)
    port_changed = (port != requested_port)

    ip = get_lan_ip()
    lan_url = "http://%s:%d" % (ip, port)
    httpd.lan_url = lan_url

    if windowed:
        run_windowed(httpd, lan_url, open_browser, port_changed, requested_port, port)
        return

    print("=" * 56)
    print("  局域网互传已启动")
    print("=" * 56)
    if port_changed:
        print("  [提示] 端口 %d 被占用，已随机改用端口 %d" % (requested_port, port))
    print("  扫码地址 : %s" % lan_url)
    print("  本机访问 : http://127.0.0.1:%d" % port)
    print("  接收目录 : %s" % FILES_DIR)
    if data_dir:
        print("  [说明] 接收数据使用了 --data-dir 指定的目录")
    print("-" * 56)
    print_qr(lan_url)
    print("-" * 56)
    print("  用手机/其他设备扫描上方二维码即可发送文字和文件。")
    print("  按 Ctrl+C 停止服务。")
    print("=" * 56)

    if open_browser:
        try:
            webbrowser.open(lan_url + "/?view=receive")
        except Exception:
            pass

    # 控制台模式：收到新内容时响一声提示
    global NOTIFIER
    NOTIFIER = lambda it: _play_sound()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
