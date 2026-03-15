"""
DataPipeline — unified price feed aggregator for Agenopoly agents.

Sources:
  1. Binance WebSocket  — real-time CEX price + volume (lowest latency)
  2. The Graph (GraphQL) — on-chain DEX prices (Uniswap v3 on Arbitrum)
  3. Chainlink REST      — oracle TWAP prices (manipulation-resistant)

The pipeline merges all three into a single normalized OHLCV stream.
Agents subscribe to pairs and receive Candle objects via asyncio.Queue.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import aiohttp

logger = logging.getLogger(__name__)

# ── Data types ─────────────────────────────────────────────────────────────

@dataclass
class Candle:
    pair:       str
    open:       float
    high:       float
    low:        float
    close:      float
    volume:     float
    timestamp:  datetime
    source:     str           # "binance" | "uniswap" | "chainlink"

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3


@dataclass
class OrderBookSnapshot:
    pair:      str
    bids:      list[tuple[float, float]]   # [(price, size), ...]
    asks:      list[tuple[float, float]]
    timestamp: datetime
    source:    str

    @property
    def mid_price(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread_bps(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.asks[0][0] - self.bids[0][0]) / self.mid_price * 10_000


# ── Binance WebSocket feed ─────────────────────────────────────────────────

BINANCE_WS_URL  = "wss://stream.binance.com:9443/stream"
BINANCE_REST    = "https://api.binance.com/api/v3"

PAIR_TO_BINANCE = {
    "WETH/USDC": "ethusdc",
    "WBTC/USDC": "btcusdc",
    "ARB/USDC":  "arbusdc",
    "USDC/WETH": "ethusdc",  # inverted — handled in normalization
}


class BinanceFeed:
    """
    Streams 1-minute klines via Binance WebSocket.
    Falls back to REST polling if WS drops.
    """

    def __init__(self, pairs: list[str]):
        self.pairs   = pairs
        self._queues: dict[str, asyncio.Queue] = {p: asyncio.Queue(maxsize=500) for p in pairs}
        self._running = False

    async def start(self):
        self._running = True
        streams = [f"{PAIR_TO_BINANCE[p]}@kline_1m" for p in self.pairs if p in PAIR_TO_BINANCE]
        url = f"{BINANCE_WS_URL}?streams={'/'.join(streams)}"

        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url) as ws:
                        logger.info(f"[Binance] WebSocket connected | {len(streams)} streams")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(json.loads(msg.data))
            except Exception as e:
                logger.warning(f"[Binance] WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False

    async def _handle_message(self, data: dict):
        k = data.get("data", {}).get("k", {})
        if not k or not k.get("x"):   # x = candle closed
            return

        symbol = k["s"].upper()
        pair   = self._symbol_to_pair(symbol)
        if not pair:
            return

        candle = Candle(
            pair=pair,
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            timestamp=datetime.utcfromtimestamp(k["T"] / 1000),
            source="binance",
        )

        if pair in self._queues:
            try:
                self._queues[pair].put_nowait(candle)
            except asyncio.QueueFull:
                self._queues[pair].get_nowait()   # drop oldest
                self._queues[pair].put_nowait(candle)

    async def get_candle(self, pair: str) -> Candle:
        return await self._queues[pair].get()

    async def fetch_history(self, pair: str, limit: int = 500) -> list[Candle]:
        """Fetch historical klines via REST for backtesting warm-up."""
        symbol = PAIR_TO_BINANCE.get(pair, "").upper()
        if not symbol:
            return []
        url = f"{BINANCE_REST}/klines?symbol={symbol}&interval=1m&limit={limit}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"[Binance] REST error {resp.status} for {pair}")
                    return []
                rows = await resp.json()

        return [
            Candle(
                pair=pair,
                open=float(r[1]), high=float(r[2]),
                low=float(r[3]),  close=float(r[4]),
                volume=float(r[5]),
                timestamp=datetime.utcfromtimestamp(r[0] / 1000),
                source="binance",
            )
            for r in rows
        ]

    @staticmethod
    def _symbol_to_pair(symbol: str) -> str | None:
        mapping = {"ETHUSDC": "WETH/USDC", "BTCUSDC": "WBTC/USDC", "ARBUSDC": "ARB/USDC"}
        return mapping.get(symbol)


# ── The Graph — on-chain DEX prices ───────────────────────────────────────

UNISWAP_V3_SUBGRAPH_ARB = (
    "https://api.thegraph.com/subgraphs/name/ianlapham/arbitrum-minimal"
)

POOL_ADDRESSES = {
    "WETH/USDC": "0xc31e54c7a869b9fcbecc14363cf510d1c41fa443",
    "WBTC/USDC": "0xa62ad78825e3a55a77823f00fe0050f567c1e4ee",
    "ARB/USDC":  "0xcda53b1f66614552f834ceef361a8d12a0b8dad8",
}


class TheGraphFeed:
    """
    Fetches on-chain Uniswap v3 pool data via The Graph GraphQL API.
    Used for:
      - Real DEX prices (vs CEX mid-price)
      - Pool liquidity depth
      - Historical on-chain prices for backtesting
    """

    POOL_QUERY = """
    query PoolData($pool: String!, $first: Int!) {
      poolHourDatas(
        where: { pool: $pool }
        orderBy: periodStartUnix
        orderDirection: desc
        first: $first
      ) {
        periodStartUnix
        token0Price
        token1Price
        volumeUSD
        liquidity
        high
        low
        open
        close
      }
    }
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.base_url = UNISWAP_V3_SUBGRAPH_ARB

    async def fetch_pool_history(self, pair: str, hours: int = 168) -> list[Candle]:
        """Fetch up to `hours` of hourly OHLCV data for a pair."""
        pool_address = POOL_ADDRESSES.get(pair)
        if not pool_address:
            logger.warning(f"[TheGraph] No pool address for {pair}")
            return []

        async with aiohttp.ClientSession() as session:
            payload = {"query": self.POOL_QUERY, "variables": {"pool": pool_address, "first": hours}}
            async with session.post(self.base_url, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"[TheGraph] HTTP {resp.status}")
                    return []
                data = await resp.json()

        rows = data.get("data", {}).get("poolHourDatas", [])
        candles = []
        for r in rows:
            try:
                candles.append(Candle(
                    pair=pair,
                    open=float(r.get("open") or r["token0Price"]),
                    high=float(r.get("high") or r["token0Price"]),
                    low=float(r.get("low")  or r["token0Price"]),
                    close=float(r.get("close") or r["token0Price"]),
                    volume=float(r.get("volumeUSD", 0)),
                    timestamp=datetime.utcfromtimestamp(r["periodStartUnix"]),
                    source="uniswap",
                ))
            except (KeyError, ValueError) as e:
                logger.debug(f"[TheGraph] Skip row: {e}")

        return sorted(candles, key=lambda c: c.timestamp)

    async def get_current_price(self, pair: str) -> float | None:
        """Single price point for the latest block."""
        candles = await self.fetch_pool_history(pair, hours=1)
        return candles[-1].close if candles else None


# ── Unified pipeline ───────────────────────────────────────────────────────

class DataPipeline:
    """
    Aggregates all feeds into one interface for agents.

    Usage:
        pipeline = DataPipeline(pairs=["WETH/USDC", "ARB/USDC"])
        await pipeline.start()
        candle = await pipeline.latest("WETH/USDC")
        history = await pipeline.history("WETH/USDC", lookback=200)
    """

    def __init__(self, pairs: list[str], graph_api_key: str = ""):
        self.pairs    = pairs
        self._binance = BinanceFeed(pairs)
        self._graph   = TheGraphFeed(graph_api_key)
        self._cache:  dict[str, list[Candle]] = {p: [] for p in pairs}
        self._subs:   dict[str, list[asyncio.Queue]] = {p: [] for p in pairs}

    async def start(self):
        """Start all feeds and warm up cache from history."""
        logger.info("[Pipeline] Warming up cache from Binance REST...")
        for pair in self.pairs:
            history = await self._binance.fetch_history(pair, limit=500)
            self._cache[pair] = history
            logger.info(f"[Pipeline] {pair}: {len(history)} candles loaded")

        # Start live feed in background
        asyncio.create_task(self._binance.start())
        asyncio.create_task(self._dispatch_loop())
        logger.info("[Pipeline] Live feeds running")

    async def history(self, pair: str, lookback: int = 200) -> list[Candle]:
        """Return the last `lookback` candles for a pair."""
        return self._cache[pair][-lookback:]

    async def latest(self, pair: str) -> Candle | None:
        return self._cache[pair][-1] if self._cache[pair] else None

    async def latest_onchain(self, pair: str) -> float | None:
        """Get the latest on-chain DEX price for comparison with CEX."""
        return await self._graph.get_current_price(pair)

    def subscribe(self, pair: str) -> asyncio.Queue:
        """Subscribe to a real-time candle stream for a pair."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs[pair].append(q)
        return q

    async def _dispatch_loop(self):
        """Forward new candles from feeds to all subscribers + cache."""
        tasks = [self._forward(pair) for pair in self.pairs]
        await asyncio.gather(*tasks)

    async def _forward(self, pair: str):
        while True:
            candle = await self._binance.get_candle(pair)
            self._cache[pair].append(candle)
            if len(self._cache[pair]) > 1000:
                self._cache[pair] = self._cache[pair][-1000:]
            for q in self._subs[pair]:
                try:
                    q.put_nowait(candle)
                except asyncio.QueueFull:
                    pass
