"""SSRF allowlisted theme asset fetching (spec §7 — SSRF allowlist v2).

Theme asset URLs are untrusted decoration coming from the web BFF, so the
validator is prefix-gated with byte-exact allowlisting and fail-closed
semantics; the fetcher is a streaming GET with a running byte cap so no
asset can exhaust memory or stall the certificate pipeline.
"""

import time
from urllib.parse import unquote, urlparse

import httpx
from django.conf import settings

# Spec stream caps (spec §6/§13): certificate_background <= 8 MB,
# logo/badge <= 1 MB. (The storage bucket's 10 MB cap is a pelikat-web
# concern and is deliberately not mirrored here.)
MAX_BG_BYTES = 8_388_608
MAX_SMALL_ASSET_BYTES = 1_048_576

_STREAM_TIMEOUT = httpx.Timeout(10.0, read=20.0)

# httpx read timeout bounds the wait for ONE chunk, not the whole response:
# enforce an overall wall-clock deadline so a trickling stream cannot hang
# the certificate pipeline (review focus: no hang).
_TOTAL_DEADLINE_SECONDS = 60.0


class ThemeAssetError(Exception):
    """A theme asset URL was rejected or its fetch failed.

    The message is staged for the warnings channel: callers must log it and
    fall back per the never-fail contract (spec §7), never propagate.
    """


def _validate(url: object) -> str:
    # Empty/unset SUPABASE_URL fails closed: nothing is ever allowed.
    if not settings.SUPABASE_URL:
        raise ThemeAssetError('theme asset url rejected: SUPABASE_URL unset')

    prefix = (
        f"{settings.SUPABASE_URL.rstrip('/')}"
        '/storage/v1/object/public/event-themes/'
    )
    if not isinstance(url, str) or not url.startswith('https://'):
        raise ThemeAssetError('theme asset url rejected: not a https string url')
    if any(not c.isprintable() or c.isspace() for c in url):
        raise ThemeAssetError(
            'theme asset url rejected: whitespace or control character'
        )
    if not url.startswith(prefix):
        raise ThemeAssetError('theme asset url rejected: outside allowlist prefix')

    lowered = url.lower()
    decoded = unquote(url)
    # Belt (fail-closed): a second decode pass so `%252e%252e` (which one
    # decode turns into literal `%2e%2e`) cannot slip past. Trade-off: a
    # legit key containing a literal `%2e` in its filename after one decode
    # is rejected too — acceptable, allowlist is decoration-only.
    decoded_twice = unquote(decoded)
    if (
        '..' in url or '..' in decoded or '..' in decoded_twice
        or '%2e%2e' in lowered or '%2f..' in lowered
    ):
        raise ThemeAssetError('theme asset url rejected: traversal sequence')

    if url == prefix:
        raise ThemeAssetError('theme asset url rejected: bare bucket base')
    parsed = urlparse(url)
    if '@' in parsed.netloc:
        raise ThemeAssetError('theme asset url rejected: userinfo')
    if parsed.query or parsed.fragment:
        raise ThemeAssetError('theme asset url rejected: query or fragment')
    return url


def validate_allowed_asset_url(url: object) -> str:
    """Return `url` unchanged when it is SSRF-allowlisted, else raise ThemeAssetError.

    Allowlist rules (spec §7):
    - https only;
    - must begin with the byte-exact
      `SUPABASE_URL + "/storage/v1/object/public/event-themes/"` prefix
      (no other host, no other bucket);
    - no `..` raw or percent-encoded (`%2e%2e`, `%2f..`, case-insensitive),
      and the percent-decoded form — including a second decode pass so
      `%252e%252e` is caught — must not contain `..`;
    - no userinfo (`@` in netloc), no bare bucket base, no query/fragment.

    An empty/unset SUPABASE_URL fails closed: every url is rejected.
    """
    return _validate(url)


def fetch_theme_asset(
    url: str, *, max_bytes: int, deadline: float | None = None,
    stream_factory=httpx.stream
) -> bytes:
    """Fetch a validated theme asset subject to `max_bytes`, or raise.

    Streams the GET with `timeout=httpx.Timeout(10.0, read=20.0)` and
    `follow_redirects=False`, plus an overall wall-clock deadline; any non-200
    reply, content-encoded response, declared/actual size over `max_bytes`,
    connection error or timeout aborts the stream and raises ThemeAssetError
    (which also covers the validation path). No content-type check: the
    storage bucket already enforces asset types.

    `deadline` (PR12 review): optional absolute `time.monotonic()` deadline
    shared by the whole certificate render. The fetch stops at
    min(its own 60s budget, remaining time), so several sequential assets can
    never stretch the request past the caller's overall limit.
    """
    _validate(url)
    # Overall deadline starts before the blocking header wait; each blocking
    # operation is still bounded by httpx connect/read timeouts, so the worst
    # case is deadline + one read timeout, never an unbounded hang.
    now = time.monotonic()
    if deadline is not None:
        remaining = deadline - now
        if remaining <= 0:
            raise ThemeAssetError(
                'theme asset fetch failed: total deadline exceeded'
            )
        deadline = now + min(_TOTAL_DEADLINE_SECONDS, remaining)
    else:
        deadline = now + _TOTAL_DEADLINE_SECONDS
    try:
        with stream_factory(
            'GET', url, timeout=_STREAM_TIMEOUT, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise ThemeAssetError(
                    f'theme asset fetch failed: HTTP {response.status_code}'
                )
            encoding = response.headers.get('Content-Encoding')
            if encoding:
                # httpx decodes before iter_bytes() yields, which would let a
                # small compressed body expand past the cap (decompression
                # bomb); assets are already-compressed PNG/JPEG, so refuse.
                raise ThemeAssetError(
                    f'theme asset fetch failed: encoded response ({encoding})'
                )
            try:
                declared = int(response.headers.get('Content-Length'))
            except (TypeError, ValueError):
                declared = None  # garbage header: fall back to the running cap
            if declared is not None and declared > max_bytes:
                raise ThemeAssetError(
                    f'theme asset fetch failed: exceeds {max_bytes} byte cap'
                )
            if time.monotonic() > deadline:
                raise ThemeAssetError(
                    'theme asset fetch failed: total deadline exceeded'
                )
            total = 0
            parts = []
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ThemeAssetError(
                        f'theme asset fetch failed: exceeds {max_bytes} byte cap'
                    )
                if time.monotonic() > deadline:
                    raise ThemeAssetError(
                        'theme asset fetch failed: total deadline exceeded'
                    )
                parts.append(chunk)
            return b''.join(parts)
    # StreamError (RuntimeError-based) and InvalidURL (plain Exception)
    # escape httpx.HTTPError, so they are named explicitly here.
    except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL) as exc:
        raise ThemeAssetError(
            f'theme asset fetch failed: {type(exc).__name__}: {exc}'
        ) from exc
