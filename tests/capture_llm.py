"""A fake OpenAI-compatible model server: it records every request and answers "ok".

Run: capture_llm.py <port> <out.jsonl>. It shows exactly what OpenCode sends a model: the
system prompt, the instructions it loaded, the skills and tools it offered.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = sys.argv[2]
USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def chunk(**fields):
    return {"id": "cap", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "cap", **fields}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body: dict):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply({"object": "list", "data": [{"id": "cap", "object": "model"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(OUT, "a") as f:
            f.write(json.dumps(req) + "\n")
        if not req.get("stream"):
            self.reply({"id": "cap", "object": "chat.completion", "created": int(time.time()),
                        "model": "cap", "usage": USAGE, "choices": [{
                            "index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "ok"}}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in (chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]),
                  chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage=USAGE)):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
