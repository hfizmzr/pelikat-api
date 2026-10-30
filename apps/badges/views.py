"""
Badge Views
"""

import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import evaluate_badges, get_badge_definitions

logger = logging.getLogger(__name__)


@api_view(["POST"])
def evaluate_badges_view(request):
    """
    POST /ai/badges/evaluate

    Request: { "runner_id": "uuid", "event_id": "uuid | null" }
    Response: { "awarded": [{ badge_key, name, description, icon }] }
    """
    runner_id = request.data.get("runner_id")
    event_id = request.data.get("event_id")

    if not runner_id:
        return Response({"error": "runner_id is required"}, status=400)

    try:
        result = evaluate_badges(runner_id, event_id or None)
        return Response(result)
    except Exception:
        logger.exception("badge evaluation failed for runner %r", runner_id)
        return Response({"error": "internal error"}, status=500)


@api_view(["GET"])
def badge_definitions_view(request):
    """
    GET /ai/badges/definitions

    Returns the full list of badge definitions (key, name, description, icon).
    """
    return Response({"badges": get_badge_definitions()})
