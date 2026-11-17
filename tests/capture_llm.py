"""A fake OpenAI-compatible model server: it records every request and answers "ok".

Run: capture_llm.py <port> <out.jsonl> [<tool call or list of calls, as JSON>] [<prompt tokens>].
It shows exactly what OpenCode sends a model: the system prompt, the instructions it loaded, the
skills and tools it offered. Given tool calls ({"name": ..., "arguments": {...}}), each request that offers tools
gets the next call as its answer, so a test can see what OpenCode does with them (the request
after a call carries the tool's result). Given prompt tokens, every answer says it read that many,
so a test can fill the context window without sending it (OpenCode then compacts: a request
carrying its summary template gets the template's headings back, which it takes as a summary).
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = sys.argv[2]
CALLS = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []
CALLS = CALLS if isinstance(CALLS, list) else [CALLS]
made = []
READ = int(sys.argv[4]) if len(sys.argv) > 4 else 1
USAGE = {"prompt_tokens": READ, "completion_tokens": 1, "total_tokens": READ + 1}
SUMMARY = "\n\n".join(f"{h}\n- (none)" for h in (
    "## Objective", "## Requirements", "## Decisions", "## Work State\n### Completed",
    "### Active", "### Blocked", "## Next Move", "## Relevant Files", "## Important Context"))


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
        asked = json.dumps(req.get("messages", [])[-2:])
        if "<template>" in asked and "## Objective" in asked:  # OpenCode compacting the session
            self.text(SUMMARY)
            return
        if req.get("tools") and len(made) < len(CALLS):
            c = CALLS[len(made)]
            made.append(1)
            call = {"index": 0, "id": f"call_{len(made)}", "type": "function", "function": {
                "name": c["name"], "arguments": json.dumps(c["arguments"])}}
            for c in (chunk(choices=[{"index": 0, "delta": {"role": "assistant",
                                                            "tool_calls": [call]}}]),
                      chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                            usage=USAGE)):
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self.text("ok")

    def text(self, content: str):
        for c in (chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": content}}]),
                  chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage=USAGE)):
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
