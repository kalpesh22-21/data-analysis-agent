# DAG scalar binding evidence — 2026-11-06

Historical engineering record. Localhost trace URLs and `/tmp` or gitignored artifact paths refer to the original test machine; those artifacts are not distributed with this repository. The observations below are recorded results, not a portable reproduction bundle.

Follow-up to [the NULL-scalar verification](2026-11-06-null-scalar-and-blueprint-feedback-live.md).

## Changes

Successful DAG execution records each consumed scalar: consuming step/parameter, producing step/output, and returned value. Step numbers match the executed SQL sequence, including skipped nodes and pause/resume. Values come from executor result records, not model claims, literal matching or defaults. NULL remains NULL.

The agent response stays SQL-only. Judge and salvage receive `execution.scalar_bindings` alongside the SQL, including through persisted-receipt fallback. Guidance explains scalar substitution while requiring checks of populations, measures and values. Missing lineage is unknown, not proof of a mismatch.

## Validation

- 1,717 tests passed; four skipped. Blueprint, judge evidence, salvage, context, session and judge-exit coverage. Ruff and whitespace checks passed.
- Three serial live DeepSeek V4.1 Flash turns, real local MCP/ClickHouse and Neo4j, required searches on; hints, Help Center and capabilities off.
- All three passed on the first judge review; no salvage. The comparison used one successful blueprint execution and no raw SQL. Its judge input included 107000.0 and 37.5 bound from step 1 into step 2.
- Salary Engineering (125000) and Sales (117500) exceed the Salary company average (107000). Hourly Operations (50/hour) exceeds the Hourly company average (37.5/hour).
- Saved-result follow-up made zero warehouse calls and preserved original units. This reproducer passes; this patch does not add a general no-refresh enforcement mechanism.
- Temporary synthetic hourly fixture changes restored and verified.

## Questions and traces

| Question | Seconds | Trace |
|---|---:|---|
| For all Active and On Leave employees, show the exact median and average recorded pay separately for Salary and Hourly employees. | 49.92 | [trace](http://localhost:6006/projects/UHJvamVjdDozNQ==/spans/d38bd0309d04fa087429597a781d3a64) |
| Which departments are above the corresponding company average pay, separately for Salary and Hourly employees? Keep Active and On Leave employees. Use the departments-above-company-average-salary blueprint if it fits. | 31.64 | [trace](http://localhost:6006/projects/UHJvamVjdDozNQ==/spans/8fc988dcd2c087298e0649f7b40e62da) |
| Summarize that department comparison using saved results and original units. Do not refresh or run another warehouse query. | 34.46 | [trace](http://localhost:6006/projects/UHJvamVjdDozNQ==/spans/782764ef17de24a95588f93543c065f4) |

Full harness and evidence: `/tmp/scalar-lineage-live-20261106/`. Summary artifacts: gitignored `docs/verification/artifacts/scalar-lineage-live-2026-11-06/`. No commit, push or remote deployment performed.

## Final pre-push rerun

Five serial live DeepSeek turns passed after the catalog refresh and judge-evidence cleanup. Every turn used finalizeAnswer, received one first-review approval, and made no salvage call. No tool errors occurred. Saved-result reuse made zero warehouse calls; the changed-population ranking reran with top_n=2 and Active/On Leave/Terminated. Temporary synthetic data was restored.

Phoenix project: `prepush-live-20261106`. Trace IDs are recorded below; access requires the originating Phoenix instance.

| Turn | Seconds | Trace ID |
|---|---:|---|
| median | 57.5 | `707ca7e8ce62c1beacfb52898c071d40` |
| dag | 34.43 | `813ae91cae42efe0cf6d1cb59a50ddc7` |
| reuse | 37.62 | `a8ef12869949e187dbb4fb22f09ed33d` |
| ranking | 16.15 | `f37494fa6531145c57567b9e4a77db0e` |
| modify | 32.76 | `1a269e4ef2c629b50059da62061b6e66` |
