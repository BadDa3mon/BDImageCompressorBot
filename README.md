# BD Image Compressor Bot

Telegram bot (aiogram 3) for compressing images and ZIP archives with images.

## Features

- Supports direct image uploads and ZIP archives.
- Preserves ZIP folder structure.
- Converts PNG to JPEG (lossy) for stronger size reduction.
- Supports local Telegram Bot API (`LOCAL_BOT_API_URL`) for larger files.
- Includes runtime progress logs and ETA updates for ZIP processing.
- Stores input/output artifacts by unique job ID for easier traceability.

## Tech Stack

- Python 3.10+
- `aiogram` 3.x
- `Pillow`
- `python-dotenv`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set your bot token in `.env`:

```env
BOT_TOKEN=your_telegram_bot_token
```

## Run (dev)

```bash
python bot.py
```

## Run (systemd)

Service file in this repo:
- `bdimagecompressorbot.service`

Typical commands:

```bash
sudo cp /opt/Bots/BDImageCompressorBot/bdimagecompressorbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bdimagecompressorbot.service
sudo systemctl status bdimagecompressorbot.service
```

## Local Telegram Bot API (Docker)

Compose file:
- `docker-compose.yml`

Run:

```bash
docker compose up -d
```

Set in `.env`:

```env
LOCAL_BOT_API_URL=http://127.0.0.1:8082
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

## Supported Inputs

- Image documents: `.jpg`, `.jpeg`, `.png`
- Telegram `photo` messages
- `.zip` archives

## Important Environment Variables

- `JPEG_QUALITY` - JPEG quality (1..95)
- `JPEG_PROGRESSIVE`, `JPEG_OPTIMIZE`
- `PNG_TO_JPEG` - convert PNG to JPEG (`1`/`0`)
- `PNG_JPEG_BG` - background for transparent PNG (`white` or `black`)
- `MAX_SIZE` - optional resize by longest side (`0` disables)
- `ZIP_IMAGE_LIMIT` - max number of image files in ZIP
- `ZIP_FILE_LIMIT` - max number of files in ZIP
- `ZIP_MAX_EXTRACT_MB` - max uncompressed ZIP size
- `MAX_INPUT_FILE_MB` - max incoming file size accepted by the bot
- `TELEGRAM_DOWNLOAD_LIMIT_MB` - Telegram-side size guard
- `BOT_API_REQUEST_TIMEOUT_SEC`, `BOT_API_DOWNLOAD_TIMEOUT_SEC`, `BOT_API_GET_FILE_RETRIES`
- `BOT_API_SERVER_FILES_DIR`, `BOT_API_LOCAL_FILES_DIR` - local Bot API path mapping
- `LOG_DIR` - bot log directory (`logs`)
- `STORAGE_DIR` - base directory for job artifacts (`artifacts`)

## Job Artifacts

Each processed task is stored under:

`artifacts/<job_id>/`

Structure:

- `input/` - original uploaded file(s)
- `output/` - processed result file(s)
- `meta/job.txt` - task metadata (chat/user/time)

## Logs

- Bot logs: `logs/bot.log`
- systemd logs:

```bash
journalctl -u bdimagecompressorbot.service -f
```
