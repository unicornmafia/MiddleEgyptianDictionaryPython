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


_GARD_TOKEN_RE = re.compile(r'^[A-Z][a-z]?\d+[A-Za-z]*$')
_GARD_SUFFIX_RE = re.compile(r'(?<=\d)([A-Za-z]+)$')


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
    # Lowercase any trailing letter suffix after the digits (N35A→N35a, T14CB→T14cb)
    tok = _GARD_SUFFIX_RE.sub(lambda m: m.group(1).lower(), tok)
    return tok


def gardiner_to_res(gardiner_signs: str) -> str:
    """Convert space-separated Gardiner codes to RES canvas format.

    Only includes tokens known to the RES library; unknown tokens are dropped.
    """
    tokens = []
    for tok in gardiner_signs.split():
        normalized = _normalize_gardiner_token(tok)
        if normalized and normalized in RES_KNOWN_SIGNS:
            tokens.append(normalized)
    return '-'.join(tokens)


def gardiner_render_items(gardiner_signs: str) -> list:
    """Return a list of render items for a Gardiner sign sequence.

    Each item is either {'kind': 'canvas', 'value': str} for a run of RES-known
    signs, or {'kind': 'img', 'key': str} for an individual sign that must be
    served as a TIFF image (unknown to RES but present in Resources/).
    """
    items = []
    canvas_run = []

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
            # Use original token for the image key (Resources/ uses original casing)
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
    q = query.upper()
    if exact:
        return list(col.find({"GardinerSigns": q}))
    return list(col.find({"GardinerSigns": {"$regex": re.escape(q)}}))


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
    return sign_translit + _search_by_translation(query, exact)


def _sort_results(results: list) -> list:
    def _key(entry):
        translations = entry.get("Translations", [])
        dict_names = []
        for t in translations:
            for m in t.get("TranslationMetadata", []):
                dict_names.append(m.get("DictionaryName", 0))
        has_faulkner = 4 in dict_names
        dict_sum = sum(dict_names)
        return (not has_faulkner, -dict_sum, entry.get("Transliteration", ""), entry.get("GardinerSigns", ""))

    return sorted(results, key=_key)


def conduct_search(search_type: str, query: str, sign_query: str, exact: bool) -> list:
    if search_type == "transliteration":
        query = _sanitize_transliteration(query)
        return _sort_results(_search_by_transliteration(query, exact))
    if search_type == "translation":
        return _sort_results(_search_by_translation(query, exact))
    if search_type == "gardiner":
        return _sort_results(_search_by_gardiner(sign_query, exact))
    # default: all
    return _sort_results(_search_all(query, exact))

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

_DATASOURCE_NAMES = {0: "dickson", 1: "vygus", 2: "lexicon", 4: "faulkner"}
_DATASOURCE_COLORS = {
    "faulkner": "lightblue",
    "vygus": "thistle",
    "dickson": "lightpink",
    "lexicon": "moccasin",
}


def datasource_name(value) -> str:
    if isinstance(value, int):
        return _DATASOURCE_NAMES.get(value, str(value))
    return str(value).lower()


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

        results = conduct_search(search_type, query, sign_query, exact_match)
        total_count = len(results)
        limited = results[:MAX_RESULTS]

        # Serialize and cache results
        serialized = [{k: v for k, v in r.items() if k != "_id"} for r in limited]
        search_id = _save_results(serialized)

        session["search_id"] = search_id
        session["display_formatted"] = display_formatted
        session["total_original"] = total_count

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
        convert_gardiner=convert_gardiner,
        prettify_transliteration=prettify_transliteration,
        datasource_name=datasource_name,
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
