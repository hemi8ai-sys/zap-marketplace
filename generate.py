#!/usr/bin/env python3
"""Zap marketplace mirror-site generator for HDSALE.

Reads the product sheet (Google Sheets, published as CSV) and the existing
CroSphera price-comparison feed, and builds a static mirror site:

    site/index.html              category list (regular HTML page, as Zap requires)
    site/index.xml               same list as XML (convenience copy)
    site/categories/<slug>.xml   <STORE><PRODUCTS><PRODUCT> ... one per category
    site/products/<SKU>.xml      extended <ProductInfo> per variation (FULLPRODUCTURL)

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

# --------------------------------------------------------------------------
# Configuration (defaults; most can be overridden with environment variables)
# --------------------------------------------------------------------------
DEFAULT_SHEET_ID = "1dag5iF6IdDIr3-bocmfaLaJUiG7amdNvq69iNYtky8o"
DEFAULT_GIDS = {"products": 543853916, "settings": 878709405, "sales": 956282013}
DEFAULT_FEED_CATALOG = "https://www.hdsale.co.il/mirrors/zap/catalog.xml"
STORE_URL = "https://www.hdsale.co.il"
STORE_NAME = "HDSALE"

# Category name in the sheet -> stable URL slug. Unknown names get a hash slug.
# Zap scans category links on a fixed address: NEVER rename a slug after Zap connected.
CATEGORY_SLUGS = {
    "אופנה": "fashion",
    "חורף וסקי": "winter-ski",
    "ביגוד ספורט גברים": "mens-sportswear",
    "ביגוד ספורט נשים": "womens-sportswear",
    "תינוקות וילדים קטנים": "babies-kids",
}

MIN_IMAGE_SIDE = 650        # Zap: at least 650x650
MAX_IMAGES = 4              # Zap: up to 4 images
MAX_URL = 255               # Zap field limits
MAX_NAME = 150
MAX_DETAILS = 255
MAX_DESCRIPTION = 2000

# Marketplace-only products (price comparison keeps using the existing feed).
IS_ZAP_PRODUCT = 0
IS_MARKETPLACE_PRODUCT = 1

# Zap asks that the manufacturer name also appears in the product name.
APPEND_MANUFACTURER_TO_NAME = True

SETTING_KEYS = {
    # key: (label prefix in the settings sheet, type, default)
    "reserve": ("שמירת מלאי בצד", int, 2),
    "min_alert": ("התראת מלאי מינימלית", int, 1),
    "manufacturer": ("יצרן", str, "HDSALE"),
    "warranty_months": ("חודשי אחריות", int, 12),
    "warranty_by": ("גורם האחריות", str, "HDSALE"),
    "shipping": ("מחיר משלוח", float, 19.0),
    "delivery_days": ("זמן אספקה", int, 7),
    "lead_days": ("זמן הכנת משלוח", int, 2),
    "logistic": ("דירוג לוגיסטי", str, "S"),
    "installments": ("מספר תשלומים", int, 12),
    "condition": ("מצב המוצר", str, "חדש"),
    "tax": ("מע", int, 1),
    # Global kill switch: "כן" publishes an empty marketplace on purpose.
    "pause": ("השבתת פרסום", str, "לא"),
}

YES_VALUES = ("כן", "yes", "y", "true", "1")

COLUMN_HEADERS = {
    # key: (substring expected in the normalized header, fallback column index)
    "sku": ("מקט וריאציה", 0),
    "group": ("מקט מוצר", 1),
    "url": ("קישור", 2),
    "name": ("שם המוצר", 3),
    "size": ("מידה", 4),
    "color": ("צבע", 5),
    "initial": ("מלאי התחלתי", 6),
    "sold": ("נמכר", 7),
    "current": ("כמות נוכחית", 8),
    "publish": ("כמות לפרסום", 9),
    "active": ("פעיל", 10),
    "price": ("מחיר", 11),
    "category": ("קטגוריה", 12),
    "notes": ("הערות", 13),
    # "כן" in any row of a product approves enlarging its small main image (only for Zap).
    "upscale_ok": ("אישור הגדלת תמונה", 14),
}

SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
CRO_IMG_RE = re.compile(
    r"https?://ecdn\.crosphera\.net/([0-9a-f]{24})/([0-9a-f]{24})\.(?:450|750)\.[A-Za-z0-9.]+"
)
ILLEGAL_XML = re.compile("[^\t\n\r" + chr(0x20) + "-" + chr(0xD7FF) + chr(0xE000) + "-" + chr(0xFFFD) + chr(0x10000) + "-" + chr(0x10FFFF) + "]")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
class FatalError(Exception):
    """Something that makes publishing unsafe (the previous site stays live)."""


class FetchError(Exception):
    pass


def norm_header(text: str) -> str:
    return re.sub(r"[\"'״׳“”‘’\s]+", "", text or "")


def clean_text(text: str, limit: Optional[int] = None) -> str:
    """Plain text: no HTML, no entities, single spaces, valid XML characters."""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace(chr(0x200F), "").replace(chr(0x200E), "")
    text = ILLEGAL_XML.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit is not None:
        text = cut_at_word(text, limit)
    return text


def cut_at_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-–")


def xml_text(value) -> str:
    return _xml_escape(ILLEGAL_XML.sub("", str(value)), {'"': "&quot;"})


def to_int(value) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def to_float(value) -> Optional[float]:
    if value is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt_num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def url_key(url: str) -> str:
    """Matching key for product URLs: path only, decoded, letters and digits only."""
    path = urllib.parse.urlparse(url.strip()).path
    path = urllib.parse.unquote(urllib.parse.unquote(path))
    return re.sub(r"[\W_]+", "", path.lower(), flags=re.UNICODE)


def encode_url(url: str) -> str:
    """Percent-encode non-ASCII characters (Zap recommends encoded links)."""
    return urllib.parse.quote(url, safe=":/?&=%#+-._~@!$'()*,;[]")


def decode_url(url: str) -> str:
    return urllib.parse.unquote(url)


def norm_category(raw: str) -> str:
    """'אביזרים > כפפות' (also '/' or '|' as separators) -> 'אביזרים > כפפות'."""
    parts = [p.strip() for p in re.split(r"[>/|]", raw or "") if p.strip()]
    return " > ".join(parts)


def category_nodes(paths) -> List[str]:
    """All category paths including every parent, sorted so parents precede children."""
    nodes = set()
    for p in paths:
        parts = p.split(" > ")
        for i in range(1, len(parts) + 1):
            nodes.add(" > ".join(parts[:i]))
    return sorted(nodes)


def slugify_category(name: str) -> str:
    if name in CATEGORY_SLUGS:
        return CATEGORY_SLUGS[name]
    ascii_part = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part}-{digest}" if ascii_part else f"cat-{digest}"


def israel_now(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("Asia/Jerusalem"))
    except Exception:  # tzdata missing: fall back to a fixed offset
        return now.astimezone(timezone(timedelta(hours=2)))


# --------------------------------------------------------------------------
# Network layer (injectable for tests)
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, timeout: int = 30, retries: int = 3, pause: float = 0.2):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause

    def get(self, url: str, max_bytes: Optional[int] = None) -> bytes:
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; HDSALE-Zap-Generator/1.0)",
                        "Accept": "*/*",
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read(max_bytes) if max_bytes else resp.read()
                time.sleep(self.pause)
                return data
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise FetchError(f"{url}: {last}")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
@dataclass
class Report:
    errors: List[str] = field(default_factory=list)      # row excluded / something broken
    warnings: List[str] = field(default_factory=list)    # published, but check it
    infos: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    per_category: Dict[str, int] = field(default_factory=dict)
    approvals: Dict[str, str] = field(default_factory=dict)  # product group -> text; waiting for the owner's OK
    upscaled: Dict[str, Dict] = field(default_factory=dict)  # image id -> details for review.html

    def add_upscaled(self, sku: str, img_id: str, source: str, dims: Tuple[int, int], kind: str) -> None:
        # Variants of one product share images: one entry per image, listing every SKU.
        entry = self.upscaled.setdefault(
            img_id, {"skus": [], "source": source, "before": f"{dims[0]}x{dims[1]}", "kind": kind}
        )
        if sku not in entry["skus"]:
            entry["skus"].append(sku)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def to_markdown(self) -> str:
        s = self.stats
        lines = [
            "# דוח הרצה: מרקטפלייס זאפ",
            "",
            f"- שורות בגיליון: {s.get('rows', 0)}",
            f"- פורסמו: **{s.get('published', 0)}**",
            f"- לא פעילות: {s.get('inactive', 0)}",
            f"- ללא מלאי לפרסום: {s.get('no_stock', 0)}",
            f"- לא פורסמו בגלל שגיאה: **{len(self.errors)}**",
            "",
        ]
        if self.approvals:
            lines.append(f"## ממתינים לאישור שלך: הגדלת תמונה ({len(self.approvals)} מוצרים)")
            lines.append(
                "המוצרים האלה לא מפורסמים כי התמונה הראשית באתר קטנה מדי. "
                "כדי לאשר הגדלה אוטומטית (רק עבור זאפ) כתוב **כן** בעמודה 'אישור הגדלת תמונה' "
                "בגיליון, באחת משורות המוצר. אפשר גם להחליף את התמונה באתר."
            )
            lines.extend(f"- {m}" for m in self.approvals.values())
            lines.append("")
        if self.per_category:
            lines.append("## לפי קטגוריה")
            for cat, n in sorted(self.per_category.items()):
                lines.append(f"- {cat}: {n}")
            lines.append("")
        for title, items in (("שגיאות (לא פורסמו)", self.errors), ("אזהרות", self.warnings), ("מידע", self.infos)):
            if items:
                lines.append(f"## {title}")
                lines.extend(f"- {m}" for m in items)
                lines.append("")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "stats": self.stats,
                "per_category": self.per_category,
                "errors": self.errors,
                "warnings": self.warnings,
                "infos": self.infos,
                "upscaled": self.upscaled,
            },
            ensure_ascii=False,
            indent=2,
        )


# --------------------------------------------------------------------------
# Google Sheet
# --------------------------------------------------------------------------
def sheet_csv_url(sheet_id: str, gid: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def read_csv_rows(fetcher, url: str) -> List[List[str]]:
    raw = fetcher.get(url)
    text = raw.decode("utf-8-sig", errors="replace")
    if text.lstrip().lower().startswith("<!doctype html") or "<html" in text[:300].lower():
        raise FatalError(
            "הגיליון לא נגיש לקריאה (גוגל החזירה דף התחברות). "
            "יש להגדיר: שיתוף > כל מי שיש לו הקישור > צופה."
        )
    return list(csv.reader(io.StringIO(text)))


def load_settings(rows: List[List[str]], report: Report) -> Dict[str, object]:
    found: Dict[str, str] = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        label = norm_header(row[0])
        for key, (prefix, _t, _d) in SETTING_KEYS.items():
            if label.startswith(norm_header(prefix)) and key not in found:
                found[key] = row[1].strip()
    settings: Dict[str, object] = {}
    for key, (_prefix, typ, default) in SETTING_KEYS.items():
        raw = found.get(key)
        if raw is None or raw == "":
            if key not in found:
                report.warn(f"ההגדרה '{SETTING_KEYS[key][0]}' לא נמצאה בגיליון הגדרות, משתמש בברירת מחדל: {default}")
            settings[key] = default
            continue
        value = None
        if typ is int:
            value = to_int(raw)
        elif typ is float:
            value = to_float(raw)
        else:
            value = raw
        if value is None:
            report.warn(f"ההגדרה '{SETTING_KEYS[key][0]}' לא תקינה ({raw!r}), משתמש בברירת מחדל: {default}")
            value = default
        settings[key] = value
    settings["installments"] = max(1, min(36, int(settings["installments"])))
    settings["reserve"] = max(0, int(settings["reserve"]))
    settings["shipping"] = max(0.0, float(settings["shipping"]))
    settings["pause"] = str(settings["pause"]).strip().lower() in YES_VALUES
    return settings


@dataclass
class Row:
    line: int
    sku: str
    group: str
    url: str
    name: str
    size: str
    color: str
    initial: Optional[int]
    sold: Optional[int]
    publish: Optional[int]
    active: bool
    price_override: Optional[float]
    category: str
    upscale_ok: bool = False


def map_columns(header: List[str]) -> Dict[str, int]:
    norm = [norm_header(h) for h in header]
    cols: Dict[str, int] = {}
    for key, (needle, fallback) in COLUMN_HEADERS.items():
        needle_n = norm_header(needle)
        idx = None
        for i, h in enumerate(norm):
            if key in ("size", "color") and h == needle_n:
                idx = i
                break
            if key not in ("size", "color") and needle_n in h and i not in cols.values():
                idx = i
                break
        cols[key] = idx if idx is not None else fallback
    return cols


def load_rows(rows: List[List[str]], settings: Dict[str, object], report: Report) -> List[Row]:
    if not rows:
        raise FatalError("גיליון המוצרים ריק.")
    cols = map_columns(rows[0])

    def cell(r: List[str], key: str) -> str:
        i = cols[key]
        return r[i].strip() if i < len(r) and r[i] is not None else ""

    out: List[Row] = []
    seen: Dict[str, int] = {}
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        sku = cell(r, "sku")
        name = cell(r, "name")
        url = cell(r, "url")
        if not sku:
            if name or url:
                report.warn(f"שורה {n}: אין מק\"ט וריאציה (עמודה A), השורה מדולגת ({name[:40]})")
            continue
        if not SKU_RE.match(sku):
            report.error(f"שורה {n}: מק\"ט {sku!r} לא תקין (אותיות באנגלית, ספרות ומקף בלבד, בלי רווחים)")
            continue
        if sku in seen:
            report.error(f"שורה {n}: מק\"ט {sku} כפול (כבר מופיע בשורה {seen[sku]}), השורה לא תפורסם")
            continue
        seen[sku] = n
        active_raw = cell(r, "active").lower()
        out.append(
            Row(
                line=n,
                sku=sku,
                group=cell(r, "group") or sku,
                url=url,
                name=name,
                size=cell(r, "size"),
                color=cell(r, "color"),
                initial=to_int(cell(r, "initial")),
                sold=to_int(cell(r, "sold")),
                publish=to_int(cell(r, "publish")),
                active=active_raw in YES_VALUES,
                price_override=to_float(cell(r, "price")),
                category=norm_category(cell(r, "category")),
                upscale_ok=cell(r, "upscale_ok").lower() in YES_VALUES,
            )
        )
        if not SKU_RE.match(out[-1].group):
            report.error(f"שורה {n}: קוד קבוצה {out[-1].group!r} לא תקין (אותיות באנגלית, ספרות ומקף בלבד)")
            out.pop()
            seen.pop(sku, None)
    return out


def check_sales(rows: List[List[str]], skus: set, report: Report) -> Dict[str, int]:
    """Sum of the sales log per SKU; reports unknown SKUs and duplicated orders."""
    totals: Dict[str, int] = {}
    seen_orders: Dict[Tuple[str, str], int] = {}
    for n, r in enumerate(rows[1:], start=2):
        if len(r) < 4 or not any(c.strip() for c in r):
            continue
        sku, qty = r[2].strip(), to_int(r[3])
        order = r[4].strip() if len(r) > 4 else ""
        if not sku or qty is None:
            continue
        if order.startswith("דוגמה"):
            continue
        if sku not in skus:
            report.warn(f"מכירות, שורה {n}: מק\"ט {sku} לא קיים בגיליון המוצרים, המכירה לא ירדה ממלאי של אף וריאציה")
            continue
        if order:
            key = (order, sku)
            if key in seen_orders:
                report.warn(f"מכירות, שורה {n}: הזמנה {order} עם מק\"ט {sku} כבר נרשמה בשורה {seen_orders[key]} (כפילות?)")
            else:
                seen_orders[key] = n
        totals[sku] = totals.get(sku, 0) + qty
    return totals


def publishable_qty(row: Row, sales: Dict[str, int], reserve: int, report: Report) -> int:
    """Quantity offered to Zap. Uses the sheet's own column J; recomputes if it is empty."""
    expected = None
    if row.initial is not None:
        expected = max(0, row.initial - sales.get(row.sku, 0) - reserve)
    if row.publish is not None:
        if expected is not None and expected != row.publish:
            report.warn(
                f"{row.sku}: 'כמות לפרסום' בגיליון ({row.publish}) שונה מהחישוב לפי מלאי התחלתי ומכירות ({expected}). נשתמש בערך שבגיליון"
            )
        return max(0, row.publish)
    return expected if expected is not None else 0


# --------------------------------------------------------------------------
# CroSphera feed and product pages
# --------------------------------------------------------------------------
def parse_products(text: str) -> List[Dict[str, str]]:
    out = []
    for block in re.findall(r"<PRODUCT\b[^>]*>(.*?)</PRODUCT>", text, flags=re.S | re.I):
        rec: Dict[str, str] = {}
        for tag in ("PRODUCT_URL", "PRODUCT_NAME", "MODEL", "DETAILS", "PRICE", "PRODUCTCODE", "CATALOG_NUMBER", "IMAGE"):
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, flags=re.S | re.I)
            if m:
                value = m.group(1)
                cd = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*$", value, flags=re.S)
                value = cd.group(1) if cd else html.unescape(value)
                rec[tag] = value.strip()
        if rec.get("PRODUCT_URL"):
            out.append(rec)
    return out


class Feed:
    def __init__(self) -> None:
        self.by_key: Dict[str, Dict[str, str]] = {}
        self.count = 0

    def add(self, rec: Dict[str, str]) -> None:
        self.count += 1
        key = url_key(rec["PRODUCT_URL"])
        old = self.by_key.get(key)
        price_ok = (to_float(rec.get("PRICE")) or 0) > 0
        old_ok = old is not None and (to_float(old.get("PRICE")) or 0) > 0
        if old is None or (price_ok and not old_ok):
            self.by_key[key] = rec

    def find(self, url: str) -> Optional[Dict[str, str]]:
        return self.by_key.get(url_key(url))


def load_feed(fetcher, catalog_url: str, report: Report) -> Feed:
    try:
        catalog = fetcher.get(catalog_url).decode("utf-8", errors="replace")
    except FetchError as exc:
        raise FatalError(f"פיד קרוספרה לא נגיש: {exc}")
    links = []
    for href in re.findall(r"""href=["']([^"']+)["']""", catalog):
        href = html.unescape(href)
        if "products.xml" in href and href not in links:
            links.append(urllib.parse.urljoin(catalog_url, href))
    if not links:
        raise FatalError("לא נמצאו קישורי קטגוריות בפיד של קרוספרה (catalog.xml). מבנה הפיד השתנה?")
    feed = Feed()
    failed = 0
    for link in links:
        try:
            text = fetcher.get(link).decode("utf-8", errors="replace")
        except FetchError as exc:
            failed += 1
            report.warn(f"קטגוריית פיד לא נטענה: {exc}")
            continue
        for rec in parse_products(text):
            feed.add(rec)
    if failed > len(links) * 0.3:
        raise FatalError(f"{failed} מתוך {len(links)} קטגוריות הפיד לא נטענו, מבטלים כדי לא לפרסם נתונים חסרים")
    if feed.count == 0:
        raise FatalError("הפיד של קרוספרה ריק (0 מוצרים).")
    report.info(f"נטענו {feed.count} רשומות מ-{len(links) - failed} קטגוריות פיד")
    return feed


def extract_body_description(page_html: str) -> str:
    """Product description text between the 'add to cart' button and the share line."""
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page_html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    start_marker = "הוסף לסל"
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = text.find("שתפו עם חברים", start)
    if end < 0:
        return ""
    return text[start:end].strip()


def parse_page(page_html: str) -> Dict[str, object]:
    info: Dict[str, object] = {"description": "", "price": None, "image_ids": [], "base": None, "og_image_id": None}
    # JSON-LD
    for blob in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page_html, flags=re.S | re.I):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]])
                if str(node.get("@type", "")).lower() == "product":
                    if node.get("description") and not info["description"]:
                        info["description"] = clean_text(str(node["description"]))
                    offers = node.get("offers")
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict) and info["price"] is None:
                        info["price"] = to_float(offers.get("price"))
    m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']', page_html, flags=re.I)
    if m and not info["description"]:
        info["description"] = clean_text(m.group(1))
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', page_html, flags=re.I)
    if m:
        mm = re.search(r"crosphera\.net/([0-9a-f]{24})/([0-9a-f]{24})\.", m.group(1))
        if mm:
            info["base"], info["og_image_id"] = mm.group(1), mm.group(2)
    ids: List[str] = []
    for store, img in CRO_IMG_RE.findall(page_html):
        info["base"] = info["base"] or store
        if img not in ids:
            ids.append(img)
    info["image_ids"] = ids
    info["body_description"] = clean_text(extract_body_description(page_html))
    return info


def image_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    """Width/height of JPEG, PNG or GIF from the header bytes (no external libraries)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xFF:
                i += 1
                continue
            length = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                w = int.from_bytes(data[i + 7 : i + 9], "big")
                return w, h
            i += 2 + length
    return None


class ImageChecker:
    """Checks image sizes once per image id (cached between runs)."""

    def __init__(self, fetcher, cache: Dict[str, List[int]]):
        self.fetcher = fetcher
        self.cache = cache

    def size(self, url: str) -> Optional[Tuple[int, int]]:
        if url in self.cache:
            w, h = self.cache[url]
            return (w, h) if w and h else None
        dims = None
        for limit in (200_000, None):
            try:
                data = self.fetcher.get(url, max_bytes=limit) if limit else self.fetcher.get(url)
            except FetchError:
                return None
            dims = image_dimensions(data)
            if dims:
                break
        if dims:
            self.cache[url] = [dims[0], dims[1]]
        return dims


# --------------------------------------------------------------------------
# Building items
# --------------------------------------------------------------------------
@dataclass
class Item:
    row: Row
    qty: int
    url: str
    name: str
    model: str
    details: str
    description: str
    price: float
    image: str
    images: List[str]
    options: List[Tuple[str, str]]
    category: str
    slug: str


def variant_label(row: Row) -> str:
    parts = []
    if row.size and row.size.strip().lower() != "one size":
        parts.append(row.size.strip())
    if row.color:
        parts.append(row.color.strip())
    return " ".join(parts)


def choose_url(url: str, report: Report, sku: str) -> str:
    raw = url.strip()
    cands = [encode_url(decode_url(raw)), decode_url(raw), raw]
    for c in cands:
        if len(c) <= MAX_URL:
            return c
    best = min(cands, key=len)
    report.warn(f"{sku}: קישור המוצר ארוך מ-{MAX_URL} תווים ({len(best)}). זאפ עלולה לחתוך אותו. כדאי לבקש מקרוספרה קישור קצר")
    return best


MIN_UPSCALE_SOURCE = 400    # smaller sources are too poor to enlarge
UPSCALE_SIZE = 1024         # square white canvas, side in pixels
# Trial: secondary images are enlarged only for these product groups (still needs "כן" in column O).
SECONDARY_UPSCALE_GROUPS = {"18890113-1"}  # the group code in column B (טייץ תרמי פרוותי)


def upscale_to_square(data: bytes, size: int = UPSCALE_SIZE) -> Optional[bytes]:
    """Enlarge an image onto a white square JPEG. Needs Pillow; returns None if unavailable."""
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        scale = size / max(im.size)
        new = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        im = im.resize(new, Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        canvas.paste(im, ((size - new[0]) // 2, (size - new[1]) // 2))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=92, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def upscale_image(fetcher, url: str, img_id: str, extra_files: Dict) -> bool:
    try:
        data = fetcher.get(url)
    except FetchError:
        return False
    out = upscale_to_square(data)
    if not out:
        return False
    extra_files[f"images/{img_id}.jpg"] = out
    return True


def build_items(rows, settings, feed, fetcher, sales, report, img_cache, base_url, page_cache=None, extra_files=None) -> List[Item]:
    extra_files = {} if extra_files is None else extra_files
    checker = ImageChecker(fetcher, img_cache)
    page_cache = {} if page_cache is None else page_cache
    items: List[Item] = []
    group_urls: Dict[str, set] = {}
    group_counts: Dict[str, int] = {}
    for r in rows:
        group_counts[r.group] = group_counts.get(r.group, 0) + 1
    approved_groups = {r.group for r in rows if r.upscale_ok}

    for r in rows:
        report.stats["rows"] = report.stats.get("rows", 0) + 1
        if not r.active:
            report.stats["inactive"] = report.stats.get("inactive", 0) + 1
            continue
        qty = publishable_qty(r, sales, settings["reserve"], report)
        if qty <= 0:
            report.stats["no_stock"] = report.stats.get("no_stock", 0) + 1
            report.info(f"{r.sku}: לא יפורסם (אין מלאי לפרסום אחרי שמירה בצד)")
            continue
        # A row that the sheet wants published; used by the safety net in run().
        report.stats["eligible"] = report.stats.get("eligible", 0) + 1
        if not r.url:
            report.error(f"שורה {r.line} ({r.sku}): חסר קישור לעמוד המוצר (עמודה C)")
            continue
        if not r.category:
            report.error(f"שורה {r.line} ({r.sku}): חסרה קטגוריה (עמודה M)")
            continue

        rec = feed.find(r.url)
        page = page_cache.get(r.url)
        if page is None:
            try:
                page = parse_page(fetcher.get(r.url).decode("utf-8", errors="replace"))
            except FetchError as exc:
                page = None
                report.warn(f"{r.sku}: עמוד המוצר לא נטען ({exc}); משתמשים בנתוני הפיד בלבד")
            page_cache[r.url] = page
        page = page or {"description": "", "price": None, "image_ids": [], "base": None, "og_image_id": None, "body_description": ""}

        # price
        price = r.price_override
        if price is None and rec:
            price = to_float(rec.get("PRICE"))
        if price is None:
            price = page.get("price")
        if not price or price <= 0:
            where = "לא נמצא בפיד" if not rec else "בפיד אין מחיר"
            report.error(f"שורה {r.line} ({r.sku}): {where} ולא הוגדר מחיר בעמודה L. הקישור: {decode_url(r.url)[:80]}")
            continue

        # images
        main_id = None
        base = page.get("base")
        if rec and rec.get("IMAGE"):
            m = re.search(r"crosphera\.net/([0-9a-f]{24})/([0-9a-f]{24})\.", rec["IMAGE"])
            if m:
                base, main_id = m.group(1), m.group(2)
        if not main_id:
            main_id = page.get("og_image_id") or (page["image_ids"][0] if page["image_ids"] else None)
        if not main_id or not base:
            report.error(f"שורה {r.line} ({r.sku}): לא נמצאה תמונה למוצר")
            continue
        ids = [main_id] + [i for i in page["image_ids"] if i != main_id]
        images: List[str] = []
        main_ok = True
        for k, img_id in enumerate(ids[: MAX_IMAGES + 4]):
            if len(images) >= MAX_IMAGES:
                break
            url_full = f"https://ecdn.crosphera.net/{base}/{img_id}.jpg"
            dims = checker.size(url_full)
            if dims and min(dims) >= MIN_IMAGE_SIDE:
                images.append(url_full)
            elif (
                k == 0
                and dims
                and min(dims) >= MIN_UPSCALE_SOURCE
                and r.group in approved_groups
                and (f"images/{img_id}.jpg" in extra_files or upscale_image(fetcher, url_full, img_id, extra_files))
            ):
                images.append(f"{base_url}/images/{img_id}.jpg")
                report.add_upscaled(r.sku, img_id, url_full, dims, "ראשית")
                report.warn(
                    f"{r.sku}: התמונה הראשית באתר קטנה ({dims[0]}x{dims[1]}). הוגדלה ל-{UPSCALE_SIZE}x{UPSCALE_SIZE} רק עבור זאפ, באישורך. מומלץ להחליף באתר בתמונה גדולה"
                )
            elif (
                k > 0
                and dims
                and min(dims) >= MIN_UPSCALE_SOURCE
                and r.group in approved_groups
                and r.group in SECONDARY_UPSCALE_GROUPS
                and (f"images/{img_id}.jpg" in extra_files or upscale_image(fetcher, url_full, img_id, extra_files))
            ):
                images.append(f"{base_url}/images/{img_id}.jpg")
                report.add_upscaled(r.sku, img_id, url_full, dims, "משנית")
                report.info(f"{r.sku}: תמונה משנית {img_id} ({dims[0]}x{dims[1]}) הוגדלה ל-{UPSCALE_SIZE}x{UPSCALE_SIZE}")
            elif k == 0 and dims and min(dims) >= MIN_UPSCALE_SOURCE and r.group not in approved_groups:
                # Needs the owner's OK first. A business decision, so it must not trip the safety net.
                main_ok = False
                report.stats["eligible"] = report.stats.get("eligible", 0) - 1
                label_txt = clean_text(r.name) or clean_text((rec or {}).get("PRODUCT_NAME", "")) or r.group
                report.approvals.setdefault(
                    r.group,
                    f"{r.group} | {label_txt[:60]} | תמונה ראשית {dims[0]}x{dims[1]} | {decode_url(r.url)[:90]}",
                )
                break
            elif k == 0:
                main_ok = False
                size_txt = f"{dims[0]}x{dims[1]}" if dims else "לא ניתן לקרוא"
                report.error(
                    f"שורה {r.line} ({r.sku}): התמונה הראשית קטנה מדי לזאפ ({size_txt}, נדרש לפחות {MIN_IMAGE_SIDE}x{MIN_IMAGE_SIDE}). יש להעלות תמונה גדולה יותר באתר"
                )
                break
            else:
                size_txt = f"{dims[0]}x{dims[1]}" if dims else "לא נקראה"
                report.info(f"{r.sku}: תמונה משנית דולגה ({size_txt}, נדרש לפחות {MIN_IMAGE_SIDE})")
        if not main_ok:
            continue

        # texts
        label = variant_label(r)
        base_name = clean_text(r.name) or clean_text((rec or {}).get("PRODUCT_NAME", ""))
        multi = group_counts.get(r.group, 1) > 1
        name = base_name
        if multi and label:
            name = f"{base_name} - {label}"
        manufacturer = str(settings["manufacturer"])
        if APPEND_MANUFACTURER_TO_NAME and manufacturer and manufacturer.lower() not in name.lower():
            name = f"{name} {manufacturer}"
        name = cut_at_word(name, MAX_NAME)

        candidates = [
            clean_text((rec or {}).get("DETAILS", "")),
            str(page.get("description") or ""),
            str(page.get("body_description") or ""),
        ]
        description = max(candidates, key=len)
        description = clean_text(description, MAX_DESCRIPTION) or name
        feed_details = clean_text((rec or {}).get("DETAILS", ""))
        details = cut_at_word(description if len(description) >= len(feed_details) else feed_details, MAX_DETAILS) or name

        options = []
        if r.color:
            options.append(("צבע", r.color.strip()))
        if r.size and r.size.strip().lower() != "one size":
            options.append(("מידה", r.size.strip()))

        group_urls.setdefault(r.group, set()).add(url_key(r.url))
        items.append(
            Item(
                row=r, qty=qty, url=choose_url(r.url, report, r.sku), name=name, model=label,
                details=details, description=description, price=float(price),
                image=images[0], images=images, options=options,
                category=r.category, slug=slugify_category(r.category),
            )
        )
    for g, urls in group_urls.items():
        if len(urls) > 1:
            report.warn(f"קוד קבוצה {g}: וריאציות עם קישורי עמוד שונים ({len(urls)}). בדוק שזה מכוון")
    return items


# --------------------------------------------------------------------------
# XML / HTML output
# --------------------------------------------------------------------------
def store_header(now: datetime) -> str:
    local = israel_now(now)
    return (
        f'<STORE URL="{xml_text(STORE_URL)}" DATE="{local:%d/%m/%Y}" TIME="{local:%H:%M:%S}" '
        f'NAME="{xml_text(STORE_NAME)}" STATUS="ONLINE" ID="">'
    )


def category_xml(items: List[Item], settings, base_url: str, now: datetime) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>', store_header(now), "<PRODUCTS>"]
    for n, it in enumerate(items, start=1):
        s = settings
        out += [
            f'<PRODUCT NUM="{n}">',
            f"<PRODUCT_URL>{xml_text(it.url)}</PRODUCT_URL>",
            f"<PRODUCT_NAME>{xml_text(it.name)}</PRODUCT_NAME>",
            f"<MODEL>{xml_text(it.model)}</MODEL>",
            f"<DETAILS>{xml_text(it.details)}</DETAILS>",
            f"<PRICE>{fmt_num(it.price)}</PRICE>",
            f"<PRODUCTCODE>{xml_text(it.row.sku)}</PRODUCTCODE>",
            f"<CATALOG_NUMBER>{xml_text(it.row.sku)}</CATALOG_NUMBER>",
            "<CURRENCY>ILS</CURRENCY>",
            f"<SHIPMENT_COST>{fmt_num(s['shipping'])}</SHIPMENT_COST>",
            f"<DELIVERY_TIME>{int(s['delivery_days'])}</DELIVERY_TIME>",
            f"<MANUFACTURER>{xml_text(s['manufacturer'])}</MANUFACTURER>",
            f"<WARRANTY>{int(s['warranty_months'])}</WARRANTY>",
            f"<WARRANTYBY>{xml_text(s['warranty_by'])}</WARRANTYBY>",
            f"<IMAGE>{xml_text(it.image)}</IMAGE>",
            f"<FULLPRODUCTURL>{xml_text(base_url + '/products/' + it.row.sku + '.xml')}</FULLPRODUCTURL>",
            f"<ISZAPPRODUCT>{IS_ZAP_PRODUCT}</ISZAPPRODUCT>",
            f"<ISMARKETPLACEPRODUCT>{IS_MARKETPLACE_PRODUCT}</ISMARKETPLACEPRODUCT>",
            f"<VARIANTGROUPCODE>{xml_text(it.row.group)}</VARIANTGROUPCODE>",
            f"<TAX>{int(s['tax'])}</TAX>",
            f"<MAXINSTALLMENTS>{int(s['installments'])}</MAXINSTALLMENTS>",
            "</PRODUCT>",
        ]
    out += ["</PRODUCTS>", "</STORE>", ""]
    return "\n".join(out)


def product_xml(it: Item, settings, terms: str) -> str:
    s = settings
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ProductInfo>",
        f"<ProductCode>{xml_text(it.row.sku)}</ProductCode>",
        f"<Product_Name>{xml_text(it.name)}</Product_Name>",
        f"<Price>{fmt_num(it.price)}</Price>",
        "<AvailableStartDate></AvailableStartDate>",
        "<AvailableEndDate></AvailableEndDate>",
        f"<LeadTimeToShip>{int(s['lead_days'])}</LeadTimeToShip>",
        f"<Condition>{xml_text(s['condition'])}</Condition>",
        f"<MaxInstallments>{int(s['installments'])}</MaxInstallments>",
        "<Pickup>false</Pickup>",
        f"<StockQuantity>{int(it.qty)}</StockQuantity>",
        f"<MinQuantityAlert>{int(s['min_alert'])}</MinQuantityAlert>",
        f"<Description>{xml_text(it.description)}</Description>",
        f"<TermsAndConditions>{xml_text(terms)}</TermsAndConditions>",
        f"<LogisticClass>{xml_text(s['logistic'])}</LogisticClass>",
        f"<Warranty>{int(s['warranty_months'])}</Warranty>",
        f"<WarrantyBy>{xml_text(s['warranty_by'])}</WarrantyBy>",
    ]
    if it.options:
        out.append("<Variants>")
        out.append("<options>")
        for name, value in it.options:
            out += ["<Option>", f"<Name>{xml_text(name)}</Name>", f"<Value>{xml_text(value)}</Value>", "</Option>"]
        out += ["</options>", "</Variants>"]
    out.append(f"<VariantGroupCode>{xml_text(it.row.group)}</VariantGroupCode>")
    out.append("<Images>")
    for u in it.images:
        out.append(f'<Image url="{xml_text(u)}"/>')
    out += ["</Images>", "</ProductInfo>", ""]
    return "\n".join(out)


def _nested_ul(categories, base_url: str) -> str:
    """Nested <ul> like the store's own mirror page: parents contain their children."""
    out: List[str] = []
    stack: List[str] = []  # open category paths
    for path, slug, _n in categories:
        depth = path.count(" > ")
        while len(stack) > depth:
            stack.pop()
            out.append("</ul></li>")
        name = path.split(" > ")[-1]
        out.append(f'<li><a href="{xml_text(base_url)}/categories/{xml_text(slug)}.xml">{xml_text(name)}</a>')
        nxt = None
        idx = categories.index((path, slug, _n))
        if idx + 1 < len(categories):
            nxt = categories[idx + 1][0]
        if nxt and nxt.startswith(path + " > "):
            out.append("<ul>")
            stack.append(path)
        else:
            out.append("</li>")
    while stack:
        stack.pop()
        out.append("</ul></li>")
    return "\n".join(out)


def index_html(categories: List[Tuple[str, str, int]], base_url: str, now: datetime) -> str:
    local = israel_now(now)
    return (
        '<!DOCTYPE html>\n<html lang="he" dir="rtl">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{STORE_NAME} | אתר מראה לזאפ מרקטפלייס</title>\n</head>\n<body>\n"
        f"<h1>{STORE_NAME}: אתר מראה לזאפ מרקטפלייס</h1>\n<p>עודכן: {local:%d/%m/%Y %H:%M}</p>\n"
        f"<ul>\n{_nested_ul(categories, base_url)}\n</ul>\n</body>\n</html>\n"
    )


def review_html(upscaled: Dict[str, Dict], now: datetime) -> str:
    """Every image enlarged in this run, shown as it goes into the XML (for the owner, not for Zap)."""
    local = israel_now(now)
    cards = []
    for img_id, e in upscaled.items():
        mine = f"images/{img_id}.jpg"
        cards.append(
            '<section class="card">\n'
            f"<p>מק\"ט: <b>{xml_text(', '.join(e['skus']))}</b> | סוג: {xml_text(e['kind'])} | "
            f"מזהה תמונה: <code>{xml_text(img_id)}</code> | "
            f"גודל: {xml_text(e['before'])} &larr; {UPSCALE_SIZE}x{UPSCALE_SIZE}</p>\n"
            f'<img src="{xml_text(mine)}" alt="{xml_text(img_id)}" width="{UPSCALE_SIZE}" height="{UPSCALE_SIZE}">\n'
            f'<p class="links"><a href="{xml_text(mine)}">קובץ מוגדל</a> | '
            f'<a href="{xml_text(e["source"])}" target="_blank" rel="noopener">הצג את המקור</a></p>\n'
            "</section>"
        )
    body = "\n".join(cards) if cards else "<p>בריצה הזאת לא הוגדלו תמונות.</p>"
    return (
        '<!DOCTYPE html>\n<html lang="he" dir="rtl">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{STORE_NAME} | בדיקת תמונות מוגדלות</title>\n"
        "<style>body{font-family:sans-serif;margin:16px;background:#fff;color:#111}"
        ".card{border:1px solid #ccc;padding:12px;margin-bottom:16px}"
        f"img{{display:block;width:{UPSCALE_SIZE}px;height:{UPSCALE_SIZE}px;max-width:100%;border:1px solid #eee}}"
        # Narrower than the image: keep it square instead of stretching it.
        f"@media (max-width:{UPSCALE_SIZE + 32}px){{img{{height:auto;aspect-ratio:1/1}}}}"
        ".links{font-size:small}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{STORE_NAME}: תמונות שהוגדלו ({len(upscaled)})</h1>\n<p>עודכן: {local:%d/%m/%Y %H:%M}</p>\n"
        f"{body}\n</body>\n</html>\n"
    )


def index_xml(categories: List[Tuple[str, str, int]], base_url: str, now: datetime) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>', store_header(now), "<CATEGORIES>"]
    for name, slug, n in categories:
        out.append(
            f'<CATEGORY NAME="{xml_text(name)}" PRODUCTS="{n}"><URL>{xml_text(base_url + "/categories/" + slug + ".xml")}</URL></CATEGORY>'
        )
    out += ["</CATEGORIES>", "</STORE>", ""]
    return "\n".join(out)


def validate_item(it: Item) -> List[str]:
    problems = []
    if not it.url or len(it.url) > MAX_URL:
        problems.append("PRODUCT_URL")
    if not it.name or len(it.name) > MAX_NAME:
        problems.append("PRODUCT_NAME")
    if not it.details or len(it.details) > MAX_DETAILS:
        problems.append("DETAILS")
    if not it.image or len(it.image) > MAX_URL:
        problems.append("IMAGE")
    if it.price <= 0:
        problems.append("PRICE")
    if it.qty <= 0:
        problems.append("StockQuantity")
    if len(it.description) > MAX_DESCRIPTION:
        problems.append("Description")
    return problems


def load_terms(path: str, settings) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = "תקופת אחריות: {warranty_months} חודשים. אחריות: {warranty_by}."
    return text.replace("{warranty_months}", str(settings["warranty_months"])).replace("{warranty_by}", str(settings["warranty_by"])).strip()


def write_site(out_dir: str, files: Dict[str, object]) -> None:
    import shutil

    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    for rel, content in files.items():
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(content, bytes):
            with open(path, "wb") as fh:
                fh.write(content)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run(
    fetcher,
    sheet_id: str,
    gids: Dict[str, int],
    feed_catalog: str,
    base_url: str,
    out_dir: str,
    report_dir: str,
    cache_dir: str,
    terms_path: str,
    now: Optional[datetime] = None,
    sheet_reader: Optional[Callable[[str], List[List[str]]]] = None,
) -> Report:
    now = now or datetime.now(timezone.utc)
    base_url = base_url.rstrip("/")
    report = Report()
    read = sheet_reader or (lambda name: read_csv_rows(fetcher, sheet_csv_url(sheet_id, gids[name])))

    settings = load_settings(read("settings"), report)
    rows = load_rows(read("products"), settings, report)
    if not rows:
        raise FatalError("לא נמצאו שורות מוצר בגיליון.")
    try:
        sales = check_sales(read("sales"), {r.sku for r in rows}, report)
    except FatalError:
        raise
    except Exception as exc:  # sales tab is optional for publishing
        sales = {}
        report.warn(f"גיליון המכירות לא נקרא: {exc}")

    feed = load_feed(fetcher, feed_catalog, report)

    cache_path = os.path.join(cache_dir, "images.json")
    img_cache: Dict[str, List[int]] = {}
    try:
        with open(cache_path, encoding="utf-8") as fh:
            img_cache = json.load(fh)
    except (OSError, ValueError):
        pass

    extra_files: Dict[str, bytes] = {}
    if settings["pause"]:
        items = []
        report.warn("פרסום מושבת (ההגדרה 'השבתת פרסום' = כן): המרקטפלייס מפורסם ריק בכוונה")
    else:
        items = build_items(rows, settings, feed, fetcher, sales, report, img_cache, base_url, extra_files=extra_files)

    final: List[Item] = []
    for it in items:
        problems = validate_item(it)
        if problems:
            report.error(f"{it.row.sku}: שדות לא תקינים ({', '.join(problems)}), לא יפורסם")
        else:
            final.append(it)
    report.stats["published"] = len(final)

    # Safety net: compare what the sheet WANTS published (active, with stock) with what actually
    # made it out. A big gap means a broken source (feed, pages, images), not a business decision.
    # Turning products off or zeroing stock in the sheet lowers "eligible" too, so it never trips this.
    state_path = os.path.join(cache_dir, "state.json")
    eligible = int(report.stats.get("eligible", 0))
    if not settings["pause"] and eligible >= 10 and len(final) < eligible * 0.5:
        raise FatalError(
            f"מתוך {eligible} מוצרים שהגיליון רוצה לפרסם, רק {len(final)} עברו בדיקה. "
            "נראה כמו תקלה במקור הנתונים, ולכן האתר הקודם נשאר כפי שהוא."
        )

    categories_all: Dict[str, str] = {}
    for node in category_nodes([r.category for r in rows if r.category]):
        categories_all[node] = slugify_category(node)
    slugs: Dict[str, str] = {}
    for name, slug in categories_all.items():
        if slug in slugs and slugs[slug] != name:
            raise FatalError(f"שתי קטגוריות שונות מקבלות את אותה כתובת ({slug}): {slugs[slug]} / {name}")
        slugs[slug] = name

    terms = load_terms(terms_path, settings)
    files: Dict[str, object] = {".nojekyll": ""}
    files.update(extra_files)
    cats_for_index: List[Tuple[str, str, int]] = []
    for name in sorted(categories_all):
        slug = categories_all[name]
        cat_items = [it for it in final if it.category == name or it.category.startswith(name + " > ")]
        report.per_category[name] = len(cat_items)
        cats_for_index.append((name, slug, len(cat_items)))
        files[f"categories/{slug}.xml"] = category_xml(cat_items, settings, base_url, now)
    for it in final:
        files[f"products/{it.row.sku}.xml"] = product_xml(it, settings, terms)
    files["index.html"] = index_html(cats_for_index, base_url, now)
    files["index.xml"] = index_xml(cats_for_index, base_url, now)
    files["review.html"] = review_html(report.upscaled, now)

    write_site(out_dir, files)

    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(img_cache, fh)
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump({"published": len(final)}, fh)
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(report.to_markdown())
    with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as fh:
        fh.write(report.to_json())
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Zap marketplace mirror generator")
    ap.add_argument("--out", default="site")
    ap.add_argument("--report", default="report")
    ap.add_argument("--cache", default=os.environ.get("CACHE_DIR") or ".cache")
    ap.add_argument("--terms", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "terms.txt"))
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL") or "")
    args = ap.parse_args(argv)
    if not args.base_url:
        print("חסר BASE_URL (כתובת האתר המראה, למשל https://zap.hdsale.co.il)", file=sys.stderr)
        return 1
    gids = {
        "products": int(os.environ.get("GID_PRODUCTS") or DEFAULT_GIDS["products"]),
        "settings": int(os.environ.get("GID_SETTINGS") or DEFAULT_GIDS["settings"]),
        "sales": int(os.environ.get("GID_SALES") or DEFAULT_GIDS["sales"]),
    }
    try:
        report = run(
            Fetcher(),
            os.environ.get("SHEET_ID") or DEFAULT_SHEET_ID,
            gids,
            os.environ.get("FEED_CATALOG_URL") or DEFAULT_FEED_CATALOG,
            args.base_url,
            args.out,
            args.report,
            args.cache,
            args.terms,
        )
    except FatalError as exc:
        print(f"::error::{exc}")
        print(f"שגיאה קריטית: {exc}", file=sys.stderr)
        return 1
    print(report.to_markdown())
    for msg in report.errors:
        print(f"::warning::{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())