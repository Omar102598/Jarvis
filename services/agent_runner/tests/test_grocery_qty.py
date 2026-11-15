import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grocery_qty as q  # noqa: E402

YOGURT = "Chobani Vanilla Greek Yogurt Protein 32 oz"
USUAL = [
    {"title": YOGURT, "store": "heb", "qty": 1, "count": 5},
    {"title": "Good & Gather Large Eggs, 12ct", "store": "target", "qty": 3, "count": 2},
]


def line(product, qty, store_key="heb", item=""):
    return {"item": item or product, "product": product, "store_key": store_key,
            "store": store_key.upper(), "qty": qty}


class QtyTests(unittest.TestCase):
    def test_yogurt_six_flagged(self):
        f = q.find_quantity_flags([line(YOGURT, 6)], USUAL)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["qty"], f[0]["usual_qty"], f[0]["has_history"]), (6, 1, True))

    def test_usual_qty_ok(self):
        self.assertEqual(q.find_quantity_flags([line(YOGURT, 1)], USUAL), [])

    def test_uses_past_max(self):
        eggs = "Good & Gather Large Eggs, 12ct"
        self.assertEqual(q.find_quantity_flags([line(eggs, 3, "target")], USUAL), [])
        self.assertEqual(len(q.find_quantity_flags([line(eggs, 4, "target")], USUAL)), 1)

    def test_no_history_threshold(self):
        self.assertEqual(q.find_quantity_flags([line("Fresh Salmon", 2)], []), [])
        f = q.find_quantity_flags([line("Fresh Salmon", 3)], [])
        self.assertEqual((f[0]["usual_qty"], f[0]["has_history"]), (2, False))

    def test_other_store_history_ignored(self):
        f = q.find_quantity_flags([line(YOGURT, 2, "amazon")], USUAL[:1])
        self.assertEqual(f, [])  # no history at amazon -> default 2 applies
        self.assertEqual(len(q.find_quantity_flags([line(YOGURT, 3, "amazon")], USUAL[:1])), 1)

    def test_different_product_not_matched(self):
        self.assertIsNone(q.usual_qty(USUAL, "heb", "Beef Jerky"))

    def test_report_and_unconfirmed(self):
        f = q.find_quantity_flags([line(YOGURT, 6)], USUAL)
        text = "\n".join(q.report_section(f))
        self.assertIn("need your approval", text)
        self.assertIn("cart has 6", text)
        self.assertEqual(q.report_section([]), [])
        self.assertEqual(q.unconfirmed({"qty_flags": f}), f)
        self.assertEqual(q.unconfirmed({"qty_flags": f, "qty_confirmed": True}), [])


if __name__ == "__main__":
    unittest.main()
