"""
ICAO 9303 TD3 (passport) MRZ parsing and check-digit validation.

Pure functions — no OCR/network dependency. OCR text lines come in; a
structurally plausible line-1/line-2 pair is parsed and every check
digit (document number, birth date, expiry date, final composite) is
verified with the standard 7-3-1 weighting.

TD3 line 2 layout (0-based, per ICAO 9303 Part 4):
    [0:9]   document number ('<' padded)   [9]      check digit
    [10:13] nationality                      [13:19] date of birth
    [19]    check digit                      [20]    sex
    [21:27] date of expiry                   [27]    check digit
    [28:42] optional data                    [42]    optional-data check digit
    [43]    final composite check digit
Composite input = [0:10] + [13:20] + [21:28] + [28:42] + [43].
"""

import re
from datetime import date

_WEIGHTS = (7, 3, 1)
_FIELD_CHARS = re.compile(r"^[A-Z0-9<]+$")
_PASSPORT_NUMBER_RE = re.compile(r"^[A-Z0-9]{5,15}$")

# OCR impact analysis
_LINE1_RE = re.compile(r"^P<[A-Z0-9<]{38,42}$")  # drop are safe
_LINE2_RE = re.compile(r"^[A-Z0-9<]{28,44}$")


def character_value(ch: str) -> int:
    """ICAO value of one character: digit=0-9, A-Z=10-35, '<'=0."""
    if ch == "<":
        return 0
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return 10 + ord(ch) - ord("A")
    raise ValueError(f"invalid MRZ character: {ch!r}")


def check_digit(s: str) -> str:
    """7-3-1 weighted check digit; '<' counts as 0."""
    total = sum(character_value(c) * _WEIGHTS[i % 3] for i, c in enumerate(s))
    return str(total % 10)


def parse_td3(text: str) -> dict | None:
    """
    Scan OCR text for a TD3 line-1/line-2 pair. Returns None if no
    MRZ is present. On a candidate pair, fields are extracted and the
    check digits validated (missing/truncated trailing digits padded
    with '<' are reported, never hard-failed on their own).
    Returns::

        {"passport_number": str, "date_of_birth": str, "date_of_expiry": str,
         "errors": [str, ...]}
        errors keys: passport_number_check_digit | date_of_birth_check_digit |
                     date_of_expiry_check_digit | composite_check_digit
    """
    lines = [ln.strip().upper() for ln in (text or "").splitlines() if ln.strip()]
    for i, l1 in enumerate(lines):
        if not l1.startswith("P<") or not _LINE1_RE.fullmatch(l1):
            continue
        # MRZ line 2 normally follows directly; scan local context so a
        # stray OCR fragment between the two lines does not kill the pair.
        for l2 in lines[i + 1:i + 3]:
            if not _LINE2_RE.fullmatch(l2) or len(l2) < 28:
                continue
            padded = (l2 + "<" * 44)[:44]
            doc, doc_cd = padded[0:9], padded[9]
            birth, birth_cd = padded[13:19], padded[19]
            expiry, expiry_cd = padded[21:27], padded[27]
            optional, optional_cd = padded[28:42], padded[42]
            composite_cd = padded[43]

            errors: list[str] = []

            doc_num = doc.rstrip("<")
            if check_digit(doc) != doc_cd:
                errors.append("passport_number_check_digit")

            if check_digit(birth) != birth_cd:
                errors.append("date_of_birth_check_digit")
            if check_digit(expiry) != expiry_cd:
                errors.append("date_of_expiry_check_digit")

            composite_input = (doc + doc_cd + birth + birth_cd +
                               expiry + expiry_cd + optional + optional_cd)
            # A '<' composite (OCR truncated the tail) cannot be judged;
            # a present-but-wrong one is hard evidence of a corrupt MRZ.
            if composite_cd != "<" and check_digit(composite_input) != composite_cd:
                errors.append("composite_check_digit")
            birth_date = _resolve_date(birth, expiring=False, errors=errors)
            expiry_date = _resolve_date(expiry, expiring=True, errors=errors)

            return {
                "passport_number": doc_num,
                "date_of_birth": birth_date,
                "date_of_expiry": expiry_date,
                "errors": errors,
                # OCR truncated the composite check digit — identity must
                # not be treated as clean even if all parsed fields passed.
                "truncated": composite_cd == "<",
            }
    return None


def _resolve_date(d6: str, *, expiring: bool, errors: list[str]) -> str | None:
    """YYMMDD -> ISO date. Expiry years are always 20yy; birth uses the
    IC century convention. Undecipherable dates surface as errors."""
    if not d6.isdigit():
        # OCR may read 0 as O etc. — never crash the request (500);
        # record the failure instead.
        errors.append("undecipherable_date")
        return None
    yy, mm, dd = int(d6[0:2]), int(d6[2:4]), int(d6[4:6])
    today = date.today()
    year = 2000 + yy if expiring else (1900 + yy if yy > today.year % 100 else 2000 + yy)
    try:
        resolved = date(year, mm, dd)
    except ValueError:
        errors.append("undecipherable_date")
        return None
    return resolved.isoformat()


def passport_number_valid(number: str) -> bool:
    """Loose fallback format for passports without a readable MRZ."""
    return bool(_PASSPORT_NUMBER_RE.fullmatch(number))
