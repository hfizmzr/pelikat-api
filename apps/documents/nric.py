"""
Canonical Malaysian NRIC (MyKad) validation rules.

Pure functions — no Django, no network. Used by the OCR path
(soft-fail, feeds needs_review warnings) and the manual encrypt-ic
path (hard reject when a profile DOB is explicitly provided).
"""

import re
from datetime import date

NRIC_RE = re.compile(r"^\d{6}-\d{2}-\d{4}$")
# OCR text may lose dashes; tolerant finder, normalized to canonical shape.
OCR_NRIC_RE = re.compile(r"\b(\d{6})[-\s](\d{2})[-\s](\d{4})\b")

_VALID_STATE_CODES = set(range(1, 17)) | set(range(21, 60)) | set(range(60, 100))
_MAX_AGE_YEARS = 130


def resolve_birth_date(yy: int, mm: int, dd: int, today: date) -> date | None:
    """
    YYMMDD -> date using the IC century convention:
    yy > current 2-digit year -> 19yy, else 20yy.
    Returns None for invalid calendar dates or future dates.
    """
    century = 1900 if yy > today.year % 100 else 2000
    try:
        d = date(century + yy, mm, dd)
    except ValueError:
        return None
    if d > today:
        return None
    return d


def validate_nric(nric: str, *, dob: str | None = None, gender: str | None = None,
                  today: date | None = None) -> dict:
    """
    Full rule-set. Returns::

        {"valid": True, "warnings": [...]}`   — accepted (warnings = soft issues)
        {"valid": False, "reason": "..."}     — hard reject

    `dob`: optional ISO YYYY-MM-DD from the verified profile. When given,
    a mismatch is a hard reject (manual entry path). `gender`: one of
    "male"/"female"/"M"/"F" — a mismatch is only a needs_review warning.
    """
    today = today or date.today()
    nric = nric.strip()

    if not NRIC_RE.fullmatch(nric):
        return {"valid": False,
                "reason": "NRIC must match the format 123456-78-9012."}

    dob_part, state_part, serial = nric.split("-")
    yy, mm, dd = int(dob_part[:2]), int(dob_part[2:4]), int(dob_part[4:6])
    state = int(state_part)
    warnings: list[str] = []

    if state not in _VALID_STATE_CODES:
        return {"valid": False,
                "reason": f"NRIC state code {state_part} is not valid."}

    if serial == "0000":
        return {"valid": False,
                "reason": "NRIC serial number cannot be 0000."}

    birth = resolve_birth_date(yy, mm, dd, today)
    if birth is None:
        return {"valid": False,
                "reason": "NRIC birth date is invalid or in the future."}

    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    if age > _MAX_AGE_YEARS:
        return {"valid": False,
                "reason": "NRIC birth date implies an impossible age (over 130 years)."}

    if dob:
        try:
            dob_date = date.fromisoformat(dob.strip())
        except ValueError:
            return {"valid": False, "reason": "Provided DOB is not a valid date."}
        if dob_date != birth:
            return {"valid": False,
                    "reason": "NRIC birth date does not match the profile date of birth."}

    if gender:
        g = gender.strip().upper()
        expected = "M" if int(serial[-1]) % 2 else "F"
        if g in ("MALE", "M") and expected == "F":
            warnings.append("NRIC serial digit suggests female but profile gender is male.")
        elif g in ("FEMALE", "F") and expected == "M":
            warnings.append("NRIC serial digit suggests male but profile gender is female.")

    return {"valid": True, "nric": nric, "birth_date": birth, "warnings": warnings}


def extract_nric_from_text(text: str) -> str | None:
    """Find a candidate NRIC in OCR text (dash-or-space separated) and
    return it in canonical dash form, or None."""
    m = OCR_NRIC_RE.search(text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
