"""Validate an ORCID iD properly, rather than with a regex that accepts any 16 digits.

An ORCID iD carries an ISO 7064 MOD 11-2 check digit, so a typo in a depositor record can be
caught at entry instead of becoming a silently wrong attribution. The final character is 0-9 or X.
"""
import re

_SHAPE = re.compile(r"^(\d{4})-(\d{4})-(\d{4})-(\d{3}[\dX])$")


class InvalidORCID(ValueError):
    pass


def check_digit(first15):
    """ISO 7064 MOD 11-2 over the first 15 digits. Returns '0'-'9' or 'X'."""
    total = 0
    for char in first15:
        total = (total + int(char)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    return "X" if result == 10 else str(result)


def normalise(value):
    """Return the canonical 0000-0000-0000-0000 form, or raise InvalidORCID.

    Accepts a bare iD or a full https://orcid.org/ URI, with or without hyphens.
    """
    if value is None:
        raise InvalidORCID("missing ORCID iD")
    text = str(value).strip().upper()
    for prefix in ("HTTPS://ORCID.ORG/", "HTTP://ORCID.ORG/", "ORCID.ORG/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    digits = text.replace("-", "").replace(" ", "")
    if len(digits) != 16:
        raise InvalidORCID(f"an ORCID iD has 16 characters, got {len(digits)}")
    formatted = "-".join(digits[i:i + 4] for i in range(0, 16, 4))
    if not _SHAPE.match(formatted):
        raise InvalidORCID("an ORCID iD is 15 digits followed by a digit or X")
    expected = check_digit(digits[:15])
    if digits[15] != expected:
        raise InvalidORCID(f"check digit is {digits[15]}, expected {expected}")
    return formatted


def is_valid(value):
    try:
        normalise(value)
    except InvalidORCID:
        return False
    return True
