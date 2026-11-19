# Alien MCP Gateway

A gateway you host in front of Alien MCP servers.

- It accepts MCP clients (Claude, ChatGPT, Copilot, VS Code, Cursor) that sign in with **your own OAuth server**, for example Keycloak.
- It maps each verified identity to an Alien API key through a pluggable credential resolver.
- It forwards the MCP traffic to Alien unchanged.

Your users sign in with their usual accounts, and your MCP endpoint lives on your own domain.

Status: in development. The setup tutorial will be published here before the first release.
