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
ERLANG_PY = SERVER_DIR / "server" / "erlang_actions.py"
STORAGE_PY = SERVER_DIR / "server" / "storage.py"
SIXCUBE_PY = SERVER_DIR / "server" / "sixcube_server.py"
MP_BATTLE_PY = SERVER_DIR / "server" / "multiplayer_battle.py"
MP_GATE_PY = SERVER_DIR / "server" / "multiplayer_gate.py"
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
    if 'os.environ.get("BB_PUBLIC_WS") == "1"' in txt:
        return
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
        BRIDGE_PY.write_text(pattern.sub(new_fn, txt, count=1), encoding="utf-8")
        print("[patch] Da cap nhat kiem tra WebSocket cho proxy Altare", flush=True)


def _revert_perf_patches() -> None:
    if APP_PY.is_file():
        txt = APP_PY.read_text(encoding="utf-8")
        if "# BB_PERF_PATCHED_V1" in txt:
            txt = txt.replace(
                "import gzip\nimport os\nimport json\nimport logging\nimport shutil\nimport sys\nimport threading\nimport time",
                "import os\nimport json\nimport logging\nimport sys\nimport threading\nimport time",
                1,
            )
            txt = re.sub(
                r"# BB_PERF_PATCHED_V1\nclass Handler\(SimpleHTTPRequestHandler\):.*?(?=\n    def log_message\(self)",
                (
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
                    "        super().do_HEAD()\n"
                ),
                txt,
                count=1,
                flags=re.DOTALL,
            )
            txt = txt.replace(
                "\n        if self._serve_gzip(send_body=True):\n            return\n        try:\n            return super().do_GET()\n        except (BrokenPipeError, ConnectionResetError):\n            return\n",
                "\n        return super().do_GET()\n",
                1,
            )
            txt = txt.replace(
                (
                    "class Server(ThreadingHTTPServer):\n"
                    "    request_queue_size = 128\n"
                    "    daemon_threads = True\n\n"
                    "    def handle_error(self, request, client_address):\n"
                    "        exc = sys.exc_info()[1]\n"
                    "        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):\n"
                    "            return\n"
                    "        log.exception('[http] Request failed from %s', client_address)"
                ),
                (
                    "class Server(ThreadingHTTPServer):\n"
                    "    def handle_error(self, request, client_address):\n"
                    "        log.exception('[http] Request failed from %s', client_address)"
                ),
                1,
            )
            APP_PY.write_text(txt, encoding="utf-8")
            print("[revert] Da go ban V1 cu cua app.py", flush=True)

    if HTML5_DIR.is_dir():
        reverted_js = 0
        for js_file in sorted(HTML5_DIR.glob("*.js")):
            if js_file.name in ("net.js", "m8_connection.js"):
                continue
            orig = js_file.read_text(encoding="utf-8")
            lines = orig.splitlines(keepends=True)
            new_lines = []
            for line in lines:
                if "cache: 'force-cache'" in line and "requestUrl" not in line and "redirect: 'error'" not in line:
                    line = line.replace("cache: 'force-cache'", "cache: 'no-store'")
                if js_file.name == "battle_assets.js":
                    line = line.replace("loadCatalog({refresh: false})", "loadCatalog({refresh: true})")
                new_lines.append(line)
            updated = "".join(new_lines)
            if updated != orig:
                js_file.write_text(updated, encoding="utf-8")
                reverted_js += 1
        if reverted_js:
            print(f"[revert] Da khoi phuc {reverted_js} file JS ve ban goc", flush=True)


def _patch_safe_performance() -> None:
    if APP_PY.is_file():
        txt = APP_PY.read_text(encoding="utf-8")
        if "# BB_SAFE_PERF_V2" not in txt:
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
                "# BB_SAFE_PERF_V2\n"
                "class Handler(SimpleHTTPRequestHandler):\n"
                "    _GZIP_EXTS = frozenset(('.json', '.js', '.css', '.xml'))\n"
                "    _MEDIA_CACHE_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp3', '.ogg', '.wav')\n"
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
                "        elif not path.startswith(('/api/', '/admin/', '/capi')) and path.lower().endswith(self._MEDIA_CACHE_EXTS):\n"
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
            assert old_handler_head in txt, "Handler head not found in app.py"
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
            print("[patch] Da bat Gzip + Cache media + TCP backlog trong app.py (giu HTTP/1.0)", flush=True)

    if BRIDGE_PY.is_file():
        btxt = BRIDGE_PY.read_text(encoding="utf-8")
        anchor = "    handler.connection.settimeout(CLIENT_IDLE_TIMEOUT)"
        nodelay = (
            "    handler.connection.settimeout(CLIENT_IDLE_TIMEOUT)\n"
            "    try:\n"
            "        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)\n"
            "    except OSError:\n"
            "        pass"
        )
        if "handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)" not in btxt and anchor in btxt:
            BRIDGE_PY.write_text(btxt.replace(anchor, nodelay, 1), encoding="utf-8")
            print("[patch] Da bat TCP_NODELAY tren WebSocket", flush=True)

    if HTML5_DIR.is_dir():
        count = 0
        for json_file in sorted(HTML5_DIR.glob("*.json")):
            st = json_file.stat()
            if st.st_size <= 1024 * 1024:
                continue
            gz_file = json_file.with_name(json_file.name + ".gz")
            if gz_file.is_file() and gz_file.stat().st_mtime >= st.st_mtime:
                continue
            tmp = json_file.with_name(f"{json_file.name}.gz.tmp")
            with json_file.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, gz_file)
            count += 1
        if count:
            print(f"[setup] Da nen san {count} file JSON lon", flush=True)


def _patch_erlang_e() -> None:
    if not ERLANG_PY.is_file():
        return
    txt = ERLANG_PY.read_text(encoding="utf-8")
    if "# BB_ERLANG_E_FIXED_V1" in txt:
        return

    # Normalize CRLF so string matches work cleanly on Linux
    txt = txt.replace("\r\n", "\n")

    old_update = (
        "        if self.dog and (not self.alive or now>=self.dog['finish'] or not any(\n"
        "                callback.__self__ is self and callback.__name__=='_event' and args[0]=='flight' and args[1][0] is self.dog\n"
        "                for _,_,callback,args in self.arena.pending)):\n"
        "            packets.extend(self._drop_dog(now))"
    )
    new_update = (
        "        # BB_ERLANG_E_FIXED_V1\n"
        "        if self.dog and not (self.ready and self.ready.get('slot')==1) and (\n"
        "                not self.alive or not self.has_buff(361201,now) or now>=self.dog['begin']+2000):\n"
        "            packets.extend(self._drop_dog(now))"
    )
    assert old_update in txt, "Erlang update() dog block not found"
    txt = txt.replace(old_update, new_update, 1)

    old_flight = (
        "        packets=piercing_step(self,flight,now,self._bullet_hit,pierce=flight['template']!=361100)\n"
        "        if flight['template']==361501 and (now>=flight['finish'] or not any(\n"
        "                callback.__self__ is self and callback.__name__=='_event' and args[0]=='flight' and args[1][0] is flight\n"
        "                for _,_,callback,args in self.arena.pending)):\n"
        "            packets.extend(self._drop_dog(now))\n"
        "        return packets"
    )
    new_flight = (
        "        return piercing_step(self,flight,now,self._bullet_hit,pierce=flight['template']!=361100)"
    )
    assert old_flight in txt, "Erlang _flight() block not found"
    txt = txt.replace(old_flight, new_flight, 1)

    old_ready = (
        "            if cast['slot']==1:\n"
        "                dog=cast['dog']\n"
        "                if self.dog is not dog or now>=dog['finish']:return []\n"
        "                pos=point_at(dog['start'],dog['end'],(now-dog['begin'])/(dog['finish']-dog['begin']))"
    )
    new_ready = (
        "            if cast['slot']==1:\n"
        "                dog=cast['dog']\n"
        "                if not dog:return []\n"
        "                self.dog=None\n"
        "                ratio=min(1.0,max(0.0,(now-dog['begin'])/max(1,dog['finish']-dog['begin'])))\n"
        "                pos=point_at(dog['start'],dog['end'],ratio)"
    )
    assert old_ready in txt, "Erlang erlang_ready slot 1 block not found"
    txt = txt.replace(old_ready, new_ready, 1)

    old_same_dest = "        if slot in (0,1,2) and not second and start==dest:return []"
    new_same_dest = (
        "        if slot==1 and not second and start==dest:\n"
        "            dest=(start[0]+60,start[1])\n"
        "        elif slot in (0,2) and start==dest:\n"
        "            return []"
    )
    assert old_same_dest in txt, "Erlang start==dest guard not found"
    txt = txt.replace(old_same_dest, new_same_dest, 1)

    ERLANG_PY.write_text(txt, encoding="utf-8")
    print("[patch] Da sua chieu E Duong Tien (giu cua so 2s, sua race 50ms, cho ban tai cho)", flush=True)



def _patch_pvp_latency() -> None:
    patched = []

    if STORAGE_PY.is_file():
        txt = STORAGE_PY.read_text(encoding="utf-8").replace("\r\n", "\n")
        if "# BB_PVP_LOWLATENCY_V1" not in txt:
            old_pragma = "        db.execute('PRAGMA foreign_keys=ON')"
            new_pragma = (
                "        # BB_PVP_LOWLATENCY_V1\n"
                "        db.execute('PRAGMA foreign_keys=ON')\n"
                "        db.execute('PRAGMA synchronous=NORMAL')\n"
                "        db.execute('PRAGMA temp_store=MEMORY')\n"
                "        db.execute('PRAGMA cache_size=-32000')"
            )
            assert old_pragma in txt, "storage.py PRAGMA block not found"
            STORAGE_PY.write_text(txt.replace(old_pragma, new_pragma, 1), encoding="utf-8")
            patched.append("storage.py")

    if SIXCUBE_PY.is_file():
        txt = SIXCUBE_PY.read_text(encoding="utf-8").replace("\r\n", "\n")
        if "# BB_PVP_LOWLATENCY_V1" not in txt:
            old_var = "    boss_notice_state = None\n\n    def checkpoint():\n        nonlocal saved_arena"
            new_var = (
                "    boss_notice_state = None\n"
                "    last_battle_checkpoint = 0.0\n\n"
                "    def checkpoint():\n"
                "        nonlocal saved_arena, last_battle_checkpoint"
            )
            assert old_var in txt, "sixcube_server.py checkpoint header not found"
            txt = txt.replace(old_var, new_var, 1)

            old_cp_end = (
                "        else:\n"
                "            ranked_matches.checkpoint(session_state,arena,saved_arena,world_db)\n\n"
                "    def send_actions(packets):"
            )
            new_cp_end = (
                "        else:\n"
                "            ranked_matches.checkpoint(session_state,arena,saved_arena,world_db)\n"
                "        last_battle_checkpoint = time.monotonic()\n\n"
                "    def battle_checkpoint_due(packets=()):\n"
                "        battle = session_state.get('battle')\n"
                "        arena = battle.get('actions') if battle else None\n"
                "        if arena is None or (arena.pk and arena.pk.get('finished')):\n"
                "            return True\n"
                "        if any(mid == 1708 for mid, _ in packets):\n"
                "            return True\n"
                "        return (time.monotonic() - last_battle_checkpoint) >= 2.0\n\n"
                "    def send_actions(packets):"
            )
            assert old_cp_end in txt, "sixcube_server.py checkpoint tail not found"
            txt = txt.replace(old_cp_end, new_cp_end, 1)

            old_tick = (
                "        battle = session_state[\"battle\"]\n"
                "        if battle and battle.get(\"entered\") and battle.get(\"actions\"):\n"
                "            packets = battle[\"actions\"].tick()\n"
                "            checkpoint()\n"
                "            send_actions(packets)"
            )
            new_tick = (
                "        # BB_PVP_LOWLATENCY_V1\n"
                "        battle = session_state[\"battle\"]\n"
                "        if battle and battle.get(\"entered\") and battle.get(\"actions\"):\n"
                "            packets = battle[\"actions\"].tick()\n"
                "            arena_actions = battle[\"actions\"]\n"
                "            finished_now = bool((arena_actions.pk and arena_actions.pk.get(\"finished\")) or any(m == 1708 for m, _ in packets))\n"
                "            if finished_now:\n"
                "                checkpoint()\n"
                "                send_actions(packets)\n"
                "            else:\n"
                "                if packets:\n"
                "                    send_actions(packets)\n"
                "                if battle_checkpoint_due(packets):\n"
                "                    checkpoint()"
            )
            assert old_tick in txt, "sixcube_server.py tick_actions block not found"
            txt = txt.replace(old_tick, new_tick, 1)

            old_handle = (
                "                        if actions:\n"
                "                            conn.settimeout(0.02)\n"
                "                        log.info(f\"[sixcube] {addr} [BATTLE-ENTRY] scene={scene_id} snapshot sent; awaiting client verification\")\n\n"
                "                    elif mid in (501, 503, 510, 514):\n"
                "                        # Dispatch only tanks with implemented base mechanics.\n"
                "                        battle = session_state[\"battle\"]\n"
                "                        if battle and battle.get(\"entered\") and battle.get(\"actions\"):\n"
                "                            packets = battle[\"actions\"].handle(mid,payload)\n"
                "                            checkpoint()\n"
                "                            send_actions(packets)\n"
                "                            continue  # Already committed before the combat reply."
            )
            new_handle = (
                "                        if actions:\n"
                "                            conn.settimeout(0.005)\n"
                "                        log.info(f\"[sixcube] {addr} [BATTLE-ENTRY] scene={scene_id} snapshot sent; awaiting client verification\")\n\n"
                "                    elif mid in (501, 503, 510, 514):\n"
                "                        # Dispatch only tanks with implemented base mechanics.\n"
                "                        battle = session_state[\"battle\"]\n"
                "                        if battle and battle.get(\"entered\") and battle.get(\"actions\"):\n"
                "                            packets = battle[\"actions\"].handle(mid,payload)\n"
                "                            arena_actions = battle[\"actions\"]\n"
                "                            finished_now = bool((arena_actions.pk and arena_actions.pk.get(\"finished\")) or any(m == 1708 for m, _ in packets))\n"
                "                            if finished_now:\n"
                "                                checkpoint()\n"
                "                                send_actions(packets)\n"
                "                            else:\n"
                "                                if packets:\n"
                "                                    send_actions(packets)\n"
                "                                if battle_checkpoint_due(packets):\n"
                "                                    checkpoint()\n"
                "                            continue"
            )
            assert old_handle in txt, "sixcube_server.py input handle block not found"
            txt = txt.replace(old_handle, new_handle, 1)

            SIXCUBE_PY.write_text(txt, encoding="utf-8")
            patched.append("sixcube_server.py")

    if MP_BATTLE_PY.is_file():
        txt = MP_BATTLE_PY.read_text(encoding="utf-8").replace("\r\n", "\n")
        if "# BB_PVP_LOWLATENCY_V1" not in txt:
            old_tick_sec = "TICK_SECONDS = .02"
            new_tick_sec = "# BB_PVP_LOWLATENCY_V1\nTICK_SECONDS = .008"
            assert old_tick_sec in txt, "multiplayer_battle.py TICK_SECONDS not found"
            txt = txt.replace(old_tick_sec, new_tick_sec, 1)

            old_loop = (
                "                    if command:\n"
                "                        self._execute(command)\n"
                "                        command_budget=time.monotonic()+.004\n"
                "                        for _ in range(31):\n"
                "                            if time.monotonic() >= command_budget:\n"
                "                                break\n"
                "                            try:\n"
                "                                self._execute(self._commands.get_nowait())\n"
                "                            except queue.Empty:\n"
                "                                break\n"
                "                    now = time.monotonic()\n"
                "                    if now >= next_tick:\n"
                "                        dirty=[]\n"
                "                        for match in tuple(self.matches.values()):\n"
                "                            try:\n"
                "                                if self._tick(match,now):dirty.append(match)\n"
                "                            except Exception as exc:\n"
                "                                if not match.error:\n"
                "                                    log.exception('Shared room %s checkpoint/tick failed',match.room_id)\n"
                "                                match.error = str(exc)\n"
                "                                self._publish_status(match)\n"
                "                        if dirty:\n"
                "                            try:\n"
                "                                self._persist_many(dirty)\n"
                "                                for match in dirty:self._publish_committed(match)\n"
                "                            except Exception as exc:\n"
                "                                for match in dirty:\n"
                "                                    if not match.error:log.exception('Shared room batch checkpoint failed')\n"
                "                                    match.error=str(exc)\n"
                "                                    self._publish_status(match)"
            )
            new_loop = (
                "                    had_input = bool(command and command[0] == 'handle')\n"
                "                    if command:\n"
                "                        self._execute(command)\n"
                "                        command_budget=time.monotonic()+.004\n"
                "                        for _ in range(31):\n"
                "                            if time.monotonic() >= command_budget:\n"
                "                                break\n"
                "                            try:\n"
                "                                next_cmd = self._commands.get_nowait()\n"
                "                                if next_cmd[0] == 'handle':\n"
                "                                    had_input = True\n"
                "                                self._execute(next_cmd)\n"
                "                            except queue.Empty:\n"
                "                                break\n"
                "                    now = time.monotonic()\n"
                "                    if had_input:\n"
                "                        next_tick = min(next_tick, now)\n"
                "                    if now >= next_tick:\n"
                "                        dirty=[]\n"
                "                        for match in tuple(self.matches.values()):\n"
                "                            try:\n"
                "                                if self._tick(match,now):dirty.append(match)\n"
                "                            except Exception as exc:\n"
                "                                if not match.error:\n"
                "                                    log.exception('Shared room %s checkpoint/tick failed',match.room_id)\n"
                "                                match.error = str(exc)\n"
                "                                self._publish_status(match)\n"
                "                        if dirty:\n"
                "                            must_persist = [\n"
                "                                m for m in dirty\n"
                "                                if m.phase != 'battle'\n"
                "                                or (m.arena and m.arena.pk and m.arena.pk['finished'] and not m.state['battle'].get('completed_recorded'))\n"
                "                                or now - getattr(m, '_last_persist', 0.0) >= 2.0\n"
                "                            ]\n"
                "                            fast_publish = [m for m in dirty if m not in must_persist]\n"
                "                            if must_persist:\n"
                "                                try:\n"
                "                                    self._persist_many(must_persist)\n"
                "                                    for match in must_persist:\n"
                "                                        match._last_persist = now\n"
                "                                        self._publish_committed(match)\n"
                "                                except Exception as exc:\n"
                "                                    for match in must_persist:\n"
                "                                        if not match.error:log.exception('Shared room batch checkpoint failed')\n"
                "                                        match.error=str(exc)\n"
                "                                        self._publish_status(match)\n"
                "                            for match in fast_publish:\n"
                "                                try:\n"
                "                                    self._publish_committed(match)\n"
                "                                    match.dirty = False\n"
                "                                except Exception as exc:\n"
                "                                    if not match.error:log.exception('Shared room publish failed')\n"
                "                                    match.error=str(exc)\n"
                "                                    self._publish_status(match)"
            )
            assert old_loop in txt, "multiplayer_battle.py _run loop not found"
            txt = txt.replace(old_loop, new_loop, 1)

            MP_BATTLE_PY.write_text(txt, encoding="utf-8")
            patched.append("multiplayer_battle.py")

    if MP_GATE_PY.is_file():
        txt = MP_GATE_PY.read_text(encoding="utf-8").replace("\r\n", "\n")
        if "# BB_PVP_LOWLATENCY_V1" not in txt:
            old_reg = "        self.conn.settimeout(.1)\n        self.last_profile = 0"
            new_reg = "        # BB_PVP_LOWLATENCY_V1\n        self.conn.settimeout(.02)\n        self.last_profile = 0"
            assert old_reg in txt, "multiplayer_gate.py _register timeout not found"
            txt = txt.replace(old_reg, new_reg, 1)

            old_start = "        self.started_room = room['room_id']\n        self.conn.settimeout(.02)"
            new_start = "        self.started_room = room['room_id']\n        self.conn.settimeout(.005)"
            assert old_start in txt, "multiplayer_gate.py _send_start timeout not found"
            txt = txt.replace(old_start, new_start, 1)

            old_clear = "        self.selection_key = None\n        self.conn.settimeout(.1)\n        self.checkpoint()"
            new_clear = "        self.selection_key = None\n        self.conn.settimeout(.02)\n        self.checkpoint()"
            assert old_clear in txt, "multiplayer_gate.py _clear_local timeout not found"
            txt = txt.replace(old_clear, new_clear, 1)

            old_in = (
                "        if mid in INPUT_MESSAGES:\n"
                "            if self.started_room:\n"
                "                self.battles.submit_input(self.profile, mid, payload)\n"
                "                return True"
            )
            new_in = (
                "        if mid in INPUT_MESSAGES:\n"
                "            if self.started_room:\n"
                "                self.battles.submit_input(self.profile, mid, payload)\n"
                "                packets = self.battles.drain_presented(self.profile)\n"
                "                if packets:\n"
                "                    self._send(packets)\n"
                "                return True"
            )
            assert old_in in txt, "multiplayer_gate.py input handle not found"
            txt = txt.replace(old_in, new_in, 1)

            MP_GATE_PY.write_text(txt, encoding="utf-8")
            patched.append("multiplayer_gate.py")

    if patched:
        print("[patch] Da toi uu PvP low-latency (SQLite WAL + 125Hz combat tick + instant packet fanout)", flush=True)

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
    _revert_perf_patches()
    _patch_html5_bridge_for_proxy()
    _patch_safe_performance()
    _patch_erlang_e()
    _patch_pvp_latency()

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
