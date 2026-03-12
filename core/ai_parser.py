"""
core/ai_parser.py
=================
Unified AI interface for parsing trader Telegram messages.

Provider priority:
  1. Configured provider (claude / openai / deepseek)
  2. Auto-fallback to Ollama phi3 if configured provider fails or is unavailable

Price extraction is always done with a thin regex layer on top of AI intent
classification — AI tells us WHAT the message means, regex pulls exact numbers.

Parsed result:
{
  "type":        "entry|pre_announcement|scouting|breakeven|tp_hit|
                  close|close_all|sl_correction|unknown",
  "direction":   "buy|sell|null",
  "symbol":      "XAUUSD",
  "entry_price": float|null,
  "stop_loss":   float|null,
  "take_profits": [float, ...],
  "tp_number":   int|null,       # for tp_hit
  "new_sl":      float|null,     # for sl_correction
  "confidence":  0.0-1.0,
  "is_reply":    bool,
  "raw_text":    str,
}
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Price extractor ────────────────────────────────────────────────────────────

def _extract_prices(text: str) -> list[float]:
    """Pull all valid XAUUSD-range prices (1000-9999) from text."""
    return [float(m) for m in re.findall(r'\b\d{4,5}(?:\.\d{1,2})?\b', text)
            if 1000 <= float(m) <= 99999]

def _extract_sl(text: str) -> Optional[float]:
    m = re.search(r'(?:sl|stop[\s_-]*loss)[:\s]+(\d{4,5}(?:\.\d{1,2})?)', text, re.IGNORECASE)
    return float(m.group(1)) if m else None

def _extract_tps(text: str) -> list[float]:
    tps = []
    for m in re.finditer(r'(?:tp\d*|take[\s_-]*profit\d*)[:\s]+(\d{4,5}(?:\.\d{1,2})?)',
                         text, re.IGNORECASE):
        tps.append(float(m.group(1)))
    # Also grab lines starting with ✅
    for m in re.finditer(r'✅[^\d]*(\d{4,5}(?:\.\d{1,2})?)', text):
        v = float(m.group(1))
        if v not in tps:
            tps.append(v)
    return tps

def _extract_entry(text: str) -> Optional[float]:
    m = re.search(r'(?:entry|buy|sell)\s+(?:at\s+)?(\d{4,5}(?:\.\d{1,2})?)', text, re.IGNORECASE)
    return float(m.group(1)) if m else None


# ── ParsedSignal ──────────────────────────────────────────────────────────────

@dataclass
class ParsedSignal:
    signal_type:   str            = "unknown"
    raw_text:      str            = ""
    symbol:        str            = "XAUUSD"
    direction:     Optional[str]  = None
    entry_price:   Optional[float] = None
    entry_type:    Optional[str]  = None
    stop_loss:     Optional[float] = None
    take_profits:  list           = field(default_factory=list)
    tp_number:     Optional[int]  = None
    new_sl:        Optional[float] = None
    confidence:    float          = 1.0
    is_reply:      bool           = False
    reply_to_id:   Optional[int]  = None
    warnings:      list           = field(default_factory=list)


# ── AI Provider ───────────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """You are an expert forex/gold trading signal analyst.
Classify this Telegram message from a gold/forex trading channel.

Message: "{text}"

Classify into EXACTLY one type:
- entry: full trade signal (has direction + at least SL or TP)
- pre_announcement: "buy now", "sell gold", "sell again" — immediate action, no levels yet
- scouting: "looking for buys", "watching sells", "am looking sells" — observation only, DO NOT trade
- breakeven: move stop loss to entry price
- tp_hit: a take profit level was hit
- close: close the most recent/specific position
- close_all: close all open positions
- sl_correction: standalone new stop loss value for existing trade
- unknown: chatter, general info, not actionable

For direction: buy, sell, or null
For symbol: extract if mentioned, default XAUUSD

Reply ONLY with valid JSON, no explanation:
{{"type": "...", "direction": "buy|sell|null", "symbol": "XAUUSD", "confidence": 0.95}}"""


class AIParser:
    """
    Unified AI parser. Tries configured provider first, falls back to Ollama.
    """

    def __init__(self, provider: str, api_key: str, model: str,
                 ollama_host: str, ollama_model: str):
        self.provider      = provider
        self.api_key       = api_key
        self.model         = model or self._default_model(provider)
        self.ollama_host   = ollama_host
        self.ollama_model  = ollama_model
        self._active       = provider   # tracks which is actually working
        self._client       = httpx.AsyncClient(timeout=20.0)

    def _default_model(self, provider: str) -> str:
        return {
            "claude":   "claude-haiku-4-5",
            "openai":   "gpt-4o-mini",
            "deepseek": "deepseek-chat",
            "ollama":   "phi3",
        }.get(provider, "phi3")

    # ── Startup check ──────────────────────────────────────────────────────────

    async def startup_check(self) -> str:
        """Test configured provider, fall back to Ollama if needed. Returns active provider."""
        if self.provider != "ollama":
            ok = await self._test_provider(self.provider)
            if ok:
                self._active = self.provider
                logger.info(f"[AI] Using provider: {self.provider} ({self.model})")
                return self._active
            logger.warning(f"[AI] {self.provider} unavailable — falling back to Ollama")

        # Try Ollama
        ok = await self._ensure_ollama()
        if ok:
            self._active = "ollama"
            logger.info(f"[AI] Using provider: ollama ({self.ollama_model})")
        else:
            logger.error("[AI] No AI provider available — parser will return unknown for all messages")
            self._active = "none"
        return self._active

    async def _test_provider(self, provider: str) -> bool:
        try:
            result = await self._call_provider(provider, "ping test — reply with: ok")
            return bool(result)
        except Exception as e:
            logger.debug(f"[AI] Provider test failed ({provider}): {e}")
            return False

    async def _ensure_ollama(self) -> bool:
        """Check Ollama is running and model is pulled."""
        try:
            resp = await self._client.get(f"{self.ollama_host}/api/tags", timeout=5.0)
            if resp.status_code != 200:
                logger.warning("[AI] Ollama not running. Start with: ollama serve")
                return False
            models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
            if self.ollama_model not in models:
                logger.info(f"[AI] Pulling {self.ollama_model}... (one-time, may take a few minutes)")
                proc = await asyncio.create_subprocess_exec(
                    "ollama", "pull", self.ollama_model,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                logger.info(f"[AI] {self.ollama_model} ready")
            return True
        except Exception as e:
            logger.warning(f"[AI] Ollama check failed: {e}")
            logger.warning("[AI] Install Ollama from https://ollama.com then run: ollama pull phi3")
            return False

    # ── Main parse ─────────────────────────────────────────────────────────────

    async def parse(self, text: str, is_reply: bool = False,
                    reply_to_id: Optional[int] = None,
                    default_symbol: str = "XAUUSD") -> ParsedSignal:
        sig = ParsedSignal(raw_text=text, is_reply=is_reply, reply_to_id=reply_to_id)

        if self._active == "none":
            return sig

        # Step 1: AI classifies intent
        ai_result = await self._classify(text)
        if not ai_result:
            return sig

        sig.signal_type = ai_result.get("type", "unknown")
        sig.direction   = ai_result.get("direction") or None
        sig.symbol      = ai_result.get("symbol") or default_symbol
        sig.confidence  = float(ai_result.get("confidence", 0.7))

        if sig.direction == "null":
            sig.direction = None

        # Step 2: Extract prices with regex
        sig.stop_loss    = _extract_sl(text)
        sig.take_profits = _extract_tps(text)
        sig.entry_price  = _extract_entry(text)

        # Step 3: Enrich based on type
        if sig.signal_type == "entry":
            if not sig.stop_loss and not sig.take_profits:
                sig.signal_type = "pre_announcement"
            elif sig.entry_price:
                sig.entry_type = "limit"
            else:
                sig.entry_type = "market"

        if sig.signal_type == "tp_hit":
            m = re.search(r'tp\s*(\d+)', text, re.IGNORECASE)
            sig.tp_number = int(m.group(1)) if m else 1

        if sig.signal_type == "sl_correction":
            prices = _extract_prices(text)
            sig.new_sl = prices[0] if prices else None

        # Remove zero TPs (open runners)
        sig.take_profits = [tp for tp in sig.take_profits if tp > 0]

        logger.info(
            f"[PARSER] {sig.signal_type.upper()} {sig.symbol} {sig.direction} "
            f"SL={sig.stop_loss} TPs={sig.take_profits} conf={sig.confidence:.2f}"
        )
        return sig

    async def _classify(self, text: str) -> Optional[dict]:
        prompt = _CLASSIFY_PROMPT.format(text=text[:500])
        try:
            raw = await self._call_provider(self._active, prompt)
            if not raw:
                return None
            # Strip markdown fences if present
            raw = re.sub(r"```(?:json)?|```", "", raw).strip()
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"[AI] JSON parse failed: {raw!r}")
            return None
        except Exception as e:
            logger.error(f"[AI] Classify error: {e}")
            return None

    # ── Provider calls ─────────────────────────────────────────────────────────

    async def _call_provider(self, provider: str, prompt: str) -> Optional[str]:
        try:
            if provider == "claude":
                return await self._call_claude(prompt)
            elif provider == "openai":
                return await self._call_openai(prompt)
            elif provider == "deepseek":
                return await self._call_deepseek(prompt)
            elif provider == "ollama":
                return await self._call_ollama(prompt)
        except Exception as e:
            logger.debug(f"[AI] {provider} call error: {e}")
            # Try fallback to Ollama if primary failed
            if provider != "ollama" and self._active != "ollama":
                logger.warning(f"[AI] {provider} failed mid-session — switching to Ollama")
                self._active = "ollama"
                return await self._call_ollama(prompt)
        return None

    async def _call_claude(self, prompt: str) -> Optional[str]:
        resp = await self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      self.model or "claude-haiku-4-5",
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    async def _call_openai(self, prompt: str) -> Optional[str]:
        resp = await self._client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model":      self.model or "gpt-4o-mini",
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def _call_deepseek(self, prompt: str) -> Optional[str]:
        resp = await self._client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model":      self.model or "deepseek-chat",
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def _call_ollama(self, prompt: str) -> Optional[str]:
        resp = await self._client.post(
            f"{self.ollama_host}/api/generate",
            json={"model": self.ollama_model, "prompt": prompt, "stream": False},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")