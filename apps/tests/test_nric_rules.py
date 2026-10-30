"""
Canonical NRIC rule-set (apps/documents/nric.py) — pure function tests.
"""
from datetime import date

import pytest

from apps.documents.nric import (
    extract_nric_from_text,
    resolve_birth_date,
    validate_nric,
)

TODAY = date(2026, 4, 15)
# Valid: born 2001-01-01, state 14 (Pulau Pinang), serial nonzero.
VALID = "010101-14-5123"


@pytest.mark.security
class TestNricShape:
    def test_valid_shape(self):
        assert validate_nric(VALID, today=TODAY)["valid"] is True

    @pytest.mark.parametrize("bad", [
        "010101145125", "010101-14-512", "010101-1-5123", "a10101-14-5123",
        "010101.14.5123", "", " 010101-14-5123  extra",
    ])
    def test_bad_shapes_rejected(self, bad):
        r = validate_nric(bad, today=TODAY)
        assert r["valid"] is False and "format" in r["reason"].lower()


@pytest.mark.security
class TestNricDate:
    def test_calendar_invalid_day(self):
        assert validate_nric("010229-14-5123", today=TODAY)["valid"] is False
        assert validate_nric("010231-14-5123", today=TODAY)["valid"] is False

    def test_valid_leap_day(self):
        # 29 Feb 2000 exists
        assert validate_nric("000229-14-5123", today=TODAY)["valid"] is True

    def test_future_date_rejected(self):
        future = date(2026, 4, 16)
        ymd = future.strftime("%y%m%d")
        assert validate_nric(f"{ymd}-14-5123", today=TODAY)["valid"] is False

    def test_century_convention(self):
        # yy 25 → 2025 (past year within current-century window)
        assert validate_nric("250101-14-5123", today=TODAY)["valid"] is True
        assert validate_nric("250101-14-5123", today=TODAY)["birth_date"] == date(2025, 1, 1)
        # yy 12 with current yy 26 → 2012
        r157 = validate_nric("121212-14-5123", today=TODAY)
        assert r157["valid"] is True and r157["birth_date"].year == 2012

    def test_age_over_130_unreachable_via_century_rule(self):
        # yy 27 ≤ current yy 26 is impossible; yy 27 → 1927 (age 99 via rule)
        r = validate_nric("270101-14-5123", today=TODAY)
        assert r["valid"] is True and r["birth_date"] == date(1927, 1, 1)

    def test_resolve_birth_date_none_on_bad_month(self):
        assert resolve_birth_date(90, 13, 1, TODAY) is None
        assert resolve_birth_date(90, 2, 30, TODAY) is None


@pytest.mark.security
class TestNricStateCode:
    @pytest.mark.parametrize("state", ["01", "05", "10", "16", "21", "58", "59", "60", "90", "99"])
    def test_valid_state_prefixes(self, state):
        r = validate_nric(f"010101-{state}-5123", today=TODAY)
        assert r["valid"] is True, r

    @pytest.mark.parametrize("state", ["00", "17", "18", "19", "20"])
    def test_invalid_state_codes(self, state):
        r = validate_nric(f"010101-{state}-5123", today=TODAY)
        assert r["valid"] is False


@pytest.mark.security
class TestNricSerialAndCrossChecks:
    def test_all_zero_serial_rejected(self):
        assert validate_nric("010101-14-0000", today=TODAY)["valid"] is False

    def test_dob_match_hard_reject_on_mismatch(self):
        r = validate_nric(VALID, dob="2000-01-01", today=TODAY)
        assert r["valid"] is False and "birth date" in r["reason"].lower()

    def test_dob_match_ok(self):
        assert validate_nric(VALID, dob="2001-01-01", today=TODAY)["valid"] is True

    def test_gender_mismatch_is_warning_not_reject(self):
        # serial ends 3 (odd) → male; claiming female → warning
        r = validate_nric(VALID, gender="female", today=TODAY)
        assert r["valid"] is True and r["warnings"], r
        r = validate_nric("010101-14-5128", gender="MALE", today=TODAY)  # even → female
        assert r["valid"] is True and r["warnings"]

    def test_gender_match_no_warning(self):
        r = validate_nric(VALID, gender="male", today=TODAY)
        assert r["valid"] is True and r["warnings"] == []


@pytest.mark.security
class TestExtractFromOcrText:
    def test_dash_form(self):
        assert extract_nric_from_text("KAD PENGENALAN TAN 010101-14-5123 END") == VALID

    def test_space_form_normalized(self):
        assert extract_nric_from_text("010101 14 5123") == VALID

    def test_no_match(self):
        assert extract_nric_from_text("PASSPORT A12345678 MYS") is None
