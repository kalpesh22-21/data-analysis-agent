import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data_agent.runtime.loop.delivery import CURRENT_DELIVERY, DeliveryContext
from data_agent.runtime.provenance.answer_formats import AnswerFormatter, cached_formats
from data_agent.runtime.session.models import ResultPreview, TrailEntry


def entry(**changes):
    return replace(
        TrailEntry(
            0,
            "q",
            "runQuery",
            {"sql": "SELECT part / total AS share FROM hr.employee"},
            "ok",
            None,
            frozenset(),
            ResultPreview(["share"], 1, False, [[0.25]], {"share": {"kind": "number"}}),
            None,
            "now",
        ),
        **changes,
    )


def response(column="share", **fmt):
    return {
        "formats": [
            {
                "result_id": "q",
                "column": column,
                "kind": "percent",
                "scale": "ratio",
                "currency": None,
                "unit": None,
                "precision": 2,
                **fmt,
            }
        ]
    }


@pytest.fixture
def context():
    value = DeliveryContext(None)
    token = CURRENT_DELIVERY.set(value)
    yield value
    CURRENT_DELIVERY.reset(token)


async def test_formats_cached_without_mutating_evidence(context):
    client = SimpleNamespace(format_columns=AsyncMock(return_value=response()))
    formatter = AnswerFormatter(client)
    receipt = entry()
    before = receipt.to_doc()
    await formatter.resolve("What share?", [receipt], 0)
    await formatter.resolve("What share?", [receipt], 0)
    assert client.format_columns.await_count == 1
    assert cached_formats([receipt], 0)["q"]["share"] == {
        "kind": "percent",
        "scale": "ratio",
        "precision": 2,
    }
    assert receipt.to_doc() == before
    payload = json.loads(client.format_columns.call_args.args[0][1]["content"])
    assert payload["results"][0]["sample_rows"] == [["0.25"]]
    # A changed receipt must get a fresh classification, never reuse stale metadata.
    changed = replace(receipt, args={"sql": "SELECT 25 AS share"})
    assert not cached_formats([changed], 0)
    await formatter.resolve("What share?", [changed], 0)
    assert client.format_columns.await_count == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"tool_name": "runBlueprint", "authoritative": True},
        {"provenance": None},
        {"status": "denied"},
        {"turn_index": 1},
        {
            "result_preview": ResultPreview(
                ["share"], 1, False, [[1]], {"share": {"kind": "integer", "precision": 0}}
            )
        },
        {
            "result_preview": ResultPreview(
                ["share"], 1, False, [[1]], {"share": {"kind": "currency", "currency": "USD"}}
            )
        },
    ],
)
async def test_skips_blueprints_ineligible_and_authoritative_formats(context, changes):
    client = SimpleNamespace(format_columns=AsyncMock())
    await AnswerFormatter(client).resolve("question", [entry(**changes)], 0)
    client.format_columns.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        {},
        response(column="unknown"),
        response(kind="currency", scale=None),
        response(precision=10),
        {"formats": response()["formats"] * 2},
    ],
)
async def test_invalid_output_falls_back_once(context, bad):
    client = SimpleNamespace(format_columns=AsyncMock(return_value=bad))
    formatter = AnswerFormatter(client)
    for _ in range(2):
        await formatter.resolve("question", [entry()], 0)
    assert not cached_formats([entry()], 0)
    assert client.format_columns.await_count == 1


async def test_timeout_is_bounded_cached_and_nonblocking(context):
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    client = SimpleNamespace(format_columns=AsyncMock(side_effect=slow))
    formatter = AnswerFormatter(client, timeout=0.001)
    await formatter.resolve("question", [entry()], 0)
    await formatter.resolve("question", [entry()], 0)
    assert not cached_formats([entry()], 0)
    assert client.format_columns.await_count == 1


async def test_normal_delivery_judge_history_and_export_share_formats():
    from tests.runtime.test_answer_document import final, section
    from tests.runtime.test_harness_improvements import CREDS, Judge, build, discovery, query, run

    client = SimpleNamespace(
        format_columns=AsyncMock(
            return_value=response(column="n", kind="integer", scale=None, precision=0)
        )
    )
    judge = Judge()
    loop, store, _, _, _ = build([discovery(), query(), final(section())], judge)
    loop._answer_formatter = AnswerFormatter(client)
    out = await run(loop)
    expected = {"kind": "integer", "precision": 0}
    assert out.answer_document["sources"]["q"]["column_formats"]["n"] == expected
    for key in ("sources", "sections", "takeaway"):
        assert judge.briefs[0].answer_document[key] == out.answer_document[key]
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document
    source = doc.messages[-1].answer_document["sources"]["q"]
    assert source["sql"] and source["column_formats"]["n"] == expected



async def test_provider_request_disables_reasoning_and_retries():
    from data_agent.runtime.model.openai_client import OpenAIModelClient

    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"formats":[]}'))]
        )
    )
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    options = []
    sdk.with_options = lambda **kwargs: options.append(kwargs) or sdk
    client = OpenAIModelClient(sdk, model="deepseek")
    assert await client.format_columns([], {}) == {"formats": []}
    assert options == [{"max_retries": 0}]
    assert create.call_args.kwargs["reasoning_effort"] == "none"
    assert create.call_args.kwargs["response_format"]["json_schema"]["strict"] is True


async def test_presentation_repair_reuses_formatting():
    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from tests.runtime.test_answer_document import final, section
    from tests.runtime.test_harness_improvements import Judge, build, discovery, query, run

    judge = Judge(
        [
            JudgeVerdict(
                False, "unsupported_by_evidence", "Use a table.", True, repair_type="presentation"
            ),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    client = SimpleNamespace(
        format_columns=AsyncMock(return_value=response(column="n", kind="integer", scale=None, precision=0))
    )
    loop, _, _, _, _ = build(
        [
            discovery(),
            query(),
            final(section("figure", column="n", label="Employees")),
            final(section(), ident="fixed"),
        ],
        judge,
    )
    loop._answer_formatter = AnswerFormatter(client)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert client.format_columns.await_count == 1
    assert judge.briefs[0].answer_document["sections"][0]["presentation"]["column"] == "n"


async def test_salvage_formats_are_reviewed_and_persisted():
    from data_agent.runtime.loop.answer_judge import JudgeVerdict
    from tests.runtime.final_answer import recovery_sections
    from tests.runtime.loop.test_partial_answer import PART, build, final, reject
    from tests.runtime.test_harness_improvements import CREDS, Judge, discovery, query, run

    part = {**PART, "sections": recovery_sections(["q"])}
    judge = Judge([reject(), reject(), reject(part), JudgeVerdict(True, reviewed=True)])
    client = SimpleNamespace(
        format_columns=AsyncMock(return_value=response(column="n", kind="integer", scale=None, precision=0))
    )
    loop, store, _, _, _ = build([discovery(), query(), final(), final()], judge)
    loop._answer_formatter = AnswerFormatter(client)
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert out.review["completion"] == "partial"
    assert client.format_columns.await_count == 1
    assert out.answer_document["sources"]["q"]["column_formats"]["n"]["kind"] == "integer"
    assert judge.briefs[-1].answer_document["sources"] == out.answer_document["sources"]
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].answer_document == out.answer_document


async def test_sample_limits_and_selection(context):
    client = SimpleNamespace(format_columns=AsyncMock(return_value={"formats": []}))
    receipt = entry(result_preview=ResultPreview(["share"], 20, True, [["x" * 1000]] * 20))
    await AnswerFormatter(client).resolve("q", [receipt], 0, selected=set())
    client.format_columns.assert_not_called()
    await AnswerFormatter(client).resolve("q", [receipt], 0, selected={"q"})
    rows = json.loads(client.format_columns.call_args.args[0][1]["content"])["results"][0][
        "sample_rows"
    ]
    assert len(rows) == 3 and len(rows[0][0]) == 200


async def test_bare_integer_postal_code_can_be_classified_without_changing_counts(context):
    client = SimpleNamespace(format_columns=AsyncMock(return_value=response(column="Zip", kind="identifier", scale=None, precision=None)))
    receipt = entry(args={"sql":"SELECT postal_code AS Zip, count(*) AS n FROM employee GROUP BY postal_code"},
                    result_preview=ResultPreview(["Zip","n"],1,False,[[75039,2]],{"Zip":{"kind":"integer"},"n":{"kind":"integer"}}))
    await AnswerFormatter(client).resolve("Show ZIP codes and counts", [receipt], 0)
    payload=json.loads(client.format_columns.call_args.args[0][1]["content"])
    assert payload["results"][0]["eligible_columns"] == ["Zip"]
    assert cached_formats([receipt],0)["q"]["Zip"] == {"kind":"identifier"}


@pytest.mark.parametrize('tool_name', ['runQuery', 'runBlueprint'])
async def test_protected_currency_context_reaches_derived_result_without_reformatting_source(context, tool_name):
    payroll = entry(
        tool_call_id='payroll', tool_name=tool_name, authoritative=True,
        args={'sql': 'SELECT sum(amount) AS gross FROM hr.payroll'} if tool_name == 'runQuery' else {'id': 'payroll-total'},
        provenance=frozenset({('hr.payroll', 'amount')}),
        result_preview=ResultPreview(['gross'], 1, False, [['8100.00']], {'gross': {'kind': 'currency', 'currency': 'USD'}}),
    )
    derived = entry(
        args={'sql': 'SELECT sum(amount) / 7 AS cost FROM hr.payroll'},
        provenance=frozenset({('hr.payroll', 'amount')}),
        result_preview=ResultPreview(['cost'], 1, False, [[1157.14]], {'cost': {'kind': 'number'}}),
    )
    client = SimpleNamespace(format_columns=AsyncMock(return_value=response(column='cost', kind='currency', currency='USD', scale=None)))
    formatter = AnswerFormatter(client)
    for _ in range(2):
        await formatter.resolve('Cost per employee?', [payroll, derived], 0, selected={'payroll', 'q'})
    assert client.format_columns.await_count == 1
    payload = json.loads(client.format_columns.call_args.args[0][1]['content'])
    assert [r['result_id'] for r in payload['results']] == ['q']
    assert payload['currency_context'] == [{
        'result_id': 'payroll', 'sql': payroll.args.get('sql', ''),
        'source_columns': [['hr.payroll', 'amount']],
        'column_formats': {'gross': {'kind': 'currency', 'currency': 'USD'}},
    }]
    assert cached_formats([derived], 0)['q']['cost'] == {'kind': 'currency', 'currency': 'USD', 'precision': 2}
    assert 'payroll' not in cached_formats([payroll], 0)


async def test_currency_context_respects_scope_turn_selection_and_keeps_currencies_separate(context):
    def money(rid, currency, **changes):
        return entry(tool_call_id=rid, result_preview=ResultPreview(['amount'], 1, False, [[10]], {
            'amount': {'kind': 'currency', 'currency': currency}}), **changes)
    client = SimpleNamespace(format_columns=AsyncMock(return_value={'formats': []}))
    trail = [entry(), money('usd', 'USD'), money('eur', 'EUR'),
             money('denied', 'GBP', status='denied'), money('unknown', 'JPY', provenance=None),
             money('old', 'AUD', turn_index=1), money('unselected', 'CAD')]
    await AnswerFormatter(client).resolve('Question', trail, 0, selected={e.tool_call_id for e in trail if e.tool_call_id != 'unselected'})
    payload = json.loads(client.format_columns.call_args.args[0][1]['content'])
    assert [(v['result_id'], v['column_formats']['amount']['currency']) for v in payload['currency_context']] == [('usd', 'USD'), ('eur', 'EUR')]
    assert len(payload['results']) == 1


async def test_extra_formats_are_dropped_before_validating_eligible_columns(context):
    receipt = entry(result_preview=ResultPreview(
        ["EmployeeCode", "Contact ZIP", "Contact Phone"], 1, False,
        [["EMP001", "02108", "+12025550100"]],
        {"EmployeeCode": {"kind": "identifier"}, "Contact ZIP": {"kind": "identifier"},
         "Contact Phone": {"kind": "text"}},
    ))
    before = receipt.to_doc()
    valid = response(column="Contact Phone", kind="identifier", scale=None, precision=None)["formats"][0]
    client = SimpleNamespace(format_columns=AsyncMock(return_value={"formats": [
        {"result_id": "q", "column": "EmployeeCode", "kind": "invalid"},
        {"result_id": "q", "column": "Contact ZIP", "kind": "currency"},
        {"result_id": "other", "column": "Contact Phone"}, valid,
    ]}))
    await AnswerFormatter(client).resolve("Show contacts", [receipt], 0)
    assert cached_formats([receipt], 0) == {"q": {"Contact Phone": {"kind": "identifier"}}}
    assert receipt.to_doc() == before
    assert client.format_columns.await_count == 1
