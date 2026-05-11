"""
core/ai_parser.py
=================
Unified AI interface for parsing trader Telegram messages.

Provider priority:
  1. Configured provider (claude / openai / deepseek)
  2. Auto-fallback to Ollama phi3 if primary fails or unavailable

Architecture:
  - Regex pre-classifier handles all obvious patterns first (no AI needed)
  - AI only runs on messages regex cannot classify
  - Confidence guard: AI pre_announcement < 0.90 → unknown (phi3 hallucinates)
  - Unicode NFKC normalisation handles Channel 3's bold math characters (𝐓𝐏→TP)
  - Comma-decimal normalisation handles Channel 1's "5099,8" format

Channel patterns observed and handled:
  Channel 1 (Mo/gtmofx):
    entry market : "Gold buy now 5172 - 5169\nSL: 5165\nTP: 5174..."
    bare         : "Gold buy now" / "Gold sell now"
    sl correct   : "Adjust SL to 5174" / "Move SL to 5096" / "max SL to 5172"
    breakeven    : "Set breakeven for zero risk now" / "SET FULL BREAKEVEN NOW!"
    tp hit       : "TP1✅" / "TP2✅✅" / "TP5 with 150 pips done"
    close        : "Closed trade" / "Closed the trade."

  Channel 2 (Paul/goldhunter):
    entry limit  : "Gold buy zone\nEntry 5105-5100\nSl 5085\nTP 5115..."
    entry limit  : "Gold buy limit\nEntry 5050\nSL 5030\nTP 5060..."
    entry limit  : "Sell limit gold 5190\nSL 5205\nTP 5181..."
    entry market : "Gold buy at 5236.66\nSL 5200\nTP..."
    breakeven    : "Move sl to be"
    sl correct   : "Move SL to 5164"

  Channel 3 (structured zone):
    entry zone   : "XAUUSD: BUYY: ****5138/5133****\nTP. 5142\nTP. 5146\nSL. 5125"
    tp hit       : "𝐆𝐎𝐋𝐃  BUYY\n𝐓𝐏1 𝐇𝐥𝐓 40+ 𝐏𝐥𝐏𝐒 𝐏𝐑𝐎𝐅𝐥𝐓 𝐃𝐎𝐍𝐄"
                   → after NFKC: "GOLD  BUYY\nTP1 HlT 40+ PlPS PROFlT DONE"
"""

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ── Text normalisation ─────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """
    Prepare text for parsing:
    1. NFKC unicode → converts mathematical bold (𝐓𝐏→TP, 𝐇𝐥𝐓→HlT, 𝐃𝐎𝐍𝐄→DONE)
    2. Comma-decimal → 5099,8 becomes 5099.8
    3. Strip markdown formatting characters (*, __, **)
    """
    t = unicodedata.normalize('NFKC', text).strip()
    # Comma decimals: 5099,8 → 5099.8 (only when digit-comma-digit)
    t = re.sub(r'(\d),(\d)', r'\1.\2', t)
    # Strip markdown bold/italic markers
    t = re.sub(r'\*{1,4}|_{1,2}', '', t)
    return t


# ── Price extraction helpers ───────────────────────────────────────────────────

def _prices(text: str) -> list[float]:
    """All 4-5 digit prices in text, excluding values followed by 'pips'."""
    return [
        float(m.group(0))
        for m in re.finditer(r'\b(\d{4,5}(?:\.\d{1,2})?)\b', text)
        if not re.search(r'\bpips\b', text[m.end():m.end() + 8], re.IGNORECASE)
        and 1000.0 <= float(m.group(0)) <= 99999.0
    ]

def _extract_sl(text: str) -> Optional[float]:
    """Extract SL price. Handles SL:, SL., SL (space), Stoploss at."""
    m = re.search(
        r'(?:sl|stop[\s_-]*loss)\s*[:\.\s]\s*(\d{4,5}(?:\.\d{1,2})?)',
        text, re.IGNORECASE
    )
    if m:
        return float(m.group(1))
    # "Stoploss at 5280"
    m = re.search(r'stoploss\s+at\s+(\d{4,5}(?:\.\d{1,2})?)', text, re.IGNORECASE)
    return float(m.group(1)) if m else None

def _extract_tps(text: str) -> list[float]:
    """
    Extract TP prices. Handles:
      TP: 5174  TP. 5142  TP1 5178  take profit 5180
      TP: open  TP: 100 pips  → filtered (no 4-digit number)
    """
    tps = []
    # Named TP lines: "TP:" / "TP." / "TP " / "TP1:" / "target:"
    for m in re.finditer(
        r'(?:tp\d*|take[\s_-]*profit\d*|target\d*)\s*[:\.\s]\s*(\d{4,5}(?:\.\d{1,2})?)',
        text, re.IGNORECASE
    ):
        v = float(m.group(1))
        if 1000 <= v <= 99999 and v not in tps:
            # Skip if followed by 'pips'
            trail = text[m.end():m.end() + 8]
            if not re.search(r'\bpips\b', trail, re.IGNORECASE):
                tps.append(v)
    # ✅ emoji followed by price (channel 1 sometimes uses these)
    for m in re.finditer(r'[✅🥇]\s*(\d{4,5}(?:\.\d{1,2})?)', text):
        v = float(m.group(1))
        if 1000 <= v <= 99999 and v not in tps:
            tps.append(v)
    return tps


def _has_runner_tp(text: str) -> bool:
    """True if the signal includes 'TP open' / 'TP runner' / 'last TP open'."""
    return bool(re.search(
        r'(?:✅\s*)?tp\s*(?:open|runner|run)\b',
        text, re.IGNORECASE
    ))


def _extract_entry(text: str, direction: Optional[str] = None) -> Optional[float]:
    """
    Extract entry price. Handles:
      - "Entry 5050" / "Entry 5105-5100" (zone → pick appropriate bound)
      - "at 5236.66" (exact market price)
      - "BUYY: 5138/5133" (channel 3 zone → pick appropriate bound)
    """
    # Zone format: PRICE/PRICE or PRICE - PRICE (only 4-5 digit numbers)
    zone = re.search(
        r'(\d{4,5}(?:\.\d{1,2})?)\s*[/\-]\s*(\d{4,5}(?:\.\d{1,2})?)',
        text
    )
    if zone:
        p1, p2 = float(zone.group(1)), float(zone.group(2))
        if 1000 <= p1 <= 99999 and 1000 <= p2 <= 99999:
            if direction == "buy":
                return min(p1, p2)   # enter at lower bound for buy
            elif direction == "sell":
                return max(p1, p2)   # enter at upper bound for sell
            else:
                return min(p1, p2)

    # "Entry PRICE" or "buy/sell at PRICE"
    m = re.search(
        r'(?:entry|(?:buy|sell)\s+at)\s+(\d{4,5}(?:\.\d{1,2})?)',
        text, re.IGNORECASE
    )
    return float(m.group(1)) if m else None


# ── Regex pre-classifier ───────────────────────────────────────────────────────

def _regex_classify(text: str) -> Optional[dict]:
    """
    Fast regex classifier. Runs before AI and handles all well-known patterns.
    Returns a classification dict or None (AI will handle it).

    Returns keys: type, direction, entry_type (for entry signals)
    """
    t  = _normalise(text)
    tl = t.lower()

    # ── Direction ─────────────────────────────────────────────────────────────
    # Handle BUYY (channel 3 spelling), BUY, buy and SELL, Sell, sell
    buy_pos  = [m.start() for m in re.finditer(r'\bbuys?y?\b', tl)]
    sell_pos = [m.start() for m in re.finditer(r'\bsells?\b', tl)]

    direction = None
    if buy_pos and not sell_pos:
        direction = "buy"
    elif sell_pos and not buy_pos:
        direction = "sell"
    elif buy_pos and sell_pos:
        direction = "sell" if sell_pos[0] < buy_pos[0] else "buy"

    has_prices = bool(_prices(t))
    has_sl     = bool(re.search(r'\b(sl|stop[\s_-]*loss|stoploss)\b', tl))
    has_tp     = bool(re.search(r'\btp[\d\s\.\:]|\btarget[\d\s]|take[\s_-]*profit', tl))

    # ── TP hit (check BEFORE close to avoid "close all target" confusion) ─────
    # Positive TP hit: ✅ checkmarks, 🥇, "N pips done", channel 3 "HlT...DONE"
    # Note: use explicit codepoint check for emoji in char class
    if re.search(r'tp\d+\s*[\u2705\U0001f947]|[\u2705\U0001f947]\s*tp\d+', t, re.IGNORECASE):
        return {"type": "tp_hit", "direction": direction}
    if re.search(r'\btp\d+\b.{0,30}\b(pips?\s+(done|profit)|hit|h[li]t)\b', tl) and \
       not re.search(r"\bdidn'?t\b|\balmost\b|\bnot\b|\bnever\b", tl):
        return {"type": "tp_hit", "direction": direction}
    if re.search(r'\btp\d+\b.{0,10}done\b', tl) and \
       not re.search(r"\bdidn'?t\b|\balmost\b", tl):
        return {"type": "tp_hit", "direction": direction}
    # Channel 3 after NFKC: "TP1 HlT 40+ PlPS PROFlT DONE"
    if re.search(r'\btp\s*\d+\s+h[li]t\b', tl):
        return {"type": "tp_hit", "direction": direction}
    # "CLOSE ALL TARGET SUCCESSFULLY" = all TPs hit, not a close command
    if re.search(r'close\s+all\s+target|target\s+successfully', tl):
        return {"type": "tp_hit", "direction": direction}
    # "Our 5th TP successfully hit" / "first TP successfully hit" — Marshal pattern
    if re.search(r'\btp\b.{0,20}\bsuccessfully\s+hit\b', tl):
        return {"type": "tp_hit", "direction": direction}
    # "TP5 with 150 pips done too" / "TP4 was 100 pips thats done"
    if re.search(r'\btp\d+\b.{0,20}\bpips\b.{0,10}\bdone\b', tl):
        return {"type": "tp_hit", "direction": direction}

    # ── Scouting ──────────────────────────────────────────────────────────────
    if re.search(
        r'\b(looking|watching|waiting|monitoring|watch)\b.{0,30}\b(buy|sell|long|short)s?\b',
        tl
    ) or re.search(r'\b(buy|sell)s?\b.{0,25}\b(looking|watching|waiting)\b', tl):
        return {"type": "scouting", "direction": direction}

    # ── Breakeven (command form only, not "breakeven hit" announcements) ──────
    if re.search(r'\bset\s+(full\s+)?breakeven\b|'
                 r'\b(sl|stop\s*loss)\s+to\s+(be|entry|breakeven|break\s*even)\b|'
                 r'\bmove\s+(sl|stop\s*loss)\s+to\s+(be|entry|breakeven|break\s*even)\b',
                 tl):
        return {"type": "breakeven", "direction": None}
    # "Breakeven hit" / "hit breakeven" → tp_hit (position closed at BE)
    if re.search(r'breakeven\s+hit|hit\s+breakeven|breakeven\s+now', tl) and \
       not re.search(r'\bset\b|\bmove\b|\bfor\s+(zero|now)\b', tl):
        return {"type": "tp_hit", "direction": direction}

    # ── Close all ─────────────────────────────────────────────────────────────
    if re.search(r'\bclose\s+all\b|\bexit\s+all\b|\bclose\s+everything\b', tl) and \
       not re.search(r'target|successfully|profit\s+done', tl):
        return {"type": "close_all", "direction": None}

    # ── Close (individual) — be conservative ──────────────────────────────────
    is_short = len(t.strip()) < 100
    close_patterns = [
        r'\bclosed?\s+(?:the\s+)?(?:last|prior|previous|that)?\s*(?:trade|position|order|it|this|now)\b',
        r"\bi'?ll?\s+cut\s+loss\s+now\b",
        r'\bcut\s+loss\s+now\b',
        r'\bclosing\s+(?:the\s+)?(?:trade|position|it)?\b',
        r'\bclose\s+with\s+breakeven\b',  # Marshal: "Not good anymore close with breakeven"
        r"don'?t\s+like\s+it.*?close",
        r'\bexit\s+(?:the\s+)?(?:trade|position|it|now)\b',
    ]
    close_match = any(re.search(p, tl) for p in close_patterns)
    if is_short and close_match and \
       not re.search(r'winner|profit\s+done|pips\s+done|target|together|successfully', tl):
        return {"type": "close", "direction": None}

    # ── SL correction (explicit standalone SL move to a price) ──────────────
    sl_correction_patterns = [
        r'\b(adjust|move|set|moving|adjusting|max|update|correct)\s+(?:the\s+)?(stop\s*loss|sl)\b',
        r'\b(stop\s*loss|sl)\s+to\s+\d{4}',
        r'\bmoving\s+(stop\s*loss|sl)\b',
        r'\bstoploss\s+at\b',
        r'\bmove\s+stop\b',
        r'\b(correct|fix|update)\s+(?:the\s+)?stop\s*loss\b',
        # Standalone "🛑 SL <price>" with no entry/TP context = correction
        r'^[\s🛑]*sl\s+\d{4,5}\s*$',
    ]
    sl_correction_match = any(
        re.search(p, tl, re.MULTILINE) for p in sl_correction_patterns
    )
    if has_prices and sl_correction_match and \
       not re.search(r'\btp\b|\btarget\b|\bentry\b|\brisky\s+trade\b', tl):
        return {"type": "sl_correction", "direction": None}

    # ── Entry signal (has direction + prices + SL or TP) ─────────────────────
    if direction and has_prices and (has_sl or has_tp):
        # Determine entry type
        entry_type = _detect_entry_type(t, tl)
        return {"type": "entry", "direction": direction, "entry_type": entry_type}

    # ── Bare pre-announcement (direction only, short, no prices) ─────────────
    bare_keywords = r'\b(now|gold|again|immediately|quick)\b'
    if direction and not has_prices and re.search(bare_keywords, tl):
        return {"type": "pre_announcement", "direction": direction}
    if direction and len(t) <= 30 and not has_prices:
        return {"type": "pre_announcement", "direction": direction}

    return None   # AI will handle this


def _detect_entry_type(text: str, tl: str) -> str:
    """
    Determine if an entry signal is market or limit.

    Market signals:
      - "buy now" / "sell now" (enter immediately at market)
      - "buy at EXACT_PRICE" / "sell at EXACT_PRICE"

    Limit signals:
      - "buy limit" / "sell limit"
      - "buy zone" / "sell zone" / "buy zones"
      - "PRICE/PRICE" or "PRICE - PRICE" zone format (without "now")
      - "Entry RANGE" pattern
    """
    # Explicit "now" → market regardless of zone in message
    if re.search(r'\b(buy|sell)\s+(now|gold\s+now)\b', tl):
        return "market"
    # "buy at PRICE" / "sell at PRICE" exact → market
    if re.search(r'\b(buy|sell)\s+at\s+\d{4}', tl):
        return "market"
    # Explicit limit keywords
    if re.search(r'\b(limit|zone|discount)\b', tl):
        return "limit"
    # Channel 3 zone format PRICE/PRICE without "now"
    if re.search(r'\d{4,5}[/]\d{4,5}', text):
        return "limit"
    # "Entry RANGE" or "Entry PRICE - PRICE"
    if re.search(r'\bentry\b.{0,20}\d{4,5}.{0,5}\d{4,5}', tl):
        return "limit"
    return "market"


# ── Parsed signal dataclass ───────────────────────────────────────────────────

@dataclass
class ParsedSignal:
    signal_type:   str             = "unknown"
    raw_text:      str             = ""
    symbol:        str             = "XAUUSD"
    direction:     Optional[str]   = None
    entry_price:   Optional[float] = None
    entry_type:    Optional[str]   = None     # "market" | "limit"
    stop_loss:     Optional[float] = None
    take_profits:  list            = field(default_factory=list)
    has_runner:    bool            = False    # True if "TP open" / "TP runner" present
    tp_number:     Optional[int]   = None
    new_sl:        Optional[float] = None
    confidence:    float           = 1.0
    is_reply:      bool            = False
    reply_to_id:   Optional[int]   = None
    warnings:      list            = field(default_factory=list)


# ── AI classify prompt ────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """You are an expert forex/gold trading signal analyst.
Classify this Telegram message from a gold/forex trading channel.

Message: "{text}"

Classify into EXACTLY one type:
- entry: full trade signal (has direction + at least SL or TP)
- pre_announcement: "buy now", "sell gold" — immediate action with no levels yet
- scouting: "looking for buys/sells", "watching for sells" — observation only, NO trade
- breakeven: command to move stop loss to entry price
- tp_hit: a take profit level was hit or confirmed
- close: close the most recent position
- close_all: close all open positions
- sl_correction: standalone new stop loss price for existing open trade
- unknown: chatter, general info, testimonials, community messages — not actionable

For direction: buy, sell, or null
For symbol: extract if mentioned, default XAUUSD
For entry_type: market (enter now) or limit (enter at a specific level)

Reply ONLY with valid JSON, no explanation:
{{"type": "...", "direction": "buy|sell|null", "symbol": "XAUUSD", "entry_type": "market|limit", "confidence": 0.95}}"""


# ── AI Parser ─────────────────────────────────────────────────────────────────

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
        self._active       = provider
        self._client       = httpx.AsyncClient(timeout=20.0)

    def _default_model(self, provider: str) -> str:
        return {
            "claude":   "claude-haiku-4-5",
            "openai":   "gpt-4o-mini",
            "deepseek": "deepseek-chat",
            "gemini":   "gemini-1.5-flash",
            "ollama":   "phi3",
        }.get(provider, "phi3")

    # ── Startup ────────────────────────────────────────────────────────────────

    async def startup_check(self) -> str:
        if self.provider != "ollama":
            ok = await self._test_provider(self.provider)
            if ok:
                self._active = self.provider
                logger.info(f"[AI] Using provider: {self.provider} ({self.model})")
                return self._active
            logger.warning(f"[AI] {self.provider} unavailable — falling back to Ollama")

        ok = await self._ensure_ollama()
        if ok:
            self._active = "ollama"
            logger.info(f"[AI] Using provider: ollama ({self.ollama_model})")
        else:
            logger.error("[AI] No AI provider available — parser returns unknown for all")
            self._active = "none"
        return self._active

    async def _test_provider(self, provider: str) -> bool:
        try:
            result = await self._call_provider(provider, "ping — reply with: ok")
            return bool(result)
        except Exception as e:
            logger.debug(f"[AI] Provider test failed ({provider}): {e}")
            return False

    async def _ensure_ollama(self) -> bool:
        try:
            resp = await self._client.get(f"{self.ollama_host}/api/tags", timeout=5.0)
            if resp.status_code != 200:
                logger.warning("[AI] Ollama not running. Start with: ollama serve")
                return False
            models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
            if self.ollama_model not in models:
                logger.info(f"[AI] Pulling {self.ollama_model}… (one-time, may take a few minutes)")
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

        # Step 1: Normalise text once — used by all downstream steps
        normalised = _normalise(text)

        # Step 2: Regex pre-filter — handles all well-known channel patterns
        regex_result = _regex_classify(normalised)
        if regex_result:
            sig.signal_type = regex_result["type"]
            sig.direction   = regex_result.get("direction")
            sig.symbol      = default_symbol
            sig.confidence  = 1.0
            sig.entry_type  = regex_result.get("entry_type", "market")
            logger.info(f"[PARSER] REGEX → {sig.signal_type} {sig.direction} "
                        f"entry_type={sig.entry_type}")

        elif self._active != "none":
            # Step 3: AI classifies what regex couldn't
            ai_result = await self._classify(normalised)
            if not ai_result:
                return sig

            sig.signal_type = ai_result.get("type", "unknown")
            sig.direction   = ai_result.get("direction") or None
            sig.symbol      = ai_result.get("symbol") or default_symbol
            sig.confidence  = float(ai_result.get("confidence", 0.7))
            sig.entry_type  = ai_result.get("entry_type", "market")

            if sig.direction == "null":
                sig.direction = None

            # Guard: do not trust low-confidence pre_announcement from AI
            # (regex handles genuine bare signals; AI hallucinating here is dangerous)
            if sig.signal_type == "pre_announcement" and sig.confidence < 0.90:
                logger.info(
                    f"[PARSER] pre_announcement conf={sig.confidence:.2f} < 0.90 "
                    f"→ downgraded to unknown  ({normalised[:60]!r})"
                )
                sig.signal_type = "unknown"

        if sig.signal_type == "unknown":
            return sig

        # Step 4: Extract prices from normalised text
        sig.stop_loss    = _extract_sl(normalised)
        sig.take_profits = _extract_tps(normalised)
        sig.entry_price  = _extract_entry(normalised, sig.direction)
        sig.has_runner   = _has_runner_tp(normalised)

        # Step 5: Type-specific enrichment
        if sig.signal_type == "entry":
            if not sig.stop_loss and not sig.take_profits and not sig.has_runner:
                # Has direction but no levels → treat as bare trade
                sig.signal_type = "pre_announcement"
            # entry_type already set by classifier

        if sig.signal_type == "tp_hit":
            m = re.search(r'tp\s*(\d+)', normalised, re.IGNORECASE)
            sig.tp_number = int(m.group(1)) if m else None

        if sig.signal_type == "sl_correction":
            # Prefer SL-keyword extraction first
            sig.new_sl = _extract_sl(normalised)
            if not sig.new_sl:
                # Fallback to first 4-5 digit price
                prices = _prices(normalised)
                sig.new_sl = prices[0] if prices else None

        # Step 6: Remove zero / pips TPs (already filtered in _extract_tps,
        # but keep this as final guard)
        sig.take_profits = [tp for tp in sig.take_profits if tp >= 1000]

        logger.info(
            f"[PARSER] {sig.signal_type.upper()} {sig.symbol} {sig.direction} "
            f"SL={sig.stop_loss} TPs={sig.take_profits} "
            f"entry={sig.entry_price} type={sig.entry_type} conf={sig.confidence:.2f}"
        )
        return sig

    async def _classify(self, text: str) -> Optional[dict]:
        prompt = _CLASSIFY_PROMPT.format(text=text[:500])
        try:
            raw = await self._call_provider(self._active, prompt)
            if not raw:
                return None
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
            elif provider == "gemini":
                return await self._call_gemini(prompt)
            elif provider == "ollama":
                return await self._call_ollama(prompt)
        except Exception as e:
            logger.debug(f"[AI] {provider} call error: {e}")
            if provider != "ollama" and self._active != "ollama":
                logger.warning(f"[AI] {provider} failed — switching to Ollama")
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

    async def _call_gemini(self, prompt: str) -> Optional[str]:
        """Google Gemini 1.5 Flash — free tier covers ~15 RPM."""
        model = self.model or "gemini-1.5-flash"
        resp = await self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 200, "temperature": 0.0},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _call_ollama(self, prompt: str) -> Optional[str]:
        resp = await self._client.post(
            f"{self.ollama_host}/api/generate",
            json={"model": self.ollama_model, "prompt": prompt, "stream": False},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")