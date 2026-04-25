# Mantra Creative Agent

## What is this?

A local FastAPI sidecar for the **Mantra Creative** web app at `mantra.majutrah.co.id`. It moves heavy compute (background removal, face enhancement, audio extraction) off the shared VPS GPU and onto the user's own PC for **lower latency, isolated workloads, and zero shared-GPU contention**.

The agent mirrors the production VPS API contract exactly, so the existing PHP web app talks to `localhost:5555` with **zero PHP changes** — just a JS smart-detect that picks local agent when available and falls back to the VPS otherwise.

**Phase 1 scope (current):** `/api/health` + `/api/remove-bg` + `/api/remove-bg/file`.
**Phase 2 (planned):** `/api/enhance` (GFPGAN), `/api/youtube-mp3`, `/api/full-album` (concat).

---

## Quick start (Windows)

```bat
cd "D:\CLAUDE\MANTRA CREATIVE\agent"
run_dev.bat
```

`run_dev.bat` will:
1. Create a `.venv/` virtual environment (first run only)
2. `pip install -r requirements.txt`
3. Launch the server on `http://127.0.0.1:5555`

> **First `/api/remove-bg` call downloads `u2net.onnx` (~168 MB)** to the user's app data dir (`%LOCALAPPDATA%\MantraAgent\models\`). This is a one-time cost — subsequent calls are cached.

---

## Quick start (manual / Linux / macOS)

```bash
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install -r requirements.txt
python main.py
```

Server listens on `http://127.0.0.1:5555` by default.

---

## Configuration (env vars)

All settings live in `config.py` and are env-overridable. Set them before launching `python main.py`.

| Variable                    | Default       | Purpose                                                                 |
| --------------------------- | ------------- | ----------------------------------------------------------------------- |
| `MANTRA_AGENT_HOST`         | `127.0.0.1`   | Bind address. Use `0.0.0.0` for LAN access (**not recommended** for prod). |
| `MANTRA_AGENT_PORT`         | `5555`        | TCP port.                                                               |
| `MANTRA_AGENT_FORCE_CPU`    | `0`           | Set to `1` to skip GPU detection and run CPU-only.                       |
| `MANTRA_AGENT_CORS_EXTRA`   | _(empty)_     | Comma-separated extra CORS origins for dev (e.g. `http://localhost:5173`). |
| `MANTRA_AGENT_LOG_LEVEL`    | `INFO`        | `DEBUG`, `INFO`, `WARNING`, `ERROR`.                                    |
| `MANTRA_AGENT_FFMPEG`       | _(empty)_     | (Phase 2) Override path to `ffmpeg.exe`. Empty = auto-discover.         |
| `MANTRA_AGENT_YTDLP`        | `yt-dlp`      | (Phase 2) Path or PATH-resolved name for `yt-dlp` binary.                |

---

## Endpoints (Phase 1)

### `GET /api/health`

Returns agent status. Used by the JS smart-detect on the web app.

```json
{
  "status": "healthy",
  "is_local_agent": true,
  "agent_version": "1.2.0",
  "rembg_available": true,
  "mode": "GPU"
}
```

### `POST /api/remove-bg`

JSON body: `{ "image": "<base64 PNG/JPG>", "model": "u2net" }`

Response: `{ "success": true, "result": "<base64 PNG with alpha>", "processing_time": 0.42 }`

> The `model` field is accepted for VPS compat but currently always uses `u2net`.

### `POST /api/remove-bg/file`

Multipart form upload: `file=<png/jpg bytes>`. Same response shape as the JSON variant.

---

## Smoke test

Verifies the agent is up, healthy, and can round-trip an image through both endpoints.

```bash
# Terminal 1 — start the agent
python main.py

# Terminal 2 — run the smoke test
python tests/test_smoke.py
# or:  python -m tests.test_smoke
```

The test generates a 256x256 PNG in-memory (coral circle on white), POSTs it to both `/api/remove-bg` endpoints, and saves the cut-out PNGs to `tests/output/`. Exits `0` on pass, `1` on any failure with a clear `FAILED:` message.

---

## GPU support

By default the agent tries CUDA (NVIDIA) and falls back to CPU. The `mode` field of `/api/health` reports which provider is active.

To enable real GPU acceleration, swap `onnxruntime` for `onnxruntime-gpu`:

```bash
pip uninstall onnxruntime -y
pip install onnxruntime-gpu==1.23.2
```

Requirements:
- NVIDIA driver compatible with CUDA 12.x
- CUDA 12.x runtime installed (or bundled DLLs on PATH)

The first call after startup loads `u2net.onnx` onto whichever provider is active; subsequent calls reuse the cached session.

---

## Architecture overview

```
Browser (mantra.majutrah.co.id)
     │
     │ JS smart-detect
     │   ├── if agent up:   http://127.0.0.1:5555/api/remove-bg
     │   └── else (fallback): api/proxy.php → VPS:8000
     ▼
Mantra Agent (this repo)
     ├── FastAPI on localhost:5555
     ├── handlers/        — endpoint handlers (mirror VPS contract)
     ├── models/          — Pydantic schemas (mirror VPS contract)
     └── utils/monkey_patch.py  — torchvision compat shim (used in Phase 2)
```

The agent intentionally mirrors the VPS contract so the PHP app stays untouched.

---

## Reference

- VPS source of truth: `D:/CLAUDE/MANTRA CREATIVE/reference/vps-gpu/mantra-fastapi/gpu_api_server.py`
- Audit notes: `D:/CLAUDE/MANTRA CREATIVE/docs/audit_vps-gpu.md`
- Master plan: `D:/CLAUDE/MANTRA CREATIVE/docs/MASTER_PLAN.md`

---

## Phase 2 roadmap

1. **Enable Phase 2 deps** in `requirements.txt` (currently commented):
   - `gfpgan==1.3.8`, `basicsr==1.4.2`, `facexlib==0.3.0`
   - `opencv-python==4.8.1.78`
   - `torch==2.6.0`, `torchvision==0.21.0`
   - `yt-dlp==2026.3.17`, `bgutil-ytdlp-pot-provider==1.3.1`
2. **`handlers/enhance.py`** — mirror VPS `/api/enhance` + `/api/enhance/file` (GFPGAN face restoration).
3. **`handlers/youtube.py`** — `/api/youtube-mp3`, `/api/youtube-info`, `/api/youtube-batch` (yt-dlp + ffmpeg).
4. **`handlers/merge.py`** — `/api/full-album` (multi-track concat with ffmpeg).
5. **Bundle** `ffmpeg.exe` + Node.js into the Windows installer for one-click setup.
