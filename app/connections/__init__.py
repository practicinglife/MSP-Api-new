"""Connect any API or data source and expose each operation as a callable, searchable tool.

Kinds (one adapter module each in ``app/connections/adapters/``):

=========  ==========================================================================================
openapi    OpenAPI 3.x / Swagger 2.0 (JSON or YAML, URL or pasted)
postman    Postman collection v2.0 / v2.1 — folders, auth, body modes and {{variables}} carry over
curl       One or more cURL commands
soap       SOAP / WSDL (incl. WCF / WS-Addressing), via zeep
graphql    Introspection of an endpoint, an SDL schema, or your own operation documents
odata      OData V2 and V4 ``$metadata`` (SAP Gateway labels and CSRF tokens included)
sql        PostgreSQL, SQLite built in; MySQL/MariaDB, SQL Server, Oracle, SAP HANA with optional drivers
mongodb    MongoDB (optional driver) — read-only
mcp        Another MCP server (streamable HTTP) — its tools served behind this app's auth and audit
=========  ==========================================================================================

Every adapter produces ``OperationDef`` rows at import time and executes them at call time. All calls go
through ``service.call_operation``: argument validation, the caller's scopes, the connection's write mode
(read_only / direct / approval), audit and response trimming.
"""
