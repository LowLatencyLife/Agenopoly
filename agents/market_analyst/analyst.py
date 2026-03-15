"""
MarketAnalystAgent — generates trade signals from TA indicators and LLM-powered sentiment.
"""

import logging
import numpy as np
from typing import Optional
from agents.base.agent import BaseAgent, AgentConfig, TradeSignal

logger = logging.getLogger(__name__)


class MarketAnalystAgent(BaseAgent):
    """
    Generates signals using:
      - Technical Analysis: RSI, MACD, Bollinger Bands, volume
      - Fundamental Analysis: LLM sentiment on crypto news headlines
    """

    def __init__(self, config: AgentConfig, anthropic_api_key: Optional[str] = None):
        super().__init__(config)
        self.api_key = anthropic_api_key
        self.watched_pairs = ["WETH/USDC", "WBTC/USDC", "ARB/USDC"]

    # ── Signal generation ──────────────────────────────────────────────────

    async def generate_signals(self) -> list[TradeSignal]:
        signals = []
        for pair in self.watched_pairs:
            prices = await self._fetch_prices(pair, lookback=100)
            if len(prices) < 26:
                continue

            ta_signal = self._compute_ta_signal(pair, prices)
            sentiment = await self._get_sentiment(pair)

            # Combine TA and sentiment into a final signal
            combined_confidence = ta_signal["confidence"] * 0.7 + sentiment * 0.3
            if combined_confidence > 0.6:
                signals.append(TradeSignal(
                    pair=pair,
                    direction=ta_signal["direction"],
                    confidence=round(combined_confidence, 3),
                    size_usd=self.config.max_position_usd * combined_confidence,
                    source="TA+FA",
                    metadata={"rsi": ta_signal.get("rsi"), "macd": ta_signal.get("macd"), "sentiment": sentiment},
                ))

        return signals

    async def evaluate_proposal(self, proposal: dict) -> bool:
        """Accept A2A proposals only when they align with our current signal."""
        pair = proposal.get("pair")
        direction = proposal.get("direction")
        prices = await self._fetch_prices(pair, lookback=50)
        ta = self._compute_ta_signal(pair, prices)
        return ta["direction"] == direction and ta["confidence"] > 0.55

    # ── Technical Analysis ─────────────────────────────────────────────────

    def _compute_ta_signal(self, pair: str, prices: list[float]) -> dict:
        arr = np.array(prices)
        rsi = self._rsi(arr, 14)
        macd_line, signal_line = self._macd(arr)
        upper, lower = self._bollinger(arr, 20, 2)
        last = arr[-1]

        bull_score = 0.0
        if rsi < 35:
            bull_score += 0.4
        elif rsi > 65:
            bull_score -= 0.4
        if macd_line > signal_line:
            bull_score += 0.3
        else:
            bull_score -= 0.3
        if last < lower:
            bull_score += 0.3
        elif last > upper:
            bull_score -= 0.3

        direction = "buy" if bull_score > 0 else "sell"
        confidence = min(abs(bull_score), 1.0)
        return {"direction": direction, "confidence": confidence, "rsi": round(float(rsi), 1), "macd": round(float(macd_line), 4)}

    @staticmethod
    def _rsi(prices: np.ndarray, period: int = 14) -> float:
        delta = np.diff(prices)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = np.mean(gain[-period:])
        avg_loss = np.mean(loss[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _macd(prices: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
        def ema(arr, span):
            k = 2 / (span + 1)
            result = [arr[0]]
            for p in arr[1:]:
                result.append(p * k + result[-1] * (1 - k))
            return np.array(result)
        macd_line = ema(prices, fast) - ema(prices, slow)
        signal_line = ema(macd_line, signal)
        return macd_line[-1], signal_line[-1]

    @staticmethod
    def _bollinger(prices: np.ndarray, period: int = 20, std_dev: float = 2.0):
        window = prices[-period:]
        mid = np.mean(window)
        std = np.std(window)
        return mid + std_dev * std, mid - std_dev * std

    # ── LLM Sentiment ──────────────────────────────────────────────────────

    async def _get_sentiment(self, pair: str) -> float:
        """
        Call Claude API to score sentiment from recent news headlines.
        Returns float in [-1.0, 1.0] mapped to [0.0, 1.0] for bearish/bullish.
        TODO: integrate real news feed (CryptoPanic, Messari).
        """
        if not self.api_key:
            return 0.5  # neutral default
        # Placeholder — real implementation calls Claude with recent headlines
        return 0.5

    # ── Data fetching ──────────────────────────────────────────────────────

    async def _fetch_prices(self, pair: str, lookback: int = 100) -> list[float]:
        """
        Fetch OHLCV from CEX or on-chain oracle.
        TODO: integrate Binance WS / Chainlink / The Graph.
        """
        # Placeholder: return synthetic prices for local testing
        np.random.seed(hash(pair) % 2**32)
        return list(np.cumsum(np.random.randn(lookback) * 10) + 1800)
