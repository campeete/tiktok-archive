# Deploy / install

## macOS (Apple Silicon, M1+)

```bash
# Prerequisites
brew install python@3.12 ffmpeg ollama
brew services start ollama

# Project
git clone git@github.com:campeete/tiktok-archive.git
cd tiktok-archive
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[analyze-mac]"

# Models
ollama pull qwen2.5:7b      # ~4.7 GB

# Config
cp .env.example .env
cp tags_vocabulary.example.yaml tags_vocabulary.yaml

# Verify
tiktok-archive check
```

If `whisper: MISSING` — make sure you installed with `[analyze-mac]` and that you're on arm64 (`uname -m` should print `arm64`).

If `ollama: MISSING` — `brew services list | grep ollama` should show `started`. If not, `brew services restart ollama`.

If `ffmpeg` is reported missing during analyze — `brew install ffmpeg`.

## Windows + WSL2 (NVIDIA RTX 4070)

```bash
# Inside WSL2 Ubuntu
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip ffmpeg

# CUDA inside WSL2: follow NVIDIA's WSL2 CUDA Toolkit guide.
# Verify with: nvidia-smi (should show your 4070)

# Ollama: install in Windows side or WSL2 side; either works.
# In WSL2:
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl start ollama
ollama pull qwen2.5:7b

# Project
git clone git@github.com:campeete/tiktok-archive.git
cd tiktok-archive
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[analyze-cuda]"
tiktok-archive check
```

If `whisper` reports CPU instead of CUDA — verify `python -c "import torch; print(torch.cuda.is_available())"` returns `True`. If not, your torch install isn't seeing CUDA. Reinstall torch from PyTorch's WSL2 instructions.

## Linux (CPU-only)

Slow but functional. Install with `[analyze-cuda]` (faster-whisper handles both CUDA and CPU) and `tiktok-archive check` will report `cpu`. Expect transcription to take roughly 1-2× the video duration on a modern x86 CPU.

## Persistent worker (macOS launchd)

Use `scripts/com.tiktok-archive.worker.plist`. Edit the `<string>` paths to point at your project location, then:

```bash
cp scripts/com.tiktok-archive.worker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tiktok-archive.worker.plist
launchctl list | grep tiktok-archive
```

To stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.tiktok-archive.worker.plist
```

## Persistent worker (systemd, Linux)

Create `/etc/systemd/system/tiktok-archive-worker.service`:

```ini
[Unit]
Description=tiktok-archive worker
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/tiktok-archive
Environment="PATH=/home/YOUR_USER/tiktok-archive/venv/bin"
ExecStart=/home/YOUR_USER/tiktok-archive/venv/bin/tiktok-archive worker
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tiktok-archive-worker
sudo systemctl status tiktok-archive-worker
```

## Cron-based sync (any Unix)

If you don't want a long-running worker, use cron to drain the queue periodically:

```cron
# Sync all due creators every hour, then drain up to 20 jobs
0 * * * * cd /path/to/tiktok-archive && /path/to/venv/bin/tiktok-archive creator sync --all >> data/logs/cron.log 2>&1
5 * * * * cd /path/to/tiktok-archive && /path/to/venv/bin/tiktok-archive worker --once --max-jobs 20 >> data/logs/cron.log 2>&1
```

`scripts/sync-and-drain.sh` is the same logic in shell-script form.

## Cross-machine setup (Mac for queries, 4070 PC for inference)

1. Install on both machines.
2. On the heavy machine (4070 PC): set `TT_STORAGE_BACKEND=r2`, run the worker continuously.
3. On the laptop: same `.env` (same R2 credentials, same DB path via Syncthing or similar). The web UI runs locally; reads come from local cache, fall back to R2 if missing.
4. Schedule `tiktok-archive backup-db` on the heavy machine nightly.

This is the Phase 1.6 cross-machine pattern. It assumes both machines can reach R2; if they can't, fall back to a shared NAS path for `data/`.
