"""
Centralized logger — writes to both console (colored) and rotating log file.
Import and use: from logger import get_logger; log = get_logger(__name__)
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from colorama import Fore, Style, init

import config

init(autoreset=True)


class ColoredFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    Fore.CYAN,
        logging.INFO:     Fore.GREEN,
        logging.WARNING:  Fore.YELLOW,
        logging.ERROR:    Fore.RED,
        logging.CRITICAL: Fore.MAGENTA,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        formatted = super().format(record)
        return f"{color}{formatted}{Style.RESET_ALL}"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler (colored)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(ColoredFormatter(fmt=fmt, datefmt=datefmt))
    logger.addHandler(console_handler)

    # File handler (rotating, max 5 MB × 3 backups)
    os.makedirs(config.LOG_DIR, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger
