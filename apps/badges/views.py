"""
Badge Views
"""

import logging
import re

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import evaluate_badges, get_badge_definitions

logger = logging.getLogger(__name__)

# PostgreSQL (and thus PostgREST) accepts both the canonical hyphenated
# 8-4-4-4-12 UUID layout and the unhyphenated 32-hex form. Python's
# uuid.UUID() strips hyphens before parsing, so a value like
# '0000000-10000-...' would pass a bare try/UUID gate yet still trigger
# 22P02 (-> 500) downstream. Accept exactly those two forms and reject
# everything else (e.g. misplaced hyphens, urn:uuid: prefixes).
_UUID_RE = re.compile(
    r'(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    r'|[0-9a-fA-F]{32})'
)


def _is_valid_uuid(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))



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
