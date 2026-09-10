"""Black-box tests for the BTCUSDT multi-broker rate feature.

Every test drives the public API of ``taskqueue.exchange``. Network access is
supplied through the public ``fetcher`` seam, so no test reaches the internet
and no internal function is patched.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from taskqueue.exchange import (
    BROKER_NAMES,
    BrokerError,
    BrokerFailure,
    InvalidQuoteError,
    Quote,
    RateReport,
    RateUnavailableError,
    fetch_quote,
    fetch_report,
    http_fetcher,
    render_html,
)

# --- canned broker payloads, shaped like the real endpoints -----------------

BINANCE_BODY = json.dumps({"symbol": "BTCUSDT", "price": "64000.10"})
OKX_BODY = json.dumps({"code": "0", "data": [{"instId": "BTC-USDT", "last": "64010.50"}]})
BYBIT_BODY = json.dumps(
    {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "64020.00"}]}}
)
KRAKEN_BODY = json.dumps({"error": [], "result": {"XBTUSDT": {"c": ["64030.00", "0.5"]}}})

BODIES = {
    "binance": BINANCE_BODY,
    "okx": OKX_BODY,
    "bybit": BYBIT_BODY,
    "kraken": KRAKEN_BODY,
}

FIXED_TIME = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class RecordingFetcher:
    """Test double injected through the public fetcher seam."""

    def __init__(self, bodies: dict[str, str] | None = None, error: Exception | None = None):
        self._bodies = bodies if bodies is not None else dict(BODIES)
        self._error = error
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        for name, body in self._bodies.items():
            if name in url or name.upper() in url:
                return body
        raise AssertionError(f"no canned body matches {url!r}")


def quote(broker: str, price: str) -> Quote:
    return Quote(broker=broker, symbol="BTCUSDT", price=Decimal(price), quote_currency="USDT")


# --- Quote ------------------------------------------------------------------


def test_quote_normalizes_and_exposes_its_fields() -> None:
    q = Quote(broker="binance", symbol="btcusdt", price=Decimal("64000.10"),
              quote_currency="usdt")
    assert q.broker == "binance"
    assert q.symbol == "BTCUSDT"
    assert q.price == Decimal("64000.10")
    assert q.quote_currency == "USDT"


def test_quote_rejects_non_positive_price() -> None:
    with pytest.raises(InvalidQuoteError, match="price must be positive"):
        Quote(broker="binance", symbol="BTCUSDT", price=Decimal("0"), quote_currency="USDT")


def test_quote_rejects_a_quote_currency_that_is_not_usdt() -> None:
    with pytest.raises(InvalidQuoteError, match="USDT"):
        Quote(broker="kraken", symbol="XBTEUR", price=Decimal("64000"), quote_currency="EUR")


# --- BrokerFailure ----------------------------------------------------------


def test_broker_failure_records_the_broker_and_the_reason() -> None:
    failure = BrokerFailure(broker="okx", reason="HTTP 503")
    assert failure.broker == "okx"
    assert failure.reason == "HTTP 503"


# --- RateReport -------------------------------------------------------------


def test_report_median_price_is_the_midpoint_of_an_odd_number_of_quotes() -> None:
    report = RateReport(
        quotes=(quote("binance", "64000"), quote("okx", "64010"), quote("bybit", "64030")),
        failures=(),
        generated_at=FIXED_TIME,
    )
    assert report.median_price == Decimal("64010")


def test_report_median_price_averages_the_middle_pair_when_even() -> None:
    report = RateReport(
        quotes=(quote("binance", "64000"), quote("okx", "64010")),
        failures=(),
        generated_at=FIXED_TIME,
    )
    assert report.median_price == Decimal("64005")


def test_report_spread_is_highest_minus_lowest() -> None:
    report = RateReport(
        quotes=(quote("binance", "64000"), quote("okx", "64010"), quote("bybit", "64030")),
        failures=(),
        generated_at=FIXED_TIME,
    )
    assert report.spread == Decimal("30")


def test_report_median_raises_when_every_broker_failed() -> None:
    report = RateReport(
        quotes=(),
        failures=(BrokerFailure(broker="binance", reason="timeout"),),
        generated_at=FIXED_TIME,
    )
    with pytest.raises(RateUnavailableError, match="no broker returned a usable quote"):
        _ = report.median_price


# --- BROKER_NAMES -----------------------------------------------------------


def test_broker_names_lists_at_least_three_distinct_brokers() -> None:
    assert len(BROKER_NAMES) >= 3
    assert len(set(BROKER_NAMES)) == len(BROKER_NAMES)
    assert "binance" in BROKER_NAMES


# --- fetch_quote ------------------------------------------------------------


@pytest.mark.parametrize(
    ("broker", "expected"),
    [
        ("binance", Decimal("64000.10")),
        ("okx", Decimal("64010.50")),
        ("bybit", Decimal("64020.00")),
        ("kraken", Decimal("64030.00")),
    ],
)
def test_fetch_quote_parses_each_brokers_payload(broker: str, expected: Decimal) -> None:
    fetcher = RecordingFetcher()
    q = fetch_quote(broker, fetcher)
    assert q.broker == broker
    assert q.price == expected
    assert q.symbol == "BTCUSDT"
    assert q.quote_currency == "USDT"
    assert len(fetcher.urls) == 1
    assert fetcher.urls[0].startswith("https://")


def test_fetch_quote_rejects_an_unknown_broker() -> None:
    fetcher = RecordingFetcher()
    with pytest.raises(BrokerError, match="unknown broker"):
        fetch_quote("not-a-broker", fetcher)
    assert fetcher.urls == []


def test_fetch_quote_wraps_a_transport_failure() -> None:
    fetcher = RecordingFetcher(error=OSError("connection reset"))
    with pytest.raises(BrokerError, match="connection reset"):
        fetch_quote("binance", fetcher)


def test_fetch_quote_rejects_a_malformed_payload() -> None:
    fetcher = RecordingFetcher(bodies={"binance": '{"symbol": "BTCUSDT"}'})
    with pytest.raises(BrokerError, match="binance"):
        fetch_quote("binance", fetcher)


def test_fetch_quote_rejects_a_non_numeric_price() -> None:
    fetcher = RecordingFetcher(bodies={"binance": json.dumps({"price": "not-a-number"})})
    with pytest.raises(BrokerError, match="binance"):
        fetch_quote("binance", fetcher)


# --- fetch_report -----------------------------------------------------------


def test_fetch_report_collects_a_quote_from_every_broker() -> None:
    report = fetch_report(RecordingFetcher(), generated_at=FIXED_TIME)
    assert [q.broker for q in report.quotes] == list(BROKER_NAMES)
    assert report.failures == ()
    assert report.generated_at == FIXED_TIME


def test_fetch_report_keeps_healthy_brokers_when_one_fails() -> None:
    bodies = dict(BODIES)
    bodies["okx"] = "{ this is not json"
    report = fetch_report(RecordingFetcher(bodies), generated_at=FIXED_TIME)
    assert [q.broker for q in report.quotes] == ["binance", "bybit", "kraken"]
    assert [f.broker for f in report.failures] == ["okx"]
    assert report.median_price == Decimal("64020.00")


def test_fetch_report_records_every_broker_as_failed_when_transport_is_down() -> None:
    report = fetch_report(
        RecordingFetcher(error=OSError("network unreachable")), generated_at=FIXED_TIME
    )
    assert report.quotes == ()
    assert [f.broker for f in report.failures] == list(BROKER_NAMES)
    with pytest.raises(RateUnavailableError):
        _ = report.median_price


# --- render_html ------------------------------------------------------------


def test_render_html_lists_every_broker_with_its_price() -> None:
    report = RateReport(
        quotes=(quote("binance", "64000.10"), quote("okx", "64010.50")),
        failures=(),
        generated_at=FIXED_TIME,
    )
    html = render_html(report)
    assert html.startswith("<!doctype html>")
    assert "BTCUSDT" in html
    assert "binance" in html
    assert "64000.10" in html
    assert "okx" in html
    assert "64010.50" in html
    assert "2026-09-09" in html


def test_render_html_escapes_broker_supplied_failure_text() -> None:
    report = RateReport(
        quotes=(quote("binance", "64000.10"),),
        failures=(BrokerFailure(broker="okx", reason="<script>alert(1)</script>"),),
        generated_at=FIXED_TIME,
    )
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_states_when_no_broker_returned_a_quote() -> None:
    report = RateReport(
        quotes=(),
        failures=(BrokerFailure(broker="binance", reason="timeout"),),
        generated_at=FIXED_TIME,
    )
    html = render_html(report)
    assert "No broker returned a usable quote" in html
    assert "timeout" in html


# --- http_fetcher -----------------------------------------------------------


@pytest.fixture
def local_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/missing":
                self.send_error(404)
                return
            body = BINANCE_BODY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_fetcher_returns_the_response_body(local_server: str) -> None:
    fetch = http_fetcher(timeout=5.0)
    assert fetch(f"{local_server}/ok") == BINANCE_BODY


def test_http_fetcher_raises_on_an_http_error(local_server: str) -> None:
    fetch = http_fetcher(timeout=5.0)
    with pytest.raises(urllib.error.HTTPError):
        fetch(f"{local_server}/missing")


def test_render_html_trims_broker_precision_to_cents() -> None:
    report = RateReport(
        quotes=(quote("binance", "77972.01000000"), quote("kraken", "77978.30000")),
        failures=(),
        generated_at=FIXED_TIME,
    )
    html = render_html(report)
    assert "77972.01000000" not in html
    assert "77978.30000" not in html
    assert ">77972.01<" in html
    assert ">77978.30<" in html
    assert "Median <b>77975.16 USDT</b>" in html
    assert "spread 6.29 USDT" in html
