"""The options the admin page can change. Each can still start from its environment variable; a saved value wins."""

from ollama_backend import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL
from settings import Setting

SETTINGS = [
    Setting("update_check_hours", "Check for updates every (hours)", "float", 6.0, "SERVICE_UPDATE_CHECK_HOURS", 0, 720,
            help="0 = only when you press Check now"),
    Setting("max_inflight", "Calls allowed to wait for the model", "int", 0, "SERVICE_MAX_INFLIGHT", 0, 10000,
            help="Per replica; more get 429. 0 = 4"),
    Setting("cache_size", "Answers kept per replica", "int", 256, "SERVICE_CACHE_SIZE", 0, 100000,
            help="The last answers for the same request, sent again without generating. Output is always greedy, so every request is kept. 0 = off"),
    Setting("chunk_tokens", "Input tokens per pass", "int", 800, "SERVICE_CHUNK_TOKENS", 50, 4000,
            help="Longer text is cut at sentence ends and normalized piece by piece. The model card advises staying under about 1000"),
    Setting("ollama_url", "Host Ollama URL", "text", DEFAULT_OLLAMA_URL, "SERVICE_OLLAMA_URL", restart=True,
            help="Required. Docker: http://host.docker.internal:11434; local: http://127.0.0.1:11434"),
    Setting("ollama_model", "Ollama model", "text", DEFAULT_OLLAMA_MODEL, "SERVICE_OLLAMA_MODEL", restart=True,
            help="S1-mini GGUF imported with ollama create or pulled from its registry; the matching tokenizer is pinned"),
    Setting("warm", "Load the model at start", "bool", True, "SERVICE_WARM", restart=True),
]
