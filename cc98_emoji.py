#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unicode emoji → PNG 图片(本地渲染,零网络依赖)。

KPW3 系统没有 emoji 字体,直接显示会变成空框。这里用 Pillow 把 emoji
渲染成小 PNG(Windows 自带 Segoe UI Emoji 字体),Kindle 就能显示出来。
渲染结果缓存在 emoji_cache/ 目录,之后直接读磁盘。
"""
import os
import threading

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "emoji_cache"

# 渲染用的字体(按优先级)
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/seguiemj.ttf",          # Windows 自带 Segoe UI Emoji
    str(BASE_DIR / "NotoEmoji-Regular.ttf"),  # 备选:放到项目目录里
    "/System/Library/Fonts/Apple Color Emoji.ttc",
)

_FONT_SIZE = 100   # 渲染尺寸;Kindle 端再用 CSS 缩到约一行文字高
_CANVAS = 128
_PAD = 4

_font = None
_font_checked = False
_lock = threading.Lock()


def _load_font():
    global _font, _font_checked
    if _font_checked:
        return _font
    if ImageFont is not None:
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    _font = ImageFont.truetype(path, _FONT_SIZE)
                    break
                except Exception:
                    continue
    _font_checked = True
    return _font


def available():
    """本机是否具备渲染 emoji 图片的能力。"""
    return _load_font() is not None


def _read_cache(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def render_png(name):
    """按码点名(如 "1f62d" 或 "1f468-2695")生成 PNG 字节;失败返回 None。"""
    font = _load_font()
    if font is None:
        return None
    path = CACHE_DIR / ("%s.png" % name)
    data = _read_cache(path)
    if data:
        return data
    with _lock:  # 简单串行化,避免同一张图重复渲染/写坏
        data = _read_cache(path)
        if data:
            return data
        try:
            text = "".join(chr(int(cp, 16)) for cp in name.split("-"))
        except ValueError:
            return None
        if not text:
            return None
        try:
            img = Image.new("RGBA", (_CANVAS, _CANVAS), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            try:
                draw.text((_PAD, _PAD), text, font=font, embedded_color=True)
            except TypeError:  # 老版本 Pillow 不支持彩色字体
                draw.text((_PAD, _PAD), text, font=font)
            bbox = img.getbbox()
            if not bbox:
                return None
            img = img.crop(bbox)
            # 贴到白底上(Kindle 页面是白底,避免透明通道兼容问题)
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, (0, 0), img)
            import io
            buf = io.BytesIO()
            flat.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
        except Exception:
            return None
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, str(path))
        except OSError:
            pass  # 写缓存失败不影响本次返回
        return data
