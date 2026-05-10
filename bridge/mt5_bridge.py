"""
MT5 File Bridge
===============
File-based communication with MT5 EA.
Uses MT5_SESSION_PREFIX env var to avoid file collisions with other
Python instances running on the same MT5 terminal.

Session ID format: {prefix}_{uuid8}  e.g. "signal_a1b2c3d4"
Command file:  python_command_{session_id}_{N}.txt   (per-request)
Response file: python_response_{session_id}_{N}.txt  (per-request)
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_MT5_GLOBAL_LOCK: Optional[asyncio.Lock] = None

def _get_lock() -> asyncio.Lock:
    global _MT5_GLOBAL_LOCK
    if _MT5_GLOBAL_LOCK is None:
        _MT5_GLOBAL_LOCK = asyncio.Lock()
    return _MT5_GLOBAL_LOCK


class MT5FileBridge:
    """
    File-based MT5 bridge. No TCP, no ports.
    Reads/writes files in MT5 Common Files directory.
    """

    def __init__(self, session_prefix: str = "signal", demo_mode: bool = False):
        self.demo_mode      = demo_mode
        self.session_prefix  = os.getenv("MT5_SESSION_PREFIX", session_prefix)
        self.session_id     = f"{self.session_prefix}_{uuid.uuid4().hex[:8]}"
        self.common_path    = self._find_common_path()
        self.request_counter = 0
        self._connected     = False

        # Shared status/session files (same prefix for EA to identify us)
        self.status_file  = self.common_path / f"mt5_status_{self.session_prefix}.txt"
        self.session_file = self.common_path / f"python_session_{self.session_prefix}.txt"

        # Demo state
        self._demo_positions: Dict[int, dict] = {}
        self._demo_orders:    Dict[int, dict] = {}   # pending limit orders
        self._demo_counter   = 1000

    # ── Path detection ─────────────────────────────────────────────────────────

    def _find_common_path(self) -> Path:
        # Allow explicit override
        override = os.getenv("MT5_FILES_PATH")
        if override:
            p = Path(override)
            if p.exists():
                return p

        candidates = [
            Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal" / "Common" / "Files",
            Path.home() / "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / "Common" / "Files",
            # Linux wine path
            Path.home() / ".wine" / "drive_c" / "users" / os.getenv("USER", "user") /
            "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / "Common" / "Files",
        ]
        for p in candidates:
            if p.exists():
                logger.info(f"[BRIDGE] MT5 Common Files: {p}")
                return p

        fallback = candidates[0]
        fallback.mkdir(parents=True, exist_ok=True)
        logger.warning(f"[BRIDGE] MT5 path not found — using fallback: {fallback}")
        return fallback

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if self.demo_mode:
            self._connected = True
            logger.info("[BRIDGE] Running in DEMO mode")
            return True

        try:
            self.session_file.write_text(self.session_id, encoding="utf-8")
            logger.info(f"[BRIDGE] Session ID: {self.session_id}")
        except Exception as e:
            logger.error(f"[BRIDGE] Cannot write session file: {e}")
            return False

        await asyncio.sleep(1.0)

        if self.status_file.exists():
            try:
                status = self.status_file.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff")
                if "ready" in status.lower():
                    logger.info("[BRIDGE] MT5 EA is ready")
                    self._connected = True
                    return True
                else:
                    logger.warning(f"[BRIDGE] EA status: {status}")
                    self._connected = True
                    return True
            except Exception as e:
                logger.error(f"[BRIDGE] Status read error: {e}")
        else:
            logger.warning("[BRIDGE] Status file not found — is EA running?")

        self._connected = False
        return False

    async def disconnect(self):
        self._connected = False
        logger.info("[BRIDGE] Disconnected")

    def is_connected(self) -> bool:
        return self._connected

    # ── Core command layer ─────────────────────────────────────────────────────

    async def _send_command(self, command: Dict, timeout: float = 30.0) -> Dict:
        if self.demo_mode:
            return await self._demo_command(command)

        async with _get_lock():
            self.request_counter += 1
            request_id = f"{self.session_id}_{self.request_counter}"
            command["request_id"] = request_id

            cmd_file = self.common_path / f"python_command_{request_id}.txt"
            try:
                cmd_file.write_text(json.dumps(command, ensure_ascii=True), encoding="utf-8")
            except Exception as e:
                logger.error(f"[BRIDGE] Write error {request_id}: {e}")
                return {"status": "error", "error": str(e)}

        # Poll for response file
        resp_file = self.common_path / f"python_response_{request_id}.txt"
        deadline  = asyncio.get_event_loop().time() + timeout
        poll      = 0.02

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll)
            poll = min(poll * 1.5, 0.5)

            if not resp_file.exists():
                continue
            try:
                raw = resp_file.read_text(encoding="utf-8", errors="ignore").strip()
                # Response file may contain tagged line: {request_id}|{json}
                for line in raw.splitlines():
                    if request_id in line:
                        payload = line.split("|", 1)[-1].strip()
                        resp_file.unlink(missing_ok=True)
                        return json.loads(payload)
                # Fallback: try parsing the whole file
                resp_file.unlink(missing_ok=True)
                return json.loads(raw)
            except Exception as e:
                logger.debug(f"[BRIDGE] Response parse error {request_id}: {e}")

        resp_file.unlink(missing_ok=True)
        logger.error(f"[BRIDGE] {request_id} ({command.get('action')}) timed out after {timeout}s")
        return {"status": "error", "error": "timeout"}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def get_equity(self) -> Optional[float]:
        resp = await self._send_command({"action": "authenticate"})
        if resp.get("status") == "success":
            return float(resp.get("equity", 0) or 0)
        return None

    async def get_price(self, symbol: str, direction: str) -> Optional[float]:
        resp = await self._send_command({
            "action": "get_historical", "symbol": symbol,
            "timeframe": "M1", "count": 1,
        })
        if resp.get("status") == "success" and resp.get("data"):
            close  = float(resp["data"][-1][4])
            spread = 0.30  # XAUUSD typical
            return round(close + spread / 2, 2) if direction == "buy" else round(close - spread / 2, 2)
        return None

    async def place_market_order(self, symbol: str, direction: str,
                                  lot: float, sl: float, tp: float,
                                  comment: str = "signal") -> Optional[dict]:
        return await self._send_command({
            "action":     "place_order",
            "symbol":     symbol,
            "order_type": "ORDER_TYPE_BUY" if direction == "buy" else "ORDER_TYPE_SELL",
            "volume":     lot,
            "sl":         sl,
            "tp":         tp or 0.0,
            "comment":    comment,
        }, timeout=10.0)

    async def place_limit_order(self, symbol: str, direction: str,
                                 lot: float, price: float,
                                 sl: float, tp: float,
                                 comment: str = "signal_limit") -> Optional[dict]:
        return await self._send_command({
            "action":     "place_order",
            "symbol":     symbol,
            "order_type": "ORDER_TYPE_BUY_LIMIT" if direction == "buy" else "ORDER_TYPE_SELL_LIMIT",
            "price":      price,
            "volume":     lot,
            "sl":         sl,
            "tp":         tp or 0.0,
            "comment":    comment,
        }, timeout=10.0)

    async def modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        # Get current position to validate SL direction
        pos = await self.get_position(ticket)
        if pos and sl > 0:
            cur_price = float(pos.get("current_price") or pos.get("price_current") or 0)
            pos_type = pos.get("type", "").lower()
            if cur_price > 0:
                if "buy" in pos_type and sl >= cur_price:
                    logger.warning(
                        f"[BRIDGE] Refusing modify ticket={ticket}: "
                        f"buy SL {sl} >= current {cur_price}"
                    )
                    return False
                if "sell" in pos_type and sl <= cur_price:
                    logger.warning(
                        f"[BRIDGE] Refusing modify ticket={ticket}: "
                        f"sell SL {sl} <= current {cur_price}"
                    )
                    return False

        resp = await self._send_command({
            "action": "modify_position", "ticket": ticket, "sl": sl, "tp": tp,
        }, timeout=10.0)
        return resp.get("status") == "success"

    async def close_position(self, ticket: int, lot: Optional[float] = None) -> bool:
        cmd: Dict = {"action": "close_position", "ticket": ticket}
        if lot:
            cmd["lot_size"] = lot
        resp = await self._send_command(cmd, timeout=10.0)
        return resp.get("status") == "success"

    async def get_all_positions(self) -> Optional[list]:
        resp = await self._send_command({"action": "get_all_positions"})
        if resp.get("status") == "success":
            return resp.get("positions", [])
        return None

    async def get_all_orders(self) -> Optional[list]:
        """Returns all pending limit/stop orders placed by this EA."""
        resp = await self._send_command({"action": "get_all_orders"})
        if resp.get("status") == "success":
            return resp.get("orders", [])
        return None

    async def cancel_order(self, ticket: int) -> bool:
        """Cancels a pending limit order by ticket."""
        resp = await self._send_command({"action": "cancel_order", "ticket": ticket})
        return resp.get("status") == "success"

    async def get_position(self, ticket: int) -> Optional[dict]:
        resp = await self._send_command({"action": "get_position", "ticket": ticket})
        if resp.get("status") == "success":
            return resp
        return None

    async def get_deal_history(self, ticket: int) -> Optional[dict]:
        resp = await self._send_command({"action": "get_deal_history", "ticket": ticket})
        if resp.get("status") == "success":
            return resp
        return None

    # ── Demo simulator ─────────────────────────────────────────────────────────

    async def _demo_command(self, command: Dict) -> Dict:
        action = command.get("action")
        if action == "authenticate":
            return {"status": "success", "balance": 1000.0, "equity": 1000.0}
        if action == "get_historical":
            return {"status": "success", "data": [[0, 2000, 2001, 1999, 2000, 100]]}
        if action == "place_order":
            ticket = self._demo_counter
            self._demo_counter += 1
            order_type = command.get("order_type", "")
            is_pending = "LIMIT" in order_type or "STOP" in order_type
            entry = {**command, "ticket": ticket, "profit": 0.0}
            if is_pending:
                self._demo_orders[ticket] = entry    # pending order
            else:
                self._demo_positions[ticket] = entry # live position
            return {"status": "success", "ticket": ticket,
                    "price": command.get("price", 2000.0)}
        if action == "get_all_orders":
            return {"status": "success", "orders": list(self._demo_orders.values())}
        if action == "cancel_order":
            ticket = command.get("ticket")
            if ticket in self._demo_orders:
                self._demo_orders.pop(ticket)
                return {"status": "success", "ticket": ticket}
            return {"status": "error", "error": "pending order not found"}
        if action == "modify_position":
            ticket = command.get("ticket")
            if ticket in self._demo_positions:
                self._demo_positions[ticket].update({"sl": command.get("sl"), "tp": command.get("tp")})
                return {"status": "success"}
            return {"status": "error", "error": "not found"}
        if action == "close_position":
            ticket = command.get("ticket")
            self._demo_positions.pop(ticket, None)
            return {"status": "success"}
        if action == "get_all_positions":
            return {"status": "success", "positions": list(self._demo_positions.values())}
        if action == "get_position":
            ticket = command.get("ticket")
            pos = self._demo_positions.get(ticket)
            if pos:
                return {"status": "success", **pos}
            return {"status": "error", "error": "not found"}
        if action == "get_deal_history":
            return {"status": "success", "exit_price": 2001.0, "profit": 1.0,
                    "volume": 0.01, "close_time": 0, "net_profit": 1.0}
        return {"status": "error", "error": f"unknown action: {action}"}