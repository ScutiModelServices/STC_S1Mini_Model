# Changelog

## Unreleased

- Fix: a caller that hangs up while the first piece of a stream is still being made now stops the work for nobody. The request timing moved from `@app.middleware("http")` to a plain ASGI middleware (`Measure` in `src/disconnect.py`), because the decorator form hides a hang-up from `request.is_disconnected()`; metrics are unchanged.

- Admin UI: separate hash-routed pages in a collapsible sidebar; health-first Overview, one main-header page title, capability-specific navigation, draft-preserving navigation and responsive desktop/mobile layout. Existing admin and public API actions are preserved.

- Admin page: the CPU threads card and tile are hidden when `CONFIG.tags` has `host-threads`, and the Project models card is shown only with `project-models`.

- Admin page: `CONFIG.tags` (same tags as `docs/purpose.md`) switch tag-driven notes: `gpu` shows a CPU-threads note, `license-restricted` shows a licence note on the Updates card.

- Fix: after Download, a CPU threads change or a sync from another replica drops the loaded model, the service now reloads it in the background (when "Load the model at start" is on) instead of showing "Starting" on `/admin/` until the first request.
- Breaking: startup, warm-up and requests load downloaded weights only. First run: open `/admin/` → Updates → Download; health/admin remain available and inference returns 503 until ready.
- Download now fetches the pinned tokenizer; local `s1-mini:f16` weights remain an operator GGUF import, with no online model version to compare. Registry-backed alternate tags pull only on Download.

- New `SERVICE_OLLAMA_PARALLEL` (default 2): generations one replica runs at once, like CNV_MiniCPM52B_Model. Before, each replica ran one at a time. Set it to the `OLLAMA_NUM_PARALLEL` of your Ollama; the in-flight cap defaults to 4 x this value.
- Breaking: removed torch backend, transformers/accelerate dependencies, `model`/`dtype` settings (`SERVICE_MODEL`/`SERVICE_DTYPE`). Ollama is now the only backend; the fixed pinned tokenizer uses tokenizers without model weights. Removed settings return 422. `project` remains unsupported (422).
- Migration: start Ollama and pull/import `SERVICE_OLLAMA_MODEL`. Local URL defaults to `http://127.0.0.1:11434`, Compose to `http://host.docker.internal:11434`; empty URL fails. Health/admin stay available during outages or missing models; normalization returns 503 with a start/pull hint and retries on the next request.
- Removed unused `stc_s1mini_model_models` volume mount/declaration. Old models volume with HF weights is unused and can be deleted after migration; keep it for rollback to the previous commit. Project model folders are no longer used. Tokens, settings, tokenizer-only cache and update markers remain in `stc_s1mini_model_data`.
- Removed unused token project restrictions: create/edit accepts only omitted/null `projects` for shared-editor compatibility; lists return 422. Inherited restrictions are ignored and hidden.
- CPU threads are saved/displayed only; Ollama chooses its own threads. Updates retain local digest checks; Download runs ollama pull. Unpublished local F16 tags still need the documented pinned import.
- Retired comparison reports and dual-backend benchmark. Historical Ryzen 7 7445HS/six-thread measurement: torch bf16 9.17 tok/s, Ollama F16 17.10 tok/s, identical outputs on twelve synthetic transcripts. Real Ollama execution and Docker builds were not repeated for this migration.

- `SERVICE_MODELS_DIR` sets where a local (no Docker) run keeps downloaded model weights: `settings.apply_models_dir()` maps it to `HF_HOME` and `OLLAMA_MODELS` at start.
- Docs: `docs/guide-cli.md`, a PowerShell-only way to test the service.

### Breaking: common service environment variables

Service-owned environment variables now use `SERVICE_*`. Old names are no longer read.
Rename old names once in your real `.env`, Compose `environment:` block, and CI/secret settings.

| Old name | New name |
|---|---|
| `S1MINI_ADMIN_PASSWORD` | `SERVICE_ADMIN_PASSWORD` |
| `S1MINI_CACHE_SIZE` | `SERVICE_CACHE_SIZE` |
| `S1MINI_CHUNK_TOKENS` | `SERVICE_CHUNK_TOKENS` |
| `S1MINI_DTYPE` | `SERVICE_DTYPE` |
| `S1MINI_HOST` | `SERVICE_HOST` |
| `S1MINI_MAX_INFLIGHT` | `SERVICE_MAX_INFLIGHT` |
| `S1MINI_MEM_LIMIT` | `SERVICE_MEM_LIMIT` |
| `S1MINI_MODEL` | `SERVICE_MODEL` |
| `S1MINI_NUM_THREAD` | `SERVICE_NUM_THREAD` |
| `S1MINI_OLLAMA_MODEL` | `SERVICE_OLLAMA_MODEL` |
| `S1MINI_OLLAMA_URL` | `SERVICE_OLLAMA_URL` |
| `S1MINI_PORT` | `SERVICE_PORT` |
| `S1MINI_REPLICAS` | `SERVICE_REPLICAS` |
| `S1MINI_TOKENS_FILE` | `SERVICE_TOKENS_FILE` |
| `S1MINI_UPDATE_CHECK_HOURS` | `SERVICE_UPDATE_CHECK_HOURS` |
| `S1MINI_WARM` | `SERVICE_WARM` |
| `S1MINI_WEB_DIR` | `SERVICE_WEB_DIR` |
| `S1MINI_WEIGHTS_MARKER` | `SERVICE_WEIGHTS_MARKER` |

- Move the default service port from 9220 to 29280, including API replicas and the nginx load balancer. Update any explicit old port in your real `.env`, saved tool-service `backends.json` connections, and browser or MCP client configuration once. With Compose defaults, recreate the stack to use the new host and container ports; a custom host-port override stays as configured, while container ports still change to 29280.

- Renamed `S1MiniModel` to `STC_S1Mini_Model`; Docker project, API image, services, load balancer and volumes now use `stc_s1mini_model` names. Callers on `citadel_core` connect to `stc_s1mini_model_lb` at the existing port.
- Docker volume migration: existing volumes with old names are orphaned; renaming does not move data. Stop the old stack before copying; never use `down -v`. The commands below assume the previous default Compose project name; substitute actual volume names if `-p` or `COMPOSE_PROJECT_NAME` overrode it.
  - `s1mini_models` → `stc_s1mini_model_models`: `docker volume inspect s1mini_models && docker volume create stc_s1mini_model_models && docker run --rm -v s1mini_models:/from:ro -v stc_s1mini_model_models:/to alpine:3.21 sh -c "cd /from && cp -a . /to/"`
  - `s1mini_tokens` → `stc_s1mini_model_data`: `docker volume inspect s1mini_tokens && docker volume create stc_s1mini_model_data && docker run --rm -v s1mini_tokens:/from:ro -v stc_s1mini_model_data:/to alpine:3.21 sh -c "cd /from && cp -a . /to/"`
- To retain an old volume instead, set its top-level volume definition to `external: true` and `name: <old-volume-name>` in a local Compose override. Rollback: stop the renamed stack, restore the previous checkout and folder name, and restart the old stack with its original volumes; copy back newer data first if the renamed stack accepted writes.

- `run.bat` loads `.env` from the service folder for a local run: it passes `--dotenv`, the service prints which variable names it loaded (never the values), and a real environment variable still wins. A plain `python src/app.py` and Docker never read `.env`.
- Untracked folders now start with `.` or `_`: `zip_versions/` is `_zip_versions/`. A local `voices`, `tem_code` or `zip_versions` folder from an older checkout needs renaming by hand.
- `run.bat` is now a menu (run the service, deploy, build the release ZIP) and `run.bat run|deploy|zip` skips it. `tools/deploy.bat`, `tools/make_zip.bat` and `tools/deploy.local.example.bat` are gone; the local deploy target is `tools/deploy.local.ini` (copy `tools/deploy.local.example.ini`).
- Admin page: **Connect to this service** card with the addresses other services can use (this page's address, same PC, Docker network, `host.docker.internal`) and every endpoint, each with a Copy button.
- Optional host Ollama backend (`SERVICE_OLLAMA_URL`, `SERVICE_OLLAMA_MODEL`) with torch fallback.
  Imports official pinned S1-mini by Superwhisper F16 GGUF using `Modelfile`; no conversion needed.
  Keeps exact HF-rendered non-thinking prompt, greedy decoding, token-based sentence chunking,
  sentence streaming, cache and 422 rules. CPU threads apply to each Ollama chunk. Transport errors
  map to EngineError; chunk completion never sets the caller's stop event. Updates report local
  digests and require explicit re-import. Adds fake-server tests and repeatable CPU/quality comparison.
  Ryzen 7 7445HS, six CPU threads: torch bf16 9.169 vs Ollama F16 17.097 output tok/s (1.86×);
  all twelve synthetic outputs identical. See `docs/backend-comparison.md` for method and limits.

- Admin **Try it** panel: descriptor-driven public API requests, live text streams, JSON/media output, status and latency, cancellation and redacted curl. Auth choice (b): pasted public token stays in memory; normal scope, limits and caches apply. Shared `web/playground.js` is copied across all model and tool services.

- `run.bat`: runs `uv sync` directly; `python -m uv` failed when uv was installed only as a program (winget).
- First version: Superwhisper S1-mini (0.6B speech-to-text normalizer) served on the CPU with `transformers`, with the
  admin page, tokens (rate limit, expiry), metrics, Settings card, update checks, weight downloads and answer cache
  shared with the other model services.
- `POST /v1/normalize` takes `text`, `styling`, `structure`, `context` and `stream`. `stream: true` sends one JSON line
  per sentence, then `done`.
- Long input is cut at sentence ends into passes of `SERVICE_CHUNK_TOKENS` tokens (default 800).
