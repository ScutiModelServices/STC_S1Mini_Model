# STC_S1Mini_Model

First start: open `/admin/` → **Updates** → **Download**; see [the web UI guide](docs/guide-web-ui.md#updating-the-model-files). Startup and requests load downloaded files only; an empty installation keeps health/admin available and returns 503 for inference. Import the model with [the Ollama guide](docs/ollama.md) before downloading its tokenizer.

The one place S1-mini is served. S1-mini is a 0.6B text normalizer for speech-to-text output (Qwen3 fine-tune by
Superwhisper). Projects send it a raw transcript over HTTP and get clean text back: fillers removed, self-corrections
resolved, punctuation and capitals added, spoken numbers, times and emails written out. It is not a chat model.
English only. It has the same admin page, tokens, rate limits, metrics, settings and update checks as the other
model services.

S1-mini always runs through Ollama. Local URL defaults to `http://127.0.0.1:11434`;
Compose defaults to `http://host.docker.internal:11434`. Explicit empty URL is an error.
Start Ollama and prepare `s1-mini:f16` first; see [pinned setup](docs/ollama.md).
The model-card system prompt stays unchanged, with greedy decoding and thinking off.
Only the admin Updates Download action downloads the pinned tokenizer. Ollama owns model memory; API replicas
keep independent queues and answer caches.

## API

| Call | Purpose |
|---|---|
| `POST /v1/normalize` | `{"text", "styling", "structure", "context", "stream"}` → `{"content", "finish_reason", "model", "usage", "duration_ms"}`. |
| `GET /health` | `{"status": "ok", "model", "ready", "num_thread"}`; `ready` is true once the model is loaded. No token needed. |

`styling`: `casual`, `semi-casual`, `semi-formal` (default), `formal`. `structure`: `prose` (default) or `lists`.
`context`: `general` (default) or `email`. Other values are refused with 422: the model garbles output for values it
was not trained on. Any other field (`messages`, `tools`, `temperature`, ...) is refused with 422 too.

Example: `so um i need to like send the the report by uh friday no wait make that thursday` becomes
`So I need to send the report by Thursday.`

An empty `content` is a valid result (a transcript that holds only fillers).

**Long text.** Input is cut at sentence ends into passes of at most *Input tokens per pass* (`SERVICE_CHUNK_TOKENS`,
default 800; the model card advises under about 1000) and each pass is normalized on its own. One sentence longer than
the limit is sent whole. Output joins with a space (a newline for `lists`).

**Streaming, per sentence.** With `"stream": true` the reply is `application/x-ndjson`. One line per finished
sentence, not per token: `{"type": "sentence", "text": "So I need to send the report by Thursday. "}`, then
`{"type": "done", ...}` holding the same body a call without `stream` returns. Each `text` keeps the whitespace that
follows it, so joining the pieces gives the answer. The first sentence arrives when the model has finished it. A
refusal before the first sentence (a full queue) is a normal HTTP error; a failure after it is sent as
`{"type": "error", "status", ...}` and ends the stream. A kept answer (see below) is streamed back sentence by sentence.

**Cache.** *Answers kept per replica* (`SERVICE_CACHE_SIZE`, default 256, 0 = off) keeps the last answers for identical
requests and sends them again without generating (`X-Cache: hit`). Decoding is always greedy, so every request is
kept. New weights empty the cache. The cache is per replica, so with N replicas the hit rate is about 1/N. `stream`
does not change the key.

**Tokens.** Project scopes are removed: token create/edit accepts only omitted or null `projects`
(for shared-editor compatibility); any list returns 422. Inherited scopes are ignored and hidden.

## Admin page

`/admin/` (password `SERVICE_ADMIN_PASSWORD`): tokens with rate limit and expiry, usage numbers, CPU threads, the
Settings card (update check hours, in-flight cap, answers kept, input tokens per pass), and the Updates card
(**Check now** shows the local import digest with no online version; **Download** fetches the pinned tokenizer).
The shared data-volume marker invalidates every replica's loaded handle and answer cache.
CPU threads are sent to Ollama as `num_thread` with every request; unset lets Ollama decide. Set it to the physical core count.
`/metrics` (admin password) is Prometheus text per token. `s1mini_request_seconds` is a histogram of how long each
`/v1` call took on the replica that answers (to the first byte for a streamed reply); it starts from 0 when the
replica restarts.

## Run

```bash
docker compose --env-file .env.compose up -d --build        # http://<host>:29280
```

Without Docker: `run.bat` (uv, listens on `127.0.0.1:29280`). Ollama must already run.
If Ollama is unavailable or its model is missing, health/admin still answer with readiness false;
normalization returns 503 with a admin Updates Download hint. The next request retries automatically.
See [deployment](deployment.md).

## Tests

```bash
uv sync
uv run pytest
```

## Limits worth knowing

- Up to `SERVICE_OLLAMA_PARALLEL` generations at once per replica (default 2); extra calls wait, and more than the in-flight cap get 429.
- The rate limit is per replica.
- A pass has a 120 s timeout. Backend failures return 503 before streaming or an error event after streaming starts.
- A style that drops end punctuation (`casual`) can leave no full stop between two passes of a long text.
- Licence: Apache 2.0 plus a naming clause. Wherever the model is named it must be "S1-mini" by "Superwhisper". Read
  the `LICENSE` on the Hub before redistributing.

## Try it from the admin page

Open `/admin/` and use **Try it** to call the public API with a service token held only in memory. Runs use normal token scopes, limits and caches; the panel shows HTTP status, latency, responses, cancellation and a curl equivalent with a token placeholder. See [the web UI guide](docs/guide-web-ui.md#try-it-send-a-real-request).

Normalize sends transcript text plus styling, structure and context to `POST /v1/normalize`. Streaming appends sentence events as they arrive. This cleans text with S1-mini by Superwhisper; it does not transcribe uploaded audio or act as a general chat model.

## Documentation

- [Purpose: input, output and what it is for](docs/purpose.md)
- [Ollama setup](docs/ollama.md)
- [Web UI](docs/guide-web-ui.md)
- [Deployment](deployment.md)
- [Changelog](CHANGELOG.md)
- [Docker on another machine](docs/guide-docker-deployment-device.md)
- [Docker on this PC](docs/guide-docker-same-device.md)
- [Run without Docker](docs/guide-no-docker-same-device.md)
- [Test it from the command line](docs/guide-cli.md)

## Admin pages and navigation

Open `/admin/` and sign in. Overview opens first; each sidebar destination has its own URL fragment and supports browser Back/Forward and direct links:

- `#overview`: health, model readiness, replicas, active tokens and request counts.
- `#playground`: service-specific request forms and streamed or media responses.
- `#tokens`: create, edit, expire and revoke scoped access tokens.
- `#runtime`: CPU threads where supported, replica information and live/restart settings.
- `#updates`: check versions and explicitly download weights.
- `#connections`: caller addresses and public endpoints.
- `#models` and `#voices`: project models and cloned voices, shown only when supported.

Collapse/Expand switches between labeled sidebar and icon rail on desktop and mobile. The non-secret sidebar preference is saved per service in this browser. Navigation preserves form drafts and active Playground requests; sign-out still clears credentials and cancels work. Page names appear once in the main header. No preview data or cross-service selector ships in the real admin UI. Existing authentication, quotas, settings precedence and explicit-download policy remain in effect.
