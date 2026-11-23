"""Parsing and selection shared by the Apify adapters (`amazon_apify.py`, `noon_apify.py`).

This is the adapters' **own** parsing (Mafer, 2026-11-22): the parser bugs of the shared
`scraper/utils.py` (1-4, 9-11 in `docs/plan.md`) are fixed here only, and `utils.py` stays
unchanged because Mumzworld and SACO use it. Do not import `utils` from here.

Everything is pure (no I/O, never raises on bad input) except `select_relevant`, which calls the
relevance agent. `origin` comes only from the data: no brand lists. The only brand matched is
Wayakit's own, to keep our products out of the competitor prices.
"""
import collections
import math
import re

# --- Numbers --------------------------------------------------------------------------------

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")

# One number: "1,000" / "1,000.5" (thousands), "12", "12.5", "12,5", ".5". No exponent and at most
# 6 integer digits, so "1e3" and a 5000-digit run never match (bugs 10 and 11). The only word
# character allowed right before it is the pack "x" ("3x500 ml").
NUM = r"(?<![.,])(?<!(?![xX×])\w)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,6}(?:[.,]\d+)?|\.\d+)(?![\d])"


def _to_float(token):
    """A number token as float; a comma before exactly 3 digits is a thousands separator, any
    other comma a decimal point. None if it does not parse."""
    token = re.sub(r",(?=\d{3}(?!\d))", "", token).replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def parse_price(value):
    """Price as a float rounded to 2 decimals, or None when missing, <= 0 or not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        text = value.translate(_ARABIC_DIGITS)
        # First run of digits, dots and commas: a currency text such as "ر.س" or "SAR." around
        # the number must not leave a stray "." in front of it.
        match = re.search(r"(-?)(\d[\d.,]*)", text)
        if not match or match.group(1):
            return None
        x = _to_float(match.group(2).rstrip(".,"))
        if x is None:
            return None
    else:
        return None
    if not math.isfinite(x):
        return None
    x = round(x, 2)
    return x if x > 0 else None  # "0.001" rounds to 0: no price


# --- Volume ---------------------------------------------------------------------------------

_ML = r"milliliters?|millilitres?|mls|ml"
_LITER = r"liters?|litres?|ltrs|ltr|lt|l"
_FLOZ = r"fl\.?\s?oz\.?|fluid\s+ounces?"
# Recognized only to be rejected: a weight or a plain "oz" (weight or volume?) is not a volume.
_REJECT = r"kilograms?|kgs|kg|grams?|gms|gm|g|pounds?|lbs?|ounces?|oz"

_SIZE_RE = re.compile(
    NUM + r"\s*(?:(?P<floz>" + _FLOZ + r")|(?P<ml>" + _ML + r")|(?P<l>" + _LITER + r")|(?P<reject>"
    + _REJECT + r"))(?![a-z])",
    re.I,
)
FLOZ_TO_ML = 29.5735


def parse_volume(text):
    """("ml", total_ml) or ("fl oz", total_floz) for the first size in `text`, times the pack
    multiplier of that size (`_size_pack`); None if there is none, if the first size is a weight
    or plain oz, or if the total is not > 0."""
    if text is None:
        return None
    text = str(text)
    match = _SIZE_RE.search(text)
    if not match or match.group("reject"):
        return None
    amount = _to_float(match.group(1))
    if amount is None:
        return None
    if match.group("floz"):
        unit = "fl oz"
    elif match.group("l"):
        unit, amount = "ml", amount * 1000
    else:
        unit = "ml"
    total = round(amount * _size_pack(text, match), 3)
    if not math.isfinite(total) or total <= 0:
        return None
    return unit, total


# --- Count and pack ---------------------------------------------------------------------------

COUNT_NOUNS = r"(?:wet\s+)?(?:wipes|sheets|count|ct|pcs|pieces|pc|cloths|rags|sachets|brushes|units)"
_VOLUME_UNITS = _FLOZ + "|" + _ML + "|" + _LITER
_NUM_PLAIN = r"(?:\d{1,6}(?:[.,]\d+)?|\.\d+)"
# A length right after "N x M" or "size x N" means a dimension ("40 x 40 cm"), never a pack.
_LENGTH = r"(?:\s*(?:cm|mm|m|in|inch(?:es)?|ft)(?![a-z])|\s*[\"”])"
_NOT_DIM = r"|\s*[x×]\s*\d"  # "x 30 x 30 cm": a further "x <number>" is a dimension chain

# Pack phrases that name a pack wherever they are: "Pack of 3" (but not "pack of 80 wipes": that
# 80 is the count), "3-pack", "3 bottles".
_PACK_PHRASES = [
    re.compile(r"\b(?:pack|set|box)\s+of\s+(\d{1,3})\b(?!\s*" + COUNT_NOUNS + r")", re.I),
    re.compile(r"\b(\d{1,3})[\s-]*pack\b", re.I),
    re.compile(r"\b(\d{1,3})\s*(?:bottles|cans|pouches|refills)\b", re.I),
]
# "3 x 500ml": only when the number after the x is a volume or a count, so a dimension
# ("40 x 40 cm") or "3X concentrate" is never a pack.
_PACK_AXB_RE = re.compile(
    r"(?<![\d.,])(\d{1,3})\s*[x×]\s*(?=" + _NUM_PLAIN + r"\s*(?:" + _VOLUME_UNITS + "|"
    + COUNT_NOUNS + r")(?![a-z]))", re.I)
# "500ml x 3"
_PACK_SIZE_X_N_RE = re.compile(
    r"(?:\d\s*(?:ml|l|ltr|litres?|liters?))\s*[x×]\s*(\d{1,3})(?![\d.,])(?!" + _LENGTH + _NOT_DIM + ")",
    re.I)
# Tied to one size or count token: "N x " right before it, " x N" right after it.
_PACK_BEFORE_RE = re.compile(r"(?<![\d.,])(\d{1,3})\s*[x×]\s*$", re.I)
_PACK_AFTER_RE = re.compile(r"\s*[x×]\s*(\d{1,3})(?![\d.,])(?!" + _LENGTH + _NOT_DIM + ")", re.I)


def _valid_pack(n):
    return n if 1 <= n <= 100 else 1


def _phrase_pack(text):
    """The first "pack of N" / "N pack" / "N bottles" multiplier, 1 if none."""
    for pattern in _PACK_PHRASES:
        match = pattern.search(text)
        if match:
            return _valid_pack(int(match.group(1)))
    return 1


def _tied_pack(text, match):
    """The "N x <token>" or "<token> x N" multiplier of the size or count token `match`, or None.

    Only a multiplier attached to the token that was used counts: in "1.5 L (3 x 500 ml)" the
    "3 x" belongs to the 500 ml breakdown, not to the 1.5 L total."""
    before = _PACK_BEFORE_RE.search(text[:match.start()])
    if before:
        return _valid_pack(int(before.group(1)))
    after = _PACK_AFTER_RE.match(text, match.end())
    if after:
        return _valid_pack(int(after.group(1)))
    return None


def _size_pack(text, match):
    tied = _tied_pack(text, match)
    return tied if tied is not None else _phrase_pack(text)


def pack_count(text):
    """Pack multiplier in 1..100 (1 when there is none or it is out of range).

    Order: the pack phrases ("Pack of N", "N pack", "N bottles") first, then "N x <volume or
    count>", then "<volume> x N". A dimension ("40 x 40 cm") is never a pack."""
    if text is None:
        return 1
    text = str(text)
    for pattern in _PACK_PHRASES + [_PACK_AXB_RE, _PACK_SIZE_X_N_RE]:
        match = pattern.search(text)
        if match:
            return _valid_pack(int(match.group(1)))
    return 1


_COUNT_AXB_RE = re.compile(r"(?<![\d.,])(\d{1,4})\s*[x×]\s*(\d{1,4})\s*" + COUNT_NOUNS + r"\b", re.I)
_COUNT_RE = re.compile(r"(?<![\d.,])(\d{1,4})(?:\.0+)?[\s-]*" + COUNT_NOUNS + r"\b", re.I)


def parse_count(text):
    """Total units (units mode) as an int in 1..100000, or None.

    An explicit "N pcs/wipes" is only multiplied by a multiplier tied to it ("80 wipes x 3") or a
    pack phrase ("Pack of 3, 80 wipes"), never by an unrelated "N x M" (a dimension such as
    "30x30cm" in "12 Pcs Towel 30x30cm")."""
    if text is None:
        return None
    text = str(text)
    match = _COUNT_AXB_RE.search(text)
    if match:
        n = int(match.group(1)) * int(match.group(2))
    else:
        match = _COUNT_RE.search(text)
        if match:
            after = _PACK_AFTER_RE.match(text, match.end())
            n = int(match.group(1)) * (_valid_pack(int(after.group(1))) if after else _phrase_pack(text))
        else:
            n = pack_count(text)
            if n <= 1:
                return None
    return n if 1 <= n <= 100000 else None


# --- Size agreement (Amazon) ------------------------------------------------------------------

def _to_ml(size):
    unit, amount = size
    return amount * FLOZ_TO_ML if unit == "fl oz" else amount


def sizes_agree(a, b):
    """Two `parse_volume` results agree within 1 ml or 2 %."""
    a_ml, b_ml = _to_ml(a), _to_ml(b)
    return abs(a_ml - b_ml) <= max(1.0, 0.02 * max(a_ml, b_ml))


def agreed_source(sources):
    """`(index, size)` for the first source (index in `sources`) of the first agreeing pair, in
    source order; None when fewer than 2 sources parse or no pair agrees."""
    parsed = [(i, parse_volume(s)) for i, s in enumerate(sources) if s is not None]
    parsed = [(i, p) for i, p in parsed if p is not None]
    for a in range(len(parsed)):
        for b in range(a + 1, len(parsed)):
            if sizes_agree(parsed[a][1], parsed[b][1]):
                return parsed[a]
    return None


def agreed_size(sources):
    """The size of the first source of the first agreeing pair (in source order), or None when
    fewer than 2 sources parse or no pair agrees."""
    found = agreed_source(sources)
    return found[1] if found else None


# --- Currency, Wayakit, text ------------------------------------------------------------------

SAR_ALIASES = {"SAR", "SR", "ر.س", "ر.س.", "﷼", "رس"}
WAYAKIT_RE = re.compile(r"waya\s*-?\s*kit|وايا\s*كيت", re.I)


def is_sar(currency):
    if currency is None:
        return False
    text = str(currency).strip()
    return text.upper() in SAR_ALIASES or text in SAR_ALIASES


def is_wayakit(*values):
    """True if any value names Wayakit: our own brand, not a competitor list."""
    return any(value and WAYAKIT_RE.search(str(value)) for value in values)


def flatten_text(obj, limit=5000):
    """Every string or number leaf of nested dicts and lists, joined with spaces, cut at `limit`."""
    parts = []
    size = 0
    stack = [obj]
    while stack and size < limit:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, (list, tuple)):
            stack.extend(reversed(node))
        elif isinstance(node, bool) or node is None:
            continue
        elif isinstance(node, (str, int, float)):
            text = str(node)
            parts.append(text)
            size += len(text) + 1
    return " ".join(parts)[:limit]


def clean(value):
    """Whitespace collapsed and stripped; None gives ''."""
    if value is None:
        return ""
    return " ".join(str(value).split())


# --- Origin (data only) -----------------------------------------------------------------------

KSA_NAMES = {"saudi arabia", "ksa", "kingdom of saudi arabia", "sa", "السعودية", "المملكة العربية السعودية"}
# Non-canonical spellings ("Saudi", "Saudi Arabia (KSA)") are local too; the 2-letter "sa" only
# as the whole value (it is a common syllable).
_KSA_COUNTRY_RE = re.compile(r"\bsaudi\b|\bksa\b|السعودية", re.I)
MADE_IN_KSA_RE = re.compile(
    r"made\s+in\s+(?:the\s+)?(?:ksa|saudi(?:\s+arabia)?|kingdom\s+of\s+saudi\s+arabia)"
    r"|صنع\s+في\s+(?:المملكة\s+العربية\s+)?السعودية",
    re.I,
)


def derive_origin(*, country_of_origin=None, texts=(), noon_global=None, unit=None):
    """'local', 'imported' or 'unknown', in this precedence: country of origin, a "made in KSA"
    text, the Noon global-shipping flag, a size in fl oz."""
    country = clean(country_of_origin).lower()
    if country:
        return "local" if country in KSA_NAMES or _KSA_COUNTRY_RE.search(country) else "imported"
    if any(text and MADE_IN_KSA_RE.search(str(text)) for text in texts or ()):
        return "local"
    if noon_global:
        return "imported"
    if unit == "fl oz":
        return "imported"
    return "unknown"


# --- Relevance and output ---------------------------------------------------------------------

WIPES_KEYWORDS = ("wipes", "rags")  # same trigger as the deleted Selenium Amazon scraper
ROW_KEYS = ("Product", "Price_SAR", "Company", "URL", "Unit of measurement", "Total quantity",
            "Seller", "Origin")


def select_relevant(candidates, keyword, search_mode, agent):
    """Size gate, then one relevance batch, then (wipes only) one units batch.

    Returns the kept candidates and a Counter of drop reasons ('no_size', 'not_relevant').
    Never catches anything: a fatal relevance error propagates and stays in `agent.fatal_error`.
    """
    reasons = collections.Counter()
    wipes = search_mode == "units" and any(w in keyword.lower() for w in WIPES_KEYWORDS)
    kept = list(candidates)
    if not wipes:
        # Before the AI: a row without a size cannot be priced, so it is not worth a call.
        sized = [c for c in kept if c.get("Total quantity")]
        reasons["no_size"] += len(kept) - len(sized)
        kept = sized
    if kept:
        answers = agent.filter_relevant([c["Product"] for c in kept], keyword)
        relevant = [c for c, ok in zip(kept, answers) if ok]
        reasons["not_relevant"] += len(kept) - len(relevant)
        kept = relevant
    if wipes and kept:
        units = agent.extract_wipes_units_batch([c["Product"] for c in kept])
        for candidate, u in zip(kept, units):
            if u and u > 0:
                candidate["Total quantity"] = u
                candidate["Unit of measurement"] = "units"
        sized = [c for c in kept if c.get("Total quantity")]
        reasons["no_size"] += len(kept) - len(sized)
        kept = sized
    return kept, reasons


def to_row(candidate):
    """The scraper-contract dict (8 keys); private '_' keys are dropped."""
    row = {key: candidate.get(key) for key in ROW_KEYS if key != "Origin"}
    row["Origin"] = derive_origin(**candidate["_origin"], unit=candidate["Unit of measurement"])
    return {key: row[key] for key in ROW_KEYS}
