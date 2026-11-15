"""Quantity sanity check for Remy's carts.

Catches the "six 32 oz tubs of yogurt instead of one" mistake: every line Remy
puts in a cart is compared with what the user usually orders (the learned
usual order, ``grocery:usual_order`` -- its ``qty`` is the most they have ever
bought of that product in one order). A line above that is flagged for the
user to confirm. Nothing here changes a quantity.

Pure functions, no Redis / network, so they are unit-testable.
"""

from __future__ import annotations

import re

# A product with no order history is flagged above this many units.
DEFAULT_MAX_QTY = 2


def _words(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9%]+", " ", str(s).lower()).split())


def usual_qty(usual_order: list[dict], store_key: str, *titles: str) -> int | None:
    """The most units of this product the user has bought per order, or None
    when there is no history for it. Matches on product title (or the item
    name) against learned entries at the same store (entries with no store
    are matched too, e.g. cart scans)."""
    cand_words = [w for w in (_words(t) for t in titles if t) if w]
    if not cand_words:
        return None
    best: int | None = None
    for u in usual_order or []:
        if u.get("store") and store_key and u.get("store") != store_key:
            continue
        uw = _words(u.get("title") or u.get("name") or "")
        if not uw:
            continue
        for cw in cand_words:
            overlap = len(cw & uw)
            # Same product: nearly all words of the smaller name are shared.
            if overlap and overlap / min(len(cw), len(uw)) >= 0.8 and overlap >= min(2, len(uw)):
                try:
                    q = max(1, int(u.get("qty") or 1))
                except (TypeError, ValueError):
                    q = 1
                best = q if best is None else max(best, q)
                break
    return best


def find_quantity_flags(lines: list[dict], usual_order: list[dict],
                        default_max: int = DEFAULT_MAX_QTY) -> list[dict]:
    """lines: [{"item", "product", "store_key", "store", "qty"}].
    Returns one flag per line whose qty is above the usual:
    {"item", "product", "store", "store_key", "qty", "usual_qty", "has_history"}.
    ``usual_qty`` is the user's past max, or ``default_max`` when no history."""
    flags = []
    for ln in lines:
        qty = int(ln.get("qty") or 1)
        hist = usual_qty(usual_order, ln.get("store_key", ""),
                         ln.get("product", ""), ln.get("item", ""))
        limit = hist if hist is not None else default_max
        if qty > limit:
            flags.append({
                "item": ln.get("item", ""),
                "product": ln.get("product") or ln.get("item", ""),
                "store": ln.get("store", ""),
                "store_key": ln.get("store_key", ""),
                "qty": qty,
                "usual_qty": limit,
                "has_history": hist is not None,
            })
    return flags


def describe_flag(f: dict) -> str:
    basis = ("you usually order" if f.get("has_history")
             else "no order history; anything over %d is flagged, so" % f["usual_qty"])
    return (f"{f['product'][:60]} @ {f.get('store', '?')}: cart has {f['qty']}, "
            f"{basis} {f['usual_qty']}")


def report_section(flags: list[dict]) -> list[str]:
    """Report lines (empty when nothing is flagged)."""
    if not flags:
        return []
    out = ["", f"⚠ QUANTITY CHECK — {len(flags)} item(s) need your approval",
           "  (higher than you usually order; quantities NOT changed)"]
    for f in flags:
        out.append(f"  ⚠ {describe_flag(f)}")
    out.append("  Confirm these quantities, or ask Remy to fix them, before checkout.")
    return out


def unconfirmed(pending: dict) -> list[dict]:
    """Flags on a pending order the user hasn't confirmed yet."""
    if pending.get("qty_confirmed"):
        return []
    return pending.get("qty_flags") or []
