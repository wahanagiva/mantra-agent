"""
Access-log middleware.

Emits one line per request to the `mantra.agent.access` logger (configured
in utils/logging_setup.py to go to access.log).

Format:
    <ip> <method> <path> <status> <duration_ms> <request_id>
"""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

access_logger = logging.getLogger("mantra.agent.access")


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate a short request ID (first 8 chars of uuid4)
        request_id = uuid.uuid4().hex[:8]
        request.state.request_id = request_id  # accessible in handlers

        start = time.perf_counter()
        client_ip = request.client.host if request.client else "-"

        try:
            response = await call_next(request)
        except Exception:
            # Let crash_handler.py deal with it - but still log the attempt
            elapsed_ms = (time.perf_counter() - start) * 1000
            access_logger.info(
                "%s %s %s 500 %.1fms req=%s EXC",
                client_ip, request.method, request.url.path, elapsed_ms, request_id,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000
        # Attach request ID to response headers so clients can reference in bug reports
        response.headers["X-Request-ID"] = request_id

        access_logger.info(
            "%s %s %s %d %.1fms req=%s",
            client_ip, request.method, request.url.path,
            response.status_code, elapsed_ms, request_id,
        )
        return response
