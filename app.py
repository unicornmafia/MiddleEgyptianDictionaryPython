import io
import json
import os
import re
import sys
import traceback
import unicodedata
import uuid

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGO_DB", "MiddleEgyptianDictionary")
ENTRIES_COLLECTION = os.environ.get("ENTRIES_COLLECTION", "DictionaryEntry")
KEYWORDS_COLLECTION = os.environ.get("KEYWORDS_COLLECTION", "KeywordSearch")

DEFAULT_PAGE_SIZE = 50
MAX_RESULTS = 500

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCES_PATH = os.path.join(BASE_DIR, "static", "Resources")
UNKNOWN_GLYPHS_PATH = os.path.join(BASE_DIR, "static", "Content", "unknown_glyphs.txt")
SEARCH_CACHE_DIR = os.path.join(BASE_DIR, "search_cache")

# ---------------------------------------------------------------------------
# Unknown glyphs
# ---------------------------------------------------------------------------

def _load_unknown_glyphs():
    try:
        if os.path.exists(UNKNOWN_GLYPHS_PATH):
            with open(UNKNOWN_GLYPHS_PATH, "r") as f:
                return [line.strip() for line in f if line.strip()]
    except OSError:
        pass
    return []

UNKNOWN_GLYPHS = _load_unknown_glyphs()

# ---------------------------------------------------------------------------
# Gardiner / transliteration converters
# ---------------------------------------------------------------------------

def _fix_unicode_name(name: str) -> str:
    """Remove leading zeros from a Unicode hieroglyph name suffix, e.g. 'A001' -> 'A1'."""
    first_zero = name.find("0")
    if first_zero == -1:
        return name
    result = name[:first_zero]
    for i in range(first_zero, len(name)):
        if name[i] != "0":
            result += name[i:]
            break
    return result


def _build_gardiner_converter() -> dict:
    converter = {}
    for codepoint in range(0x13000, 0x1342F):
        try:
            char = chr(codepoint)
            name = unicodedata.name(char)  # e.g. "EGYPTIAN HIEROGLYPH A001"
            suffix = name.split()[-1]
            key = _fix_unicode_name(suffix).upper()
            converter[key] = char
        except ValueError:
            pass
    return converter


GARDINER_CONVERTER = _build_gardiner_converter()

TRANSLIT_MAP = {
    "A": chr(0xA722),  # Ꜣ  Egyptological Alef
    "a": chr(0xA724),  # Ꜥ  Egyptological Ain
    "H": chr(0x1E25),  # ḥ  h with dot below
    "x": chr(0x1E2B),  # ḫ  h with breve below
    "X": chr(0x1E96),  # ẖ  h with line below
    "S": chr(0x0161),  # š  s with caron
    "T": chr(0x1E6F),  # ṯ  t with line below
    "D": chr(0x1E0F),  # ḏ  d with line below
}


_GARD_TOKEN_RE = re.compile(r'^(?:AA|[A-Z][a-z]?)\d+[A-Za-z]*$')
_GARD_SUFFIX_RE = re.compile(r'(?<=\d)([A-Za-z]+)$')
_MDC_POSTFIX_RE = re.compile(r'((?:Aa|AA|[A-Z])\d+)([A-Za-z]+)')
_RES_CODE_RE = re.compile(r'(?:Aa|[A-Za-z])\d+[a-z]*')

# Translation table ported from ManuelDeCodageToRESConverter.cs
_MDC_TRANSLATION_DIC: dict[str, str] = {
    "D153": "D26", "O38A": "O38[mirror]",
    "R8A": "R8*[sep=0,fix]R8*[sep=0,fix]R8", "S56": "S7",
    "S14B": "S14b", "U6A": "U6a", "U6B": "U6b", "V30A": "V30a",
    "V40A": "V40a", "W3A": "W3a", "W14A": "W14a", "W24A": "W24a",
    "T26F": "T26", "T26H": "T26", "A51&X1": "insert[b,sep=0.2](A51,X1)",
    "Y1V": "Y1a",
    # Vygus errors
    "A33B": "A33", "A95": "A15", "A21A": "A21", "A34A": "A34",
    "A36C": "A37", "A4B": "A4", "A4C": "A4", "A43B": "A41",
    "A299B": "A41", "A40B": "A42", "A59B": "A59", "A133A": "A6",
    "N90": "D12", "Y24": "D12", "D26A": "D26", "D210": "D36",
    "D46D": "D46", "E102B": "E4", "F16A": "F16[mirror]",
    "F37D": "F37a", "F37E": "F37a", "F37AA": "F37a", "F37B": "F37a",
    "F37F": "F37a", "F37J": "F37a", "F39A": "F39", "F51D": "F51",
    "F51F": "F51", "G22A": "G22", "G29A": "G29", "G49E": "G49",
    "G237": "G49", "G167": "G50", "G7C": "G7",
    "H6B": "H6[rotate=330]", "I14A": "I14", "I14B": "I14",
    "I14C": "I14", "O202": "M13", "O353": "M13", "M4B": "M4",
    "M7A": "M7", "N11A": "N11[rotate=90]", "N62A": "N12[rotate=180]",
    "N21A": "N21[mirror]", "N24E": "N24", "S106": "N37", "N8A": "N8",
    "O29V": "O29[rotate=90]", "O30U": "O30[rotate=180]",
    "O40A": "O40", "O48A": "O48", "P4A": "P4", "P30": "P4",
    "P34": "P4", "P36": "P4", "Q12A": "Q2", "R1E": "R1",
    "R10B": "R10", "R15A": "R15", "R3C": "R3", "R3P": "R3",
    "S15A": "S15", "S116": "S27:S27",
    "G56": "stack(A,a)", "G57": "stack(A,f)", "V71": "stack(a,H)",
    "G58": "stack(Aa15,A)", "M145": "stack(Aa15,i)",
    "I34": "stack(b,D)", "D170A": "stack(D28,D52)",
    "D189": "stack(D32, W24)", "V41": "stack(D37,Z7)",
    "G225": "stack(G29,U7)", "V81": "stack(k,H)",
    "V90": "stack(k,V29)", "G87": "stack(m,D40)",
    "M159": "stack(M23, a)", "O91": "stack(O6, a)",
    "O90": "stack(O6, O29)", "R78": "stack(R15, a)",
    "T69": "stack(T14, a)", "T29C": "stack(T28, T30)",
    "U1A": "stack(U1,i)", "T14C": "T14[mirror]",
    "T19B": "T19", "T79": "T19", "T21V": "T21[rotate=270]",
    "T24E": "T24[mirror]", "T30A": "T30", "T30B": "T30A",
    "S123": "T32A", "S126": "T32A", "T9C": "T9", "U19A": "U19",
    "U32B": "U32", "U39Q": "U39", "U39L": "U40", "U7A": "U8",
    "S89": "V1", "V36G": "V36", "V5A": "V5[rotate=330]",
    "W15B": "W15", "W21A": "W21", "X3A": "X3", "Y2V": "Y1A",
    "Y8A": "Y8", "Z11A": "Z11",
    # Numbers
    "Z15A": "Z1*Z1", "Z15B": "Z1*Z1*Z1", "Z15C": "Z1*Z1*Z1*Z1",
    "Z15I": ".*[sep=0]Z1*Z1*[sep=0].:Z1*Z1*Z1", "Z15": "Z1",
    "V20I": "V20*V20", "V20J": "V20*V20*V20",
    "V20K": "V20*V20*V20*V20",
    "V20L": ".*[sep=0]V20*V20*[sep=0].:V20*V20*V20",
    "100": "V1", "V1A": "V1*V1", "V1B": "V1*V1*V1",
    "V1C": "V1*V1*V1*V1",
    "V1I": ".*[sep = 0]V1*V1*[sep=0].:V1*V1*V1", "1000": "M12",
    "D50A": "D50*[fix,sep=0.2]D50",
    "D50B": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50",
    "D50C": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50",
    "D50D": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50:[fix,sep=0.2].*[sep=0]D50*[fix,sep=0.2]D50*[sep=0].",
    "D50E": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50:[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50",
    "D50F": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50:[fix,sep=0.2,size=inf].*[sep=0.2] D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[sep=0.2].",
    "D50G": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50:[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50",
    "D50H": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50:[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50:[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50",
    "D50I": "D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50*[fix,sep=0.2]D50",
    "D67A": "D67:[fix,sep=0.3]D67",
    "D67B": "D67*[fix,sep=0.3]D67:[fit,fix,sep=0.3]D67",
    "D67C": "D67*[fix,sep=0.3]D67:[fix,sep=0.3]D67*[fix,sep=0.3]D67",
    "D67D": "D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67:[fit,fix,sep=0.3]D67*[fix,sep=0.3]D67",
    "D67E": "D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67:[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67",
    "D67F": "D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67:[fit,fix,sep=0.3,size=inf]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67",
    "D67G": "D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67:[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67",
    "D67H": "D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67:[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67:[fix,sep=0.3]D67*[fix,sep=0.3]D67*[fix,sep=0.3]D67",
    "F51A": "F51*[fit,fix,sep=0.2]F51*[fit,fix,sep=0.2]F51",
    "F51B": "F51:[fit,fix,sep=0.2]F51:[fit,fix,sep=0.2]F51",
    "M12A": "M12:[fix,sep=0.3]M12",
    "M12B": "M12*[fix,sep=0.3]M12:[fit,fix,sep=0.3]M12",
    "M12C": "M12*[fix,sep=0.3]M12:[fix,sep=0.3]M12*[fix,sep=0.3]M12",
    "M12D": "M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12:[fit,fix,sep=0.3]M12*[fix,sep=0.3]M12",
    "M12E": "M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12:[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12",
    "M12F": "M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12:[fit,fix,sep=0.3,size=inf]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12",
    "M12G": "M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12:[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12",
    "M12H": "M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12:[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12:[fix,sep=0.3]M12*[fix,sep=0.3]M12*[fix,sep=0.3]M12",
    "N33A": "N33*[sep=0.5]N33*[sep=0.5] N33",
    "A14&Z2": "insert[bs,sep=0.5](A14,Z1*[sep=1.5]Z1*[sep=1.5]Z1)",
    "A17&Z2": "insert[be](A17*[sep=0.0]empty[width=0.2,height=0.0],Z1*Z1*Z1)",
    "A24&Z2D": "insert[te,sep=0.5](A24*[sep=0.0]empty[width=0.3],Z1*[sep=0.5]Z1*[sep=0.5]Z1)",
    "Z2A": "Z1*Z1*Z1", "Z2B": "D67*[sep=0.5]D67*[sep=0.5]D67",
    "Z2C": "Z1:[fix,sep=0.3]Z1*[sep=2.0]Z1",
    "Z2D": "Z1*[sep=2.0]Z1:[fix,sep=0.3]Z1",
    "Z2": "Z1*[sep=2.0]Z1*[sep=2.0]Z1",
    "Z3A": "Z1[rotate=90]:Z1[rotate=90]:Z1[rotate=90]",
    "Z3B": "D67:D67:D67", "Z3": "Z1:[sep=0.3]Z1:[sep=0.3]Z1",
    "Z4A": "Z1*[sep=2]Z1", "Z4B": "Z1[rotate=90]:Z1[rotate=90]",
}
# Sort keys by length descending so more-specific codes match before shorter ones
_MDC_TRANSLATION_KEYS = sorted(_MDC_TRANSLATION_DIC, key=len, reverse=True)


def _mdc_to_res(mdc: str) -> str:
    """Apply MdC→RES token substitutions, & → :, and postfix-letter lowercasing."""
    result = mdc
    for k in _MDC_TRANSLATION_KEYS:
        result = result.replace(k, _MDC_TRANSLATION_DIC[k])
    result = result.replace('&', ':')
    result = _MDC_POSTFIX_RE.sub(lambda m: m.group(1) + m.group(2).lower(), result)
    return result


def _load_gardiner_to_mdc() -> dict:
    path = os.path.join(BASE_DIR, "static", "gardiner_to_mdc.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except OSError:
        return {}


GARDINER_TO_MDC = _load_gardiner_to_mdc()


def _build_res_known_signs() -> set:
    """Extract the set of sign codes known to the RES library from res_signinfo.js."""
    path = os.path.join(BASE_DIR, "static", "Scripts", "res", "res_signinfo.js")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return set(re.findall(r'^([A-Za-z0-9]+):', content, re.MULTILINE))
    except OSError:
        return set()


RES_KNOWN_SIGNS = _build_res_known_signs()


def _normalize_gardiner_token(tok: str) -> str | None:
    """Normalize a raw Gardiner token for RES use. Returns None if not a valid token."""
    if not _GARD_TOKEN_RE.match(tok):
        return None
    # J-series → Aa-series (e.g. J8 → Aa8)
    if tok[0] == 'J' and (len(tok) < 2 or not tok[1].isalpha()):
        tok = 'Aa' + tok[1:]
    # AA-series (uppercase) → Aa-series (e.g. AA1 → Aa1, AA15 → Aa15)
    elif tok.startswith('AA') and len(tok) > 2 and tok[2].isdigit():
        tok = 'Aa' + tok[2:]
    # Lowercase any trailing letter suffix after the digits (N35A→N35a, T14CB→T14cb)
    tok = _GARD_SUFFIX_RE.sub(lambda m: m.group(1).lower(), tok)
    return tok


def gardiner_to_res(gardiner_signs: str) -> str:
    """Convert space-separated Gardiner codes to RES canvas format with proper grouping."""
    key = ' '.join(gardiner_signs.upper().split())
    mdc = GARDINER_TO_MDC.get(key)
    if mdc:
        return _mdc_to_res(mdc)
    # Fallback: flat horizontal join of RES-known tokens
    tokens = []
    for tok in gardiner_signs.split():
        normalized = _normalize_gardiner_token(tok)
        if normalized and normalized in RES_KNOWN_SIGNS:
            tokens.append(normalized)
    return '-'.join(tokens)


def gardiner_render_items(gardiner_signs: str) -> list:
    """Return render items for a Gardiner sign sequence.

    When all signs are in the MdC grouping lookup, returns a single canvas item
    with proper stacking/grouping operators. Otherwise splits into canvas runs
    and image fallbacks per sign.
    """
    key = ' '.join(gardiner_signs.upper().split())
    mdc = GARDINER_TO_MDC.get(key)

    if mdc:
        res_str = _mdc_to_res(mdc)
        codes = _RES_CODE_RE.findall(res_str)
        if codes and all(c in RES_KNOWN_SIGNS for c in codes):
            return [{'kind': 'canvas', 'value': res_str}]

    # Fallback: per-sign canvas runs with image fallbacks for unknown signs
    items: list = []
    canvas_run: list = []

    def _flush():
        if canvas_run:
            items.append({'kind': 'canvas', 'value': '-'.join(canvas_run)})
            canvas_run.clear()

    for tok in gardiner_signs.split():
        normalized = _normalize_gardiner_token(tok)
        if normalized is None:
            continue
        if normalized in RES_KNOWN_SIGNS:
            canvas_run.append(normalized)
        else:
            _flush()
            items.append({'kind': 'img', 'key': tok})

    _flush()
    return items


def fix_incongruent_lettering(glyph: str) -> str:
    answer = glyph
    if answer in ("Y1v", "Y1V"):
        answer = "Y1A"
    if answer.startswith("J"):
        answer = "AA" + glyph[1:]
    if answer.upper() not in GARDINER_CONVERTER and answer:
        last = answer[-1]
        if last.isalpha():
            shorter = answer[:-1]
            if shorter.upper() in GARDINER_CONVERTER:
                answer = shorter
    return answer


def convert_gardiner(input_str: str) -> str:
    parts = []
    for glyph in input_str.split(" "):
        fixed = fix_incongruent_lettering(glyph)
        char = GARDINER_CONVERTER.get(fixed.upper())
        parts.append(char if char else f" {fixed} ")
    return "".join(parts)


def prettify_transliteration(input_str: str) -> str:
    result = input_str
    for letter, replacement in TRANSLIT_MAP.items():
        result = result.replace(letter, replacement)
    return result

# ---------------------------------------------------------------------------
# MongoDB helpers
# ---------------------------------------------------------------------------

_mongo_client = None
_mongo_db = None


def get_db():
    global _mongo_client, _mongo_db
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        _mongo_db = _mongo_client[DB_NAME]
    return _mongo_db


def entries_collection():
    return get_db()[ENTRIES_COLLECTION]


def keywords_collection():
    return get_db()[KEYWORDS_COLLECTION]

# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------

_CASE_SENSITIVE_CHARS = set("AaSsHhXxDdTt")


def _sanitize_transliteration(query: str) -> str:
    query = "".join(c if c in _CASE_SENSITIVE_CHARS else c.lower() for c in query)
    return query.replace("j", "i")


def _sanitize_keywords(query: str) -> list[str]:
    words = re.split(r"\W+", query.lower())
    return [w for w in words if w]


def _search_by_transliteration(query: str, exact: bool) -> list:
    col = entries_collection()
    if exact:
        return list(col.find({"Transliteration": query}))
    return list(col.find({"Transliteration": {"$regex": re.escape(query)}}))


def _search_by_gardiner(query: str, exact: bool) -> list:
    col = entries_collection()
    if exact:
        return list(col.find({"GardinerSigns": query}))
    return list(col.find({"GardinerSigns": {"$regex": re.escape(query)}}))


def _search_by_translation(query: str, exact: bool) -> list:
    keywords = _sanitize_keywords(query)
    if not keywords:
        return []

    kw_col = keywords_collection()
    pipeline = [
        {"$match": {"Keyword": {"$in": keywords}}},
        {"$unwind": "$EntryIds"},
        {"$group": {"_id": "$EntryIds", "Count": {"$sum": 1}}},
    ]
    if exact:
        pipeline.append({"$match": {"Count": len(keywords)}})
    else:
        pipeline.append({"$sort": {"Count": -1}})

    pipeline += [
        {
            "$lookup": {
                "from": ENTRIES_COLLECTION,
                "localField": "_id",
                "foreignField": "_id",
                "as": "DictionaryEntry",
            }
        },
        {"$unwind": "$DictionaryEntry"},
        {"$replaceRoot": {"newRoot": "$DictionaryEntry"}},
    ]

    return list(kw_col.aggregate(pipeline))


def _search_all(query: str, exact: bool) -> list:
    col = entries_collection()
    if exact:
        sign_translit = list(
            col.find(
                {"$or": [{"Transliteration": query}, {"GardinerSigns": query}]}
            )
        )
    else:
        sign_translit = list(
            col.find(
                {
                    "$or": [
                        {"Transliteration": {"$regex": re.escape(query)}},
                        {"GardinerSigns": {"$regex": re.escape(query)}},
                    ]
                }
            )
        )
    seen_ids = {e["_id"] for e in sign_translit}
    translation_results = [e for e in _search_by_translation(query, exact) if e["_id"] not in seen_ids]
    # Return as two groups: sign/translit matches always precede translation-only matches.
    # _sort_results is applied within each group so ranking stays coherent but a
    # keyword-only hit can never jump ahead of a direct sign/transliteration match.
    return _sort_results(sign_translit) + _sort_results(translation_results)


def _sort_results(results: list) -> list:
    def _key(entry):
        unique_sources = set()
        for t in entry.get("Translations", []):
            for m in t.get("TranslationMetadata", []):
                unique_sources.add(m.get("DictionaryName"))
        has_faulkner = 4 in unique_sources
        return (not has_faulkner, -len(unique_sources), entry.get("Transliteration", ""), entry.get("GardinerSigns", ""))

    return sorted(results, key=_key)


def _filter_by_sources(results: list, sources: list[int]) -> list:
    if not sources or len(sources) >= len(_DATASOURCE_NAMES):
        return results
    source_set = set(sources)
    filtered = []
    for entry in results:
        for t in entry.get("Translations", []):
            if any(m.get("DictionaryName") in source_set for m in t.get("TranslationMetadata", [])):
                filtered.append(entry)
                break
    return filtered


def conduct_search(search_type: str, query: str, sign_query: str, exact: bool,
                   selected_sources: list[int] | None = None) -> list:
    if search_type == "transliteration":
        query = _sanitize_transliteration(query)
        results = _sort_results(_search_by_transliteration(query, exact))
    elif search_type == "translation":
        results = _sort_results(_search_by_translation(query, exact))
    elif search_type == "gardiner":
        results = _sort_results(_search_by_gardiner(sign_query, exact))
    else:
        results = _search_all(query, exact)
    if selected_sources is not None:
        results = _filter_by_sources(results, selected_sources)
    return results

# ---------------------------------------------------------------------------
# Result cache (file-backed, keyed by UUID stored in session)
# ---------------------------------------------------------------------------

def _save_results(results: list) -> str:
    os.makedirs(SEARCH_CACHE_DIR, exist_ok=True)
    search_id = str(uuid.uuid4())
    path = os.path.join(SEARCH_CACHE_DIR, f"{search_id}.json")
    # Strip MongoDB ObjectId to string
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, default=str)
    return search_id


def _load_results(search_id: str) -> list | None:
    if not search_id:
        return None
    path = os.path.join(SEARCH_CACHE_DIR, f"{search_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# DataSource helpers
# ---------------------------------------------------------------------------

_DATASOURCE_NAMES = {
    0: "dickson",
    1: "vygus",
    2: "lexicon",
    4: "faulkner",
    5: "collier_manley",
    6: "allen",
    7: "hoch",
    8: "kamrin",
    9: "gardiner_grammar",
    10: "evans",
    11: "faulkner_revised",
    12: "vygus_2012",
}
_DATASOURCE_DISPLAY = {
    "dickson": "Dickson",
    "vygus": "Vygus",
    "lexicon": "Lexicon",
    "faulkner": "Faulkner",
    "collier_manley": "Collier & Manley",
    "allen": "Allen",
    "hoch": "Hoch",
    "kamrin": "Kamrin",
    "gardiner_grammar": "Gardiner Grammar",
    "evans": "Evans",
    "faulkner_revised": "Faulkner (Revised)",
    "vygus_2012": "Vygus (2012)",
}
_DATASOURCE_COLORS = {
    "faulkner": "lightblue",
    "vygus": "thistle",
    "dickson": "lightpink",
    "lexicon": "moccasin",
    "collier_manley": "lightgreen",
    "allen": "lightyellow",
    "hoch": "lavender",
    "kamrin": "peachpuff",
    "gardiner_grammar": "honeydew",
    "evans": "aliceblue",
    "faulkner_revised": "lightcyan",
    "vygus_2012": "plum",
}


def datasource_name(value) -> str:
    if isinstance(value, int):
        return _DATASOURCE_NAMES.get(value, str(value))
    return str(value).lower()


def datasource_display(name: str) -> str:
    return _DATASOURCE_DISPLAY.get(name.lower(), name.replace("_", " ").title())


def datasource_color(name: str) -> str:
    return _DATASOURCE_COLORS.get(name.lower(), "lightgray")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    error = session.pop("error", None)
    return render_template("index.html", error=error)


@app.route("/submit", methods=["POST"])
def submit():
    try:
        query = request.form.get("Query", "")
        sign_query = request.form.get("SignQuery", "")
        exact_match = request.form.get("ExactMatch", "false").lower() in ("true", "1", "on")
        search_type = request.form.get("Type", "transliteration")
        display_formatted = request.form.get("DisplayFormatted", "true").lower() in ("true", "1", "on")

        sources_str = request.form.get("SelectedSources", "").strip()
        if sources_str:
            try:
                selected_sources: list[int] | None = [int(x) for x in sources_str.split(",") if x.strip()]
            except ValueError:
                selected_sources = None
        else:
            selected_sources = None

        results = conduct_search(search_type, query, sign_query, exact_match, selected_sources)
        total_count = len(results)
        limited = results[:MAX_RESULTS]

        # Serialize and cache results
        serialized = [{k: v for k, v in r.items() if k != "_id"} for r in limited]
        search_id = _save_results(serialized)

        session["search_id"] = search_id
        session["display_formatted"] = display_formatted
        session["total_original"] = total_count
        session["selected_sources"] = selected_sources

        return redirect(url_for("results", page=1))
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        session["error"] = str(e)
        return redirect(url_for("index"))


@app.route("/results")
def results():
    search_id = session.get("search_id")
    all_results = _load_results(search_id)
    display_formatted = session.get("display_formatted", True)
    total_original = session.get("total_original", 0)

    if all_results is None:
        return render_template("index.html", error="Session expired or no search has been run yet.")

    total = len(all_results)
    total_pages = max(1, -(-total // DEFAULT_PAGE_SIZE))  # ceiling division
    page = max(1, min(int(request.args.get("page", 1)), total_pages))

    start = (page - 1) * DEFAULT_PAGE_SIZE
    paged = all_results[start : start + DEFAULT_PAGE_SIZE]

    selected_sources = session.get("selected_sources")
    all_source_count = len(_DATASOURCE_NAMES)
    source_filtered = selected_sources is not None and len(selected_sources) < all_source_count
    source_filter_labels = (
        [_DATASOURCE_DISPLAY.get(_DATASOURCE_NAMES.get(s, ""), str(s)) for s in selected_sources]
        if source_filtered else []
    )

    return render_template(
        "results.html",
        results=paged,
        display_formatted=display_formatted,
        current_page=page,
        total_pages=total_pages,
        total_results=total,
        page_size=DEFAULT_PAGE_SIZE,
        truncated=total_original > MAX_RESULTS,
        original_count=total_original,
        unknown_glyphs=UNKNOWN_GLYPHS,
        source_filtered=source_filtered,
        source_filter_labels=source_filter_labels,
        convert_gardiner=convert_gardiner,
        prettify_transliteration=prettify_transliteration,
        datasource_name=datasource_name,
        datasource_display=datasource_display,
        datasource_color=datasource_color,
        gardiner_to_res=gardiner_to_res,
        gardiner_render_items=gardiner_render_items,
    )


@app.route("/image")
def image():
    key = request.args.get("key", "")
    image_key = key.replace("AA", "J").replace("Aa", "J")
    tiff_path = os.path.join(RESOURCES_PATH, f"{image_key}.tiff")
    if not os.path.exists(tiff_path):
        return "", 404
    img = Image.open(tiff_path)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/faulkner_entries")
def faulkner_entries():
    try:
        page = int(request.args.get("page", 0))
    except (TypeError, ValueError):
        return jsonify([])
    if page < 17 or page > 419:
        return jsonify([])

    pipeline = [
        {"$unwind": "$Translations"},
        {"$unwind": "$Translations.TranslationMetadata"},
        {
            "$match": {
                "Translations.TranslationMetadata.DictionaryName": 4,
                "Translations.TranslationMetadata.Page": page,
            }
        },
        {"$sort": {"Translations.TranslationMetadata.IndexOnPage": 1}},
    ]

    raw = list(entries_collection().aggregate(pipeline))
    output = [
        {
            "Transliteration": r.get("Transliteration", ""),
            "GardinerSigns": r.get("GardinerSigns", ""),
            "Res": r.get("Res"),
            "ManuelDeCodage": r.get("ManuelDeCodage", ""),
            "Translations": r.get("Translations", {}),
        }
        for r in raw
    ]
    return jsonify(output)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/gardiner_signs")
def gardiner_signs():
    return render_template("gardiner_signs.html")


@app.route("/references")
def references():
    return render_template("references.html")


@app.route("/sources")
def sources():
    return render_template("sources.html")


@app.route("/faulkner")
def faulkner():
    return render_template("faulkner.html")


# ---------------------------------------------------------------------------
# Compatibility routes for URLs hardcoded in the original JavaScript files
# ---------------------------------------------------------------------------

@app.route("/Scripts/<path:filename>")
def scripts_compat(filename):
    """Serve Scripts/ at the root path, as the original JS fetches from /Scripts/."""
    return send_from_directory(os.path.join(BASE_DIR, "static", "Scripts"), filename)


@app.route("/Search/FaulknerEntries")
def faulkner_entries_compat():
    """Alias used by faulkner.js: /Search/FaulknerEntries?page=N"""
    return faulkner_entries()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
