"""Optional Claude assist layer (Stage D). OFF by default.

Nothing in this package imports the ``anthropic`` SDK at module level: the package is
optional, so every SDK import is lazy and guarded. Every assist call degrades to the
deterministic template text when the LLM is disabled, unavailable, times out, refuses
or returns unusable output. The LLM never decides priority, numbering, SLA, assignment
or whether anything is sent; it only drafts text that a person reviews.
"""
