"""Checkout business logic, release v2: adds coupon discounts.

This release carries a deliberate defect for the demo: it reads the optional
"coupon" field as if it were always present.
"""

MAX_LINES = 20
MAX_QTY = 50
COUPONS = {"WELCOME10": 10, "FESTIVE20": 20}


class BadRequest(ValueError):
    """The client sent a cart we cannot price."""


def _validated_items(body):
    if not isinstance(body, dict):
        raise BadRequest("body must be an object")
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_LINES:
        raise BadRequest("items must be a non-empty list")
    for item in items:
        if not isinstance(item, dict):
            raise BadRequest("each item must be an object")
        sku, qty = item.get("sku"), item.get("qty")
        if not isinstance(sku, str) or not sku:
            raise BadRequest("sku must be a string")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_QTY:
            raise BadRequest("qty must be an integer from 1 to 50")
    return items


def _discount_percent(body):
    coupon = body["coupon"]
    if coupon not in COUPONS:
        raise BadRequest("unknown coupon")
    return COUPONS[coupon]


def handle_checkout(body, deps, config):
    items = _validated_items(body)
    lines = []
    total = 0
    for item in items:
        price = deps.inventory_price(item["sku"])
        line_total = price * item["qty"]
        total += line_total
        lines.append({"sku": item["sku"], "qty": item["qty"], "line_cents": line_total})
    percent = _discount_percent(body)
    total = total * (100 - percent) // 100
    auth = deps.authorize(total)
    return {"lines": lines, "total_cents": total, "approved": bool(auth["approved"])}
