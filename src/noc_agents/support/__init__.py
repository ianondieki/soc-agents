"""The support desk: online complaint registration and management (docs/SUPPORT_DESK.md).

A customer registers a complaint; four agents handle it, each in its own module, and the
orchestrator in :mod:`desk` runs them in order and records every step:

* :mod:`triage`     -- category, urgency, sentiment, risk flags, language, confidence, route;
* :mod:`resolver`   -- answers from the knowledge base (:mod:`kb`) only when grounded;
* :mod:`actions`    -- the action agent, calling the tools in :mod:`tools` under the limits
                       in ``config/support/policy.yaml``;
* :mod:`escalation` -- the ordered policy table that sends a case to a person, with a reason.

Around them: :mod:`text` (normalisation, Kiswahili/Sheng synonyms, MSISDNs), :mod:`places`
(towns and regions, derived from the operator profile), :mod:`policy` and :mod:`accounts` (the
validated config files), :mod:`context` (all of it in one object, and the feature flag),
:mod:`views` (the read side and the public view), :mod:`seed`, :mod:`ratelimit` and
:mod:`llm_port` (the transfer-recording LLM port).

:mod:`evals` measures the whole desk on a labelled golden set without HTTP and without
touching the live database. Everything is deterministic unless ``LLM_ENABLED`` is on, and
even then the model only breaks a low-confidence triage tie; every model failure falls back.

Nothing here sends anything: the tools act on demo fixtures (``config/support/accounts.yaml``)
and the "configuration SMS" is recorded, never delivered.
"""
