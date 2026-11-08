# STC_S1Mini_Model: without Docker, on Windows

Run the S1-mini API directly with Python and local Ollama: one process, no load balancer, listening on `127.0.0.1:29280`.

## Requirements
- Windows with Python 3.11 or newer and uv (`winget install astral-sh.uv`).
- Running Ollama with the selected S1-mini model; see [setup](ollama.md).
- Network for the small pinned tokenizer at first successful warm-up.

## Start
Double-click `run.bat`, or run it in a terminal from `STC_S1Mini_Model\`. Choose **1 Run the service** in the menu, or run `run.bat run` to skip the menu. It runs `uv sync` to create or update `.venv`, then starts
`python src/app.py --dotenv`. Ollama must already run; the API downloads only its tokenizer.

Linux or macOS:
```bash
cd STC_S1Mini_Model
uv sync
uv run python src/app.py
```

## Settings
`run.bat` loads `.env` in this folder (copy `.env.example`) through `--dotenv` and prints the names it loaded, never the values; a real environment variable wins over it. A plain `python src/app.py` does not read `.env`. Set variables in `.env` or in the environment before starting:

| Variable | Default | Effect |
|---|---|---|
| `SERVICE_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` only if other devices must reach it. |
| `SERVICE_PORT` | `29280` | Port. |
| `SERVICE_ADMIN_PASSWORD` | empty | Unlocks `/admin/` and requires tokens on every call but `/health`. Empty = no auth. |
| `SERVICE_OLLAMA_URL` | `http://127.0.0.1:11434` | Required URL; empty fails. |
| `SERVICE_OLLAMA_MODEL` | `s1-mini:f16` | Installed model. |
| `SERVICE_WARM` | `1` | Load the model at start; with `0` the first request pays the load time. |
| `SERVICE_NUM_THREAD` | empty | Saved display value only; Ollama chooses its own threads. |
| `SERVICE_MODELS_DIR` | the library's default cache | Local tokenizer cache override through `HF_HOME`; a real `HF_HOME` wins. Does not configure an already-running Ollama server cache. |

PowerShell: `$env:SERVICE_ADMIN_PASSWORD = "secret"; .\run.bat`
Bash: `SERVICE_ADMIN_PASSWORD=secret uv run python src/app.py`

## Check
```bash
curl http://127.0.0.1:29280/health
```
`ready` is `false` until the model has loaded. Then open `http://127.0.0.1:29280/admin/` (see [guide-web-ui.md](guide-web-ui.md)),
make a token, and try a call:
```bash
curl -X POST http://127.0.0.1:29280/v1/normalize -H "Content-Type: application/json" -H "X-Internal-Token: s1mini_..." -d '{"text":"so um i need to like send the report by uh friday no wait thursday"}'
```

## Stop
Ctrl+C in the terminal. Do not run this and the Docker stack together: both use port 29280.

## Tests
`uv run pytest`.
