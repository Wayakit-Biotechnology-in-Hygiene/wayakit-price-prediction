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
# character allowed right before it is the pack "x" ("3x500 ml"). Never right after "<digit>/": the
# "2" of "1/2 Gallon" or the "19" of the case notation "12/19 oz" is not an amount, so such a size is
# not read at all ("Liquid/500ml" still is).
NUM = r"(?<![.,])(?<!\d/)(?<!(?![xX×])\w)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,6}(?:[.,]\d+)?|\.\d+)(?![\d])"


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
# Volume mode only (Mafer, 2026-11-23): a plain "oz" / "ounces" is read as fl oz (liquids sold in
# US units: "32oz", "32 Ounces", "64-Ounce"), unless the text marks it as a weight (`_oz_is_weight`).
# In a title it only counts when another source confirms it (`title_volume`, `amazon_size`).
_OZ = r"ounces?|ozs|oz"
_GALLON = r"gallons?|gal"  # US gallon = 128 fl oz
# Recognized only to be rejected: a weight is not a volume.
_REJECT = r"kilograms?|kgs|kg|grams?|gms|gm|g|pounds?|lbs?"
_SEP = r"\s*(?:-\s*)?"  # "64-Ounce"; written this way so a long run of spaces cannot backtrack

_SIZE_RE = re.compile(
    NUM.replace(r"(\d{1,3}(?:,", r"(?P<num>\d{1,3}(?:,", 1) + _SEP + r"(?:(?P<floz>" + _FLOZ + r")|(?P<gal>"
    + _GALLON + r")|(?P<ml>" + _ML + r")|(?P<l>" + _LITER + r")|(?P<oz>" + _OZ + r")|(?P<reject>" + _REJECT
    + r"))(?![a-z])",
    re.I,
)
_NET_WEIGHT_BEFORE_RE = re.compile(r"\b(?:net\s*)?(?:wt|weight)\.?\s*[:.]?\s*$", re.I)
# Right after an oz: "(595 g)", "/ 396g", "Net Wt" -> the oz is a weight.
_WEIGHT_AFTER_RE = re.compile(
    r"\s*(?:[(/,-]\s*)?(?:\d{1,6}(?:[.,]\d+)?\s*(?:kilograms?|kgs|kg|grams?|gms|gm|g|pounds?|lbs?)(?![a-z])"
    r"|net\s*(?:wt|weight)\b)", re.I)
FLOZ_TO_ML = 29.5735
FLOZ_PER_GALLON = 128
# A plain oz in a text that names a solid or an aerosol is a net weight ("Dishwasher Pods 16 oz",
# "Paste Wax 11 oz", "Clay Bar 3.5 oz", "Aerosol 8.8 oz"). Wayakit sells liquids only, so these
# rows are not wanted anyway (Mafer, 2026-11-23).
_SOLID_RE = re.compile(
    r"\b(?:powders?|pods?|pacs?|tablets?|tabs|granules?|granular|crystals|capsules?|flakes|cubes?|paste"
    r"|bars?|(?<!non-)(?<!non )sticks?|soda|beads?|clay|salts?|blocks?|aerosols?)\b", re.I)
_THOUSANDS_DOT_RE = re.compile(r"\d{1,3}\.\d{3}")  # "1.000 ml": 1 ml or 1000 ml? Not read.

EXPLICIT, GALLON, PLAIN_OZ, UNUSABLE = "explicit", "gal", "oz", "unusable"


def _oz_is_weight(text, match, solid):
    """`solid`: the text names a solid or an aerosol (computed once per text)."""
    before = text[max(0, match.start() - 24):match.start()]
    return bool(solid or _NET_WEIGHT_BEFORE_RE.search(before) or _WEIGHT_AFTER_RE.match(text, match.end()))


def _size_tokens(text):
    """Every size token of `text`, in order, as `(kind, unit, amount, match)`.

    `kind` is EXPLICIT (ml, liters, fl oz), GALLON, PLAIN_OZ (a plain oz read as fl oz) or
    UNUSABLE (a weight: grams, pounds, an oz marked as a weight or next to a solid; or a number
    that cannot be read). `amount` is the size of that one token in `unit` ("ml" or "fl oz"),
    without any pack."""
    tokens = []
    solid = bool(_SOLID_RE.search(text))
    for match in _SIZE_RE.finditer(text):
        number = match.group("num")
        amount = _to_float(number)
        if (amount is None or match.group("reject") or (match.group("oz") and _oz_is_weight(text, match, solid))
                or (match.group("ml") and _THOUSANDS_DOT_RE.fullmatch(number))):
            tokens.append((UNUSABLE, None, None, match))
        elif match.group("floz"):
            tokens.append((EXPLICIT, "fl oz", amount, match))
        elif match.group("oz"):
            tokens.append((PLAIN_OZ, "fl oz", amount, match))
        elif match.group("gal"):
            tokens.append((GALLON, "fl oz", amount * FLOZ_PER_GALLON, match))
        elif match.group("l"):
            tokens.append((EXPLICIT, "ml", amount * 1000, match))
        else:
            tokens.append((EXPLICIT, "ml", amount, match))
    return tokens


def _same(a, b):
    """Two sizes of one title are the same quantity: `sizes_agree`, or within 7 % when one is
    metric and the other US, because labels round the conversion ("4 L (1 Gallon)", "500ml/16oz")."""
    if a[0] == b[0]:
        return sizes_agree(a, b)
    a_ml, b_ml = _to_ml(a), _to_ml(b)
    return abs(a_ml - b_ml) <= 0.07 * max(a_ml, b_ml)


def _token_value(text, token):
    """`(unit, amount, tied)` of one token: its amount times the "N x" attached to it, if any."""
    _, unit, amount, match = token
    tied = _tied_pack(text, match)
    return unit, amount * (tied or 1), tied is not None


def _stated_total(text, tokens, chosen):
    """The pack total for the token `chosen`, in its own unit.

    One size, or the same size repeated ("32Oz, Blue, 32 Oz Spray (2 Pack)"): the size times the
    pack phrase of the text. When the text itself shows the total ("400ml Twin Pack 2x200ml",
    "1.5 L (3 x 500 ml)", "500ml, Pack of 3 (1.5 L total)") the pack phrase never multiplies it
    again: the largest stated quantity is the total."""
    usable = [t for t in tokens if t[0] != UNUSABLE][:8]
    values = [_token_value(text, t) for t in usable]
    unit, amount, _ = _token_value(text, chosen)
    pack = _volume_phrase_pack(text)
    first = values[0][:2]
    if any(tied for _, _, tied in values) or not all(_same(v[:2], first) for v in values):
        biggest = max((v[:2] for v in values), key=_to_ml)
        if not _same((unit, amount), biggest) and pack > 1 and _same((unit, amount * pack), biggest):
            amount *= pack
    else:
        amount *= pack
    total = round(amount, 3)
    return (unit, total) if math.isfinite(total) and total > 0 else None


def parse_volume(text):
    """("ml", total_ml) or ("fl oz", total_floz) for the first size in `text`, as a pack total
    (`_stated_total`); None if there is none, if the first size is a weight (grams, pounds, or an
    oz marked as a weight: "Net Wt 16 oz", "21 oz (595 g)", an oz in a text that names a solid or
    an aerosol), or if the total is not > 0.

    A plain "oz" / "ounces" counts as fl oz and a gallon as 128 fl oz (volume mode only; Mafer,
    2026-11-23). A fraction ("1/2 Gallon") or a case notation ("12/19 oz") is not read. For a
    title use `title_volume`, which first refuses ambiguous titles."""
    if text is None:
        return None
    text = str(text)
    tokens = _size_tokens(text)
    if not tokens or tokens[0][0] == UNUSABLE:
        return None
    return _stated_total(text, tokens, tokens[0])


def volume_kind(text):
    """EXPLICIT, GALLON or PLAIN_OZ for the size `parse_volume` reads from `text`; None if none."""
    if text is None:
        return None
    tokens = _size_tokens(str(text))
    return tokens[0][0] if tokens and tokens[0][0] != UNUSABLE else None


# --- Ambiguous titles (volume mode) ------------------------------------------------------------
# Conservative mode (Mafer, 2026-11-23): a title that shows any of these signals is not used for
# the size at all, and the row is dropped. A dropped row is better than a wrong volume, and a
# kit of different products cannot be classified as one product type.
_COUNT_WORD = r"(?:\d{1,3}|two|three|four|five|six|eight|ten|twelve)"
# Things sold next to a liquid that make the row a kit of different products.
_KIT_ACCESSORY = (r"(?:towels?|cloths?|sponges?|mitts?|pads?|gloves?|rags?|buckets?|scrubbers?|dusters?|mops?"
                  r"|wipes|microfib\w*|chamois|squeegees?|clay\s+bars?)")
# With a count they are a kit too; alone they are usually part of the bottle ("with sprayer").
_ACCESSORY = r"(?:" + _KIT_ACCESSORY + r"|brush(?:es)?|applicators?|sprayers?|nozzles?)"
_NOT_A_SIZE = (r"(?!\s*(?:-\s*)?(?:" + _FLOZ + "|" + _GALLON + "|" + _ML + "|" + _LITER + "|" + _OZ + "|"
               + _REJECT + r")(?![a-z]))")  # the number of "1L for Towels" is the size, not a count
_AMBIGUOUS_TITLE_RES = (
    # pack notations the pack parser does not read: "6/Carton", "Case of 6", "Multipack", "Duo",
    # "40 Units" (bottles or wipes?)
    re.compile(r"\d\s*/\s*(?:cartons?|cases?|ctns?|boxe?s?|packs?)\b|\b(?:cases?|cartons?)\s+of\b|\bmulti-?packs?\b"
               r"|\bduo\b|(?<![\w.,-])\d{1,3}\s*units?\b|\bpack\s+\d{1,3}" + _NOT_A_SIZE + r"(?!\s*[x×*\d.,])|\d\s*\+\s*\d", re.I),
    # a kit of different products: "Car Wash Kit", "Bundle", "Gift Set", "Spray + 3 Refills",
    # "with Microfiber Towel", "and 3 Pack of Premium Microfiber Towels", "2 Wash Mitts",
    # "Pack of Ten Towels"
    re.compile(r"\b(?:kits?|bundles?|combo)\b|\bset\b(?!\s+of\b)(?!\s*-)"
               r"|(?:^|\s)\+\s*\d+(?![\d.,])(?!\s*[%x×])"
               r"|(?:\b(?:with|and|plus|includes?|including|incl)\b\.?|\bw/|[+&,/|–]|\s-\s)\s*(?:[\w-]+\s+){0,4}?"
               + _KIT_ACCESSORY + r"\b"
               r"|\b" + _KIT_ACCESSORY + r"\s+included\b"
               r"|(?:\b(?:with|and|plus)\b|[+&])\s*" + _COUNT_WORD + r"\s+refills?\b"
               r"|(?<![\w.,-])(?:pack\s+of\s+)?" + _COUNT_WORD + _NOT_A_SIZE
               + r"(?:\s*-\s*|\s+)(?:(?:packs?|pcs?|pieces|x)\s+)?(?:of\s+)?(?:[a-z-]+\s+){0,2}" + _ACCESSORY
               + r"\b(?!\s+(?:method|wash|system))",
               re.I),
    # a dose, a dilution, a capacity or a yield: "1 oz per Gallon", "1 oz/gallon", "Mix 1 oz",
    # "makes 5 L", "Treats up to 50 Gallons", "for 55 Gallon Tanks", "Capacity", "Makes 8 Bottles",
    # "Fills 2 Bottles"
    re.compile(r"\bper\s+(?:\d[\d.,]*\s*(?:-\s*)?)?(?:gal(?:lon)?s?|l|liters?|litres?|ml)\b"
               r"|/\s*gal(?:lon)?s?\b"
               r"|\b(?:makes?|making|yields?|treats?|covers?|dilutes?\s+to)\s+(?:up\s+to\s+)?(?:about\s+)?"
               r"\d[\d.,]*(?:\s*(?:-|to)\s*\d[\d.,]*)?\s*(?:-\s*)?(?:" + _FLOZ + "|" + _GALLON + "|" + _ML + "|" + _LITER
               + "|" + _OZ + r")(?![a-z])"
               r"|\b(?:makes?|making|fills?|refills|replaces?|yields?|equals?|for)\s+(?:up\s+to\s+)?" + _COUNT_WORD
               + r"\s*(?:bottles?|jugs?|cans|refills?|packs?)\b"
               r"|\bmix(?:es|ing)?\s+\d"
               r"|\b(?:to|with|for|in|into)\s+(?:up\s+to\s+)?\d[\d.,]*\s*(?:-\s*)?gal(?:lon)?s?\b"
               r"|\bgal(?:lon)?s?\s+(?:tanks?|aquariums?|ponds?|pools?|of\s+water)\b"
               r"|\bcapacity\b", re.I),
    # "2X 150 oz": a concentration ("2X Ultra") or two bottles? Case-sensitive on purpose.
    re.compile(r"(?<![\w.,])\d{1,2}X\s+\d"),
)
_KIT_RE = _AMBIGUOUS_TITLE_RES[1]
# "Fabric Softener 3L for Bedding and Towels", "Safe for Microfiber Towels": a use, not a kit.
_USE_BEFORE_RE = re.compile(r"\b(?:for|safe)\b", re.I)
# A gallon container is not a liquid: "Car Wash Bucket 5 Gallon", "Pump Sprayer 2 Gallon".
_CONTAINER_RE = re.compile(r"\b(?:buckets?|sprayers?|fountains?|dispensers?|containers?|empty)\b", re.I)
_BETWEEN_SIZES_RE = re.compile(r"\b(?:and|plus|with)\b|[+&]", re.I)
_WORD_RE = re.compile(r"[^\W\d_]{2,}")
# The only words that may stand between two sizes of one title: any other word names a second
# product, a scent or a variant ("Cleaner 250 ml, Conditioner 250 ml", "1L Blue, 1L Pink").
_FILLER_WORDS = {"pack", "packs", "of", "total", "each", "per", "twin", "double", "dual", "triple", "fl", "oz",
                 "ml", "the", "in", "approx", "approximately", "equals", "equal", "net", "is", "or", "pcs",
                 "fluid", "ounce", "ounces", "liter", "liters", "litre", "litres", "gallon", "gallons", "us"}
_MAX_TITLE_SIZES = 8


def _sizes_conflict(text):
    """True if the title states two sizes that are not the same thing said twice: "500ml and 1L
    Refill", "16 oz Shampoo & 16 oz Conditioner", "Leather Cleaner 250 ml, Leather Conditioner 250
    ml", "1L Blue, 1L Pink", "30ml to 1L Water, 500ml Bottle". The same size in two units ("24oz -
    709ml", "4 L (1 Gallon)", "2 x 500 ml (16.9 fl oz)") or a pack and its total ("3 x 500 ml (1.5
    L)", "500ml, Pack of 3 (1.5 L total)") is not a conflict. Any word other than a pack or unit
    word between two sizes is."""
    tokens = [t for t in _size_tokens(text) if t[0] != UNUSABLE]
    if len(tokens) < 2:
        return False
    if len(tokens) > _MAX_TITLE_SIZES:
        return True  # a keyword-stuffed title; also bounds the work below
    pack = _volume_phrase_pack(text)
    seen = []  # every earlier size, as its total and as one unit
    for index, token in enumerate(tokens):
        unit, total, tied = _token_value(text, token)
        one = (unit, token[2])
        if index:
            between_text = text[tokens[index - 1][3].end():token[3].start()]
            if _BETWEEN_SIZES_RE.search(between_text):
                return True
            if any(w.lower() not in _FILLER_WORDS for w in _WORD_RE.findall(between_text)):
                return True
            mine = [(unit, total), one]
            if not any(_same(m, s) for m in mine for s in seen):
                if not (pack > 1 and any(_same(m, (s[0], s[1] * pack)) or _same((m[0], m[1] * pack), s)
                                         for m in mine for s in seen)):
                    return True
        seen += [(unit, total), one]
    return False


def _is_kit(text):
    """A kit match that is not a use clause ("for Bedding and Towels", "Safe for ... Towels")."""
    for match in _KIT_RE.finditer(text):
        if not _USE_BEFORE_RE.search(text[max(0, match.start() - 40):match.end()]):
            return True
    return False


def title_is_ambiguous(text):
    """True if a title must not be used for the size (volume mode) and its row is dropped: a count
    of 2 or more ("28 oz, 4 Count"), a pack notation the parser does not read ("6/Carton", "Case
    of 6", "40 Units"), a kit of different products ("Kit", "Bundle", "+ 3 Refills", "with
    Microfiber Towel", "2 Wash Mitts"), a dose, dilution, capacity or yield ("1 oz per Gallon",
    "makes 5 L", "Treats up to 50 Gallons", "Fills 2 Bottles"), "2X 150 oz", or two sizes that are
    not the same thing said twice (`_sizes_conflict`)."""
    text = str(text or "")
    if has_count(text) or _is_kit(text):
        return True
    if any(pattern.search(text) for i, pattern in enumerate(_AMBIGUOUS_TITLE_RES) if i != 1):
        return True
    if _CONTAINER_RE.search(text) and any(t[0] == GALLON for t in _size_tokens(text)):
        return True
    return _sizes_conflict(text)


def title_volume(text, explicit_only=False):
    """The size of a title, or None if the title is ambiguous (`title_is_ambiguous`).

    With `explicit_only` (a title with no other source to confirm it: Noon, and Amazon's
    title-only rule) a plain oz or a gallon is not enough (Mafer, 2026-11-23): only a size in ml,
    liters or fl oz is returned ("Spray 32 oz" -> None, "24oz - 709ml" -> 709 ml)."""
    if text is None:
        return None
    text = str(text)
    if title_is_ambiguous(text):
        return None
    tokens = _size_tokens(text)
    if not tokens or tokens[0][0] == UNUSABLE:
        return None
    chosen = next((t for t in tokens if t[0] == EXPLICIT), None) if explicit_only else tokens[0]
    return _stated_total(text, tokens, chosen) if chosen else None


# --- Count and pack ---------------------------------------------------------------------------

COUNT_NOUNS = r"(?:wet\s+)?(?:wipes|sheets|count|ct|pcs|pieces|pc|cloths|rags|sachets|brushes|units)"
_VOLUME_UNITS = _FLOZ + "|" + _ML + "|" + _LITER
# Volume mode also knows oz and gallons (`volume_pack`); units mode (`pack_count`) does not.
_VOLUME_UNITS_ALL = _FLOZ + "|" + _GALLON + "|" + _ML + "|" + _LITER + "|" + _OZ
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
# The same two, volume mode: oz, fl oz and gallons too ("3 x 16 oz", "19 fl oz x 3"), and "*" as
# the multiplier ("500ML*2").
_VOLUME_NOT_DIM = r"|\s*[x×*]\s*\d"
_VOLUME_PACK_AXB_RE = re.compile(
    r"(?<![\d.,])(\d{1,3})\s*[x×*]\s*(?=" + _NUM_PLAIN + _SEP + r"(?:" + _VOLUME_UNITS_ALL + r")(?![a-z]))", re.I)
_END_N = r"(?!\d|[.,]\d)"  # "x 2," and "x 2." are a pack; "x 2.5" is not
_VOLUME_PACK_SIZE_X_N_RE = re.compile(
    r"(?:\d" + _SEP + r"(?:" + _VOLUME_UNITS_ALL + r"))\s*[x×*]\s*(\d{1,3})" + _END_N + r"(?!" + _LENGTH
    + _VOLUME_NOT_DIM + ")", re.I)
# Volume-mode pack phrases: a multipack of the same product is kept with its total (Mafer,
# 2026-11-23). On top of the units-mode phrases: word numbers ("Pack of Two", "Six Pack"),
# "2 Packs", "2 Jugs", "Twin Pack" / "Twinpack" / "Double Pack" (2), "Triple Pack" (3). The number
# must stand alone: the "40" of "WD-40 Cans" is not a pack.
_WORD_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10, "twelve": 12}
_N = r"(\d{1,3}|" + "|".join(_WORD_NUMBERS) + ")"
_VOLUME_PACK_PHRASES = [
    re.compile(r"\b(?:pack|set|box)\s+of\s+" + _N + r"\b(?!\s*" + COUNT_NOUNS + r")", re.I),
    re.compile(r"(?<![\w.,-])" + _N + r"[\s-]*packs?\b", re.I),
    re.compile(r"(?<![\w.,-])" + _N + r"\s*(?:bottles|cans|pouches|refills|jugs)\b", re.I),
    re.compile(r"(?:[)\],]|\s-|\beach)\s*[x×*]\s*(\d{1,3})" + _END_N + r"(?!" + _LENGTH + _VOLUME_NOT_DIM + ")", re.I),
    re.compile(r"\(\s*[x×*]\s*(\d{1,3})\s*\)", re.I),
]
_TWIN_PACK_RE = re.compile(r"\b(?:twin|double|dual)[\s-]*pack\b|\btwinpack\b", re.I)
_TRIPLE_PACK_RE = re.compile(r"\b(?:triple|tri)[\s-]*pack\b", re.I)
_VOLUME_PACK_BEFORE_RE = re.compile(r"(?<![\d.,])(\d{1,3})\s*[x×*]\s*$", re.I)
_VOLUME_PACK_AFTER_RE = re.compile(r"\s*[x×*]\s*(\d{1,3})" + _END_N + r"(?!" + _LENGTH + _VOLUME_NOT_DIM + ")", re.I)
# "28 oz, 4 Count": the count may be bottles, pods, wipes or a bundled accessory, so a title with
# a count of 2 or more is ambiguous for its size (Amazon: `amazon_size` does not use it).
_TITLE_COUNT_RE = re.compile(r"(?<![\d.,])(\d{1,4})(?:\.0+)?[\s-]*(?:count|ct|pk|pcs|pieces)\b", re.I)
# Tied to a count token: " x N" right after it.
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


def has_count(text):
    """True if `text` states a count of 2 or more ("4 Count", "16 ct", "12 pk", "6 pcs")."""
    return any(int(m.group(1)) >= 2 for m in _TITLE_COUNT_RE.finditer(str(text or "")))


def _volume_phrase_pack(text):
    """Volume mode: the first pack phrase of `_VOLUME_PACK_PHRASES`, else Twin (2) / Triple (3)
    Pack; 1 if none."""
    for pattern in _VOLUME_PACK_PHRASES:
        match = pattern.search(text)
        if match:
            word = match.group(1).lower()
            if word in _WORD_NUMBERS:
                return _WORD_NUMBERS[word]
            try:
                return _valid_pack(int(word))
            except ValueError:  # a case variant such as "SİX": not a number we read
                return 1
    if _TWIN_PACK_RE.search(text):
        return 2
    if _TRIPLE_PACK_RE.search(text):
        return 3
    return 1


def _tied_pack(text, match):
    """The "N x <token>" or "<token> x N" multiplier of the size token `match` (volume mode: "x",
    "×" or "*"), or None.

    Only a multiplier attached to the token that was used counts: in "1.5 L (3 x 500 ml)" the
    "3 x" belongs to the 500 ml breakdown, not to the 1.5 L total."""
    before = _VOLUME_PACK_BEFORE_RE.search(text[max(0, match.start() - 16):match.start()])
    if before:
        return _valid_pack(int(before.group(1)))
    after = _VOLUME_PACK_AFTER_RE.match(text, match.end())
    if after:
        return _valid_pack(int(after.group(1)))
    return None


def _size_pack(text, match):
    tied = _tied_pack(text, match)
    return tied if tied is not None else _volume_phrase_pack(text)


def pack_count(text):
    """Pack multiplier in 1..100 (1 when there is none or it is out of range).

    Order: the pack phrases ("Pack of N", "N pack", "N bottles") first, then "N x <volume or
    count>", then "<volume> x N". A dimension ("40 x 40 cm") is never a pack. Units mode: ml and
    liters only (oz and gallons are volume mode, `volume_pack`)."""
    if text is None:
        return 1
    text = str(text)
    for pattern in _PACK_PHRASES + [_PACK_AXB_RE, _PACK_SIZE_X_N_RE]:
        match = pattern.search(text)
        if match:
            return _valid_pack(int(match.group(1)))
    return 1


def volume_pack(text):
    """Volume-mode pack multiplier of a title (used by Amazon's size rule): the volume pack
    phrases (`_volume_phrase_pack`), then "N x <volume>" and "<volume> x N" with oz, fl oz and
    gallons too; 1 if none."""
    if text is None:
        return 1
    text = str(text)
    phrase = _volume_phrase_pack(text)
    if phrase > 1:
        return phrase
    for pattern in (_VOLUME_PACK_AXB_RE, _VOLUME_PACK_SIZE_X_N_RE):
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


def first_agreeing(parsed, allowed=None):
    """`(index, size)` for the first element of the first agreeing pair of already parsed sizes
    (None entries skipped), in order; None when fewer than 2 parse or no pair agrees. A pair of
    original indexes `(i, j)` only counts if `allowed(i, j)` is true (when given)."""
    present = [(i, p) for i, p in enumerate(parsed) if p is not None]
    for a in range(len(present)):
        for b in range(a + 1, len(present)):
            if allowed is not None and not allowed(present[a][0], present[b][0]):
                continue
            if sizes_agree(present[a][1], present[b][1]):
                return present[a]
    return None


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
_KSA_COUNTRY_RE = re.compile(r"\bsaudi\w*|\bk\.?s\.?a\b|السعودي[ةه]", re.I)
# A value that names no country is not a country of origin.
_NO_COUNTRY = {"unknown", "na", "none", "null", "no", "yes", "tbd", "not applicable", "not specified",
               "not available", "not known", "not mentioned", "unspecified", "other", "others", "various",
               "multiple", "multiple countries", "local", "imported", "see package", "see packaging", "see label"}
MADE_IN_KSA_RE = re.compile(
    r"made\s+in\s+(?:the\s+)?(?:ksa|saudi(?:\s+arabia)?|kingdom\s+of\s+saudi\s+arabia)"
    r"|صنع\s+في\s+(?:المملكة\s+العربية\s+)?السعودية",
    re.I,
)


def derive_origin(*, country_of_origin=None, texts=()):
    """'local', 'imported' or 'unknown', in this precedence: country of origin, a "made in KSA"
    text; anything else is unknown.

    Neither a size in fl oz nor Noon's `shipping_banner_details.global` is a signal (Mafer,
    2026-11-23): a fl oz size does not prove a product is imported, and the 2026-11-23 smoke run
    showed the same generic Noon fee message on every product, local ones included."""
    country = clean(country_of_origin).lower()
    if " ".join(re.sub(r"[^\w\s]", "", country).split()) in _NO_COUNTRY or not any(ch.isalpha() for ch in country):
        country = ""  # "Unknown", "N/A", "N.A.", "-" name no country
    if country:
        return "local" if country in KSA_NAMES or _KSA_COUNTRY_RE.search(country) else "imported"
    if any(text and MADE_IN_KSA_RE.search(str(text)) for text in texts or ()):
        return "local"
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
    row["Origin"] = derive_origin(**candidate["_origin"])
    return {key: row[key] for key in ROW_KEYS}
