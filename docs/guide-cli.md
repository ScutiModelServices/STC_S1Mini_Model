# Test it from the command line

First start: open `/admin/` → **Updates** → **Download**; see [the web UI guide](guide-web-ui.md#updating-the-model-files). Startup and requests load downloaded files only; an empty installation keeps health/admin available and returns 503 for inference. Import the model with [the Ollama guide](ollama.md) before downloading its tokenizer.

Calls S1 Mini with PowerShell only, no admin page. Port 29280. Replace `<...>` values. The commands were written from the code and not yet run against a live server.

## Get a token

Every call except `/health` needs a token while `SERVICE_ADMIN_PASSWORD` is set. Admin calls take the admin password itself; a token works only on `/v1/*`. Create one token and reuse it. Each `POST /admin/tokens` call makes a new one, so do not put it in a loop. Revoke extras on the admin page.

```powershell
$base = "http://127.0.0.1:29280"
$pw   = @{ "X-Internal-Token" = "<SERVICE_ADMIN_PASSWORD>" }
$tok  = (Invoke-RestMethod -Method Post -Uri "$base/admin/tokens" -Headers $pw -ContentType "application/json" -Body '{"name":"cli-test"}').token
$tok
$h    = @{ "X-Internal-Token" = $tok }
Invoke-RestMethod "$base/health"
```

`$pw` (the admin password) is for `/admin/*` only. `$h` (the token) is for `/v1/*` only; using the password there gives `Invalid token`. `$tok` prints once: save it to skip this step next time, then set `$h` from it. With no admin password set there is no auth: skip `-Headers $h`.

## Call it

```powershell
$req = @{ text = "um so i think we should uh meet on tuesday no wait thursday" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/v1/normalize" -Headers $h -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($req))
```

Options: `styling`, `structure`, `context`, `stream`.

## Problems

| Answer | Cause |
|---|---|
| `401` | Wrong value: admin routes need the admin password, `/v1/*` needs a token. |
| `503` | Start Ollama and pull/import the configured model. |
| `429` | Token rate used up or too many calls waiting; wait `Retry-After` seconds. |
| `422` | A field is unknown or out of range; the answer says which. |
| connection refused | Not running, or another port; see the run-path guides. |
