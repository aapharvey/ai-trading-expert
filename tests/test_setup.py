"""
TASK-1 tests: project setup, config loading, logger initialization.
"""

import os
import logging
import pytest


class TestConfig:
    def test_env_file_exists(self):
        assert os.path.exists(".env"), ".env file must exist"

    def test_env_not_in_gitignore_tracked(self):
        """Ensure .env is listed in .gitignore."""
        with open(".gitignore") as f:
            content = f.read()
        assert ".env" in content

    def test_config_loads_without_error(self):
        import config
        assert config.SYMBOL == "BTCUSDT"
        assert config.CATEGORY == "linear"

    def test_required_credentials_present(self):
        import config
        assert config.BYBIT_API_KEY, "BYBIT_API_KEY must be set"
        assert config.BYBIT_API_SECRET, "BYBIT_API_SECRET must be set"
        assert config.TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN must be set"
        assert config.TELEGRAM_CHAT_ID, "TELEGRAM_CHAT_ID must be set"

    def test_signal_weights_all_positive(self):
        import config
        for name, weight in config.SIGNAL_WEIGHTS.items():
            assert weight > 0, f"Weight for {name} must be positive"

    def test_min_rr_ratio_valid(self):
        import config
        assert config.MIN_RR_RATIO >= 1.0

    def test_timeframes_defined(self):
        import config
        for tf in ["15m", "1h", "4h", "1d"]:
            assert tf in config.TIMEFRAMES, f"Timeframe {tf} missing"


class TestLogger:
    def test_get_logger_returns_logger(self):
        from logger import get_logger
        log = get_logger("test")
        assert isinstance(log, logging.Logger)

    def test_logger_has_handlers(self):
        from logger import get_logger
        log = get_logger("test_handlers")
        assert len(log.handlers) >= 2  # console + file

    def test_logger_creates_log_dir(self):
        import config
        from logger import get_logger
        get_logger("test_dir")
        assert os.path.exists(config.LOG_DIR)

    def test_logger_does_not_duplicate_handlers(self):
        from logger import get_logger
        log1 = get_logger("same_name")
        log2 = get_logger("same_name")
        assert log1 is log2
        assert len(log1.handlers) == len(log2.handlers)

    def test_logger_writes_message(self, tmp_path, mocker):
        from logger import get_logger
        log = get_logger("test_write_isolated")
        # Logger has propagate=False; verify it actually calls handlers
        mock_handler = mocker.MagicMock(spec=logging.Handler)
        mock_handler.level = logging.DEBUG
        mock_handler.filters = []
        log.addHandler(mock_handler)
        log.info("TASK-1 logger test passed")
        mock_handler.handle.assert_called_once()
        log.removeHandler(mock_handler)


class TestProjectStructure:
    REQUIRED_FILES = [
        "config.py",
        "logger.py",
        "requirements.txt",
        "README.md",
        ".gitignore",
        ".env.example",
        "src/__init__.py",
        "src/analyzers/__init__.py",
        "src/engine/__init__.py",
        "src/models/__init__.py",
        "tests/__init__.py",
    ]

    @pytest.mark.parametrize("filepath", REQUIRED_FILES)
    def test_required_file_exists(self, filepath):
        assert os.path.exists(filepath), f"Missing required file: {filepath}"
