"""
Global crash handler.

Catches any unhandled exception in handlers, writes a dedicated crash report
file (one per request), and returns a user-friendly 500 response. The
`request_id` can be used to find the crash report on disk.
"""
import logging
import sys

from fastapi import Request
from fastapi.responses import JSONResponse

from utils.logging_setup import write_crash_report

logger = logging.getLogger("mantra.agent.crash")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Called by FastAPI for any exception not caught by a per-handler try/except.

    Writes a crash report file with the full traceback and returns a JSON 500
    that includes the request_id so the user can reference it.
    """
    request_id = getattr(request.state, "request_id", "unknown")

    # Write crash report to disk
    try:
        report_path = write_crash_report(
            request_id,
            sys.exc_info(),
            context={
                "method": request.method,
                "path": str(request.url.path),
                "client": request.client.host if request.client else "-",
            },
        )
        logger.error(
            "Unhandled exception in %s %s - crash report: %s",
            request.method, request.url.path, report_path,
        )
    except Exception as e:
        logger.error("Unhandled + failed to write crash report: %s", e)

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": f"Internal server error (see agent logs). request_id={request_id}",
            "request_id": request_id,
        },
    )


def register(app) -> None:
    """Wire the handler. Call from main.py once after app creation."""
    app.add_exception_handler(Exception, unhandled_exception_handler)
