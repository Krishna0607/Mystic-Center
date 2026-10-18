"""User-side client for the mysticd socket."""

from __future__ import annotations

import json
import os
import socket

from .util import SOCKET_PATH

_RID = {"n": 0}


class DaemonDown(RuntimeError):
    pass


class Client:
    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self.sock: socket.socket | None = None

    # ---- low level ----------------------------------------------------------

    def _connect(self) -> socket.socket:
        if self.sock is not None:
            return self.sock
        if not os.path.exists(SOCKET_PATH):
            raise DaemonDown(
                f"daemon socket {SOCKET_PATH} missing — is mystic-center.service running?")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(SOCKET_PATH)
        except OSError as e:
            s.close()
            raise DaemonDown(f"cannot reach daemon: {e}") from e
        self.sock = s
        self.rfile = s.makefile("rb")
        return s

    def request(self, op: str, **args) -> dict:
        _RID["n"] += 1
        rid = _RID["n"]
        sock = self._connect()
        req = json.dumps({"id": rid, "op": op, "args": args}) + "\n"
        try:
            sock.sendall(req.encode())
            line = self.rfile.readline()
            if not line:
                raise DaemonDown("daemon closed connection")
            resp = json.loads(line.decode())
        except socket.timeout as e:
            raise DaemonDown(f"daemon timeout on {op!r}") from e
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "daemon error"))
        return resp.get("data", {})

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- convenience ----------------------------------------------------------

    def snapshot(self) -> dict:
        return self.request("snapshot")

    def modes(self) -> dict:
        return self.request("modes")


def main_status() -> dict:
    with Client() as c:
        return c.snapshot()
