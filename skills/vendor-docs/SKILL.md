---
name: vendor-docs
description: Use when checking gateways, model names, tool support, context limits, pricing, or provider routing
---
# Vendor and gateway documentation

Resolve provider questions from current endpoint evidence and authoritative
documentation without exposing credentials.

## Decision path

1. Identify the actual base URL and gateway first. Route and transport security
   follow the endpoint, not the marketed model family.
2. Consult official or locally captured API contracts, then distinguish
   documented behavior from a dated probe and from inference.
3. Treat model catalogs as visibility only. Validate tool schema, context,
   parameters, response model identity, and usage fields on the exact route when
   a live test is authorized.
4. Record requested and served model IDs; aggregators may silently substitute a
   version even when HTTP status is successful.
5. Label cost, quota, tool support, and availability with observation dates.
   Do not cache a dynamic model pool as a permanent capability table.

## Safety

- Never print, copy, or inspect credential values. Use environment variables and
  redacted diagnostics.
- Do not send project or medical data in a canary. Use a fixed synthetic prompt.
- A 403 can be a route or model-permission failure; discriminate with endpoint
  and route evidence before blaming the key.
- Do not make a paid or token-generating probe unless the user requested it or
  the task already authorizes that provider call.

References — where a deployment keeps its own gateway notes varies, so read
whatever is actually present instead of assuming a path:

- The official API documentation for the endpoint actually in use. The endpoint
  decides route, transport security, and which models exist; the model family
  name does not.
- Any gateway or key audit notes this workspace happens to keep.
- `core/models.py` for zylab's current local catalog, including which
  capabilities were measured versus assumed.
