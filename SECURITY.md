# Security posture

## Safe defaults

- The MCP server uses stdio and does not expose an HTTP listening port.
- Credential-bearing provider endpoints must use HTTPS on port 443, resolve only
  to public IP addresses, and may be restricted with `allowed_hosts`.
- Tool exceptions do not return arbitrary upstream response bodies to the
  model. Search, page, news, and documentation results are explicitly marked
  as untrusted external data.
- Provider response bodies are capped at 5 MiB before decoding.
- Extracted-page caches have a 200,000-character per-entry limit, a 20 MiB
  aggregate limit, TTL cleanup, and owner-only mode where the platform honors
  POSIX file modes.

## Remaining operator responsibilities

The local user account and its MCP client remain the trust boundary. Keep
`config/.env`, `state/`, logs, and cache directories private. Review custom
provider configuration before loading it, keep dependencies patched, and treat
all remote content as reference data rather than executable instructions.
