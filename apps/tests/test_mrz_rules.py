"""
ICAO 9303 TD3 MRZ parser + check-digit rules (apps/documents/mrz.py).

Golden sample generated and cross-validated with a reference MRZ
implementation (mrz-library TD3 generator/checker).
"""
import pytest

from apps.documents.mrz import check_digit, parse_td3, passport_number_valid

SAMPLE = (
    "P<MYSTAN<<MEI<LING<<<<<<<<<<<<<<<<<<<<<<<<<<\n"
    "A123456784MYS8501020F3001318ZE184736B<<<<<30"
)


@pytest.mark.security
class TestCheckDigit:
    @pytest.mark.parametrize("s,expected", [
        ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "7"),
        ("0123456789", "7"),
        ("0123456789ABCDEF", "0"),
        ("0", "0"),
        ("<<<<<<", "0"),
        ("A12345678", "4"),
        ("850102", "0"),
        ("300131", "8"),
    ])
    def test_known_values(self, s, expected):
        assert check_digit(s) == expected


@pytest.mark.security
class TestParseTd3:
    def test_golden_sample_parses_clean(self):
        r = parse_td3(SAMPLE)
        assert r is not None
        assert r["passport_number"] == "A12345678"
        assert r["date_of_birth"] == "1985-01-02"
        assert r["date_of_expiry"] == "2030-01-31"
        assert r["errors"] == []

    def test_truncated_tail_not_judged(self):
        # composite CD lost to OCR truncation → '<' → not an error
        r = parse_td3(SAMPLE[:-1])
        assert r["errors"] == []

    @pytest.mark.parametrize("corrupt,err", [
        ("A123456784", "A123456786"),   # document number check digit
        ("MYS8501020", "MYS8501027"),   # birth date check digit
        ("F3001318", "F3001317"),       # expiry date check digit
    ])
    def test_corrupted_check_digits_detected(self, corrupt, err):
        r = parse_td3(SAMPLE.replace(corrupt, err))
        assert r is not None and r["errors"], r

    def test_corrupted_composite_detected(self):
        r = parse_td3(SAMPLE[:-1] + "4")
        assert r["errors"] == ["composite_check_digit"]

    def test_no_mrz(self):
        assert parse_td3("KAD PENGENALAN TAN 900101-14-5123") is None
        assert parse_td3("") is None

    def test_non_digit_date_does_not_crash(self):
        """OCR commonly reads 0 as O. The date slices may therefore contain
        letters; parse_td3 must record undecipherable_date in errors instead
        of raising ValueError (which surfaces as an unhandled HTTP 500)."""
        bad_birth = SAMPLE.replace("850102", "8S0102")
        bad_expiry = SAMPLE.replace("300131", "3001S1")
        for line2 in (bad_birth, bad_expiry):
            r = parse_td3(line2)
            assert r is not None, line2
            assert r["passport_number"] == "A12345678", r
            assert "undecipherable_date" in r["errors"], r
            assert r["date_of_birth"] is None or isinstance(r["date_of_birth"], str)

    def test_fragments_with_noise_between_lines_still_parse(self):
        noisy = "P<MYSTAN<<MEI<LING<<<<<<<<<<<<<<<<<<<<<<<<<<\nJUNKFIELD\n" + SAMPLE.split("\n")[1]
        r = parse_td3(noisy)
        assert r is not None and r["passport_number"] == "A12345678"


@pytest.mark.security
class TestPassportNumberShape:
    @pytest.mark.parametrize("n", ["A1234567", "AB12345", "123456789012345", "X0X0X"])
    def test_valid(self, n):
        assert passport_number_valid(n)

    @pytest.mark.parametrize("n", ["ABCD", "A1234567890123456", "A12345_6", "", "ABC<"])
    def test_invalid(self, n):
        assert not passport_number_valid(n)
