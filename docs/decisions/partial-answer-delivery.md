# Deliver supported findings when the full request cannot finish

A rejected full answer previously cleared accumulated components and ended in a
blanket inability message, even when successful results supported part of the request.
The runtime now treats a reviewed partial answer as an independent delivery approval.

## Judge contract and recovery

On a final proposal the judge can reject the full answer and simultaneously return
`partial_answer`: exact supported prose, evidence IDs, selected table result IDs and
capability references, and a nonempty list of unfinished parts. The runtime renders
that exact answer followed by `Unfinished: ` and the disclosed gaps. This approves
only the subset; the original rejection still governs agent repair. The ordinary
prose-correction contract is unchanged and cannot be combined with a partial approval.

Structural validation requires successful eligible evidence visible to that judge,
current access scope, existing selected components, valid prose, and explicit gaps.
Evidence correctness and sufficiency remain the judge's responsibility. Partial
answers can remove incorrect claims and numbers, unlike a prose-only correction.
No original assumptions accompany the subset; necessary qualifications must be in
its approved prose. The judge can approve useful progress within a single request.

The latest approved subset is persisted in the turn's review state. A later explicit
review supersedes it. Any changed successful evidence or access scope invalidates
it, conservatively requiring fresh review. Failed subsequent attempts alone do not
invalidate independently supported findings. An unavailable review cannot approve a
new subset or clear an earlier rejection.

On terminal failure, no progress, or a hard budget stop, delivery first reuses an
unchanged subset approval. Otherwise it permits one terminal judge attempt using
existing authorized results; it does not restart the agent or query the warehouse.
This also works after a service failure before the agent produced a final proposal.
The terminal verdict is reused by delivery so it does not trigger duplicate review.
An explicit terminal rejection without an approved subset remains binding.

## Budget and persistence

The aggregate budget remains configurable and defaults to three per-call timeouts.
There are now separate repair and terminal-review reserves, each capped at one call
or one third of the total. Ordinary reviews cannot consume either reserve; repair
validation cannot consume the terminal reserve. All attempts charge elapsed time.
This supersedes the half-total repair reserve described in the earlier Kimi report.

Delivered partials expose `review.completion = partial` alongside reviewed approval.
The execution stop reason remains available separately. Persisted messages include
an exact component selection so history cannot reconstruct rejected tables from the
last attempted full answer. The message and all selected components share the union
of their approved evidence provenance; narrowing access withholds them together.

Regression tests cover terminal recovery, approved subset reuse after unavailable
review, later contradictory/new evidence and rejection, scope changes, malformed or
missing support, exact table/card selection, history reload, and isolated review
reserves. Live model behavior still requires a separate probe.

Final validation: **4,410 passed, 5 skipped** across runtime and UI tests; Ruff and
whitespace checks pass. One inbox form test failed intermittently in an earlier run,
then passed unchanged both alone and in the final full run. No live Kimi probe was
run for this change.
