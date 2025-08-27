#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Downloader Bot — MTProto/Telethon edition (รองรับไฟล์ใหญ่)
ทางเลือก B: ใช้ MTProto แทน Bot API → โหลด/อัปโหลดไฟล์ใหญ่ได้ (ระดับ GB)

สิ่งที่เปลี่ยนจากเวอร์ชันเดิม (python-telegram-bot / Bot API):
- เปลี่ยนมาใช้ Telethon (MTProto) ทั้งหมด
- ยังมี progress message ระหว่างดาวน์โหลดเหมือนเดิม
- รองรับทั้งไฟล์จาก Telegram และลิงก์ http/https
- จำกัดการใช้งานเฉพาะ OWNER_ID ตาม config.ini

ต้องเตรียมใน config.ini (อยู่โฟลเดอร์เดียวกับสคริปต์):
[bot]
token = 123456:ABC-YourBotToken
owner_id = 123456789
download_dir = downloads
date_subfolders = true

# ใหม่สำหรับ MTProto (ต้องมี! สมัครที่ https://my.telegram.org):
api_id = 123456
api_hash = abcdef0123456789abcdef0123456789

การติดตั้งไลบรารีที่ต้องใช้:
    pip install telethon aiohttp

การรัน:
    python3 tele.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from telethon import events, types
from telethon import TelegramClient

# ---------- คอนฟิกพื้นฐาน ----------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.ini"

if not CONFIG_PATH.exists():
    raise SystemExit(
        f"ไม่พบไฟล์คอนฟิก: {CONFIG_PATH}\n"
        "โปรดสร้างไฟล์ config.ini แล้วรันใหม่"
    )

cfg = ConfigParser()
cfg.read(CONFIG_PATH, encoding="utf-8")

BOT_TOKEN = (cfg.get("bot", "token", fallback="") or "").strip()
if not BOT_TOKEN:
    raise SystemExit("กรุณาใส่ค่า [bot] token ใน config.ini")

try:
    OWNER_ID = cfg.getint("bot", "owner_id")
except Exception:
    raise SystemExit("กรุณาใส่ค่า [bot] owner_id (เป็นตัวเลข) ใน config.ini")

BASE_DIR = (SCRIPT_DIR / "log").resolve()
DATE_SUBFOLDERS = False

try:
    API_ID = cfg.getint("bot", "api_id")
    API_HASH = (cfg.get("bot", "api_hash", fallback="") or "").strip()
except Exception:
    raise SystemExit("กรุณาใส่ค่า [bot] api_id และ api_hash สำหรับ MTProto ใน config.ini")

if not API_ID or not API_HASH:
    raise SystemExit("กรุณาใส่ค่า [bot] api_id และ api_hash สำหรับ MTProto ใน config.ini")

# ---------- เตรียมระบบ ----------
BASE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("downloader-bot-mtproto")

URL_REGEX = re.compile(r'(https?://[^\s<>"]+)', re.IGNORECASE)


# ---------- Utils ----------
def _save_dir(sub: str = "") -> Path:
    # Store every download in a single folder (no daily/type subfolders)
    d = BASE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d
    if DATE_SUBFOLDERS:
        d = d / datetime.now().strftime("%Y-%m-%d")
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    return d

def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    return name.strip()[:150] or "file"

def _unique_path(dirpath: Path, name: str) -> Path:
    stem = Path(name).stem
    suffix = Path(name).suffix
    candidate = dirpath / f"{stem}{suffix}"
    i = 1
    while candidate.exists():
        candidate = dirpath / f"{stem}_{i}{suffix}"
        i += 1
    return candidate

def _human(n: Optional[int]) -> str:
    if not n and n != 0:
        return "unknown"
    units = ["B","KB","MB","GB","TB"]
    s = float(n)
    i = 0
    while s >= 1024 and i < len(units)-1:
        s /= 1024.0
        i += 1
    return f"{s:.1f} {units[i]}"

def _bar(pct: Optional[float]) -> str:
    total_blocks = 16
    if pct is None:
        return "░" * total_blocks
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100.0 * total_blocks))
    return "█" * filled + "░" * (total_blocks - filled)


# ---------- Messaging helpers (Telethon) ----------
async def _progress_message(client: TelegramClient, chat_id: int, filename: str, total: Optional[int]):
    text = (
        f"🚀 กำลังเริ่มดาวน์โหลด\n"
        f"• ไฟล์: {filename}\n"
        f"• ขนาด: {_human(total)}\n\n"
        f"{_bar(0)} 0%"
    )
    return await client.send_message(chat_id, text)

async def _update_progress(msg, filename: str, downloaded: int, total: Optional[int], force: bool=False):
    pct = None
    if total and total > 0:
        pct = downloaded * 100.0 / total
    text = (
        f"⬇️ ดาวน์โหลดอยู่…\n"
        f"• ไฟล์: {filename}\n"
        f"• ขนาด: {_human(total)} | ได้มา: {_human(downloaded)}\n\n"
        f"{_bar(pct)} {0 if pct is None else int(pct)}%"
    )
    try:
        await msg.edit(text)
    except Exception as e:
        if force:
            log.debug("edit failed (ignored): %s", e)

async def _finish_message(msg, filename: str, path: Path, total: Optional[int], took: float):
    text = (
        f"✅ สำเร็จ\n"
        f"• ไฟล์: {filename}\n"
        f"• ปลายทาง: {path.name}\n"
        f"• ขนาด: {_human(total)}\n"
        f"• ใช้เวลา: {took:.1f}s"
    )
    try:
        await msg.edit(text)
    except Exception as e:
        log.warning("edit final failed: %s", e)

async def _error_message(msg, filename: str, err: str):
    try:
        await msg.edit(f"❌ ล้มเหลว: {filename}\nเหตุผล: {err}")
    except Exception:
        pass


# ---------- ดาวน์โหลดผ่าน HTTP(S) ----------
async def _stream_to_file(url: str, dest: Path, progress_msg, filename: str, total_hint: Optional[int]=None):
    """
    สตรีมดาวน์โหลดผ่าน HTTP พร้อมอัปเดต progress ทีละชิ้น
    """
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=1800)
    t0 = time.time()
    downloaded = 0
    last_edit_ts = 0.0
    last_pct_shown = -1

    dest.parent.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            total = total_hint or resp.content_length
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    # throttle การอัปเดต: ทุก ≥0.7s หรือเพิ่ม ≥2%
                    now = time.time()
                    pct = int(downloaded * 100 / total) if total else None
                    if pct is None:
                        do_update = (now - last_edit_ts) >= 0.7
                    else:
                        do_update = ((now - last_edit_ts) >= 0.7) and (pct >= last_pct_shown + 2)
                    if do_update:
                        await _update_progress(progress_msg, filename, downloaded, total)
                        last_edit_ts = now
                        if pct is not None:
                            last_pct_shown = pct

    await _finish_message(progress_msg, filename, dest, total_hint or downloaded, time.time() - t0)
    return dest

async def _http_download_with_progress(client: TelegramClient, chat_id: int, url: str, subdir: str) -> Path:
    # เดาชื่อไฟล์จาก URL หรือ header
    def _guess_filename_from_url(u: str) -> str:
        base = u.split("?")[0]
        tail = base.rstrip("/").split("/")[-1] or "download"
        return _sanitize_filename(tail)

    # HEAD/GET เพื่อหา filename จาก header
    fname = _guess_filename_from_url(url)
    dirpath = _save_dir("links" if subdir == "" else subdir)
    dest = _unique_path(dirpath, fname)

    # เตรียม progress msg ก่อนเริ่ม (ยังไม่รู้ขนาด -> unknown)
    msg = await _progress_message(client, chat_id, fname, None)

    # ลองดูจาก headers ตอนเริ่ม GET เพื่อใช้ content-length/filename
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=1800)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename="?([^";]+)"?', cd, flags=re.IGNORECASE)
            if m:
                fname = _sanitize_filename(m.group(1))
                dest = _unique_path(dirpath, fname)

    # โหลดจริงพร้อม progress (จะใช้ content-length ครั้งนี้)
    await _stream_to_file(url, dest, msg, fname, total_hint=None)
    return dest


# ---------- ดาวน์โหลดจาก Telegram (MTProto) ----------
def _infer_filename_from_message(msg: types.Message) -> str:
    # Document
    if msg.document:
        for attr in msg.document.attributes or []:
            if isinstance(attr, types.DocumentAttributeFilename):
                return _sanitize_filename(attr.file_name)
        # no filename attribute -> fallback by mime
        ext = ""
        if msg.document.mime_type:
            mt = msg.document.mime_type
            if "/" in mt:
                ext = "." + mt.split("/")[-1].split("-")[0]
        return _sanitize_filename(f"document_{msg.id}{ext}")
    # Photo
    if msg.photo:
        return _sanitize_filename(f"photo_{msg.id}.jpg")
    # Voice
    if msg.voice:
        return _sanitize_filename(f"voice_{msg.id}.ogg")
    # Audio
    if msg.audio:
        title = "audio"
        ext = ".mp3" if (msg.audio.mime_type or "").endswith("mpeg") else ".ogg"
        return _sanitize_filename(f"{title}_{msg.id}{ext}")
    # Video
    if msg.video:
        return _sanitize_filename(f"video_{msg.id}.mp4")
    # Sticker/Animation/Other
    return _sanitize_filename(f"file_{msg.id}")

async def _download_tg_with_progress(client: TelegramClient, msg: types.Message, subdir: str) -> Tuple[Path, int]:
    filename = _infer_filename_from_message(msg)
    dirpath = _save_dir(subdir)
    dest = _unique_path(dirpath, filename)

    # แจ้งเตือนเริ่มดาวน์โหลด + แถบ progress
    progress_msg = await _progress_message(client, msg.chat_id, filename, getattr(msg, 'file', None) and getattr(msg.file, 'size', None))

    t0 = time.time()
    state = {
        "last_ts": 0.0,
        "last_pct": -1,
        "total": getattr(msg, 'file', None) and getattr(msg.file, 'size', None) or None,
    }

    loop = asyncio.get_event_loop()

    def _cb(current: int, total: int):
        # สร้าง task เพื่อแก้ไขข้อความแบบ async โดยไม่บล็อค Telethon
        now = time.time()
        pct = int(current * 100 / total) if total else None
        if pct is None:
            do_update = (now - state["last_ts"]) >= 0.7
        else:
            do_update = ((now - state["last_ts"]) >= 0.7) and (pct >= state["last_pct"] + 2)
        if do_update:
            loop.create_task(_update_progress(progress_msg, filename, current, total))
            state["last_ts"] = now
            if pct is not None:
                state["last_pct"] = pct

    # ดาวน์โหลดจริง
    path_str = await client.download_media(msg, file=str(dest), progress_callback=_cb)
    if not path_str:
        await _error_message(progress_msg, filename, "download_media() คืนค่า path ว่าง")
        raise RuntimeError("download_media failed")

    path = Path(path_str)
    # ในบางเคส Telethon อาจเติมส่วนขยายให้เอง → ทำให้ชื่อไม่ตรงกับ dest เดิม
    # ให้ย้ายไปเป็นชื่อที่ยูนีคในโฟลเดอร์ (ถ้าจำเป็น)
    if path.name != dest.name:
        final_dest = _unique_path(dirpath, path.name)
        if final_dest != path:
            try:
                path.replace(final_dest)
                path = final_dest
            except Exception as e:
                log.warning("rename final file failed: %s", e)

    size = path.stat().st_size if path.exists() else 0
    await _finish_message(progress_msg, filename, path, state["total"] or size, time.time() - t0)
    return path, size


# ---------- การตรวจสิทธิ์ผู้ใช้ ----------
def _is_owner(event) -> bool:
    try:
        return int(event.sender_id) == int(OWNER_ID)
    except Exception:
        return False

async def _reject_if_not_owner(event) -> bool:
    if _is_owner(event):
        return False
    try:
        await event.reply("⛔ บอทนี้อนุญาตเฉพาะเจ้าของเท่านั้น")
    except Exception:
        pass
    return True


# ---------- Handlers (Telethon) ----------
@events.register(events.NewMessage(pattern=r'^/start$'))
async def cmd_start(event):
    if await _reject_if_not_owner(event):
        return
    me = await event.client.get_me()
    uid = event.sender_id or "unknown"
    lines = [
        "👋 สวัสดี! นี่คือบอทดาวน์โหลดไฟล์ (Telethon + MTProto)",
        f"• ชื่อบอท: @{getattr(me, 'username', '')}",
        f"• user id ของคุณ: {uid}",
        f"• โฟลเดอร์เซฟ: {BASE_DIR}",
        f"• ใช้โฟลเดอร์รายวัน: {DATE_SUBFOLDERS}",
        "",
        "ส่งไฟล์/ลิงก์มาได้เลย บอทจะแจ้งเริ่มดาวน์โหลดและโชว์ความคืบหน้าจนเสร็จ (เฉพาะเจ้าของ)",
        "",
        "คำสั่ง:",
        "/id — แสดง user id ของคุณ",
        "/ping — ตรวจสอบสถานะ",
    ]
    await event.reply("\n".join(lines))

@events.register(events.NewMessage(pattern=r'^/id$'))
async def cmd_id(event):
    if await _reject_if_not_owner(event):
        return
    uid = event.sender_id or "unknown"
    await event.reply(f"🆔 user id ของคุณคือ: {uid}")

@events.register(events.NewMessage(pattern=r'^/ping$'))
async def cmd_ping(event):
    if await _reject_if_not_owner(event):
        return
    await event.reply("🏓 pong")

@events.register(events.NewMessage(func=lambda e: bool(e.document)))
async def handle_document(event):
    if await _reject_if_not_owner(event):
        return
    try:
        path, size = await _download_tg_with_progress(event.client, event.message, "documents")
        log.info("Downloaded document: %s (%s)", path, _human(size))
    except Exception as e:
        log.exception("Failed to download document: %s", e)

@events.register(events.NewMessage(func=lambda e: bool(e.photo)))
async def handle_photo(event):
    if await _reject_if_not_owner(event):
        return
    try:
        path, size = await _download_tg_with_progress(event.client, event.message, "photos")
        log.info("Downloaded photo: %s (%s)", path, _human(size))
    except Exception as e:
        log.exception("Failed to download photo: %s", e)

@events.register(events.NewMessage(func=lambda e: bool(e.video)))
async def handle_video(event):
    if await _reject_if_not_owner(event):
        return
    try:
        path, size = await _download_tg_with_progress(event.client, event.message, "videos")
        log.info("Downloaded video: %s (%s)", path, _human(size))
    except Exception as e:
        log.exception("Failed to download video: %s", e)

@events.register(events.NewMessage(func=lambda e: bool(e.audio)))
async def handle_audio(event):
    if await _reject_if_not_owner(event):
        return
    try:
        path, size = await _download_tg_with_progress(event.client, event.message, "audios")
        log.info("Downloaded audio: %s (%s)", path, _human(size))
    except Exception as e:
        log.exception("Failed to download audio: %s", e)

@events.register(events.NewMessage(func=lambda e: bool(e.voice)))
async def handle_voice(event):
    if await _reject_if_not_owner(event):
        return
    try:
        path, size = await _download_tg_with_progress(event.client, event.message, "voices")
        log.info("Downloaded voice: %s (%s)", path, _human(size))
    except Exception as e:
        log.exception("Failed to download voice: %s", e)

@events.register(events.NewMessage(func=lambda e: bool(e.raw_text and URL_REGEX.search(e.raw_text))))
async def handle_text_links(event):
    if await _reject_if_not_owner(event):
        return
    text = event.raw_text or ""
    urls = URL_REGEX.findall(text)
    if not urls:
        return
    for url in urls:
        try:
            await _http_download_with_progress(event.client, event.chat_id, url, "links")
        except Exception as e:
            log.exception("Failed to download url %s: %s", url, e)
            try:
                await event.reply(f"❌ ดาวน์โหลดลิงก์ไม่สำเร็จ: {url}\nเหตุผล: {e}")
            except Exception:
                pass


# ---------- main ----------
def main():
    client = TelegramClient("bot_session", API_ID, API_HASH)
    # Login ด้วย bot token
    client.start(bot_token=BOT_TOKEN)

    # ติดตั้ง handlers
    client.add_event_handler(cmd_start)
    client.add_event_handler(cmd_id)
    client.add_event_handler(cmd_ping)
    client.add_event_handler(handle_document)
    client.add_event_handler(handle_photo)
    client.add_event_handler(handle_video)
    client.add_event_handler(handle_audio)
    client.add_event_handler(handle_voice)
    client.add_event_handler(handle_text_links)

    log.info("Bot is starting with Telethon (MTProto)…")
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
