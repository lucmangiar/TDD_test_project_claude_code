"""Live BTC/USDT spot rate, gathered from several independent brokers.

Nothing here stores a rate: every price is fetched from a broker at the moment
it is needed. The generated page keeps fetching on its own, straight from the
browser, so an open page stays current without Python running.

The traded pair is a parameter. Only USDT-quoted pairs are accepted -- ``Pair``
and ``Quote`` both reject anything else -- so a report can never silently mix in
a BTC/USD or BTC/EUR price. USDT is a US-dollar-pegged stablecoin, so these
figures track the dollar value of the asset without being a true USD rate.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape
from typing import Any

__all__ = [
    "BROKER_NAMES",
    "DEFAULT_PAIR",
    "REFRESH_SECONDS",
    "BrokerError",
    "BrokerFailure",
    "Fetcher",
    "InvalidQuoteError",
    "Pair",
    "Quote",
    "RateReport",
    "RateUnavailableError",
    "broker_url",
    "fetch_quote",
    "fetch_report",
    "http_fetcher",
    "render_html",
]

#: A callable that turns a URL into a response body.
Fetcher = Callable[[str], str]

QUOTE_CURRENCY = "USDT"

#: How often the generated page re-polls the brokers, in seconds.
REFRESH_SECONDS = 1800


class BrokerError(Exception):
    """A broker could not be queried, or answered with something unusable."""


class InvalidQuoteError(BrokerError):
    """A pair or quote violated the USDT contract."""


class RateUnavailableError(BrokerError):
    """No broker produced a usable quote."""


@dataclass(frozen=True)
class Pair:
    """A USDT-quoted trading pair, e.g. BTC/USDT."""

    base: str
    quote: str = QUOTE_CURRENCY

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", self.base.strip().upper())
        object.__setattr__(self, "quote", self.quote.strip().upper())
        if not self.base.isalnum():
            raise InvalidQuoteError(f"base currency must be alphanumeric, got {self.base!r}")
        if self.quote != QUOTE_CURRENCY:
            raise InvalidQuoteError(
                f"pair quoted in {self.quote}; only {QUOTE_CURRENCY} is accepted"
            )

    @property
    def symbol(self) -> str:
        """The pair as an exchange symbol, e.g. ``BTCUSDT``."""
        return f"{self.base}{self.quote}"


DEFAULT_PAIR = Pair(base="BTC")


@dataclass(frozen=True)
class Quote:
    """One broker's price for a pair."""

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


def _concat(pair: Pair) -> str:
    return pair.symbol


def _dashed(pair: Pair) -> str:
    return f"{pair.base}-{pair.quote}"


def _kraken_symbol(pair: Pair) -> str:
    # Kraken lists bitcoin under its ISO-style code XBT, not BTC.
    base = "XBT" if pair.base == "BTC" else pair.base
    return f"{base}{pair.quote}"


@dataclass(frozen=True)
class _BrokerSpec:
    template: str
    symbol_for: Callable[[Pair], str]
    #: Where the price sits in the JSON body. ``"*"`` means "first value".
    price_path: tuple[str | int, ...]


_SPECS: dict[str, _BrokerSpec] = {
    "binance": _BrokerSpec(
        "https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
        _concat,
        ("price",),
    ),
    "okx": _BrokerSpec(
        "https://www.okx.com/api/v5/market/ticker?instId={symbol}",
        _dashed,
        ("data", 0, "last"),
    ),
    "bybit": _BrokerSpec(
        "https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}",
        _concat,
        ("result", "list", 0, "lastPrice"),
    ),
    "kraken": _BrokerSpec(
        "https://api.kraken.com/0/public/Ticker?pair={symbol}",
        _kraken_symbol,
        ("result", "*", "c", 0),
    ),
}

#: Brokers polled by default, in order.
BROKER_NAMES: tuple[str, ...] = tuple(_SPECS)


def broker_url(broker: str, pair: Pair = DEFAULT_PAIR) -> str:
    """Return the endpoint that quotes ``pair`` on ``broker``."""
    spec = _SPECS.get(broker)
    if spec is None:
        raise BrokerError(f"unknown broker: {broker!r}")
    return spec.template.format(symbol=spec.symbol_for(pair))


def _extract(payload: Any, path: tuple[str | int, ...]) -> str:
    node = payload
    for step in path:
        node = next(iter(node.values())) if step == "*" else node[step]
    return str(node)


def fetch_quote(broker: str, fetcher: Fetcher, pair: Pair = DEFAULT_PAIR) -> Quote:
    """Return one broker's quote for ``pair``, or raise ``BrokerError``."""
    url = broker_url(broker, pair)
    spec = _SPECS[broker]

    try:
        body = fetcher(url)
    except Exception as exc:
        raise BrokerError(f"{broker}: transport failure: {exc}") from exc

    try:
        price = Decimal(_extract(json.loads(body), spec.price_path))
    except (ValueError, TypeError, KeyError, IndexError, StopIteration, InvalidOperation) as exc:
        raise BrokerError(f"{broker}: malformed payload: {exc!r}") from exc

    return Quote(
        broker=broker, symbol=pair.symbol, price=price, quote_currency=pair.quote
    )


def fetch_report(
    fetcher: Fetcher,
    brokers: tuple[str, ...] | None = None,
    generated_at: datetime | None = None,
    pair: Pair = DEFAULT_PAIR,
) -> RateReport:
    """Poll every broker once, keeping the ones that answer usably."""
    names = BROKER_NAMES if brokers is None else tuple(brokers)
    quotes: list[Quote] = []
    failures: list[BrokerFailure] = []
    for name in names:
        try:
            quotes.append(fetch_quote(name, fetcher, pair))
        except BrokerError as exc:
            failures.append(BrokerFailure(broker=name, reason=str(exc)))
    return RateReport(
        quotes=tuple(quotes),
        failures=tuple(failures),
        generated_at=generated_at or datetime.now(UTC),
    )


_CENTS = Decimal("0.01")


def _format_price(value: Decimal) -> str:
    """Render a price at cent precision, dropping broker-native trailing zeros."""
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _rows(report: RateReport) -> str:
    if not report.quotes:
        return '<tr><td class="none" colspan="3">No broker returned a usable quote.</td></tr>'
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


def _failure_items(report: RateReport) -> str:
    return "".join(
        f"<li><b>{escape(failure.broker)}</b> &mdash; {escape(failure.reason)}</li>"
        for failure in report.failures
    )


_STYLE = (
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
    "#failures{margin-top:1.5rem;font-size:.875rem;color:#6c675e}"
    "#failures:empty{display:none}"
    "button{font:inherit;padding:.3rem .7rem;border:1px solid #cfc8bb;border-radius:4px;"
    "background:#fff;cursor:pointer}"
    "footer{margin-top:1.5rem;font-size:.75rem;color:#8b857a}"
)

_SCRIPT = """
const CFG = JSON.parse(document.getElementById("cfg").textContent);
const $ = (id) => document.getElementById(id);

function dig(payload, path) {
  let node = payload;
  for (const step of path) {
    node = step === "*" ? Object.values(node)[0] : node[step];
    if (node === undefined || node === null) throw new Error("unexpected payload shape");
  }
  return Number(node);
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

async function poll(broker) {
  const response = await fetch(broker.url, { cache: "no-store" });
  if (!response.ok) throw new Error("HTTP " + response.status);
  const price = dig(await response.json(), broker.path);
  if (!(price > 0)) throw new Error("non-positive price");
  return { broker: broker.name, price };
}

async function refresh() {
  $("refresh").disabled = true;
  const settled = await Promise.allSettled(CFG.brokers.map(poll));
  const quotes = [];
  const failures = [];
  settled.forEach((outcome, index) => {
    if (outcome.status === "fulfilled") quotes.push(outcome.value);
    else failures.push({ broker: CFG.brokers[index].name, reason: String(outcome.reason) });
  });

  const body = $("rows");
  body.textContent = "";
  if (quotes.length === 0) {
    const row = document.createElement("tr");
    const only = cell("No broker returned a usable quote.", "none");
    only.colSpan = 3;
    row.appendChild(only);
    body.appendChild(row);
    $("summary").textContent = "No broker returned a usable quote.";
    $("summary").className = "summary none";
  } else {
    const prices = quotes.map((q) => q.price);
    const mid = median(prices);
    quotes.sort((a, b) => a.price - b.price);
    for (const q of quotes) {
      const row = document.createElement("tr");
      const delta = q.price - mid;
      row.appendChild(cell(q.broker));
      row.appendChild(cell(q.price.toFixed(2), "num"));
      row.appendChild(cell((delta > 0 ? "+" : "") + delta.toFixed(2), "num delta"));
      body.appendChild(row);
    }
    const spread = Math.max(...prices) - Math.min(...prices);
    $("summary").className = "summary";
    $("summary").textContent =
      "Median " + mid.toFixed(2) + " USDT \\u00b7 spread " + spread.toFixed(2) +
      " USDT across " + quotes.length + " brokers";
  }

  const list = $("failures");
  list.textContent = "";
  if (failures.length) {
    const heading = document.createElement("h2");
    heading.textContent = "Unavailable";
    list.appendChild(heading);
    const ul = document.createElement("ul");
    for (const failure of failures) {
      const li = document.createElement("li");
      li.textContent = failure.broker + " \\u2014 " + failure.reason;
      ul.appendChild(li);
    }
    list.appendChild(ul);
  }

  $("stamp").textContent = new Date().toISOString();
  $("refresh").disabled = false;
}

$("refresh").addEventListener("click", refresh);
setInterval(refresh, CFG.refreshSeconds * 1000);
refresh();
"""


def render_html(report: RateReport, refresh_seconds: int = REFRESH_SECONDS) -> str:
    """Render the report as a standalone page that keeps refreshing itself."""
    symbol = report.quotes[0].symbol if report.quotes else DEFAULT_PAIR.symbol
    pair = Pair(base=symbol.removesuffix(QUOTE_CURRENCY))

    config = json.dumps(
        {
            "refreshSeconds": refresh_seconds,
            "symbol": pair.symbol,
            "brokers": [
                {"name": name, "url": broker_url(name, pair), "path": list(spec.price_path)}
                for name, spec in _SPECS.items()
            ],
        }
    )

    try:
        summary = (
            f"Median <b>{escape(_format_price(report.median_price))} USDT</b>"
            f" &middot; spread {escape(_format_price(report.spread))} USDT"
            f" across {len(report.quotes)} brokers"
        )
        summary_class = "summary"
    except RateUnavailableError:
        summary = "No broker returned a usable quote."
        summary_class = "summary none"

    minutes = max(1, refresh_seconds // 60)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(pair.symbol)} across brokers</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<h1>{escape(pair.symbol)} spot rate</h1>"
        f'<p class="{summary_class}" id="summary">{summary}</p>'
        '<p><button id="refresh" type="button">Refresh now</button></p>'
        '<table><thead><tr><th>Broker</th><th class="num">Price (USDT)</th>'
        '<th class="num">vs median</th></tr></thead>'
        f'<tbody id="rows">{_rows(report)}</tbody></table>'
        f'<section id="failures">{_failure_items(report)}</section>'
        f'<footer>Updated <span id="stamp">{escape(report.generated_at.isoformat())}</span>. '
        f"This page re-polls every broker directly, every {minutes} minutes. "
        "Prices are quoted in USDT, a US-dollar-pegged stablecoin, so they track the "
        "dollar value of the asset but are not a USD rate.</footer>"
        f'<script type="application/json" id="cfg">{config}</script>'
        f"<script>{_SCRIPT}</script>"
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
    base = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    print(render_html(fetch_report(http_fetcher(), pair=Pair(base=base))))
