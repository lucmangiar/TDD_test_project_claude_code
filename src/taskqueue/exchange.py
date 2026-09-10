"""BTCUSDT spot rate, gathered from several independent brokers.

Every broker here is queried for the *BTC/USDT* pair specifically, never
BTC/USD or BTC/EUR: ``Quote`` rejects any other quote currency outright. USDT
is a US-dollar-pegged stablecoin, so these figures are dollar-denominated, but
they are USDT prices and can drift from a true USD rate when the peg moves.

Network access is injected as a ``Fetcher`` so callers -- and tests -- control
where the bytes come from.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape
from typing import Any

__all__ = [
    "BROKER_NAMES",
    "BrokerError",
    "BrokerFailure",
    "Fetcher",
    "InvalidQuoteError",
    "Quote",
    "RateReport",
    "RateUnavailableError",
    "fetch_quote",
    "fetch_report",
    "http_fetcher",
    "render_html",
]

#: A callable that turns a URL into a response body.
Fetcher = Callable[[str], str]

SYMBOL = "BTCUSDT"
QUOTE_CURRENCY = "USDT"


class BrokerError(Exception):
    """A broker could not be queried, or answered with something unusable."""


class InvalidQuoteError(BrokerError):
    """A quote violated the BTCUSDT contract."""


class RateUnavailableError(BrokerError):
    """No broker produced a usable quote."""


@dataclass(frozen=True)
class Quote:
    """One broker's BTCUSDT price."""

    broker: str
    symbol: str
    price: Decimal
    quote_currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "quote_currency", self.quote_currency.upper())
        if self.price <= 0:
            raise InvalidQuoteError(f"price must be positive, got {self.price}")
        if self.quote_currency != QUOTE_CURRENCY:
            raise InvalidQuoteError(
                f"{self.broker} quoted in {self.quote_currency}; only {QUOTE_CURRENCY} is accepted"
            )


@dataclass(frozen=True)
class BrokerFailure:
    """A broker that could not be included in the report, and why."""

    broker: str
    reason: str


@dataclass(frozen=True)
class RateReport:
    """The outcome of polling every broker once."""

    quotes: tuple[Quote, ...]
    failures: tuple[BrokerFailure, ...]
    generated_at: datetime

    @property
    def median_price(self) -> Decimal:
        prices = sorted(quote.price for quote in self.quotes)
        if not prices:
            raise RateUnavailableError("no broker returned a usable quote")
        middle = len(prices) // 2
        if len(prices) % 2:
            return prices[middle]
        return (prices[middle - 1] + prices[middle]) / 2

    @property
    def spread(self) -> Decimal:
        prices = [quote.price for quote in self.quotes]
        if not prices:
            raise RateUnavailableError("no broker returned a usable quote")
        return max(prices) - min(prices)


_CENTS = Decimal("0.01")


def _format_price(value: Decimal) -> str:
    """Render a price at cent precision, dropping broker-native trailing zeros."""
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _parse_binance(payload: Any) -> str:
    return str(payload["price"])


def _parse_okx(payload: Any) -> str:
    return str(payload["data"][0]["last"])


def _parse_bybit(payload: Any) -> str:
    return str(payload["result"]["list"][0]["lastPrice"])


def _parse_kraken(payload: Any) -> str:
    pair = next(iter(payload["result"].values()))
    return str(pair["c"][0])


@dataclass(frozen=True)
class _BrokerSpec:
    url: str
    parse: Callable[[Any], str]


_SPECS: dict[str, _BrokerSpec] = {
    "binance": _BrokerSpec(
        "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", _parse_binance
    ),
    "okx": _BrokerSpec(
        "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", _parse_okx
    ),
    "bybit": _BrokerSpec(
        "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT", _parse_bybit
    ),
    "kraken": _BrokerSpec(
        "https://api.kraken.com/0/public/Ticker?pair=XBTUSDT", _parse_kraken
    ),
}

#: Brokers polled by default, in order.
BROKER_NAMES: tuple[str, ...] = tuple(_SPECS)


def fetch_quote(broker: str, fetcher: Fetcher) -> Quote:
    """Return one broker's BTCUSDT quote, or raise ``BrokerError``."""
    spec = _SPECS.get(broker)
    if spec is None:
        raise BrokerError(f"unknown broker: {broker!r}")

    try:
        body = fetcher(spec.url)
    except Exception as exc:
        raise BrokerError(f"{broker}: transport failure: {exc}") from exc

    try:
        raw_price = spec.parse(json.loads(body))
        price = Decimal(raw_price)
    except (ValueError, TypeError, KeyError, IndexError, StopIteration, InvalidOperation) as exc:
        raise BrokerError(f"{broker}: malformed payload: {exc!r}") from exc

    return Quote(broker=broker, symbol=SYMBOL, price=price, quote_currency=QUOTE_CURRENCY)


def fetch_report(
    fetcher: Fetcher,
    brokers: tuple[str, ...] | None = None,
    generated_at: datetime | None = None,
) -> RateReport:
    """Poll every broker once, keeping the ones that answer usably."""
    names = BROKER_NAMES if brokers is None else tuple(brokers)
    quotes: list[Quote] = []
    failures: list[BrokerFailure] = []
    for name in names:
        try:
            quotes.append(fetch_quote(name, fetcher))
        except BrokerError as exc:
            failures.append(BrokerFailure(broker=name, reason=str(exc)))
    return RateReport(
        quotes=tuple(quotes),
        failures=tuple(failures),
        generated_at=generated_at or datetime.now(UTC),
    )


def _rows(report: RateReport) -> str:
    if not report.quotes:
        return (
            '<tr><td class="none" colspan="3">No broker returned a usable quote.</td></tr>'
        )
    median = report.median_price
    rows = []
    for quote in sorted(report.quotes, key=lambda q: q.price):
        delta = quote.price - median
        sign = "+" if delta > 0 else ""
        rows.append(
            f"<tr><td>{escape(quote.broker)}</td>"
            f'<td class="num">{escape(_format_price(quote.price))}</td>'
            f'<td class="num delta">{sign}{escape(_format_price(delta))}</td></tr>'
        )
    return "".join(rows)


def _failure_rows(report: RateReport) -> str:
    if not report.failures:
        return ""
    items = "".join(
        f"<li><b>{escape(failure.broker)}</b> &mdash; {escape(failure.reason)}</li>"
        for failure in report.failures
    )
    return f'<section class="failures"><h2>Unavailable</h2><ul>{items}</ul></section>'


def render_html(report: RateReport) -> str:
    """Render the report as a standalone HTML page."""
    try:
        summary = (
            f'<p class="summary">Median <b>{escape(_format_price(report.median_price))} USDT</b>'
            f" &middot; spread {escape(_format_price(report.spread))} USDT"
            f" across {len(report.quotes)} brokers</p>"
        )
    except RateUnavailableError:
        summary = '<p class="summary none">No broker returned a usable quote.</p>'

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>BTCUSDT across brokers</title><style>"
        "body{font:14px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:44rem;"
        "color:#1c1b19;background:#faf9f7}"
        "h1{font-size:1.25rem;margin:0 0 .25rem}"
        ".summary{margin:.25rem 0 1.25rem;color:#4a463f}"
        ".none{color:#a8321f}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{padding:.5rem .75rem;border-bottom:1px solid #e4dfd5;text-align:left}"
        "th{font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;color:#6c675e}"
        ".num{font-variant-numeric:tabular-nums;text-align:right}"
        ".delta{color:#6c675e}"
        ".failures{margin-top:1.5rem;font-size:.875rem;color:#6c675e}"
        "footer{margin-top:1.5rem;font-size:.75rem;color:#8b857a}"
        "</style></head><body>"
        f"<h1>{SYMBOL} spot rate</h1>"
        f"{summary}"
        "<table><thead><tr><th>Broker</th><th class=\"num\">Price (USDT)</th>"
        '<th class="num">vs median</th></tr></thead>'
        f"<tbody>{_rows(report)}</tbody></table>"
        f"{_failure_rows(report)}"
        f"<footer>Generated {escape(report.generated_at.isoformat())}. "
        "Prices are BTC/USDT. USDT is pegged to the US dollar, so these track the "
        "dollar value of BTC but are not a USD rate.</footer>"
        "</body></html>"
    )


def http_fetcher(timeout: float = 5.0) -> Fetcher:
    """Return a ``Fetcher`` that performs a real HTTP GET."""

    def fetch(url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "taskqueue-exchange/0.1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
        return body.decode("utf-8")

    return fetch


if __name__ == "__main__":  # pragma: no cover
    print(render_html(fetch_report(http_fetcher())))
