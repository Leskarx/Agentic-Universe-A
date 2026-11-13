from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))

_NON_DIGITS_RE = re.compile(r"\D")
_HEADER_SEP_RE = re.compile(r"[_\s]+")
_DATE_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_SLASH_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_DATE_DASH_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_DATE_MON_RE = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$")
_CURRENCY_PREFIX_RE = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse runs of '_' or whitespace to a single space."""
    return _HEADER_SEP_RE.sub(" ", header.strip().lower()).strip()


def normalize_phone(raw: str) -> str | None:
    """Normalize an Indian mobile number to +91XXXXXXXXXX, or None if invalid."""
    digits = _NON_DIGITS_RE.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in ("6", "7", "8", "9"):
        return f"+91{digits}"
    return None


def normalize_date(raw: str) -> str | None:
    """Parse YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, or DD-Mon-YYYY into ISO YYYY-MM-DD."""
    s = raw.strip()
    if not s:
        return None

    m = _DATE_ISO_RE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_SLASH_RE.match(s) or _DATE_DASH_RE.match(s)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = _DATE_MON_RE.match(s)
            if not m:
                return None
            d = int(m.group(1))
            mo = _MONTHS.get(m.group(2).lower(), 0)
            y = int(m.group(3))
            if mo == 0:
                return None

    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def normalize_decimal(raw: str) -> str | None:
    """Parse non-negative currency/decimal string, rounded to 2 decimal places."""
    s = raw.strip()
    if not s:
        return None
    s = _CURRENCY_PREFIX_RE.sub("", s).strip()
    if not s:
        return None
    s = s.replace(",", "")
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    if not val.is_finite() or val < 0:
        return None
    quantized = val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:.2f}"


def normalize_enum(raw: str, allowed_values: tuple[str, ...]) -> str | None:
    """Case-insensitive enum match, returned lower-case."""
    v = raw.strip().lower()
    if not v or v not in allowed_values:
        return None
    return v


def parse_timestamp(raw: str) -> tuple[str, str, str] | None:
    """
    Parse UTC ('...Z'), offset ('...+05:30'), or naive ('YYYY-MM-DD HH:MM:SS' -> IST) timestamp.
    Returns (occurred_at_utc, occurred_at_ist, occurred_date_ist) or None if invalid.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt_utc = dt.astimezone(timezone.utc)
    dt_ist = dt.astimezone(IST)
    return (
        dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        dt_ist.isoformat(timespec="seconds"),
        dt_ist.strftime("%Y-%m-%d"),
    )
