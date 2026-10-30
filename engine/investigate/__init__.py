"""`investigate` — a hypothesis -> checked cuts -> deterministic verdicts ->
skeptic -> memory loop over the existing L1-L7 pipeline (docs/investigate.md).

The LLM proposes hypotheses, cut wording and which skeptic checks to run (plus
L1/L2 inside every cut). It never assigns a label, computes a number, or decides
whether a check passed — that is all deterministic Python in this package.
"""
