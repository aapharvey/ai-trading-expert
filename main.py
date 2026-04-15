"""
BTC Trading Signal System — Main Entry Point.

Runs continuously, polling Bybit every 60 seconds.
Phase 4: Entry Zone Guard + Liquidity Map (Block 7) + Volume Profile (Block 8).
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
from src.analyzers.sentiment import SentimentAnalyzer
from src.analyzers.on_chain import OnChainAnalyzer
from src.engine.confluence import ConfluenceEngine
from src.models.signals import SentimentResult, OnChainResult
from src.journal.signal_journal import SignalJournal
from src.journal.outcome_checker import OutcomeChecker

init(autoreset=True)
log = get_logger(__name__)


class TradingBot:
    def __init__(self):
        self.client    = BybitClient()
        self.telegram  = TelegramNotifier()
        self.pa        = PriceActionAnalyzer()
        self.tech      = TechnicalAnalyzer()
        self.of        = OrderFlowAnalyzer()
        self.sentiment = SentimentAnalyzer()   # Phase 2: F&G + news (cached internally)
        self.on_chain  = OnChainAnalyzer()     # Phase 2: CoinMetrics (cached internally)
        self.engine    = ConfluenceEngine()
        self.journal   = SignalJournal()       # Phase 3: paper trading log
        self.checker   = OutcomeChecker(self.journal, self.client, self.telegram)  # Phase 3: auto-verify
        self.scheduler = BackgroundScheduler(timezone="UTC")

        # Dashboard state
        self._price:        float = 0.0
        self._trend:        str   = "-"
        self._rsi:          float = 0.0
        self._funding:      float = 0.0
        self._oi_class:     str   = "-"
        self._fear_greed:   str   = "-"
        self._last_signal:  str   = "-"
        self._signals_sent: int   = 0
        self._start_time          = datetime.now(timezone.utc)
        self._running:      bool  = False

    # ─── Main market cycle (every 60s) ───────────────────────────────────────

    def market_cycle(self) -> None:
        """Fetch market data → analyze → check signals → update dashboard."""
        _cycle_start = time.monotonic()
        try:
            # Fetch market data (staggered 0.3s to respect rate limits)
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

            # Analyze (Phase 1 blocks)
            pa_result   = self.pa.analyze(klines_4h)
            tech_result = self.tech.analyze(klines_1h)
            of_result   = self.of.analyze(
                oi_history=oi,
                candles=klines_1h,
                funding_history=funding,
                current_price=price,
            )

            # Phase 2 blocks (use cached results — updated by their own schedules)
            sent_result = self.sentiment.analyze()
            oc_result   = self.on_chain.analyze()

            # Update dashboard state
            self._price      = price
            self._trend      = pa_result.trend
            self._rsi        = tech_result.rsi or 0.0
            self._funding    = of_result.funding_rate or 0.0
            self._oi_class   = of_result.oi_class or "-"
            self._fear_greed = (
                f"{sent_result.fear_greed_value} ({sent_result.fear_greed_label})"
                if sent_result.fear_greed_value is not None
                else "-"
            )

            # Evaluate confluence (all 5 blocks)
            sig = self.engine.evaluate(pa_result, tech_result, of_result, sent_result, oc_result)
            if sig:
                message_id = self.telegram.send_signal(sig)
                if message_id:
                    self._signals_sent += 1
                    direction_str = f"{sig.direction.value} [{sig.strength}/5]"
                    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                    self._last_signal = f"{direction_str} at {ts}"
                    log.info("Signal sent: %s", direction_str)
                    self.journal.record(sig, telegram_message_id=message_id)

            self._print_dashboard()

            _elapsed = time.monotonic() - _cycle_start
            if _elapsed > 50:
                log.warning("Market cycle took %.1fs — approaching scheduler limit", _elapsed)
            else:
                log.debug("Market cycle completed in %.1fs", _elapsed)

        except BybitAPIError as exc:
            log.error("Bybit API error in market cycle: %s", exc)
        except Exception as exc:
            log.error("Unexpected error in market cycle: %s", exc, exc_info=True)

    def check_outcomes(self) -> None:
        """Phase 3: resolve pending signal outcomes (runs every 30 min)."""
        try:
            self.checker.check_pending()
        except Exception as exc:
            log.error("Outcome checker error: %s", exc)

    def weekly_report(self) -> None:
        """Phase 3: send weekly performance stats to Telegram (Sundays 09:00 UTC)."""
        try:
            stats = self.journal.get_stats(days=7)
            self.telegram.send_stats_report(stats)
        except Exception as exc:
            log.error("Weekly report error: %s", exc)

    def heartbeat(self) -> None:
        """Hourly Telegram heartbeat."""
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
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        line = "=" * 60

        try:
            print(f"\n{line}")
            print(f" BTC/USDT | ${self._price:>10,.2f} | Trend: {self._trend:<8} | {now}")
            print(f" RSI: {self._rsi:>5.1f} | Funding: {self._funding:>+.4f}% | OI: {self._oi_class}")
            print(f" Fear & Greed: {self._fear_greed}")
            print(f" Last signal : {self._last_signal}")
            print(f" Signals sent: {self._signals_sent} | Uptime: {hours}h {minutes}m")
            print(line)
        except UnicodeEncodeError:
            log.info(
                "BTC $%.2f | %s RSI:%.1f Fund:%+.4f%% F&G:%s | Signals:%d Up:%dh%dm",
                self._price, self._trend, self._rsi, self._funding,
                self._fear_greed, self._signals_sent, hours, minutes,
            )

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        log.info("Starting BTC Trading Signal System (Phase 4)...")

        # Log module status
        if config.CRYPTOPANIC_API_KEY:
            log.info("CryptoPanic news: ENABLED")
        else:
            log.info("CryptoPanic news: DISABLED (no API key)")

        log.info("CoinMetrics on-chain: ENABLED (free, no key required)")

        self.telegram.send_alert(
            "🟢 <b>BTC Signal System started (Phase 4)</b>\n"
            f"Fear&Greed: on | "
            f"News: {'on' if config.CRYPTOPANIC_API_KEY else 'off'} | "
            f"On-chain: on | Liquidity: on | VolumeProfile: on"
        )

        # Schedule jobs
        self.scheduler.add_job(
            self.market_cycle, "interval",
            seconds=config.MARKET_POLL_INTERVAL_SEC,
            id="market_cycle", max_instances=1, coalesce=True,
        )
        self.scheduler.add_job(
            self.heartbeat, "interval",
            minutes=config.HEARTBEAT_INTERVAL_MIN,
            id="heartbeat",
        )
        self.scheduler.add_job(
            self.check_outcomes, "interval",
            minutes=30,
            id="outcome_checker",
        )
        self.scheduler.add_job(
            self.weekly_report, "cron",
            day_of_week="sun", hour=9, minute=0,
            id="weekly_report",
        )

        self.scheduler.start()
        self._running = True

        # First cycle immediately
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
    missing = [
        k for k in ("BYBIT_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not getattr(config, k, "")
    ]
    if missing:
        log.critical("Missing credentials in .env: %s", ", ".join(missing))
        sys.exit(1)

    bot = TradingBot()

    def handle_sigterm(signum, frame):
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    bot.start()


if __name__ == "__main__":
    main()
