"""测试用的假模型服务：实现 OpenAI 兼容的 /v1/chat/completions 与 /v1/models。

它不需要真的模型——回复内容由最后一条用户消息里的关键词决定，
方便端到端测试断言（记忆条数、分句、沉默、报错等）。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHORT_REPLY = "咕噜~本鱼听见了：{snippet}"
LONG_REPLY = (
    "第一句先拿来量一量长度，看看切分灵不灵。"
    "第二句继续拉长内容，让分句逻辑真的生效。"
    "第三句还没说完呢，别急着催本鱼。"
    "第四句总该差不多了吧，再长就不礼貌了。"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class MockOpenAI:
    """线程化的假模型服务。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.fail_next = 0  # 让接下来 N 次请求返回 500，用来测降级

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # 静音
                return

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    body = json.dumps(
                        {"object": "list", "data": [{"id": "deepseek-v4-flash-free", "object": "model"}]}
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                payload = self._read_body()
                with outer._lock:
                    outer.requests.append({"path": self.path, "body": payload, "ts": time.time()})
                    failing = outer.fail_next > 0
                    if failing:
                        outer.fail_next -= 1
                if failing:
                    body = b'{"error":{"message":"mock upstream failure"}}'
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                prompt = sum(len(str(m.get("content", ""))) for m in payload.get("messages", []))
                reply = outer._reply_for(payload)
                usage = {
                    "prompt_tokens": max(1, prompt // 2),
                    "completion_tokens": max(1, len(reply) // 2),
                    "total_tokens": max(1, prompt // 2 + len(reply) // 2),
                }
                if payload.get("stream"):
                    self._send_stream(reply, usage, str(payload.get("model") or "mock"))
                else:
                    body = json.dumps(
                        {
                            "id": "chatcmpl-mock",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": payload.get("model") or "mock",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": reply},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": usage,
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def _send_stream(self, reply: str, usage: dict, model: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(data: str) -> None:
                    blob = data.encode("utf-8")
                    self.wfile.write(f"{len(blob):X}\r\n".encode())
                    self.wfile.write(blob + b"\r\n")
                    self.wfile.flush()

                for i in range(0, len(reply), 3):  # 三字一小段，模拟真实流
                    piece = reply[i : i + 3]
                    chunk(
                        "data: "
                        + json.dumps(
                            {
                                "id": "chatcmpl-mock",
                                "object": "chat.completion.chunk",
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": piece}}],
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    time.sleep(0.005)
                chunk(
                    "data: "
                    + json.dumps({"id": "chatcmpl-mock", "choices": [], "usage": usage})
                    + "\n\n"
                )
                chunk("data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---------------------------------------------------------------- 行为
    @staticmethod
    def _reply_for(payload: dict) -> str:
        last = ""
        for msg in reversed(payload.get("messages", [])):
            if msg.get("role") == "user":
                last = str(msg.get("content") or "")
                break
        if "长文" in last:
            return LONG_REPLY
        if "沉默" in last:
            return "[沉默]"
        return SHORT_REPLY.format(snippet=last.strip()[:24] or "空消息")

    @property
    def last_request(self) -> dict | None:
        with self._lock:
            return self.requests[-1]["body"] if self.requests else None

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.requests)
