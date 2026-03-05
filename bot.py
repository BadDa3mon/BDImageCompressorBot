import asyncio
import configparser
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.telegram import SimpleFilesPathWrapper
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from aiogram.utils.chat_action import ChatActionSender
from dotenv import load_dotenv

from compressor import (
    SUPPORTED_EXT,
    compress_image_file,
    is_supported_image,
    load_config_from_env,
    output_path_for_source,
)

load_dotenv()

def _clean_env(v: Optional[str]) -> str:
    if v is None:
        return ""
    return v.split("#", 1)[0].strip()


def _int_env(name: str, default: int) -> int:
    raw = _clean_env(os.getenv(name))
    if not raw:
        return default
    return int(raw)


def _str_env(name: str, default: str = "") -> str:
    raw = _clean_env(os.getenv(name))
    return raw if raw else default


BOT_TOKEN = _str_env("BOT_TOKEN")
LOCAL_BOT_API_URL = _str_env("LOCAL_BOT_API_URL")
ZIP_IMAGE_LIMIT = _int_env("ZIP_IMAGE_LIMIT", 2000)
ZIP_FILE_LIMIT = _int_env("ZIP_FILE_LIMIT", 10000)
ZIP_MAX_EXTRACT_MB = _int_env("ZIP_MAX_EXTRACT_MB", 512)
ZIP_MAX_EXTRACT_BYTES = ZIP_MAX_EXTRACT_MB * 1024 * 1024
MAX_INPUT_FILE_MB = _int_env("MAX_INPUT_FILE_MB", 100)
MAX_INPUT_FILE_BYTES = MAX_INPUT_FILE_MB * 1024 * 1024
TELEGRAM_DOWNLOAD_LIMIT_MB = _int_env("TELEGRAM_DOWNLOAD_LIMIT_MB", 2000 if LOCAL_BOT_API_URL else 20)
TELEGRAM_DOWNLOAD_LIMIT_BYTES = TELEGRAM_DOWNLOAD_LIMIT_MB * 1024 * 1024
BOT_API_REQUEST_TIMEOUT_SEC = _int_env("BOT_API_REQUEST_TIMEOUT_SEC", 900)
BOT_API_DOWNLOAD_TIMEOUT_SEC = _int_env("BOT_API_DOWNLOAD_TIMEOUT_SEC", 1800)
BOT_API_GET_FILE_RETRIES = _int_env("BOT_API_GET_FILE_RETRIES", 3)
BOT_API_SERVER_FILES_DIR = _str_env("BOT_API_SERVER_FILES_DIR", "/var/lib/telegram-bot-api")
BOT_API_LOCAL_FILES_DIR = Path(_str_env("BOT_API_LOCAL_FILES_DIR", "/opt/Bots/BDImageCompressorBot/data")).resolve()
DATA_CONF_PATH = Path(_str_env("DATA_CONF_PATH", "data.conf"))
LOG_DIR = Path(_str_env("LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "bot.log"
STORAGE_DIR = Path(_str_env("STORAGE_DIR", "artifacts"))

HELP_TEXT = (
    "Я пережимаю PNG/JPG и ZIP-архивы с картинками.\n\n"
    "• Пришли картинку (лучше как файл, не photo — Telegram иногда сжимает заранее)\n"
    "• Или пришли ZIP — я сохраню структуру папок и верну *_compressed.zip*\n\n"
    "Поддерживаемые: .jpg .jpeg .png\n"
)


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tg-compressor")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    return logger


log = setup_logging()
router = Router()
DATA_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def upsert_user_to_data_conf(msg: Message) -> None:
    if not msg.from_user or not msg.chat:
        return

    u = msg.from_user
    c = msg.chat
    section = f"user:{u.id}"

    async with DATA_LOCK:
        cfg = configparser.ConfigParser()
        if DATA_CONF_PATH.exists():
            cfg.read(DATA_CONF_PATH, encoding="utf-8")

        if not cfg.has_section(section):
            cfg.add_section(section)
            cfg.set(section, "first_seen", _now_iso())

        cfg.set(section, "last_seen", _now_iso())
        cfg.set(section, "chat_id", str(c.id))
        cfg.set(section, "username", u.username or "")
        cfg.set(section, "first_name", u.first_name or "")
        cfg.set(section, "last_name", u.last_name or "")

        DATA_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DATA_CONF_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)


def _safe_filename(name: str) -> str:
    name = (name or "").strip().replace("\x00", "")
    name = re.sub(r"[^\w\-.()@\s]+", "_", name, flags=re.UNICODE)
    return name[:180] if len(name) > 180 else (name or "file")


def _compressed_name(original: str) -> str:
    p = Path(original)
    return f"{p.stem}_compressed{p.suffix}"


def _compressed_zip_name(original: str) -> str:
    p = Path(original)
    if p.suffix.lower() == ".zip":
        return f"{p.stem}_compressed.zip"
    return f"{p.name}_compressed.zip"


def _job_prefix(job_id: str) -> str:
    return f"Задача {job_id}"


def _job_storage_dir(job_id: str) -> Path:
    return STORAGE_DIR / job_id


def _store_file(job_id: str, section: str, src: Path, name: Optional[str] = None) -> Optional[Path]:
    """
    Сохраняет копию файла в artifacts/<job_id>/<section>/.
    Не роняет основной пайплайн при ошибках.
    """
    try:
        target_dir = _job_storage_dir(job_id) / section
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (name or src.name)
        shutil.copy2(src, target)
        return target
    except Exception as e:
        log.warning("[%s] Failed to store artifact %s: %s", job_id, src, e)
        return None


def _store_text(job_id: str, section: str, filename: str, content: str) -> None:
    try:
        target_dir = _job_storage_dir(job_id) / section
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_text(content, encoding="utf-8")
    except Exception as e:
        log.warning("[%s] Failed to store text artifact %s/%s: %s", job_id, section, filename, e)


def _fmt_eta(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}ч {m}м {s}с"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


async def _send_or_edit_status(msg: Message, status_msg: Optional[Message], text: str) -> Message:
    if status_msg is None:
        return await msg.answer(text)
    try:
        return await status_msg.edit_text(text)
    except Exception:
        return await msg.answer(text)


def _zip_safe_extract(zf: zipfile.ZipFile, dst_dir: Path) -> List[Path]:
    out_paths: List[Path] = []
    base_dir = dst_dir.resolve()

    for member in zf.infolist():
        if member.is_dir():
            continue

        member_name = member.filename.replace("\\", "/")
        if member_name.startswith("/") or member_name.startswith("../") or "/../" in member_name:
            continue

        target_path = (dst_dir / member_name).resolve()
        if not target_path.is_relative_to(base_dir):
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member, "r") as src, open(target_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)

        out_paths.append(target_path)

    return out_paths


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in src_dir.rglob("*"):
            if p.is_file():
                z.write(p, arcname=p.relative_to(src_dir).as_posix())


async def _download_file(bot: Bot, file_id: str, dst_path: Path, job_id: str) -> None:
    for attempt in range(1, BOT_API_GET_FILE_RETRIES + 1):
        try:
            t0 = time.monotonic()
            log.info("[%s] get_file attempt=%s started", job_id, attempt)
            file_info = await bot.get_file(file_id, request_timeout=BOT_API_REQUEST_TIMEOUT_SEC)
            log.info("[%s] get_file attempt=%s done in %.2fs", job_id, attempt, time.monotonic() - t0)

            t1 = time.monotonic()
            log.info("[%s] download started -> %s", job_id, dst_path.name)
            with open(dst_path, "wb") as out:
                await bot.download(file_info, destination=out, timeout=BOT_API_DOWNLOAD_TIMEOUT_SEC)
            log.info("[%s] download done in %.2fs", job_id, time.monotonic() - t1)
            return
        except TelegramNetworkError as e:
            # На локальном Bot API getFile для больших файлов может отвечать долго.
            if "request timeout" in str(e).lower() and attempt < BOT_API_GET_FILE_RETRIES:
                log.warning("[%s] get_file timeout on attempt=%s, retrying", job_id, attempt)
                await asyncio.sleep(min(5 * attempt, 15))
                continue
            raise RuntimeError(
                "Локальный Bot API не ответил вовремя при подготовке файла. "
                "Попробуйте позже или увеличьте BOT_API_REQUEST_TIMEOUT_SEC."
            ) from e
        except TelegramBadRequest as e:
            if "file is too big" in str(e).lower():
                raise RuntimeError(
                    f"Файл слишком большой для скачивания ботом через Telegram API (лимит около {TELEGRAM_DOWNLOAD_LIMIT_MB} MB)."
                ) from e
            raise
    raise RuntimeError("Не удалось скачать файл через локальный Bot API.")


async def _process_single_image(job_id: str, bot: Bot, msg: Message, file_id: str, filename: str) -> Tuple[int, int]:
    cfg = load_config_from_env()
    with tempfile.TemporaryDirectory(prefix="tgcmp_") as td:
        td_path = Path(td)
        src_path = td_path / _safe_filename(filename)
        dst_path = Path(output_path_for_source(str(src_path), str(td_path / _compressed_name(src_path.name)), cfg))

        async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
            await _download_file(bot, file_id, src_path, job_id)
        _store_file(job_id, "input", src_path)

        t0 = time.monotonic()
        log.info("[%s] compress started (single): %s", job_id, src_path.name)
        src_b, dst_b = await asyncio.to_thread(compress_image_file, str(src_path), str(dst_path), cfg)
        log.info("[%s] compress done in %.2fs", job_id, time.monotonic() - t0)
        _store_file(job_id, "output", dst_path)

        caption = f"{_job_prefix(job_id)} выполнена ✅\n1 фото сжато\n{src_b/1024:.1f} KB → {dst_b/1024:.1f} KB"
        await msg.answer_document(document=FSInputFile(str(dst_path), filename=dst_path.name), caption=caption)
        return (1, 1)


async def _process_zip(
    job_id: str,
    bot: Bot,
    msg: Message,
    file_id: str,
    filename: str,
    status_msg: Optional[Message] = None,
) -> Tuple[int, int]:
    cfg = load_config_from_env()
    with tempfile.TemporaryDirectory(prefix="tgzip_") as td:
        td_path = Path(td)
        src_zip = td_path / _safe_filename(filename if filename.lower().endswith(".zip") else f"{filename}.zip")

        async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
            await _download_file(bot, file_id, src_zip, job_id)
        _store_file(job_id, "input", src_zip)

        extract_dir = td_path / "in"
        out_dir = td_path / "out"
        extract_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(src_zip, "r") as zf:
            t_unzip = time.monotonic()
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > ZIP_FILE_LIMIT:
                raise RuntimeError(f"Слишком много файлов в архиве: {len(infos)} (лимит {ZIP_FILE_LIMIT}).")

            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > ZIP_MAX_EXTRACT_BYTES:
                raise RuntimeError(
                    f"Архив слишком большой после распаковки: {total_uncompressed / 1024 / 1024:.1f} MB "
                    f"(лимит {ZIP_MAX_EXTRACT_MB} MB)."
                )

            extracted_files = _zip_safe_extract(zf, extract_dir)
            log.info("[%s] unzip done in %.2fs, files=%s", job_id, time.monotonic() - t_unzip, len(extracted_files))

        for p in extracted_files:
            rel = p.relative_to(extract_dir)
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)

        images = [p for p in extracted_files if is_supported_image(str(p))]
        total_images = len(images)
        if total_images > ZIP_IMAGE_LIMIT:
            raise RuntimeError(f"Слишком много картинок в архиве: {total_images} (лимит {ZIP_IMAGE_LIMIT}).")

        processed = 0
        total_src = 0
        total_dst = 0

        zip_work_started = time.monotonic()
        for idx, src_img in enumerate(images, start=1):
            rel = src_img.relative_to(extract_dir)
            dst_img_orig = out_dir / rel
            dst_img = Path(output_path_for_source(str(src_img), str(dst_img_orig), cfg))
            try:
                t_img = time.monotonic()
                s, d = await asyncio.to_thread(compress_image_file, str(src_img), str(dst_img), cfg)
                one_elapsed = time.monotonic() - t_img
                if dst_img != dst_img_orig and dst_img_orig.exists():
                    dst_img_orig.unlink(missing_ok=True)
                processed += 1
                total_src += s
                total_dst += d
                log.info("[%s] compressed zip image %s in %.2fs", job_id, rel.as_posix(), one_elapsed)

                avg_per_img = (time.monotonic() - zip_work_started) / max(idx, 1)
                remaining = max(total_images - idx, 0)
                eta_text = _fmt_eta(avg_per_img * remaining)
                status_msg = await _send_or_edit_status(
                    msg,
                    status_msg,
                    f"{_job_prefix(job_id)} в работе ⏳\n"
                    f"Сжато: {idx}/{total_images}\n"
                    f"Последний файл: {one_elapsed:.1f}с\n"
                    f"Осталось примерно: {eta_text}",
                )
            except Exception as e:
                log.warning("[%s] Failed to compress %s: %s", job_id, src_img, e)

        out_zip = td_path / _compressed_zip_name(src_zip.name)
        t_pack = time.monotonic()
        _zip_dir(out_dir, out_zip)
        log.info("[%s] zip pack done in %.2fs", job_id, time.monotonic() - t_pack)
        _store_file(job_id, "output", out_zip)

        caption = (
            f"{_job_prefix(job_id)} выполнена ✅\n"
            f"{processed} фото сжато (из {total_images})\n"
            f"Суммарно: {total_src/1024:.1f} KB → {total_dst/1024:.1f} KB\n"
            f"Ниже архив."
        )
        await msg.answer_document(document=FSInputFile(str(out_zip), filename=out_zip.name), caption=caption)
        return (processed, total_images)


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await upsert_user_to_data_conf(message)
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await upsert_user_to_data_conf(message)
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@router.message(F.photo | F.document)
async def media_handler(message: Message, bot: Bot) -> None:
    await upsert_user_to_data_conf(message)

    job_id = str(uuid.uuid4())
    user = message.from_user
    chat = message.chat
    log.info(
        "[%s] New job from chat=%s user=%s (@%s)",
        job_id,
        chat.id if chat else None,
        user.id if user else None,
        user.username if user else None,
    )

    status_msg: Optional[Message] = await _send_or_edit_status(
        message, None, f"{_job_prefix(job_id)} принята. Выполняется... ⏳"
    )
    _store_text(
        job_id,
        "meta",
        "job.txt",
        (
            f"job_id={job_id}\n"
            f"created_at={_now_iso()}\n"
            f"chat_id={chat.id if chat else ''}\n"
            f"user_id={user.id if user else ''}\n"
            f"username={user.username if user else ''}\n"
        ),
    )

    try:
        if message.photo:
            photo = message.photo[-1]
            log.info(
                "[%s] photo received: file_id=%s file_unique_id=%s size=%s",
                job_id,
                photo.file_id,
                photo.file_unique_id,
                photo.file_size,
            )
            if photo.file_size and photo.file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
                await _send_or_edit_status(
                    message,
                    status_msg,
                    f"{_job_prefix(job_id)}: Telegram API не дает скачать такие файлы ботом (>{TELEGRAM_DOWNLOAD_LIMIT_MB} MB).",
                )
                return
            if photo.file_size and photo.file_size > MAX_INPUT_FILE_BYTES:
                await _send_or_edit_status(
                    message,
                    status_msg,
                    f"{_job_prefix(job_id)}: файл слишком большой ({photo.file_size / 1024 / 1024:.1f} MB), лимит {MAX_INPUT_FILE_MB} MB.",
                )
                return

            filename = f"photo_{photo.file_unique_id}.jpg"
            processed, total = await _process_single_image(job_id, bot, message, photo.file_id, filename)
            await _send_or_edit_status(message, status_msg, f"{_job_prefix(job_id)} выполнена ✅\n{processed} фото сжато")
            log.info("[%s] Done: processed=%s total=%s (photo)", job_id, processed, total)
            return

        if message.document:
            doc = message.document
            log.info(
                "[%s] document received: name=%s mime=%s file_id=%s file_unique_id=%s size=%s",
                job_id,
                doc.file_name,
                doc.mime_type,
                doc.file_id,
                doc.file_unique_id,
                doc.file_size,
            )
            if doc.file_size and doc.file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
                await _send_or_edit_status(
                    message,
                    status_msg,
                    f"{_job_prefix(job_id)}: Telegram API не дает скачать такие файлы ботом (>{TELEGRAM_DOWNLOAD_LIMIT_MB} MB).",
                )
                return
            if doc.file_size and doc.file_size > MAX_INPUT_FILE_BYTES:
                await _send_or_edit_status(
                    message,
                    status_msg,
                    f"{_job_prefix(job_id)}: файл слишком большой ({doc.file_size / 1024 / 1024:.1f} MB), лимит {MAX_INPUT_FILE_MB} MB.",
                )
                return

            filename = doc.file_name or f"file_{doc.file_unique_id}"
            ext = Path(filename.lower()).suffix

            if ext == ".zip":
                processed, total = await _process_zip(job_id, bot, message, doc.file_id, filename, status_msg)
                await _send_or_edit_status(message, status_msg, f"{_job_prefix(job_id)} выполнена ✅\n{processed} фото сжато")
                log.info("[%s] Done: processed=%s total=%s (zip)", job_id, processed, total)
                return

            if ext in SUPPORTED_EXT or (doc.mime_type and doc.mime_type.startswith("image/")):
                if ext not in SUPPORTED_EXT and doc.mime_type:
                    ext = ".png" if doc.mime_type.endswith("png") else ".jpg"
                    filename = f"{Path(filename).stem}{ext}"

                processed, total = await _process_single_image(job_id, bot, message, doc.file_id, filename)
                await _send_or_edit_status(message, status_msg, f"{_job_prefix(job_id)} выполнена ✅\n{processed} фото сжато")
                log.info("[%s] Done: processed=%s total=%s (doc image)", job_id, processed, total)
                return

            await _send_or_edit_status(message, status_msg, f"{_job_prefix(job_id)}: это не PNG/JPG и не ZIP")
            log.info("[%s] Unsupported document: %s mime=%s", job_id, filename, doc.mime_type)
            return

        await _send_or_edit_status(message, status_msg, f"{_job_prefix(job_id)}: пришли PNG/JPG или ZIP")
        log.info("[%s] No supported content in message", job_id)

    except Exception as e:
        log.exception("[%s] Failed job: %s", job_id, e)
        await _send_or_edit_status(
            message, status_msg, f"{_job_prefix(job_id)} не выполнена ❌\nПричина: внутренняя ошибка обработки."
        )


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in .env")

    session = None
    if LOCAL_BOT_API_URL:
        BOT_API_LOCAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(
                LOCAL_BOT_API_URL,
                is_local=True,
                wrap_local_file=SimpleFilesPathWrapper(
                    server_path=Path(BOT_API_SERVER_FILES_DIR),
                    local_path=BOT_API_LOCAL_FILES_DIR,
                ),
            ),
            timeout=BOT_API_REQUEST_TIMEOUT_SEC,
        )

    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()
    dp.include_router(router)

    if LOCAL_BOT_API_URL:
        log.info("Bot started (aiogram, local Bot API: %s)", LOCAL_BOT_API_URL)
    else:
        log.info("Bot started (aiogram)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
