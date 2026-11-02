"""Project delivered documents into historical finalizer examples, never executions."""

import hashlib
import json
from copy import deepcopy

from jsonschema import Draft202012Validator

from data_agent.runtime.composer import PRESENTATION_SCHEMA, PROPOSAL_SCHEMA, SECTION_SCHEMA


def delivered_answer_entry(message):
    document = message.answer_document
    review = message.review or {}
    if (
        message.role != "assistant"
        or not document
        or message.failure
        or review.get("status") not in {"approved", "disabled"}
        or document.get("schema_version") != 2
    ):
        return None
    sections = deepcopy(document.get("sections", []))
    try:
        for section in sections:
            p = section["presentation"]
            kind = p["kind"]
            if "x_column" in p:
                p["category_column"] = p.pop("x_column")
            if "x_columns" in p:
                p["category_columns"] = p.pop("x_columns")
            if kind == "heatmap":
                p["series_column"] = p.pop("y_column")
                p["measure_column"] = p.pop("value_column")
            elif "y_column" in p:
                p["measure_column"] = p.pop("y_column")
            if "ranking" in p:
                p.update(p.pop("ranking"))
            for series in p.get("series", []):
                series["measure_column"] = series.pop("y_column")
            section["presentation"] = {
                k: v for k, v in p.items() if k in PRESENTATION_SCHEMA["properties"]
            }
        sections = [
            {k: v for k, v in section.items() if k in SECTION_SCHEMA["properties"]}
            for section in sections
        ]
        # Older documents dropped table_reason. Do not manufacture justifications
        # or teach an invalid multi-table proposal from an incomplete record.
        tables = [s for s in sections if s["presentation"]["kind"] == "table"]
        if len(tables) > 1 and any(not s.get("table_reason", "").strip() for s in tables):
            return None
        args = {
            "takeaway": message.content,
            "sections": sections,
            "evidence": list(dict.fromkeys(r for s in sections for r in s["evidence_refs"])),
        }
        if not Draft202012Validator(PROPOSAL_SCHEMA).is_valid(args):
            return None
    except (KeyError, TypeError, AttributeError):
        return None
    identity = json.dumps(
        [message.turn_index, message.ts, document.get("answer_id"), document.get("revision")]
    )
    call_id = "history_final_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "tool_name": "finalizeAnswer",
        "args": args,
        "historical_delivery": True,
        "content": json.dumps(
            {
                "status": "delivered",
                "historical": True,
                "completion": review.get("completion", "unknown"),
                "answer_id": document.get("answer_id"),
                "revision": document.get("revision"),
                "sources": document.get("sources", {}),
            },
            ensure_ascii=False,
        ),
    }
