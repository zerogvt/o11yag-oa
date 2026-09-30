"""The ticket corpus the load generator draws from.

Weighted so the traffic looks like a support queue rather than a uniform mix:
mostly policy questions, a steady trickle of order actions, the occasional
escalation. The order ids match mcp-crm's fake system of record.

Two kinds of entry. A fixed ticket is sent word for word. A templated one
(customer None, text naming a template set) is filled in when it is picked: a
random order from mcp-crm's generated catalogue, its owner, and a random
phrasing. Without that, every refund ticket named the same order, and after the
first one the CRM could only answer `already_refunded`.

ORD-1001 and ORD-1004 stay fixed because the docs and demos name them. ORD-1004
is 430 EUR, above the approval gate's auto-approve ceiling — that ticket always
stops and waits for a person. About a third of the generated orders are over
the ceiling too, so a templated refund stops at the gate some of the time.

The weights keep the mix the docs quote: 6 order actions in 23.
"""
import random

# Must match mcp-crm/data.py: the generated order range and owner_of().
GENERATED_FIRST = 1005
GENERATED_LAST = 1200


def owner_of(n: int) -> str:
    return f"C-{100 + n % 40}"


# Every refund phrasing must contain "refund" (the action worker's rules look
# for it) and none may contain an escalation word the orchestrator routes on —
# "lawyer", "sue" (so not "issue"), "manager", "human", "complaint".
TEMPLATES = {
    "refund": [
        "I want a refund for order {order}, it stopped working after a week.",
        "Please refund {order}, it arrived damaged.",
        "Can I get a refund on {order}? It is not what I ordered.",
        "I'd like a refund for {order}, the item is faulty.",
        "Order {order} arrived broken, please refund it.",
        "Requesting a refund for {order}, it does not fit.",
    ],
    "status": [
        "What is the status of order {order}?",
        "Where is my order {order}?",
        "Has order {order} shipped yet?",
        "Can you tell me when {order} will arrive?",
    ],
}

TICKETS = [
    # (weight, customer_id, text)
    (3, "C-7",  "What is your returns policy for something I opened?"),
    (3, "C-12", "How long does standard delivery take within the EU?"),
    (3, "C-31", "Is accidental damage covered by the warranty?"),
    (2, "C-7",  "Can I still cancel an order that was placed an hour ago?"),
    (2, "C-12", "How do I change the delivery address on my order?"),
    (2, "C-7",  "How long do refunds take to show up on my card?"),
    (1, "C-7",  "I want a refund for order ORD-1001, the headphones stopped working."),
    (1, "C-31", "I need a refund on ORD-1004, the office chair is the wrong colour."),
    (3, None,   "refund"),     # templated: random generated order
    (1, None,   "status"),     # templated: random generated order
    (1, "C-31", "This is unacceptable, I want to speak to a human manager right now."),
    (1, "C-7",  "If this isn't resolved today I'm contacting my lawyer."),
]

# Build the list the load generator actually picks from.
#
# Each ticket above has a weight. We simply add that ticket to the list `weight`
# times — so a ticket with weight 3 appears 3 times and a ticket with weight 1
# appears once. random.choice() then picks uniformly from this list, which makes
# the weight-3 tickets turn up three times as often. No probability maths needed.
POPULATION = []
for weight, customer_id, text in TICKETS:
    for _ in range(weight):
        POPULATION.append((customer_id, text))


def pick():
    """Return (customer_id, text) for the next ticket."""
    customer_id, text = random.choice(POPULATION)
    if customer_id is None:
        n = random.randint(GENERATED_FIRST, GENERATED_LAST)
        customer_id = owner_of(n)
        text = random.choice(TEMPLATES[text]).format(order=f"ORD-{n}")
    return customer_id, text
