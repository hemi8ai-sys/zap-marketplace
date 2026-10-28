import os
import struct
import sys
import tempfile
import unittest
import zlib
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generate as g  # noqa: E402

BASE = "aaaaaaaaaaaaaaaaaaaaaaaa"
IMG1 = "111111111111111111111111"
IMG2 = "222222222222222222222222"
IMG_SMALL = "333333333333333333333333"


def png(w, h):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))


PAGE = f"""<html><head><meta property="og:image" content="https://ecdn.crosphera.net/{BASE}/{IMG1}.750.1x1.jpg"></head><body>
<img src="https://ecdn.crosphera.net/{BASE}/{IMG1}.450.1x1.jpg">
<img src="https://ecdn.crosphera.net/{BASE}/{IMG2}.450.1x1.jpg">
הוסף לסל <p>חולצה <b>נושמת</b> &amp; קלה</p> שתפו עם חברים</body></html>"""

FEED_CAT = '<a href="products.xml?catId=1">x</a>'
FEED_PRODUCTS = f"""<STORE><PRODUCTS>
<PRODUCT><PRODUCT_URL>https://www.hdsale.co.il/p/shirt</PRODUCT_URL><PRODUCT_NAME>חולצה</PRODUCT_NAME>
<DETAILS>קצר</DETAILS><PRICE>0</PRICE></PRODUCT>
<PRODUCT><PRODUCT_URL>https://www.hdsale.co.il/p/shirt</PRODUCT_URL><PRODUCT_NAME>חולצה</PRODUCT_NAME>
<DETAILS>&lt;b&gt;חולצה&lt;/b&gt; נושמת</DETAILS><PRICE>79</PRICE>
<IMAGE>https://ecdn.crosphera.net/{BASE}/{IMG1}.450.jpg</IMAGE></PRODUCT>
</PRODUCTS></STORE>"""


class FakeFetcher:
    def __init__(self, small=False):
        self.small = small

    def get(self, url, max_bytes=None):
        if url.endswith("catalog.xml"):
            return FEED_CAT.encode()
        if "products.xml" in url:
            return FEED_PRODUCTS.encode()
        if url.endswith(".jpg") and "ecdn" in url:
            if IMG_SMALL in url or (self.small and IMG1 in url):
                return png(600, 600)
            return png(1024, 1024)
        if "hdsale.co.il/p/" in url:
            return PAGE.encode()
        raise g.FetchError(url)


SETTINGS = [["הגדרה", "ערך"], ["שמירת מלאי בצד (יחידות)", "2"], ["יצרן", "HDSALE"], ["מחיר משלוח", "19"],
            ["מספר תשלומים מקסימלי", "3"], ["חודשי אחריות", "12"]]
HEAD = ["מקט וריאציה", "מקט מוצר", "קישור", "שם המוצר", "מידה", "צבע", "מלאי התחלתי", "נמכר", "כמות נוכחית",
        "כמות לפרסום", "פעיל", "מחיר", "קטגוריה", "הערות"]


def prow(sku, size, qty, active="כן", url="https://www.hdsale.co.il/p/shirt", cat="אופנה", price=""):
    return [sku, "G1", url, "חולצה", size, "שחור", "5", "0", "5", str(qty), active, price, cat, ""]


def run_with(products, small=False, sales=None):
    tmp = tempfile.mkdtemp()
    data = {"settings": SETTINGS, "products": [HEAD] + products, "sales": sales or [["h"]]}
    rep = g.run(FakeFetcher(small), "x", {"products": 1, "settings": 2, "sales": 3},
                "https://www.hdsale.co.il/mirrors/zap/catalog.xml", "https://example.test/zap",
                os.path.join(tmp, "site"), os.path.join(tmp, "rep"), os.path.join(tmp, "cache"),
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terms.txt"),
                sheet_reader=lambda n: data[n])
    return rep, os.path.join(tmp, "site")


class Tests(unittest.TestCase):
    def test_happy_path_and_wellformed(self):
        rep, site = run_with([prow("A-1", "M", 3), prow("A-2", "L", 2)])
        self.assertEqual(rep.stats["published"], 2)
        for root, _d, files in os.walk(site):
            for f in files:
                if f.endswith(".xml"):
                    ET.parse(os.path.join(root, f))
        cat = ET.parse(os.path.join(site, "categories", "fashion.xml")).getroot()
        prods = cat.findall("./PRODUCTS/PRODUCT")
        self.assertEqual(len(prods), 2)
        self.assertEqual(prods[0].findtext("PRICE"), "79")
        self.assertIn("HDSALE", prods[0].findtext("PRODUCT_NAME"))
        info = ET.parse(os.path.join(site, "products", "A-1.xml")).getroot()
        self.assertEqual(info.findtext("StockQuantity"), "3")
        self.assertEqual(info.findtext("VariantGroupCode"), "G1")
        self.assertEqual(len(info.findall("./Variants/options/Option")), 2)
        self.assertLessEqual(len(info.findall("./Images/Image")), 4)
        self.assertTrue(os.path.exists(os.path.join(site, "index.html")))

    def test_zero_stock_and_inactive_not_published(self):
        rep, site = run_with([prow("A-1", "M", 0), prow("A-2", "L", 3, active="לא"), prow("A-3", "S", 1)])
        self.assertEqual(rep.stats["published"], 1)
        self.assertFalse(os.path.exists(os.path.join(site, "products", "A-1.xml")))
        self.assertFalse(os.path.exists(os.path.join(site, "products", "A-2.xml")))

    def test_duplicate_sku(self):
        rep, _ = run_with([prow("A-1", "M", 3), prow("A-1", "L", 3)])
        self.assertEqual(rep.stats["published"], 1)
        self.assertTrue(any("כפול" in e for e in rep.errors))

    def test_small_main_image_excluded(self):
        rep, _ = run_with([prow("A-1", "M", 3)], small=True)
        self.assertEqual(rep.stats["published"], 0)
        self.assertTrue(any("קטנה" in e for e in rep.errors))

    def test_empty_category_gets_page(self):
        rep, site = run_with([prow("A-1", "M", 0)])
        self.assertTrue(os.path.exists(os.path.join(site, "categories", "fashion.xml")))

    def test_missing_category_error(self):
        rep, _ = run_with([prow("A-1", "M", 3, cat="")])
        self.assertEqual(rep.stats["published"], 0)

    def test_price_override(self):
        _, site = run_with([prow("A-1", "M", 3, price="99.5")])
        cat = ET.parse(os.path.join(site, "categories", "fashion.xml")).getroot()
        self.assertEqual(cat.find("./PRODUCTS/PRODUCT/PRICE").text, "99.50")

    def test_html_stripped_and_truncation(self):
        self.assertEqual(g.clean_text("<p>a &amp; b</p>"), "a & b")
        long = "מילה " * 100
        self.assertLessEqual(len(g.clean_text(long, 255)), 255)
        self.assertFalse(g.cut_at_word(long, 255).endswith(" "))

    def test_illegal_chars_removed(self):
        self.assertEqual(g.clean_text("a\x00b\x0bc"), "abc")

    def test_url_key_matches_encoded(self):
        a = "https://www.hdsale.co.il/p/%D7%97%D7%95%D7%9C%D7%A6%D7%94-1"
        b = "https://www.hdsale.co.il/p/חולצה-1"
        self.assertEqual(g.url_key(a), g.url_key(b))

    def test_image_dimensions(self):
        self.assertEqual(g.image_dimensions(png(800, 900)), (800, 900))
        self.assertIsNone(g.image_dimensions(b"nope"))

    def test_sales_reduce_unknown_sku_warns(self):
        rep, _ = run_with([prow("A-1", "M", 3)], sales=[["h"], ["", "", "ZZZ", "1", "ord1"]])
        self.assertTrue(any("ZZZ" in w for w in rep.warnings))

    def test_invalid_sku(self):
        rep, _ = run_with([prow("A 1", "M", 3), prow("B-1", "M", 3)])
        self.assertTrue(any("לא תקין" in e for e in rep.errors))


if __name__ == "__main__":
    unittest.main()
