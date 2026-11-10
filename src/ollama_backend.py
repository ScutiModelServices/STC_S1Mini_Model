"""S1-mini through Ollama with a pinned tokenizer and exact non-thinking prompt.

Blocking transport and tokenizer work run only in S1MiniEngine worker threads.
Raw generation preserves the model-card prompt and greedy decoding.
"""
from __future__ import annotations

import json
import http.client
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

from engine import DEFAULT_MODEL, EngineError, S1MiniEngine

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "s1-mini:f16"
TOKENIZER_REVISION = "88f6b15896c73bbb13a3b596e0afe8ea0d5150b4"


def _call(url: str, path: str, body: dict[str, Any] | None, timeout: float):
    request = urllib.request.Request(
        f"{url}{path}", data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 400:
            raise EngineError("Ollama refused the request (400)", 400) from None
        raise EngineError("Ollama is unavailable", 503) from None
    except (OSError, http.client.HTTPException):
        raise EngineError("Ollama is unreachable", 503) from None


def _read_json(response) -> dict[str, Any]:
    """Read failures use the same public engine error contract as connection failures."""
    try:
        return OllamaLoaded._decode(response.read())
    except (OSError, http.client.HTTPException) as error:
        raise EngineError("Ollama response failed", 503) from None


class S1Tokenizer:
    """Pinned Qwen3 vocabulary with the exact two-message non-thinking template."""

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer

    def __call__(self, text: str, **_: Any) -> dict[str, Any]:
        return {"input_ids": self._tokenizer.encode(text, add_special_tokens=False).ids}

    def apply_chat_template(self, messages: list[dict[str, str]], **_: Any) -> str:
        return ("".join(f"<|im_start|>{row['role']}\n{row['content']}<|im_end|>\n" for row in messages)
                + "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def load_tokenizer(source: str) -> S1Tokenizer:
    """Load only the pinned vocabulary already downloaded by an admin."""
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    filename = hf_hub_download(source, "tokenizer.json", revision=TOKENIZER_REVISION, token=False, local_files_only=True)
    return S1Tokenizer(Tokenizer.from_file(filename))


class OllamaLoaded:
    """Loaded.count/stream backed by Ollama's raw /api/generate NDJSON protocol."""

    def __init__(self, url: str, model: str, tokenizer: Any = None, load_timeout: float = 15.0) -> None:
        self.url, self.model = url.rstrip("/"), model
        self.num_thread: int | None = None  # CPU threads Ollama uses per request; None = Ollama decides
        self.tokenizer = tokenizer
        with _call(self.url, "/api/generate", {"model": model, "prompt": "", "stream": False,
                                             "options": self._options()}, load_timeout) as response:
            _read_json(response)
        if self.tokenizer is None:
            self.tokenizer = load_tokenizer(DEFAULT_MODEL)

    def count(self, text: str) -> int:
        return len(self.tokenizer(text, add_special_tokens=False)["input_ids"])

    def _options(self) -> dict[str, Any]:
        options = {"temperature": 0, "repeat_penalty": 1.0, "num_ctx": 4096, "num_gpu": 0}
        if self.num_thread:
            options["num_thread"] = self.num_thread
        return options

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        try:
            piece = json.loads(raw)
            if not isinstance(piece, dict):
                raise ValueError("expected an object")
        except (ValueError, UnicodeError) as error:
            raise EngineError("Ollama returned invalid JSON", 503) from None
        if piece.get("error"):
            raise EngineError("Ollama generation failed", 503)
        return piece

    def stream(self, messages: list[dict[str, str]], max_new_tokens: int, max_time: float,
               stop: threading.Event) -> Iterator[tuple[str, Any]]:
        """Close only this chunk's response on exit; never mutate caller's stop event."""
        if stop.is_set():
            return
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        # Reserve room for this exact prompt plus its output; do not truncate an oversized sentence.
        context = max(4096, self.count(prompt) + max_new_tokens)
        body = {"model": self.model, "prompt": prompt, "raw": True, "stream": True,
                "options": {**self._options(), "num_ctx": context, "num_predict": max_new_tokens}}
        deadline = time.monotonic() + max_time
        try:
            with _call(self.url, "/api/generate", body, max_time) as response:
                for raw in response:
                    if stop.is_set():
                        return
                    if time.monotonic() >= deadline:
                        raise EngineError("Ollama generation timed out", 503)
                    piece = self._decode(raw)
                    if text := piece.get("response"):
                        yield "content", text
                    if piece.get("done"):
                        yield "done", {"prompt_tokens": piece.get("prompt_eval_count", 0),
                                       "completion_tokens": piece.get("eval_count", 0),
                                       "finish": "length" if piece.get("done_reason") == "length" else "stop"}
                        return
                if not stop.is_set():
                    raise EngineError("Ollama stream ended before done", 503)
        except (OSError, http.client.HTTPException) as error:
            raise EngineError("Ollama stream failed", 503) from None



def _parallel() -> int:
    """Generations one replica runs at once: SERVICE_OLLAMA_PARALLEL (default 2), the OLLAMA_NUM_PARALLEL of your Ollama."""
    raw = os.environ.get("SERVICE_OLLAMA_PARALLEL", "").strip() or "2"
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError("SERVICE_OLLAMA_PARALLEL must be a whole number of 1 or more")
    return int(raw)

def engine_from_env(*, config: Any = None, **options: Any) -> S1MiniEngine:
    """Always use Ollama; saved startup settings win over environment values."""
    def value(name: str, default: str) -> str:
        if config is not None:
            # Config ignores blank environment values. Detect explicit blanks here instead.
            row = next(row for row in config.describe() if row["key"] == name)
            if row["source"] != "saved" and f"SERVICE_{name.upper()}" in os.environ:
                return os.environ[f"SERVICE_{name.upper()}"].strip()
            return str(row["value"]).strip()
        return os.environ.get(f"SERVICE_{name.upper()}", default).strip()

    url = value("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
    if not url:
        raise ValueError("SERVICE_OLLAMA_URL is required and cannot be empty")
    model = value("ollama_model", DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL

    def local_digest(_: str) -> str | None:
        name = model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
        with _call(url, "/api/tags", None, 15) as response:
            reply = _read_json(response)
        return next((row["digest"] for row in reply.get("models", []) if row["name"] == name), None)

    def pull(_: str) -> None:
        from huggingface_hub import hf_hub_download

        hf_hub_download(DEFAULT_MODEL, "tokenizer.json", revision=TOKENIZER_REVISION, token=False, force_download=True)
        # Local GGUF imports have no registry counterpart; the operator imports them.
        if model == DEFAULT_OLLAMA_MODEL:
            return
        from progress import ollama_pull_progress

        with _call(url, "/api/pull", {"model": model, "stream": True}, 3600) as response:
            status = ollama_pull_progress(response, model)
        if status != "success":
            raise EngineError("Ollama did not finish pulling the model", 503)

    def loader(source: str) -> OllamaLoaded:
        try:
            return OllamaLoaded(url, source)
        except EngineError as error:
            if error.status == 400:
                raise
            raise EngineError("Model unavailable; start Ollama and ask an admin to press Download on the Updates card.", 503) from None
        except Exception:
            raise EngineError("Tokenizer unavailable; ask an admin to press Download on the Updates card.", 503) from None

    parallel = _parallel()
    engine = S1MiniEngine(model, loader=loader, parallel=parallel, cached_sha=local_digest,
                         remote_sha=lambda source: None, download=pull, **options)
    return engine
