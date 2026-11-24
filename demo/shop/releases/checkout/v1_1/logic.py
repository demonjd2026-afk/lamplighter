"""Checkout business logic, release v1_1 (refactor only, same behaviour as v1)."""

MAX_LINES = 20
MAX_QTY = 50


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


def _price_line(item, deps):
    line_total = deps.inventory_price(item["sku"]) * item["qty"]
    return {"sku": item["sku"], "qty": item["qty"], "line_cents": line_total}


def handle_checkout(body, deps, config):
    lines = [_price_line(item, deps) for item in _validated_items(body)]
    total = sum(line["line_cents"] for line in lines)
    auth = deps.authorize(total)
    return {"lines": lines, "total_cents": total, "approved": bool(auth["approved"])}
