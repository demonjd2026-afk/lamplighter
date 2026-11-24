"""The model clients against a local stand-in server: request shape, parsing, and no key leakage."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lamplighter.llm import AnthropicClient, LLMError, OpenAICompatibleClient

KEY = "test-key-do-not-leak-0123456789"


@pytest.fixture()
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            if body["model"] == "broken":
                self.send_response(400)
                self.end_headers()
                return
            if self.path == "/v1/messages":
                reply = {"type": "message", "content": [{"type": "thinking", "thinking": "..."},
                                                         {"type": "text", "text": "hello "},
                                                         {"type": "text", "text": "world"}]}
            else:
                reply = {"choices": [{"message": {"role": "assistant", "content": "hello world"}}]}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()
    httpd.server_close()


def test_anthropic_request_shape_and_text_blocks(server):
    base, seen = server
    assert AnthropicClient(base, KEY, "some-model", "2026-10-21").complete("sys", "hi", 100) == "hello world"
    request = seen[0]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == KEY and request["headers"]["anthropic-version"] == "2026-10-21"
    assert request["body"] == {"model": "some-model", "max_tokens": 100, "system": "sys",
                               "messages": [{"role": "user", "content": "hi"}]}   # no sampling parameters


def test_openai_compatible_request_shape(server):
    base, seen = server
    assert OpenAICompatibleClient(base, KEY, "some-model").complete("sys", "hi") == "hello world"
    assert seen[0]["path"] == "/chat/completions" and seen[0]["headers"]["authorization"] == f"Bearer {KEY}"
    assert [m["role"] for m in seen[0]["body"]["messages"]] == ["system", "user"]


def test_client_errors_are_not_retried_and_never_leak_the_key(server):
    base, seen = server
    with pytest.raises(LLMError) as raised:
        AnthropicClient(base, KEY, "broken", "2026-10-21").complete("sys", "hi")
    assert len(seen) == 1 and KEY not in str(raised.value)
