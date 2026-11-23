"""A fake model server, OpenAI-compatible and Anthropic: it records every request and answers "ok".

Run: capture_llm.py <port> <out.jsonl> [<tool call or list of calls, as JSON>] [<prompt tokens>].
It shows exactly what OpenCode sends a model: the system prompt, the instructions it loaded, the
skills and tools it offered. Given tool calls ({"name": ..., "arguments": {...}}), each request that offers tools
gets the next call as its answer, so a test can see what OpenCode does with them (the request
after a call carries the tool's result). Given prompt tokens, every answer says it read that many,
so a test can fill the context window without sending it (OpenCode then compacts: a request
carrying its summary template gets the template's headings back, which it takes as a summary).
With CAPTURE_HEADER_DELAY=<seconds>, every POST waits that long before its headers, as a slow
reader's server does until its first token (#491).

A POST to a path ending in `/messages` (any query) is answered as Anthropic's Messages API (its
SSE events, `tool_use` blocks, `usage` with input/output and cache tokens); every other POST as
OpenAI's chat completions. The API providers OpenCode ships with post there: DeepInfra to
`<baseURL>/openai/chat/completions` with `Authorization: Bearer`, Anthropic to
`<baseURL>/messages?beta=true` with `x-api-key` (the spike for #1009, OpenCode 2.3.2). The out file
holds request bodies only, one per line, as it always has; CAPTURE_HEADERS=<file> also records
every request's `{method, path, headers}` there (GETs too; header names lower-cased, values as
sent: only test keys ever reach it). Modes, for the API routes (#1011), by environment:
  CAPTURE_STATUS=<code>   answer with that HTTP status and an error body, in the protocol's shape
                          (OpenAI `{"error": {message, type, code}}`, Anthropic `{"type": "error",
                          "error": {type, message}}`): 429 a rate limit, 401 a key refused, 402
                          out of credit. CAPTURE_STATUS_TIMES=<n>: only the first n requests (then
                          it answers as usual: a limit that lifts); unset, every request.
  CAPTURE_RETRY_AFTER=<s> with a status, a `Retry-After` header (OpenCode waits at least that).
  CAPTURE_ERROR_BODY=<text>  the error's message instead of "capture status <code>" (a key-like
                          string, to see it come back through OpenCode verbatim).
  CAPTURE_USAGE=<json>    the usage every answer reports, in OpenAI's shape (`prompt_tokens`,
                          `completion_tokens`, `prompt_tokens_details.cached_tokens`,
                          `completion_tokens_details.reasoning_tokens`); an Anthropic answer
                          reports the same numbers as `input_tokens` (less the cached),
                          `cache_read_input_tokens`, `output_tokens`. Unset: <prompt tokens>
                          read, 1 written, nothing cached.
  CAPTURE_REASONING=<s>   stream reasoning for that many seconds (a delta every 0.25 s, at least
                          one) before the answer: OpenAI `reasoning_content` deltas, Anthropic a
                          `thinking` block. A reasoning model's silence, as `run --format json`
                          prints none of it until the step ends.

It answers one request at a time, as a model server with one slot does, but accepts every
connection at once, as a real server's accept loop does. It listens at once too: HTTPServer's
own server_bind looks the address's name up (socket.getfqdn) between bind and listen, and on
GitHub's macOS runners that took 35 s, while the kernel dropped every SYN to the bound, not yet
listening port: OpenCode's first request of a round failed to connect (`ETIMEDOUT: Unable to
connect`) three times and the round started 35 s late, whatever the header delay (#950,
2026-11-21).
"""
import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = sys.argv[2]
CALLS = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []
CALLS = CALLS if isinstance(CALLS, list) else [CALLS]
made = []
READ = int(sys.argv[4]) if len(sys.argv) > 4 else 1
DELAY = float(os.environ.get("CAPTURE_HEADER_DELAY") or 0)
HEADERS = os.environ.get("CAPTURE_HEADERS")
STATUS = int(os.environ.get("CAPTURE_STATUS") or 0)
STATUS_TIMES = int(os.environ.get("CAPTURE_STATUS_TIMES") or 0)  # 0: every request
RETRY_AFTER = os.environ.get("CAPTURE_RETRY_AFTER")
ERROR_BODY = os.environ.get("CAPTURE_ERROR_BODY")
REASONING = float(os.environ.get("CAPTURE_REASONING") or 0)
USAGE = json.loads(os.environ["CAPTURE_USAGE"]) if os.environ.get("CAPTURE_USAGE") else \
    {"prompt_tokens": READ, "completion_tokens": 1, "total_tokens": READ + 1}
failed = []
SUMMARY = "\n\n".join(f"{h}\n- (none)" for h in (
    "## Objective", "## Requirements", "## Decisions", "## Work State\n### Completed",
    "### Active", "### Blocked", "## Next Move", "## Relevant Files", "## Important Context"))
SLOT = threading.Lock()  # one request at a time
ANTHROPIC_ERRORS = {400: "invalid_request_error", 401: "authentication_error", 402: "billing_error",
                    403: "permission_error", 404: "not_found_error", 429: "rate_limit_error",
                    529: "overloaded_error"}


def chunk(**fields):
    return {"id": "cap", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "cap", **fields}


def anthropic_usage() -> dict:
    """USAGE in Anthropic's terms: its input_tokens leaves out what the cache served."""
    cached = (USAGE.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {"input_tokens": max(0, USAGE.get("prompt_tokens", 0) - cached),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0,
            "output_tokens": USAGE.get("completion_tokens", 0)}


def reasoning_steps() -> int:
    return max(1, int(REASONING / 0.25)) if REASONING else 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):  # a request the client gave up on
            pass

    def seen(self):
        if HEADERS:
            with open(HEADERS, "a") as f:
                f.write(json.dumps({"method": self.command, "path": self.path, "headers": {
                    k.lower(): v for k, v in self.headers.items()}}) + "\n")

    def reply(self, body: dict, code: int = 200, extra: dict | None = None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.seen()
        self.reply({"object": "list", "data": [{"id": "cap", "object": "model"}]})

    def do_POST(self):
        self.seen()
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with SLOT:
            self.answer(req)

    @property
    def anthropic(self) -> bool:
        return self.path.split("?")[0].endswith("/messages")

    def answer(self, req: dict):
        with open(OUT, "a") as f:
            f.write(json.dumps(req) + "\n")
        time.sleep(DELAY)
        if STATUS and (not STATUS_TIMES or len(failed) < STATUS_TIMES):
            failed.append(1)
            msg = ERROR_BODY or f"capture status {STATUS}"
            body = {"type": "error", "error": {"type": ANTHROPIC_ERRORS.get(STATUS, "api_error"),
                                               "message": msg}} if self.anthropic else \
                {"error": {"message": msg, "type": "capture", "code": STATUS}}
            self.reply(body, STATUS, {"Retry-After": RETRY_AFTER} if RETRY_AFTER else None)
            return
        asked = json.dumps(req.get("messages", [])[-2:])
        if "<template>" in asked and "## Objective" in asked:  # OpenCode compacting the session
            call, text = None, SUMMARY
        elif req.get("tools") and len(made) < len(CALLS):
            call, text = CALLS[len(made)], None
            made.append(1)
        else:
            call, text = None, "ok"
        if self.anthropic:
            self.messages(req, call, text)
        else:
            self.completions(req, call, text)

    def completions(self, req: dict, call, text):
        if not req.get("stream"):
            message = {"role": "assistant", "content": text}
            if call:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call_{len(made)}", "type": "function", "function": {
                        "name": call["name"], "arguments": json.dumps(call["arguments"])}}]}
            self.reply({"id": "cap", "object": "chat.completion", "created": int(time.time()),
                        "model": "cap", "usage": USAGE, "choices": [{
                            "index": 0, "finish_reason": "tool_calls" if call else "stop",
                            "message": message}]})
            return
        self.stream_head()
        role = {"role": "assistant"}  # on the first delta, as before these modes
        for i in range(reasoning_steps()):
            self.sse(chunk(choices=[{"index": 0, "delta": {**role, "reasoning_content": f"Thinking {i}. "}}]))
            role = {}
            time.sleep(0.25)
        if call:
            self.sse(chunk(choices=[{"index": 0, "delta": {**role, "tool_calls": [{
                "index": 0, "id": f"call_{len(made)}", "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"])}}]}}]))
            self.sse(chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                           usage=USAGE))
        else:
            self.sse(chunk(choices=[{"index": 0, "delta": {**role, "content": text}}]))
            self.sse(chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage=USAGE))
        self.wfile.write(b"data: [DONE]\n\n")

    def messages(self, req: dict, call, text):
        usage = anthropic_usage()
        if call:
            blocks = [{"type": "tool_use", "id": f"toolu_{len(made)}", "name": call["name"],
                       "input": call["arguments"]}]
        else:
            blocks = [{"type": "text", "text": text}]
        stop = "tool_use" if call else "end_turn"
        if not req.get("stream"):
            self.reply({"id": "msg_cap", "type": "message", "role": "assistant", "model": "cap",
                        "content": blocks, "stop_reason": stop, "stop_sequence": None,
                        "usage": usage})
            return
        self.stream_head()
        self.event("message_start", {"type": "message_start", "message": {
            "id": "msg_cap", "type": "message", "role": "assistant", "model": "cap", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}})
        index = 0
        if reasoning_steps():
            self.event("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "thinking", "thinking": ""}})
            for i in range(reasoning_steps()):
                self.event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "thinking_delta",
                                                             "thinking": f"Thinking {i}. "}})
                time.sleep(0.25)
            self.event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "signature_delta",
                                                         "signature": "capture"}})
            self.event("content_block_stop", {"type": "content_block_stop", "index": 0})
            index = 1
        block = blocks[0]
        if call:
            self.event("content_block_start", {"type": "content_block_start", "index": index,
                                               "content_block": {**block, "input": {}}})
            self.event("content_block_delta", {"type": "content_block_delta", "index": index,
                                               "delta": {"type": "input_json_delta",
                                                         "partial_json": json.dumps(block["input"])}})
        else:
            self.event("content_block_start", {"type": "content_block_start", "index": index,
                                               "content_block": {"type": "text", "text": ""}})
            self.event("content_block_delta", {"type": "content_block_delta", "index": index,
                                               "delta": {"type": "text_delta", "text": text}})
        self.event("content_block_stop", {"type": "content_block_stop", "index": index})
        self.event("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": usage["output_tokens"]}})
        self.event("message_stop", {"type": "message_stop"})

    def stream_head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def sse(self, obj: dict):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    def event(self, name: str, obj: dict):
        self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # not HTTPServer's, which looks the host's name up before listen (the docstring, #950):
        # nothing reads server_name, as in chat.py's _Server (#236)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


if __name__ == "__main__":
    Server(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
