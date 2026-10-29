"""Small helpers for reading the printer's replies and G-code text by hand.

Everything the plugin reads is plain ASCII: a letter followed by a number,
fixed words like ``NOT INITIALIZED``, or slicer comments. These helpers say
what counts as a number, so the parsers in ``m850``, ``zshift`` and ``ladder``
can read like the formats they describe.
"""

import string

DIGITS = string.digits
LETTERS = string.ascii_letters


def is_digits(text):
    """True for one or more ASCII digits: ``"330"``."""
    return text != "" and all(c in DIGITS for c in text)


def is_single_digit(text):
    return len(text) == 1 and text in DIGITS


def is_int(text):
    """True for a whole number with an optional minus sign: ``"-330"``."""
    if text.startswith("-"):
        text = text[1:]
    return is_digits(text)


def is_decimal(text):
    """True for an optional minus sign, digits and an optional decimal part:
    ``"-0.8250"``, ``"12"``.
    """
    if text.startswith("-"):
        text = text[1:]
    whole, dot, fraction = text.partition(".")
    return is_digits(whole) and (not dot or is_digits(fraction))


def take_while(text, allowed, start=0):
    """Return the index of the first character from ``start`` not in ``allowed``."""
    end = start
    while end < len(text) and text[end] in allowed:
        end += 1
    return end


def number_end(text, start):
    """Find a G-code number at ``start`` and return the index just past it.

    A number is an optional sign, then digits with an optional decimal part
    (``5``, ``5.``, ``5.25``) or a decimal part alone (``.25``), as in
    ``X5.25`` or ``E-.8``. Reading stops at anything else, so ``3.11.0`` reads
    as ``3.11``. Returns ``start`` when there is no number there.
    """
    pos = start
    if pos < len(text) and text[pos] in "+-":
        pos += 1
    digits_start = pos
    end = take_while(text, DIGITS, digits_start)
    has_whole = end > digits_start
    if end < len(text) and text[end] == ".":
        fraction_end = take_while(text, DIGITS, end + 1)
        if has_whole or fraction_end > end + 1:
            end = fraction_end
    if end == digits_start:
        return start  # a sign or a dot with no digits is not a number
    return end
