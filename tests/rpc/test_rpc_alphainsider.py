import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from requests import Timeout

from freqtrade.enums import RPCMessageType
from freqtrade.exceptions import OperationalException
from freqtrade.rpc.alphainsider import AlphaInsider


def _config(tmp_path):
    return {
        "dry_run": True,
        "alphainsider": {
            "enabled": True,
            "api_key": "test-token",
            "strategy_id": "strategy-1",
            "pair_map": {"BTC/USDT": "BTC-USD:COINBASE"},
            "state_file": str(tmp_path / "alphainsider.json"),
        },
    }


def _fill(message_type=RPCMessageType.ENTRY_FILL):
    msg = {
        "type": message_type,
        "trade_id": 42,
        "pair": "BTC/USDT",
        "direction": "long",
        "amount": 0.001,
        "stake_amount": 80.0,
        "order_rate": 80000.0,
        "open_date": datetime(2026, 1, 1, tzinfo=UTC),
    }
    if message_type == RPCMessageType.EXIT_FILL:
        msg.update(
            {
                "close_date": datetime(2026, 1, 2, tzinfo=UTC),
                "is_final_exit": True,
            }
        )
    return msg


def _handler(mocker, tmp_path):
    request = mocker.patch("requests.Session.request")
    verify = MagicMock()
    verify.raise_for_status.return_value = None
    verify.json.return_value = {
        "success": True,
        "response": {"scope": ["newOrder", "getOrders", "getPositions"]},
    }
    request.return_value = verify
    handler = AlphaInsider(MagicMock(), _config(tmp_path))
    return handler, request


def test_alphainsider_requires_dry_run(tmp_path):
    config = _config(tmp_path)
    config["dry_run"] = False
    with pytest.raises(OperationalException, match="dry_run=true"):
        AlphaInsider(MagicMock(), config)


def test_alphainsider_rejects_untrusted_base_url(tmp_path):
    config = _config(tmp_path)
    config["alphainsider"]["base_url"] = "https://example.com/api"
    with pytest.raises(OperationalException, match="Refusing to send token"):
        AlphaInsider(MagicMock(), config)


def test_alphainsider_mirrors_fill_once(mocker, tmp_path):
    handler, request = _handler(mocker, tmp_path)
    order = MagicMock()
    order.raise_for_status.return_value = None
    order.json.return_value = {
        "success": True,
        "response": {"order_id": "order-1"},
    }
    request.return_value = order

    msg = _fill()
    handler.send_msg(msg)
    handler.send_msg(msg)

    assert request.call_count == 2  # startup verification plus one order
    _, kwargs = request.call_args
    assert kwargs["json"]["action"] == "buy"
    assert kwargs["json"]["stock_id"] == "BTC-USD:COINBASE"
    state = json.loads((tmp_path / "alphainsider.json").read_text())
    assert state["locked"] is False
    assert len(state["completed"]) == 1


def test_alphainsider_exit_reverses_long_action(mocker, tmp_path):
    handler, request = _handler(mocker, tmp_path)
    order = MagicMock()
    order.raise_for_status.return_value = None
    order.json.return_value = {
        "success": True,
        "response": {"order_id": "order-2"},
    }
    request.return_value = order

    handler.send_msg(_fill(RPCMessageType.EXIT_FILL))

    assert request.call_args.kwargs["json"]["action"] == "sell"


def test_alphainsider_timeout_engages_persistent_lock(mocker, tmp_path):
    handler, request = _handler(mocker, tmp_path)
    request.side_effect = Timeout("unknown outcome")

    with pytest.raises(OperationalException, match="safety lock"):
        handler.send_msg(_fill())

    state = json.loads((tmp_path / "alphainsider.json").read_text())
    assert state["locked"] is True
    assert len(state["uncertain"]) == 1

    with pytest.raises(OperationalException, match="safety-locked"):
        handler.send_msg(_fill(RPCMessageType.EXIT_FILL))


def test_alphainsider_rejects_unmapped_pair(mocker, tmp_path):
    handler, _ = _handler(mocker, tmp_path)
    msg = _fill()
    msg["pair"] = "ETH/USDT"

    with pytest.raises(OperationalException, match="No AlphaInsider stock mapping"):
        handler.send_msg(msg)


def test_rpc_manager_registers_alphainsider(mocker, tmp_path, default_conf):
    from freqtrade.rpc import RPCManager
    from tests.conftest import get_patched_freqtradebot

    default_conf["telegram"]["enabled"] = False
    default_conf["alphainsider"] = _config(tmp_path)["alphainsider"]
    mocker.patch("freqtrade.rpc.alphainsider.AlphaInsider._verify_connection")

    manager = RPCManager(get_patched_freqtradebot(mocker, default_conf))

    assert "alphainsider" in [module.name for module in manager.registered_modules]
