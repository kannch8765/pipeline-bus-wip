#!/usr/bin/env python3
"""Capture the Pipeline Bus frontend with local Chrome/Chromium.

No third-party Python packages are required. The helper starts a temporary local
server, opens the dashboard in a headless browser, waits for rendering, writes a
viewport screenshot, and shuts the server down.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote


def find_browser() -> str:
    configured = os.environ.get("CHROME_BIN")
    if configured and Path(configured).exists():
        return configured

    for command in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "msedge",
    ):
        found = shutil.which(command)
        if found:
            return found

    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise FileNotFoundError(
        "Chrome/Chromium was not found. Set CHROME_BIN to the browser executable."
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1100)
    parser.add_argument("--wait-ms", type=int, default=3500)
    parser.add_argument("--data", help="Optional JSON URL/path passed through ?data=")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("preview.png"),
    )
    args = parser.parse_args()

    frontend = Path(__file__).resolve().parent
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    browser = find_browser()
    port = free_port()

    def handler(*handler_args: object, **handler_kwargs: object) -> QuietHandler:
        return QuietHandler(
            *handler_args,
            directory=str(frontend),
            **handler_kwargs,
        )

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/"
    if args.data:
        url += f"?data={quote(args.data, safe='/:._-')}"

    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        "--force-device-scale-factor=1",
        f"--window-size={args.width},{args.height}",
        f"--virtual-time-budget={args.wait_ms}",
        f"--screenshot={output}",
        url,
    ]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        command.insert(1, "--no-sandbox")

    try:
        completed = subprocess.run(command, check=False)
    finally:
        server.shutdown()
        server.server_close()

    if completed.returncode != 0 or not output.exists():
        print("Preview capture failed.", file=sys.stderr)
        return completed.returncode or 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
