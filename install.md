# Installing Seam

Written so a person **or an agent** (Claude Code, Codex, …) can follow it top to bottom. Every step ends with a
command that proves it worked. The last step, `seam doctor`, checks everything at once.

## 0. What you need

| | Minimum | Tested on |
|---|---|---|
| GPU | NVIDIA, 24 GB VRAM (32 GB recommended) | RTX 5090 32 GB, driver 617 |
| RAM | 64 GB (model offloading) | 128 GB |
| Disk | ~50 GB free for models (~90 GB with the optional LTX-2.3 set) | NVMe |
| OS | Windows 10/11 or Linux | Windows 11 |
| Software | ComfyUI ≥ 0.41.3, Python 3.12, `uv`, Node 20+, ffmpeg, Git | ComfyUI Desktop 0.41.3 |
| Account | Hugging Face account (LTX-2.5 is gated) | — |

## 1. ComfyUI

Use **ComfyUI Desktop** (https://www.comfy.org/download) or a manual ComfyUI ≥ 0.41.3 install. No custom nodes
are needed: Seam only uses core nodes. Start it and check:

```bash
curl http://127.0.0.1:8188/system_stats      # Desktop may use port 8000 — use whatever your ComfyUI shows
```

## 2. Models (~44 GB, open weights)

1. Accept the LTX-2.5 license at https://huggingface.co/Lightricks/LTX-2.5 (gated) and log in: `hf auth login`
   (or set `HF_TOKEN`).
2. Download into your ComfyUI `models` folder (resumable, size-checked):

```bash
git clone https://github.com/jdnichollsc/seam && cd seam
uv run python scripts/download_models.py --models-dir "<path to ComfyUI>/models" --only l25
```

`--only l25` is everything the launch effects use (LTX-2.5 + the Gemma-4 E2B shot reader). LTX-2.3 + its transition
LoRA (~39 GB more) is optional: no launch effect uses it today; drop `--only l25` to fetch it too. `seam doctor` lists it
as optional.

Desktop users: the models folder is in Desktop's settings (e.g. `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Shared\models`).
Restart ComfyUI afterwards so it sees the new files.

## 3. Comfy API proxy (Comfy Developer Platform, v2 API)

Seam talks to ComfyUI through the official **Comfy SDK**, which speaks the Comfy API v2. Locally that API is
served by the official proxy in front of ComfyUI:

```bash
uv tool install comfy-api-proxy
comfy-api-proxy start --comfyui http://127.0.0.1:8188     # serves http://127.0.0.1:8189, local only
curl http://127.0.0.1:8189/api/v2/health
```

Using **Comfy Cloud** or a deployed Comfy API instead: skip this step and set `COMFY_BASE_URL` and
`COMFY_API_KEY` (server-side only — never in the browser).

## 4. ffmpeg

Windows: `winget install --id Gyan.FFmpeg -e` · macOS: `brew install ffmpeg` · Linux: `apt install ffmpeg`.
Open a new terminal, then `ffprobe -version`.

## 5. Seam

```bash
uv sync                                   # Python deps (comfy-sdk, fastapi, mcp, …)
cd web && npm install && npm run build && cd ..
uv run seam doctor                        # must print "ready"
uv run seam serve                         # http://127.0.0.1:7860
```

Environment variables (all optional): `COMFY_BASE_URL` (default `http://127.0.0.1:8189`), `COMFY_API_KEY`,
`SEAM_COMFYUI_URL` (for `seam doctor`'s model check, default `http://127.0.0.1:8188`), `SEAM_PORT` (7860),
`SEAM_DATA_DIR` (`~/.seam`: uploads, outputs, job database).

Smoke test without the UI:

```bash
uv run seam presets
uv run seam make first.png last.png --preset smoke -o smoke.mp4
```

## 6. Use it from Claude (skill + MCP)

Keep `seam serve` running; the MCP server is a client of it, so agent jobs appear in the web gallery.

**Claude Code** (plugin = skill + MCP server):

```bash
claude plugin marketplace add <path-or-git-url-of-this-repo>
claude plugin install seam
```

or, without the plugin system: `claude mcp add seam -- uv run --project <path/to/seam> seam mcp` and copy
`skills/seam/` into `~/.claude/skills/`.

**Claude Desktop** — add to `claude_desktop_config.json` (Settings → Developer → Edit config), then restart:

```json
{ "mcpServers": { "seam": { "command": "uv", "args": ["run", "--project", "C:/path/to/seam", "seam", "mcp"],
  "env": { "SEAM_URL": "http://127.0.0.1:7860" } } } }
```

**Codex** — `~/.codex/config.toml`:

```toml
[mcp_servers.seam]
command = "uv"
args = ["run", "--project", "/path/to/seam", "seam", "mcp"]
env = { SEAM_URL = "http://127.0.0.1:7860" }
```

Check: ask Claude "make a smoke transition from first.png to last.png with Seam".

## Troubleshooting

- `seam doctor` prints a fix for every failed check.
- A job stays "Waiting for the GPU": the ComfyUI queue is shared — another workflow is running.
- Out of memory: close other GPU apps; ComfyUI offloads to RAM, so 64 GB+ RAM matters.
