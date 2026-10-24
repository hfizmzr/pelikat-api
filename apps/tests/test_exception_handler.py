"""Regression tests for the global DRF exception handler (pelikat.exceptions).

PR #9 shipped the JSON-500 guarantee; the retroactive merge audit found the
handler crashed with AttributeError when DRF returns a LIST payload
(e.g. array-style validation errors), re-raising as a Django HTML 500 and
defeating the guarantee. Red-first coverage for the list-payload branch.
"""

from rest_framework.exceptions import ValidationError

from pelikat.exceptions import exception_handler


class TestListPayload:
    def test_list_validation_errors_normalized_to_error_key(self):
        # DRF renders ValidationError(list) as a list payload — the handler
        # must wrap it as {"error": [...]}, not crash on .get("detail").
        response = exception_handler(ValidationError(["bad input"]), {})
        assert response is not None
        assert response.status_code == 400
        assert response.data == {"error": ["bad input"]}

    def test_list_payload_is_json_serializable(self):
        response = exception_handler(ValidationError(["bad input"]), {})
        assert response.data == {"error": ["bad input"]}
        # Would raise AttributeError before the fix.
        assert list(response.data.keys()) == ["error"]


class TestDictPayloadStillNormalized:
    def test_detail_renamed_to_error(self):
        response = exception_handler(ValidationError({"detail": "nope"}), {})
        assert response is not None
        assert response.status_code == 400
        assert response.data["error"] == "nope"

    def test_existing_error_key_not_overwritten(self):
        response = exception_handler(ValidationError({"error": "kept"}), {})
        assert response is not None
        assert response.data["error"] == "kept"
