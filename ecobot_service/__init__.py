"""The weather and air service on its own (docs/mcp-service.md 3.8, step 5): the readings file, the sources and syncers that fill it, the
alert rules that write the alert log, and the MCP server over them (lib/mcp_server.py).

  - `python -m ecobot_service` (__main__.py): the service, Streamable HTTP at http://MCP_SERVICE_BIND/mcp;
  - core.py: the service's startup (the sources, the alert log, the tools, the alert rules, the keep-warm, the first fetch, the archive,
    the backfills and Home Assistant's imports), the one function the service and the bot (ecobot.py, while it runs everything
    in-process) both call."""
