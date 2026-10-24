"""
Global DRF exception handler.

Guarantee (P1 #3): every exception escaping a DRF view — APIException or
an arbitrary unhandled Exception — is rendered as JSON. Without this, an
unhandled Exception is re-raised by DRF's default handler and Django's
ExceptionMiddleware leaks a plain-HTML debug 500 page to BFF callers.
"""

import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        # Normalize DRF's {"detail": ...} to our {"error": ...} contract.
        detail = response.data.get("detail")
        if detail is not None and "error" not in response.data:
            response.data["error"] = detail
        return response
    logger.exception("unhandled exception in DRF view")
    # No stack/internal text echoed: generic message only.
    return Response({"error": "Internal server error"}, status=500)
