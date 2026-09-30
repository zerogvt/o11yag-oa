"""The fake system of record.

In-memory and reset on every pod restart, which is the right trade for a demo
but is also exactly why nothing here should be mistaken for a real CRM: the
audit record of a refund outlives the refund itself.
"""
import random

ORDERS = {
    "ORD-1001": {"customer_id": "C-7", "total_eur": 89.90, "status": "delivered",
                 "item": "wireless headphones", "days_since_delivery": 12},
    "ORD-1002": {"customer_id": "C-7", "total_eur": 249.00, "status": "delivered",
                 "item": "espresso machine", "days_since_delivery": 3},
    "ORD-1003": {"customer_id": "C-12", "total_eur": 19.50, "status": "packing",
                 "item": "usb-c cable", "days_since_delivery": 0},
    "ORD-1004": {"customer_id": "C-31", "total_eur": 430.00, "status": "shipped",
                 "item": "office chair", "days_since_delivery": 0},
}

# A larger catalogue for the load generator, so its refund tickets mostly land on
# an order nobody has refunded yet instead of replaying ORD-1001 forever. The
# four orders above stay hand-written because the docs and demos name them.
#
# Fixed seed: every pod restart builds the same catalogue, so the load generator
# can name any id in the range without asking. Each order's owner is derived
# from its number by owner_of(); loadgen/tickets.py repeats that rule, so change
# both together. Owners are C-100..C-139, never the hand-written customers, so
# list_customer_orders for C-7 still returns two orders, not sixty.
GENERATED_FIRST = 1005
GENERATED_LAST = 1200

_ITEMS = [
    # (item, typical price in EUR) — spread both sides of the auto-approve ceiling
    ("usb-c cable", 15), ("phone case", 25), ("desk lamp", 45), ("wireless mouse", 35),
    ("mechanical keyboard", 120), ("bluetooth speaker", 70), ("coffee grinder", 95),
    ("running shoes", 110), ("backpack", 60), ("monitor", 280), ("air fryer", 140),
    ("electric toothbrush", 55), ("kettle", 40), ("smart watch", 230), ("yoga mat", 30),
]


def owner_of(n: int) -> str:
    return f"C-{100 + n % 40}"


def _generate():
    rng = random.Random(GENERATED_FIRST)
    for n in range(GENERATED_FIRST, GENERATED_LAST + 1):
        item, price = rng.choice(_ITEMS)
        status = rng.choices(["delivered", "shipped", "packing"], weights=[7, 2, 1])[0]
        ORDERS[f"ORD-{n}"] = {
            "customer_id": owner_of(n),
            "total_eur": round(price * rng.uniform(0.8, 1.2), 2),
            "status": status,
            "item": item,
            "days_since_delivery": rng.randint(1, 40) if status == "delivered" else 0,
        }


_generate()

TICKETS = {}
REFUNDS = []
