"""Shared service-role Supabase client factory.

Single source of truth for the per-request service-role client used by
every apps/* service (bypasses RLS). Per-call creation semantics (new
client each request) — call sites may patch/test via this module.
"""

from django.conf import settings
from supabase import create_client


def get_sb():
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
