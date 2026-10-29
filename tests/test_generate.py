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
        "כמות לפרסום", "פעיל", "מחיר", "קטגוריה", "הערות", "אישור הגדלת תמונה"]


def prow(sku, size, qty, active="כן", url="https://www.hdsale.co.il/p/shirt", cat="אופנה", price="", approve=""):
    return [sku, "G1", url, "חולצה", size, "שחור", "5", "0", "5", str(qty), active, price, cat, "", approve]


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
        self.assertTrue(rep.approvals or any("קטנה" in e for e in rep.errors))

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


    def test_nested_categories(self):
        rep, site = run_with([prow("A-1", "M", 3, cat="אופנה > גרביים"), prow("A-2", "L", 2, cat="אופנה")])
        self.assertEqual(rep.stats["published"], 2)
        html = open(os.path.join(site, "index.html"), encoding="utf-8").read()
        ET.fromstring(html.replace("<!DOCTYPE html>", "").replace('<meta charset="UTF-8">', '<meta charset="UTF-8"/>').replace('<meta name="viewport" content="width=device-width, initial-scale=1">', ""))
        self.assertIn("<ul>", html)
        self.assertEqual(rep.per_category["אופנה"], 2)
        self.assertEqual(rep.per_category["אופנה > גרביים"], 1)

    def test_norm_category(self):
        self.assertEqual(g.norm_category(" אופנה/ גרביים "), "אופנה > גרביים")

    def test_pause_switch_publishes_empty(self):
        global SETTINGS
        old = SETTINGS
        SETTINGS = old + [["השבתת פרסום", "כן"]]
        try:
            rep, site = run_with([prow("A-1", "M", 3), prow("A-2", "L", 2)])
        finally:
            SETTINGS = old
        self.assertEqual(rep.stats["published"], 0)
        self.assertFalse(os.path.exists(os.path.join(site, "products", "A-1.xml")))
        cat = ET.parse(os.path.join(site, "categories", "fashion.xml")).getroot()
        self.assertEqual(len(cat.findall("./PRODUCTS/PRODUCT")), 0)

    def test_inactive_row_with_stock_not_published(self):
        rep, site = run_with([prow("A-1", "M", 5, active="לא"), prow("A-2", "L", 5)])
        self.assertEqual(rep.stats["published"], 1)
        self.assertFalse(os.path.exists(os.path.join(site, "products", "A-1.xml")))

    def test_switching_everything_off_is_not_a_fatal_error(self):
        rows = [prow(f"B-{i}", "M", 5, active="לא") for i in range(12)]
        rep, _ = run_with(rows)
        self.assertEqual(rep.stats["published"], 0)

    def test_broken_source_still_trips_safety_net(self):
        rows = [prow(f"C-{i}", "M", 5, url="") for i in range(12)]
        with self.assertRaises(g.FatalError):
            run_with(rows)

    def test_small_image_waits_for_approval(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        import io

        buf = io.BytesIO()
        Image.new("RGB", (600, 580), (200, 30, 30)).save(buf, "JPEG")
        small_jpg = buf.getvalue()

        class RealSmall(FakeFetcher):
            def get(self, url, max_bytes=None):
                if url.endswith(".jpg") and "ecdn" in url:
                    return small_jpg
                return super().get(url, max_bytes)

        rows = [prow(f"P-{i}", "M", 3) for i in range(12)]  # many pending: must not trip the safety net
        tmp = tempfile.mkdtemp()
        data = {"settings": SETTINGS, "products": [HEAD] + rows, "sales": [["h"]]}
        rep = g.run(RealSmall(), "x", {"products": 1, "settings": 2, "sales": 3},
                    "https://www.hdsale.co.il/mirrors/zap/catalog.xml", "https://example.test/zap",
                    os.path.join(tmp, "site"), os.path.join(tmp, "rep"), os.path.join(tmp, "cache"),
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terms.txt"),
                    sheet_reader=lambda n: data[n])
        self.assertEqual(rep.stats["published"], 0)
        self.assertEqual(list(rep.approvals), ["G1"])
        self.assertIn("600x580", rep.approvals["G1"])
        self.assertIn("ממתינים לאישור", rep.to_markdown())

    def test_small_main_image_is_upscaled(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        import io

        buf = io.BytesIO()
        Image.new("RGB", (600, 580), (200, 30, 30)).save(buf, "JPEG")
        small_jpg = buf.getvalue()

        class RealSmall(FakeFetcher):
            def get(self, url, max_bytes=None):
                if url.endswith(".jpg") and "ecdn" in url:
                    return small_jpg
                return super().get(url, max_bytes)

        tmp = tempfile.mkdtemp()
        data = {"settings": SETTINGS, "products": [HEAD, prow("A-1", "M", 3, approve="כן")], "sales": [["h"]]}
        rep = g.run(RealSmall(), "x", {"products": 1, "settings": 2, "sales": 3},
                    "https://www.hdsale.co.il/mirrors/zap/catalog.xml", "https://example.test/zap",
                    os.path.join(tmp, "site"), os.path.join(tmp, "rep"), os.path.join(tmp, "cache"),
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terms.txt"),
                    sheet_reader=lambda n: data[n])
        self.assertEqual(rep.stats["published"], 1)
        files = os.listdir(os.path.join(tmp, "site", "images"))
        self.assertEqual(len(files), 1)
        im = Image.open(os.path.join(tmp, "site", "images", files[0]))
        self.assertEqual(im.size, (1024, 1024))
        info = ET.parse(os.path.join(tmp, "site", "products", "A-1.xml")).getroot()
        self.assertTrue(info.find("./Images/Image").get("url").startswith("https://example.test/zap/images/"))

    def _run_secondary(self, size, approve="כן", groups=("G1",)):
        """Main image is fine (1024); the secondary IMG2 is a real JPEG of `size`."""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        import io
        from unittest import mock

        buf = io.BytesIO()
        Image.new("RGB", size, (30, 30, 200)).save(buf, "JPEG")
        second_jpg = buf.getvalue()

        class SmallSecond(FakeFetcher):
            def get(self, url, max_bytes=None):
                if url.endswith(".jpg") and "ecdn" in url and IMG2 in url:
                    return second_jpg
                return super().get(url, max_bytes)

        tmp = tempfile.mkdtemp()
        data = {"settings": SETTINGS, "products": [HEAD, prow("A-1", "M", 3, approve=approve), prow("A-2", "L", 3)],
                "sales": [["h"]]}
        with mock.patch.object(g, "SECONDARY_UPSCALE_GROUPS", set(groups)):
            rep = g.run(SmallSecond(), "x", {"products": 1, "settings": 2, "sales": 3},
                        "https://www.hdsale.co.il/mirrors/zap/catalog.xml", "https://example.test/zap",
                        os.path.join(tmp, "site"), os.path.join(tmp, "rep"), os.path.join(tmp, "cache"),
                        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terms.txt"),
                        sheet_reader=lambda n: data[n])
        site = os.path.join(tmp, "site")
        urls = [i.get("url") for i in ET.parse(os.path.join(site, "products", "A-1.xml")).getroot().findall("./Images/Image")]
        review = open(os.path.join(site, "review.html"), encoding="utf-8").read()
        return rep, site, urls, review

    def test_secondary_image_upscaled_for_trial_group(self):
        from PIL import Image

        rep, site, urls, review = self._run_secondary((600, 500))
        self.assertEqual(rep.stats["published"], 2)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[1], f"https://example.test/zap/images/{IMG2}.jpg")
        self.assertEqual(Image.open(os.path.join(site, "images", f"{IMG2}.jpg")).size, (1024, 1024))
        # One image shared by both variants: one review entry listing both SKUs.
        self.assertEqual(list(rep.upscaled), [IMG2])
        self.assertEqual(rep.upscaled[IMG2]["skus"], ["A-1", "A-2"])
        self.assertIn(IMG2, review)
        self.assertIn("600x500", review)
        self.assertIn("1024x1024", review)
        self.assertIn(f"images/{IMG2}.jpg", review)
        self.assertIn(f"https://ecdn.crosphera.net/{BASE}/{IMG2}.jpg", review)

    def test_secondary_image_not_upscaled_outside_trial(self):
        rep, site, urls, review = self._run_secondary((600, 500), groups=("18890113",))
        self.assertEqual(rep.stats["published"], 2)
        self.assertEqual(len(urls), 1)
        self.assertFalse(os.path.exists(os.path.join(site, "images")))
        self.assertEqual(rep.upscaled, {})
        self.assertIn("לא הוגדלו", review)

    def test_secondary_image_needs_approval(self):
        rep, site, urls, _ = self._run_secondary((600, 500), approve="")
        self.assertEqual(rep.stats["published"], 2)
        self.assertEqual(len(urls), 1)
        self.assertEqual(rep.upscaled, {})
        self.assertFalse(rep.approvals)  # a skipped secondary never holds the product back

    def test_secondary_image_below_400_dropped(self):
        rep, site, urls, _ = self._run_secondary((390, 500))
        self.assertEqual(rep.stats["published"], 2)
        self.assertEqual(len(urls), 1)
        self.assertEqual(rep.upscaled, {})
        self.assertTrue(any("390x500" in m for m in rep.infos))

    def test_review_page_built_every_run(self):
        _, site = run_with([prow("A-1", "M", 3)])
        review = open(os.path.join(site, "review.html"), encoding="utf-8").read()
        self.assertIn("לא הוגדלו", review)


if __name__ == "__main__":
    unittest.main()