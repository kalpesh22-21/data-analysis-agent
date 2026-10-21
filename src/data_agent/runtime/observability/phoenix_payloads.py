"""Phoenix-only export copies: bounded deltas, reusable definitions, selective masking.

No model messages, session documents, query arguments or live spans are mutated.
Deduplication is bounded and process-local; an eviction/restart safely repeats content.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections import OrderedDict

from opentelemetry import trace
from opentelemetry.sdk.trace import Event, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status

_NUMBER = re.compile(r"(?<![\w.])[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?!\w)")
_BEARER = re.compile(r"\bBearer\s+[^\s\"',;]+", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SECRET_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|service[_-]?key|password|client[_-]?secret|access[_-]?token|scope[_-]?token)\s*[:=]\s*[^\s,;]+"
)
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "jwt",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "scopetoken",
        "apikey",
        "servicekey",
        "xservicekey",
        "password",
        "clientsecret",
        "cookie",
        "setcookie",
    }
)
_METRICS = frozenset(
    {
        "k",
        "limit",
        "row_count",
        "column_count",
        "count",
        "truncated",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "duration_ms",
        "latency_ms",
    }
)
_ROW_KEYS = frozenset({"rows", "preview_rows", "result_rows"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _secret_key(key):
    return re.sub(r"[^a-z]", "", str(key).rsplit(".", 1)[-1].lower()) in _SECRET_KEYS


def sanitize(value, *, redact=False, compact=False, key="", depth=0):
    """Mask business content, preserving operational metrics and SQL structure."""
    if _secret_key(key):
        return "[secret]"
    if depth > 24:
        return "[omitted: nesting limit]"
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if (redact or compact) and name in _ROW_KEYS and isinstance(item, list):
                result[name] = "[SQL result values omitted]"
                result.setdefault("returned_row_count", len(item))
                continue
            # resolveValues and live column context contain SQL-derived cells too.
            if (
                (redact or compact)
                and name == "values"
                and isinstance(item, list)
                and ("ranking" in value or any(isinstance(v, dict) and "freq" in v for v in item))
            ):
                result[name] = "[SQL result values omitted]"
                result["returned_value_count"] = len(item)
                continue
            if (
                (redact or compact)
                and name == "context"
                and "name" in value
                and isinstance(item, dict)
            ):
                result[name] = {
                    k: {"row_count": len(v), "values": "[SQL result values omitted]"}
                    for k, v in item.items()
                    if isinstance(v, list)
                }
                continue
            result[name] = sanitize(item, redact=redact, compact=compact, key=name, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            sanitize(item, redact=redact, compact=compact, key=key, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return _json(sanitize(parsed, redact=redact, compact=compact, depth=depth + 1))
        text = _SECRET_TEXT.sub(
            lambda m: m[1] + "=[secret]",
            _JWT.sub("[secret]", _BEARER.sub("Bearer [secret]", value)),
        )
        return (
            _NUMBER.sub("[number]", text)
            if redact and key.rsplit(".", 1)[-1] not in _METRICS
            else text
        )
    if (
        redact
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and key.rsplit(".", 1)[-1] not in _METRICS
    ):
        return "[number]"
    return value


class SessionTraceProcessor(SpanProcessor):
    """Carry session and request context to child spans, including LLMs and tools."""

    def on_start(self, span, parent_context=None):
        parent = trace.get_current_span(parent_context)
        attrs = getattr(parent, "attributes", None) or {}
        for key in ("session.id", "metadata"):
            if attrs.get(key):
                span.set_attribute(key, attrs[key])

    def on_end(self, span):
        pass

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class PhoenixPayloadExporter(SpanExporter):
    """Transform only the downstream export, retaining IDs/timing/status and metrics."""

    def __init__(self, inner, *, redact=False, max_entries=4096, max_value_chars=16000):
        self.inner, self.redact = inner, redact
        self.max_entries, self.max_value_chars = max_entries, max_value_chars
        self.seen = OrderedDict()
        self.lock = threading.Lock()

    def _bound(self, value):
        encoded = _json(value)
        if len(encoded) <= self.max_value_chars:
            return value
        return {
            "preview": encoded[: self.max_value_chars],
            "omitted_chars": len(encoded) - self.max_value_chars,
        }

    def _reference(self, value, *, namespace, kind, span, ordinal=0):
        # Hash secret-free originals so distinct masked numbers don't collapse.
        clean = sanitize(value)
        digest = hashlib.sha256(_json(clean).encode()).hexdigest()
        key = (namespace, kind, digest, ordinal)
        previous = self.seen.get(key)
        if previous:
            self.seen.move_to_end(key)
            return {"ref": digest, **previous}
        location = {
            "trace_id": format(span.context.trace_id, "032x"),
            "span_id": format(span.context.span_id, "016x"),
        }
        self.seen[key] = location
        while len(self.seen) > self.max_entries:
            self.seen.popitem(last=False)
        return {
            "hash": digest,
            "kind": kind,
            "content": self._bound(sanitize(value, redact=self.redact, compact=True)),
        }

    def _request(self, value, span, namespace):
        """Keep standard message objects readable; deduplicate older history only."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return sanitize(value, redact=self.redact, compact=True)
        if not isinstance(value, dict):
            value = {"input": value}
        result = {}
        omitted = 0
        for key, item in value.items():
            if key in {"instructions", "tools", "functions"}:
                reference = self._reference(item, namespace="definitions", kind=key, span=span)
                if "content" in reference:
                    result[key] = reference["content"]
                else:
                    omitted += 1
            elif key in {"messages", "input"} and isinstance(item, list):
                messages, occurrences = [], {}
                latest_user = max(
                    (
                        i
                        for i, m in enumerate(item)
                        if isinstance(m, dict) and m.get("role", m.get("message.role")) == "user"
                    ),
                    default=-1,
                )
                for i, message in enumerate(item):
                    identity = _json(sanitize(message))
                    ordinal = occurrences.get(identity, 0)
                    occurrences[identity] = ordinal + 1
                    role = (
                        message.get("role", message.get("message.role"))
                        if isinstance(message, dict)
                        else None
                    )
                    definition = role in {"system", "developer"}
                    reference = self._reference(
                        message,
                        namespace="definitions" if definition else namespace,
                        kind="prompt" if definition else "message",
                        span=span,
                        ordinal=ordinal,
                    )
                    if "content" in reference or i == latest_user:
                        messages.append(sanitize(message, redact=self.redact, compact=True))
                    else:
                        omitted += 1
                result[key] = messages
            else:
                result[key] = sanitize(item, redact=self.redact, compact=True, key=key)
        if omitted:
            result["history_note"] = (
                f"{omitted} unchanged messages or definitions omitted; see earlier calls in this session."
            )
        return result

    def _message_attributes(self, attrs, span, namespace):
        """Preserve OpenInference's native Messages rendering for each call."""
        groups = {}
        for key, value in attrs.items():
            match = re.match(r"llm.input_messages\.(\d+)\.(.*)", key)
            if match:
                groups.setdefault(int(match[1]), {})[match[2]] = value
        latest_user = max(
            (i for i, m in groups.items() if m.get("message.role") == "user"), default=-1
        )
        selected, occurrences, omitted = [], {}, 0
        for i, message in sorted(groups.items()):
            identity = _json(sanitize(message))
            ordinal = occurrences.get(identity, 0)
            occurrences[identity] = ordinal + 1
            reference = self._reference(
                message, namespace=namespace, kind="display_message", span=span, ordinal=ordinal
            )
            if "content" in reference or i == latest_user:
                selected.append(sanitize(message, redact=self.redact, compact=True))
            else:
                omitted += 1
        output = {}
        for i, message in enumerate(selected):
            for key, value in message.items():
                if isinstance(value, str) and len(value) > self.max_value_chars:
                    value = value[: self.max_value_chars] + "\n[Long message truncated]"
                output[f"llm.input_messages.{i}.{key}"] = value
        if omitted:
            output["phoenix.history.omitted_messages"] = omitted
        return output

    def transform(self, span):
        attrs = dict(span.attributes or {})
        compact = True
        namespace = attrs.get("session.id") or format(span.context.trace_id, "032x")
        output = self._message_attributes(attrs, span, namespace)
        if compact and "input.value" not in attrs:
            # Some SDK versions expose only flattened OpenInference messages.
            groups = {}
            for key, value in attrs.items():
                match = re.match(r"llm.input_messages\.(\d+)\.(.*)", key)
                if match:
                    groups.setdefault(int(match[1]), {})[match[2]] = value
            if groups:
                attrs["input.value"] = _json({"messages": [groups[k] for k in sorted(groups)]})
        definitions = {key: value for key, value in attrs.items() if key.startswith("llm.tools.")}
        if compact and definitions:
            output["phoenix.tool_definitions"] = _json(
                self._reference(
                    definitions, namespace="definitions", kind="tool_schemas", span=span
                )
            )
        for key, value in attrs.items():
            if compact and (key.startswith(("llm.input_messages.", "llm.tools."))):
                continue
            if attrs.get("openinference.span.kind") == "TOOL" and key in {
                "output.value",
                "output.mime_type",
            }:
                continue
            if key == "input.value" and attrs.get("openinference.span.kind") != "LLM":
                output[key] = sanitize(value, redact=self.redact, compact=True)
            elif compact and key == "input.value":
                if any(k.startswith("llm.input_messages.") for k in output):
                    # Native OpenInference messages carry the readable payload once.
                    user_prefixes = [
                        k.rsplit(".", 1)[0]
                        for k, v in output.items()
                        if k.endswith(".message.role") and v == "user"
                    ]
                    latest = user_prefixes[-1] if user_prefixes else ""
                    question = output.get(latest + ".content", "")
                    if not question:
                        question = next(
                            (
                                v
                                for k, v in output.items()
                                if latest
                                and k.startswith(latest + ".contents.")
                                and k.endswith(".text")
                            ),
                            "",
                        )
                    output[key] = (
                        str(question)[:2000]
                        + "\n\nCurrent messages are shown in Messages; unchanged earlier history is omitted."
                    ).strip()
                    output["input.mime_type"] = "text/plain"
                else:
                    output[key] = _json(self._request(value, span, namespace))
            elif key == "input.mime_type" and "input.mime_type" in output:
                continue
            elif key == "tool.result.preview_rows" and (compact or self.redact):
                output["tool.result.values_omitted"] = True
            elif key == "llm.invocation_parameters" and compact:
                try:
                    params = json.loads(value)
                    # Tools and messages are already captured by input.value.
                    params = {
                        k: v
                        for k, v in params.items()
                        if k not in {"tools", "functions", "messages", "input", "instructions"}
                    }
                except (ValueError, TypeError, AttributeError):
                    params = {"omitted": True}
                output[key] = _json(sanitize(params, redact=self.redact, compact=compact))
            else:
                content = key in {
                    "input.value",
                    "output.value",
                    "llm.invocation_parameters",
                    "db.statement",
                } or key.startswith(
                    (
                        "tool.args.",
                        "tool.result.",
                        "llm.input_messages.",
                        "llm.output_messages.",
                        "exception.",
                    )
                )
                clean = sanitize(value, redact=self.redact and content, compact=compact, key=key)
                if content and key.startswith("tool.args."):
                    clean = sanitize(
                        value, redact=self.redact, compact=compact, key=key.rsplit(".", 1)[-1]
                    )
                if compact and isinstance(clean, str) and len(clean) > self.max_value_chars:
                    clean = _json(self._bound(clean))
                output[key] = clean
        output["phoenix.payload.compacted"] = True
        output["phoenix.payload.redacted"] = self.redact
        clone = copy.copy(span)
        clone._attributes = output
        clone._events = tuple(
            Event(
                e.name,
                {
                    k: sanitize(v, redact=self.redact, compact=compact, key=k)
                    for k, v in (e.attributes or {}).items()
                },
                e.timestamp,
            )
            for e in span.events
        )
        if span.status.description:
            clone._status = Status(
                span.status.status_code, sanitize(span.status.description, redact=self.redact)
            )
        return clone

    def export(self, spans):
        with self.lock:
            before = self.seen.copy()
            try:
                result = self.inner.export([self.transform(span) for span in spans])
            except Exception:
                self.seen = before
                raise
            if result != SpanExportResult.SUCCESS:
                self.seen = before
            return result

    def shutdown(self):
        return self.inner.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.inner.force_flush(timeout_millis)
