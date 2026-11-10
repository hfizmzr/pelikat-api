"""
Security Header Tests (NFR04: XFO + nosniff + referrer pinned in settings)
"""


def test_security_headers_present(api_client):
    """Client responses carry the pinned NFR04 security headers."""
    response = api_client.get('/nonexistent-url/')
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Referrer-Policy'] == 'same-origin'
