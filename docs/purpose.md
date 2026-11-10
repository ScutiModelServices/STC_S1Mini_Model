# Purpose

**Kind:** text → text (speech-to-text clean-up)

**Group:** Speech Text Cleanup

**Tags:** `cpu`, `streaming`

| | |
|---|---|
| Input | Raw transcript text from a speech-to-text model. Options: styling, structure, context. |
| Output | Clean text: fillers removed, self-corrections resolved, punctuation and capitals added, spoken numbers/times/emails written out. |
| Runs on | CPU HTTP API with a reachable Ollama server and pinned tokenizer. Ollama owns the 0.6B model. English only. |

**Use for:** step after STT in a dictation flow.
**Not for:** chat, summarising, other languages, voice anything. It never hears audio.
