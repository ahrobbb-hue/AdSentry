"""
AdSentry Logger Utility
Provides styled, consistent, and timestamped console and file logging.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

try:
    import colorlog

    HAS_COLORLOG = True
except ImportError:
    HAS_COLORLOG = False


def setup_logger(name: str = "AdSentry", level: str = "INFO") -> logging.Logger:
    """Configures and returns a styled logger instance."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers if setup_logger is called repeatedly
    if logger.handlers:
        return logger

    # Ensure logs directory exists
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "adsentry.log"

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    if HAS_COLORLOG:
        color_formatter = colorlog.ColoredFormatter(
            "%(log_color)s[%(asctime)s] [%(levelname)-8s]%(reset)s %(blue)s(%(name)s)%(reset)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
        console_handler.setFormatter(color_formatter)
    else:
        plain_formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)-8s] (%(name)s): %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(plain_formatter)

    logger.addHandler(console_handler)

    # File Handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] (%(name)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger
