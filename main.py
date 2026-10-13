# -*- coding: utf-8 -*-
"""
SolAlpha Sniper v3.5 — Telegram bot (Render Web Service)
مصدر العملات: قناة الإشارات https://t.me/solhousesignal حصرياً.
طريقة القراءة (بالترتيب): 0) الوضع اليدوي: ترسل للبوت عقوداً أو تحوّل (Forward) رسائل الإشارات فيحللها /best (لا يحتاج أي إعداد)
                          1) Telethon بجلسة مستخدم (TELETHON_SESSION) — الأوثق
                          2) HTTP على مرايا المعاينة العامة (t.me/s وأخواتها) كاحتياط
ملاحظة: حسابات البوت لا تستطيع قراءة سجل القنوات (BotMethodInvalidError) فلا تُستخدم.

/best : يقرأ آخر رسائل القناة → يستخرج عقود Solana → يفحصها ويعطيك أفضل صفقة + نوعها + بدائل
/diag : تشخيص مصدر القراءة (Telethon/HTTP) وفلترة العقود
/help : شرح

أنواع الصفقات:
  ⚡ SCALP    : دخول من Fibonacci Golden Zone (0.5–0.618)، هدف +18% / +25%
  🚀 MOMENTUM : عملة تصعد بفوليوم متسارع، هدف +15% / +30%
  🧭 HOLD     : عملة أقدم (24h+) بسيولة وفوليوم جيدين، هدف +50% / +100%

الحظر الصلب (لا يُخفَّف أبداً): Mint/Freeze ملغاة، LP محروقة/مقفولة، ليست Rugged، لا Danger في RugCheck.
أما Dev/Top10/Insider/تقييم RugCheck فتخفض الـ Score وتظهر كتحذيرات (المستوى B).

متغيرات البيئة:
  TELEGRAM_BOT_TOKEN (مطلوب) | PORT و RENDER_EXTERNAL_URL (تضبطهما Render)
  TELEGRAM_API_ID + TELEGRAM_API_HASH (مطلوبان — من https://my.telegram.org → API development tools)
  TELETHON_SESSION : StringSession لحساب مستخدم، تولّدها مرة واحدة محلياً بـ gen_session.py
  SIGNAL_HTTP_MIRRORS (اختياري) : قوالب روابط مفصولة بفاصلة، فيها {ch}، تُجرَّب بالترتيب
  SIGNAL_CHANNEL (افتراضي solhousesignal) | SIGNAL_MESSAGE_LIMIT (افتراضي 60) | SIGNAL_MAX_AGE_HOURS (افتراضي 24)
  ALLOW_RISKY=0 للصرامة الكاملة
"""

import asyncio
import html
import logging
import math
import os
import re
import signal
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient
from telethon.sessions import StringSession

# ───────────────────────────── الإعدادات ─────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 80)            # 🟢 قوية (مع المستوى A)
RISKY_MIN_SCORE = _env_float("RISKY_MIN_SCORE", 65)  # 🟡 متوسطة

# فلتر أولي صلب
MIN_LIQUIDITY_USD = _env_float("MIN_LIQUIDITY_USD", 15_000)
MIN_VOLUME_H1_USD = _env_float("MIN_VOLUME_H1_USD", 8_000)
MIN_MCAP_USD = _env_float("MIN_MCAP_USD", 30_000)
MAX_MCAP_USD = _env_float("MAX_MCAP_USD", 100_000_000)
MIN_PAIR_AGE_MIN = _env_float("MIN_PAIR_AGE_MIN", 45)
MAX_H1_CHANGE_PCT = _env_float("MAX_H1_CHANGE_PCT", 300)
MAX_5M_DROP_PCT = _env_float("MAX_5M_DROP_PCT", 10)
MAX_1H_DROP_PCT = _env_float("MAX_1H_DROP_PCT", 25)

# الأمان
MAX_DEV_PCT = _env_float("MAX_DEV_PCT", 5)
MAX_TOP10_PCT = _env_float("MAX_TOP10_PCT", 25)
MIN_LP_LOCKED_PCT = _env_float("MIN_LP_LOCKED_PCT", 95)
RUGCHECK_MAX_NORM = _env_float("RUGCHECK_MAX_NORM", 20)
RUGCHECK_MAX_RAW = _env_float("RUGCHECK_MAX_RAW", 500)
ALLOW_RISKY = _env_float("ALLOW_RISKY", 1) >= 1
RISKY_MAX_TOP10_PCT = _env_float("RISKY_MAX_TOP10_PCT", 45)
RISKY_MAX_DEV_PCT = _env_float("RISKY_MAX_DEV_PCT", 10)
RISKY_MAX_RUG_NORM = _env_float("RISKY_MAX_RUG_NORM", 50)
RISK_PENALTY = 8

# Fibonacci / الخطط
MIN_CANDLES = 20
MAX_WINDOW_CANDLES = 72
SWING_LOOKBACK_CANDLES = 36
MIN_LEG_PCT = _env_float("MIN_LEG_PCT", 12)
MAX_ABOVE_ZONE_PCT = _env_float("MAX_ABOVE_ZONE_PCT", 10)
MAX_SL_RISK_PCT = _env_float("MAX_SL_RISK_PCT", 25)
SL_BUFFER = 0.015
TP1_PCT = 0.18
TP2_PCT = 0.25

MAX_DISCOVERED_TOKENS = 150

# قناة الإشارات + Telethon
TG_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TG_SESSION = os.getenv("TELETHON_SESSION", "").strip()
SIGNAL_CHANNEL = os.getenv("SIGNAL_CHANNEL", "solhousesignal").strip().lstrip("@").replace("https://t.me/", "")
SIGNAL_MESSAGE_LIMIT = max(5, int(_env_float("SIGNAL_MESSAGE_LIMIT", 60)))
MANUAL_TTL_HOURS = _env_float("MANUAL_TTL_HOURS", 12)
MANUAL_MAX = 60
SIGNAL_PAGES = max(1, int(_env_float("SIGNAL_PAGES", 2)))            # صفحات HTTP، كل صفحة ≈ 20 رسالة
SIGNAL_HTTP_MIRRORS = [
    t.strip() for t in os.getenv(
        "SIGNAL_HTTP_MIRRORS",
        "https://t.me/s/{ch},https://telegram.me/s/{ch},https://tx.me/s/{ch}",
    ).split(",") if "{ch}" in t
]
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.8",
    "Cache-Control": "no-cache",
}
SIGNAL_MAX_AGE_HOURS = _env_float("SIGNAL_MAX_AGE_HOURS", 24)
MAX_SECURITY_CHECKS = int(_env_float("MAX_SECURITY_CHECKS", 30))
MAX_CANDLE_FETCH = int(_env_float("MAX_CANDLE_FETCH", 8))
SCAN_BUDGET_SEC = _env_float("SCAN_BUDGET_SEC", 100)
USER_COOLDOWN_SEC = 15
SCAN_CACHE_SEC = 20
RUGCHECK_CACHE_SEC = 600
KEEPALIVE_SEC = 540

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    # عناوين برامج/نظام تظهر أحياناً في الرسائل وليست عملات
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}

KIND_LABEL = {
    "SCALP": "⚡ سكالبيج — Fibonacci Golden Zone",
    "MOMENTUM": "🚀 زخم Momentum — ركوب الصعود",
    "HOLD": "🧭 احتفاظ Swing — ساعات إلى أيام",
}
KIND_SHORT = {"SCALP": "⚡ سكالبيج", "MOMENTUM": "🚀 زخم", "HOLD": "🧭 احتفاظ"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("solalpha")

# ───────────────────────────── أدوات مساعدة ─────────────────────────────


def to_float(v: Any, default: float = 0.0) -> float:
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def fmt_price(p: float) -> str:
    if p <= 0:
        return "0"
    decimals = max(2, -int(math.floor(math.log10(p))) + 3)
    return f"{p:.{decimals}f}"


API_STATUS: dict = {}   # host -> (آخر حالة، وقتها) — يظهر في /diag وفي رسائل الفشل


def _note_api(url: str, status: str) -> None:
    try:
        API_STATUS[url.split("/")[2]] = (status, time.time())
    except IndexError:
        pass


def api_status_text() -> str:
    names = {"api.dexscreener.com": "DexScreener", "api.geckoterminal.com": "GeckoTerminal", "api.rugcheck.xyz": "RugCheck"}
    lines = []
    for host, name in names.items():
        st = API_STATUS.get(host)
        if st:
            lines.append(f"• {name}: {st[0]} (قبل {int(time.time() - st[1])}ث)")
    return "\n".join(lines) if lines else "• لا توجد معلومات عن حالة المصادر بعد"


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    retries: int = 1,
) -> Any:
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, params=params, headers=headers)
            _note_api(url, f"HTTP {r.status_code}")
            if r.status_code == 429:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            if r.status_code != 200:
                log.warning("HTTP %s from %s", r.status_code, url)
                return None
            return r.json()
        except ValueError:
            _note_api(url, "JSON غير صالح")
            return None
        except httpx.HTTPError as e:
            _note_api(url, type(e).__name__)
            log.warning("Request failed (%s): %s", url, e)
            await asyncio.sleep(0.6 * (attempt + 1))
    return None


# ───────────────────────────── اكتشاف العملات (من قناة الإشارات) ─────────────────────────────

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {ch: i for i, ch in enumerate(B58_ALPHABET)}

# عنوان Solana = Base58 بطول 32–44 حرفاً (والتحقق النهائي أنه يفك إلى 32 بايت)
SOL_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
# روابط DexScreener تحمل عنوان "الزوج" لا العملة → نحلّه لاحقاً عبر endpoint الأزواج
DEX_PAIR_URL_RE = re.compile(r"(?i:dexscreener\.com)/solana/([1-9A-HJ-NP-Za-km-z]{32,44})")
# روابط المحافظ/الحسابات ليست عقود عملات
EXPLORER_URL_RE = re.compile(r"(?i:solscan\.io|solana\.fm|explorer\.solana\.com)/(?:account|address)/\S+")



@dataclass
class Signal:
    mint: str                       # عنوان العملة (أو عنوان زوج إذا is_hint)
    post_id: int
    ts: Optional[float]             # وقت نشر الرسالة (epoch ثوانٍ)
    is_hint: bool = False           # جاء من رابط DexScreener (قد يكون عنوان زوج)
    manual: bool = False            # أرسله المستخدم يدوياً (لصق/Forward)
    added: float = 0.0              # وقت استلام البوت له (للوضع اليدوي)


def is_valid_solana_address(s: str) -> bool:
    n = 0
    for ch in s:
        v = B58_INDEX.get(ch)
        if v is None:
            return False
        n = n * 58 + v
    pad = len(s) - len(s.lstrip("1"))
    return pad + (n.bit_length() + 7) // 8 == 32


def extract_addresses(text: str):
    """يرجع (عناوين_عقود، عناوين_من_روابط_DexScreener) مرتبة وفريدة."""
    if not text:
        return [], []
    hints: list = []
    for a in DEX_PAIR_URL_RE.findall(text):
        if a not in hints and a not in EXCLUDED_MINTS and is_valid_solana_address(a):
            hints.append(a)
    cleaned = EXPLORER_URL_RE.sub(" ", DEX_PAIR_URL_RE.sub(" ", text))
    mints: list = []
    for a in SOL_ADDR_RE.findall(cleaned):
        if a not in mints and a not in EXCLUDED_MINTS and is_valid_solana_address(a):
            mints.append(a)
    return mints, hints


def fmt_age(minutes: float) -> str:
    if minutes < 1:
        return "الآن"
    if minutes < 60:
        return f"منذ {int(minutes)} دقيقة"
    if minutes < 1440:
        return f"منذ {minutes / 60:.1f} ساعة"
    return f"منذ {minutes / 1440:.1f} يوم"


_POST_RE = re.compile(r'data-post="([^"]+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_PREVIEW_RE = re.compile(r'<a[^>]*tgme_widget_message_link_preview[^>]*href="([^"]+)"')
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def _html_to_text(fragment: str) -> str:
    s = _ANCHOR_RE.sub(lambda m: f" {m.group(1)} {m.group(2)} ", fragment)  # نُبقي قيمة href
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def parse_channel_html(page: str) -> list:
    """يحلل صفحة معاينة القناة (…/s/<channel>) → [{id, ts, text}]."""
    matches = list(_POST_RE.finditer(page))
    out = []
    for i, mt in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page)
        chunk = page[mt.start():end]
        chan, _, pid = mt.group(1).partition("/")
        if chan.lower() != SIGNAL_CHANNEL.lower() or not pid.isdigit():
            continue
        parts = [_html_to_text(t) for t in _TEXT_RE.findall(chunk)]
        parts += [html.unescape(u) for u in _PREVIEW_RE.findall(chunk)]
        ts = None
        tm = _TIME_RE.search(chunk)
        if tm:
            try:
                ts = datetime.fromisoformat(tm.group(1).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        out.append({"id": int(pid), "ts": ts, "text": "\n".join(parts)})
    return out


TL: dict = {"client": None, "error": None}   # حالة عميل Telethon (يُهيّأ في post_init)


async def create_telethon():
    """يرجع (client, خطأ). يحتاج جلسة مستخدم؛ حسابات البوت ممنوعة من قراءة سجل القنوات."""
    if not TG_SESSION:
        return None, "TELETHON_SESSION غير مضبوطة (جلسة البوت لا تقرأ سجل القنوات)"
    if not (TG_API_ID and TG_API_HASH):
        return None, "TELEGRAM_API_ID / TELEGRAM_API_HASH غير مضبوطين"
    try:
        api_id = int(TG_API_ID)
    except ValueError:
        return None, "TELEGRAM_API_ID يجب أن يكون رقماً"
    client = TelegramClient(StringSession(TG_SESSION), api_id, TG_API_HASH, receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None, "TELETHON_SESSION غير صالحة/منتهية — ولّدها من جديد بـ gen_session.py"
    except Exception as e:
        log.exception("Telethon connect failed")
        try:
            await client.disconnect()
        except Exception:
            pass
        return None, f"فشل اتصال Telethon: {type(e).__name__}: {e}"
    me = None
    try:
        me = await client.get_me()
    except Exception:
        pass
    if me is not None and getattr(me, "bot", False):
        await client.disconnect()
        return None, "TELETHON_SESSION تخص بوتاً — استخدم جلسة حساب مستخدم عادي"
    log.info("Telethon connected (user session)")
    return client, None


async def _read_channel(tl, limit: int, cutoff: float, info: dict) -> list:
    """أحدث الرسائل أولاً → [{id, ts, text}] مع نص الرسالة وروابطها (hidden links/أزرار/معاينة)."""
    out: list = []
    async for msg in tl.iter_messages(SIGNAL_CHANNEL, limit=limit):
        ts = msg.date.timestamp() if getattr(msg, "date", None) else None
        info["messages"] += 1
        if ts:
            info["newest_ts"] = max(info.get("newest_ts", 0.0), ts)
        if ts and ts < cutoff:
            info["expired"] += 1
            break  # الرسائل مرتبة من الأحدث → ما بعدها أقدم
        parts = [msg.raw_text or ""]
        try:
            for ent, _txt in msg.get_entities_text():
                if getattr(ent, "url", None):
                    parts.append(ent.url)           # رابط مخفي خلف نص
        except Exception:
            pass
        wp = getattr(getattr(msg, "media", None), "webpage", None)
        if getattr(wp, "url", None):
            parts.append(wp.url)                    # معاينة الرابط
        try:
            for row in msg.buttons or []:
                for b in row:
                    if getattr(b, "url", None):
                        parts.append(b.url)         # أزرار inline
        except Exception:
            pass
        out.append({"id": msg.id, "ts": ts, "text": "\n".join(parts)})
    return out


def _describe_tl_error(e: Exception) -> str:
    name = type(e).__name__
    if name == "BotMethodInvalidError":
        return "Telethon: الجلسة جلسة بوت والبوتات لا تقرأ سجل القنوات — استخدم TELETHON_SESSION لحساب مستخدم"
    if name == "FloodWaitError" and getattr(e, "seconds", None):
        return f"Telethon: FloodWait — انتظر {e.seconds} ثانية"
    return f"Telethon: {name}: {e}"


async def _read_http(client: httpx.AsyncClient, info: dict, cutoff: float, errors: list):
    """يجرّب مرايا المعاينة العامة بالترتيب. يرجع (رسائل، نجحت_القراءة)."""
    for tmpl in SIGNAL_HTTP_MIRRORS:
        base = tmpl.format(ch=SIGNAL_CHANNEL)
        host = base.split("/")[2] if "//" in base else base
        info.update(messages=0, expired=0, newest_ts=0.0)
        msgs: list = []
        seen: set = set()
        before = None
        reached = False
        for _ in range(SIGNAL_PAGES):
            try:
                r = await client.get(base, params={"before": before} if before else None, headers=WEB_HEADERS)
            except httpx.HTTPError as e:
                errors.append(f"{host}: {type(e).__name__}")
                break
            if r.status_code != 200:
                errors.append(f"{host}: HTTP {r.status_code}")
                break
            if "tgme_widget_message" not in r.text:
                errors.append(f"{host}: لا معاينة عامة أو الطلب محجوب")
                break
            reached = True
            page = [m for m in parse_channel_html(r.text) if m["id"] not in seen]
            if not page:
                break
            oldest = None
            for m in page:
                seen.add(m["id"])
                info["messages"] += 1
                if m["ts"]:
                    info["newest_ts"] = max(info["newest_ts"], m["ts"])
                    oldest = m["ts"] if oldest is None else min(oldest, m["ts"])
                if m["ts"] and m["ts"] < cutoff:
                    info["expired"] += 1
                    continue
                msgs.append(m)
            if oldest is not None and oldest < cutoff:
                break
            before = min(m["id"] for m in page)
            await asyncio.sleep(0.3)
        if reached:
            info["source"] = f"HTTP ({host})"
            return msgs, True
    return [], False


async def discover_signals(client: Optional[httpx.AsyncClient] = None):
    """يقرأ آخر رسائل القناة: Telethon (جلسة مستخدم) ثم HTTP كاحتياط. يرجع (Signal الأحدث أولاً، info)."""
    info = {
        "ok": False, "error": None, "errors": [], "source": None, "messages": 0, "expired": 0,
        "with_addr": 0, "addresses": 0, "newest_age_min": None, "newest_ts": 0.0,
    }
    now = time.time()
    cutoff = now - SIGNAL_MAX_AGE_HOURS * 3600
    errors: list = info["errors"]
    msgs: list = []
    got = False

    tl = TL.get("client")
    if tl is not None:
        try:
            msgs = await asyncio.wait_for(_read_channel(tl, SIGNAL_MESSAGE_LIMIT, cutoff, info), timeout=25)
            got = True
            info["source"] = "Telethon"
        except asyncio.TimeoutError:
            errors.append("Telethon: انتهت مهلة القراءة")
        except Exception as e:
            log.warning("Telethon read failed: %s: %s", type(e).__name__, e)
            errors.append(_describe_tl_error(e))
    else:
        errors.append("Telethon: " + (TL.get("error") or "غير مهيّأ"))

    if not got and client is not None:
        msgs, got = await _read_http(client, info, cutoff, errors)

    info["ok"] = got
    if not got:
        info["error"] = " | ".join(errors)
        return [], info

    found: dict = {}

    def add(addr: str, post_id: int, ts, is_hint: bool) -> None:
        cur = found.get(addr)
        if cur is None or post_id > cur.post_id:
            found[addr] = Signal(addr, post_id, ts, is_hint and not (cur and not cur.is_hint))

    for m in msgs:
        mints, hints = extract_addresses(m["text"])
        if mints or hints:
            info["with_addr"] += 1
        for a in mints:
            add(a, m["id"], m["ts"], False)
        for a in hints:
            add(a, m["id"], m["ts"], True)

    if info["newest_ts"]:
        info["newest_age_min"] = (now - info["newest_ts"]) / 60
    ordered = sorted(found.values(), key=lambda s: (s.ts or 0, s.post_id), reverse=True)
    info["addresses"] = len(ordered)
    return ordered[:MAX_DISCOVERED_TOKENS], info


def _merge_pairs(best: dict, payloads: list) -> None:
    for data in payloads:
        if isinstance(data, dict):
            data = data.get("pairs")
        if not isinstance(data, list):
            continue
        for p in data:
            if not isinstance(p, dict) or p.get("chainId") != "solana":
                continue
            base = (p.get("baseToken") or {}).get("address")
            if not base or base in EXCLUDED_MINTS:
                continue
            liq = to_float((p.get("liquidity") or {}).get("usd"))
            cur = best.get(base)
            if cur is None or liq > to_float((cur.get("liquidity") or {}).get("usd")):
                best[base] = p


async def fetch_gecko_pairs(client: httpx.AsyncClient, mints: list) -> list:
    """احتياط: GeckoTerminal (tokens/multi + top_pools) محوَّل لشكل أزواج DexScreener."""
    out: list = []
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = await fetch_json(
            client, f"{GECKO}/networks/solana/tokens/multi/{','.join(chunk)}", params={"include": "top_pools"}
        )
        if not isinstance(data, dict):
            continue
        pools = {p.get("id"): p for p in (data.get("included") or []) if isinstance(p, dict)}
        for tok in data.get("data") or []:
            try:
                ta = tok.get("attributes") or {}
                mint = ta.get("address")
                tp = ((tok.get("relationships") or {}).get("top_pools") or {}).get("data") or []
                pool = pools.get(tp[0].get("id")) if tp else None
                if not mint or not pool:
                    continue
                pa = pool.get("attributes") or {}
                dex_id = str((((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id") or "")
                created_ms = None
                if pa.get("pool_created_at"):
                    try:
                        created_ms = datetime.fromisoformat(str(pa["pool_created_at"]).replace("Z", "+00:00")).timestamp() * 1000
                    except ValueError:
                        created_ms = None
                tx = pa.get("transactions") or {}
                out.append({
                    "chainId": "solana",
                    "dexId": dex_id.replace("-", ""),          # pump-fun → pumpfun
                    "pairAddress": pa.get("address"),
                    "baseToken": {"address": mint, "name": ta.get("name"), "symbol": ta.get("symbol")},
                    "priceUsd": ta.get("price_usd"),
                    "liquidity": {"usd": pa.get("reserve_in_usd")},
                    "marketCap": pa.get("market_cap_usd") or ta.get("market_cap_usd"),
                    "fdv": pa.get("fdv_usd") or ta.get("fdv_usd"),
                    "volume": pa.get("volume_usd") or {},
                    "priceChange": pa.get("price_change_percentage") or {},
                    "txns": {k: {"buys": (v or {}).get("buys"), "sells": (v or {}).get("sells")} for k, v in tx.items()},
                    "pairCreatedAt": created_ms,
                })
            except Exception:
                log.exception("gecko pair parse failed")
    return out


async def fetch_pairs(client: httpx.AsyncClient, addresses: list, pair_hints: Any = (), sink: Optional[dict] = None) -> list:
    """sink: قاموس يُملأ تدريجياً فتبقى النتائج الجزئية حتى لو انتهت المهلة."""
    best: dict = sink if sink is not None else {}
    hint_set = set(pair_hints)
    mints = [a for a in addresses if a not in hint_set]

    # 1) DexScreener (الجديد)
    chunks = [addresses[i:i + 30] for i in range(0, len(addresses), 30)]
    responses = await asyncio.gather(
        *(fetch_json(client, f"{DEX}/tokens/v1/solana/{','.join(c)}") for c in chunks)
    )
    _merge_pairs(best, list(responses))

    # 2) DexScreener (endpoint القديم) لما لم يُحَل
    missing = [a for a in mints if a not in best]
    if missing:
        mchunks = [missing[i:i + 30] for i in range(0, len(missing), 30)]
        resp2 = await asyncio.gather(
            *(fetch_json(client, f"{DEX}/latest/dex/tokens/{','.join(c)}") for c in mchunks)
        )
        _merge_pairs(best, list(resp2))

    # 3) GeckoTerminal احتياطاً لما لم يُحَل
    missing = [a for a in mints if a not in best]
    if missing:
        _merge_pairs(best, [await fetch_gecko_pairs(client, missing)])

    # عناوين جاءت من روابط DexScreener ولم تُحَل كعملات → نجرّبها كعناوين أزواج
    known_pairs = {p.get("pairAddress") for p in best.values()}
    hints = [h for h in dict.fromkeys(hint_set) if h not in best and h not in known_pairs]
    if hints:
        hchunks = [hints[i:i + 30] for i in range(0, len(hints), 30)]
        hresp = await asyncio.gather(
            *(fetch_json(client, f"{DEX}/latest/dex/pairs/solana/{','.join(c)}") for c in hchunks)
        )
        _merge_pairs(best, list(hresp))
    return list(best.values())


async def probe_apis(client: httpx.AsyncClient) -> list:
    """اختبار مباشر لاتصال السيرفر بكل مصدر (يظهر في /diag)."""
    sol = "So11111111111111111111111111111111111111112"
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    tests = {
        "DexScreener": f"{DEX}/tokens/v1/solana/{sol}",
        "GeckoTerminal": f"{GECKO}/networks/solana/tokens/{sol}",
        "RugCheck": f"{RUGCHECK}/tokens/{usdc}/report",
    }

    async def one(name: str, url: str):
        t0 = time.time()
        try:
            r = await client.get(url, timeout=10)
            return name, f"{'✅' if r.status_code == 200 else '❌'} HTTP {r.status_code} ({time.time() - t0:.1f}s)"
        except Exception as e:
            return name, f"❌ {type(e).__name__}"

    return list(await asyncio.gather(*(one(n, u) for n, u in tests.items())))


# ───────────────────────────── المقاييس والفلترة الأولية ─────────────────────────────


def extract_metrics(pair: dict) -> Optional[dict]:
    base = pair.get("baseToken") or {}
    mint = base.get("address")
    pair_addr = pair.get("pairAddress")
    if not mint or not pair_addr:
        return None
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    tx = pair.get("txns") or {}
    t5 = tx.get("m5") or {}

    created = to_float(pair.get("pairCreatedAt"))
    age_min = (time.time() * 1000 - created) / 60000 if created > 0 else None

    m = {
        "mint": mint,
        "name": str(base.get("name") or "Unknown"),
        "symbol": str(base.get("symbol") or "?"),
        "pair": pair_addr,
        "dex": str(pair.get("dexId") or ""),
        "price": to_float(pair.get("priceUsd")),
        "liq": to_float((pair.get("liquidity") or {}).get("usd")),
        "mcap": to_float(pair.get("marketCap")) or to_float(pair.get("fdv")),
        "vol5": to_float(vol.get("m5")),
        "vol1": to_float(vol.get("h1")),
        "vol6": to_float(vol.get("h6")),
        "vol24": to_float(vol.get("h24")),
        "chg5": to_float(chg.get("m5")),
        "chg1": to_float(chg.get("h1")),
        "chg6": to_float(chg.get("h6")),
        "chg24": to_float(chg.get("h24")),
        "buys5": to_float(t5.get("buys")),
        "sells5": to_float(t5.get("sells")),
        "age_min": age_min,
    }
    m["vol_pace"] = (m["vol5"] * 12 / m["vol1"]) if m["vol1"] > 0 else 0.0
    m["h1_vs_h6"] = (m["vol1"] / (m["vol6"] / 6)) if m["vol6"] > 0 else 0.0
    m["liq_mcap"] = (m["liq"] / m["mcap"]) if m["mcap"] > 0 else 0.0
    total5 = m["buys5"] + m["sells5"]
    m["buy_ratio5"] = (m["buys5"] / total5) if total5 > 0 else 0.0
    return m


def pre_filter(m: dict) -> Optional[str]:
    if m["dex"] == "pumpfun":
        return "لم تهاجر من Bonding Curve بعد"
    if m["price"] <= 0:
        return "بيانات سعر غير صالحة"
    if m["liq"] < MIN_LIQUIDITY_USD:
        return "سيولة منخفضة"
    if not (MIN_MCAP_USD <= m["mcap"] <= MAX_MCAP_USD):
        return "الماركت كاب خارج النطاق"
    if m["age_min"] is None or m["age_min"] < MIN_PAIR_AGE_MIN:
        return "عمر الزوج قصير جداً"
    if m["vol1"] < MIN_VOLUME_H1_USD:
        return "حجم تداول 1h ضعيف"
    if m["chg5"] <= -MAX_5M_DROP_PCT or m["chg1"] <= -MAX_1H_DROP_PCT:
        return "انهيار حاد في السعر"
    if m["chg1"] > MAX_H1_CHANGE_PCT:
        return "ارتفاع مفرط خلال ساعة (خطر قمة)"
    return None


def reason_detail(m: dict, reason: str) -> str:
    """يحوّل سبب الفلتر الأولي إلى جملة بالأرقام الفعلية والحد المطلوب."""
    try:
        if reason == "سيولة منخفضة":
            return f"سيولة {fmt_usd(m['liq'])} (الحد الأدنى {fmt_usd(MIN_LIQUIDITY_USD)})"
        if reason == "الماركت كاب خارج النطاق":
            return f"ماركت كاب {fmt_usd(m['mcap'])} (النطاق {fmt_usd(MIN_MCAP_USD)}–{fmt_usd(MAX_MCAP_USD)})"
        if reason == "عمر الزوج قصير جداً":
            if m["age_min"] is None:
                return "عمر الزوج غير معروف"
            return f"عمر الزوج {m['age_min']:.0f} دقيقة (الحد الأدنى {MIN_PAIR_AGE_MIN:.0f})"
        if reason == "حجم تداول 1h ضعيف":
            return f"فوليوم 1h {fmt_usd(m['vol1'])} (الحد الأدنى {fmt_usd(MIN_VOLUME_H1_USD)})"
        if reason == "انهيار حاد في السعر":
            return f"هبوط 5د {m['chg5']:+.0f}% / 1h {m['chg1']:+.0f}%"
        if reason.startswith("ارتفاع مفرط"):
            return f"صعود 1h {m['chg1']:+.0f}% (الحد {MAX_H1_CHANGE_PCT:.0f}%)"
        if reason.startswith("لم تهاجر"):
            return "ما زالت على Bonding Curve (لا LP للفحص) — تُقبل بعد الهجرة"
    except Exception:
        pass
    return reason


def sec_detail(sec: "Security") -> str:
    nums = [f"Top10 {sec.top10:.0f}%"]
    if sec.dev is not None:
        nums.append(f"Dev {sec.dev:.1f}%")
    if sec.lp_locked is not None:
        nums.append(f"LP {sec.lp_locked:.0f}%")
    if sec.rug_norm is not None:
        nums.append(f"RugCheck {sec.rug_norm:.0f}/100")
    return " + ".join(sec.reasons) + " [" + " • ".join(nums) + "]"


def prelim_rank(m: dict) -> float:
    return (
        min(m["vol_pace"], 4)
        + min(m["h1_vs_h6"], 6) / 2
        + min(m["liq_mcap"], 0.2) * 10
        + (1.0 if m["chg1"] > 0 else 0.0)
    )


# ───────────────────────────── الفلترة الأمنية (RugCheck) ─────────────────────────────


@dataclass
class Security:
    passed: bool
    reasons: list = field(default_factory=list)      # حظر صلب
    warnings: list = field(default_factory=list)     # مخاطرة أعلى (المستوى B)
    top10: float = 0.0
    dev: Optional[float] = None
    lp_locked: Optional[float] = None
    rug_norm: Optional[float] = None
    rug_raw: Optional[float] = None
    bundled: bool = False


_RUG_CACHE: dict = {}


async def get_rugcheck_report(client: httpx.AsyncClient, mint: str) -> Optional[dict]:
    cached = _RUG_CACHE.get(mint)
    if cached and time.time() - cached[0] < RUGCHECK_CACHE_SEC:
        return cached[1]
    data = await fetch_json(client, f"{RUGCHECK}/tokens/{mint}/report", retries=2)
    await asyncio.sleep(0.2)
    if isinstance(data, dict):
        _RUG_CACHE[mint] = (time.time(), data)
        return data
    return None


def analyze_security(report: dict, pair_address: str) -> Security:
    reasons: list = []
    soft: list = []
    token = report.get("token") if isinstance(report.get("token"), dict) else {}

    def authority_revoked(key: str) -> Optional[bool]:
        for src in (token, report):
            if key in src:
                return src[key] in (None, "", "null")
        return None

    mint_rev = authority_revoked("mintAuthority")
    if mint_rev is not True:
        reasons.append("Mint Authority غير ملغاة" if mint_rev is False else "تعذر التحقق من Mint Authority")
    freeze_rev = authority_revoked("freezeAuthority")
    if freeze_rev is not True:
        reasons.append("Freeze Authority غير ملغاة" if freeze_rev is False else "تعذر التحقق من Freeze Authority")

    if report.get("rugged") is True:
        reasons.append("التوكن مُعلَّم كـ Rugged")
    tf = report.get("transferFee")
    if isinstance(tf, dict) and to_float(tf.get("pct")) > 0:
        reasons.append("رسوم تحويل على التوكن")

    risks = [r for r in (report.get("risks") or []) if isinstance(r, dict)]
    if any(str(r.get("level", "")).lower() == "danger" for r in risks):
        reasons.append("RugCheck: مخاطر بمستوى Danger")

    norm = report.get("score_normalised")
    raw = report.get("score")
    rug_norm = to_float(norm, -1.0) if norm is not None else None
    rug_raw = to_float(raw, -1.0) if raw is not None else None
    if rug_norm is not None:
        if rug_norm > RISKY_MAX_RUG_NORM:
            reasons.append("تقييم RugCheck سيئ")
        elif rug_norm > RUGCHECK_MAX_NORM:
            soft.append(f"RugCheck ليس Good (خطورة {rug_norm:.0f}/100)")
    elif rug_raw is not None:
        if rug_raw > RUGCHECK_MAX_RAW:
            reasons.append("تقييم RugCheck سيئ")
    else:
        reasons.append("تعذر قراءة تقييم RugCheck")

    markets = [mk for mk in (report.get("markets") or []) if isinstance(mk, dict)]
    chosen = next((mk for mk in markets if mk.get("pubkey") == pair_address), None)
    if chosen is None and markets:
        chosen = max(
            markets,
            key=lambda mk: to_float((mk.get("lp") or {}).get("baseUSD"))
            + to_float((mk.get("lp") or {}).get("quoteUSD")),
        )
    lp_locked: Optional[float] = None
    if chosen is not None and isinstance(chosen.get("lp"), dict) and "lpLockedPct" in chosen["lp"]:
        lp_locked = to_float(chosen["lp"].get("lpLockedPct"))
    if lp_locked is None:
        reasons.append("تعذر التحقق من حالة LP")
    elif lp_locked < MIN_LP_LOCKED_PCT:
        reasons.append("LP غير محروقة/مقفولة بالكامل")

    pool_accounts = set()
    for mk in markets:
        for key in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
            if mk.get(key):
                pool_accounts.add(mk[key])
    all_holders = [h for h in (report.get("topHolders") or []) if isinstance(h, dict)]
    real_holders = [
        h for h in all_holders
        if h.get("address") not in pool_accounts and h.get("owner") not in pool_accounts
    ]
    real_holders.sort(key=lambda h: to_float(h.get("pct")), reverse=True)
    top10 = sum(to_float(h.get("pct")) for h in real_holders[:10])
    if not all_holders:
        reasons.append("تعذر جلب بيانات الحيتان")
    elif top10 > RISKY_MAX_TOP10_PCT:
        reasons.append("تركّز كبار الملاك Top10 مرتفع جداً")
    elif top10 > MAX_TOP10_PCT:
        soft.append(f"Top10 مرتفع ({top10:.0f}%)")

    dev: Optional[float] = None
    creator = report.get("creator")
    if creator:
        dev = 0.0
        for h in all_holders:
            if h.get("address") == creator or h.get("owner") == creator:
                dev = max(dev, to_float(h.get("pct")))
        supply = to_float(token.get("supply"))
        creator_bal = to_float(report.get("creatorBalance"))
        if supply > 0 and creator_bal > 0:
            dev = max(dev, creator_bal / supply * 100)
        if dev > RISKY_MAX_DEV_PCT:
            reasons.append("حصة المطور Dev مرتفعة جداً")
        elif dev > MAX_DEV_PCT:
            soft.append(f"حصة المطور {dev:.1f}%")

    bundled = (
        any(h.get("insider") for h in real_holders)
        or to_float(report.get("graphInsidersDetected")) > 0
        or bool(report.get("insiderNetworks"))
        or any(
            kw in str(r.get("name", "")).lower()
            for r in risks
            for kw in ("bundl", "snip", "insider")
        )
    )
    if bundled:
        soft.append("أنماط Bundled/Sniped/Insider")

    if soft and not ALLOW_RISKY:
        reasons.extend(soft)
        soft = []

    return Security(
        passed=not reasons,
        reasons=reasons,
        warnings=soft,
        top10=top10,
        dev=dev,
        lp_locked=lp_locked,
        rug_norm=rug_norm,
        rug_raw=rug_raw,
        bundled=bundled,
    )


# ───────────────────────────── الخطط (Scalp / Momentum / Hold) ─────────────────────────────


@dataclass
class ScalpPlan:
    swing_low: float
    swing_high: float
    zone_low: float
    zone_high: float
    status: str            # IN_ZONE | ABOVE_ZONE | WAIT | MOMENTUM | HOLD
    above_pct: float
    entry: float
    tp1: float
    tp2: float
    ext1272: float
    ext1618: float
    sl: float
    risk_pct: float
    rr1: float
    rr2: float
    leg_pct: float
    kind: str = "SCALP"


async def fetch_candles(client: httpx.AsyncClient, pool: str) -> list:
    data = await fetch_json(
        client,
        f"{GECKO}/networks/solana/pools/{pool}/ohlcv/minute",
        params={"aggregate": 5, "limit": 100, "currency": "usd"},
    )
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") if isinstance(data, dict) else None
    candles = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            ts, o, h, l, c, v = (to_float(x) for x in row[:6])
            if h > 0 and l > 0 and h >= l:
                candles.append((ts, o, h, l, c, v))
    candles.sort(key=lambda x: x[0])
    return candles


def compute_scalp_plan(candles: list, price: float):
    """SCALP: يرجع (ScalpPlan, None) أو (None, سبب_الرفض). WAIT = السعر بعيد فوق المنطقة."""
    if len(candles) < MIN_CANDLES:
        return None, "شموع غير كافية لحساب Fibonacci"

    window = candles[-MAX_WINDOW_CANDLES:]
    hi_idx = max(range(len(window)), key=lambda i: window[i][2])
    high = window[hi_idx][2]
    seg = window[max(0, hi_idx - SWING_LOOKBACK_CANDLES): hi_idx + 1]
    lo_rel = min(range(len(seg)), key=lambda i: seg[i][3])
    low = seg[lo_rel][3]
    if lo_rel == len(seg) - 1:
        return None, "لا توجد موجة صاعدة واضحة (Swing Low/High)"
    if price > high:
        high = price

    rng = high - low
    if low <= 0 or rng / low * 100 < MIN_LEG_PCT:
        return None, "الموجة الصاعدة ضعيفة"

    zone_high = high - 0.5 * rng
    zone_low = high - 0.618 * rng
    sl = low * (1 - SL_BUFFER)

    if zone_low * 0.99 <= price <= zone_high * 1.01:
        status, entry, above_pct = "IN_ZONE", price, 0.0
    elif price > zone_high * 1.01:
        above_pct = (price / zone_high - 1) * 100
        status = "ABOVE_ZONE" if above_pct <= MAX_ABOVE_ZONE_PCT else "WAIT"
        entry = (zone_low + zone_high) / 2
    else:
        return None, "السعر كسر المنطقة الذهبية للأسفل"

    if entry <= sl:
        return None, "وقف الخسارة فوق نقطة الدخول"
    risk_pct = (entry - sl) / entry * 100
    if risk_pct > MAX_SL_RISK_PCT:
        return None, "وقف الخسارة بعيد جداً (مخاطرة عالية)"

    return ScalpPlan(
        swing_low=low,
        swing_high=high,
        zone_low=zone_low,
        zone_high=zone_high,
        status=status,
        above_pct=above_pct,
        entry=entry,
        tp1=entry * (1 + TP1_PCT),
        tp2=entry * (1 + TP2_PCT),
        ext1272=low + 1.272 * rng,
        ext1618=low + 1.618 * rng,
        sl=sl,
        risk_pct=risk_pct,
        rr1=(TP1_PCT * 100) / risk_pct,
        rr2=(TP2_PCT * 100) / risk_pct,
        leg_pct=rng / low * 100,
        kind="SCALP",
    ), None


def momentum_ok(m: dict) -> bool:
    return (
        3 <= m["chg1"] <= 150
        and m["chg5"] >= -1
        and m["vol_pace"] >= 0.8
        and m["buy_ratio5"] >= 0.45
    )


def hold_ok(m: dict) -> bool:
    age = m["age_min"] or 0
    return (
        age >= 1440
        and m["liq"] >= 40_000
        and m["chg24"] > 0
        and m["chg1"] >= -5
        and m["mcap"] > 0
        and m["vol24"] / m["mcap"] >= 0.2
    )


def compute_alt_plan(kind: str, m: dict, candles: list) -> ScalpPlan:
    price = m["price"]
    if kind == "MOMENTUM":
        zone_low, zone_high = price * 0.97, price
        tp1, tp2 = price * 1.15, price * 1.30
        sl = price * 0.88
        if candles:
            recent_low = min(c[3] for c in candles[-8:])
            sl = clamp(recent_low * 0.985, price * 0.85, price * 0.93)
    else:  # HOLD
        zone_low, zone_high = price * 0.92, price
        tp1, tp2 = price * 1.50, price * 2.00
        sl = price * 0.75
    risk_pct = (price - sl) / price * 100
    return ScalpPlan(
        swing_low=0.0,
        swing_high=0.0,
        zone_low=zone_low,
        zone_high=zone_high,
        status=kind,
        above_pct=0.0,
        entry=price,
        tp1=tp1,
        tp2=tp2,
        ext1272=0.0,
        ext1618=0.0,
        sl=sl,
        risk_pct=risk_pct,
        rr1=((tp1 / price - 1) * 100) / risk_pct,
        rr2=((tp2 / price - 1) * 100) / risk_pct,
        leg_pct=0.0,
        kind=kind,
    )


def compute_score(m: dict, sec: Security, plan: ScalpPlan):
    """Score /100 = سيولة/MC (20) + فوليوم (20) + زخم (10) + جودة الدخول (25) + أمان (25) − عقوبة مخاطرة."""
    b: dict = {}
    b["liq_mcap"] = 20 * clamp(m["liq_mcap"] / 0.10, 0, 1)

    pace = 8 * clamp(m["vol_pace"] / 2.0, 0, 1)
    trend = 7 * clamp(m["h1_vs_h6"] / 3.0, 0, 1)
    buys = 5 * clamp((m["buy_ratio5"] - 0.5) / 0.2, 0, 1)
    b["volume"] = pace + trend + buys

    mom = 0.0
    if m["chg5"] > 0:
        mom += 4 if m["chg5"] <= 25 else 2
    if m["chg1"] > 0:
        mom += 6 if m["chg1"] <= 150 else 3
    b["momentum"] = mom

    if plan.kind == "SCALP":
        if plan.status == "IN_ZONE":
            b["fib"] = 25.0
        elif plan.status == "ABOVE_ZONE":
            b["fib"] = 18 * (1 - 0.5 * clamp(plan.above_pct / MAX_ABOVE_ZONE_PCT, 0, 1))
        else:
            b["fib"] = 6.0
    elif plan.kind == "MOMENTUM":
        b["fib"] = 14.0
    else:
        b["fib"] = 12.0

    safety = 10 * (1 - clamp(sec.top10 / MAX_TOP10_PCT, 0, 1))
    safety += 5 * (1 - clamp((sec.dev or 0.0) / MAX_DEV_PCT, 0, 1)) if sec.dev is not None else 2.5
    if sec.rug_norm is not None:
        safety += 10 * (1 - clamp(sec.rug_norm / RUGCHECK_MAX_NORM, 0, 1))
    else:
        safety += 5
    b["safety"] = safety
    b["risk_penalty"] = -RISK_PENALTY if sec.warnings else 0.0

    total = int(round(sum(b.values())))
    return int(clamp(total, 0, 100)), b


# ───────────────────────────── المسح الكامل ─────────────────────────────


@dataclass
class Candidate:
    m: dict
    sec: Security
    plan: ScalpPlan
    score: int
    breakdown: dict


@dataclass
class ScanResult:
    ranked: list = field(default_factory=list)       # كل الصفقات مرتبة بالـ Score
    stats: Counter = field(default_factory=Counter)
    sources: dict = field(default_factory=dict)
    signals: int = 0
    discovered: int = 0
    verdicts: dict = field(default_factory=dict)   # mint -> {sym, mcap, icon, text}
    unresolved: list = field(default_factory=list)  # عقود بلا بيانات سوق
    after_prefilter: int = 0
    after_security: int = 0
    finished_at: float = 0.0


def grade_of(c: Candidate) -> str:
    if not c.sec.warnings and c.score >= MIN_SCORE:
        return "🟢 قوية"
    if c.score >= RISKY_MIN_SCORE:
        return "🟡 متوسطة"
    return "🟠 ضعيفة (مضاربة)"


async def run_scan(
    client: httpx.AsyncClient,
    progress=None,
    budget: float = SCAN_BUDGET_SEC,
    extra: Optional[list] = None,
) -> ScanResult:
    t0 = time.time()

    def left() -> float:
        return max(2.0, budget - (time.time() - t0))

    async def say(text: str) -> None:
        if progress:
            try:
                await progress(text)
            except Exception:
                pass

    res = ScanResult()

    def set_v(m: dict, icon: str, text: str) -> None:
        res.verdicts[m["mint"]] = {"sym": m["symbol"], "mcap": m["mcap"], "icon": icon, "text": text}

    # 1) قراءة القناة
    await say(f"📡 (1/4) قراءة آخر إشارات قناة @{SIGNAL_CHANNEL} ...")
    signals, res.sources = await discover_signals(client)
    if extra:  # عقود أرسلها المستخدم يدوياً (تُدمج مع عقود القناة، وعقد القناة له الأولوية)
        have = {s.mint for s in signals}
        signals = [e for e in extra if e.mint not in have] + signals
        signals = signals[:MAX_DISCOVERED_TOKENS]
        res.sources["manual"] = len(extra)
    res.signals = len(signals)
    if not signals:
        res.finished_at = time.time()
        return res
    sig_map = {s.mint: s for s in signals}
    addrs = [s.mint for s in signals]
    hints = [s.mint for s in signals if s.is_hint]

    # 2) بيانات الأزواج
    await say(f"📊 (2/4) جلب بيانات {len(addrs)} عقد من القناة...")
    sink: dict = {}
    try:
        await asyncio.wait_for(fetch_pairs(client, addrs, hints, sink), timeout=min(35.0, left()))
    except asyncio.TimeoutError:
        log.warning("fetch_pairs timed out (partial=%d)", len(sink))
    pairs = list(sink.values())
    res.discovered = len(pairs)

    pre = []
    matched: set = set()
    for p in pairs:
        m = extract_metrics(p)
        if m is None:
            res.stats["بيانات ناقصة"] += 1
            continue
        sig = sig_map.get(m["mint"]) or sig_map.get(m["pair"])
        if sig:
            m["signal_ts"], m["signal_post"], m["signal_manual"] = sig.ts, sig.post_id, sig.manual
            matched.add(sig.mint)
        reason = pre_filter(m)
        if reason:
            res.stats[reason] += 1
            set_v(m, "❌", reason_detail(m, reason))
        else:
            pre.append(m)
    res.unresolved = [s.mint for s in signals if s.mint not in matched]
    if pairs and len(signals) > len(matched):
        res.stats["عقد بلا زوج على DexScreener (جديد جداً أو ليس عملة)"] += len(signals) - len(matched)
    res.after_prefilter = len(pre)
    pre.sort(key=prelim_rank, reverse=True)
    for m in pre[MAX_SECURITY_CHECKS:]:
        set_v(m, "⏭️", f"لم يُفحص أمنياً (حد الفحص {MAX_SECURITY_CHECKS} عملة) — ليس حكماً على العملة")
    pre = pre[:MAX_SECURITY_CHECKS]

    # 3) الأمان
    await say(f"🛡️ (3/4) فحص أمان {len(pre)} عملة عبر RugCheck...")
    sem = asyncio.Semaphore(4)

    async def check(m: dict):
        async with sem:
            report = await get_rugcheck_report(client, m["mint"])
        return m, (analyze_security(report, m["pair"]) if report else None)

    task_map = {asyncio.create_task(check(m)): m for m in pre}
    tasks = list(task_map)
    secure: list = []
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=max(5.0, min(45.0, left() - 25)))
        for t in pending:
            t.cancel()
            set_v(task_map[t], "⏱️", "انتهت مهلة RugCheck — لم تُفحص (ليس حكماً على العملة)")
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            res.stats["انتهت مهلة فحص RugCheck (لم تُفحص)"] += len(pending)
        for t in done:
            if t.cancelled() or t.exception() is not None:
                res.stats["خطأ أثناء فحص RugCheck"] += 1
                set_v(task_map[t], "⚠️", "خطأ أثناء فحص RugCheck — ليس حكماً على العملة")
                continue
            m, sec = t.result()
            if sec is None:
                res.stats["تعذر جلب تقرير RugCheck"] += 1
                set_v(m, "⚠️", "تعذر جلب تقرير RugCheck (حد طلبات/غير مفهرسة) — ليس حكماً على العملة")
            elif not sec.passed:
                for r in sec.reasons:
                    res.stats[r] += 1
                set_v(m, "🛡️", sec_detail(sec))
            else:
                secure.append((m, sec))
    res.after_security = len(secure)

    # 4) الخطط
    await say(f"📈 (4/4) تحليل {len(secure)} عملة آمنة وبناء الخطط...")
    secure.sort(key=lambda ms: prelim_rank(ms[0]), reverse=True)
    candle_map: dict = {}
    top = [m for m, _ in secure[:MAX_CANDLE_FETCH]]
    if top and left() > 6:
        csem = asyncio.Semaphore(2)

        async def get_candles(m: dict) -> list:
            async with csem:
                c = await fetch_candles(client, m["pair"])
                await asyncio.sleep(0.6)
                return c

        ctasks = {m["mint"]: asyncio.create_task(get_candles(m)) for m in top}
        done, pending = await asyncio.wait(ctasks.values(), timeout=max(4.0, left() - 3))
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for mint, t in ctasks.items():
            if t in done and not t.cancelled() and t.exception() is None:
                candle_map[mint] = t.result()

    ranked: list = []
    for m, sec in secure:
        candles = candle_map.get(m["mint"], [])
        plans = []
        scalp_reason = None
        if candles:
            plan, reject = compute_scalp_plan(candles, m["price"])
            if plan is not None and plan.status != "WAIT":
                plans.append(plan)
            elif reject:
                res.stats[reject] += 1
                scalp_reason = reject
            else:
                scalp_reason = "السعر بعيد فوق المنطقة الذهبية (انتظر تصحيحاً)"
        if momentum_ok(m):
            plans.append(compute_alt_plan("MOMENTUM", m, candles))
        if hold_ok(m):
            plans.append(compute_alt_plan("HOLD", m, candles))
        if not plans:
            res.stats["لا تطابق أي نمط (سكالبيج/زخم/احتفاظ)"] += 1
            detail = (
                f"اجتازت الأمان لكن بلا نمط دخول — 1h {m['chg1']:+.0f}% • 5د {m['chg5']:+.0f}% • "
                f"وتيرة الفوليوم {m['vol_pace']:.1f}x • شراء 5د {m['buy_ratio5'] * 100:.0f}%"
            )
            detail += f" • سكالبيج: {scalp_reason}" if scalp_reason else " • سكالبيج: لم تُجلب شموع"
            set_v(m, "📉", detail)
            continue
        for plan in plans:
            score, breakdown = compute_score(m, sec, plan)
            ranked.append(Candidate(m, sec, plan, score, breakdown))

    ranked.sort(key=lambda c: c.score, reverse=True)
    res.ranked = ranked
    for c in ranked:
        v = res.verdicts.get(c.m["mint"])
        if v is None or v["icon"] != "✅":
            set_v(c.m, "✅", f"Score {c.score} — {KIND_SHORT[c.plan.kind]} ({grade_of(c)})")
    res.finished_at = time.time()
    return res


# ───────────────────────────── تنسيق الرسائل ─────────────────────────────


def format_best(c: Candidate, alts: list) -> str:
    m, s, p = c.m, c.sec, c.plan
    name, sym = html.escape(m["name"]), html.escape(m["symbol"])
    grade = grade_of(c)

    def mc(price: float) -> str:
        return fmt_usd(m["mcap"] * price / m["price"])

    tp1_pct = (p.tp1 / p.entry - 1) * 100
    tp2_pct = (p.tp2 / p.entry - 1) * 100
    risky = bool(s.warnings)
    dev_txt = f"{s.dev:.1f}%" if s.dev is not None else "غير متاح"
    rug_txt = f"خطورة {s.rug_norm:.0f}/100" if s.rug_norm is not None else f"raw {s.rug_raw:.0f}"
    bundle_txt = "🚨 مرصودة أنماط Bundled/Insider" if s.bundled else "✅ لا يوجد"
    b = c.breakdown

    head = (
        f"{grade} — <b>صفقة {html.escape(KIND_LABEL[p.kind])}</b>\n"
        f"📊 التقييم: <b>{c.score}/100</b>\n\n"
    )
    warn_block = ""
    if risky:
        warn_block = (
            "🚨 <b>تحذيرات:</b> " + " • ".join(html.escape(w) for w in s.warnings) + "\n"
            "<i>اجتازت الحظر الصلب (Mint/Freeze/LP/Danger) لكنها ليست نظيفة — حجم صغير جداً.</i>\n\n"
        )

    ident = (
        f"🪙 <b>{name}</b> (${sym})\n"
        f"📋 العقد CA (اضغط للنسخ):\n<code>{m['mint']}</code>\n\n"
    )

    safety = (
        f"🛡️ <b>الأمان والسيولة</b>\n"
        f"• Mint ✅ ملغاة | Freeze ✅ ملغاة\n"
        f"• LP: {s.lp_locked:.0f}% محروقة/مقفولة 🔒\n"
        f"• RugCheck: {rug_txt}\n"
        f"• السيولة: {fmt_usd(m['liq'])} | MC: {fmt_usd(m['mcap'])} | Liq/MC: {m['liq_mcap'] * 100:.1f}%\n"
        f"• 🐋 Top10: {s.top10:.1f}% | Dev: {dev_txt} | Bundles: {bundle_txt}\n\n"
        f"⚡ <b>الزخم</b>: 5m {m['chg5']:+.1f}% | 1h {m['chg1']:+.1f}% | 24h {m['chg24']:+.0f}%\n"
        f"• فوليوم 5m ×{m['vol_pace']:.1f} | ضغط الشراء {m['buy_ratio5'] * 100:.0f}%\n\n"
    )

    if p.kind == "SCALP":
        status = (
            "✅ السعر داخل المنطقة الذهبية الآن"
            if p.status == "IN_ZONE"
            else f"⏳ السعر أعلى المنطقة بـ {p.above_pct:.1f}% — انتظر الارتداد (Limit داخل المنطقة)"
        )
        plan_txt = (
            f"📈 <b>الخطة</b>\n{status}\n"
            f"🟢 Golden Zone (0.5–0.618):\n"
            f"   <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
            f"   (MC: {mc(p.zone_low)} ← {mc(p.zone_high)})\n"
            f"🎯 TP1 (+{tp1_pct:.0f}%): <code>{fmt_price(p.tp1)}</code> | MC {mc(p.tp1)} | Fib 1.272: <code>{fmt_price(p.ext1272)}</code>\n"
            f"🎯 TP2 (+{tp2_pct:.0f}%): <code>{fmt_price(p.tp2)}</code> | MC {mc(p.tp2)} | Fib 1.618: <code>{fmt_price(p.ext1618)}</code>\n"
            f"🛑 SL (تحت القاع بـ 1.5%): <code>{fmt_price(p.sl)}</code> | MC {mc(p.sl)}\n"
            f"⚖️ مخاطرة حتى SL: {p.risk_pct:.1f}% | R:R → TP1 {p.rr1:.2f} / TP2 {p.rr2:.2f}\n"
            f"📐 Swing: {fmt_price(p.swing_low)} → {fmt_price(p.swing_high)} (+{p.leg_pct:.0f}%)\n\n"
        )
    else:
        note = (
            "لا تطارد السعر إن ارتفع أكثر من 5% عن منطقة الدخول."
            if p.kind == "MOMENTUM"
            else "صفقة أبطأ: راقبها يومياً، ولا تعتمد على شمعة واحدة."
        )
        plan_txt = (
            f"📈 <b>الخطة</b>\n"
            f"🟢 منطقة الدخول: <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
            f"   (MC: {mc(p.zone_low)} ← {mc(p.zone_high)})\n"
            f"🎯 TP1 (+{tp1_pct:.0f}%): <code>{fmt_price(p.tp1)}</code> | MC {mc(p.tp1)}\n"
            f"🎯 TP2 (+{tp2_pct:.0f}%): <code>{fmt_price(p.tp2)}</code> | MC {mc(p.tp2)}\n"
            f"🛑 SL: <code>{fmt_price(p.sl)}</code> | MC {mc(p.sl)}\n"
            f"⚖️ مخاطرة حتى SL: {p.risk_pct:.1f}% | R:R → TP1 {p.rr1:.2f} / TP2 {p.rr2:.2f}\n"
            f"<i>{note}</i>\n\n"
        )

    breakdown = (
        f"<i>سيولة/MC {b['liq_mcap']:.0f}/20 • فوليوم {b['volume']:.0f}/20 • زخم {b['momentum']:.0f}/10 "
        f"• دخول {b['fib']:.0f}/25 • أمان {b['safety']:.0f}/25"
        f"{' • عقوبة −' + str(RISK_PENALTY) if risky else ''}</i>\n"
    )
    links = (
        f"🔗 <a href=\"https://dexscreener.com/solana/{m['pair']}\">DexScreener</a> • "
        f"<a href=\"https://rugcheck.xyz/tokens/{m['mint']}\">RugCheck</a>\n"
    )

    alt_txt = ""
    if alts:
        alt_txt = "\n📋 <b>بدائل:</b>\n"
        for a in alts:
            alt_txt += (
                f"• {KIND_SHORT[a.plan.kind]} {html.escape(a.m['name'])} (${html.escape(a.m['symbol'])})"
                f" — {a.score}/100 {grade_of(a)}\n<code>{a.m['mint']}</code>\n"
            )

    disclaimer = (
        "\n⚠️ <i>تحليل آلي وليس نصيحة مالية. لا ضمان للربح — لا تدخل بأكثر مما تتحمل خسارته.</i>"
    )
    sig_txt = ""
    post_id = m.get("signal_post")
    if m.get("signal_manual") and not post_id:
        ts = m.get("signal_ts")
        sig_txt = "📥 <b>المصدر</b>: عقد أرسلتَه يدوياً" + (f" — الإشارة {fmt_age((time.time() - ts) / 60)}" if ts else "") + "\n\n"
    elif post_id:
        ts = m.get("signal_ts")
        age_min = (time.time() - ts) / 60 if ts else None
        when = fmt_age(age_min) if age_min is not None else "وقت غير معروف"
        sig_txt = (
            f"📡 <b>إشارة القناة</b>: {when} — "
            f"<a href=\"https://t.me/{SIGNAL_CHANNEL}/{post_id}\">الرسالة الأصلية</a>\n"
        )
        if age_min is not None and age_min > 120:
            sig_txt += "<i>الإشارة قديمة نسبياً — اعتمد على السعر والمنطقة الحاليين لا على سعر الإشارة.</i>\n"
        sig_txt += "\n"
    return head + warn_block + ident + sig_txt + safety + plan_txt + breakdown + links + alt_txt + disclaimer


def format_no_result(res: ScanResult) -> str:
    lines = [
        f"🚫 <b>لا توجد صفقة من إشارات @{html.escape(SIGNAL_CHANNEL)} تجتاز الحظر الصلب الآن</b>",
        "",
        f"📡 عقود مفحوصة (القناة + يدوي): {res.signals} | وُجد لها زوج: {res.discovered}",
        f"✅ اجتازت الفلتر الأولي: {res.after_prefilter}",
        f"🛡️ اجتازت الحظر الأمني الصلب: {res.after_security}",
    ]
    if res.stats:
        lines += ["", "📋 <b>أسباب الاستبعاد:</b>"]
        for reason, n in res.stats.most_common(12):
            lines.append(f"• {html.escape(reason)}: {n}")
    lines += ["", "💡 جرّب /diag لتشخيص قراءة القناة، أو انتظر إشارات جديدة ثم أعد المحاولة."]
    return "\n".join(lines)


def format_verdicts(res: ScanResult, limit: int = 14) -> str:
    items = sorted(res.verdicts.values(), key=lambda v: (0 if v["icon"] == "✅" else 1, -v["mcap"]))
    total = len(items) + len(res.unresolved)
    lines = [f"📋 <b>تقرير كل عقد ({total})</b>", ""]
    for v in items[:limit]:
        lines.append(f"{v['icon']} <b>{html.escape(v['sym'])}</b> ({fmt_usd(v['mcap'])}) — {html.escape(v['text'])}")
    room = max(0, limit - len(items))
    for a in res.unresolved[:room]:
        lines.append(f"❔ <code>{a[:5]}…{a[-4:]}</code> — لا بيانات سوق من DexScreener/GeckoTerminal (جديدة جداً أو غير مدرجة)")
    shown = min(len(items), limit) + min(len(res.unresolved), room)
    if total > shown:
        lines.append(f"… و{total - shown} أخرى")
    lines += ["", "💡 كل حد أعلاه قابل للتعديل من متغيرات البيئة (MIN_PAIR_AGE_MIN، MIN_LIQUIDITY_USD، MIN_VOLUME_H1_USD…)."]
    return "\n".join(lines)


def format_no_signals(res: ScanResult) -> str:
    info = res.sources if isinstance(res.sources, dict) else {}
    lines = [f"📡 <b>لم أجد عقوداً صالحة في قناة @{html.escape(SIGNAL_CHANNEL)}</b>", ""]
    if info.get("error"):
        lines.append(f"⚠️ {html.escape(str(info['error']))}")
    else:
        lines.append(f"• رسائل مقروءة: {info.get('messages', 0)} | وصلنا لرسائل أقدم من {SIGNAL_MAX_AGE_HOURS:.0f}h: {'نعم' if info.get('expired') else 'لا'}")
        lines.append(f"• رسائل فيها عقود: {info.get('with_addr', 0)}")
    lines += ["", "📥 <b>الوضع اليدوي</b>: حوّل لي (Forward) رسائل الإشارات من القناة، أو الصق العقود هنا، ثم أرسل /best."]
    if not TG_SESSION:
        lines += ["🔑 (للقراءة التلقائية: TELETHON_SESSION بجلسة حساب مستخدم.)"]
    lines += ["", "💡 جرّب /diag للتفاصيل، أو ارفع SIGNAL_MAX_AGE_HOURS إن كانت إشارات القناة قليلة."]
    return "\n".join(lines)


def to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ───────────────────────────── أوامر تلغرام ─────────────────────────────

WELCOME = (
    "🎯 <b>SolAlpha Sniper</b>\n\n"
    "يقرأ آخر إشارات قناة @solhousesignal ويفحص عقودها، ثم يعطيك أفضل صفقة متاحة مع نوعها (سكالبيج / زخم / احتفاظ).\n\n"
    "• /best — أفضل صفقة من إشارات القناة + بدائل\n"
    "• أرسل لي عقداً (أو حوّل رسالة إشارة من القناة) ثم /best لتحليله\n"
    "• /clear — مسح العقود التي أرسلتها\n"
    "• /diag — تشخيص قراءة القناة (Telethon)\n"
    "• /help — شرح الأنواع والفلاتر\n\n"
    "⚠️ أداة تحليل وليست نصيحة مالية."
)

HELP = (
    "📥 <b>الوضع اليدوي</b>: الصق عنوان عقد أو حوّل (Forward) رسالة إشارة إلى البوت، ثم أرسل /best. "
    f"تبقى العقود {MANUAL_TTL_HOURS:.0f} ساعة أو حتى /clear.\n\n"
    f"📡 <b>المصدر</b>: عقود آخر رسائل @{SIGNAL_CHANNEL} فقط (آخر {SIGNAL_MAX_AGE_HOURS:.0f} ساعة) — لا مسح عشوائي للسوق.\n\n"
    "📋 بعد كل /best تصلك رسالة «تقرير كل عقد» بسبب قبول/رفض كل عملة بالأرقام.\n\n"
    "🛡️ <b>حظر صلب دائماً</b>: Mint/Freeze ملغاتان • LP محروقة/مقفولة • ليست Rugged • لا Danger في RugCheck.\n\n"
    f"🧼 <b>المستوى A (نظيف)</b>: Dev ≤ {MAX_DEV_PCT:.0f}% • Top10 ≤ {MAX_TOP10_PCT:.0f}% • بلا Bundled/Insider • RugCheck Good.\n"
    "⚠️ <b>المستوى B</b>: يظهر مع تحذيرات صريحة وعقوبة في التقييم.\n\n"
    "<b>الأنواع</b>\n"
    "⚡ سكالبيج: دخول من Golden Zone، TP +18%/+25%\n"
    "🚀 زخم: صعود بفوليوم متسارع، TP +15%/+30%\n"
    "🧭 احتفاظ: عملة 24h+ بسيولة وفوليوم جيدين، TP +50%/+100%\n\n"
    f"🟢 قوية: A وScore ≥ {MIN_SCORE:.0f} • 🟡 متوسطة: ≥ {RISKY_MIN_SCORE:.0f} • 🟠 ضعيفة: أقل."
)


async def post_init(application: Application) -> None:
    application.bot_data["http"] = httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": "SolAlphaSniper/3.5", "Accept": "application/json"},
        follow_redirects=True,
    )
    application.bot_data["scan_lock"] = asyncio.Lock()
    application.bot_data["cooldowns"] = {}
    application.bot_data["last_scan"] = None
    TL["client"], TL["error"] = await create_telethon()


async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("http")
    if client:
        await client.aclose()
    if TL.get("client"):
        try:
            await TL["client"].disconnect()
        except Exception:
            pass


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


def _cooldown_wait(bd: dict, user_id: int) -> int:
    now = time.time()
    last = bd["cooldowns"].get(user_id, 0)
    if now - last < USER_COOLDOWN_SEC:
        return int(USER_COOLDOWN_SEC - (now - last)) + 1
    bd["cooldowns"][user_id] = now
    return 0


async def safe_edit(status_msg, original_msg, text: str) -> None:
    """يضمن وصول الرد دائماً: HTML ثم نص عادي ثم رسالة جديدة."""
    try:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    except Exception:
        log.exception("HTML edit failed")
    plain = to_plain(text)[:4000]
    try:
        await status_msg.edit_text(plain, disable_web_page_preview=True)
        return
    except Exception:
        log.exception("Plain edit failed")
    try:
        await original_msg.reply_text(plain, disable_web_page_preview=True)
    except Exception:
        log.exception("Reply failed")


async def safe_send(msg, text: str) -> None:
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        log.exception("HTML send failed")
        try:
            await msg.reply_text(to_plain(text)[:4000], disable_web_page_preview=True)
        except Exception:
            log.exception("Plain send failed")


def _manual_store(bd: dict, uid: int) -> dict:
    store = bd.setdefault("manual", {}).setdefault(uid, {})
    cutoff = time.time() - MANUAL_TTL_HOURS * 3600
    for k in [k for k, s in store.items() if s.added < cutoff]:
        store.pop(k, None)
    return store


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يلتقط العقود من رسالة نصية أو محوّلة (Forward) ويحفظها لهذا المستخدم."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    parts = [msg.text or msg.caption or ""]
    for ent in list(msg.entities or []) + list(msg.caption_entities or []):
        if getattr(ent, "url", None):
            parts.append(ent.url)                       # رابط مخفي خلف نص
    rm = getattr(msg, "reply_markup", None)
    for row in getattr(rm, "inline_keyboard", None) or []:
        for b in row:
            if getattr(b, "url", None):
                parts.append(b.url)                     # أزرار الرسالة المحوّلة
    mints, hints = extract_addresses("\n".join(parts))
    if not (mints or hints):
        await msg.reply_text("لم أجد عنوان عقد Solana في هذه الرسالة. الصق العقد أو حوّل لي رسالة الإشارة، ثم أرسل /best.")
        return

    ts, post_id = None, 0                               # ts يبقى None للصق العادي
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
        d = getattr(fo, "date", None)
        ts = d.timestamp() if d else None
        chat = getattr(fo, "chat", None)
        if chat is not None and (getattr(chat, "username", "") or "").lower() == SIGNAL_CHANNEL.lower():
            post_id = getattr(fo, "message_id", 0) or 0
    elif getattr(msg, "forward_date", None):            # توافق مع إصدارات PTB الأقدم
        ts = msg.forward_date.timestamp()
        fc = getattr(msg, "forward_from_chat", None)
        if fc is not None and (getattr(fc, "username", "") or "").lower() == SIGNAL_CHANNEL.lower():
            post_id = getattr(msg, "forward_from_message_id", 0) or 0

    store = _manual_store(context.application.bot_data, user.id)
    now = time.time()
    for a in mints:
        store[a] = Signal(a, post_id, ts, False, True, now)
    for a in hints:
        if a not in store:
            store[a] = Signal(a, post_id, ts, True, True, now)
    while len(store) > MANUAL_MAX:
        store.pop(min(store, key=lambda k: store[k].added), None)
    await msg.reply_text(
        f"✅ استلمت {len(mints) + len(hints)} عقد. إجمالي المنتظر للتحليل: {len(store)}.\n"
        "حوّل المزيد، أو أرسل /best للتحليل (و/clear للمسح)."
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = _manual_store(context.application.bot_data, update.effective_user.id)
    n = len(store)
    store.clear()
    await update.effective_message.reply_text(f"🧹 مُسح {n} عقد.")


async def best_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data

    wait = _cooldown_wait(bd, update.effective_user.id)
    if wait:
        await msg.reply_text(f"⏱️ انتظر {wait} ثانية قبل فحص جديد.")
        return

    status_msg = await msg.reply_text("🔎 بدأ المسح...")

    async def progress(text: str) -> None:
        await status_msg.edit_text(text)

    res = None
    try:
        extras = list(_manual_store(bd, update.effective_user.id).values())
        cached = bd.get("last_scan") if not extras else None
        if cached and time.time() - cached.finished_at < SCAN_CACHE_SEC:
            res = cached
        else:
            async with bd["scan_lock"]:
                res = await asyncio.wait_for(
                    run_scan(bd["http"], progress, SCAN_BUDGET_SEC, extras), timeout=SCAN_BUDGET_SEC + 30
                )
                bd["last_scan"] = res if (res.discovered > 0 and not extras) else None

        if res.signals == 0:
            text = format_no_signals(res)
        elif res.discovered == 0:
            text = (
                f"⚠️ وجدتُ {res.signals} عقداً لكن لم أحصل على بيانات سوق لأي منها.\n\n"
                f"حالة المصادر:\n{api_status_text()}\n\n"
                "• HTTP 403/429 أو timeout ⇒ المشكلة في اتصال سيرفر Render بالمصدر (جرّب بعد دقيقة).\n"
                "• HTTP 200 ⇒ العقود غير مدرجة بعد (جديدة جداً أو ما زالت قبل الإدراج).\n"
                "شغّل /diag لاختبار المصادر مباشرة."
            )
        elif res.ranked:
            best = res.ranked[0]
            alts, seen = [], {best.m["mint"]}
            for c in res.ranked[1:]:
                if c.m["mint"] in seen:
                    continue
                seen.add(c.m["mint"])
                alts.append(c)
                if len(alts) == 2:
                    break
            text = format_best(best, alts)
        else:
            text = format_no_result(res)
    except asyncio.TimeoutError:
        text = "⌛ انتهت مهلة المسح (المصادر بطيئة). حاول مجدداً بعد قليل."
    except Exception:
        log.exception("Scan failed")
        text = "❌ حدث خطأ أثناء المسح. حاول مجدداً بعد قليل."

    await safe_edit(status_msg, msg, text)
    if res is not None and (res.verdicts or res.unresolved):
        await safe_send(msg, format_verdicts(res))


async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data

    wait = _cooldown_wait(bd, update.effective_user.id)
    if wait:
        await msg.reply_text(f"⏱️ انتظر {wait} ثانية.")
        return

    status_msg = await msg.reply_text("🧪 جارٍ تشخيص قراءة القناة ...")
    try:
        async with bd["scan_lock"]:
            client = bd["http"]
            signals, info = await asyncio.wait_for(discover_signals(client), timeout=60)
            addrs = [s.mint for s in signals]
            hints = [s.mint for s in signals if s.is_hint]
            sink: dict = {}
            if addrs:
                try:
                    await asyncio.wait_for(fetch_pairs(client, addrs, hints, sink), timeout=40)
                except asyncio.TimeoutError:
                    pass
            pairs = list(sink.values())
            probes = await probe_apis(client)
        stats: Counter = Counter()
        passed = 0
        for p in pairs:
            m = extract_metrics(p)
            if m is None:
                stats["بيانات ناقصة"] += 1
                continue
            reason = pre_filter(m)
            if reason:
                stats[reason] += 1
            else:
                passed += 1

        manual_n = len(_manual_store(bd, update.effective_user.id))
        age = info.get("newest_age_min")
        lines = [f"🧪 <b>تشخيص قراءة القناة</b> (@{html.escape(SIGNAL_CHANNEL)})", ""]
        lines.append(f"• Telethon: {'✅ متصل (جلسة مستخدم)' if TL.get('client') else '❌ غير متصل'}")
        lines.append(f"• قراءة القناة: {'✅ نجحت عبر ' + str(info['source']) if info['ok'] else '❌ فشلت'}")
        shown = info["errors"] if not info["ok"] else [e for e in info["errors"] if not e.startswith("Telethon") or info["source"] != "Telethon"]
        for er in shown[:4]:
            lines.append(f"   ↳ {html.escape(str(er))}")
        lines += [
            f"• رسائل مقروءة: {info['messages']} ",
            f"• رسائل فيها عقود: {info['with_addr']}",
            f"• أحدث رسالة: {fmt_age(age) if age is not None else 'غير معروف'}",
            f"• عقود أرسلتها يدوياً (منتظرة): {manual_n}",
            "",
            f"🔢 عقود فريدة مستخرجة: {len(addrs)} | أزواج DexScreener: {len(pairs)}",
            f"✅ اجتازت الفلتر الأولي: {passed}",
        ]
        missing = len(addrs) - len(pairs)
        if missing > 0 and pairs:
            lines.append(f"• بلا زوج على DexScreener: ~{missing}")
        lines += ["", "🌐 <b>اختبار المصادر من السيرفر:</b>"] + [f"• {n}: {r}" for n, r in probes]
        if stats:
            lines += ["", "📋 <b>أسباب الاستبعاد:</b>"]
            for reason, n in stats.most_common(12):
                lines.append(f"• {html.escape(reason)}: {n}")
        text = "\n".join(lines)
    except Exception:
        log.exception("Diag failed")
        text = "❌ فشل التشخيص. راجع Logs في Render."

    await safe_edit(status_msg, msg, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)


# ───────────────────────────── Health server + Keep-alive + التشغيل ─────────────────────────────


async def handle_health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health check server running on port %s", port)
    return runner


async def keepalive_loop(client: httpx.AsyncClient) -> None:
    """يمنع نوم خدمة Render المجانية: طلب HTTP دوري على الرابط العام للخدمة."""
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
    if not url:
        log.info("Keep-alive disabled (no RENDER_EXTERNAL_URL / SELF_URL)")
        return
    target = url.rstrip("/") + "/health"
    log.info("Keep-alive pinging %s every %ss", target, KEEPALIVE_SEC)
    while True:
        await asyncio.sleep(KEEPALIVE_SEC)
        try:
            await client.get(target)
        except Exception as e:
            log.warning("Keep-alive failed: %s", e)


async def run_all() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("best", best_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT | filters.CAPTION), on_user_message
    ))
    app.add_error_handler(on_error)

    runner = await start_web_server()
    await app.initialize()
    await post_init(app)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    keepalive = asyncio.create_task(keepalive_loop(app.bot_data["http"]))
    log.info("SolAlpha Sniper v3.5 is running — signal channel: @%s", SIGNAL_CHANNEL)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await stop.wait()
    finally:
        log.info("Shutting down...")
        keepalive.cancel()
        try:
            await app.updater.stop()
            await app.stop()
            await post_shutdown(app)
            await app.shutdown()
        finally:
            await runner.cleanup()


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN غير مضبوط في متغيرات البيئة.")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
