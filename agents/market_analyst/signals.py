"""
SignalEngine — Technical and Fundamental Analysis signal generator.

Indicators implemented:
  TA: RSI, MACD, Bollinger Bands, ATR, EMA cross, Volume profile, OBV
  FA: LLM-powered news sentiment via Claude API (Anthropic)

Each indicator returns a SignalComponent with direction + confidence [0,1].
The engine combines them via weighted voting into a final TradeSignal.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp
import numpy as np

from data_pipeline.feeds import Candle, DataPipeline

logger = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


# ── Types ──────────────────────────────────────────────────────────────────

@dataclass
class SignalComponent:
    name:       str
    direction:  str      # "buy" | "sell" | "neutral"
    confidence: float    # 0.0 – 1.0
    value:      float    # raw indicator value for logging
    metadata:   dict = field(default_factory=dict)


@dataclass
class AggregatedSignal:
    pair:        str
    direction:   str
    confidence:  float
    components:  list[SignalComponent]
    timestamp:   datetime = field(default_factory=datetime.utcnow)
    reasoning:   str = ""

    def to_dict(self) -> dict:
        return {
            "pair":       self.pair,
            "direction":  self.direction,
            "confidence": round(self.confidence, 4),
            "timestamp":  self.timestamp.isoformat(),
            "reasoning":  self.reasoning,
            "components": [
                {"name": c.name, "dir": c.direction, "conf": round(c.confidence, 3), "val": round(c.value, 4)}
                for c in self.components
            ],
        }


# ── Indicator weights ──────────────────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "rsi":       0.15,
    "macd":      0.20,
    "bollinger": 0.15,
    "ema_cross": 0.15,
    "atr":       0.05,   # volatility filter only — low weight
    "obv":       0.10,
    "volume":    0.10,
    "sentiment": 0.10,
}


# ── Technical indicators ───────────────────────────────────────────────────

class Indicators:
    """Pure-numpy TA implementations. All functions are stateless."""

    @staticmethod
    def rsi(closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        delta = np.diff(closes)
        gain  = np.where(delta > 0, delta, 0.0)
        loss  = np.where(delta < 0, -delta, 0.0)
        avg_g = np.mean(gain[-period:])
        avg_l = np.mean(loss[-period:])
        if avg_l == 0:
            return 100.0
        return float(100 - 100 / (1 + avg_g / avg_l))

    @staticmethod
    def macd(closes: np.ndarray, fast=12, slow=26, signal=9) -> tuple[float, float, float]:
        """Returns (macd_line, signal_line, histogram)."""
        def ema(arr: np.ndarray, span: int) -> np.ndarray:
            k = 2 / (span + 1)
            result = np.empty_like(arr)
            result[0] = arr[0]
            for i in range(1, len(arr)):
                result[i] = arr[i] * k + result[i-1] * (1 - k)
            return result

        if len(closes) < slow + signal:
            return 0.0, 0.0, 0.0

        macd_line   = ema(closes, fast) - ema(closes, slow)
        signal_line = ema(macd_line, signal)
        histogram   = macd_line - signal_line
        return float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1])

    @staticmethod
    def bollinger(closes: np.ndarray, period=20, std_dev=2.0) -> tuple[float, float, float]:
        """Returns (upper, mid, lower)."""
        if len(closes) < period:
            m = float(closes[-1])
            return m, m, m
        window = closes[-period:]
        mid    = float(np.mean(window))
        std    = float(np.std(window))
        return mid + std_dev * std, mid, mid - std_dev * std

    @staticmethod
    def ema(closes: np.ndarray, span: int) -> float:
        if len(closes) < span:
            return float(closes[-1])
        k = 2 / (span + 1)
        val = closes[0]
        for c in closes[1:]:
            val = c * k + val * (1 - k)
        return float(val)

    @staticmethod
    def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period=14) -> float:
        """Average True Range — measures volatility."""
        if len(closes) < period + 1:
            return float(np.mean(highs[-period:] - lows[-period:]))
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:]  - closes[:-1]),
            )
        )
        return float(np.mean(tr[-period:]))

    @staticmethod
    def obv(closes: np.ndarray, volumes: np.ndarray) -> float:
        """On-Balance Volume — cumulative volume momentum."""
        if len(closes) < 2:
            return 0.0
        delta   = np.diff(closes)
        signed  = np.where(delta > 0, volumes[1:], np.where(delta < 0, -volumes[1:], 0.0))
        obv_arr = np.cumsum(signed)
        # Normalize to z-score for comparison
        if obv_arr.std() == 0:
            return 0.0
        return float((obv_arr[-1] - obv_arr.mean()) / obv_arr.std())

    @staticmethod
    def volume_spike(volumes: np.ndarray, period=20) -> float:
        """Returns how many std-devs above average the current volume is."""
        if len(volumes) < period + 1:
            return 0.0
        avg = np.mean(volumes[-period-1:-1])
        std = np.std(volumes[-period-1:-1])
        if std == 0:
            return 0.0
        return float((volumes[-1] - avg) / std)


# ── Signal engine ──────────────────────────────────────────────────────────

class SignalEngine:
    """
    Computes all TA indicators + LLM sentiment and returns an AggregatedSignal.

    Usage:
        engine = SignalEngine(pipeline, anthropic_api_key="sk-ant-...")
        signal = await engine.compute("WETH/USDC")
        if signal.confidence > 0.65:
            ...
    """

    def __init__(
        self,
        pipeline:           DataPipeline,
        anthropic_api_key:  str = "",
        weights:            dict[str, float] = None,
        lookback:           int = 200,
    ):
        self.pipeline   = pipeline
        self.api_key    = anthropic_api_key
        self.weights    = weights or DEFAULT_WEIGHTS
        self.lookback   = lookback
        self._ind       = Indicators()
        self._news_cache: dict[str, tuple[float, float]] = {}   # pair → (score, timestamp)

    async def compute(self, pair: str) -> AggregatedSignal:
        """Compute all signals and aggregate into one final signal."""
        candles = await self.pipeline.history(pair, self.lookback)
        if len(candles) < 30:
            return AggregatedSignal(pair=pair, direction="neutral", confidence=0.0, components=[])

        closes  = np.array([c.close  for c in candles])
        highs   = np.array([c.high   for c in candles])
        lows    = np.array([c.low    for c in candles])
        volumes = np.array([c.volume for c in candles])

        components = [
            self._rsi_signal(closes),
            self._macd_signal(closes),
            self._bollinger_signal(closes),
            self._ema_cross_signal(closes),
            self._atr_signal(highs, lows, closes),
            self._obv_signal(closes, volumes),
            self._volume_signal(volumes),
        ]

        # Add sentiment if API key available
        sentiment = await self._sentiment_signal(pair)
        if sentiment:
            components.append(sentiment)

        return self._aggregate(pair, components)

    # ── TA signal builders ─────────────────────────────────────────────────

    def _rsi_signal(self, closes: np.ndarray) -> SignalComponent:
        rsi = self._ind.rsi(closes)
        if rsi < 30:
            direction, conf = "buy",  min(1.0, (30 - rsi) / 20)
        elif rsi > 70:
            direction, conf = "sell", min(1.0, (rsi - 70) / 20)
        else:
            direction, conf = "neutral", 0.0
        return SignalComponent("rsi", direction, conf, rsi)

    def _macd_signal(self, closes: np.ndarray) -> SignalComponent:
        macd_line, signal_line, histogram = self._ind.macd(closes)
        if histogram > 0 and macd_line > signal_line:
            direction = "buy"
            conf = min(1.0, abs(histogram) / (abs(macd_line) + 1e-9) * 2)
        elif histogram < 0 and macd_line < signal_line:
            direction = "sell"
            conf = min(1.0, abs(histogram) / (abs(macd_line) + 1e-9) * 2)
        else:
            direction, conf = "neutral", 0.0
        return SignalComponent("macd", direction, conf, histogram, {"macd": macd_line, "signal": signal_line})

    def _bollinger_signal(self, closes: np.ndarray) -> SignalComponent:
        upper, mid, lower = self._ind.bollinger(closes)
        last = closes[-1]
        band_width = upper - lower
        if band_width == 0:
            return SignalComponent("bollinger", "neutral", 0.0, last)
        if last < lower:
            conf = min(1.0, (lower - last) / (band_width * 0.5))
            return SignalComponent("bollinger", "buy",  conf, last, {"upper": upper, "lower": lower, "mid": mid})
        elif last > upper:
            conf = min(1.0, (last - upper) / (band_width * 0.5))
            return SignalComponent("bollinger", "sell", conf, last, {"upper": upper, "lower": lower, "mid": mid})
        return SignalComponent("bollinger", "neutral", 0.0, last)

    def _ema_cross_signal(self, closes: np.ndarray) -> SignalComponent:
        ema9  = self._ind.ema(closes, 9)
        ema21 = self._ind.ema(closes, 21)
        diff  = (ema9 - ema21) / (ema21 + 1e-9)
        if diff > 0:
            direction, conf = "buy",  min(1.0, diff * 100)
        elif diff < 0:
            direction, conf = "sell", min(1.0, abs(diff) * 100)
        else:
            direction, conf = "neutral", 0.0
        return SignalComponent("ema_cross", direction, conf, diff, {"ema9": ema9, "ema21": ema21})

    def _atr_signal(self, highs, lows, closes) -> SignalComponent:
        """ATR is a volatility filter. High ATR reduces confidence in all signals."""
        atr   = self._ind.atr(highs, lows, closes)
        price = closes[-1]
        atr_pct = atr / price
        # High volatility = lower confidence across the board (filter, not direction)
        conf = max(0.0, 1.0 - atr_pct * 20)
        return SignalComponent("atr", "neutral", conf, atr_pct)

    def _obv_signal(self, closes, volumes) -> SignalComponent:
        z = self._ind.obv(closes, volumes)
        if z > 1.0:
            direction, conf = "buy",  min(1.0, (z - 1.0) / 2)
        elif z < -1.0:
            direction, conf = "sell", min(1.0, (abs(z) - 1.0) / 2)
        else:
            direction, conf = "neutral", 0.0
        return SignalComponent("obv", direction, conf, z)

    def _volume_signal(self, volumes: np.ndarray) -> SignalComponent:
        spike = self._ind.volume_spike(volumes)
        conf  = min(1.0, abs(spike) / 3) if abs(spike) > 1.5 else 0.0
        direction = "neutral"   # volume alone doesn't give direction
        return SignalComponent("volume", direction, conf, spike)

    # ── LLM sentiment ──────────────────────────────────────────────────────

    async def _sentiment_signal(self, pair: str) -> Optional[SignalComponent]:
        if not self.api_key:
            return None

        # Cache sentiment for 15 minutes to avoid API spam
        now = datetime.utcnow().timestamp()
        if pair in self._news_cache:
            score, cached_at = self._news_cache[pair]
            if now - cached_at < 900:
                direction, conf = self._score_to_signal(score)
                return SignalComponent("sentiment", direction, conf, score)

        try:
            score = await self._fetch_sentiment(pair)
            self._news_cache[pair] = (score, now)
            direction, conf = self._score_to_signal(score)
            return SignalComponent("sentiment", direction, conf, score)
        except Exception as e:
            logger.warning(f"[Sentiment] Failed for {pair}: {e}")
            return None

    async def _fetch_sentiment(self, pair: str) -> float:
        """
        Ask Claude to score recent market sentiment for a pair.
        Returns float in [-1.0 bearish, +1.0 bullish].
        """
        base_asset = pair.split("/")[0].replace("W", "", 1)   # "WETH" → "ETH"

        prompt = f"""You are a crypto market analyst. Based on your knowledge of {base_asset} market conditions, 
recent macro trends, and on-chain activity patterns, provide a sentiment score.

Respond ONLY with a JSON object, no other text:
{{
  "score": <float between -1.0 (very bearish) and 1.0 (very bullish)>,
  "reasoning": "<one sentence>",
  "confidence": <float 0.0-1.0>
}}"""

        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANTHROPIC_API,
                headers={
                    "x-api-key":         self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 150,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"API error {resp.status}")
                data = await resp.json()
                text = data["content"][0]["text"].strip()
                parsed = json.loads(text)
                score = float(parsed["score"])
                logger.debug(f"[Sentiment] {pair}: {score:.2f} — {parsed.get('reasoning','')}")
                return max(-1.0, min(1.0, score))

    @staticmethod
    def _score_to_signal(score: float) -> tuple[str, float]:
        if score > 0.2:
            return "buy",  min(1.0, (score - 0.2) / 0.8)
        elif score < -0.2:
            return "sell", min(1.0, (abs(score) - 0.2) / 0.8)
        return "neutral", 0.0

    # ── Aggregation ────────────────────────────────────────────────────────

    def _aggregate(self, pair: str, components: list[SignalComponent]) -> AggregatedSignal:
        """
        Weighted voting:
          - Each component contributes its weight * confidence to buy or sell
          - ATR acts as a global confidence multiplier (high volatility = lower output)
          - Final direction = whichever side has higher weighted score
        """
        buy_score  = 0.0
        sell_score = 0.0
        atr_multiplier = 1.0

        for c in components:
            w = self.weights.get(c.name, 0.05)
            if c.name == "atr":
                atr_multiplier = c.confidence   # [0,1] — used to scale final confidence
                continue
            if c.direction == "buy":
                buy_score  += w * c.confidence
            elif c.direction == "sell":
                sell_score += w * c.confidence

        total = buy_score + sell_score
        if total == 0:
            return AggregatedSignal(pair=pair, direction="neutral", confidence=0.0, components=components)

        if buy_score > sell_score:
            direction   = "buy"
            raw_conf    = buy_score / total * (buy_score / sum(self.weights.values()))
        else:
            direction   = "sell"
            raw_conf    = sell_score / total * (sell_score / sum(self.weights.values()))

        final_conf = min(1.0, raw_conf * atr_multiplier)

        # Build human-readable reasoning
        top = sorted(components, key=lambda c: c.confidence, reverse=True)[:3]
        reasoning = f"{direction.upper()} | conf={final_conf:.2f} | " + " · ".join(
            f"{c.name}={c.direction}({c.confidence:.2f})" for c in top
        )

        return AggregatedSignal(
            pair=pair,
            direction=direction,
            confidence=final_conf,
            components=components,
            reasoning=reasoning,
        )
