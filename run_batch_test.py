"""
Runs every email in sample_emails.json through run_agent() and reports
the routing decision + result for each, grouped by category. This is
the real test of the agent design across a realistic spread of cases,
not just the two hand-picked examples from earlier.
"""
import json

from agent_engine import run_agent


def run_batch():
    with open("sample_emails.json") as f:
        emails = json.load(f)

    results = []

    for email in emails:
        print("=" * 70)
        print(f"[{email['category'].upper()}] {email['id']} — {email['subject']}")
        print("-" * 70)
        print(email["body"])
        print("-" * 70)

        try:
            extracted, tool_result, reply = run_agent(email["body"])

            if not extracted:
                tool_chain = "NONE"
            else:
                # extracted is now a list of tool calls (could be more than
                # one, e.g. get_order_details then verify_order_modification)
                tool_chain = " -> ".join(call["tool_called"] for call in extracted)

            status = tool_result.get("status", "?")

            print(f"Tools called: {tool_chain}")
            print(f"Status: {status}")
            print(f"Reply:\n{reply}")

            results.append({
                "id": email["id"],
                "category": email["category"],
                "tool_called": tool_chain,
                "status": status,
            })
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({
                "id": email["id"],
                "category": email["category"],
                "tool_called": "ERROR",
                "status": str(e),
            })

    # --- Summary ---
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        print(f"{r['id']:12} [{r['category']:12}] tool={r['tool_called']:28} status={r['status']}")

    # Sanity flags worth eyeballing manually afterward:
    print("\nCheck manually:")
    print("- Did every 'ambiguous' email trigger request_clarification (not verify_order_modification)?")
    print("- Did every 'unambiguous' email trigger verify_order_modification?")
    print("- Did the edge cases (bad order ID, dispatched order, vague email) get handled sensibly")
    print("  rather than crashing or silently succeeding?")

    with open("batch_test_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nFull results saved to batch_test_results.json")


if __name__ == "__main__":
    run_batch()