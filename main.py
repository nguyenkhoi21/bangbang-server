#!/usr/bin/env python3
"""BangBang M72 cloud bootstrapper for Altare / GitHub deploys."""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("BB_RELEASE_REPO", "nguyenkhoi21/bangbang-server")
TAG = os.environ.get("BB_RELEASE_TAG", "v1.0")
ROOT = Path(__file__).resolve().parent
SERVER_DIR = ROOT / "SERVER_BB_M72"
APP_PY = SERVER_DIR / "server" / "app.py"
BRIDGE_PY = SERVER_DIR / "server" / "html5_bridge.py"
HTML5_DIR = SERVER_DIR / "www" / "html5"


class NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization and Accept headers before following CDN redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            for hdr_dict in (new_req.headers, new_req.unredirected_hdrs):
                for key in list(hdr_dict.keys()):
                    if key.lower() in ("authorization", "accept"):
                        del hdr_dict[key]
        return new_req


_OPENER = urllib.request.build_opener(NoAuthRedirectHandler())


def _github_headers(token: str, accept: str) -> dict[str, str]:
    headers = {
        "User-Agent": "bangbang-launcher",
        "Accept": accept,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _download_file(url: str, dest: Path, headers: dict[str, str], label: str) -> None:
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        next_report = 100 * 1024 * 1024
        with dest.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    mb = done / (1024 * 1024)
                    if total:
                        pct = done * 100 / total
                        print(f"[setup] Dang tai {label}: {mb:.0f} MB ({pct:.0f}%)", flush=True)
                    else:
                        print(f"[setup] Dang tai {label}: {mb:.0f} MB", flush=True)
                    next_report += 100 * 1024 * 1024
    print(f"[setup] Tai xong {label} ({dest.stat().st_size / (1024 * 1024):.1f} MB)", flush=True)


def _bootstrap_game_files() -> None:
    if APP_PY.is_file():
        return

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    release_url = f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"
    print(f"[setup] Lan chay dau: dang lay danh sach file game tu {REPO} ({TAG})...", flush=True)

    try:
        req = urllib.request.Request(release_url, headers=_github_headers(token, "application/vnd.github+json"))
        with _OPENER.open(req, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            print(
                f"[loi] Khong truy cap duoc GitHub Release ({exc.code}). "
                "Neu repo dang de Private, hay them bien moi truong GH_TOKEN (Personal Access Token) "
                "trong muc Environment variables tren Altare, hoac chuyen repo nguyenkhoi21/bangbang-server sang Public.",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)
        raise

    assets = sorted(
        (a for a in release.get("assets", []) if a.get("name", "").startswith("bb_part") and a.get("name", "").endswith(".tar.gz")),
        key=lambda a: a["name"],
    )
    if not assets:
        print("[loi] Khong tim thay file bb_part*.tar.gz trong GitHub Release.", file=sys.stderr, flush=True)
        sys.exit(1)

    for asset in assets:
        name = asset["name"]
        archive_path = ROOT / name
        try:
            _download_file(
                asset["url"],
                archive_path,
                _github_headers(token, "application/octet-stream"),
                name,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                print(
                    f"[loi] Khong tai duoc {name} (HTTP {exc.code}). "
                    "Hay them GH_TOKEN vao Environment variables tren Altare hoac chuyen repo sang Public.",
                    file=sys.stderr,
                    flush=True,
                )
                sys.exit(1)
            raise

        print(f"[setup] Dang giai nen {name}...", flush=True)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=ROOT)
        archive_path.unlink(missing_ok=True)
        print(f"[setup] Da giai nen va don {name}", flush=True)

    if not APP_PY.is_file():
        print("[loi] Giai nen xong nhung khong thay SERVER_BB_M72/server/app.py", file=sys.stderr, flush=True)
        sys.exit(1)


def _patch_html5_bridge_for_proxy() -> None:
    if not BRIDGE_PY.is_file():
        return
    txt = BRIDGE_PY.read_text(encoding="utf-8")
    changed = False

    if 'os.environ.get("BB_PUBLIC_WS") == "1"' not in txt:
        new_fn = '''def _same_origin(handler):
    import os
    origin = handler.headers.get("Origin")
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        if parsed.username is not None or parsed.path.rstrip("/") or parsed.query or parsed.fragment:
            return False
        if os.environ.get("BB_PUBLIC_WS") == "1":
            fetch_site = (handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
            return fetch_site in ("", "same-origin", "none")
        host = handler.headers.get("Host", "").strip()
        if not host:
            return False
        forwarded_host = (handler.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        forwarded_proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        is_tls = isinstance(handler.connection, ssl.SSLSocket)
        scheme = "https" if (is_tls or forwarded_proto in ("https", "wss")) else "http"
        netloc = parsed.netloc.lower()
        return (parsed.scheme == scheme and netloc == host.lower()) or (
            bool(forwarded_host) and parsed.scheme == scheme and netloc == forwarded_host.lower()
        )
    except ValueError:
        return False


'''
        pattern = re.compile(r"def _same_origin\(handler\):.*?(?=def _requested_role\(path\):)", re.DOTALL)
        if pattern.search(txt):
            txt = pattern.sub(new_fn, txt, count=1)
            changed = True

    nodelay_anchor = "    handler.connection.settimeout(CLIENT_IDLE_TIMEOUT)"
    nodelay_code = (
        "    handler.connection.settimeout(CLIENT_IDLE_TIMEOUT)\n"
        "    try:\n"
        "        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)\n"
        "    except OSError:\n"
        "        pass"
    )
    if "handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)" not in txt and nodelay_anchor in txt:
        txt = txt.replace(nodelay_anchor, nodelay_code, 1)
        changed = True

    if changed:
        BRIDGE_PY.write_text(txt, encoding="utf-8")
        print("[patch] Da cap nhat WebSocket proxy + TCP_NODELAY", flush=True)


def _patch_app_performance() -> None:
    if not APP_PY.is_file():
        return
    txt = APP_PY.read_text(encoding="utf-8")
    if "# BB_PERF_PATCHED_V1" in txt:
        return

    old_imports = "import os\nimport json\nimport logging\nimport sys\nimport threading\nimport time"
    new_imports = "import gzip\nimport os\nimport json\nimport logging\nimport shutil\nimport sys\nimport threading\nimport time"
    if "import shutil" not in txt and old_imports in txt:
        txt = txt.replace(old_imports, new_imports, 1)

    old_handler_head = (
        "class Handler(SimpleHTTPRequestHandler):\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        super().__init__(*args, directory=str(WWW), **kwargs)\n"
        "\n"
        "    def end_headers(self) -> None:\n"
        "        path = urlparse(getattr(self, 'path', '')).path\n"
        "        import admin_buff\n"
        "        if admin_buff.page_path(path):\n"
        "            self.send_header('Cache-Control', 'no-store')\n"
        "            self.send_header('X-Content-Type-Options', 'nosniff')\n"
        "            self.send_header('Referrer-Policy', 'no-referrer')\n"
        "            self.send_header('X-Frame-Options', 'DENY')\n"
        "            self.send_header('Content-Security-Policy', \"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'\")\n"
        "        super().end_headers()\n"
        "\n"
        "    def do_HEAD(self) -> None:\n"
        "        path = urlparse(self.path).path\n"
        "        import admin_buff\n"
        "        if path.startswith('/api/admin/'):\n"
        "            return admin_buff.handle(self)\n"
        "        if admin_buff.page_path(path):\n"
        "            if not admin_buff.protect_page(self):\n"
        "                return\n"
        "        super().do_HEAD()"
    )

    new_handler_head = (
        "# BB_PERF_PATCHED_V1\n"
        "class Handler(SimpleHTTPRequestHandler):\n"
        "    protocol_version = 'HTTP/1.1'\n"
        "    _GZIP_EXTS = frozenset(('.json', '.js', '.css', '.xml', '.html', '.svg'))\n"
        "    _CACHE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp3', '.ogg', '.wav', '.json', '.js', '.css', '.swf', '.dat', '.cub', '.xml')\n"
        "\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        super().__init__(*args, directory=str(WWW), **kwargs)\n"
        "\n"
        "    def end_headers(self) -> None:\n"
        "        path = urlparse(getattr(self, 'path', '')).path\n"
        "        import admin_buff\n"
        "        if admin_buff.page_path(path):\n"
        "            self.send_header('Cache-Control', 'no-store')\n"
        "            self.send_header('X-Content-Type-Options', 'nosniff')\n"
        "            self.send_header('Referrer-Policy', 'no-referrer')\n"
        "            self.send_header('X-Frame-Options', 'DENY')\n"
        "            self.send_header('Content-Security-Policy', \"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'\")\n"
        "        elif not path.startswith(('/api/', '/admin/', '/capi')) and path.lower().endswith(self._CACHE_EXTS):\n"
        "            self.send_header('Cache-Control', 'public, max-age=604800, immutable')\n"
        "        super().end_headers()\n"
        "\n"
        "    def _serve_gzip(self, send_body: bool = True) -> bool:\n"
        "        path = urlparse(self.path).path\n"
        "        if 'gzip' not in (self.headers.get('Accept-Encoding') or '').lower():\n"
        "            return False\n"
        "        if path in ('/', '/index.html') or path.endswith('/'):\n"
        "            return False\n"
        "        try:\n"
        "            www_root = WWW.resolve()\n"
        "            file_path = (WWW / path.lstrip('/')).resolve()\n"
        "            if not file_path.is_relative_to(www_root):\n"
        "                return False\n"
        "        except (OSError, ValueError):\n"
        "            return False\n"
        "        if not file_path.is_file() or file_path.suffix.lower() not in self._GZIP_EXTS:\n"
        "            return False\n"
        "        st = file_path.stat()\n"
        "        if st.st_size <= 1024:\n"
        "            return False\n"
        "        gz_path = file_path.with_name(file_path.name + '.gz')\n"
        "        if gz_path.is_symlink():\n"
        "            return False\n"
        "        if not gz_path.is_file() or gz_path.stat().st_mtime < st.st_mtime:\n"
        "            tmp = file_path.with_name(f'{file_path.name}.{os.getpid()}.{threading.get_ident()}.gz.tmp')\n"
        "            with file_path.open('rb') as src, gzip.open(tmp, 'wb', compresslevel=6) as dst:\n"
        "                shutil.copyfileobj(src, dst)\n"
        "            os.replace(tmp, gz_path)\n"
        "        gz_size = gz_path.stat().st_size\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', self.guess_type(str(file_path)))\n"
        "        self.send_header('Content-Encoding', 'gzip')\n"
        "        self.send_header('Vary', 'Accept-Encoding')\n"
        "        self.send_header('Content-Length', str(gz_size))\n"
        "        self.end_headers()\n"
        "        if send_body:\n"
        "            try:\n"
        "                with gz_path.open('rb') as gf:\n"
        "                    shutil.copyfileobj(gf, self.wfile)\n"
        "            except (BrokenPipeError, ConnectionResetError):\n"
        "                pass\n"
        "        return True\n"
        "\n"
        "    def do_HEAD(self) -> None:\n"
        "        path = urlparse(self.path).path\n"
        "        import admin_buff\n"
        "        if path.startswith('/api/admin/'):\n"
        "            return admin_buff.handle(self)\n"
        "        if admin_buff.page_path(path):\n"
        "            if not admin_buff.protect_page(self):\n"
        "                return\n"
        "        if self._serve_gzip(send_body=False):\n"
        "            return\n"
        "        try:\n"
        "            super().do_HEAD()\n"
        "        except (BrokenPipeError, ConnectionResetError):\n"
        "            pass"
    )

    assert old_handler_head in txt, "Handler header block not found in app.py"
    txt = txt.replace(old_handler_head, new_handler_head, 1)

    old_get_tail = "\n        return super().do_GET()\n"
    new_get_tail = (
        "\n        if self._serve_gzip(send_body=True):\n"
        "            return\n"
        "        try:\n"
        "            return super().do_GET()\n"
        "        except (BrokenPipeError, ConnectionResetError):\n"
        "            return\n"
    )
    assert old_get_tail in txt, "do_GET tail not found in app.py"
    txt = txt.replace(old_get_tail, new_get_tail, 1)

    old_server = (
        "class Server(ThreadingHTTPServer):\n"
        "    def handle_error(self, request, client_address):\n"
        "        log.exception('[http] Request failed from %s', client_address)"
    )
    new_server = (
        "class Server(ThreadingHTTPServer):\n"
        "    request_queue_size = 128\n"
        "    daemon_threads = True\n"
        "\n"
        "    def handle_error(self, request, client_address):\n"
        "        exc = sys.exc_info()[1]\n"
        "        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):\n"
        "            return\n"
        "        log.exception('[http] Request failed from %s', client_address)"
    )
    assert old_server in txt, "Server block not found in app.py"
    txt = txt.replace(old_server, new_server, 1)

    APP_PY.write_text(txt, encoding="utf-8")
    print("[patch] Da bat HTTP/1.1 Keep-Alive + Gzip + Cache-Control trong app.py", flush=True)


def _patch_client_caching() -> None:
    if not HTML5_DIR.is_dir():
        return
    updated = 0
    for js_file in sorted(HTML5_DIR.glob("*.js")):
        if js_file.name in ("net.js", "m8_connection.js"):
            continue
        text = js_file.read_text(encoding="utf-8")
        new_text = re.sub(r"cache\s*:\s*['\"]no-store['\"]", "cache: 'force-cache'", text)
        if js_file.name == "battle_assets.js":
            new_text = new_text.replace("loadCatalog({refresh: true})", "loadCatalog({refresh: false})")
        if new_text != text:
            js_file.write_text(new_text, encoding="utf-8")
            updated += 1
    if updated:
        print(f"[patch] Da bat browser force-cache cho {updated} file JS", flush=True)


def _precompress_large_json() -> None:
    if not HTML5_DIR.is_dir():
        return
    count = 0
    for json_file in sorted(HTML5_DIR.glob("*.json")):
        st = json_file.stat()
        if st.st_size <= 1024 * 1024:
            continue
        gz_file = json_file.with_name(json_file.name + ".gz")
        if gz_file.is_file() and gz_file.stat().st_mtime >= st.st_mtime:
            continue
        print(f"[setup] Dang nen gzip {json_file.name} ({st.st_size / (1024 * 1024):.1f} MB)...", flush=True)
        tmp = json_file.with_name(f"{json_file.name}.gz.tmp")
        with json_file.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, gz_file)
        count += 1
    if count:
        print(f"[setup] Da nen san {count} file JSON lon", flush=True)


def _patch_performance() -> None:
    _patch_app_performance()
    _patch_client_caching()
    _precompress_large_json()


def _maybe_start_cloudflared(port: str) -> None:
    if os.environ.get("ENABLE_CLOUDFLARED") != "1":
        return
    bin_path = ROOT / "cloudflared"
    if not bin_path.is_file():
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
        print("[cloudflared] Dang tai cloudflared...", flush=True)
        _download_file(url, bin_path, {"User-Agent": "bangbang-launcher"}, "cloudflared")
        os.chmod(bin_path, 0o755)
    print(f"[cloudflared] Dang mo HTTPS tunnel toi cong {port}...", flush=True)
    subprocess.Popen([str(bin_path), "tunnel", "--url", f"http://127.0.0.1:{port}"])


def main() -> None:
    os.chdir(ROOT)
    port = (os.environ.get("PORT") or os.environ.get("SERVER_PORT") or "19083").strip()
    os.environ.setdefault("BB_PUBLIC_ADMIN", "1")
    os.environ.setdefault("BB_PUBLIC_WS", "1")

    _bootstrap_game_files()
    _patch_html5_bridge_for_proxy()
    _patch_performance()

    key_file = SERVER_DIR / "data" / "admin_buff.key"
    if key_file.is_file():
        os.chmod(key_file, 0o600)

    _maybe_start_cloudflared(port)

    print(f"[start] Khoi dong BangBang M72 tren cong {port}...", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(APP_PY),
            "--html5-local",
            "--bind",
            "0.0.0.0",
            "--port",
            port,
            "--enable-admin-buff",
        ],
    )


if __name__ == "__main__":
    main()
