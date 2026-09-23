"""Screening for content that must not be remembered automatically.

  secret    -> never stored (passwords, keys, tokens, card numbers, ...)
  sensitive -> never stored silently; needs the user's confirmation
Detection is regex/keyword based and deliberately conservative. It is a
safety net, not a guarantee: it will miss unusual secrets. Matched text is
never logged or returned, only the category.
"""

import re
from dataclasses import dataclass

_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "credential_word": re.compile(
        r"\b(pass(?:word|wd|phrase|code)|pwd|passcode|api[ _-]?key|secret[ _-]?key|access[ _-]?key|"
        r"auth(?:entication)?[ _-]?token|bearer|private[ _-]?key|client[ _-]?secret|otp|cvv|cvc|"
        r"pin(?: code| number)?)\b\s*(?:is|are|was|=|:)",
        re.IGNORECASE,
    ),
    "credential_mention": re.compile(
        r"\b(password|passphrase|api[ _-]?key|secret[ _-]?key|private[ _-]?key|access[ _-]?token|"
        r"auth[ _-]?token|recovery[ _-]?code|security[ _-]?code)\b",
        re.IGNORECASE,
    ),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "known_token_prefix": re.compile(
        r"\b(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
        r"AIza[0-9A-Za-z_-]{30,})"
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "long_random_string": re.compile(r"\b(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}\b"),
    "national_id": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_SENSITIVE_CATEGORIES: dict[str, re.Pattern[str]] = {
    "health": re.compile(
        r"\b(diagnos\w*|medication|prescri\w+|therapy|therapist|depress\w*|anxiety|cancer|diabet\w*|"
        r"hiv|pregnan\w*|disorder|illness|surgery|medical)\b",
        re.IGNORECASE,
    ),
    "religion_politics": re.compile(
        r"\b(religio\w*|muslim|christian|hindu|jewish|buddhist|atheist|vote[ds]?|political\w*|"
        r"democrat\w*|republican\w*)\b",
        re.IGNORECASE,
    ),
    "sexuality_identity": re.compile(r"\b(gay|lesbian|bisexual|transgender|sexual orientation)\b", re.IGNORECASE),
    "finance": re.compile(
        r"\b(salary|income|bank|account number|iban|loan|debt|credit score|net worth|tax(?:es)? id)\b",
        re.IGNORECASE,
    ),
    "contact_or_address": re.compile(
        r"(\b\d{1,5}\s+\w+(?:\s\w+)?\s(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd)\b|"
        r"\b\+?\d[\d ()-]{8,}\d\b|[\w.+-]+@[\w-]+\.[\w.]+|\bhome address\b|\bphone number\b|\bemail address\b)",
        re.IGNORECASE,
    ),
    "date_of_birth": re.compile(r"\b(date of birth|born on|birthday|my dob)\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class Screening:
    secret: str | None = None  # category name, never the matched text
    sensitive: str | None = None


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def _looks_like_card(text: str) -> bool:
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return True
    return False


def screen(text: str) -> Screening:
    """Classify `text`. Secrets take precedence over merely sensitive content."""
    for category, pattern in _SECRET_PATTERNS.items():
        if pattern.search(text):
            return Screening(secret=category)
    if _looks_like_card(text):
        return Screening(secret="payment_card")
    for category, pattern in _SENSITIVE_CATEGORIES.items():
        if pattern.search(text):
            return Screening(sensitive=category)
    return Screening()
