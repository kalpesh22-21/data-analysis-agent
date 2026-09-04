# Help Center Search and Document API v1

This contract defines the two read-only APIs consumed by the Data Analysis Agent. Search returns article excerpts for recall; the agent service reranks those excerpts and exposes the top five to the model. Document retrieval returns the complete selected article.

## Shared requirements

- JSON encoded as UTF-8.
- HTTPS in deployed environments.
- User authentication: `Authorization: Bearer <jwt>`. The agent forwards the same JWT it receives from the UI for the current request. It does not forward the session id or column scope separately.
- Stable, opaque article IDs. IDs are case-sensitive and must remain valid while an article exists.
- Article content is plain text or Markdown. It must not contain executable HTML or scripts.
- A complete document must never exceed 4,000 tokens using the tokenizer agreed with the agent team. This limit includes the title/body if the service combines them in `content`.
- Clients may retry `429` and `5xx`; both operations must therefore be safe to repeat.
- Do not return secrets, access-controlled customer data, or user-specific data.

All error responses use:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Human-readable operational message",
    "request_id": "req_01J..."
  }
}
```

`code` is a stable machine-readable string. `message` must not expose stack traces, credentials, internal hosts, or query internals.

## Search API

`POST /v1/help-center/search`

Request:

```json
{
  "query": "How do I change an employee's position?",
  "limit": 25
}
```

Fields:

| Field | Type | Rules |
|---|---|---|
| `query` | string | Required, trimmed, 1–2,000 characters |
| `limit` | integer | Required, 1–100 |

Success (`200`):

```json
{
  "documents": [
    {
      "id": "article_12345",
      "score": 0.82,
      "snippet": "To change an employee position, open Personnel Action Forms..."
    }
  ]
}
```

Fields:

| Field | Type | Rules |
|---|---|---|
| `documents` | array | Required; zero to `limit` entries, ordered best-first by search score |
| `id` | string | Required, non-empty stable article ID |
| `score` | number | Required, finite; higher means more relevant |
| `snippet` | string | Required, non-empty excerpt from the article; recommended maximum 2,000 characters |

Multiple snippets may share an article ID when different sections match. The agent reranks snippets independently. Empty results are a successful `200` with `"documents": []`.

## Get document API

`GET /v1/help-center/documents/{id}`

`id` is percent-encoded as one URL path segment.

Success (`200`):

```json
{
  "id": "article_12345",
  "content": "# Change an employee's position\n\nComplete article content..."
}
```

Fields:

| Field | Type | Rules |
|---|---|---|
| `id` | string | Required; exactly matches the requested decoded ID |
| `content` | string | Required, non-empty complete article, at most 4,000 tokens |

Return `404` when the article does not exist or is no longer available. Do not substitute a related article.

## Status codes

| Status | Meaning |
|---|---|
| `200` | Success |
| `400` | Invalid request |
| `401` / `403` | Invalid user JWT or user not authorized to access the resource |
| `404` | Document not found; get API only |
| `429` | Rate limited; include `Retry-After` when possible |
| `500` / `502` / `503` / `504` | Transient service failure |

## Operational expectations

- Target timeout: respond within 10 seconds; normal p95 should be substantially lower.
- Include `Content-Type: application/json` on every response.
- Support distributed request correlation using a response `X-Request-Id`; accept an inbound `X-Request-Id` if supplied.
- Search indexes and get storage should be version-consistent enough that a returned ID is immediately retrievable. During deletion races, get may return `404`.
- Log request metadata and latency, but avoid logging full queries, snippets, or document bodies unless the environment has an explicitly approved content-logging policy.

## Acceptance examples

1. Searching with `limit: 25` never returns 26 entries.
2. A valid search with no match returns `200` and an empty array.
3. Every ID returned by search can be passed unchanged to get.
4. Get returns the entire article, not the matching search excerpt.
5. Scores reject JSON `NaN`/infinity and non-numeric strings.
6. No returned complete document exceeds the agreed 4,000-token ceiling.
