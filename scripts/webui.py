"""Launch the local OurNotes dashboard without starting an emulator or publishing data."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def ready(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/api/launcher/ping", timeout=0.5) as response:
            return json.load(response).get("app") == "ournotes-launcher"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    url = f"http://127.0.0.1:{args.port}"
    if ready(url):
        if not args.no_browser:
            webbrowser.open(url)
        return
    from ournotes.common.bounded_log import console_logs
    from ournotes.common.secrets import SecretStore
    from ournotes.paths import DATA_ROOT
    with SecretStore(DATA_ROOT / 'state').lock('client'):
        console_logs(DATA_ROOT).prepare()
        if sys.stdout is None or sys.stderr is None or os.environ.get('OURNOTES_INSTALL_ROOT'):
            from ournotes.paths import DATA_ROOT
            folder = DATA_ROOT / "state/webui"
            folder.mkdir(parents=True, exist_ok=True)
            from ournotes.common.bounded_log import LogStream, console_logs
            log = LogStream(console_logs(DATA_ROOT), folder / "server.log")
            sys.stdout = sys.stderr = log
        import uvicorn

        from ournotes.control.web import create_app
        from ournotes.paths import DATA_ROOT
        app = create_app()
        app.state.launcher.notifications.url = url
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False, log_level="info"
            )
        )
        clean_shutdown = False
        def request_shutdown():
            nonlocal clean_shutdown
            clean_shutdown = True
            server.should_exit = True
        app.state.shutdown = request_shutdown
        if not args.no_browser:

            def show():
                for _ in range(50):
                    if ready(url):
                        webbrowser.open(url)
                        return
                    time.sleep(0.2)

            threading.Thread(target=show, daemon=True).start()
        server.run()
        jobs = app.state.launcher.jobs
        if clean_shutdown and not any(p.poll() is None for p in jobs.processes.values()):
            for reader in jobs.log_threads.values():
                reader.join(timeout=5)
            if not any(reader.is_alive() for reader in jobs.log_threads.values()):
                from ournotes.common.bounded_log import console_logs
                console_logs(DATA_ROOT).clear()



if __name__ == "__main__":
    main()
