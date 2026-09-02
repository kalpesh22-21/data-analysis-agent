"""Prompt fragments shared by more than one package on the learning plane.

⚠ A RULE LIVES HERE WHEN TWO PACKAGES OWE THE MODEL THE SAME SENTENCE AND NEITHER OWNS THE OTHER.

`mint/prompt.py` used to import `DATE_RULE` from `revise/prompt.py`, which read as minting
depending on revising — two peers with no relationship, joined because one of them happened to
write the sentence down first. The dependency was also the wrong shape for what the rule IS: it
belongs to the CHECK that enforces it (`generalize/validate.py::check_no_frozen_date_literal`),
and every prompt that lets a model write SQL owes it.

Nothing here imports from the packages that use it, so it cannot become a place for one caller's
specifics to leak into another's prompt.
"""

from .sql_rules import DATE_RULE

__all__ = ["DATE_RULE"]
