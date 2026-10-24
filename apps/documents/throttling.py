"""
Throttling for the internal IC/passport endpoints.

Every request arrives from the BFF host, so throttling on REMOTE_ADDR
alone would rate-limit all users as one bucket. The scoped throttle
keys on the caller's user id (header or request body) and falls back
to the forwarded client IP.

Scopes are resolved per request by route (P2 split):
  - documents_encrypt / encrypt-ic → ic_doc_upload  (strict 10/h — real
    OCR + encryption executions; failed uploads consume quota: an
    acceptable anti-abuse compromise, DRF throttles run pre-view)
  - documents_delete → ic_doc_delete (30/h — deletes must not burn the
    upload budget; E2E pain: delete 429 while testing)
"""

from rest_framework.throttling import SimpleRateThrottle

_SCOPE_BY_URL_NAME = {
    "documents_delete": "ic_doc_delete",
    "documents_encrypt": "ic_doc_upload",
    "documents_encrypt_ic": "ic_doc_upload",
}
_DEFAULT_SCOPE = "ic_doc_upload"


class UserScopedRateThrottle(SimpleRateThrottle):
    scope = _DEFAULT_SCOPE  # default; re-resolved per request in get_cache_key

    def get_cache_key(self, request, view):
        # Resolve the scope (and its rate) from the matched route so
        # deletes never consume the upload budget. Falls back to the
        # strict upload scope for unknown routes.
        match = getattr(request, "resolver_match", None)
        url_name = getattr(match, "url_name", None) if match else None
        self.scope = _SCOPE_BY_URL_NAME.get(url_name, _DEFAULT_SCOPE)
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

        # Key on the body user_id first: that is the identity the upload is
        # stored under. Header X-User-Id is only a fallback — otherwise a
        # caller could rotate headers to dodge the per-user bucket.
        from rest_framework.exceptions import ParseError
        try:
            # Body may not be a mapping (JSON array/null, scalar) — `.get`
            # on it raises AttributeError, not ParseError → 500.
            data = request.data if isinstance(request.data, dict) else {}
            user_id = data.get("user_id")
        except ParseError:
            user_id = None
        if not user_id:
            user_id = request.headers.get("X-User-Id")
        if isinstance(user_id, str) and user_id.strip():
            ident = user_id.strip()
        else:
            ident = (
                request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.META.get("REMOTE_ADDR", "")
            )
        return self.cache_format % {"scope": self.scope, "ident": ident}
