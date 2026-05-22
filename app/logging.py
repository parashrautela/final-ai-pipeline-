import logging
import sys
from datetime import datetime

from app.config import settings

class ReadableFormatter(logging.Formatter):
    def format(self, record):
        ts = datetime.utcnow().strftime("%H:%M:%S")

        # Pick up optional context fields injected via extra={}
        ctx_parts = []
        for field in ("product_id", "job_id"):
            val = getattr(record, field, None)
            if val:
                ctx_parts.append(f"{field}={val}")
        ctx = f"  [{', '.join(ctx_parts)}]" if ctx_parts else ""

        line = f"{ts}  {record.levelname:<7}  {record.getMessage()}{ctx}"

        # Append traceback onl
        #  for — keeps INFO lines clean
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


def setup_logging():
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ReadableFormatter())
    root.handlers = [handler]

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger(__name__)
