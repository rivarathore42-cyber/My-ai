---
title: Galaxy AI
emoji: 🌌
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Galaxy AI Engine v7.0

A private, local-first assistant: 20 agents (math, knowledge, code, creative writing, translation, data,
memory, voice, and more) behind one chat page with a voice orb.

## First run

1. Open the Space and wait for the build to finish.
2. Open the **Logs** tab. The console prints a line that starts with `PAIRING CODE:`.
3. Enter that 8-digit code on the page. Your browser keeps a private session for 30 days, so you enter it once.

Admin routes use an operator token. It is written to the logs at boot. To keep one fixed token across restarts, add
`GALAXY_API_TOKEN` as a Space secret. Chatting never needs it.

## Settings (Space → Settings → Variables and secrets)

| Name | Default | What it does |
|---|---|---|
| `GALAXY_AUTH` | `paired` | Keep `paired` on a public Space. `local` is refused on any non-loopback address. |
| `GALAXY_OWNER_ID` | `owner` | The user id the paired device belongs to. |
| `GALAXY_GGUF_REPO` / `GALAXY_GGUF_FILE` | Qwen2.5 1.5B Instruct (q4_k_m) | The local model. Empty values run heuristics only. A 7B model fits in memory but answers slowly on the free CPU. |
| `GALAXY_API_TOKEN` (secret) | generated | Fixed operator token for admin routes. |

## Limits of the free tier (read before relying on it)

- **It sleeps when idle.** The first request after a sleep takes minutes while the container starts.
- **Storage is not permanent.** A restart or a rebuild clears the database under `/app/data`, so conversations,
  memory and paired devices are lost. Pair again after each restart; the new code is in the logs.
- **CPU only.** The model answers slowly. Everything else (math, notes, routing, the reply contract) is fast.

## Voice (Hey Galaxy)

Voice Wake-Up runs fully offline in the browser with Vosk. It needs `vendor/vosk.js` and two model archives, which
the Dockerfile fetches only when you build with `FETCH_VOICE=1` (Space → Settings → Variables: build argument).
Before enabling it, check the versions and the checksums of the sources in `scripts/fetch_voice_assets.py`.
Without these files the page shows voice as unavailable; nothing else changes.

Audio never leaves the device: the browser's speech service is not used.
