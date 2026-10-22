"""
Throttling for the internal IC/passport endpoints.

Every request arrives from the BFF host, so throttling on REMOTE_ADDR
alone would rate-limit all users as one bucket. The scoped throttle
keys on the caller's user id (header or request body) and falls back
to the forwarded client IP.
"""

from rest_framework.throttling import SimpleRateThrottle


class UserScopedRateThrottle(SimpleRateThrottle):
    scope = "ic_documents"  # rate from REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']

    def get_cache_key(self, request, view):
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
