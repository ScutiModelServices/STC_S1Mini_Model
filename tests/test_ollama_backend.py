"""Transport contracts against a real HTTP server; no model weights or tokenizer downloads."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import create_app
from engine import EngineError, S1MiniEngine, messages_for
from ollama_backend import OllamaLoaded, S1Tokenizer, engine_from_env
from options import SETTINGS
from settings import Config


class Tokenizer:
    def __call__(self, text, **kwargs):
        return {"input_ids": text.split()}

    def apply_chat_template(self, messages, **kwargs):
        assert kwargs == {"tokenize": False, "add_generation_prompt": True, "enable_thinking": False}
        return json.dumps(messages)


@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_json({"models": [{"name": "m:latest", "digest": "abc"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            prompt = body.get("prompt", "")
            if "http-error" in prompt:
                self.send_json({"error": "invalid model"}, 400)
                return
            if "broken-chunk" in prompt:
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"20\r\nshort\r\n")
                self.close_connection = True
                return
            self.send_response(200)
            self.end_headers()
            if self.path == "/api/pull":
                pieces = [{"status": "success"}]
            elif not prompt:
                pieces = [{"done": True}]
            elif "bad-json" in prompt:
                self.wfile.write(b"not json\n")
                return
            elif "truncated" in prompt:
                pieces = [{"response": "Partial. "}]
            elif "stream-error" in prompt:
                pieces = [{"error": "runner crashed"}]
            else:
                pieces = [{"response": "Hello. "}, {"response": "Bye."},
                          {"done": True, "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 4}]
            for piece in pieces:
                self.wfile.write(json.dumps(piece).encode() + b"\n")
                self.wfile.flush()

        def send_json(self, body, status=200):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{http.server_port}", seen
    http.shutdown()
    http.server_close()
    worker.join()


def loaded(url, **kwargs):
    return OllamaLoaded(url, "m", Tokenizer(), **kwargs)


def test_exact_prompt_greedy_options_and_counts(server):
    url, seen = server
    model = loaded(url)
    assert model.count("one two three") == 3
    messages = messages_for("um hello", "formal", "lists", "email")
    stop = threading.Event()
    events = list(model.stream(messages, 12, 5, stop))
    assert events == [("content", "Hello. "), ("content", "Bye."),
                      ("done", {"prompt_tokens": 7, "completion_tokens": 4, "finish": "stop"})]
    assert not stop.is_set()
    assert seen[-1]["prompt"] == json.dumps(messages)
    assert seen[-1]["raw"] is True
    assert seen[-1]["options"] == {"temperature": 0, "num_predict": 12, "repeat_penalty": 1.0,
                                   "num_ctx": 4096, "num_gpu": 0}
    assert "num_thread" not in seen[-1]["options"]


def test_chunk_completion_never_sets_callers_stop_and_close_stops_only_current_chunk(server):
    model = loaded(server[0])
    stop = threading.Event()
    for _ in range(2):
        events = model.stream(messages_for("hi", "casual", "prose", "general"), 12, 5, stop)
        assert next(events) == ("content", "Hello. ")
        events.close()
        assert not stop.is_set()
    stop.set()
    before = len(server[1])
    assert list(model.stream([], 12, 5, stop)) == []
    assert len(server[1]) == before


def test_long_single_sentence_gets_context_for_both_prompt_and_output(server):
    model = loaded(server[0])
    messages = messages_for("word " * 5000, "casual", "prose", "general")
    list(model.stream(messages, 6500, 5, threading.Event()))
    body = server[1][-1]
    assert body["options"]["num_ctx"] == model.count(body["prompt"]) + 6500


@pytest.mark.parametrize("text,status", [("http-error", 400), ("stream-error", 503),
                                        ("bad-json", 503), ("truncated", 503), ("broken-chunk", 503)])
def test_transport_failures_are_engine_errors(server, text, status):
    with pytest.raises(EngineError) as error:
        list(loaded(server[0]).stream(messages_for(text, "casual", "prose", "general"), 12, 5, threading.Event()))
    assert error.value.status == status


def test_unreachable_server_is_engine_error():
    with pytest.raises(EngineError):
        loaded("http://127.0.0.1:1", load_timeout=0.1)


def test_warmup_body_read_failure_is_engine_error(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            raise TimeoutError("stalled warmup")

    monkeypatch.setattr("ollama_backend._call", lambda *args: Response())
    with pytest.raises(EngineError, match="Ollama response failed"):
        loaded("http://unused")


def test_saved_backend_settings_win_over_environment(server, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SERVICE_OLLAMA_MODEL", "wrong")
    config = Config(tmp_path / "test-config.json", SETTINGS)
    config.set({"ollama_url": server[0], "ollama_model": "m"})
    monkeypatch.setattr("ollama_backend.load_tokenizer", lambda source: Tokenizer())
    engine = engine_from_env(config=config)
    assert engine.model == "m"
    assert asyncio.run(engine.normalize("hi", "casual", "prose", "general"))["content"] == "Hello. Bye."
    config.set({"ollama_url": ""})
    with pytest.raises(ValueError, match="SERVICE_OLLAMA_URL.*required"):
        engine_from_env(config=config)


def test_engine_wiring_multichunk_stream_cache_422_and_updates(server, monkeypatch):
    url, seen = server
    monkeypatch.delenv("SERVICE_OLLAMA_URL", raising=False)
    assert isinstance(engine_from_env(), S1MiniEngine)
    monkeypatch.setenv("SERVICE_OLLAMA_URL", url)
    monkeypatch.setenv("SERVICE_OLLAMA_MODEL", "m")
    monkeypatch.setattr("ollama_backend.load_tokenizer", lambda source: Tokenizer())
    engine = engine_from_env(chunk_tokens=2)
    assert isinstance(engine, S1MiniEngine)
    engine.num_thread = 2
    app = TestClient(create_app(engine, cache_size=4))
    body = {"text": "one two. three four."}
    first = app.post("/v1/normalize", json=body)
    assert first.status_code == 200
    assert first.json()["content"] == "Hello. Bye. Hello. Bye."
    assert first.json()["usage"] == {"input_tokens": 14, "output_tokens": 8}
    before = len(seen)
    second = app.post("/v1/normalize", json={**body, "stream": True})
    assert second.headers["X-Cache"] == "hit" and len(seen) == before
    assert sum(json.loads(line)["type"] == "sentence" for line in second.text.splitlines()) >= 2
    assert app.post("/v1/normalize", json={**body, "temperature": 0}).status_code == 422
    assert asyncio.run(engine.cached_sha()) == "abc"
    assert asyncio.run(engine.remote_sha()) is None
    monkeypatch.setattr("huggingface_hub.hf_hub_download", lambda *args, **kwargs: "tokenizer.json")
    asyncio.run(engine.pull())
    assert seen[-1] == {"model": "m", "stream": True}

def test_url_default_is_local_and_empty_is_error(monkeypatch):
    monkeypatch.delenv("SERVICE_OLLAMA_URL", raising=False)
    assert isinstance(engine_from_env(), S1MiniEngine)
    monkeypatch.setenv("SERVICE_OLLAMA_URL", "  ")
    with pytest.raises(ValueError, match="SERVICE_OLLAMA_URL.*required"):
        engine_from_env()


@pytest.mark.parametrize("stream", [False, True])
def test_unreachable_ollama_keeps_service_up_and_returns_503(monkeypatch, stream):
    monkeypatch.setenv("SERVICE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SERVICE_OLLAMA_MODEL", "m")
    monkeypatch.setattr("ollama_backend.load_tokenizer", lambda source: Tokenizer())
    with TestClient(create_app(warm=True)) as client:
        assert client.get("/health").json()["ready"] is False
        assert client.get("/admin/").status_code == 200
        reply = client.post("/v1/normalize", json={"text": "hello", "stream": stream})
        assert reply.status_code == 503
        assert "press Download on the Updates card" in reply.json()["detail"]
        assert "127.0.0.1:1" not in reply.text


def test_removed_settings_are_unknown(tmp_path):
    config = Config(tmp_path / "test-config.json", SETTINGS)
    names = {row["key"] for row in config.describe()}
    for removed in ("model", "d" + "type", "max_" + "project_" + "models", "project_" + "models_dir"):
        assert removed not in names
        with pytest.raises(ValueError, match="Unknown setting"):
            config.set({removed: "old"})


def test_pinned_tokenizer_preserves_counts_and_nonthinking_prompt():
    from tokenizers import Tokenizer as Vocabulary
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    vocabulary = Vocabulary(WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
    vocabulary.pre_tokenizer = Whitespace()
    tokenizer = S1Tokenizer(vocabulary)
    assert tokenizer("hello world")["input_ids"] == [1, 2]
    messages = messages_for("hello world", "formal", "prose", "general")
    assert tokenizer.apply_chat_template(messages) == (
        f"<|im_start|>system\n{messages[0]['content']}<|im_end|>\n"
        "<|im_start|>user\n[Styling: formal] [Structure: prose] [Context: general]\n"
        "hello world<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


def test_missing_model_returns_503_and_recovers_after_model_is_available(server, monkeypatch):
    import ollama_backend

    real_call = ollama_backend._call
    missing = [True]

    def call(url, path, body, timeout):
        if missing[0]:
            raise EngineError("Ollama is unavailable", 503)
        return real_call(url, path, body, timeout)

    monkeypatch.setenv("SERVICE_OLLAMA_URL", server[0])
    monkeypatch.setenv("SERVICE_OLLAMA_MODEL", "m")
    monkeypatch.setattr(ollama_backend, "_call", call)
    monkeypatch.setattr(ollama_backend, "load_tokenizer", lambda source: Tokenizer())
    with TestClient(create_app(warm=True)) as client:
        reply = client.post("/v1/normalize", json={"text": "hello"})
        assert reply.status_code == 503
        assert "press Download on the Updates card" in reply.json()["detail"]
        assert client.get("/health").json()["ready"] is False
        missing[0] = False
        assert client.post("/v1/normalize", json={"text": "hello"}).status_code == 200
        assert client.get("/health").json()["ready"] is True


def test_removed_request_field_and_settings_return_422(tmp_path):
    from conftest import make_engine

    config = Config(tmp_path / "test-config.json", SETTINGS)
    client = TestClient(create_app(make_engine(), admin_password="admin", config=config))
    headers = {"X-Internal-Token": "admin"}
    assert client.put("/admin/settings", headers=headers, json={"values": {"model": "old"}}).status_code == 422
    public = TestClient(create_app(make_engine()))
    assert public.post("/v1/normalize", json={"text": "hello", "project": "old"}).status_code == 422


def test_explicit_download_pulls_even_when_local_digest_is_unchanged(server, monkeypatch):
    monkeypatch.setenv("SERVICE_OLLAMA_URL", server[0])
    monkeypatch.setenv("SERVICE_OLLAMA_MODEL", "m")
    monkeypatch.setattr("ollama_backend.load_tokenizer", lambda source: Tokenizer())
    engine = engine_from_env()
    asyncio.run(engine.warm())
    client = TestClient(create_app(engine, admin_password="admin"))
    check = client.post("/admin/updates/check", headers={"X-Internal-Token": "admin"})
    assert check.json()["weights"][0]["latest"] is None
    monkeypatch.setattr("huggingface_hub.hf_hub_download", lambda *args, **kwargs: "tokenizer.json")
    reply = client.post("/admin/updates/weights", headers={"X-Internal-Token": "admin"})
    assert reply.status_code == 200
    assert {"model": "m", "stream": True} in server[1]
    assert engine.ready is False and engine.generation == 1


def test_parallel_comes_from_service_ollama_parallel(monkeypatch):
    monkeypatch.setenv("SERVICE_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.delenv("SERVICE_OLLAMA_PARALLEL", raising=False)
    assert engine_from_env().parallel == 2
    monkeypatch.setenv("SERVICE_OLLAMA_PARALLEL", "4")
    assert engine_from_env().parallel == 4
    monkeypatch.setenv("SERVICE_OLLAMA_PARALLEL", "0")
    with pytest.raises(ValueError):
        engine_from_env()
