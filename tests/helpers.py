"""Shared test helpers: import path, configs, fake model servers."""
import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lonoboard.config import DEFAULTS  # noqa: E402


def config(**board):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["board"].update(board)
    cfg["detect"]["enabled"] = False
    cfg["llama_models"]["dir"] = "/nonexistent"
    cfg["fleet"]["tailscale"] = False
    cfg["_path"] = None
    return cfg


class SSE(list):
    """A route body sent as server-sent events, one item per event."""


class FakeServer:
    """An HTTP server answering from a routes dict: {"GET /path": (status, body, content type)}.
    body may be a dict or list (sent as JSON), str, bytes, or SSE([...]) for a stream."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self, method):
                path = self.path.split("?")[0]
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((method, path, body, dict(self.headers)))
                route = outer.routes.get(f"{method} {path}")
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, payload, ctype = (route + ("application/json",))[:3] if len(route) == 2 else route
                if isinstance(payload, SSE):                     # server-sent events
                    self.send_response(status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for item in payload:
                        data = item if isinstance(item, str) else json.dumps(item)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    return
                data = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else \
                    payload.encode() if isinstance(payload, str) else payload
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


LLAMACPP = {
    "GET /health": (200, {"status": "ok"}),
    "GET /props": (200, {"total_slots": 4, "model_alias": "qwen3-8b", "model_path": "/models/qwen3-8b-Q4_K_M.gguf",
                         "build_info": "b1-abc1234", "default_generation_settings": {"n_ctx": 32768}}),
    "GET /v1/models": (200, {"data": [{"id": "qwen3-8b", "meta": {"n_ctx": 32768, "n_ctx_train": 40960, "n_params": 8190735360,
                                                                  "size": 5027783488, "ftype": "Q4_K - Medium"},
                                       "architecture": {"input_modalities": ["text"]}}]}),
    "GET /slots": (200, [{"id": 0, "id_task": 41, "is_processing": True, "next_token": [{"n_decoded": 12}]},
                         {"id": 1, "id_task": 7, "is_processing": False, "next_token": [{"n_decoded": 0}]}]),
    "GET /metrics": (501, {"error": "metrics disabled"}),
    "POST /v1/chat/completions": (200, SSE([
        {"choices": [{"delta": {"reasoning_content": "thinking about it"}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {"content": " there"}}]},
        {"choices": [], "timings": {"predicted_n": 2, "predicted_per_second": 42.0}},
        "[DONE]"])),
}

OLLAMA = {
    "GET /api/version": (200, {"version": "0.12.6"}),
    "GET /api/tags": (200, {"models": [
        {"name": "llama3.1:8b", "size": 4920753328, "details": {"parameter_size": "8.0B", "quantization_level": "Q4_K_M"}},
        {"name": "nomic-embed-text:latest", "size": 274302450, "details": {"parameter_size": "137M", "quantization_level": "F16"}}]}),
    "GET /api/ps": (200, {"models": [{"name": "llama3.1:8b", "size": 6654289920, "size_vram": 6654289920, "context_length": 8192}]}),
    "GET /v1/models": (200, {"data": [{"id": "llama3.1:8b"}]}),
}

VLLM = {
    "GET /v1/models": (200, {"data": [{"id": "meta-llama/Llama-3.1-8B-Instruct", "max_model_len": 32768}]}),
    "GET /version": (200, {"version": "0.10.2"}),
    "GET /health": (200, ""),
    "GET /metrics": (200, "# HELP vllm:num_requests_running running\n"
                          'vllm:num_requests_running{model_name="m"} 3.0\n'
                          'vllm:generation_tokens_total{model_name="m"} 1500.0\n'
                          'vllm:prompt_tokens_total{model_name="m"} 900.0\n'
                          'vllm:request_success_total{finished_reason="stop",model_name="m"} 4.0\n'
                          'vllm:request_success_total{finished_reason="length",model_name="m"} 1.0\n', "text/plain"),
}

OPENAI = {
    "GET /v1/models": (200, {"data": [{"id": "local-model"}, {"id": "other-model"}]}),
}
