"""
Telegram notification module.
Sends trading signals, system alerts, and heartbeats to a Telegram chat.
Uses the Telegram Bot API directly via requests (no async dependency).
"""

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

    def _send(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Send a text message. Returns True on success, False on any error.
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
        try:
            resp = self._session.post(url, json=payload, timeout=_SEND_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                raise TelegramError(body.get("description", "unknown error"))
            log.debug("Telegram message sent (chat=%s)", self._chat_id)
            return True
        except TelegramError as exc:
            log.error("Telegram API error: %s", exc)
        except requests.RequestException as exc:
            log.error("Telegram network error: %s", exc)
        return False

    # ─── Public API ──────────────────────────────────────────────────────────

    def send_signal(self, signal: TradeSignal) -> bool:
        """Format and send a trading signal message."""
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

    def send_alert(self, message: str) -> bool:
        """Send a plain system alert message."""
        log.info("Sending alert: %s", message)
        return self._send(f"ℹ️ {message}")

    def send_heartbeat(self, price: float, trend: str, rsi: float, funding: float) -> bool:
        """Send hourly status heartbeat."""
        text = (
            f"💓 <b>System Heartbeat</b>\n"
            f"BTC: <b>${price:,.0f}</b>\n"
            f"Trend: {trend} | RSI(1h): {rsi:.1f} | Funding: {funding:+.4f}%\n"
            f"<i>System running normally.</i>"
        )
        return self._send(text)

    def test_connection(self) -> bool:
        """Send a test message to verify bot credentials. Returns True if OK."""
        return self._send("🔧 <b>BTC Signal Bot</b> — connection test OK ✅")
