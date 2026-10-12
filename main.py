# -*- coding: utf-8 -*-
"""
SolAlpha Sniper v3.4 — Telegram bot (Render Web Service)
مصدر العملات: قناة الإشارات https://t.me/solhousesignal حصرياً عبر HTTP Mirrors.

/best : يقرأ آخر رسائل القناة → يستخرج عقود Solana → يفحصها ويعطيك أفضل صفقة + نوعها + بدائل
/diag : تشخيص قراءة القناة وفلترة العقود
/help : شرح

أنواع الصفقات:
  ⚡ SCALP    : دخول من Fibonacci Golden Zone (0.5–0.618)، هدف +18% / +25%
  🚀 MOMENTUM : عملة تصعد بفوليوم متسارع، هدف +15% / +30%
  🧭 HOLD     : عملة أقدم (24h+) بسيولة وفوليوم جيدين، هدف +50% / +100%

الحظر الصلب (لا يُخفَّف أبداً): Mint/Freeze ملغاة، LP محروقة/مقفولة، ليست Rugged، لا Danger في RugCheck.
أما Dev/Top10/Insider/تقييم RugCheck فتخفض الـ Score وتظهر كتحذيرات (المستوى B).

متغيرات البيئة:
  TELEGRAM_BOT_TOKEN (مطلوب) | PORT و RENDER_EXTERNAL_URL (تضبطهما Render)
  SIGNAL_HTTP_MIRRORS (اختياري) : قوالب روابط مفصولة بفاصلة، فيها {ch}، تُجرَّب بالترتيب
  SIGNAL_CHANNEL (افتراضي solhousesignal) | SIGNAL_PAGES (افتراضي 2) | SIGNAL_MAX_AGE_HOURS (افتراضي 24)
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
from telegram.ext import Application, CommandHandler, ContextTypes

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

# قناة الإشارات
SIGNAL_CHANNEL = os.getenv("SIGNAL_CHANNEL", "solhousesignal").strip().lstrip("@").replace("https://t.me/", "")
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
            if r.status_code == 429:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            if r.status_code != 200:
                log.warning("HTTP %s from %s", r.status_code, url)
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Request failed (%s): %s", url, e)
            await asyncio.sleep(0.6 * (attempt + 1))
    return None


# ───────────────────────────── اكتشاف العملات (من قناة الإشارات) ─────────────────────────────

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {ch: i for i, ch in enumerate(B58_ALPHABET)}

SOL_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
DEX_PAIR_URL_RE = re.compile(r"(?i:dexscreener\.com)/solana/([1-9A-HJ-NP-Za-km-z]{32,44})")
EXPLORER_URL_RE = re.compile(r"(?i:solscan\.io|solana\.fm|explorer\.solana\.com)/(?:account|address)/\S+")


@dataclass
class Signal:
    mint: str
    post_id: int
    ts: Optional[float]
    is_hint: bool = False


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
    s = _ANCHOR_RE.sub(lambda m: f" {m.group(1)} {m.group(2)} ", fragment)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def parse_channel_html(page: str) -> list:
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


async def discover_signals(client: httpx.AsyncClient):
    info = {
        "ok": False, "error": None, "errors": [], "source": None, "messages": 0, "expired": 0,
        "with_addr": 0, "addresses": 0, "newest_age_min": None, "newest_ts": 0.0,
    }
    now = time.time()
    cutoff = now - SIGNAL_MAX_AGE_HOURS * 3600
    errors: list = info["errors"]
    msgs: list = []
    got = False

    for tmpl in SIGNAL_HTTP_MIRRORS:
        base = tmpl.format(ch=SIGNAL_CHANNEL)
        host = base.split("/")[2] if "//" in base else base
        info.update(messages=0, expired=0, newest_ts=0.0)
        seen: set = set()
        before = None
        reached = False
        temp_msgs = []
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
                temp_msgs.append(m)
            if oldest is not None and oldest < cutoff:
                break
            before = min(m["id"] for m in page)
            await asyncio.sleep(0.3)
        if reached and temp_msgs:
            msgs = temp_msgs
            got = True
            info["source"] = f"HTTP ({host})"
            break

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


async def fetch_pairs(client: httpx.AsyncClient, addresses: list, pair_hints: Any = ()) -> list:
    chunks = [addresses[i:i + 30] for i in range(0, len(addresses), 30)]
    responses = await asyncio.gather(
        *(fetch_json(client, f"{DEX}/tokens/v1/solana/{','.join(c)}") for c in chunks)
    )
    best: dict = {}
    _merge_pairs(best, list(responses))

    known_pairs = {p.get("pairAddress") for p in best.values()}
    hints = [h for h in dict.fromkeys(pair_hints) if h not in best and h not in known_pairs]
    if hints:
        hchunks = [hints[i:i + 30] for i in range(0, len(hints), 30)]
        hresp = await asyncio.gather(
            *(fetch_json(client, f"{DEX}/latest/dex/pairs/solana/{','.join(c)}") for c in hchunks)
        )
        _merge_pairs(best, list(hresp))
    return list(best.values())


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
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
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
    data = await fetch_json(client, f"{RUGCHECK}/tokens/{mint}/report", retries=1)
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
    status: str
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
    else:
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
    ranked: list = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)
    sources: dict = field(default_factory=dict)
    signals: int = 0
    discovered: int = 0
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

    await say(f"📡 (1/4) قراءة آخر إشارات قناة @{SIGNAL_CHANNEL}...")
    signals, res.sources = await discover_signals(client)
    res.signals = len(signals)
    if not signals:
        res.finished_at = time.time()
        return res
    sig_map = {s.mint: s for s in signals}
    addrs = [s.mint for s in signals]
    hints = [s.mint for s in signals if s.is_hint]

    await say(f"📊 (2/4) جلب بيانات {len(addrs)} عقد من القناة...")
    try:
        pairs = await asyncio.wait_for(fetch_pairs(client, addrs, hints), timeout=min(25.0, left()))
    except asyncio.TimeoutError:
        pairs = []
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
            m["signal_ts"], m["signal_post"] = sig.ts, sig.post_id
            matched.add(sig.mint)
        reason = pre_filter(m)
        if reason:
            res.stats[reason] += 1
        else:
            pre.append(m)
    if pairs and len(signals) > len(matched):
        res.stats["عقد بلا زوج على DexScreener"] += len(signals) - len(matched)
    res.after_prefilter = len(pre)
    pre.sort(key=prelim_rank, reverse=True)
    pre = pre[:MAX_SECURITY_CHECKS]

    await say(f"🛡️ (3/4) فحص أمان {len(pre)} عملة عبر RugCheck...")
    sem = asyncio.Semaphore(4)

    async def check(m: dict):
        async with sem:
            report = await get_rugcheck_report(client, m["mint"])
        return m, (analyze_security(report, m["pair"]) if report else None)

    tasks = [asyncio.create_task(check(m)) for m in pre]
    secure: list = []
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=max(5.0, min(45.0, left() - 25)))
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            res.stats["انتهت مهلة فحص RugCheck"] += len(pending)
        for t in done:
            if t.cancelled() or t.exception() is not None:
                res.stats["خطأ أثناء فحص RugCheck"] += 1
                continue
            m, sec = t.result()
            if sec is None:
                res.stats["تعذر جلب تقرير RugCheck"] += 1
            elif not sec.passed:
                for r in sec.reasons:
                    res.stats[r] += 1
            else:
                secure.append((m, sec))
    res.after_security = len(secure)

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
        if candles:
            plan, reject = compute_scalp_plan(candles, m["price"])
            if plan is not None and plan.status != "WAIT":
                plans.append(plan)
            elif reject:
                res.stats[reject] += 1
        if momentum_ok(m):
            plans.append(compute_alt_plan("MOMENTUM", m, candles))
        if hold_ok(m):
            plans.append(compute_alt_plan("HOLD", m, candles))
        if not plans:
            res.stats["لا تطابق أي نمط"] += 1
            continue
        for plan in plans:
            score, breakdown = compute_score(m, sec, plan)
            ranked.append(Candidate(m, sec, plan, score, breakdown))

    ranked.sort(key=lambda c: c.score, reverse=True)
    res.ranked = ranked
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
            "🚨 <b>تحذيرات:</b> " + " • ".join(html.escape(w) for w in s.warnings) + "\n\n"
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
        f"• السيولة: {fmt_usd(m['liq'])} | MC: {fmt_usd(m['mcap'])}\n\n"
    )

    if p.kind == "SCALP":
        status = (
            "✅ السعر داخل المنطقة الذهبية الآن"
            if p.status == "IN_ZONE"
            else f"⏳ السعر أعلى المنطقة بـ {p.above_pct:.1f}%"
        )
        plan_txt = (
            f"📈 <b>الخطة</b>\n{status}\n"
            f"🟢 Golden Zone (0.5–0.618):\n"
            f"   <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
            f"🎯 TP1 (+{tp1_pct:.0f}%): <code>{fmt_price(p.tp1)}</code>\n"
            f"🎯 TP2 (+{tp2_pct:.0f}%): <code>{fmt_price(p.tp2)}</code>\n"
            f"🛑 SL: <code>{fmt_price(p.sl)}</code>\n\n"
        )
    else:
        plan_txt = (
            f"📈 <b>الخطة</b>\n"
            f"🟢 منطقة الدخول: <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
            f"🎯 TP1: <code>{fmt_price(p.tp1)}</code> | 🎯 TP2: <code>{fmt_price(p.tp2)}</code>\n"
            f"🛑 SL: <code>{fmt_price(p.sl)}</code>\n\n"
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

    return head + warn_block + ident + safety + plan_txt + links + alt_txt


def format_no_signals(res: ScanResult) -> str:
    info = res.sources if isinstance(res.sources, dict) else {}
    lines = [f"📡 <b>لم أجد عقوداً صالحة في قناة @{html.escape(SIGNAL_CHANNEL)}</b>", ""]
    if info.get("error"):
        lines.append(f"⚠️ {html.escape(str(info['error']))}")
    else:
        lines.append(f"• رسائل مقروءة: {info.get('messages', 0)}")
    return "\n".join(lines)


def format_no_result(res: ScanResult) -> str:
    return f"🚫 <b>لا توجد صفقة تجتاز الفلتر حالياً من قناة @{html.escape(SIGNAL_CHANNEL)}</b>"


def to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ───────────────────────────── أوامر تلغرام ─────────────────────────────

WELCOME = "🎯 <b>SolAlpha Sniper v3.4</b>\n\nجاهز لقراءة إشارات قناة @solhousesignal وتحليلها.\n• /best\n• /diag\n• /help"
HELP = "أداة تحليل صفقات Solana."


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
    try:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await original_msg.reply_text(to_plain(text)[:4000], disable_web_page_preview=True)


async def best_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data

    wait = _cooldown_wait(bd, update.effective_user.id)
    if wait:
        await msg.reply_text(f"⏱️ انتظر {wait} ثانية.")
        return

    status_msg = await msg.reply_text("🔎 بدأ المسح...")
    async def progress(text: str) -> None:
        await status_msg.edit_text(text)

    try:
        async with bd["scan_lock"]:
            res = await run_scan(bd["http"], progress, SCAN_BUDGET_SEC)
        if res.signals == 0:
            text = format_no_signals(res)
        elif res.discovered == 0:
            text = "⚠️ وجدت عقوداً لكن لم يتم استرجاع بياناتها من DexScreener."
        elif res.ranked:
            text = format_best(res.ranked[0], res.ranked[1:3])
        else:
            text = format_no_result(res)
    except Exception:
        log.exception("Scan failed")
        text = "❌ حدث خطأ أثناء المسح."

    await safe_edit(status_msg, msg, text)


async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data
    status_msg = await msg.reply_text("🧪 جارٍ التشخيص...")
    try:
        async with bd["scan_lock"]:
            _, info = await discover_signals(bd["http"])
        text = f"🧪 <b>التشخيص</b>\n• الحالة: {'✅ نجحت' if info['ok'] else '❌ فشلت'}\n• المصدر: {info.get('source')}\n• الخطأ: {info.get('error')}"
    except Exception:
        text = "❌ فشل التشخيص."
    await safe_edit(status_msg, msg, text)


async def post_init(application: Application) -> None:
    application.bot_data["http"] = httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": "SolAlphaSniper/3.4", "Accept": "application/json"},
        follow_redirects=True,
    )
    application.bot_data["scan_lock"] = asyncio.Lock()
    application.bot_data["cooldowns"] = {}
    application.bot_data["last_scan"] = None


async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("http")
    if client:
        await client.aclose()


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
    return runner


async def run_all() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("best", best_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))

    runner = await start_web_server()
    await app.initialize()
    await post_init(app)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()
    await app.updater.stop()
    await app.stop()
    await post_shutdown(app)
    await app.shutdown()
    await runner.cleanup()


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN غير مضبوط.")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
