"""Existing DAG fixtures model employee/payroll join keys as unique.

Keep their ordered business-result cassettes separate from the new preflight
cardinality reads. Those reads still run through the real scoped dispatcher and
are recorded; fanout/refusal behavior has its own harness regression tests.
"""

from data_agent.runtime.mcp.fake_client import FakeMCPClient, RecordedCall


def is_cardinality_probe(args):
    sql = args.get("sql", "")
    return " AS row_count" in sql and " AS distinct_count" in sql and "uniqExact" in sql


class UniqueJoinKeysMCP(FakeMCPClient):
    async def call_tool(self, tool_name, args, *, jwt, session_id):
        if tool_name == "runQuery" and is_cardinality_probe(args):
            self.calls.append(RecordedCall(tool_name, args, jwt, session_id))
            return {
                "columns": ["row_count", "distinct_count"],
                "rows": [[2, 2]],
                "row_count": 1,
                "truncated": False,
            }
        return await super().call_tool(tool_name, args, jwt=jwt, session_id=session_id)


def execution_calls(client):
    return [call for call in client.calls if not is_cardinality_probe(call.args)]
