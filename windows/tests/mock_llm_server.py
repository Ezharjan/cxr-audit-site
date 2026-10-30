"""MOCK OpenAI-compatible LLM server for automated application-flow tests ONLY.

It returns deterministic keyword-based answers and identifies itself with
``owned_by: primer-mock-llm`` so every script labels results as MOCK. Passing
tests against this server says nothing about real model quality or about
Ollama compatibility on the user's machine.

Modes:
  structured  honour response_format JSON schema (like recent Ollama/vLLM)
  qwen-think  ignore the schema and reply "<think>..</think>" + fenced JSON,
              like a reasoning model whose thinking leaks into content
"""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LUNIT = ["atelectasis_llm", "calcification_llm", "cardiomegaly_llm", "consolidation_llm", "fibrosis_llm",
         "mediastinal_widening_llm", "nodule_llm", "pleural_effusion_llm", "pneumoperitoneum_llm",
         "pneumothorax_llm", "tb"]
KEYWORDS = {"consolidation_llm": "consolidation", "pleural_effusion_llm": "effusion", "nodule_llm": "nodule",
            "cardiomegaly_llm": "cardiomegaly", "pneumothorax_llm": "pneumothorax", "atelectasis_llm": "atelectasis"}


def report_text(messages):
    text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
    # Prompts mention "<report> tags" before the real block, so use the last block.
    lower = text.lower()
    start = lower.rfind("<report>")
    end = lower.find("</report>", start)
    return lower[start + 8:end] if start >= 0 and end > start else lower


def answer(messages, schema_props):
    text = report_text(messages)
    if schema_props is None:
        schema_props = LUNIT if "_llm" in " ".join(m.get("content", "") for m in messages) else ["grade"]
    if "grade" in schema_props:
        negative = re.search(r"\bno (pleural effusion|consolidation|pneumothorax)", text)
        grade = 4 if "pneumothorax" in text and not negative else 3 if "consolidation" in text or "nodule" in text else 1 if "clear" in text else 2
        return {"grade": grade}
    def present(word):
        return word in text and not re.search(r"\bno (?:\w+ ){0,2}" + word, text)
    return {key: bool(KEYWORDS.get(key) and present(KEYWORDS[key])) for key in LUNIT}


class Handler(BaseHTTPRequestHandler):
    mode = "structured"
    model = "qwen3:4b"
    calls = 0

    def log_message(self, *args):
        pass

    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            return self.send(200, {"object": "list", "data": [{"id": self.model, "object": "model", "owned_by": "primer-mock-llm"}]})
        if self.path == "/api/version":
            return self.send(200, {"version": "mock"})
        if self.path == "/api/ps":
            return self.send(200, {"models": []})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self.send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        Handler.calls += 1
        if body.get("model") != self.model:
            return self.send(404, {"error": {"message": f"model '{body.get('model')}' not found"}})
        fmt = body.get("response_format") or {}
        props = None
        if fmt.get("type") == "json_schema":
            props = list(fmt["json_schema"]["schema"].get("properties", {}))
        if "Please fix this JSON" in json.dumps(body.get("messages")):
            content = json.dumps(answer(body["messages"], None))
        else:
            result = json.dumps(answer(body["messages"], props))
            content = result if self.mode == "structured" else f"<think>Reviewing the synthetic report.</think>\n```json\n{result}\n```"
        self.send(200, {"id": "mock", "object": "chat.completion", "created": 0, "model": self.model,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": content}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def serve(port=11434, mode="structured", model="qwen3:4b"):
    Handler.mode, Handler.model = mode, model
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--mode", choices=("structured", "qwen-think"), default="structured")
    args = parser.parse_args()
    print(f"MOCK LLM on 127.0.0.1:{args.port} mode={args.mode} (not real inference)", flush=True)
    serve(args.port, args.mode).serve_forever()
