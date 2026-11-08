# Host Ollama setup

S1-mini by Superwhisper always runs through Ollama. The API downloads only a pinned tokenizer,
using `tokenizers` for exact counts and the original two-message Qwen3 non-thinking template.
Tokenizer revision: `88f6b15896c73bbb13a3b596e0afe8ea0d5150b4` from `superwhisper/s1-mini`.
Raw `/api/generate` preserves the model-card prompt and control line. Decoding stays greedy,
with repetition penalty 1, CPU offload disabled and context sized for prompt plus output budget.

## Pinned local import

The official [GGUF repository](https://huggingface.co/superwhisper/s1-mini-GGUF) has F16 exports.
The tracked `Modelfile` imports `.local-models/s1-mini-f16.gguf`.

```powershell
New-Item -ItemType Directory -Force .local-models | Out-Null
Invoke-WebRequest 'https://huggingface.co/superwhisper/s1-mini-GGUF/resolve/34add00a48a2e5d24e5a4ee5405a99620a3a240c/s1-mini-f16.gguf' -OutFile .local-models/s1-mini-f16.gguf
Get-FileHash .local-models/s1-mini-f16.gguf -Algorithm SHA256
ollama create s1-mini:f16 -f Modelfile
ollama show s1-mini:f16
```

Verify SHA-256: `0370da4f1bae19e3150bcafa33c5d396c15f97bf25519540a3e013db5cc00af4`.
Ollama must already run. Another model must match the pinned vocabulary and prompt.

## Run

Local default URL is `http://127.0.0.1:11434`; Compose defaults to `http://host.docker.internal:11434`
with Linux host-gateway mapping. Host Ollama must accept container connections. Explicit empty URL fails.
Saved URL/model settings beat environment values and apply after restart.

```powershell
$env:SERVICE_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:SERVICE_OLLAMA_MODEL = 's1-mini:f16'
uv run python src/app.py
```

Health/admin start during outages; readiness stays false and calls return 503 with a start/pull hint.
The next request retries. CPU threads are saved/displayed only; Ollama chooses its own threads.
API replicas have independent queues/caches and share Ollama model memory.

## Updates and verification

Check now compares local digests. Download runs `ollama pull`, then invalidates every replica
through the data-volume marker. Unpublished local F16 imports cannot be pulled from a registry;
re-import the verified GGUF when needed. Failed pulls keep current loaded state.
The old models volume is unused; Docker tokenizer cache is under `/data/tokenizer-cache`.

```powershell
uv run pytest
uv run python tools/check_sync.py
```

Fake-server tests verify prompts, streams, chunks, cancellation, readiness, 503 errors and updates.
Real Ollama quality/speed comparison and Docker builds have not been repeated for this migration.
