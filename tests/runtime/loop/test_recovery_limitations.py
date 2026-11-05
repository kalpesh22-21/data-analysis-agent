from data_agent.runtime.loop.partial_answer import _with_unfinished


def test_repeated_limitation_is_not_appended_again():
    answer = "Sales has 3 employees. Help Center was unavailable."
    assert _with_unfinished(answer, ["help center was unavailable."]) == answer


def test_distinct_periods_and_reasons_are_preserved():
    answer = "January expenses could not be verified."
    assert _with_unfinished(answer, ["February expenses could not be verified.",
                                  "February expenses could not be verified."]) == (
        answer + "\n\nUnfinished: February expenses could not be verified."
    )
