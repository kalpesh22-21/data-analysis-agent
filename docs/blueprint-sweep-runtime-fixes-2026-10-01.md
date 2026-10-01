# Runtime follow-up to the September 30 blueprint sweep

The remote corpus is unchanged. These changes address runtime behavior and diagnostics:

- **B1:** closed enums resolve against `enum_values` without a warehouse domain query. Invalid enum values still pause; they never run a template.
- **C1:** `positive_integer` has independent authored bounds, with an implementation ceiling of 2,147,483,647. New `nonnegative_integer` accepts zero and otherwise follows the same validation. `relative_window` retains its existing 1–120 limit. The remote author must change numeric parameters currently mislabeled as relative windows; existing YAML is not silently reinterpreted.
- **C2:** `count(*)` is permitted. Column-expanding stars, including nested and qualified stars, remain rejected. Source tables must still belong to the declared footprint.
- **C3:** explicitly quoted display aliases in GROUP BY/ORDER BY can resolve to their SELECT expressions, and identifier normalization is applied before qualification to support derived outputs across subqueries. WHERE references still undergo strict source qualification. Ambiguous simple identifier renames remain conservative; this does not turn on unrestricted alias expansion. Undeclared source columns, including ones inside alias expressions, still fail.
- **C5 diagnostics:** verification failures include the reason, declared grain and actual result column names. The runtime does not guess that `Employee` means `Employee Code`; that remains a corpus correction.
- **D1:** a NULL scalar intermediate returns `RUN_BLUEPRINT_INTERMEDIATE_UNAVAILABLE`, names the producer and value, and is nonretryable. Dependent SQL is not dispatched. Malformed/fanned-out scalar outputs still fail closed.

The diagnostic probe exercises these changes through the shared runtime code. No MCP API or deployed physical schema changes are included. The `zip_code` schema mismatch and the empty data visible to the sweep token require investigation on the remote side.

Regression coverage includes runtime slot binding, compile-time alias/footprint checks, numeric boundary validation, NULL intermediate recovery metadata, and explicit grain diagnostics. A subsequent local MCP probe used 12 unchanged definitions copied from the report into temporary files: eight completed, two retained their authored grain failures, one returned the expected nonretryable NULL-intermediate error, and one retained its invalid relative-window bounds. All five reported C3 alias patterns and the C2 count(*) blueprint completed. Most results were empty, so populated-data correctness and the remote deployment remain unverified. The remote ZIP-column denial did not reproduce locally.

Validation: the final full run produced **8,617 passed, 229 skipped, 22 failed**. All 22 failures are in `tests/eval/test_metrics.py` and `tests/eval/test_runtime_mechanics.py`; the same 22 failures reproduce in a clean worktree at the unchanged `13d2aa4` baseline. The focused runtime/retrieval/learning run passed 5,290 tests (3 skipped) before the final two diagnostic regressions were added. Ruff and whitespace checks pass.
