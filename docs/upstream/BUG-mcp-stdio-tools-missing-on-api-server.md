# Bug: `hermes mcp add`-ed stdio servers' tools never reach the model on the api_server path

**Version:** v2026.9.24 (0.21.5), single-profile gateway, Linux, Python 3.12
**Severity:** the server is listed as `✓ enabled` everywhere the user can look, yet the
model reports the tools do not exist. Silent, not an error.

## Symptom

Four stdio MCP servers are added and enabled. The model never sees their tools:

```
> Use tool_search for 'jira'. Only the tool names.
Nothing found — no Jira tool is connected.

> Call tool_call with name='mcp__kozos__recall'
"'mcp__kozos__recall' is not a known tool name. Deferred tools must be invoked
 through tool_call by the exact name tool_search returns..."
```

The deferred pool the model can see contains only native tools
(`cronjob`, `session_search`, `terminal`, `todo`).

## What is verifiably correct

| check | result |
|---|---|
| `hermes mcp add <name> --command <py> --args <script>` | `✓ Connected! Found N tool(s)` for all four |
| `hermes mcp list` | all four `✓ enabled` |
| `config.yaml` → `mcp_servers` | correct `command` / `args` / `enabled: true` |
| server processes | start and answer `ListToolsRequest` (`logs/mcp-stderr.log`) |
| `cache/mcp_schema_cache.json` | present, 23 KB |
| `tools.mcp_tool_config._load_mcp_config()` | returns all four (scoped **and** unscoped) |
| `tools.mcp_tool_discovery._owner_scope_home()` | `None` (single-profile — expected) |
| `_get_platform_tools(cfg, "api_server")` | includes `kozos`, `behavior`, `reminder`, `atlassian-ro` (19 toolsets) |
| `registry.get_tool_to_toolset_map()` | contains `mcp-kozos`, `mcp-behavior`, `mcp-reminder`, `mcp-atlassian-ro` |

## Reproduction of the working path (plain process)

```python
from model_tools import get_tool_definitions
from tools.mcp_tool_discovery import discover_mcp_tools
# catalog embedded in the tool_search description:
#   before discovery : 0  mcp__* names
discover_mcp_tools()
#   after  discovery : 33 mcp__* names
```

This also works when run exactly the way the gateway does it — on an executor
thread through `copy_context().run` (`gateway/run.py::_discover_gateway_mcp_tools`):

```
induláskor (main thread)                  catalog:  0 mcp__
discover on executor thread -> main       catalog: 33 mcp__
```

And it is independent of the toolset filter — both `enabled_toolsets=None` and the
explicit 19-name api_server list yield a catalog with 33 `mcp__*` names.

## Where it breaks

Startup discovery runs (servers start, `ListToolsRequest` logged), and tool
assembly in-process produces the correct catalog — but in the **serving turn** the
names are unknown. The registration does not survive into the request context.

Tried, did not help:
- `refresh_agent_mcp_tools(agent, quiet_mode=True, preserve_prefix=True)` at the end
  of `api_server._create_agent` — returns an EMPTY set, i.e. it derives from a
  registry that is empty at that point (its own docstring describes exactly this
  case: *"servers that connect later ... are invisible until rebuilt"*).
- calling `register_mcp_servers(_load_mcp_config())` from
  `_discover_gateway_mcp_tools` before `discover_mcp_tools()` — no effect.

Candidate area: the registration scope. `discover_mcp_tools` registers inside
`_owner_secret_scope()`; `_core._server_scope_keys[...] = _core._mcp_registry_scope()`
(`mcp_tool_discovery.py:216,340`), and `_deregister_mcp_tool_all_scopes` /
`_reregister_orphaned_adopters` show registrations are scope-keyed. On a
single-profile gateway `_mcp_registry_scope()` is `None`, so the intent is "no
scope" — but the serving turn evidently resolves a different scope key.

## Why it matters

There is no user-visible failure anywhere: `hermes mcp list` says enabled, the
schema cache is warm, the servers are running. Only the model says the tool does
not exist — which reads to the user as the model refusing to use a tool it has.
