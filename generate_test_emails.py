"""
Generates a batch of synthetic, realistic wholesale order-change emails
for testing the reconciliation agent — including deliberately ambiguous
ones (matching the lager/cider SKU pairs in setup_db.py) alongside clean,
unambiguous ones.

All output is fictional and generated for demo/testing purposes only.
Run this once to produce sample_emails.json, which the dashboard and
future tests can draw from instead of relying on 2-3 hand-written cases.
"""
import json

import anthropic

client = anthropic.Anthropic()
MODEL = "claude-sonnet-5"

GENERATION_PROMPT = """Generate 15 realistic, messy wholesale order-change emails from pub/bar customers to a beverage distributor.

Context: the distributor's catalog includes these products (customers won't always know or use exact SKU codes):
- Highgate Lager, available as a 50L keg or a 30L keg
- Orchard Press Cider, available as Original or Lime variants
- Vale Wine Co. Pinot Grigio (sold by the case)

Existing sample orders in the system: ORD-9941 (The Crown Pub), ORD-8822 (The Rusty Anchor, already dispatched), ORD-7710 (The Feathers).

Vary the emails across these categories, aiming for roughly this mix:
- 6 emails that are UNAMBIGUOUS: they clearly specify which exact product variant they mean (e.g. "the 50L keg" or "the Lime cider"), reference an order ID, and request a clear quantity change or cancellation.
- 6 emails that are AMBIGUOUS: they reference a product category without specifying which variant (e.g. just "kegs" or "the cider" or "lager" without size), so a reader genuinely cannot tell which SKU they mean.
- 3 emails that are edge cases: one references an order ID that doesn't exist in the system, one tries to modify the already-dispatched order (ORD-8822), one has a typo-heavy or very vague request.

Vary tone (rushed, casual, overly formal, apologetic) and include realistic imperfections (typos, missing punctuation, run-on sentences) like real hospitality-industry emails.

Return ONLY a JSON array, no other text, no markdown code fences. Each object must have exactly these fields:
{"id": "email_01", "category": "unambiguous" | "ambiguous" | "edge_case", "subject": "...", "body": "..."}
"""


def generate_sample_emails():
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": GENERATION_PROMPT}],
    )

    raw_text = "".join(block.text for block in response.content if block.type == "text").strip()

    # Defensive cleanup in case the model wraps the JSON in fences despite
    # being told not to.
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        emails = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"Failed to parse Claude's response as JSON: {e}")
        print("--- raw response ---")
        print(raw_text)
        raise

    with open("sample_emails.json", "w") as f:
        json.dump(emails, f, indent=2)

    print(f"Generated {len(emails)} sample emails -> sample_emails.json")
    by_category = {}
    for e in emails:
        by_category[e["category"]] = by_category.get(e["category"], 0) + 1
    print("Breakdown:", by_category)


if __name__ == "__main__":
    generate_sample_emails()