"""S1-mini (a 0.6B Qwen3 text normalizer for speech-to-text output) served through Ollama.

The model is not a chat model: every call sends the fixed system prompt and one control line, then the transcript,
and decodes greedily with thinking switched off in the chat template (without that the model emits an empty
<think> block and stops). Long transcripts are cut at sentence ends into chunks of at most `chunk_tokens` tokens,
each normalized on its own. Output is handed out sentence by sentence, not token by token.

One generation runs at a time per replica; Ollama owns model memory and CPU scheduling.

shortcut: a single sentence longer than `chunk_tokens` is sent whole; an empty result (filler-only input) is valid."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from sentences import SentenceBuffer, chunks

DEFAULT_MODEL = "superwhisper/s1-mini"

# Required by the model card, word for word; any change makes the model hallucinate.
SYSTEM_PROMPT = (
    "You are a text normalizer for speech-to-text transcripts. The input begins "
    "with a control line specifying the styling, structure, and context settings; "
    "clean the transcript to match those settings and output only the cleaned text."
)
STYLINGS = ("casual", "semi-casual", "semi-formal", "formal")
STRUCTURES = ("prose", "lists")
CONTEXTS = ("general", "email")


class EngineError(RuntimeError):
    """The model could not answer. `status` is the HTTP status the service maps it to."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def messages_for(text: str, styling: str, structure: str, context: str) -> list[dict[str, str]]:
    control = f"[Styling: {styling}] [Structure: {structure}] [Context: {context}]"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"{control}\n{text}"}]


def default_marker() -> Path:
    """File in the shared data volume whose content changes when one replica downloads new weights."""
    configured = os.environ.get("SERVICE_WEIGHTS_MARKER")
    return Path(configured) if configured else Path(os.environ.get("SERVICE_TOKENS_FILE", "tokens.json")).resolve().parent / ".s1mini-weights-version"


class S1MiniEngine:
    """Ollama loader and digest/download callables are injectable for tests."""

    def __init__(self, model: str = DEFAULT_MODEL, *, chunk_tokens: int = 800, timeout: float = 120.0, parallel: int = 1,
                 marker: Path | None = None, loader: Callable[[str], Any] | None = None,
                 cached_sha: Callable[[str], str | None] | None = None,
                 remote_sha: Callable[[str], str] | None = None,
                 download: Callable[[str], None] | None = None) -> None:
        self.model = model
        self.chunk_tokens = chunk_tokens
        self._threads: int | None = None
        self._timeout = timeout
        self._marker = marker
        self._seen = self._read_marker()
        if loader is None:
            raise ValueError("An Ollama loader is required; use engine_from_env()")
        self._loader = loader
        self._remote_sha_of, self._cached_sha_of, self._download_of = remote_sha, cached_sha, download
        self._base: Any | None = None
        self._load_lock = threading.Lock()
        self.parallel = max(1, int(parallel))  # generations one replica runs at once; match OLLAMA_NUM_PARALLEL
        self._gate = asyncio.Semaphore(self.parallel)
        self.generation = 0  # part of the answer cache key; changes whenever the loaded model is dropped

    @property
    def num_thread(self) -> int | None:
        return self._threads

    @num_thread.setter
    def num_thread(self, count: int) -> None:
        self._threads = count  # Display and persistence only; Ollama chooses its own threads.

    @property
    def ready(self) -> bool:
        return self._base is not None

    async def close(self) -> None:
        self._base = None

    async def warm(self) -> None:
        """Load the model now so the first request does not pay for it."""
        await asyncio.to_thread(self._base_model)

    def _base_model(self) -> Any:
        with self._load_lock:
            if self._base is None:
                self._base = self._loader(self.model)
            return self._base

    def _events(self, text: str, styling: str, structure: str, context: str, stop: threading.Event) -> Iterator[tuple[str, Any]]:
        """Invalidate unavailable handles and return an actionable public error."""
        try:
            yield from self._generate_events(text, styling, structure, context, stop)
        except EngineError as error:
            if error.status == 503:
                self._forget()
                raise EngineError("Model unavailable; start Ollama and ask an admin to press Download on the Updates card.", 503) from None
            raise

    def _generate_events(self, text: str, styling: str, structure: str, context: str, stop: threading.Event) -> Iterator[tuple[str, Any]]:
        """("sentence", text with the whitespace after it) as sentences complete, then ("done", stats)."""
        loaded = self._base_model()
        parts = chunks(text, loaded.count, self.chunk_tokens)
        gap = "\n" if structure == "lists" else " "
        prompt = completion = 0
        finish = "stop"
        for number, part in enumerate(parts):
            window = int(1.3 * loaded.count(part) + 32)  # output length tracks input length (model card)
            buffer = SentenceBuffer()
            for kind, value in loaded.stream(messages_for(part.strip(), styling, structure, context), window, self._timeout, stop):
                if kind == "content":
                    for sentence in buffer.feed(value):
                        yield "sentence", sentence
                else:
                    prompt += value["prompt_tokens"]
                    completion += value["completion_tokens"]
                    if value["finish"] == "length":
                        finish = "length"
            tail = buffer.flush()
            if tail.strip():  # the last sentence has no whitespace after it yet
                yield "sentence", tail + (gap if number < len(parts) - 1 else "")
        yield "done", {"prompt_tokens": prompt, "completion_tokens": completion, "finish": finish}

    async def normalize(self, text: str, styling: str, structure: str, context: str) -> dict[str, Any]:
        stop = threading.Event()
        async with self._gate:
            def run() -> dict[str, Any]:
                events = list(self._events(text, styling, structure, context, stop))
                return {**events[-1][1], "content": "".join(v for k, v in events if k == "sentence").strip()}

            return await asyncio.to_thread(run)

    async def normalize_stream(self, text: str, styling: str, structure: str, context: str) -> AsyncIterator[tuple[str, Any]]:
        """("sentence", text) pieces, then ("done", stats). Holds the generation slot until the last piece; when the
        caller closes the iterator the generation is stopped."""
        stop = threading.Event()
        async with self._gate:
            events = self._events(text, styling, structure, context, stop)
            try:
                while (event := await asyncio.to_thread(next, events, None)) is not None:
                    yield event
            finally:
                stop.set()  # a running `next` then ends by itself; closing the generator here could race with it

    # Updates: Ollama digests; data-volume marker invalidates every replica cache.

    async def cached_sha(self) -> str | None:
        return await asyncio.to_thread(self._cached_sha_of, self.model)

    async def remote_sha(self) -> str:
        return await asyncio.to_thread(self._remote_sha_of, self.model)

    async def pull(self) -> None:
        await asyncio.to_thread(self._download_of, self.model)

    async def unload(self) -> None:
        """Invalidate this replica and publish a marker without blocking the event loop."""
        await asyncio.to_thread(self._unload)

    def _unload(self) -> None:
        if self._marker is not None:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            self._seen = uuid.uuid4().hex
            self._marker.write_text(self._seen, "utf-8")
        self._forget()

    def sync(self) -> None:
        """Called every few seconds: drop the loaded model when another replica downloaded new weights."""
        current = self._read_marker()
        if current != self._seen:
            self._seen = current
            self._forget()

    def _forget(self) -> None:
        with self._load_lock:
            self._base = None
            self.generation += 1  # kept answers are from the old weights

    def _read_marker(self) -> str | None:
        try:
            return self._marker.read_text("utf-8") if self._marker else None
        except OSError:
            return None
