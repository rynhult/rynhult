#!/usr/bin/env python3
"""A scripted OpenAI-compatible chat completions server, so a real agent CLI can run without a model account (tests).

    fake_llm.py <script.json> <port file> <log file>
script.json is a list of replies, one per request (the last one repeats): {"tool": name, "args": {...}} asks for a tool
call, {"text": "..."} answers. Title requests (no tools offered) get a title and don't use up a reply. It listens on
127.0.0.1 on a port the kernel picks, written to the port file; each request is logged as one JSON line.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

script = json.load(open(sys.argv[1]))
log = open(sys.argv[3], "a")
count = {"n": 0}
lock = threading.Lock()


def chunk(delta: dict, finish: str | None = None) -> dict:
    return {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # /v1/models
        body = json.dumps({"object": "list", "data": [{"id": "m", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if request.get("tools"):
            with lock:
                n = count["n"]
                count["n"] += 1
            reply = script[min(n, len(script) - 1)]
        else:
            n, reply = -1, {"text": "Fake title"}
        log.write(json.dumps({"n": n, "tools": [t.get("function", {}).get("name") for t in request.get("tools", [])],
                              "system": [m.get("content") for m in request.get("messages", []) if m.get("role") == "system"],
                              "last": request.get("messages", [])[-3:]}) + "\n")
        log.flush()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [chunk({"role": "assistant"})]
        if "tool" in reply:
            events += [chunk({"tool_calls": [{"index": 0, "id": f"call_{n}", "type": "function",
                                              "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}]}),
                       chunk({}, "tool_calls")]
        else:
            events += [chunk({"content": reply["text"]}), chunk({}, "stop")]
        events.append({"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[2], "w") as handle:
    handle.write(str(server.server_address[1]))
server.serve_forever()
