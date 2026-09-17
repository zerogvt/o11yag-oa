"""The fake system of record.

In-memory and reset on every pod restart, which is the right trade for a demo
but is also exactly why nothing here should be mistaken for a real CRM: the
audit record of a refund outlives the refund itself.
"""
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

TICKETS = {}
REFUNDS = []
