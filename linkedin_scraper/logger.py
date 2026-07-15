import os
from pathlib import Path

from loguru import logger

from linkedin_scraper.console import console
from linkedin_scraper.constants import LOGS_PATH

LOG_DIR_ENV = "LINKEDIN_SCRAPER_LOG_DIR"  # override the log directory; tests point it at a tmp dir
LOG_FILE_NAME = "{time:YYYY-MM-DD}.log"  # one file per day
LOG_ROTATION = "00:00"  # start a new file at midnight
LOG_RETENTION = "10 days"  # delete a day's file after this long
LOG_LEVEL_CONSOLE = "INFO"  # what shows in your terminal
LOG_LEVEL_FILE = "DEBUG"  # what gets written to the log file
LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:^8}</level> | {message}"


def init_logging() -> None:
    """Point loguru at the console and a dated log file."""
    log_dir = Path(os.environ.get(LOG_DIR_ENV, LOGS_PATH))
    logger.remove()  # drop loguru's default handler so nothing is logged twice
    # Route console logs through the Rich console so a live spinner and log lines never fight.
    logger.add(
        lambda m: console.print(m, end="", markup=False, highlight=False, soft_wrap=True),
        format=LOG_FORMAT,
        level=LOG_LEVEL_CONSOLE,
        colorize=False,
    )
    logger.add(
        str(log_dir / LOG_FILE_NAME),  # loguru creates the parent directory itself
        format=LOG_FORMAT,
        level=LOG_LEVEL_FILE,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        encoding="utf-8",
    )
