"""
Quick isolated test: confirms agent_engine_azure.py can actually connect
to Azure SQL and run the agent end-to-end, before wiring it into app.py.
Run this once after setting up your Azure SQL environment variables.
"""
from contextlib import closing

from agent_engine_azure import get_connection, run_agent

# Unambiguous case: should call verify_order_modification and succeed.
test_email = (
    "Hi, this is The Crown Pub. Can you update order ORD-9941 -- "
    "we'd like to bump the Orchard Press Cider Original up to 15 "
    "cases instead of 10, please. Thanks!"
)

print("Testing connection to Azure SQL via agent_engine_azure...")
print("-" * 70)

with closing(get_connection()) as conn:
    tool_log, final_result, reply = run_agent(conn, test_email)

print(f"Tool chain: {' -> '.join(c['tool_called'] for c in tool_log) if tool_log else 'NONE'}")
print(f"Final status: {final_result.get('status', '?')}")
print(f"Reply:\n{reply}")
print("-" * 70)

if final_result.get("status") == "Success":
    print("✅ Azure SQL connection confirmed working end-to-end.")
    print("Note: this only CHECKED feasibility -- nothing was written yet,")
    print("since the actual write is gated behind the approval step in app.py.")
else:
    print(f"⚠️  Got status '{final_result.get('status')}' -- check the reply above for details.")