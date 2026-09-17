"""The support knowledge base, inline.

Eight short policy documents. Inline rather than mounted so the stack seeds
itself with no extra Job, ConfigMap or volume to forget about.

These are all benign. The indirect-prompt-injection demo — a document whose text
instructs the agent to call a tool — belongs in this corpus, and this is where
it will go, but it is deliberately not here in v1: the security act should be
built and labelled as such, not smuggled into the reference stack.
"""

DOCS = [
    ("kb-returns-01",
     "Returns policy: customers may return any unopened item within 30 days of "
     "delivery for a full refund. Opened items can be returned within 14 days if "
     "faulty. Return shipping is free for faulty items, otherwise 4.99 EUR."),
    ("kb-refunds-01",
     "Refund processing: approved refunds are issued to the original payment "
     "method and take 3-5 business days to appear. Refunds above 100 EUR require "
     "a supervisor to approve them before they are issued."),
    ("kb-shipping-01",
     "Shipping: standard delivery is 3-5 business days within the EU. Express "
     "delivery is next business day if ordered before 14:00 CET. We do not ship "
     "to PO boxes."),
    ("kb-warranty-01",
     "Warranty: all electronics carry a 24-month manufacturer warranty. Accidental "
     "damage is not covered. A warranty claim needs the order number and a photo "
     "of the fault."),
    ("kb-cancellation-01",
     "Order cancellation: orders can be cancelled free of charge until they enter "
     "the packing stage. After that the order must be received and returned "
     "instead. Packing usually starts within 2 hours of ordering."),
    ("kb-accounts-01",
     "Accounts: customers can change their delivery address themselves in the "
     "account portal until the order ships. Email address changes require identity "
     "verification by support."),
    ("kb-payments-01",
     "Payments: we accept card, SEPA direct debit and PayPal. A failed payment is "
     "retried once after 24 hours before the order is cancelled automatically."),
    ("kb-escalation-01",
     "Escalation: any message mentioning legal action, a regulator, or asking "
     "explicitly for a human must be handed to a human agent without an automated "
     "answer being given first."),
]
