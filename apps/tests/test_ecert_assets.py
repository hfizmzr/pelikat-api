"""Tests for the SSRF-allowlisted theme asset fetcher (spec §7, plan Task 2).

No test here touches the network: the stream factory is injected, so
httpx.stream is never reached and every vector is in-process.
"""

import httpx
import pytest
from django.conf import settings

from apps.ecert import assets as assets_module
from apps.ecert.assets import (
    MAX_BG_BYTES,
    MAX_SMALL_ASSET_BYTES,
    ThemeAssetError,
    fetch_theme_asset,
    validate_allowed_asset_url,
)

PREFIX = 'https://test.supabase.co/storage/v1/object/public/event-themes/'

CHUNKS_TO_TIMEOUT = httpx.Timeout(10.0, read=20.0)


# ---------------------------------------------------------------------------
# fake stream plumbing
# ---------------------------------------------------------------------------

class FakeHeaders:
    """Minimal stand-in for the subset of httpx.Headers.get the fetcher uses."""

    def __init__(self, items=None):
        self._items = items or {}

    def get(self, name, default=None):
        return self._items.get(name, default)


class FakeResponse:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = headers if headers is not None else FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield from self._chunks


def fake_stream(chunks=(), status=200, headers=None):
    """When the stream factory is called, it records the call and returns
    a fake response."""
    calls = []

    def factory(method, url, **kwargs):
        calls.append({'method': method, 'url': url, 'kwargs': kwargs})
        return FakeResponse(status, chunks, headers=headers)

    factory.calls = calls
    return factory


# ---------------------------------------------------------------------------
# validate_allowed_asset_url — accept matrix
# ---------------------------------------------------------------------------

def test_exact_prefix_accepts_and_returns_url_unchanged():
    url = PREFIX + 'backgrounds/abc.png'
    assert validate_allowed_asset_url(url) == url


def test_exact_prefix_nested_path_accepts():
    url = PREFIX + 'asset-c220f/logo.png'
    assert validate_allowed_asset_url(url) == url


def test_supabase_url_override_moves_the_allowlist(monkeypatch):
    monkeypatch.setattr(settings, 'SUPABASE_URL', 'https://other.supabase.co')
    accepted = (
        'https://other.supabase.co/storage/v1/object/public/event-themes/a.png'
    )
    rejected = PREFIX + 'a.png'
    assert validate_allowed_asset_url(accepted) == accepted
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(rejected)


# ---------------------------------------------------------------------------
# validate_allowed_asset_url — reject matrix
# ---------------------------------------------------------------------------

def test_same_supabase_different_bucket_rejects():
    evil = 'https://test.supabase.co/storage/v1/object/public/event-themes2/a.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(evil)


def test_raw_traversal_path_rejects():
    url = PREFIX + '../storage/v1/../service_role'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_encoded_dot_dot_rejects():
    url = PREFIX + '%2e%2e/certs/reg.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_double_encoded_dot_dot_rejects():
    """Belt: one decode turns %252e%252e into literal '%2e%2e'; only the
    second decode reveals the traversal, so it must be rejected too."""
    url = PREFIX + '%252e%252e/certs/reg.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_encoded_slash_dot_dot_rejects():
    url = PREFIX + 'a%2F..%2F/private'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_case_mixed_prefix_rejects():
    url = PREFIX.replace('https', 'HTTPS') + 'a.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_host_swap_rejects():
    evil = 'https://evil.com/storage/v1/object/public/event-themes/a.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(evil)


def test_prefix_lookalike_host_rejects():
    evil = 'https://evil.supabase.co/storage/v1/object/public/event-themes/a.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(evil)


def test_non_https_rejects():
    url = 'http://test.supabase.co/storage/v1/object/public/event-themes/a.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_userinfo_rejects():
    evil = 'https://user@test.supabase.co/storage/v1/object/public/event-themes/a.png'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(evil)


def test_query_string_rejects():
    url = PREFIX + 'a.png?width=100'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_fragment_rejects():
    url = PREFIX + 'a.png#seed'
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(url)


def test_bare_bucket_base_rejects():
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(PREFIX)


def test_non_string_rejects():
    for bad in (None, 123, object(), ['x']):
        with pytest.raises(ThemeAssetError):
            validate_allowed_asset_url(bad)


def test_whitespace_or_control_character_rejects():
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(PREFIX + 'a\nb')
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(PREFIX + 'a b.png')


def test_empty_supabase_url_rejects_everything(monkeypatch):
    monkeypatch.setattr(settings, 'SUPABASE_URL', '')
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url(PREFIX + 'a.png')
    with pytest.raises(ThemeAssetError):
        validate_allowed_asset_url('https://evil.com/a.png')


# ---------------------------------------------------------------------------
# fetch_theme_asset — streaming and caps
# ---------------------------------------------------------------------------

def test_fetch_returns_joined_bytes_and_pins_request_shape():
    stream = fake_stream([b'ab', b'cd'])
    url = PREFIX + 'a.png'
    assert fetch_theme_asset(url, max_bytes=10, stream_factory=stream) == b'abcd'
    call = stream.calls[0]
    assert call['method'] == 'GET'
    assert call['url'] == url
    assert call['kwargs']['follow_redirects'] is False
    assert call['kwargs']['timeout'] == CHUNKS_TO_TIMEOUT


def test_fetch_does_not_touch_network_when_validation_fails():
    def factory(method, url, **kwargs):
        raise AssertionError('network call must not happen on invalid url')

    with pytest.raises(ThemeAssetError):
        fetch_theme_asset('https://evil.com/a.png', max_bytes=10,
                          stream_factory=factory)


def test_fetch_cap_aborts_one_byte_over():
    stream = fake_stream([b'x' * 11])
    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'big.png', max_bytes=10,
                          stream_factory=stream)


def test_fetch_cap_exact_boundary_passes():
    stream = fake_stream([b'x' * 6, b'y' * 4])
    result = fetch_theme_asset(PREFIX + 'exact.png', max_bytes=10,
                               stream_factory=stream)
    assert result == b'xxxxxx' + b'yyyy'


def test_fetch_background_cap_constants():
    assert MAX_BG_BYTES == 8 * 1024 * 1024  # spec §6/§13: background <= 8 MB
    assert MAX_SMALL_ASSET_BYTES == 1_048_576


def test_fetch_bg_declared_length_8mb_boundary(monkeypatch):
    """Spec §6/§13 boundary: declared content-length exactly 8388608 passes,
    8388609 is rejected before any byte is read."""
    stream_eq = fake_stream([b'\x00' * 8_388_608],
                            headers=FakeHeaders({'Content-Length': 8388608}))
    assert fetch_theme_asset(PREFIX + 'bg.png', max_bytes=MAX_BG_BYTES,
                             stream_factory=stream_eq) == b'\x00' * 8_388_608
    over = fake_stream([], status=200,
                       headers=FakeHeaders({'Content-Length': 8388609}))
    with pytest.raises(ThemeAssetError, match='exceeds .* byte cap'):
        fetch_theme_asset(PREFIX + 'bg.png', max_bytes=MAX_BG_BYTES,
                          stream_factory=over)


def test_fetch_redirect_attempt_treated_as_error():
    stream = fake_stream(status=302)
    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=stream)


def test_fetch_non_200_treated_as_error():
    stream = fake_stream(status=404)
    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=stream)


def test_fetch_timeout_wraps_as_theme_asset_error():
    def factory(method, url, **kwargs):
        raise httpx.ConnectTimeout('timed out')

    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=factory)


def test_fetch_connection_error_wraps_as_theme_asset_error():
    def factory(method, url, **kwargs):
        raise httpx.ConnectError('connection refused')

    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=factory)


def test_fetch_invalid_url_wraps_as_theme_asset_error():
    def factory(method, url, **kwargs):
        raise httpx.InvalidURL('nonprintable url')

    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=factory)


def test_fetch_midstream_http_error_wraps_as_theme_asset_error():
    class BrokenResponse(FakeResponse):
        def iter_bytes(self):
            yield b'start'
            raise httpx.ReadError('stream cut')

    def factory(method, url, **kwargs):
        return BrokenResponse(200, [])

    with pytest.raises(ThemeAssetError):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=factory)


def test_fetch_rejects_content_encoded_response():
    stream = fake_stream(
        [b'\x1f\x8b\x08\x00'], headers=FakeHeaders(
            {'Content-Encoding': 'gzip', 'Content-Length': '4'}
        )
    )
    with pytest.raises(ThemeAssetError, match='encoded response'):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=stream)


def test_fetch_rejects_oversized_declared_length_before_reading():
    class AssertingResponse(FakeResponse):
        def iter_bytes(self):
            raise AssertionError('body must not be read when length is known')

    def factory(method, url, **kwargs):
        return AssertingResponse(
            200, [], headers=FakeHeaders({'Content-Length': '11'})
        )

    with pytest.raises(ThemeAssetError, match='byte cap'):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=factory)


def test_fetch_total_deadline_aborts_trickling_stream(monkeypatch):
    # Fake controllable clock: deadline is crossed only AFTER the first
    # chunk, so the in-loop deadline check is what must fire.
    clock = {'now': 0.0}
    monkeypatch.setattr(assets_module.time, 'monotonic', lambda: clock['now'])

    class TrickleResponse(FakeResponse):
        def iter_bytes(self):
            yield b'x'
            clock['now'] += assets_module._TOTAL_DEADLINE_SECONDS + 1
            yield b'y'

    def factory(method, url, **kwargs):
        return TrickleResponse(200, [])

    with pytest.raises(ThemeAssetError, match='deadline'):
        fetch_theme_asset(PREFIX + 'a.png', max_bytes=10, stream_factory=factory)


def test_fetch_shared_deadline_bounds_sequential_fetches(monkeypatch):
    # PR12 review: sequential fetches must share the render's overall
    # deadline: min(own 60s budget, remaining). Tiny budget => later calls
    # raise before any stream is opened.
    clock = {'now': 0.0}
    monkeypatch.setattr(assets_module.time, 'monotonic', lambda: clock['now'])

    first = fake_stream([b'ab'])
    assert fetch_theme_asset(
        PREFIX + 'a.png', max_bytes=10,
        deadline=clock['now'] + 5.0, stream_factory=first,
    ) == b'ab'  # tiny remaining budget, own 60s cap is not the binding one

    expired = clock['now'] + 5.0
    clock['now'] += 10.0  # deadline now in the past
    never = fake_stream([b'x'])

    for _ in range(2):  # later fetches: fast short-circuit, zero network
        with pytest.raises(ThemeAssetError, match='deadline'):
            fetch_theme_asset(
                PREFIX + 'a.png', max_bytes=10, deadline=expired,
                stream_factory=never,
            )
    assert never.calls == []  # short-circuited before any stream opened
    assert len(first.calls) == 1  # only the one in-budget fetch hit the wire


def test_fetch_shared_deadline_caps_own_budget(monkeypatch):
    # remaining < the fetch's own 60s budget => remaining is the binding one:
    # a trickling stream must abort at the shared deadline, not at 60s.
    clock = {'now': 100.0}
    monkeypatch.setattr(assets_module.time, 'monotonic', lambda: clock['now'])

    def factory(method, url, **kwargs):
        class SlowResponse(FakeResponse):
            def iter_bytes(self):
                yield b'a'
                clock['now'] += 60.0  # past the 5s shared budget, own 60s ok
                yield b'b'

        return SlowResponse(200, [])

    with pytest.raises(ThemeAssetError, match='deadline'):
        fetch_theme_asset(
            PREFIX + 'a.png', max_bytes=10, deadline=clock['now'] + 5.0,
            stream_factory=factory,
        )
