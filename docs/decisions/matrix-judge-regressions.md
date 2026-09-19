# Matrix judge regression cases

2026-09-19: added focused coverage for unsupported UI claims and unrelated selected options discovered in the judged route matrix. The judge now checks every selected option, even when prose omits it or a correct table answers the request. Examples distinguish workflow documentation from promises about the fields in a particular option.

Cases: `tests/fixtures/runtime/matrix_judge_cases.json`. Run with the existing `scripts/probe_runtime_judge.py --cases tests/fixtures/runtime/matrix_judge_cases.json --output <local-output-path>` harness using its configured model credentials. These are live model evaluations, not deterministic CI assertions.

Live verification: Kimi 2.7 Code, six of six reviewed verdicts matched expectations. Three rejections and three valid-answer controls; no fail-open verdict counted as passing. Requests were paced outside the reviewer timeout in a temporary harness (35 seconds between starts, 90-second review timeout). No production pacing or timeout change. Completion tracing targeted the local `data-agent-runtime` Phoenix project.

| Case | Expected | Observed | Trace |
|---|---|---|---|
| unsupported_create_seats | reject | unsupported_by_evidence | `78e95c44e4cad5b43505d650176d91d7` |
| grounded_navigation | approve | approved | `35b3d46880f753d33a7d765584561a04` |
| unsupported_position_prompt | reject | unsupported_by_evidence | `a92c9206b7ab42c4518c439441e640d3` |
| grounded_draft | approve | approved | `b1cf1db97a15de346eecf829eb23c358` |
| unrelated_profile_beside_table | reject | capability_intent_mismatch | `db28516c073c27f0bd99eb6e9026a28c` |
| requested_navigation_beside_table | approve | approved | `618852b71a013a99df50f85e845611e7` |

The unrelated-profile rejection selected only `profile1` with `repair_type=omit_component`, preserving the valid table. The full runtime suite passed 3,862 tests (6 skipped) after the discovery removal and judge guidance changes. This is a focused regression result, not a rerun of the full routing matrix or evidence that all model outputs will now comply. Warehouse consistency and multipart-ledger compliance remain separate open findings.
