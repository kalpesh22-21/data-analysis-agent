"""Layer-4 evaluation harness (Release 1, docs/decisions/release-1/07-evaluation.md).

Three suites, split by WHAT VARIES:

  A1  `test_runtime_mechanics.py`  scripted model  — runtime behaviour, per-commit CI
  A2  `test_routing_live.py`       live model      — routing decisions, release gate
  A3  answer grading                               — DEFERRED (see README.md)

Read `README.md` before adding a case. In particular: the scripted suite CANNOT
fail on a bad prompt, and green there means the runtime works — not that the
agent routes.
"""
