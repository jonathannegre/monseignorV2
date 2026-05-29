import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import check_alpaca_account as check


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeUrlopen:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def __call__(self, request, timeout=20):
        self.urls.append(request.full_url)
        suffix = request.full_url.rsplit("/", 1)[-1]
        return FakeHTTPResponse(self.payloads[suffix])


class FailingUrlopen:
    called = False

    def __call__(self, request, timeout=20):
        self.called = True
        raise AssertionError(f"unexpected HTTP call to {request.full_url}")


def test_verified_account_includes_positions_and_open_orders():
    fake = FakeUrlopen({
        "account": {
            "cash": "1000.12",
            "buying_power": "1000.12",
            "equity": "1000.12",
            "multiplier": "1",
            "trading_blocked": False,
            "account_blocked": False,
            "pattern_day_trader": False,
        },
        "positions": [
            {"symbol": "SPY", "qty": "1", "market_value": "500.00", "side": "long"},
        ],
        "orders?status=open": [
            {"id": "ord-1", "symbol": "AAPL", "side": "buy", "qty": "1", "status": "new"},
            {"id": "ord-2", "symbol": "MSFT", "side": "sell", "qty": "1", "status": "accepted"},
        ],
    })

    result = check.check_alpaca_account(
        api_key="key",
        secret="secret",
        base_url="https://paper-api.alpaca.markets",
        urlopen=fake,
    )

    assert result["account_verified"] is True
    assert result["trading_blocked"] is False
    assert result["reason"] == "ok"
    assert result["cash"] == 1000.12
    assert result["buying_power"] == 1000.12
    assert result["open_positions_count"] == 1
    assert result["open_orders_count"] == 2
    assert result["positions"][0]["symbol"] == "SPY"
    assert result["open_orders"][0]["id"] == "ord-1"
    assert fake.urls == [
        "https://paper-api.alpaca.markets/v2/account",
        "https://paper-api.alpaca.markets/v2/positions",
        "https://paper-api.alpaca.markets/v2/orders?status=open",
    ]


def test_margin_available_blocks_trading_for_cash_only_policy():
    fake = FakeUrlopen({
        "account": {
            "cash": "100.00",
            "buying_power": "200.00",
            "equity": "100.00",
            "multiplier": "2",
            "trading_blocked": False,
            "account_blocked": False,
        },
        "positions": [],
        "orders?status=open": [],
    })

    result = check.check_alpaca_account(
        api_key="key",
        secret="secret",
        base_url="https://paper-api.alpaca.markets",
        urlopen=fake,
    )

    assert result["account_verified"] is True
    assert result["trading_blocked"] is True
    assert result["reason"] == "cash_buying_power_margin_incoherence"
    assert result["cash"] == 100.0
    assert result["buying_power"] == 200.0
    assert result["margin_multiplier"] == 2.0
    assert result["margin_available_ignored"] is True
    assert result["cash_control_basis"] == "cash_only"


def test_rejects_non_exact_https_paper_host_before_sending_credentials():
    for base_url in (
        "https://paper-api.alpaca.markets.evil.example",
        "http://paper-api.alpaca.markets",
    ):
        fake = FailingUrlopen()

        result = check.check_alpaca_account(
            api_key="key",
            secret="secret",
            base_url=base_url,
            urlopen=fake,
        )

        assert result["account_verified"] is False
        assert result["trading_blocked"] is True
        assert result["reason"] == "base_url_not_paper"
        assert fake.called is False


def test_invalid_or_negative_account_numbers_block_trading():
    cases = [
        ({"cash": "oops", "buying_power": "100", "equity": "100", "multiplier": "1"}, "invalid_account_cash"),
        ({"cash": "NaN", "buying_power": "100", "equity": "100", "multiplier": "1"}, "invalid_account_cash"),
        ({"cash": "Infinity", "buying_power": "100", "equity": "100", "multiplier": "1"}, "invalid_account_cash"),
        ({"cash": "100", "buying_power": None, "equity": "100", "multiplier": "1"}, "invalid_account_buying_power"),
        ({"cash": "100", "buying_power": "NaN", "equity": "100", "multiplier": "1"}, "invalid_account_buying_power"),
        ({"cash": "100", "buying_power": "100", "equity": "bad", "multiplier": "1"}, "invalid_account_equity"),
        ({"cash": "100", "buying_power": "100", "equity": "-Infinity", "multiplier": "1"}, "invalid_account_equity"),
        ({"cash": "100", "buying_power": "100", "equity": "100", "multiplier": "bad"}, "invalid_account_multiplier"),
        ({"cash": "100", "buying_power": "100", "equity": "100", "multiplier": "NaN"}, "invalid_account_multiplier"),
        ({"cash": "100", "buying_power": "100", "equity": "-50", "multiplier": "1"}, "negative_equity"),
        ({"cash": "100", "buying_power": "-1", "equity": "100", "multiplier": "1"}, "negative_buying_power"),
    ]

    for account, reason in cases:
        account.update({"trading_blocked": False, "account_blocked": False})
        fake = FakeUrlopen({"account": account, "positions": [], "orders?status=open": []})

        result = check.check_alpaca_account(
            api_key="key",
            secret="secret",
            base_url="https://paper-api.alpaca.markets",
            urlopen=fake,
        )

        assert result["account_verified"] is True
        assert result["trading_blocked"] is True
        assert result["reason"] == reason


def test_malformed_account_positions_or_orders_payloads_block_structurally():
    good_account = {
        "cash": "100",
        "buying_power": "100",
        "equity": "100",
        "multiplier": "1",
        "trading_blocked": False,
        "account_blocked": False,
    }
    cases = [
        ({"account": [], "positions": [], "orders?status=open": []}, "invalid_account_payload", False),
        ({"account": good_account, "positions": {"symbol": "SPY"}, "orders?status=open": []}, "invalid_positions_payload", True),
        ({"account": good_account, "positions": [], "orders?status=open": ["bad"]}, "invalid_orders_payload", True),
    ]

    for payloads, reason, account_verified in cases:
        fake = FakeUrlopen(payloads)

        result = check.check_alpaca_account(
            api_key="key",
            secret="secret",
            base_url="https://paper-api.alpaca.markets",
            urlopen=fake,
        )

        assert result["account_verified"] is account_verified
        assert result["trading_blocked"] is True
        assert result["reason"] == reason
