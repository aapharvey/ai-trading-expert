"""
BTC Trading Signal System — Main Entry Point.

Runs continuously, polling Bybit every 60 seconds.
Sends signals and heartbeats to Telegram.
Press Ctrl+C to stop gracefully.
"""

import signal
import sys
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from colorama import Fore, Style, init

import config
from logger import get_logger
from src.bybit_client import BybitClient, BybitAPIError
from src.telegram_notifier import TelegramNotifier
from src.analyzers.price_action import PriceActionAnalyzer
from src.analyzers.technical import TechnicalAnalyzer
from src.analyzers.order_flow import OrderFlowAnalyzer
from src.engine.confluence import ConfluenceEngine

init(autoreset=True)
log = get_logger(__name__)


class TradingBot:
    def __init__(self):
        self.client    = BybitClient()
        self.telegram  = TelegramNotifier()
        self.pa        = PriceActionAnalyzer()
        self.tech      = TechnicalAnalyzer()
        self.of        = OrderFlowAnalyzer()
        self.engine    = ConfluenceEngine()
        self.scheduler = BackgroundScheduler(timezone="UTC")

        # Dashboard state
        self._price:       float = 0.0
        self._trend:       str   = "—"
        self._rsi:         float = 0.0
        self._funding:     float = 0.0
        self._oi_class:    str   = "—"
        self._last_signal: str   = "—"
        self._signals_sent: int  = 0
        self._start_time         = datetime.now(timezone.utc)
        self._running:     bool  = False

    # ─── Main cycle ──────────────────────────────────────────────────────────

    def market_cycle(self) -> None:
        """Called every 60 seconds. Fetch data → analyze → check signals."""
        try:
            # ── Fetch data (staggered to respect rate limits) ─────────────────
            ticker    = self.client.get_ticker()
            time.sleep(0.3)
            klines_1h = self.client.get_klines(interval=config.TIMEFRAMES["1h"])
            time.sleep(0.3)
            klines_4h = self.client.get_klines(interval=config.TIMEFRAMES["4h"])
            time.sleep(0.3)
            oi        = self.client.get_open_interest()
            time.sleep(0.3)
            funding   = self.client.get_funding_rate()

            price = ticker["last_price"]

            # ── Analyze ───────────────────────────────────────────────────────
            pa_result   = self.pa.analyze(klines_4h)
            tech_result = self.tech.analyze(klines_1h)
            of_result   = self.of.analyze(
                oi_history=oi,
                candles=klines_1h,
                funding_history=funding,
                current_price=price,
            )

            # ── Update dashboard state ────────────────────────────────────────
            self._price    = price
            self._trend    = pa_result.trend
            self._rsi      = tech_result.rsi or 0.0
            self._funding  = of_result.funding_rate or 0.0
            self._oi_class = of_result.oi_class or "—"

            # ── Evaluate signals ──────────────────────────────────────────────
            signal = self.engine.evaluate(pa_result, tech_result, of_result)
            if signal:
                sent = self.telegram.send_signal(signal)
                if sent:
                    self._signals_sent += 1
                    direction_str = f"{signal.direction.value} [{signal.strength}/5]"
                    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                    self._last_signal = f"{direction_str} at {ts}"
                    log.info("Signal sent: %s", direction_str)

            self._print_dashboard()

        except BybitAPIError as exc:
            log.error("Bybit API error in market cycle: %s", exc)
        except Exception as exc:
            log.error("Unexpected error in market cycle: %s", exc, exc_info=True)

    def heartbeat(self) -> None:
        """Sent hourly to Telegram to confirm system is alive."""
        try:
            self.telegram.send_heartbeat(
                price=self._price,
                trend=self._trend,
                rsi=self._rsi,
                funding=self._funding,
            )
            log.info("Heartbeat sent")
        except Exception as exc:
            log.error("Heartbeat error: %s", exc)

    # ─── Dashboard ───────────────────────────────────────────────────────────

    def _print_dashboard(self) -> None:
        elapsed = datetime.now(timezone.utc) - self._start_time
        hours, rem = divmod(int(elapsed.total_seconds()), 3600)
        minutes = rem // 60

        price_color = Fore.GREEN if self._trend == "BULLISH" else (
            Fore.RED if self._trend == "BEARISH" else Fore.YELLOW
        )
        rsi_color = Fore.RED if self._rsi > 70 else (
            Fore.GREEN if self._rsi < 30 else Fore.WHITE
        )
        fund_color = Fore.RED if self._funding > 0.05 else (
            Fore.GREEN if self._funding < -0.05 else Fore.WHITE
        )

        line = "=" * 55
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        try:
            print(f"\n{line}")
            print(
                f" BTC/USDT | ${self._price:>10,.2f} | Trend: {self._trend:<8} | {now}"
            )
            print(
                f" RSI: {self._rsi:>5.1f} | Funding: {self._funding:>+.4f}% | OI: {self._oi_class}"
            )
            print(f" Last signal : {self._last_signal}")
            print(f" Signals sent: {self._signals_sent} | Uptime: {hours}h {minutes}m")
            print(line)
        except UnicodeEncodeError:
            # Fallback for terminals with limited encoding
            log.info(
                "BTC $%.2f | Trend:%s RSI:%.1f Fund:%+.4f%% | Signals:%d Up:%dh%dm",
                self._price, self._trend, self._rsi, self._funding,
                self._signals_sent, hours, minutes,
            )

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        log.info("Starting BTC Trading Signal System...")
        self.telegram.send_alert("🟢 <b>BTC Signal System started</b>")

        # Schedule jobs
        self.scheduler.add_job(
            self.market_cycle,
            "interval",
            seconds=config.MARKET_POLL_INTERVAL_SEC,
            id="market_cycle",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.heartbeat,
            "interval",
            minutes=config.HEARTBEAT_INTERVAL_MIN,
            id="heartbeat",
        )

        self.scheduler.start()
        self._running = True

        # Run first cycle immediately
        self.market_cycle()

        log.info("Scheduler running. Press Ctrl+C to stop.")
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        log.info("Shutting down...")
        self._running = False
        self.scheduler.shutdown(wait=False)
        self.telegram.send_alert("🔴 <b>BTC Signal System stopped</b>")
        log.info("System stopped.")


def main() -> None:
    # Validate credentials on startup
    missing = []
    if not config.BYBIT_API_KEY:
        missing.append("BYBIT_API_KEY")
    if not config.TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        log.critical("Missing credentials in .env: %s", ", ".join(missing))
        sys.exit(1)

    bot = TradingBot()

    # Handle SIGTERM (Docker/process managers)
    def handle_sigterm(signum, frame):
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    bot.start()


if __name__ == "__main__":
    main()
