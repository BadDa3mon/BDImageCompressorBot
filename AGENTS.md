# AGENTS.md

Guidelines for contributors and automation agents working in this repository.

## Project Scope

This project is a Telegram image-compression bot:
- runtime: `aiogram` 3
- image pipeline: `Pillow`
- local Bot API support via Docker Compose
- production run via `systemd`

## Key Paths

- Main bot: `bot.py`
- Compression logic: `compressor.py`
- Environment template: `.env.example`
- Bot logs: `logs/bot.log`
- Job artifacts: `artifacts/<job_id>/`
- Local Bot API compose: `docker-compose.yml`
- systemd service: `bdimagecompressorbot.service`

## Operational Rules

- Keep edits minimal and focused.
- Preserve existing behavior unless explicitly changing requirements.
- Do not hardcode secrets/tokens in code or docs.
- Prefer environment variables for runtime configuration.
- When changing compression behavior, keep both single-image and ZIP flows consistent.
- Keep artifact storage behavior consistent (`input/`, `output/`, `meta/` per `job_id`).

## Local Bot API Notes

- `LOCAL_BOT_API_URL` should point to local endpoint (for example `http://127.0.0.1:8082`).
- Local mode may return file paths; bot uses path mapping config:
  - `BOT_API_SERVER_FILES_DIR`
  - `BOT_API_LOCAL_FILES_DIR`
- If download issues occur, verify:
  - container is running
  - bot can reach local API
  - filesystem permissions in `data/`

## Testing Checklist

After changes:

1. Run syntax check:
   - `python3 -m py_compile bot.py compressor.py`
2. Restart service:
   - `sudo systemctl restart bdimagecompressorbot.service`
3. Validate flows in Telegram:
   - small JPEG document
   - PNG input (confirm conversion and size reduction)
   - ZIP with multiple images
4. Watch logs:
   - `journalctl -u bdimagecompressorbot.service -f`
   - `tail -f logs/bot.log`

## Style

- Use clear, concise English in comments and docs.
- Keep code ASCII unless existing files require otherwise.
