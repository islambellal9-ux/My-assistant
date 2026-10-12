# -*- coding: utf-8 -*-
"""
SolAlpha Sniper — Telegram bot (Render Web Service - Phone Optimized)
قراءة إشارات قناة @solhousesignal مباشرة وبأمان.
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SIGNAL_CHANNEL = os.getenv("SIGNAL_CHANNEL", "solhousesignal").strip().lstrip("@")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

MIN_SCORE = _env_float("MIN_SCORE", 80)
RISKY_MIN_SCORE = _env_float("RISKY_MIN_SCORE", 65)
MIN_LIQUIDITY_USD = _env_float("MIN_LIQUIDITY_USD", 15_000)
MIN_VOLUME_H1_USD = _env_float("MIN_VOLUME_H1_USD", 8_000)
MIN_MCAP_USD = _env_float("MIN_MCAP_USD", 30_000)
MAX_MCAP_USD = _env_float("MAX_MCAP_USD", 100_000_000)
MIN_PAIR_AGE_MIN = _env_float("MIN_PAIR_AGE_MIN", 45)

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml",
}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("solalpha")

@dataclass
class Signal:
    mint: str
    post_id: int
    ts: Optional[float]
    is_hint: bool = False

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {ch: i for i, ch in enumerate(B58_ALPHABET)}
SOL_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")

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
    mints = [a for a in SOL_ADDR_RE.findall(text) if is_valid_solana_address(a)]
    return mints, []

async def fetch_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> Any:
    try:
        r = await client.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

async def discover_signals(client: httpx.AsyncClient):
    url = f"https://t.me/s/{SIGNAL_CHANNEL}"
    try:
        r = await client.get(url, headers=WEB_HEADERS)
        if r.status_code != 200:
            return [], {"ok": False, "error": f"HTTP {r.status_code}"}
        
        text_content = r.text
        mints = extract_addresses(text_content)[0]
        unique_mints = list(dict.fromkeys(mints))
        
        signals = [Signal(mint=m, post_id=i, ts=time.time()) for i, m in enumerate(unique_mints)]
        return signals, {"ok": True, "messages": len(signals)}
    except Exception as e:
        return [], {"ok": False, "error": str(e)}

async def fetch_pairs(client: httpx.AsyncClient, addresses: list) -> list:
    if not addresses:
        return []
    chunks = [addresses[i:i + 30] for i in range(0, len(addresses), 30)]
    responses = await asyncio.gather(
        *(fetch_json(client, f"{DEX}/tokens/v1/solana/{','.join(c)}") for c in chunks)
    )
    best = {}
    for data in responses:
        if isinstance(data, list):
            for p in data:
                if isinstance(p, dict) and p.get("chainId") == "solana":
                    base = (p.get("baseToken") or {}).get("address")
                    if base:
                        best[base] = p
    return list(best.values())

async def run_scan(client: httpx.AsyncClient):
    signals, info = await discover_signals(client)
    if not signals:
        return None, info
    
    addrs = [s.mint for s in signals]
    pairs = await fetch_pairs(client, addrs)
    if not pairs:
        return None, info
        
    return pairs[0], info # إرجاع أول عملة صالحة كمثال مباشر وسريع

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 **SolAlpha Sniper (Phone Version)** جاهز.\nاكتب /best لمعرفة الصفقة أو /diag للتشخيص.", parse_mode=ParseMode.HTML)

async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.application.bot_data["http"]
    _, info = await discover_signals(client)
    await update.message.reply_text(f"🧪 **التشخيص من الهاتف:**\n• الحالة: {'✅ نجحت' if info.get('ok') else '❌ فشلت'}\n• الخطأ: {info.get('error', 'لا يوجد')}", parse_mode=ParseMode.HTML)

async def best_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 جارٍ البحث عن أفضل صفقة...")
    client = context.application.bot_data["http"]
    pair, info = await run_scan(client)
    
    if not pair:
        await msg.edit_text("🚫 لم يتم العثور على صفقات مطابقة حالياً.")
        return
        
    base = pair.get("baseToken", {})
    text = (
        f"🟢 **صفقة مقترحة من القناة**\n\n"
        f"🪙 **{base.get('name')}** (${base.get('symbol')})\n"
        f"📋 العقد:\n<code>{base.get('address')}</code>\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair.get('pairAddress')}'>DexScreener</a>"
    )
    await msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def post_init(app: Application):
    app.bot_data["http"] = httpx.AsyncClient(timeout=10.0, follow_redirects=True)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("best", best_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    
    # سيرفر بسيط لـ Render
    async def health(request):
        return web.Response(text="OK")
    
    loop = asyncio.get_event_loop()
    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    
    async def start_server():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()
        
    loop.run_until_complete(start_server())
    app.run_polling()

if __name__ == "__main__":
    main()
