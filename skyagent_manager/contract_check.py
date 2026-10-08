"""Local-only HTTP contract verification with synthetic records and credentials."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import Thread

from skyagent_manager.sync import (
    ACCOUNT_PATH,
    AccountImportClient,
    SyncClient,
    validate_config,
)


@contextmanager
def local_backend():
    state = {"requests": [], "records": {}, "mode": "normal"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never print credentials, payloads, or request URLs.

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(length))
            state["requests"].append((self.path, payload))
            status = 200
            if self.headers.get("x-skyhotel-import-secret") != "synthetic-secret":
                status, body = 401, {"ok": False}
            elif state["mode"] == "redirect":
                status, body = 307, {}
            elif state["mode"] == "invalid-json":
                body = None
            elif state["mode"] == "oversized":
                body = "x" * (2 * 1024 * 1024 + 1)
            else:
                results = []
                for index, item in enumerate(payload["items"]):
                    accepted = (
                        item.get("phone") != "00000000001"
                        if self.path == ACCOUNT_PATH
                        else item.get("name") != "reject-synthetic"
                    )
                    if accepted:
                        state["records"][
                            (self.path, item.get("id", item.get("phone", "")))
                        ] = item
                    results.append({"index": index, "ok": accepted})
                if state["mode"] == "missing-result":
                    results = results[:-1]
                elif state["mode"] == "duplicate-index":
                    results.append(results[0])
                body = {"ok": True, "results": list(reversed(results))}
            encoded = b"not-json" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if status == 307:
                self.send_header("Location", "/redirect-target")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

    class LocalServer(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer normally performs reverse DNS even for numeric loopback.
            # These tests need no DNS and must remain offline on restricted hosts.
            TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = self.server_address[1]

    server = LocalServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def check_contract() -> dict:
    checks = []
    with local_backend() as (url, state):
        config = validate_config(url, "/test/members", "/test/benefits", True)
        client = SyncClient(config, "members", "synthetic-secret")
        try:
            items = [
                {
                    "id": "synthetic-1",
                    "name": "accepted-synthetic",
                    "phone": "000",
                    "note": "",
                },
                {
                    "id": "synthetic-2",
                    "name": "reject-synthetic",
                    "phone": "001",
                    "note": "",
                },
            ]
            results = client.submit(items)
            if [result.ok for result in results] != [True, False]:
                raise RuntimeError("per-record result mapping failed")
            checks.append("http-auth-and-index-mapping")
            client.submit([items[0]])
            if len(state["records"]) != 1:
                raise RuntimeError("synthetic backend deduplication failed")
            checks.append("synthetic-backend-upsert")
            for mode in ("redirect", "invalid-json", "oversized"):
                state["mode"] = mode
                count = len(state["requests"])
                if client.submit([items[0]])[0].ok:
                    raise RuntimeError("invalid response accepted")
                if len(state["requests"]) != count + 1:
                    raise RuntimeError("unexpected redirect or retry")
                checks.append(mode)
            state["mode"] = "normal"
            account_client = AccountImportClient(config, "synthetic-secret")
            try:
                account = {
                    "id": "local-account-id",
                    "phone": "00000000000",
                    "token": "synthetic-account-token",
                    "is_online": True,
                    "is_new_user": False,
                    "is_enabled": True,
                    "remark": "软件导入",
                }
                if not account_client.submit([account])[0].ok:
                    raise RuntimeError("account contract failed")
                path, body = state["requests"][-1]
                if path != ACCOUNT_PATH or "id" in body["items"][0]:
                    raise RuntimeError("account payload mismatch")
                checks.append("original-account-contract")
            finally:
                account_client.close()
        finally:
            client.close()
        bad_client = SyncClient(config, "members", "incorrect-synthetic-secret")
        try:
            if bad_client.submit([items[0]])[0].ok:
                raise RuntimeError("invalid credentials accepted")
            checks.append("http-401")
        finally:
            bad_client.close()
    return {"ok": True, "scope": "loopback-only-synthetic", "checks": checks}


def main(report_path: Path | None = None) -> int:
    try:
        report = check_contract()
    except Exception as exc:
        report = {"ok": False, "error_type": type(exc).__name__}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path is not None:
        report_path.write_text(output + "\n", encoding="utf-8")
    if sys.stdout is not None:
        print(output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
