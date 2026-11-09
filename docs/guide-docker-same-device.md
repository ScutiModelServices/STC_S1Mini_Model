# STC_S1Mini_Model: Docker Desktop on this PC

First start: open `/admin/` → **Updates** → **Download**; see [the web UI guide](guide-web-ui.md#updating-the-model-files). Startup and requests load downloaded files only; an empty installation keeps health/admin available and returns 503 for inference. Import the model with [the Ollama guide](ollama.md) before downloading its tokenizer.

Run the S1-mini API in Docker Desktop; reachable host Ollama runs the model.

## Requirements

- Docker Desktop with Compose v2, and external `citadel_core` network created by Citadel.
- Running host Ollama with the selected S1-mini GGUF; [import it first](ollama.md).
- Network for the small pinned tokenizer on first successful warm-up.
- Ollama owns model RAM. API replica memory has not been measured for this migration.

## Settings

Copy `.env.example` to `.env`. Saved admin values beat environment values.

| Variable | Default | Effect |
|---|---|---|
| `SERVICE_ADMIN_PASSWORD` | empty | Unlocks admin; public requests then need service tokens. Empty disables auth. |
| `SERVICE_PORT` | `29280` | Host port published by nginx. |
| `SERVICE_OLLAMA_URL` | `http://host.docker.internal:11434` | Required reachable Ollama address; empty fails. |
| `SERVICE_OLLAMA_MODEL` | `s1-mini:f16` | Installed model; saved restart setting wins. |
| `SERVICE_OLLAMA_PARALLEL` | `2` | Generations one replica runs at once; set it to the `OLLAMA_NUM_PARALLEL` of your Ollama. |
| `SERVICE_REPLICAS` | `1` | API replicas sharing Ollama model memory. |
| `SERVICE_MEM_LIMIT` | `0` | Optional per-API memory cap; Ollama memory is separate. |
| `SERVICE_NUM_THREAD` | empty | Saved/displayed only; Ollama chooses its own threads. |

Admin settings cover update interval, in-flight cap (0 = 4), cache size (256; 0 disables),
input tokens per pass (800), warm-up and Ollama URL/model. Tokenizer source/revision are fixed.
See [admin settings](guide-web-ui.md#settings).

## Start

```bash
docker compose up -d --build
```

Tokens, settings, tokenizer cache and update marker share `stc_s1mini_model_data`.
Ollama owns its model files. Docker includes the Linux host-gateway mapping.
The old models volume is unused; see [migration and rollback](../CHANGELOG.md).

## Check

```bash
curl http://127.0.0.1:29280/health
```

Health/admin answer while Ollama is unavailable. Readiness becomes true after successful warm-up
or a request; normalization returns 503 with a admin Updates Download hint until then. Open
`http://127.0.0.1:29280/admin/` and create tokens. Citadel callers use `http://stc_s1mini_model_lb:29280`.

## Updating

```bash
docker compose up -d --build
docker compose restart stc_s1mini_model_lb
```

Model updates use [the Updates card](guide-web-ui.md#updating-the-model-files).
Docker builds and a real Ollama run have not been repeated for this migration.

## Problems

| Symptom | Fix |
|---|---|
| 503 or not ready | Start Ollama, import the model, then use Updates → Download for the tokenizer. |
| 401 | Create a valid token and send it in `X-Internal-Token`. |
| 422 | Unknown field or invalid styling/structure/context. |
| 429 | Wait `Retry-After` seconds; rate or in-flight limit reached. |
| nginx 502 after scaling | Restart `stc_s1mini_model_lb`. |
| Slow answers | Configure Ollama on its host; the API threads card has no generation effect. |
| Port 29280 in use | Stop the other copy or change `SERVICE_PORT`. |
