"""LearningExtractor — forced structured output + retry-on-mismatch (D31).
Matrix rows 1/8/9; task items 1, 7 (extractor level).
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor import ExtractedCandidate
from data_agent.learning.extractor.schema import EXTRACTOR_TOOL_NAME, SchemaMismatchError

from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    emit_extractor,
    make_extractor,
    make_summary,
    malformed_turn,
    scripted_turn,
)


async def test_valid_scripted_emit_yields_one_candidate():
    extractor = emit_extractor([blueprint_raw()])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1
    assert result.declines == ()
    assert isinstance(result.candidates[0], ExtractedCandidate)
    assert result.candidates[0].header.type == "blueprint"


async def test_declined_candidate_surfaces_in_declines_not_candidates():
    # A no-evidence candidate is emitted by the model but rejected at validation.
    extractor = emit_extractor([blueprint_raw(evidence=[])])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert result.candidates == ()
    assert len(result.declines) == 1
    assert result.declines[0].reason == "no_evidence"


async def test_empty_candidates_array_is_valid_zero_output():
    extractor = emit_extractor([])
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert result.candidates == ()
    assert result.declines == ()


async def test_forced_tool_is_offered_to_the_model():
    extractor = emit_extractor([blueprint_raw()])
    await extractor.extract(make_summary(), KEEP_VERDICT)
    # The extractor offers exactly the emit_candidates tool.
    client = extractor._model_client
    tool_names = {t.get("name") for t in client.calls[0].tools}
    assert tool_names == {EXTRACTOR_TOOL_NAME}


# --- item 7: retry-on-mismatch → raises (drives dead-letter in the consumer) -


async def test_persistent_malformed_response_raises_after_retries():
    # max_retries=2 ⇒ 1 initial + 2 retries = 3 malformed attempts, then raise.
    extractor = make_extractor(
        [malformed_turn(), malformed_turn(), malformed_turn()], max_retries=2
    )
    with pytest.raises(SchemaMismatchError):
        await extractor.extract(make_summary(), KEEP_VERDICT)


async def test_retry_recovers_when_a_later_attempt_is_well_formed():
    # malformed first, then a valid emit within the retry budget → succeeds.
    extractor = make_extractor(
        [malformed_turn(), scripted_turn([blueprint_raw()])], max_retries=2
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1


async def test_exactly_max_retries_plus_one_attempts_are_made():
    client_turns = [malformed_turn(), malformed_turn()]  # 1 initial + 1 retry
    extractor = make_extractor(client_turns, max_retries=1)
    with pytest.raises(SchemaMismatchError):
        await extractor.extract(make_summary(), KEEP_VERDICT)
    assert extractor._model_client.calls_made == 2
