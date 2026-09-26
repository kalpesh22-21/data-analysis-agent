# Final delivery releases unused review reserves

Date: 2026-09-26. Follow-up to the payroll/accruals probe and the partial-answer
budget design. Earlier evaluation reports remain unchanged.

`review_delivery()` now invokes `review_once(..., terminal=True)`. Once this request
is ending, final review can consume its remaining aggregate allowance, including
unused repair and partial-recovery reserves. A paused request also ends this budget
context; a later resume receives a fresh context as before.

This fixes the reproduced case where a 60-second first timeout left 120 seconds in
a 180-second budget, but normal delivery could make no further judge call. Per-call
timeouts, the aggregate deadline, the two-attempt delivery bound, and the binding
explicit-rejection check remain in force. Agent proposal and repair reviews still
cannot consume the protected terminal reserve while agent work is ongoing.

Regression cases exercise 180- and 90-second configured totals with initial timeout,
then approval, rejection, or continued timeout. They assert actual judge calls,
per-call deadlines, bounded total spend, and withholding of explicitly rejected
content. Existing partial-answer and repair-reserve tests remain applicable.
