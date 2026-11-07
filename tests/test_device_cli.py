"""Exercise the actual terminal and HTTP planner with no model or devices."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer


@unittest.skipIf(os.name == "nt", "POSIX PTY smoke test")
class CLISmoke(unittest.TestCase):
    def test_real_terminal_korean_preview_stop_exit(self):
        import fcntl
        import pty
        import struct
        import termios
        calls = []
        spec = dict(mode="execute", actions=[dict(device="christmas", changes=dict(preset="santa", snow="heavy"))],
                    explanation="산타와 눈")
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(dict(done=True, message=dict(content=json.dumps(spec)))).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KEVIN_", "TERMINATOR_"))}
        env["KEVIN_OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"
        proc = subprocess.Popen([sys.executable, "src/companion_cli.py"], stdin=slave, stdout=slave, stderr=slave,
                                env=env, cwd=Path(__file__).resolve().parent.parent)
        os.close(slave)
        def until(label):
            output, deadline = bytearray(), time.monotonic() + 12
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output.extend(os.read(master, 65536))
                    if label.encode() in output:
                        return
            self.fail("missing terminal state: " + label)
        try:
            until("Space: 말 걸기")
            os.write(master, b" ")
            until("듣고 있어요.")
            os.write(master, "산타 눈 펑펑\r".encode())
            until("미리보기 완료")
            self.assertEqual(len(calls), 1)
            self.assertIn("devices", json.loads(calls[0]["messages"][1]["content"]))
            os.write(master, b"\x1b")
            until("대기 동작 취소")
            os.write(master, b"\x03")
            until("\x1b[?1049l")
            self.assertEqual(proc.wait(timeout=3), 0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
            os.close(master)
            server.shutdown()
            server.server_close()
