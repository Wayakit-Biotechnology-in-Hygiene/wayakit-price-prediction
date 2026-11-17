# scraper/log_config.py
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs') # logs directory at the project root
LOG_FILENAME = os.path.join(LOG_DIR, 'wayakit_app.log')

def setup_logging(log_level=logging.INFO):
    """Configures logging for the application."""

    # Create the logs directory if it does not exist
    os.makedirs(LOG_DIR, exist_ok=True)

    # Create the main logger
    logger = logging.getLogger('wayakit_price_prediction') # Unique name for your logger
    logger.setLevel(log_level) # Minimum severity level to log

    # Avoid duplicate handlers if the function is called multiple times
    if logger.hasHandlers():
        logger.handlers.clear()

    # Format of the log messages
    log_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 1. Handler to write to a file (with rotation)
    # The file is rotated when it reaches 5MB, keeping 3 backup files.
    file_handler = RotatingFileHandler(
        LOG_FILENAME, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
    )
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)

    # 2. Handler to write to the console (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_format)
    logger.addHandler(console_handler)

    return logger

# Configure the logger when the module is imported
logger = setup_logging(log_level=logging.DEBUG)

# You can add a simple function to get the configured logger from other modules
def get_logger():
  """Returns the configured logger instance."""
  return logging.getLogger('wayakit_price_prediction')