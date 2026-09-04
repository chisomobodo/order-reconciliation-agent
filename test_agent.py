"""
Quick manual test for run_agent — one unambiguous email, one deliberately
ambiguous one. Prints which tool Claude called and the final customer-facing
reply for each, so the clarification path can be eyeballed.

Requires mock_erp.db to already exist (run setup_db.py first) and
ANTHROPIC_API_KEY to be set.
"""
from contextlib import closing

from agent_engine import get_connection, run_agent

TEST_EMAILS = {
    "unambiguous (ORD-9941, cider original)": """\
Hi, this is The Crown Pub. Can you update order ORD-9941 — we'd like to
bump the Orchard Press Cider Original up to 15 cases instead of 10, please.
Thanks!
""",
    "ambiguous (lager, no keg size given)": """\
Hi, we need to increase our lager order to 30 units — just the regular
kegs, not the cider. Let me know what you need from us. Thanks!
""",
}

if __name__ == "__main__":
    for label, email in TEST_EMAILS.items():
        print("=" * 70)
        print(f"TEST CASE: {label}")
        print("-" * 70)
        print(email.strip())
        print("-" * 70)

        with closing(get_connection()) as conn:
            tool_call, result, reply_text = run_agent(conn, email)

        if tool_call is None:
            print("No tool called.")
        else:
            print(f"Tool called: {tool_call['tool_called']}")
            print(f"Tool inputs: {tool_call['inputs']}")

        print(f"Tool result: {result}")
        print()
        print("Final reply to customer:")
        print(reply_text)
        print()
