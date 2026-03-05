import os
import shutil
from dataclasses import dataclass
from typing import Tuple
from PIL import Image, ImageOps

SUPPORTED_EXT = {".jpg", ".jpeg", ".png"}


@dataclass
class CompressConfig:
    jpeg_quality: int = 80
    jpeg_progressive: bool = True
    jpeg_optimize: bool = True

    png_optimize: bool = True
    png_compress_level: int = 9
    png_quantize: bool = False  # lossy for png (if no alpha)
    png_to_jpeg: bool = True
    png_jpeg_bg: str = "white"  # white|black

    max_size: int = 0  # 0 = no resize


def _clean_env(v: str) -> str:
    if v is None:
        return ""
    return v.split("#", 1)[0].strip()


def _bool_env(v: str, default: bool) -> bool:
    v = _clean_env(v)
    if not v:
        return default
    return v in {"1", "true", "TRUE", "yes", "YES", "on", "ON"}


def _int_env(name: str, default: int) -> int:
    raw = _clean_env(os.getenv(name))
    if not raw:
        return default
    return int(raw)


def load_config_from_env() -> CompressConfig:
    return CompressConfig(
        jpeg_quality=_int_env("JPEG_QUALITY", 80),
        jpeg_progressive=_bool_env(os.getenv("JPEG_PROGRESSIVE"), True),
        jpeg_optimize=_bool_env(os.getenv("JPEG_OPTIMIZE"), True),
        png_optimize=_bool_env(os.getenv("PNG_OPTIMIZE"), True),
        png_compress_level=_int_env("PNG_COMPRESS_LEVEL", 9),
        png_quantize=_bool_env(os.getenv("PNG_QUANTIZE"), False),
        png_to_jpeg=_bool_env(os.getenv("PNG_TO_JPEG"), True),
        png_jpeg_bg=_clean_env(os.getenv("PNG_JPEG_BG")).lower() or "white",
        max_size=_int_env("MAX_SIZE", 0),
    )


def is_supported_image(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in SUPPORTED_EXT


def _maybe_resize(img: Image.Image, max_size: int) -> Image.Image:
    if not max_size or max_size <= 0:
        return img
    w, h = img.size
    long_side = max(w, h)
    if long_side <= max_size:
        return img
    scale = max_size / float(long_side)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return img.resize((new_w, new_h), Image.LANCZOS)


def output_path_for_source(src_path: str, dst_path: str, cfg: CompressConfig) -> str:
    ext = os.path.splitext(src_path)[1].lower()
    if ext == ".png" and cfg.png_to_jpeg:
        base, _ = os.path.splitext(dst_path)
        return f"{base}.jpg"
    return dst_path


def compress_image_file(src_path: str, dst_path: str, cfg: CompressConfig) -> Tuple[int, int]:
    """
    Returns (src_bytes, dst_bytes)
    """
    dst_path = output_path_for_source(src_path, dst_path, cfg)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    src_bytes = os.path.getsize(src_path)

    ext = os.path.splitext(src_path)[1].lower()
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)
        img = _maybe_resize(img, cfg.max_size)

        if ext in {".jpg", ".jpeg"}:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            img.save(
                dst_path,
                format="JPEG",
                quality=max(1, min(cfg.jpeg_quality, 95)),
                optimize=cfg.jpeg_optimize,
                progressive=cfg.jpeg_progressive,
            )

        elif ext == ".png":
            if cfg.png_to_jpeg:
                if "A" in img.getbands():
                    bg = (255, 255, 255) if cfg.png_jpeg_bg != "black" else (0, 0, 0)
                    alpha_img = img.convert("RGBA")
                    canvas = Image.new("RGB", alpha_img.size, bg)
                    canvas.paste(alpha_img, mask=alpha_img.split()[-1])
                    img = canvas
                elif img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                img.save(
                    dst_path,
                    format="JPEG",
                    quality=max(1, min(cfg.jpeg_quality, 95)),
                    optimize=cfg.jpeg_optimize,
                    progressive=cfg.jpeg_progressive,
                )
            else:
                if cfg.png_quantize:
                    has_alpha = ("A" in img.getbands())
                    if not has_alpha:
                        img = img.convert("RGB").quantize(colors=256, method=Image.Quantize.FASTOCTREE)

                img.save(
                    dst_path,
                    format="PNG",
                    optimize=cfg.png_optimize,
                    compress_level=max(0, min(cfg.png_compress_level, 9)),
                )
        else:
            shutil.copy2(src_path, dst_path)

    dst_bytes = os.path.getsize(dst_path)
    return src_bytes, dst_bytes
