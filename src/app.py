"""HTTP face of S1-mini, a speech-to-text transcript normalizer served through Ollama.

POST /v1/normalize {"text", "styling", "structure", "context", "stream"} returns the cleaned text. With
"stream": true the answer arrives as JSON lines, one per sentence. GET /health reports whether the model is loaded."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from disconnect import CallerGone, Measure, first_piece
from cache import AnswerCache
from engine import CONTEXTS, STRUCTURES, STYLINGS, EngineError, S1MiniEngine, default_marker
from ollama_backend import engine_from_env
from ollama_updates import OllamaUpdates
from options import SETTINGS
import settings
from tokens import LatencyHistogram, TokenStore
from sentences import cut
from updates import Updates

_log = logging.getLogger(__name__)

class NormalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # anything else is refused, not silently ignored

    text: str = Field(min_length=1, max_length=200_000)
    styling: Literal[STYLINGS] = "semi-formal"  # type: ignore[valid-type]
    structure: Literal[STRUCTURES] = "prose"  # type: ignore[valid-type]
    context: Literal[CONTEXTS] = "general"  # type: ignore[valid-type]
    stream: bool = False  # true: the answer arrives as JSON lines, one per sentence


class SettingsUpdate(BaseModel):
    values: dict[str, Any]  # None resets an option to its environment variable or default


class NewToken(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    projects: None = None  # Shared editor sends null; project restrictions have no meaning here.
    rate: int | None = Field(None, ge=1, le=100_000)  # calls per minute
    expires_days: int | None = Field(None, ge=1, le=3650)


class TokenScope(BaseModel):
    projects: None = None
    rate: int | None = Field(None, ge=1, le=100_000)


class Threads(BaseModel):
    num_thread: int = Field(ge=1, le=256)


def create_app(client: S1MiniEngine | None = None, admin_password: str | None = None,
               tokens: TokenStore | None = None, warm: bool = False,
               updates: Updates | None = None, check_hours: float = 0, max_inflight: int | None = None,
               cache_size: int | None = None, config: settings.Config | None = None) -> FastAPI:
    """`admin_password` (SERVICE_ADMIN_PASSWORD) unlocks /admin/, where each project gets a token of its own; when it
    is set, every call except /health must send one of those tokens in the X-Internal-Token header. Unset = no
    auth at all (local use only). `tokens` holds the project tokens (SERVICE_TOKENS_FILE). `cache_size` > 0 keeps that
    many answers per replica (SERVICE_CACHE_SIZE). `config` (the admin page's settings) replaces `warm`, `check_hours`,
    `max_inflight` and `cache_size` and keeps them live; without it they stay fixed."""
    tokens = tokens or TokenStore(os.environ.get("SERVICE_TOKENS_FILE", "tokens.json"))
    client = client or engine_from_env(
        config=config, marker=default_marker(), chunk_tokens=config.get("chunk_tokens") if config else 800
    )
    inflight = 0
    cache = AnswerCache()

    def cache_limit() -> int:
        """Answers this replica keeps; 0 = off."""
        return config.get("cache_size") if config else (
            cache_size if cache_size is not None else int(os.environ.get("SERVICE_CACHE_SIZE") or 0))

    def inflight_limit() -> int:
        """Calls this replica lets wait for the model at once; more get 429. 0 = 4."""
        value = config.get("max_inflight") if config else (
            max_inflight if max_inflight is not None else int(os.environ.get("SERVICE_MAX_INFLIGHT") or 0))
        return value or 4 * client.parallel

    updates = updates or OllamaUpdates(client.model, cached_sha=lambda: client.cached_sha(), remote_sha=lambda: client.remote_sha(),
                                 download=lambda: client.pull(), unload=lambda: client.unload(),
                                 description="Checks the local Ollama import without downloading; no online model version to compare. Download fetches the pinned tokenizer; import model weights using the Ollama guide. Registry-backed alternatives also pull on Download.",
                                 reload_note="Weights downloaded. Every replica drops the old copy within a few seconds and loads the new weights on its next request.")

    async def check_forever() -> None:
        """Checks every `update_check_hours` (0 = off). The value is read again every second, so the admin page can change it."""
        last = None
        while True:
            wait = (config.get("update_check_hours") if config else check_hours) * 3600
            now = time.monotonic()
            if wait > 0 and (last is None or now - last >= wait):
                last = now
                try:
                    status = await updates.check()
                    if status["error"]:
                        _log.warning("Update check failed: %s", status["error"])
                except Exception:
                    _log.exception("Update check failed")
            await asyncio.sleep(min(wait, 1.0) if wait > 0 else 1.0)

    async def sync_forever() -> None:
        """Applies what another replica saved (CPU threads, chunk size) and drops the model it replaced with new weights."""
        while True:
            saved = settings.load(tokens.settings_path)
            if saved.get("num_thread") and saved["num_thread"] != client.num_thread:
                client.num_thread = int(saved["num_thread"])
            client.chunk_tokens = config.get("chunk_tokens")
            await asyncio.to_thread(client.sync)
            # A dropped model (download, threads change, sync) leaves /health "Starting" until a request loads it: reload once per drop.
            if config.get("warm") and not client.ready and client.generation != warmed:
                await warm_up()
            await asyncio.sleep(1)

    warmed = 0  # generation of the last warm-up attempt

    async def warm_up() -> None:
        nonlocal warmed
        warmed = client.generation
        try:
            await client.warm()
        except Exception:
            _log.exception("Model warm-up failed; the first request will try again")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        background: list[asyncio.Task] = []
        try:
            if config.get("warm") if config else warm:
                # In the background: Ollama loads the model, and /health must answer meanwhile.
                background.append(asyncio.create_task(warm_up()))
            if config or check_hours > 0:
                background.append(asyncio.create_task(check_forever()))
            if config:
                background.append(asyncio.create_task(sync_forever()))
            yield
        finally:
            for task in background:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await client.close()

    def authorize(request: Request, x_internal_token: str = Header("")) -> None:
        """Checks the token. A token's projects list has no meaning here (one model, no project models). Unset admin password = no auth."""
        if not admin_password:
            return
        row = tokens.lookup(x_internal_token)
        if row is None:
            raise HTTPException(401, "Invalid token")
        wait = tokens.throttle(row)
        if wait:
            raise HTTPException(429, "Too many requests for this token", headers={"Retry-After": str(wait)})
        request.state.token_id = row["id"]

    def admin(x_internal_token: str = Header("")) -> None:
        if not admin_password or not hmac.compare_digest(x_internal_token, admin_password):
            raise HTTPException(401, "Admin password required (set SERVICE_ADMIN_PASSWORD)")

    app = FastAPI(title="S1-mini service", lifespan=lifespan)

    async def slot():
        nonlocal inflight
        limit = inflight_limit()
        if limit and inflight >= limit:
            raise HTTPException(429, "S1-mini is busy: too many requests waiting", headers={"Retry-After": "5"})
        inflight += 1
        try:
            yield
        finally:
            inflight -= 1

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "model": client.model, "ready": client.ready,
                "num_thread": client.num_thread}

    def build(reply: dict[str, Any], started: float) -> dict[str, Any]:
        return {"content": reply["content"], "finish_reason": reply["finish"], "model": client.model,
                "usage": {"input_tokens": reply["prompt_tokens"], "output_tokens": reply["completion_tokens"]},
                "duration_ms": round((time.perf_counter() - started) * 1000)}

    def cache_key(body: NormalizeRequest) -> str:
        """Everything that shapes the answer (decoding is always greedy)."""
        part = [client.generation, client.chunk_tokens, body.text, body.styling, body.structure, body.context]
        return hashlib.sha256(json.dumps(part, sort_keys=True, default=str).encode()).hexdigest()

    def line(event: dict[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=False) + "\n"

    def ndjson(lines: Any, cached: str) -> StreamingResponse:
        return StreamingResponse(lines, media_type="application/x-ndjson", headers={"X-Cache": cached})

    async def replay(result: dict[str, Any]):
        """A kept answer in the shape of a stream: one line per sentence, then `done`."""
        pieces, rest = cut(result["content"])
        for sentence in pieces + ([rest] if rest else []):
            yield line({"type": "sentence", "text": sentence})
        yield line({"type": "done", **result})

    async def live_lines(events: Any, first: tuple[str, Any] | None, started: float, key: str | None, size: int):
        """One JSON line per sentence, then `done` with the same body a call without `stream` returns. A failure
        after the first piece can only be sent as an `error` line."""
        parts: list[str] = []
        stats: dict[str, Any] = {}
        try:
            async def all_events():
                if first is not None:
                    yield first
                async for event in events:
                    yield event

            async for kind, value in all_events():
                if kind == "sentence":
                    parts.append(value)
                    yield line({"type": "sentence", "text": value})
                else:
                    stats = value
        except EngineError as error:
            yield line({"type": "error", "status": 422 if error.status == 400 else (error.status or 500), "message": str(error)})
            return
        except Exception:
            _log.exception("S1-mini normalize failed")
            yield line({"type": "error", "status": 500, "message": "S1-mini could not answer"})
            return
        finally:
            await events.aclose()
        result = build({**stats, "content": "".join(parts).strip()}, started)
        if key:
            cache.put(key, result, size)
        yield line({"type": "done", **result})

    @app.post("/v1/normalize")
    async def normalize(body: NormalizeRequest, request: Request, response: Response, _auth: None = Depends(authorize),
                        _slot: None = Depends(slot)) -> Any:
        started = time.perf_counter()
        size = cache_limit()
        key = cache_key(body) if size else None
        kept = cache.get(key, size) if key else None
        if kept is not None:
            kept = {**kept, "duration_ms": round((time.perf_counter() - started) * 1000)}
            if body.stream:
                return ndjson(replay(kept), "hit")
            response.headers["X-Cache"] = "hit"
            return kept
        args = (body.text, body.styling, body.structure, body.context)
        try:
            if body.stream:
                events = client.normalize_stream(*args)
                try:
                    first = await first_piece(request, events, None)  # the first sentence is waited for here, so a refusal is still an HTTP error
                except CallerGone:
                    await events.aclose()
                    return Response(status_code=499)  # nobody is listening; the generation is already stopped
                except BaseException:
                    await events.aclose()
                    raise
                return ndjson(live_lines(events, first, started, key, size), "miss")
            reply = await client.normalize(*args)
        except EngineError as error:
            raise HTTPException(422 if error.status == 400 else (error.status or 500), str(error)) from None
        except Exception:
            _log.exception("S1-mini normalize failed")
            raise HTTPException(500, "S1-mini could not answer") from None
        result = build(reply, started)
        if key:
            cache.put(key, result, size)
        response.headers["X-Cache"] = "miss"
        return result

    @app.put("/admin/threads", dependencies=[Depends(admin)])
    def set_threads(body: Threads) -> dict[str, Any]:
        """Applies to this replica now and is saved; the other replicas apply it within a few seconds. It beats SERVICE_NUM_THREAD."""
        client.num_thread = body.num_thread
        settings.save(tokens.settings_path, num_thread=body.num_thread)
        return {"num_thread": client.num_thread}

    @app.get("/admin/workers", dependencies=[Depends(admin)])
    def workers() -> dict[str, Any]:
        """Read-only: the replica count is a Docker Compose setting the service cannot change."""
        def number(name: str) -> int | None:
            value = os.environ.get(name, "").strip()
            return int(value) if value.isdigit() else None

        return {"replicas": number("SERVICE_REPLICAS"), "ollama_parallel": client.parallel}

    @app.get("/admin/updates", dependencies=[Depends(admin)])
    async def update_status() -> dict[str, Any]:
        """Return the last check without I/O; zero check hours disables only the timer."""
        return {"enabled": True, **updates.status()}

    @app.post("/admin/updates/check", dependencies=[Depends(admin)])
    async def check_updates() -> dict[str, Any]:
        return {"enabled": True, **await updates.check()}

    @app.post("/admin/updates/weights", dependencies=[Depends(admin)])
    async def update_weights() -> dict[str, Any]:
        try:
            return {"enabled": True, **await updates.update_weights()}
        except Exception as error:
            _log.exception("Weight update failed")
            raise HTTPException(502, f"Weight update failed: {error}") from None

    @app.get("/admin/tokens", dependencies=[Depends(admin)])
    def list_tokens() -> dict[str, Any]:
        return {"tokens": [{**row, "projects": None} for row in tokens.list()]}

    @app.post("/admin/tokens", dependencies=[Depends(admin)])
    def create_token(body: NewToken) -> dict[str, Any]:
        return tokens.create(body.name.strip(), None, body.rate, body.expires_days)

    @app.get("/admin/settings", dependencies=[Depends(admin)])
    def get_settings() -> dict[str, Any]:
        return {"settings": config.describe() if config else []}

    @app.put("/admin/settings", dependencies=[Depends(admin)])
    def put_settings(body: SettingsUpdate) -> dict[str, Any]:
        """Saves options; null resets one. Every replica reads the shared file, so a change applies everywhere within
        a second, except options marked `restart`."""
        if config is None:
            raise HTTPException(409, "Settings are fixed in this setup")
        try:
            config.set(body.values)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return {"settings": config.describe()}

    @app.put("/admin/tokens/{token_id}", dependencies=[Depends(admin)])
    def update_token(token_id: str, body: TokenScope) -> dict[str, bool]:
        if not tokens.update(token_id, None, body.rate):
            raise HTTPException(404, "No such token")
        return {"ok": True}

    latency = LatencyHistogram()

    app.add_middleware(Measure, observe=latency.observe,
                       record=lambda token_id, ms, failed: run_in_threadpool(tokens.record, token_id, ms, failed))

    @app.get("/metrics", dependencies=[Depends(admin)])
    def metrics() -> PlainTextResponse:
        """Prometheus text per token, from the shared token file (up to a minute behind). Send the admin password."""
        return PlainTextResponse(tokens.metrics("s1mini") + latency.render("s1mini"))

    @app.delete("/admin/tokens/{token_id}", dependencies=[Depends(admin)])
    def revoke_token(token_id: str) -> dict[str, bool]:
        if not tokens.revoke(token_id):
            raise HTTPException(404, "No such token")
        return {"ok": True}

    web = Path(os.environ.get("SERVICE_WEB_DIR", Path(__file__).resolve().parents[1] / "web"))
    if web.is_dir():
        app.mount("/admin", StaticFiles(directory=web, html=True))

    return app


def main() -> None:
    import sys

    import uvicorn

    if "--dotenv" in sys.argv[1:]:  # run.bat asks for it; a plain `python src/app.py` and Docker never read .env files
        loaded = settings.load_env_files(Path(__file__).resolve().parents[1])
        for name, names in loaded.items():
            print(f"{name}: loaded {', '.join(names) or 'no variables'} (names only; a real environment variable wins)", flush=True)
        if not loaded:
            print(".env.local / .env: no file in the service folder", flush=True)
    settings.apply_models_dir()  # SERVICE_MODELS_DIR -> HF_HOME / OLLAMA_MODELS

    store = TokenStore(os.environ.get("SERVICE_TOKENS_FILE", "tokens.json"))
    config = settings.Config(store.settings_path, SETTINGS)
    client = engine_from_env(
        config=config, chunk_tokens=config.get("chunk_tokens"), marker=default_marker()
    )
    saved = settings.load(store.settings_path)
    threads = saved.get("num_thread") or os.environ.get("SERVICE_NUM_THREAD", "").strip()
    if threads:
        client.num_thread = int(threads)
    app = create_app(client=client, admin_password=os.environ.get("SERVICE_ADMIN_PASSWORD") or None,
                     tokens=store, config=config)
    host, port = os.environ.get("SERVICE_HOST", "127.0.0.1"), int(os.environ.get("SERVICE_PORT", "29280"))

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)  # after uvicorn's own "running on" line
            shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            print(f"INFO:     Admin page: http://{shown}:{port}/admin/", flush=True)

    Server(uvicorn.Config(app, host=host, port=port)).run()


if __name__ == "__main__":
    main()
