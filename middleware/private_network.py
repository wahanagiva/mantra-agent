"""
Chrome Private Network Access (PNA) preflight handler.

Chrome 94+ blocks fetches from public HTTPS sites (like https://mantra.majutrah.co.id)
to private IPs (like 127.0.0.1) unless the target server explicitly opts in via
CORS preflight headers.

Reference: https://developer.chrome.com/blog/private-network-access-update/

When the browser sends a preflight OPTIONS with:
    Access-Control-Request-Private-Network: true

The server must respond with:
    Access-Control-Allow-Private-Network: true
    Access-Control-Allow-Origin: <matching origin>

Starlette's built-in CORSMiddleware does NOT emit this header, so we add it via a
lightweight middleware that runs AFTER CORSMiddleware.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """
    Adds `Access-Control-Allow-Private-Network: true` to OPTIONS responses
    when the browser requested PNA, so Chrome allows the cross-origin fetch
    to 127.0.0.1.

    Safe by default — only fires on OPTIONS with the specific request header,
    and matches the Origin header already validated by CORSMiddleware.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if (
            request.method == "OPTIONS"
            and request.headers.get("access-control-request-private-network", "").lower() == "true"
            and response.headers.get("Access-Control-Allow-Origin")
        ):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response
