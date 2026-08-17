"""
Symbol normalization shared by the search index and the search query.

Ordering is important! Separate primes, delete/fold, map, then unaccent.
Unaccenting earlier would turn the ayn into an apostrophe (splitting the word)
and rewrite the copyright sign to "(C)", so the map would never match it.

Symbols targeted are derived from the FOLIO "Diacritics & Symbols Macros for AutoHotKey" sheet.
https://docs.google.com/spreadsheets/d/1wimdktbJeKNN2iaGkiYbNEQEaLkC2epK8QDfxjV5THk
"""

import re

# Stage 0. These marks are coordinate punctuation between digits ("138°57ʹ10ʺ")
# and romanization marks between letters ("aktualʹnykh").
PRIME_BETWEEN_DIGITS_PATTERN: str = "(?<=[0-9])[ʹʺ](?=[0-9])"

_PRIME_BETWEEN_DIGITS = re.compile(PRIME_BETWEEN_DIGITS_PATTERN)

# Stage 1a. Deleted so the surrounding word stays whole.
SYMBOL_DELETIONS: tuple[str, ...] = (
    "ʻ",  # U+02BB ayn
    "ʼ",  # U+02BC alif/hamza
    "ʹ",  # U+02B9 soft sign
    "ʺ",  # U+02BA hard sign
    "︠",  # U+FE20 ligature left half
    "︡",  # U+FE21 ligature right half
    "︢",  # U+FE22 double tilde left half
    "︣",  # U+FE23 double tilde right half
)

# Stage 1b. Folded to ASCII. Without this the parser treats them as separators,
# so "C₆H₁₂O₆" contributes only the bare letters c, h, o.
SYMBOL_FOLDINGS: dict[str, str] = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "₀": "0",
    "₁": "1",
    "₂": "2",
    "₃": "3",
    "₄": "4",
    "₅": "5",
    "₆": "6",
    "₇": "7",
    "₈": "8",
    "₉": "9",
    "⁺": "+",
    "⁻": "-",
    "⁽": "(",
    "⁾": ")",
    "₊": "+",
    "₋": "-",
    "₍": "(",
    "₎": ")",
}

# Stage 2. Mapped to sentinel tokens so the symbol survives tokenization. The
# "bcsym" prefix avoids collisions: mapping the flat sign to "flat" would match
# titles like "The Flat Earth".
SYMBOL_SENTINELS: dict[str, str] = {
    "♭": "bcsymflat",
    "♯": "bcsymsharp",
    "♮": "bcsymnatural",
    "°": "bcsymdegree",
    "©": "bcsymcopyright",
    "℗": "bcsymphonogram",
}

# One table for stage 1: folded characters map to a replacement, deleted ones
# map to None.
_TRANSLATE_TABLE: dict[int, str | None] = {
    ord(char): replacement for char, replacement in SYMBOL_FOLDINGS.items()
} | dict.fromkeys(ord(char) for char in SYMBOL_DELETIONS)


def normalize_symbols(text: str) -> str:
    """
    Apply stages 0 to 2; unaccent is left to Postgres.

    The query side must call this on raw user input, before tsquery operators
    are added. Sentinels are space padded, and a space introduced afterwards
    would sit between two terms with nothing joining them.
    """
    if not text:
        return text
    result = _PRIME_BETWEEN_DIGITS.sub(" ", text)
    result = result.translate(_TRANSLATE_TABLE)
    for symbol, sentinel in SYMBOL_SENTINELS.items():
        if symbol in result:
            result = result.replace(symbol, f" {sentinel} ")
    return result
