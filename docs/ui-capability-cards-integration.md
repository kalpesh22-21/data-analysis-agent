# Prepared actions in answer-document v2

Render prepared actions from `answer_document.sections[].actions`, each containing
`capability_ref` and a reviewed `label`. On selection, request authenticated
`GET /answer/action?answer_id=<id>&capability_ref=<ref>`. Lane5 resolves the approved,
persisted action under the caller's current scope. A 404 means unavailable; do not
construct an action or launch it from model prose. Action resolution is separate
from SQL result paging.

Data presentations use [answer-document v2](ui-answer-document-spec.md). Their
sources carry SQL and formats; no raw capability payload or old top-level card
array is required to render an answer. Text sections may carry prepared actions.
Paused turns have no sealed document.

Table/CSV options are UI-owned for all data results except figures. Prepared actions
are not data-download permissions. Do not duplicate a data table just to expose an
action or CSV. Preserve explicit action authorization after history reload/refresh.
