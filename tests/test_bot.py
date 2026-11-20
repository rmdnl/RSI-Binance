import os
import tempfile
import unittest
from decimal import Decimal, ROUND_DOWN
from unittest.mock import patch

from bot import Bot, Config, LiveBroker, closed_only, load_state, rsi_series


class OrderNotFound(Exception):
    pass


class FakeExchange:
    def __init__(self, free=0.00067):
        self.free = free
        self.created = []
        self.fetch_calls = []

    def fetch_balance(self):
        return {"free": {"SOL": self.free}}

    def market(self, symbol):
        return {
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}},
            "info": {"filters": [
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            ]},
        }

    def amount_to_precision(self, symbol, qty):
        return format(Decimal(str(qty)).quantize(Decimal("0.001"), rounding=ROUND_DOWN), "f")

    def cost_to_precision(self, symbol, cost):
        return str(cost)

    def create_market_buy_order_with_cost(self, symbol, cost, params):
        self.created.append((symbol, cost, params))
        return {"id": "buy-1", "status": "closed", "filled": 1.0,
                "average": 10.0, "cost": 10.0, "fees": []}

    def create_market_sell_order(self, *args):
        self.created.append(args)
        return {"id": "sell-1", "status": "closed", "filled": 1.0,
                "average": 10.0, "cost": 10.0, "fees": []}

    def fetch_order(self, order_id, symbol, params):
        self.fetch_calls.append((order_id, symbol, params))
        return {"id": "buy-1", "status": "closed", "filled": 1.0,
                "average": 10.0, "cost": 10.0, "fees": []}


class TimeoutExchange(FakeExchange):
    def create_market_buy_order_with_cost(self, symbol, cost, params):
        raise TimeoutError("simulated response timeout")


class MissingOrderExchange(FakeExchange):
    def fetch_order(self, order_id, symbol, params):
        self.fetch_calls.append((order_id, symbol, params))
        raise OrderNotFound("not found yet")


class DummyNotifier:
    def send(self, message):
        pass


def make_config():
    with patch.dict(os.environ, {"SYMBOLS": "SOL/USDT", "DRY_RUN": "true"}, clear=True):
        return Config.from_env()


class IndicatorTests(unittest.TestCase):
    def test_constant_price_rsi_is_neutral(self):
        values = rsi_series([10.0] * 30, 14)
        self.assertEqual(values[-1], 50.0)

    def test_closed_only_excludes_open_candle(self):
        candles = [[0, 1, 2, 0, 1, 1], [900000, 1, 2, 0, 1, 1]]
        self.assertEqual(closed_only(candles, 900000, 1800000), candles)
        self.assertEqual(closed_only(candles, 900000, 1799999), candles[:1])


class ConfigTests(unittest.TestCase):
    def test_rejects_non_finite_position_size(self):
        with patch.dict(os.environ, {
            "SYMBOLS": "SOL/USDT", "DRY_RUN": "true", "POSITION_PCT": "nan"
        }, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


class BrokerSafetyTests(unittest.TestCase):
    def test_dust_sell_is_rejected_before_order_submission(self):
        ex = FakeExchange(free=0.00067)
        broker = LiveBroker(ex, "USDT")
        result = broker.sell("SOL/USDT", 0.00067, 70.0, "sell-id", True)
        self.assertTrue(result["dust"])
        self.assertEqual(ex.created, [])

    def test_buy_sends_client_order_id(self):
        ex = FakeExchange(free=10)
        broker = LiveBroker(ex, "USDT")
        result = broker.buy("SOL/USDT", 10, 10, "buy-id", True)
        self.assertEqual(result["qty"], 1.0)
        self.assertEqual(ex.created[0][2]["clientOrderId"], "buy-id")

    def test_buy_reconciliation_queries_existing_order_without_resubmitting(self):
        ex = FakeExchange(free=10)
        broker = LiveBroker(ex, "USDT")
        result = broker.buy("SOL/USDT", 10, 10, "buy-id", False)
        self.assertEqual(result["qty"], 1.0)
        self.assertEqual(ex.created, [])
        self.assertEqual(ex.fetch_calls[0][0], None)
        self.assertEqual(ex.fetch_calls[0][2]["origClientOrderId"], "buy-id")

    def test_balance_mismatch_blocks_market_sell(self):
        ex = FakeExchange(free=0.0005)
        broker = LiveBroker(ex, "USDT")
        result = broker.sell("SOL/USDT", 0.001, 70.0, "sell-id", True)
        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(ex.created, [])


class PendingBuyTests(unittest.TestCase):
    def test_buy_intent_is_saved_before_exchange_request(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg.state_file = os.path.join(tmp, "state.json")
            ex = TimeoutExchange(free=10)
            state = {"symbols": {"SOL/USDT": {"status": "idle", "position": None, "entries": []}}}
            bot = Bot(cfg, None, LiveBroker(ex, "USDT"), DummyNotifier(), state, now_fn=lambda: 1000)
            with self.assertRaises(TimeoutError):
                bot._begin_buy("SOL/USDT", state["symbols"]["SOL/USDT"],
                               10, 10, 2, 30, 30, 29, 33, 1000)
            self.assertIn("buy_pending", state["symbols"]["SOL/USDT"])
            self.assertTrue(os.path.exists(cfg.state_file))

    def test_unknown_buy_order_is_queried_not_resubmitted(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg.state_file = os.path.join(tmp, "state.json")
            ex = MissingOrderExchange(free=10)
            state = {"symbols": {"SOL/USDT": {
                "status": "idle", "position": None,
                "buy_pending": {"client_order_id": "buy-id", "cost": 10, "price": 10,
                    "tp_pct": 2, "rsi_4h": 30, "rsi_1h": 30,
                    "rsi_15m_prev": 29, "rsi_15m_last": 33,
                    "created_at": 100, "attempts": 0, "next_check_at": 0}
            }}}
            bot = Bot(cfg, None, LiveBroker(ex, "USDT"), DummyNotifier(), state, now_fn=lambda: 100)
            bot.process("SOL/USDT")
            self.assertEqual(ex.created, [])
            self.assertEqual(ex.fetch_calls[0][2]["origClientOrderId"], "buy-id")
            self.assertIn("buy_pending", state["symbols"]["SOL/USDT"])


class StateSafetyTests(unittest.TestCase):
    def test_corrupt_state_fails_closed_instead_of_resetting_positions(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not valid json")
            with self.assertRaises(ValueError):
                load_state(path, cfg)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "{not valid json")

    def test_dust_state_is_not_processed_as_a_trading_position(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg.state_file = os.path.join(tmp, "state.json")
            state = {"symbols": {"SOL/USDT": {
                "status": "dust", "position": {"qty": 0.00067}
            }}}
            bot = Bot(cfg, None, object(), DummyNotifier(), state)
            bot._scan_entry = lambda *args: self.fail("dust pair must not scan for entries")
            bot.process("SOL/USDT")


if __name__ == "__main__":
    unittest.main()
