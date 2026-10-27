"""Expose each discovered MCP tool separately; never a generic execution bypass."""
import hashlib
from agent.business_bridge import BusinessBackendBridge
from agent.errors import AgentError, Code
from agent.tools.contract import ToolSpec, SOURCE_MCP, RISK_READ, RISK_WRITE


class McpProvider(BusinessBackendBridge):
    def _entries(self, actor):
        store = self._backend.mcp_store
        if store is None:
            return []
        entries = []
        for connection in store.list(actor.user_id):
            if not connection["enabled"] or not connection.get("tools_version"):
                continue
            for tool in store.list_tools(actor.user_id, connection["id"]):
                name = "mcp_" + hashlib.sha256((connection["id"] + ":" + tool["name"]).encode()).hexdigest()[:24]
                spec = ToolSpec(name=name, title=f"{connection['name']} · {tool['name']}",
                    description=tool["description"][:4000], parameters=tool["schema"],
                    risk=RISK_WRITE if tool["writes"] else RISK_READ,
                    billable=tool["billable"], source=SOURCE_MCP, server_id=connection["id"],
                    version=tool["version"], requires_confirmation=True,
                    supports_status=False, supports_cancel=False)
                entries.append((spec, connection["id"], tool["name"]))
        return entries

    def list_tools(self, actor):
        return [entry[0] for entry in self._entries(actor)]

    def _business_name(self, spec):
        return "mcp.execute"

    def _arguments(self, actor, spec, arguments):
        for current, connection_id, name in self._entries(actor):
            if current.name == spec.name and current.version == spec.version:
                return {"connection_id": connection_id, "tool_name": name, "arguments": arguments}
        raise AgentError(Code.ACTION_STALE, "MCP 连接或工具已更新，请重新发现并确认", http_status=409)

    def prepare(self, actor, spec, arguments, context):
        return super().prepare(actor, spec, self._arguments(actor, spec, arguments), context)

    def execute(self, actor, spec, arguments, *, request_key, context, prepared_id=None):
        wrapped = self._arguments(actor, spec, arguments)
        if not prepared_id:
            prepared_id = super().prepare(actor, spec, wrapped, context)["prepared_id"]
        return super().execute(actor, spec, wrapped, request_key=request_key, context=context, prepared_id=prepared_id)
