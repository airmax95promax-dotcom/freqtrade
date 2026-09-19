"""Fail-closed AlphaInsider paper-trade mirror for confirmed Freqtrade dry-run fills."""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import requests
from requests import RequestException

from freqtrade.constants import Config
from freqtrade.enums import RPCMessageType
from freqtrade.exceptions import OperationalException
from freqtrade.rpc import RPC, RPCHandler
from freqtrade.rpc.rpc_types import RPCOrderMsg, RPCSendMsg


logger = logging.getLogger(__name__)


class AlphaInsider(RPCHandler):
    """Mirror confirmed Freqtrade dry-run fills to AlphaInsider paper trading.

    This handler deliberately does not implement an Exchange subclass. AlphaInsider is
    an external paper-order destination, while Freqtrade exchanges are CCXT execution
    venues. Mirroring at the RPC fill boundary keeps Freqtrade's exchange engine intact.
    """

    _SUPPORTED_TYPES = {RPCMessageType.ENTRY_FILL, RPCMessageType.EXIT_FILL}

    def __init__(self, rpc: RPC, config: Config) -> None:
        super().__init__(rpc, config)
        cfg = config["alphainsider"]

        if not config.get("dry_run", False):
            raise OperationalException(
                "AlphaInsider mirroring requires Freqtrade dry_run=true. Refusing to start."
            )

        self._api_key = str(cfg.get("api_key") or "").strip()
        self._strategy_id = str(cfg.get("strategy_id") or "").strip()
        if not self._api_key or not self._strategy_id:
            raise OperationalException(
                "AlphaInsider mirroring requires api_key and strategy_id. "
                "Provide them through FREQTRADE__ALPHAINSIDER environment variables."
            )

        self._base_url = str(cfg.get("base_url") or "https://alphainsider.com/api").rstrip("/")
        parsed_url = urlparse(self._base_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "alphainsider.com":
            raise OperationalException(
                "AlphaInsider base_url must use HTTPS on alphainsider.com. Refusing to send token."
            )
        self._timeout = float(cfg.get("timeout", 10))
        self._pair_map = {str(k): str(v) for k, v in cfg.get("pair_map", {}).items()}
        state_path = cfg.get("state_file") or "user_data/alphainsider_mirror_state.json"
        self._state_path = Path(state_path).expanduser().resolve()
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": self._api_key, "Content-Type": "application/json"}
        )
        self._state = self._load_state()

        if self._state.get("locked", False):
            raise OperationalException(
                "AlphaInsider mirror is safety-locked after an uncertain submission. "
                f"Inspect and reconcile {self._state_path} before restarting."
            )

        self._verify_connection()

    def cleanup(self) -> None:
        self._session.close()

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"version": 1, "locked": False, "completed": {}, "uncertain": {}}
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationalException(
                f"Unable to read AlphaInsider mirror state at {self._state_path}."
            ) from exc
        if not isinstance(value, dict):
            raise OperationalException("AlphaInsider mirror state must be a JSON object.")
        value.setdefault("completed", {})
        value.setdefault("uncertain", {})
        value.setdefault("locked", False)
        return value

    def _persist_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
        temporary.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self._state_path)

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        response = self._session.request(
            method, f"{self._base_url}/{endpoint.lstrip('/')}", timeout=self._timeout, **kwargs
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("success") is not True:
            message = (
                result.get("response", "Unexpected AlphaInsider response")
                if isinstance(result, dict)
                else "Unexpected AlphaInsider response"
            )
            raise RequestException(str(message), response=response)
        return result.get("response")

    def _verify_connection(self) -> None:
        try:
            token = self._request("GET", "verifyToken")
        except (RequestException, ValueError) as exc:
            raise OperationalException("Unable to verify the AlphaInsider API token.") from exc
        scopes = token.get("scope", []) if isinstance(token, dict) else []
        required_scopes = {"newOrder", "getOrders", "getPositions"}
        missing_scopes = required_scopes.difference(scopes)
        if missing_scopes:
            raise OperationalException(
                "AlphaInsider token lacks required scopes: " + ", ".join(sorted(missing_scopes))
            )

    @staticmethod
    def _serialize_value(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _event_key(self, msg: RPCOrderMsg) -> str:
        fields = [
            msg["type"].value,
            msg.get("trade_id"),
            msg.get("pair"),
            msg.get("direction"),
            msg.get("amount"),
            msg.get("stake_amount"),
            msg.get("order_rate"),
            msg.get("open_date"),
            msg.get("close_date"),
            msg.get("is_final_exit"),
            msg.get("cumulative_profit"),
        ]
        canonical = "|".join(self._serialize_value(value) for value in fields)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _stock_id(self, pair: str) -> str:
        stock_id = self._pair_map.get(pair)
        if not stock_id:
            raise OperationalException(
                f"No AlphaInsider stock mapping configured for Freqtrade pair {pair!r}."
            )
        return stock_id

    @staticmethod
    def _action(msg: RPCOrderMsg) -> str:
        direction = str(msg.get("direction", "")).lower()
        is_entry = msg["type"] == RPCMessageType.ENTRY_FILL
        if direction == "long":
            return "buy" if is_entry else "sell"
        if direction == "short":
            return "sell" if is_entry else "buy"
        raise OperationalException(f"Unsupported Freqtrade direction {direction!r}.")

    def send_msg(self, msg: RPCSendMsg) -> None:
        if msg["type"] not in self._SUPPORTED_TYPES:
            return
        order_msg = cast(RPCOrderMsg, msg)
        if self._state.get("locked", False):
            raise OperationalException("AlphaInsider mirror is safety-locked.")

        event_key = self._event_key(order_msg)
        if event_key in self._state["completed"]:
            logger.info("Skipping duplicate AlphaInsider mirror event %s", event_key[:12])
            return

        payload = {
            "strategy_id": self._strategy_id,
            "stock_id": self._stock_id(str(order_msg["pair"])),
            "action": self._action(order_msg),
            "type": "market",
            "amount": f"{float(order_msg['amount']):.15f}",
        }

        try:
            order = self._request("POST", "newOrder", json=payload)
        except (RequestException, ValueError) as exc:
            # The request may have reached AlphaInsider even when its response was lost.
            # Never retry automatically: lock and force explicit reconciliation.
            self._state["locked"] = True
            self._state["uncertain"][event_key] = {
                "trade_id": order_msg.get("trade_id"),
                "pair": order_msg.get("pair"),
                "action": payload["action"],
                "amount": payload["amount"],
            }
            self._persist_state()
            raise OperationalException(
                "AlphaInsider submission outcome is uncertain; mirror safety lock engaged."
            ) from exc

        order_id = order.get("order_id") if isinstance(order, dict) else None
        if not order_id:
            self._state["locked"] = True
            self._state["uncertain"][event_key] = {"trade_id": order_msg.get("trade_id")}
            self._persist_state()
            raise OperationalException(
                "AlphaInsider response contained no order_id; mirror safety lock engaged."
            )

        self._state["completed"][event_key] = {
            "order_id": str(order_id),
            "trade_id": order_msg.get("trade_id"),
            "event": order_msg["type"].value,
        }
        # Bound persistent idempotency history while preserving insertion order.
        if len(self._state["completed"]) > 5000:
            oldest = next(iter(self._state["completed"]))
            del self._state["completed"][oldest]
        self._persist_state()
        logger.info(
            "Mirrored Freqtrade %s for trade %s to AlphaInsider order %s.",
            order_msg["type"].value,
            order_msg.get("trade_id"),
            order_id,
        )
