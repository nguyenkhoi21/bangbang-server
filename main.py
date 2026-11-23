#!/usr/bin/env python3
"""BangBang M72 cloud bootstrapper for Altare / GitHub deploys."""
from __future__ import annotations

import json
import os
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

    _bootstrap_game_files()

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
