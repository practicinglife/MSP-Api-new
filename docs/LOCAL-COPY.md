# Keeping a local copy of an API's data

Any read operation of any connection can be kept in this server's database and refreshed on a schedule. People
and AI clients can then look things up instantly, without calling the API each time. This is the same "sync it,
then query it locally" approach the MSP pack uses for its vendors, available for every API you add.

## Turning it on
On the Connections page, open a **read** operation, fill in its arguments (they are used on every sync) and
press **Keep a local copy**. Pick how often: every 15 minutes, hourly, every 6 hours or daily. The first sync runs
right away. Approvers and Admins manage syncs; everyone with access to the connection can search the copy.

The same through the API:
```bash
curl -X POST https://tools.example.com/api/connections/desk/syncs -H "X-API-Key: <approver key>" \
  -H "Content-Type: application/json" \
  -d '{"operation": "list_tickets", "args": {"status": "open"}, "interval_minutes": 60}'
```
| Field | Meaning |
|---|---|
| `operation`, `args` | The read operation and the arguments used on every run |
| `interval_minutes` | 5 minutes to 7 days (default 60) |
| `records_path` | Where the list is in the response, e.g. `data.items`. Blank = found automatically (`value`, `results`, `items`, `data`, `rows`, `documents`, GraphQL `edges`/`nodes` …) |
| `key_field` | What identifies a record, e.g. `serialNumber` or `company.id`. Blank = `id` / `key` / `uuid` / `_id`, else a hash of the record |
| `paging` | Blank = detected. Or `{"type": "page", "param": "page", "start": 0}`, `{"type": "offset"}`, `{"type": "cursor", "param": "after", "cursor_path": "meta.next"}`, `{"type": "link"}`, `{"type": "none"}`; `size` sets the page size |
| `max_pages` | Safety limit per run (default 50, at most 1000) |

Other endpoints: `GET/PATCH/DELETE /api/connections/<c>/syncs/<id>` and `POST …/syncs/<id>/run?wait=true` (sync now).
Deleting a sync deletes its stored records; deleting a connection deletes all of them.

## Paging
Detected automatically, in this order: a `Link: <…>; rel="next"` header or a next-link field (`@odata.nextLink`,
`next` …) whose query parameters the operation accepts; a cursor in the response (`next_cursor`,
`nextPageToken`, GraphQL `pageInfo.endCursor` …) fed back into the operation's cursor parameter; a page or offset
parameter of the operation. When the operation has a page-size parameter, 100 per page is requested unless you
set one. A run stops at an empty page, a page with nothing new (an API that ignores the page parameter), a page
shorter than the page size, or `max_pages`.

## What a run does
- Every page is called the normal way (the connection's credentials, the outbound network guard, argument
  checks) as a read-only `sync` identity. Write operations can never be synced.
- New records are added, changed ones updated, unchanged ones only marked as seen.
- After a **complete** run, records the API no longer returns are marked removed. They are kept for history,
  hidden from searches unless asked for, and come back if they reappear. A run cut short by `max_pages` is
  `partial` and removes nothing.
- If a run fails (API down, bad credentials), the last good copy stays and the error is shown on the sync.
- One audit entry per run (`source = sync`) with the counts.
- `MSP_SYNC_MAX_RECORDS` (default 200,000) caps one sync; `MSP_CONNECTION_SYNC=off` stops the scheduler.

## Searching
- **Connections page**: open the connection → *Local copy*. Type words (names, numbers, emails, hostnames;
  typos are fine) and/or exact filters such as `status=open company.id=42`.
- **API**: `GET /api/connections/<c>/records?q=acme&f.status=open` (repeat `f.x` for any-of), one record with
  `GET /api/connections/<c>/records/<key>`, and every connection at once with `GET /api/search/records?q=…`.
  An exact record key always comes first.
- **AI clients**: a connection's MCP server gets `search_records` and `get_record` once it keeps a copy, and its
  instructions say what is kept and how fresh it is, so the client looks there first and calls the API only for
  live data. The hub (`/mcp`) searches every copy the caller may read.

Records are protected like the API they came from: a key or token without read access to the connection can't
see them.
