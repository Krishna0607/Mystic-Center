"""Root-side control daemon: Unix socket JSON RPC + fan engine lifecycle."""

from __future__ import annotations

import grp
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import threading

from .privops import PrivOps
from .util import RUN_DIR, SOCKET_PATH

LOG = logging.getLogger("mystic.daemon")

MAX_MSG = 64 * 1024


def _peer_allowed(sock: socket.socket) -> bool:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        _pid, uid, gid = struct.unpack("3i", creds)
    except OSError:
        return False
    if uid == 0:
        return True
    try:
        wheel = grp.getgrnam("wheel")
    except KeyError:
        return False
    if gid == wheel.gr_gid:
        return True
    import pwd
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        return False
    if user in wheel.gr_mem:
        return True
    try:
        return wheel.gr_gid in os.getgrouplist(user, gid)
    except OSError:
        return False


class RPC(socketserver.StreamRequestHandler):
    server: "MysticServer"  # type: ignore[assignment]

    def handle(self) -> None:
        if not _peer_allowed(self.request):
            self._reply({"id": None, "ok": False, "error": "peer not authorized"})
            return
        while True:
            try:
                line = self.rfile.readline(MAX_MSG)
            except (OSError, ValueError):
                return
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._reply({"id": None, "ok": False, "error": f"bad json: {exc}"})
                return
            rid = req.get("id")
            op = req.get("op", "")
            args = req.get("args", {}) or {}
            try:
                data = self.server.ops_dispatch(op, args)
                self._reply({"id": rid, "ok": True, "data": data})
            except Exception as exc:  # every op failure is reported whole
                self._reply({"id": rid, "ok": False, "error": str(exc)})
            if req.get("close"):
                return

    def _reply(self, obj: dict) -> None:
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass


class MysticServer(socketserver.ThreadingUnixStreamServer):
    request_queue_size = 16
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, ops_dispatch, socket_path: str = SOCKET_PATH) -> None:
        self.ops_dispatch = ops_dispatch
        run_dir = os.path.dirname(socket_path)
        os.makedirs(run_dir, exist_ok=True)
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        super().__init__(socket_path, RPC)
        try:
            os.chmod(socket_path, 0o660)
            import pwd
            gid = grp.getgrnam("wheel").gr_gid
            os.chown(socket_path, 0, gid)
        except OSError:
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ops = PrivOps()
    server = MysticServer(ops_dispatch=_dispatch_builder(ops))
    LOG.info("mysticd listening on %s", SOCKET_PATH)

    def _shutdown(signum, _frame):
        LOG.info("signal %s — shutting down", signum)
        try:
            if msiec_fan_is_advanced():
                from . import msiec
                msiec.set_fan_mode("auto")
        except Exception as exc:
            LOG.warning("shutdown fan-mode check failed: %s", exc)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # leftover safety: if a previous crash left advanced mode (from a tool
    # that does drive duties), park at firmware auto on our start
    try:
        if msiec_fan_is_advanced():
            LOG.warning("found leftover advanced duty control — restoring auto")
            from . import msiec
            msiec.set_fan_mode("auto")
    except Exception:
        pass

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        ops.close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
    return 0


def msiec_fan_is_advanced() -> bool:
    try:
        from . import msiec
        return msiec.fan_mode() == "advanced"
    except Exception:
        return False


def _dispatch_builder(ops: PrivOps):
    def dispatch(op: str, args: dict):
        if op == "snapshot":
            return ops.snapshot()
        if op == "modes":
            return ops.modes()
        if op in ("set_shift_mode",):
            return ops.set_shift_mode(args["mode"])
        if op == "calibrate_shift_mode":
            return ops.calibrate_shift_mode()
        if op == "set_cooler_boost":
            return ops.set_cooler_boost(bool(args.get("on")))
        if op == "set_super_battery":
            return ops.set_super_battery(bool(args.get("on")))
        if op == "set_fan_preset":
            return ops.set_fan_preset(args["preset"])
        if op == "fan_status":
            return ops.fan_status()
        if op == "set_battery_limit":
            return ops.set_battery_limit(int(args["end"]),
                                         args.get("start"))
        if op == "set_webcam":
            return ops.set_webcam(bool(args.get("on")))
        if op == "set_fn_switch":
            return ops.set_fn_switch(args["side"])
        if op == "ec_dump":
            return ops.ec_dump(args.get("addrs"))
        raise KeyError(f"unknown op {op!r}")
    return dispatch


if __name__ == "__main__":
    raise SystemExit(main())
