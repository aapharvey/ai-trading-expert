"""
Telegram notification module.
Sends trading signals, system alerts, and heartbeats to a Telegram chat.
Uses the Telegram Bot API directly via requests (no async dependency).
"""

from typing import Optional

import requests

import config
from logger import get_logger
from src.models.signals import TradeSignal, Direction

log = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
_SEND_TIMEOUT = 10
_MAX_MESSAGE_LEN = 4096


class TelegramError(Exception):
    """Raised when Telegram API returns ok=false."""


class TelegramNotifier:
    """
    Sends messages to a Telegram chat via Bot API.
    All methods are synchronous (no asyncio required).
    Failures are logged but do NOT raise — system continues running.
    """

    def __init__(
        self,
        token: str = config.TELEGRAM_BOT_TOKEN,
        chat_id: str = config.TELEGRAM_CHAT_ID,
    ):
        self._token = token
        self._chat_id = chat_id
        self._session = requests.Session()

    # ─── Internal ────────────────────────────────────────────────────────────

    def _send(self, text: str, parse_mode: str = "HTML", reply_to_message_id: Optional[int] = None) -> Optional[int]:
        """
        Send a text message. Returns Telegram message_id on success, None on error.
        Truncates messages longer than Telegram's limit.
        """
        if len(text) > _MAX_MESSAGE_LEN:
            text = text[:_MAX_MESSAGE_LEN - 20] + "\n...[truncated]"

        url = _TELEGRAM_API.format(token=self._token, method="sendMessage")
        payload = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            resp = self._session.post(url, json=payload, timeout=_SEND_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                raise TelegramError(body.get("description", "unknown error"))
            message_id = body["result"]["message_id"]
            log.debug("Telegram message sent (chat=%s, id=%d)", self._chat_id, message_id)
            return message_id
        except TelegramError as exc:
            log.error("Telegram API error: %s", exc)
        except requests.RequestException as exc:
            log.error("Telegram network error: %s", exc)
        return None

    # ─── Public API ──────────────────────────────────────────────────────────

    def send_signal(self, signal: TradeSignal) -> Optional[int]:
        """Format and send a trading signal message. Returns message_id or None on error."""
        direction_emoji = "📈" if signal.direction == Direction.LONG else "📉"
        risk_pct = abs(signal.entry_mid - signal.stop_loss) / signal.entry_mid * 100

        factors_text = "\n".join(f"✅ {f}" for f in signal.factors) if signal.factors else "—"

        text = (
            f"🚨 <b>BTC TRADING SIGNAL</b>\n"
            f"{'─' * 30}\n"
            f"Direction: <b>{signal.direction.value}</b> {direction_emoji}\n"
            f"Strength: {signal.strength_stars} ({signal.strength}/5)\n"
            f"{'─' * 30}\n"
            f"Entry Zone: <b>${signal.entry_low:,.0f} – ${signal.entry_high:,.0f}</b>\n"
            f"Target 1:  <b>${signal.tp1:,.0f}</b> "
            f"(+{abs(signal.tp1 - signal.entry_mid) / signal.entry_mid * 100:.1f}%)\n"
            f"Target 2:  <b>${signal.tp2:,.0f}</b> "
            f"(+{abs(signal.tp2 - signal.entry_mid) / signal.entry_mid * 100:.1f}%)\n"
            f"Stop Loss: <b>${signal.stop_loss:,.0f}</b> (-{risk_pct:.1f}%)\n"
            f"R:R Ratio: <b>1:{signal.rr_ratio:.1f}</b>\n"
            f"{'─' * 30}\n"
            f"Confluence factors:\n{factors_text}\n"
            f"{'─' * 30}\n"
            f"Timeframe: {signal.timeframe.value}\n"
            f"Risk: 1–2% of portfolio per trade\n\n"
            f"⚠️ <i>NOT FINANCIAL ADVICE. Manual execution only.</i>"
        )
        log.info(
            "Sending %s signal (strength=%d/5) to Telegram",
            signal.direction.value, signal.strength,
        )
        return self._send(text)

    def send_outcome_reply(
        self,
        message_id: int,
        outcome: str,
        exit_price: float,
        direction: str,
    ) -> bool:
        """Send outcome notification as a reply to the original signal message."""
        _labels = {
            "WIN_FULL":    f"✅ <b>TP2 досягнуто</b> | вихід: <b>${exit_price:,.0f}</b>",
            "WIN_PARTIAL": f"🟡 <b>TP1 досягнуто</b> | вихід: <b>${exit_price:,.0f}</b>",
            "LOSS":        f"❌ <b>SL спрацював</b> | вихід: <b>${exit_price:,.0f}</b>",
            "EXPIRED":     "⏱ <b>Час вийшов</b> | результат невизначений",
        }
        text = _labels.get(outcome, f"ℹ️ Результат: {outcome} | ${exit_price:,.0f}")
        log.info("Sending outcome reply (msg_id=%d): %s", message_id, outcome)
        return self._send(text, reply_to_message_id=message_id) is not None

    def send_alert(self, message: str) -> bool:
        """Send a plain system alert message."""
        log.info("Sending alert: %s", message)
        return self._send(f"ℹ️ {message}") is not None

    def send_heartbeat(self, price: float, trend: str, rsi: float, funding: float) -> bool:
        """Send hourly status heartbeat."""
        text = (
            f"💓 <b>System Heartbeat</b>\n"
            f"BTC: <b>${price:,.0f}</b>\n"
            f"Trend: {trend} | RSI(1h): {rsi:.1f} | Funding: {funding:+.4f}%\n"
            f"<i>System running normally.</i>"
        )
        return self._send(text) is not None

    def send_stats_report(self, stats: dict) -> bool:
        """Send weekly performance report from the signal journal."""
        if stats.get("total", 0) == 0:
            return self._send(
                f"<b>Weekly Signal Report ({stats.get('days', 7)}d)</b>\n"
                f"No resolved signals in this period."
            ) is not None

        total    = stats["total"]
        wins     = stats["win_full"] + stats["win_partial"]
        win_rate = stats["win_rate"] * 100

        text = (
            f"<b>Weekly Signal Report ({stats['days']}d)</b>\n"
            f"{'─' * 28}\n"
            f"Total signals: <b>{total}</b>\n"
            f"Win rate: <b>{win_rate:.0f}%</b> ({wins}/{total})\n"
            f"  Full wins (TP2):    {stats['win_full']}\n"
            f"  Partial (TP1 only): {stats['win_partial']}\n"
            f"  Losses:             {stats['losses']}\n"
            f"  Expired:            {stats['expired']}\n"
            f"{'─' * 28}\n"
            f"LONG: {stats['long_count']} | SHORT: {stats['short_count']}"
        )
        log.info("Sending weekly stats report to Telegram")
        return self._send(text) is not None

    def test_connection(self) -> bool:
        """Send a test message to verify bot credentials. Returns True if OK."""
        return self._send("🔧 <b>BTC Signal Bot</b> — connection test OK ✅") is not None
