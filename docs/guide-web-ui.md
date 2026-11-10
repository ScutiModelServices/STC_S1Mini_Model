# STC_S1Mini_Model: the admin web UI

The admin UI at `/admin/` has separate sidebar pages for health, access and model configuration. It works with every deployment mode. Only the address differs:

| Run | Address |
|---|---|
| Docker or `run.bat` on this PC | `http://127.0.0.1:29280/admin/` |
| Docker on the deployment box | `http://<box-ip>:29280/admin/` |

If `SERVICE_PORT` is changed, use that port.

## Before you open it

The page needs the admin password: the value of `SERVICE_ADMIN_PASSWORD`.
- Docker Desktop on this device: set it in `.env.compose` next to `docker-compose.yml`, then `docker compose --env-file .env.compose up -d`.
- Deployment device: set it in `.env` next to `docker-compose.yml` (Deploy option 2 copies it), then `docker compose up -d`.
- No Docker: set it in `.env.local` (or `.env`) in this folder and start with `run.bat` (it passes `--dotenv`), or set it in the environment first; a real environment variable wins over both files and `.env.local` wins over `.env`. A plain `python src/app.py` reads neither.
- Empty password = no auth at all and the page answers `Admin password required (set SERVICE_ADMIN_PASSWORD)`. Set
  one to use the page.

## Sign in and service status

Type the admin password and press **Sign in**. A wrong password shows an inline `Wrong admin password` error.
The password stays in `sessionStorage` for this browser tab, so reloading restores the session. **Sign out** clears
the password and any freshly created token. If browser storage is blocked, sign in again after reloading.

The header shows the service address and a health badge with an icon and text: **Healthy**, **Starting**, or
**Unreachable**. Health is checked without authentication every 10 seconds while signed in. **Starting** means the model
has not successfully loaded through Ollama yet. Tiles show model readiness, replicas, Ollama parallel requests, CPU threads, and active tokens.
Use **Theme** to cycle between system, light, and dark appearance; this preference is saved in the browser.

## Tokens

Each calling project gets its own token, so one can be revoked without touching the others.
- **Create:** type a project name (up to 64 characters) and press **Create token**. The token appears once in
  an accent-tinted box with a **Copy** button and the reminder “Shown once. Copy it before you leave.”
  The box is cleared on the next create attempt or sign-out. Clipboard access has a fallback for plain HTTP;
  if both methods are blocked, select the token and copy it manually.
- **Use:** the caller sends it in the `X-Internal-Token` header on every call except `/health`.
- **List:** the table shows **Project**, **Created**, **Last used**, and **Requests** (authenticated calls so far). Last used is **never** until a token
  authenticates a request, then a relative time; hover a timestamp for its exact local time. The list refreshes
  every 10 seconds. Token values are not shown again.
- **Revoke:** press the red-outlined **Revoke** pill and confirm in the in-app dialog, which names the project.
  **Cancel**, Escape, or a click on the backdrop closes the dialog. The caller gets `401 Invalid token` from then on. Lost
  token = revoke it and create a new one.

Last-used timestamps are Unix seconds or null. They are persisted at most once per minute per token, so they
are approximate. Request counts are added to the file with that same write; uses between writes are
counted in memory, so a crash can lose up to a minute of them. Old token files without the field still load and show **never** until next use. A companion
`tokens.json.lock` file serializes updates across replicas without adding dependencies.

Tokens live in `tokens.json` (`SERVICE_TOKENS_FILE`). In Docker that is the `stc_s1mini_model_data` volume at `/data`, so they
survive `docker compose down` and rebuilds. Without Docker it is `tokens.json` in the folder the service starts from.
Tokens are enforced only while `SERVICE_ADMIN_PASSWORD` is set.

## CPU threads

This card saves/displays 1 to 256 and synchronizes replicas. It has no generation effect:
Ollama chooses its own threads. Configure Ollama on its host. Saved value beats `SERVICE_NUM_THREAD`.

## Replicas and parallel requests (read-only)

The tiles show replicas and parallel generations from `SERVICE_REPLICAS` and `SERVICE_OLLAMA_PARALLEL`: each replica runs that many generations at once (default 2; set it to the `OLLAMA_NUM_PARALLEL` of your Ollama).
The muted line says **Replicas: N. Change SERVICE_REPLICAS in .env.compose (or .env on the deployment device), then rebuild.** The
page cannot change them. To change, set them in `.env.compose` (or `.env` on the deployment device), then:
```bash
docker compose --env-file .env.compose up -d --build
docker compose restart stc_s1mini_model_lb
```
`unknown` means the variable is not set, which is normal without Docker. Without Docker there is one process.

## Problems

| Symptom | Fix |
|---|---|
| Page does not load | Service not running, wrong port, or blocked by a firewall. Test `/health`. |
| `Wrong admin password` | Password differs from `SERVICE_ADMIN_PASSWORD` in the running container. Recreate it after editing `.env.compose` or `.env`. |
| `Admin password required (set SERVICE_ADMIN_PASSWORD)` | The password is empty. Set it and restart. |
| Token lost | Revoke it and create a new one. |
| `/health` shows `ready: false` | Ollama or its model is unavailable, or the tokenizer is loading (see the container logs). |
| Never expose port 29280 to the internet | It is a LAN tool; use a firewall or VPN for remote access. |

## Limiting a token to projects

Not used by this service: it has one model and no project models, so the list has no effect.

The shared editor remains for compatibility; leave the projects field blank. Token create/edit accepts
only omitted or null scopes; any list returns 422. Inherited scopes are ignored and hidden.
The `project` request field is unsupported and returns 422.

## Rate limit, expiry, edits, usage and metrics

- **Requests per minute** (optional, when creating or editing a token): over the limit a call gets `429` with `Retry-After`. The count is per replica, so with several replicas behind the balancer the real limit can be up to that many times higher.
- **Expires in (days)** (optional, only when creating): after that the token is refused like a revoked one, and the row shows "Expired". Create a new token to continue.
- **Edit** changes the allowed projects and the rate limit without revoking the token.
- **Usage:** each row shows errors (answers with status 500 or above) and the average time per call. The counters are written at most once a minute.
- **Metrics:** `GET /metrics` (send the admin password in `X-Internal-Token`) returns the same numbers as Prometheus text, one series per token. It reads the shared token file, so any replica answers with the same data. `s1mini_request_seconds` is a histogram of how long each `/v1` call took on the replica that answers (to the first byte for a streamed reply); it starts from 0 when the replica restarts.
- **Project models** card: unused by this service.

## Settings

**Host Ollama URL** and **Ollama model** (default `s1-mini:f16`) apply after restart.
Local URL defaults to `http://127.0.0.1:11434`; Compose supplies the host-gateway address.
Blank URL fails startup. Tokenizer source/revision are fixed. Other settings cover update interval,
in-flight cap, cache size, input tokens per pass and warm-up. Saved values win; Reset removes them.
Replicas re-read settings; restart rows apply on startup.

## Updating the model files

The service loads only downloaded weights and tokenizer files. Startup, warm-up and requests never download. With no downloaded model, `/health` and `/admin/` answer, `ready` is false and inference returns 503 with an admin Download hint. First start: open `/admin/` → **Updates** → **Download**. S1-mini is a local Ollama import: import its verified GGUF using [the Ollama guide](ollama.md), then use Download for the pinned tokenizer. There is no online model version to compare. Registry-backed alternate tags also pull on Download.

Admin curl alternative:

```bash
curl -X POST http://127.0.0.1:29280/admin/updates/weights -H "X-Internal-Token: <ADMIN_PASSWORD>"
```

## Connect to this service

The **Connect to this service** card lists the addresses other services can use: the address you opened the page with, the same PC (`127.0.0.1`), another Docker container on the same network (the `_lb` name from the compose file), and a Docker container calling this PC (`host.docker.internal`), then every endpoint with a **Copy** button. Callers send a token from the tokens card in `X-Internal-Token`.

## Try it: send a real request

1. Sign in to `/admin/` and find **Try it**.
2. The token field is hidden. Signing in to the admin page makes a temporary public token named `playground (temporary)` (valid 1 day) and fills it in, and choices such as voices load by themselves. It is the public client token, not the admin password.
3. Choose an operation, complete its labeled fields and send it. Requests go to this service's public route, with the same token scope, rate/admission limits and cache as a separate client.
4. Read the HTTP status, elapsed time and response. NDJSON/SSE text appears while it arrives; JSON is formatted. HTTP errors, including 401, 422, 429 and 503, keep their response body visible. Use **Cancel** to abort an active request.
5. Copy the curl equivalent to repeat the request elsewhere. The token is always a placeholder; fill it locally. For an upload, provide the indicated input file. Multi-step operations show their follow-up request too.

Authentication is **a temporary public token made from the admin session and held only in memory**. The playground does not persist it in browser storage, put it in a URL, log it or include it in curl. Sign out or close the tab revokes it; opening the admin page in a second tab replaces it. It appears in the tokens list and counts as an active token while it lives. The existing admin sign-in is separate. A playground run consumes normal service quotas and can generate retained files where the public API normally does so; cancelling the browser fetch does not promise to undo work already accepted by a backend.

The browser caps uploads at **10 MiB**, displayed text at **256 KiB**, and binary audio/image responses at **32 MiB**. Server limits still apply and may be lower. Media uses bounded Blob URLs, revoked when replaced or cleared. Text is rendered as text, never executed as HTML. Audio players become available after the bounded audio download finishes. No new environment setting is needed.

### This service's request

Normalize sends transcript text plus styling, structure and context to `POST /v1/normalize`. Streaming appends sentence events as they arrive. This cleans text with S1-mini by Superwhisper; it does not transcribe uploaded audio or act as a general chat model.

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
