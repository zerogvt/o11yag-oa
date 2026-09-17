"""The ticket corpus the load generator draws from.

Weighted so the traffic looks like a support queue rather than a uniform mix:
mostly policy questions, a steady trickle of order actions, the occasional
escalation. The order ids match mcp-crm's fake system of record.

ORD-1004 is 430 EUR, which is above the approval gate's auto-approve ceiling —
that ticket is the one that always stops and waits for a person.
"""
TICKETS = [
    # (weight, customer_id, text)
    (3, "C-7",  "What is your returns policy for something I opened?"),
    (3, "C-12", "How long does standard delivery take within the EU?"),
    (3, "C-31", "Is accidental damage covered by the warranty?"),
    (2, "C-7",  "Can I still cancel an order that was placed an hour ago?"),
    (2, "C-12", "How do I change the delivery address on my order?"),
    (2, "C-7",  "How long do refunds take to show up on my card?"),
    (2, "C-7",  "I want a refund for order ORD-1001, the headphones stopped working."),
    (2, "C-7",  "Please refund ORD-1002, the espresso machine arrived damaged."),
    (1, "C-31", "I need a refund on ORD-1004, the office chair is the wrong colour."),
    (1, "C-12", "What is the status of order ORD-1003?"),
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
