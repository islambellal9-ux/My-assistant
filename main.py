#!/usr/bin/env python3
"""
Bot Telegram - تحليل عملات سولانا العميق (Deep On-Chain & Scalping Analysis)

يفحص أي عنوان عقد (Mint Address) على سولانا عبر:
  - DexScreener  (السعر، السيولة، الحجم، البيع/الشراء)
  - RugCheck     (أمان العقد، توزيع الحاملين، حرق/قفل السيولة)
  - Pump.fun     (مصدر العملة، محفظة المطوّر، حالة الهجرة لـ Raydium)

ثم يحسب تقييماً من 100 ويرسل تقرير عربي منسّق على تيليغرام.

Variables d'environnement requises :
  TELEGRAM_BOT_TOKEN   - توكن البوت من BotFather
  PORT                 - منفذ Health Check (افتراضي 10000، يضبطه Render تلقائياً)

Déploiement Render : Web Service, Start Command -> python bot.py
"""

import asyncio
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------------- #
#  الإعدادات العامة
# ------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("solana-scan-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
RUGCHECK_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
RUGCHECK_FULL_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
PUMPFUN_URL = "https://frontend-api.pump.fun/coins/{mint}"

NA = "غير متوفر"


# ------------------------------------------------------------------------- #
#  خادم Health Check (مطلوب لـ Render باش يبقى البوت شغال H24)
# ------------------------------------------------------------------------- #

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - اسم الدالة مفروض من http.server
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Solana Scan Bot is alive")

    def log_message(self, fmt, *args):  # تعطيل سجلات HTTP المزعجة
        return


def run_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health check server listening on port %s", port)
    server.serve_forever()


# ------------------------------------------------------------------------- #
#  أدوات مساعدة عامة
# ------------------------------------------------------------------------- #

_MISSING = object()


def safe_get(d: Any, *keys, default=None):
    """يمشي داخل dict/list متداخلة بدون ما يطيح الكود إذا مفتاح ناقص.

    يفرّق بين "المفتاح غير موجود" (يرجّع default) و"المفتاح موجود وقيمته null"
    (يرجّع None فعلاً) - مهم جداً لأن RugCheck يستعمل null بمعنى "الصلاحية ملغاة = آمن".
    """
    cur = d
    for k in keys:
        if cur is None:
            return default
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def fmt_usd(n: Optional[float]) -> str:
    if n is None:
        return NA
    try:
        n = float(n)
    except (TypeError, ValueError):
        return NA
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:,.2f}"


def fmt_price(n: Optional[float]) -> str:
    if n is None:
        return NA
    try:
        n = float(n)
    except (TypeError, ValueError):
        return NA
    if n == 0:
        return "$0"
    if n >= 1:
        return f"${n:,.4f}"
    # عدد كافٍ من الخانات العشرية للأسعار الصغيرة جداً (شائع في عملات الميم)
    decimals = max(4, -int(f"{n:.1e}".split("e")[1]) + 3)
    return f"${n:.{min(decimals, 14)}f}"


def fmt_pct(n: Optional[float], signed: bool = False) -> str:
    if n is None:
        return NA
    try:
        n = float(n)
    except (TypeError, ValueError):
        return NA
    sign = "+" if (signed and n >= 0) else ""
    return f"{sign}{n:.1f}%"


def age_from_ms(ms: Optional[int]) -> str:
    if not ms:
        return NA
    try:
        seconds = max(0, time.time() - (ms / 1000))
    except (TypeError, ValueError):
        return NA
    if seconds < 3600:
        return f"{int(seconds // 60)} دقيقة"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ساعة"
    return f"{int(seconds // 86400)} يوم"


# ------------------------------------------------------------------------- #
#  طلبات الـ APIs الخارجية (كل واحدة معزولة، فشل وحدة ما يوقفش الباقي)
# ------------------------------------------------------------------------- #

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("GET %s -> HTTP %s", url, resp.status)
                return None
            return await resp.json(content_type=None)
    except asyncio.TimeoutError:
        log.warning("Timeout fetching %s", url)
        return None
    except (aiohttp.ClientError, ValueError) as exc:
        log.warning("Error fetching %s: %s", url, exc)
        return None


async def fetch_dexscreener(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    data = await fetch_json(session, DEXSCREENER_URL.format(mint=mint))
    pairs = safe_get(data, "pairs", default=None)
    if not pairs:
        return None
    # نختار البركة (pool) ذات أعلى سيولة، هي الأكثر تمثيلاً للسعر الحقيقي
    best = max(pairs, key=lambda p: safe_get(p, "liquidity", "usd", default=0) or 0)
    return best


async def fetch_rugcheck(session: aiohttp.ClientSession, mint: str) -> dict:
    """يحاول يجيب التقرير الكامل، وإذا فشل يرجع للملخص. يرجّع dict فاضي عند الفشل الكامل."""
    full = await fetch_json(session, RUGCHECK_FULL_URL.format(mint=mint))
    if full:
        return full
    summary = await fetch_json(session, RUGCHECK_SUMMARY_URL.format(mint=mint))
    return summary or {}


async def fetch_pumpfun(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    return await fetch_json(session, PUMPFUN_URL.format(mint=mint))


# ------------------------------------------------------------------------- #
#  التحليل: استخراج كل محور من البيانات الخام
# ------------------------------------------------------------------------- #

def analyze_origin(dex: Optional[dict], pump: Optional[dict]) -> dict:
    source = NA
    if dex:
        dex_id = safe_get(dex, "dexId", default="")
        if dex_id:
            source = {
                "raydium": "Raydium",
                "pumpfun": "Pump.fun",
                "pumpswap": "Pump.fun (PumpSwap)",
                "orca": "Orca",
                "meteora": "Meteora",
            }.get(str(dex_id).lower(), str(dex_id).title())
    if pump is not None and source in (NA, ""):
        source = "Pump.fun" if not safe_get(pump, "complete", default=True) else "Pump.fun → Raydium"
    migrated = None
    if pump is not None:
        migrated = bool(safe_get(pump, "complete", default=False))
    return {"source": source, "migrated": migrated}


def analyze_dev(rug: dict, pump: Optional[dict]) -> dict:
    creator = safe_get(rug, "creator", default=None) or safe_get(rug, "creatorAddress", default=None)
    if not creator and pump is not None:
        creator = safe_get(pump, "creator", default=None)

    top_holders = safe_get(rug, "topHolders", default=None) or safe_get(rug, "holders", default=None) or []
    dev_pct = None
    dev_sold = None
    if creator and isinstance(top_holders, list):
        match = next(
            (h for h in top_holders if str(safe_get(h, "address", default="")).lower() == str(creator).lower()),
            None,
        )
        if match:
            dev_pct = safe_get(match, "pct", default=None)
            dev_sold = False
        else:
            # المطوّر ظهر كـ creator لكن ما عادش ضمن كبار الحاملين = غالباً باع
            dev_sold = True
            dev_pct = 0.0

    # تاريخ المطوّر (عملات سابقة) - RugCheck أحياناً يعطيه ضمن "risks" أو "creatorTokens"
    prior_rugs = None
    risks = safe_get(rug, "risks", default=[]) or []
    for r in risks:
        name = str(safe_get(r, "name", default="")).lower()
        if "creator" in name or "previous" in name or "history" in name:
            prior_rugs = safe_get(r, "description", default=NA)
            break

    return {
        "address": creator or NA,
        "dev_pct": dev_pct,
        "dev_sold": dev_sold,
        "history_note": prior_rugs,
    }


def analyze_holders(rug: dict) -> dict:
    top_holders = safe_get(rug, "topHolders", default=None) or safe_get(rug, "holders", default=None) or []
    top10_pct = None
    if isinstance(top_holders, list) and top_holders:
        try:
            top10_pct = sum(float(safe_get(h, "pct", default=0) or 0) for h in top_holders[:10])
        except (TypeError, ValueError):
            top10_pct = None

    snipers_note = NA
    bundled = None
    risks = safe_get(rug, "risks", default=[]) or []
    for r in risks:
        name = str(safe_get(r, "name", default="")).lower()
        if "snip" in name or "bundle" in name:
            bundled = True
            snipers_note = safe_get(r, "description", default="تم رصد قناصة/حزمة شراء بالبلوك الأول")
            break
    if bundled is None:
        bundled = False
        snipers_note = "لم يُرصد نمط قنص واضح في البيانات المتاحة"

    return {"top10_pct": top10_pct, "bundled": bundled, "snipers_note": snipers_note}


def analyze_security(rug: dict) -> dict:
    token = safe_get(rug, "token", default={}) or {}
    mint_auth = safe_get(token, "mintAuthority", default=_MISSING)
    if mint_auth is _MISSING:
        mint_auth = safe_get(rug, "mintAuthority", default=_MISSING)
    freeze_auth = safe_get(token, "freezeAuthority", default=_MISSING)
    if freeze_auth is _MISSING:
        freeze_auth = safe_get(rug, "freezeAuthority", default=_MISSING)

    mint_disabled = None if mint_auth is _MISSING else (mint_auth in (None, "", "null"))
    freeze_disabled = None if freeze_auth is _MISSING else (freeze_auth in (None, "", "null"))

    markets = safe_get(rug, "markets", default=[]) or []
    lp_locked_pct = None
    if markets:
        vals = [
            safe_get(m, "lp", "lpLockedPct", default=None)
            for m in markets
            if safe_get(m, "lp", "lpLockedPct", default=None) is not None
        ]
        if vals:
            try:
                lp_locked_pct = sum(float(v) for v in vals) / len(vals)
            except (TypeError, ValueError):
                lp_locked_pct = None

    score_raw = safe_get(rug, "score_normalised", default=None)
    if score_raw is None:
        score_raw = safe_get(rug, "score", default=None)

    return {
        "mint_disabled": mint_disabled,
        "freeze_disabled": freeze_disabled,
        "lp_locked_pct": lp_locked_pct,
        "rugcheck_score": score_raw,
    }


def analyze_momentum(dex: Optional[dict]) -> dict:
    if not dex:
        return {
            "vol_5m": None, "vol_1h": None, "liq_usd": None, "mcap": None,
            "buys_5m": None, "sells_5m": None, "change_5m": None, "change_1h": None,
            "price": None, "pair_age_ms": None,
        }
    return {
        "vol_5m": safe_get(dex, "volume", "m5", default=None),
        "vol_1h": safe_get(dex, "volume", "h1", default=None),
        "liq_usd": safe_get(dex, "liquidity", "usd", default=None),
        "mcap": safe_get(dex, "marketCap", default=None) or safe_get(dex, "fdv", default=None),
        "buys_5m": safe_get(dex, "txns", "m5", "buys", default=None),
        "sells_5m": safe_get(dex, "txns", "m5", "sells", default=None),
        "change_5m": safe_get(dex, "priceChange", "m5", default=None),
        "change_1h": safe_get(dex, "priceChange", "h1", default=None),
        "price": safe_get(dex, "priceUsd", default=None),
        "pair_age_ms": safe_get(dex, "pairCreatedAt", default=None),
    }


# ------------------------------------------------------------------------- #
#  الخوارزمية: تقييم من 100
# ------------------------------------------------------------------------- #

def compute_score(security: dict, holders: dict, momentum: dict, dev: dict) -> dict:
    score = 0.0
    reasons = []

    # 1) الأمان: 40 نقطة
    sec_pts = 0.0
    if security["mint_disabled"] is True:
        sec_pts += 12
    elif security["mint_disabled"] is False:
        reasons.append("صلاحية السك مفعّلة (خطر تضخيم العرض)")
    if security["freeze_disabled"] is True:
        sec_pts += 12
    elif security["freeze_disabled"] is False:
        reasons.append("صلاحية التجميد مفعّلة (خطر منع البيع)")
    lp = security["lp_locked_pct"]
    if lp is not None:
        sec_pts += min(16, (lp / 100) * 16)
        if lp < 50:
            reasons.append("نسبة قفل/حرق السيولة منخفضة")
    score += sec_pts

    # 2) توزيع الحاملين: 25 نقطة
    dist_pts = 0.0
    t10 = holders["top10_pct"]
    if t10 is not None:
        if t10 <= 15:
            dist_pts += 18
        elif t10 <= 25:
            dist_pts += 11
        elif t10 <= 40:
            dist_pts += 5
            reasons.append("تركيز عالٍ نسبياً في أكبر 10 محافظ")
        else:
            reasons.append("تجميع خطير في أكبر 10 محافظ (أكثر من 40%)")
    if holders["bundled"] is False:
        dist_pts += 7
    elif holders["bundled"] is True:
        reasons.append("رُصد نمط قنص/حزمة شراء بالبلوك الأول")
    if dev.get("dev_sold") is True:
        dist_pts += 0  # المطور باع: لا عقوبة إضافية هنا (أحياناً إيجابي)، لكن ينذكر في التقرير
    elif dev.get("dev_sold") is False and (dev.get("dev_pct") or 0) > 15:
        reasons.append("المطوّر لا يزال يملك نسبة كبيرة من العرض")
    score += dist_pts

    # 3) الحجم ونسبة الشراء/البيع: 20 نقطة
    vol_pts = 0.0
    liq = momentum["liq_usd"] or 0
    vol5 = momentum["vol_5m"]
    buys, sells = momentum["buys_5m"], momentum["sells_5m"]
    if liq and vol5 is not None:
        ratio = vol5 / liq if liq else 0
        if 0.05 <= ratio <= 3:
            vol_pts += 10
        elif ratio > 3:
            reasons.append("حجم التداول ضخم جداً مقابل السيولة (قد يكون مصطنعاً)")
    if buys is not None and sells is not None:
        total = buys + sells
        if total >= 20:
            bratio = buys / max(sells, 1)
            if bratio >= 1.2:
                vol_pts += 10
            elif bratio >= 0.8:
                vol_pts += 5
            else:
                reasons.append("البيع يفوق الشراء في آخر 5 دقائق")
        else:
            reasons.append("عدد صفقات قليل جداً آخر 5 دقائق")
    score += vol_pts

    # 4) كفاية السيولة: 15 نقطة
    liq_pts = 0.0
    if liq:
        if liq >= 50_000:
            liq_pts += 15
        elif liq >= 15_000:
            liq_pts += 9
        elif liq >= 5_000:
            liq_pts += 4
            reasons.append("سيولة منخفضة، انزلاق سعري متوقع")
        else:
            reasons.append("سيولة ضعيفة جداً، خطر مرتفع")
    score += liq_pts

    total = round(min(100, max(0, score)))
    if total >= 75:
        verdict = "🚀 فرصة سكالپينغ ممتازة"
    elif total >= 45:
        verdict = "⚠️ منطقة مخاطرة"
    else:
        verdict = "🔴 خطر - لا تشتري!"

    main_reason = reasons[0] if reasons else "لا توجد ملاحظات حرجة من الفحوصات المتاحة"
    return {"total": total, "verdict": verdict, "main_reason": main_reason, "all_reasons": reasons}


# ------------------------------------------------------------------------- #
#  بناء التقرير النهائي (عربي)
# ------------------------------------------------------------------------- #

def green_red(cond: Optional[bool], yes_label: str, no_label: str) -> str:
    if cond is None:
        return f"❓ {NA}"
    return f"🟢 {yes_label}" if cond else f"🔴 {no_label}"


def build_report(mint: str, name: str, symbol: str, origin: dict, dev: dict,
                  holders: dict, security: dict, momentum: dict, rating: dict) -> str:

    dev_line = green_red(dev.get("dev_sold"), "باع كل حصته", f"ما زال يملك {dev.get('dev_pct', 0) or 0:.1f}%")
    top10_line = NA
    if holders["top10_pct"] is not None:
        tag = "🟢 موزعة جيداً" if holders["top10_pct"] <= 25 else "🔴 تجميع خطير"
        top10_line = f"{holders['top10_pct']:.1f}% من الإمداد [{tag}]"
    snipers_line = "تم التخلص منهم ✅" if holders["bundled"] is False else ("ما زالوا يتحكمون ⚠️" if holders["bundled"] else NA)

    mint_line = green_red(security["mint_disabled"], "ملغاة", "مفعلة - خطر")
    freeze_line = green_red(security["freeze_disabled"], "ملغاة", "مفعلة - خطر")
    lp_line = f"{security['lp_locked_pct']:.0f}%" if security["lp_locked_pct"] is not None else NA
    rc_score_line = f"{security['rugcheck_score']}" if security["rugcheck_score"] is not None else NA

    bs_ratio = NA
    if momentum["buys_5m"] is not None and momentum["sells_5m"] is not None:
        bratio = momentum["buys_5m"] / max(momentum["sells_5m"], 1)
        bs_ratio = f"{momentum['buys_5m']} شراء / {momentum['sells_5m']} بيع (النسبة: {bratio:.1f})"

    report = f"""
🪙 *{name}* (${symbol})
📍 *المصدر:* {origin['source']}
💰 *السعر الحالي:* {fmt_price(momentum['price'])} | *القيمة السوقية:* {fmt_usd(momentum['mcap'])} | *السيولة:* {fmt_usd(momentum['liq_usd'])}
⏱ *عمر العملة:* {age_from_ms(momentum['pair_age_ms'])}

👑 *تحليل المطور والحيتان (Dev & Holders):*
• *المطور (Dev):* {dev_line}
• *أكبر 10 كبار الملاك (Top 10):* {top10_line}
• *القناصة (Snipers/Bundles):* {snipers_line}

🛡 *الأمان والسيولة (RugCheck):*
• *خاصية السك (Mint):* {mint_line}
• *خاصية التجميد (Freeze):* {freeze_line}
• *حرق/قفل السيولة (LP):* {lp_line}
• *تقييم RugCheck الخام:* {rc_score_line}

📈 *الحركة وحجم التداول:*
• *حجم التداول (5 د):* {fmt_usd(momentum['vol_5m'])} | *(1 س):* {fmt_usd(momentum['vol_1h'])}
• *العمليات (5 د):* {bs_ratio}
• *تغيّر السعر:* 5د {fmt_pct(momentum['change_5m'], signed=True)} | 1س {fmt_pct(momentum['change_1h'], signed=True)}

🎯 *التقييم النهائي: {rating['total']}/100*
• *التوصية:* {rating['verdict']}
• *السبب الرئيسي:* {rating['main_reason']}

`{mint}`
""".strip()
    return report


# ------------------------------------------------------------------------- #
#  منطق التحليل الكامل لعملة واحدة
# ------------------------------------------------------------------------- #

async def analyze_mint(mint: str) -> str:
    async with aiohttp.ClientSession() as session:
        dex, rug, pump = await asyncio.gather(
            fetch_dexscreener(session, mint),
            fetch_rugcheck(session, mint),
            fetch_pumpfun(session, mint),
        )

    if not dex and not rug and not pump:
        return (
            "⚠️ ما قدرتش نلقى أي بيانات لهذا العقد.\n"
            "تأكد من صحة العنوان، أو العملة جد جديدة وما زالت البيانات ما توصلتش للمصادر."
        )

    name = (
        safe_get(dex, "baseToken", "name", default=None)
        or safe_get(pump, "name", default=None)
        or "عملة غير معروفة"
    )
    symbol = (
        safe_get(dex, "baseToken", "symbol", default=None)
        or safe_get(pump, "symbol", default=None)
        or "???"
    )

    origin = analyze_origin(dex, pump)
    dev = analyze_dev(rug, pump)
    holders = analyze_holders(rug)
    security = analyze_security(rug)
    momentum = analyze_momentum(dex)
    rating = compute_score(security, holders, momentum, dev)

    return build_report(mint, name, symbol, origin, dev, holders, security, momentum, rating)


# ------------------------------------------------------------------------- #
#  معالجات تيليغرام
# ------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 أهلاً بك في بوت التحليل العميق لعملات سولانا.\n\n"
        "ابعثلي عنوان العقد (Mint Address) لأي عملة على Pump.fun أو Raydium، "
        "ونرجّعلك تقرير تحليل كامل: الأمان، المطوّر، الحاملين، والحجم.\n\n"
        "مثال:\n`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_mint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mint = (update.message.text or "").strip()
    if not SOLANA_MINT_RE.match(mint):
        await update.message.reply_text(
            "❌ هذا ما يشبه عنوان عقد سولانا صحيح. تأكد وأعد المحاولة."
        )
        return

    status_msg = await update.message.reply_text("🔎 جاري التحليل العميق، لحظات من فضلك...")
    try:
        report = await analyze_mint(mint)
    except Exception:  # pylint: disable=broad-except
        log.exception("Unhandled error analyzing mint %s", mint)
        report = "❌ صار خطأ غير متوقع أثناء التحليل. حاول مرة أخرى بعد شوية."

    try:
        await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception:  # fallback إذا فشل تنسيق Markdown (أحرف خاصة في الاسم مثلاً)
        await status_msg.edit_text(report)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ابعثلي عنوان عقد سولانا صحيح (Mint Address) باش نحلله ليك. اكتب /start للمساعدة."
    )


# ------------------------------------------------------------------------- #
#  نقطة الانطلاق
# ------------------------------------------------------------------------- #

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("خطأ: متغيّر البيئة TELEGRAM_BOT_TOKEN غير موجود.")

    # خادم Health Check في Thread منفصل حتى يبقى البوت "حي" في نظر Render
    threading.Thread(target=run_health_server, args=(PORT,), daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.Regex(SOLANA_MINT_RE), handle_mint))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    log.info("Bot started. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
