"""Inbox ranking — `novelty × groundedness² × session-quality` (plan §4).

With the corroboration threshold at 1 the review queue becomes the place work
accumulates, so its ORDER is the whole product: a queue nobody can prioritise is a queue
nobody reads. These tests pin the three axes and — more importantly — the ASYMMETRY
between the first two, which is the one part a plain product gets wrong.

Slugs:
  * S7-groundedness-from-parameterization — rule/known-column vs. free-floating inline.
  * S7-quality-from-session-signals       — single-shot accept vs. long struggle, with a
                                            corrected blueprint as a negative.
  * S7-novelty-gated-by-groundedness      — a hardcoded one-off nothing resembles must
                                            NOT outrank a reusable template of a
                                            familiar question.
  * S7-unmeasured-is-not-a-score          — an absent stamp is reported, never faked.
  * S7-cutoff-hides-never-drops           — the cutoff filters a listing; nothing is lost.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.signals import NoveltyStamp, SessionSignals
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.ranking import (
    groundedness,
    novelty,
    review_score,
    session_quality,
)
from data_agent.learning.promotion import PromotionPolicy

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

# A clean single-shot accepted session — the reference point the plan names as quality 1.
CLEAN = SessionSignals(
    accepted_signal="explicit_confirm",
    turn_count=1,
    failed_fixed_count=0,
    askuser_count=0,
    corrected_blueprint=False,
)


def _env(**overrides) -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    env = CandidateEnvelope.from_doc(doc["single"]["envelope"])
    return replace(env, **overrides)


def _with_params(env: CandidateEnvelope, params: list[dict]) -> CandidateEnvelope:
    payload = dict(env.payload)
    payload["parameterization"] = params
    return replace(env, payload=payload)


def _with_uses(env: CandidateEnvelope, uses: list[str]) -> CandidateEnvelope:
    payload = dict(env.payload)
    gen = dict(payload["generalization"])
    gen["uses"] = uses
    payload["generalization"] = gen
    return replace(env, payload=payload)


def _slot(binds_to: str | None) -> dict:
    return {"role": "slot", "slot": {"name": "x", "type": "string", "binds_to": binds_to}}


_INLINE = {"role": "inline", "why": "metric-defining"}
_RULE = {"role": "rule", "rule_id": "active_employee"}


# --- groundedness -------------------------------------------------------------


def test_a_fully_parameterized_blueprint_is_fully_grounded():
    env = _with_uses(_env(), ["payroll.payroll_fact.department"])
    env = _with_params(env, [_RULE, _slot("payroll.payroll_fact.department")])
    assert groundedness(env) == (1.0, True)


def test_a_hardcoded_one_off_is_ungrounded():
    """Every literal baked into the template: a transcription of one question, not a
    reusable blueprint."""
    env = _with_params(_env(), [_INLINE, _INLINE, _INLINE])
    assert groundedness(env) == (0.0, True)


def test_a_slot_pointing_outside_the_declared_uses_is_not_grounded():
    """The predicate is MEMBERSHIP IN `uses`, not "a `binds_to` string is present". A
    slot naming a column the blueprint does not declare it reads is not evidence of
    catalog grounding — it is evidence the plan disagrees with its own footprint, and
    `_assert_binds_to_subset_uses` will reject it at landing anyway."""
    env = _with_uses(_env(), ["payroll.payroll_fact.department"])
    env = _with_params(env, [_slot("hr.employee.region")])
    assert groundedness(env) == (0.0, True)


def test_a_windowed_slot_with_no_binds_to_is_grounded():
    """`relative_window` consumes no column domain, and `SlotSpec.parse` REFUSES a
    `binds_to` for it — absent is the correct value there. Counting it as ungrounded
    would penalise the one slot type that is right by construction."""
    env = _with_params(_env(), [_slot(None)])
    assert groundedness(env) == (1.0, True)


def test_an_empty_binds_to_is_ungrounded_not_a_windowed_slot():
    """`""` is a value `SlotSpec.parse` REFUSES (it tests `is not None`), so the
    ranking's predicate must be `is None` too. The two agree on every input except this
    one — which is exactly what an "emit null" instruction produces from a model."""
    env = _with_params(_env(), [_slot("")])
    assert groundedness(env) == (0.0, True)


def test_a_blueprint_with_no_literal_predicates_is_fully_grounded():
    """`SELECT count(*) FROM employees` has no literals to hardcode. The measure is
    "what share of this template's literals are still baked in", and the answer for an
    empty list is none of them — scoring it low would sink the most general blueprints."""
    assert groundedness(_with_params(_env(), [])) == (1.0, True)


@pytest.mark.parametrize("junk", ["not-a-list", 5, {"role": "slot"}, None])
def test_a_malformed_parameterization_field_is_unmeasured_not_zero(junk):
    """Untrusted rehydrated JSON. An absent/malformed FIELD means "no opinion" (the
    neutral), which must stay distinct from a measured 0.0 — the latter is a real claim
    that everything is hardcoded."""
    env = _with_params(_env(), junk)
    score, measured = groundedness(env)
    assert measured is False and score == 1.0


def test_a_non_mapping_entry_counts_against_the_score_never_skipped():
    """Derived from the OPERATION: the score is grounded/total. Skipping a corrupted
    entry would shrink the DENOMINATOR and therefore RAISE the score — a malformed
    candidate would rank above a well-formed one."""
    env = _with_uses(_env(), ["payroll.payroll_fact.department"])
    env = _with_params(env, [_slot("payroll.payroll_fact.department"), "garbage"])
    assert groundedness(env) == (0.5, True)


# --- session quality ----------------------------------------------------------


def test_a_clean_single_shot_acceptance_scores_one():
    assert session_quality(_env(session_signals=CLEAN)) == (1.0, True)


def test_a_long_struggle_scores_lower_than_a_single_shot():
    struggle = replace(CLEAN, turn_count=6, failed_fixed_count=3, askuser_count=2)
    fought, _ = session_quality(_env(session_signals=struggle))
    clean, _ = session_quality(_env(session_signals=CLEAN))
    assert 0.0 < fought < clean


def test_a_corrected_blueprint_is_a_negative():
    """The plan names this one specifically. The agent ran an EXISTING blueprint and a
    human corrected it — whatever the analyst finally accepted, the session's own
    evidence is that the blueprint layer already got something wrong."""
    corrected = replace(CLEAN, corrected_blueprint=True)
    assert session_quality(_env(session_signals=corrected))[0] == pytest.approx(0.5)


def test_no_detected_acceptance_scores_below_a_confirmed_one():
    unaccepted, _ = session_quality(_env(session_signals=replace(CLEAN, accepted_signal=None)))
    confirmed, _ = session_quality(_env(session_signals=CLEAN))
    assert unaccepted < confirmed


def test_an_unknown_acceptance_spelling_falls_to_the_weakest_bucket():
    """The value comes off a rehydrated doc. A foreign writer inventing a signal name
    must not be able to promote its candidate up the queue, so an unknown spelling
    resolves to the weakest bucket rather than to the middle."""
    unknown, _ = session_quality(_env(session_signals=replace(CLEAN, accepted_signal="vibes")))
    none, _ = session_quality(_env(session_signals=replace(CLEAN, accepted_signal=None)))
    assert unknown == none


def test_an_envelope_with_no_session_signals_is_unmeasured():
    """A candidate extracted before the stamp existed: the summary it came from is gone
    and cannot be re-read, so the honest answer is "nobody looked"."""
    assert session_quality(_env(session_signals=None)) == (1.0, False)


# --- novelty ------------------------------------------------------------------


def test_novelty_comes_from_the_dedup_stamp():
    env = _env(novelty=NoveltyStamp(novelty=0.8, measured=True, compared_against=5))
    assert novelty(env) == (0.8, True)


def test_an_unmeasured_novelty_stamp_is_not_maximal_novelty():
    """`measured=False` means the graph could not be consulted. Reporting it as "very
    novel" would rank a candidate nobody could compare above one we actually know is
    new — the same failure `PriorArtUnavailableError` exists to prevent one layer down."""
    env = _env(novelty=NoveltyStamp(novelty=0.0, measured=False))
    score, measured = novelty(env)
    assert measured is False and score == 1.0


# --- THE asymmetry ------------------------------------------------------------


def test_a_novel_ungrounded_one_off_ranks_below_a_familiar_grounded_template():
    """THE property the plan is explicit about, and the one a plain three-way product
    gets wrong.

    A plain `novelty × groundedness × quality` is symmetric in its first two terms, so
    (novelty 1.0, groundedness 0.1) and (0.1, 1.0) would tie. They are not equally worth
    a reviewer's time: the most novel candidate is usually the most IDIOSYNCRATIC, and
    high novelty with low groundedness is a hardcoded one-off, not a discovery. Novelty
    is therefore GATED — scaled by groundedness before it enters the product — which
    breaks the tie by an order of magnitude in the right direction."""
    ungrounded_but_novel = _with_params(
        _env(
            session_signals=CLEAN,
            novelty=NoveltyStamp(novelty=1.0, measured=True, compared_against=9),
        ),
        [_INLINE] * 9 + [_RULE],  # 0.1 grounded
    )
    grounded_but_familiar = _with_uses(
        _env(
            session_signals=CLEAN,
            novelty=NoveltyStamp(novelty=0.1, measured=True, compared_against=9),
        ),
        ["payroll.payroll_fact.department"],
    )
    grounded_but_familiar = _with_params(
        grounded_but_familiar, [_slot("payroll.payroll_fact.department")]
    )

    assert (
        review_score(grounded_but_familiar).score
        > review_score(ungrounded_but_novel).score
    )


def test_the_score_can_never_exceed_the_candidates_groundedness():
    """The invariant the gate buys, stated directly: however novel and however clean the
    session, a candidate whose filters are mostly hardcoded cannot climb the queue."""
    env = _with_params(
        _env(
            session_signals=CLEAN,
            novelty=NoveltyStamp(novelty=1.0, measured=True, compared_against=3),
        ),
        [_INLINE, _INLINE, _INLINE, _RULE],  # 0.25
    )
    scored = review_score(env)
    assert scored.score <= scored.groundedness


def test_the_score_does_not_use_the_top_of_its_range_is_a_known_limitation():
    """KNOWN LIMITATION, measured LIVE and pinned here because it is a footgun with a
    knob attached.

    The unit suite cannot see it: `FakeEmbeddingClient` and the in-memory index's token
    overlap both span the full 0-1, while a real sentence embedder does not. Measured
    against the live dev corpus (10 canon blueprints, `all-mpnet-base-v2`, the real
    `Neo4jPriorArtIndex`):

        an exact re-derivation of a landed intent  cosine 0.9998  novelty 0.0002
        a plausible NEW HR question                       0.7411         0.2589
        a genuinely unrelated question                    0.5869         0.4131
        total nonsense                                    0.5342         0.4658

    Two texts about nothing in common still score ~0.53, so novelty occupies roughly
    [0.0, 0.47]. A PERFECT candidate — fully grounded, cleanly accepted, genuinely new —
    therefore scores about 0.26, and a `review_score_cutoff` of 0.5 would hide the entire
    queue while looking like a moderate setting.

    Asserted here on the arithmetic rather than on the embedder: given a REALISTIC best
    novelty, even a flawless candidate lands nowhere near 1.0. It is not a bug (the score
    is an ordering, not a percentage) and it is not fixed by rescaling, which would only
    move the arbitrary constant around — it is documented, in the knob's own description."""
    flawless = _with_uses(
        _env(
            session_signals=CLEAN,
            novelty=NoveltyStamp(novelty=0.26, measured=True, compared_against=10),
        ),
        ["payroll.payroll_fact.department"],
    )
    flawless = _with_params(flawless, [_slot("payroll.payroll_fact.department"), _RULE])

    scored = review_score(flawless)
    assert scored.groundedness == 1.0
    assert scored.session_quality == 1.0
    assert scored.score == pytest.approx(0.26)  # the ceiling in practice, not 1.0


def test_every_axis_stays_inside_zero_to_one():
    worst = _with_params(
        _env(
            session_signals=SessionSignals(
                accepted_signal=None, turn_count=99, failed_fixed_count=99,
                askuser_count=99, corrected_blueprint=True,
            ),
            novelty=NoveltyStamp(novelty=0.0, measured=True),
        ),
        [_INLINE],
    )
    scored = review_score(worst)
    assert 0.0 <= scored.score <= 1.0


# --- the listing --------------------------------------------------------------


async def _seeded_store(*envs: CandidateEnvelope) -> InMemoryCandidateStore:
    store = InMemoryCandidateStore()
    for env in envs:
        await store.put(env)
    return store


def _queued(candidate_id: str, *, params: list, novel: float, created: str):
    env = _env(
        session_signals=CLEAN,
        novelty=NoveltyStamp(novelty=novel, measured=True, compared_against=3),
        status=CandidateStatus.IN_REVIEW,
        created_at=created,
    )
    env = replace(env, candidate_id=candidate_id, content_hash=candidate_id)
    env = _with_uses(env, ["payroll.payroll_fact.department"])
    return _with_params(env, params)


async def test_the_review_queue_is_ordered_best_first():
    good = _queued(
        "c::good", params=[_slot("payroll.payroll_fact.department")], novel=0.9,
        created="2026-01-01T00:00:00+00:00",
    )
    poor = _queued(
        "c::poor", params=[_INLINE, _INLINE, _INLINE], novel=0.9,
        created="2020-01-01T00:00:00+00:00",  # OLDER — FIFO alone would put it first
    )
    inbox = ReviewInbox(await _seeded_store(good, poor))

    items = await inbox.list()

    assert [i.candidate_id for i in items] == ["c::good", "c::poor"]


async def test_ties_fall_back_to_arrival_order():
    """A great many candidates score identically at the shipped weights, so the tiebreak
    is what the queue's order actually IS most of the time. It must be FIFO — the
    behaviour the inbox had before ranking existed — not the store's incidental order and
    not the candidate id."""
    params = [_slot("payroll.payroll_fact.department")]
    later = _queued("c::aaa-later", params=params, novel=0.5, created="2026-06-01T00:00:00+00:00")
    earlier = _queued("c::zzz-earlier", params=params, novel=0.5, created="2026-01-01T00:00:00+00:00")
    inbox = ReviewInbox(await _seeded_store(later, earlier))

    items = await inbox.list()

    assert [i.candidate_id for i in items] == ["c::zzz-earlier", "c::aaa-later"]


async def test_the_archive_listing_is_not_re_ranked():
    """`?status=rejected` is HISTORY. Ranking it by how interesting it would have been is
    meaningless, and reordering it would break the newest-first contract the LIMIT relies
    on to trim old rows rather than present ones."""
    old = replace(
        _queued("c::old", params=[_INLINE], novel=0.1, created="2020-01-01T00:00:00+00:00"),
        status=CandidateStatus.REJECTED,
    )
    new = replace(
        _queued("c::new", params=[_slot("payroll.payroll_fact.department")], novel=0.99,
                created="2026-01-01T00:00:00+00:00"),
        status=CandidateStatus.REJECTED,
    )
    inbox = ReviewInbox(await _seeded_store(old, new))

    items = await inbox.list(status=CandidateStatus.REJECTED, order="desc")

    assert [i.candidate_id for i in items] == ["c::new", "c::old"]  # newest first, unranked


def _unmeasurable(candidate_id: str, *, created: str) -> CandidateEnvelope:
    """A writer-routed `global_knowledge` item — the realistic unmeasured row.

    No dedup verdict (S6 only adjudicates blueprints) ⇒ novelty unmeasured. No
    `parameterization` ⇒ groundedness unmeasured. Two of three axes are neutral BY
    CONSTRUCTION, not by accident, which is what makes this the default case rather than
    an edge case."""
    env = _env(
        session_signals=CLEAN,
        status=CandidateStatus.IN_REVIEW,
        created_at=created,
        type="global_knowledge",
        novelty=None,
    )
    payload = {"statement": "the fiscal year starts in April", "scope": "fiscal calendar"}
    return replace(
        env, candidate_id=candidate_id, content_hash=candidate_id, payload=payload
    )


async def test_an_unmeasured_row_never_outranks_a_measured_one():
    """THE inversion a plain score-sort has, and the reason the sort partitions first.

    Novelty's measured ceiling against a real corpus is ~0.47, so a perfect MEASURED
    candidate scores ~0.26 — while an unmeasured one scores up to `1.0 × 1.0² × quality`.
    Sorting both on that one number pins every knowledge item permanently above every
    blueprint the machine actually knows something about. Comparing a measurement against
    a placeholder is the error; putting the placeholders in a second block is the fix."""
    measured = _queued(
        "c::measured", params=[_slot("payroll.payroll_fact.department")], novel=0.26,
        created="2026-06-01T00:00:00+00:00",  # NEWER — FIFO alone would put it second
    )
    blind = _unmeasurable("c::blind", created="2020-01-01T00:00:00+00:00")
    inbox = ReviewInbox(await _seeded_store(measured, blind))

    items = await inbox.list()

    assert [i.candidate_id for i in items] == ["c::measured", "c::blind"]
    # ...and the raw score genuinely inverts, which is why the partition is needed at all.
    by_id = {i.candidate_id: i.score for i in items}
    assert by_id["c::blind"].score > by_id["c::measured"].score
    assert by_id["c::blind"].measured is False
    assert by_id["c::measured"].measured is True


async def test_unmeasured_rows_are_ordered_among_themselves_by_arrival():
    """Within the unmeasured block the neutrals are shared, so the score says nothing and
    the tiebreak does all the work — which must be FIFO, the behaviour the inbox had
    before ranking existed."""
    later = _unmeasurable("c::aaa-later", created="2026-06-01T00:00:00+00:00")
    earlier = _unmeasurable("c::zzz-earlier", created="2026-01-01T00:00:00+00:00")
    inbox = ReviewInbox(await _seeded_store(later, earlier))

    assert [i.candidate_id for i in await inbox.list()] == [
        "c::zzz-earlier",
        "c::aaa-later",
    ]


async def test_a_cutoff_never_hides_a_measured_row_while_keeping_an_unmeasured_one():
    """The second half of the inversion, and the nastier half: with one filter over both
    groups, a non-zero cutoff preferentially removes the candidates we know MOST about and
    keeps the ones we know NOTHING about — the knob doing the exact opposite of its name.

    A cutoff is a judgement about a score; an unmeasured row has no score to judge, so it
    is exempt. It is also never in the way, because the sort has already put it last."""
    measured = _queued(
        "c::measured", params=[_slot("payroll.payroll_fact.department")], novel=0.26,
        created="2026-01-01T00:00:00+00:00",
    )
    blind = _unmeasurable("c::blind", created="2026-01-01T00:00:00+00:00")
    inbox = ReviewInbox(
        await _seeded_store(measured, blind),
        policy=PromotionPolicy(review_score_cutoff=0.5),
    )

    items = await inbox.list()

    # The measured row is below 0.5 and is hidden; the blind one is exempt, not "above".
    assert [i.candidate_id for i in items] == ["c::blind"]
    assert items[0].score.measured is False


async def test_a_cutoff_that_empties_a_non_empty_queue_warns(caplog):
    """An operator who empties their own inbox is TOLD. Silently returning zero rows to a
    reviewer who misjudged a knob — and the knob IS easy to misjudge, since a perfect
    candidate scores ~0.26 — is how a queue stops being read."""
    good = _queued(
        "c::good", params=[_slot("payroll.payroll_fact.department")], novel=0.26,
        created="2026-01-01T00:00:00+00:00",
    )
    inbox = ReviewInbox(
        await _seeded_store(good), policy=PromotionPolicy(review_score_cutoff=0.9)
    )

    with caplog.at_level("WARNING"):
        items = await inbox.list()

    assert items == []
    assert "hid ALL 1 row(s)" in caplog.text


async def test_an_empty_queue_does_not_warn(caplog):
    """Nothing was hidden, so there is nothing to tell anyone. A warning on every poll of
    an empty inbox is how a real warning gets filtered out."""
    inbox = ReviewInbox(
        await _seeded_store(), policy=PromotionPolicy(review_score_cutoff=0.9)
    )

    with caplog.at_level("WARNING"):
        assert await inbox.list() == []

    assert "hid ALL" not in caplog.text


async def test_the_cutoff_hides_a_row_and_never_deletes_it():
    """`review_score_cutoff` is applied at LIST time. A routing-time cutoff would be a
    silent terminal state — a candidate discarded for a score nobody recorded a decision
    about. A list filter leaves the row stored, still `in_review`, and it reappears the
    moment the knob moves back."""
    poor = _queued("c::poor", params=[_INLINE, _INLINE], novel=0.9, created="2026-01-01T00:00:00+00:00")
    good = _queued(
        "c::good", params=[_slot("payroll.payroll_fact.department")], novel=0.9,
        created="2026-01-01T00:00:00+00:00",
    )
    store = await _seeded_store(poor, good)

    strict = ReviewInbox(store, policy=PromotionPolicy(review_score_cutoff=0.5))
    assert [i.candidate_id for i in await strict.list()] == ["c::good"]

    # The row was never touched: the default (cutoff 0.0) shows it again.
    assert (await store.get("c::poor")).status == CandidateStatus.IN_REVIEW
    assert len(await ReviewInbox(store).list()) == 2


async def test_a_non_string_created_at_never_crashes_the_listing():
    """`created_at` is read off the doc with no type check, and it is the SECOND element
    of the sort key — so it is compared only when the scores TIE. A non-str would
    therefore be a tie-dependent `TypeError`: a listing that works for months and then
    fails the first time two candidates score the same. Same latent load-dependent shape
    a previous slice found in `_best_card`."""
    params = [_slot("payroll.payroll_fact.department")]
    a = replace(_queued("c::a", params=params, novel=0.5, created="x"), created_at=42)
    b = _queued("c::b", params=params, novel=0.5, created="2026-01-01T00:00:00+00:00")
    inbox = ReviewInbox(await _seeded_store(a, b))

    items = await inbox.list()

    assert {i.candidate_id for i in items} == {"c::a", "c::b"}


async def test_the_projected_score_matches_the_order_it_was_sorted_by():
    """The score a reviewer SEES and the key the queue was SORTED by must come from one
    computation, or the UI renders an order it cannot explain."""
    envs = [
        _queued("c::a", params=[_INLINE], novel=0.9, created="2026-01-01T00:00:00+00:00"),
        _queued(
            "c::b", params=[_slot("payroll.payroll_fact.department")], novel=0.2,
            created="2026-01-02T00:00:00+00:00",
        ),
    ]
    items = await ReviewInbox(await _seeded_store(*envs)).list()

    scores = [i.score.score for i in items]
    assert scores == sorted(scores, reverse=True)
