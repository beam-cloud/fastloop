import logging
import sys

_logger = None


class LoggerRedirectHandler(logging.Handler):
    def __init__(self, target_logger):
        super().__init__()
        self.target_logger = target_logger

    def emit(self, record):
        record.name = self.target_logger.name
        self.target_logger.handle(record)


def setup_logger(name: str = "fastloop", level: int = logging.INFO) -> logging.Logger:
    global _logger
    if _logger:
        return _logger

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "[%(asctime)s: %(levelname)s/%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(handler)

    # Redirect uvicorn and fastapi logs
    redirect_handler = LoggerRedirectHandler(logger)
    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
        specific_logger = logging.getLogger(logger_name)
        specific_logger.handlers = [redirect_handler]
        specific_logger.setLevel(level)
        specific_logger.propagate = False

    root_logger = logging.getLogger()
    root_logger.handlers = [redirect_handler]
    root_logger.setLevel(level)
    root_logger.propagate = False

    _logger = logger
    return logger
