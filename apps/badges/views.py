"""
Badge Views
"""

import logging
import uuid as uuid_lib

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import evaluate_badges, get_badge_definitions

logger = logging.getLogger(__name__)


def _is_valid_uuid(value) -> bool:
    if not isinstance(value, str):
        return False
    # PostgreSQL uuid input has no colon form, but Python's UUID() strips
    # urn:/uuid: prefixes — those would surface later as 22P02 -> 500.
    if ':' in value:
        return False
    try:
        uuid_lib.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True



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

    # runner_id/event_id map to UUID columns in Supabase (run_logs,
    # runner_badges, ...). PostgREST rejects non-UUID ids with 22P02
    # ("invalid input syntax for type uuid"), which used to surface as a
    # 500 "internal error". Reject malformed ids up-front as client errors.
    if not _is_valid_uuid(runner_id):
        return Response({"error": "runner_id must be a valid UUID"}, status=400)

    if event_id and not _is_valid_uuid(event_id):
        return Response({"error": "event_id must be a valid UUID"}, status=400)

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
