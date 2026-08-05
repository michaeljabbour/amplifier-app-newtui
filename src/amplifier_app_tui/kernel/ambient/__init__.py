"""The ambient delegation layer (compliance item **B8**).

``docs/plans/2026-08-03-voice-first-ambient-delegation.md`` is the
specification this package implements. Its central architectural claim -- the
one every module here is arranged to protect -- is that **voice and mobile
clients stay thin adapters**: an adapter may authenticate a principal,
transport bytes, and render, and it may hold no policy, no permission state,
no session ownership and no history. Everything an adapter would otherwise be
tempted to remember lives here instead, one layer below the channel, so a
second client (a phone, a chat bridge, a Rust CLI) inherits it rather than
re-implementing -- and re-breaking -- it.

Layering (ADR-0007): pure ``kernel/`` logic over the filesystem and the
existing contracts. No Textual, no amplifier-core, no runtime import; every
module unit-tests against ``tmp_path`` with an injected clock, exactly like
:mod:`kernel.session_control` does.

What sits underneath, reused unchanged:

- **B6** :mod:`kernel.session_control` -- the lease, the ``pause`` ->
  ``handoff.claim`` escalation, actor attribution and the durable
  ``control-audit.jsonl``. The interpretation loop *gates* on B6 rather than
  inventing a second gate.
- **B7** :mod:`kernel.attention_store` + ``ui.notifications`` -- the durable,
  cross-process attention record whose ``event_id`` is the correlation key the
  reply channel resolves against.

The design doc's eight required contract extensions map onto modules:

- **E1** authenticated principal -> ``Actor`` -- :mod:`.principal`, which
  *consumes* ``kernel.session_authz``. The authorization policy itself is NOT
  owned here, and is optional: absent, the layer degrades to an explicitly
  unverified local principal.
- **E2** grant store + ``source.*``/``grant.*`` audit -- :mod:`.grants`.
- **E3** structured, editable interpretation payload -- :mod:`.interpretation`.
- **E4** push payload carries the attention event id -- already delivered by
  B7 (``ui.notifications.attention_push_payload``); :mod:`.voice` adds the
  stricter pointer-only *ambient* payload on top.
- **E5** durable cross-process attention records -- already delivered by B7
  (:mod:`kernel.attention_store`); consumed by :mod:`.reply`.
- **E6** cross-project session discovery -- :mod:`.discovery`.
- **E7** authenticated inbound reply channel -- :mod:`.reply` provides the
  security/submission core and loopback transport; :mod:`.reply_listener`
  gives live TUI sessions startup/shutdown ownership plus private same-host
  port discovery. **No remotely reachable listener ships** -- see those
  modules.
- **E8** Teams/Outlook connectors -- :mod:`.sources` ships the PORT and a
  working local implementation only. **A real connector cannot be built or
  verified offline**; that module says so and explains what is missing.

:mod:`.voice` is the adapter the doc deliberately sequenced last: a thin
composition over the modules above that holds no state of its own.
"""

from __future__ import annotations

from .discovery import ActivityRow, SessionDiscovery, discover_sessions, project_row
from .grants import (
    Grant,
    GrantDecision,
    GrantError,
    GrantStore,
    authorize_source,
    consume_grant,
    parse_scope,
)
from .interpretation import (
    EDITABLE_FIELDS,
    Interpretation,
    InterpretationDesk,
    InterpretationError,
    ReversibilityClass,
)
from .principal import LocalPrincipal, PrincipalLike, actor_for, session_authz_available
from .reply import (
    CorrelationTable,
    DeviceRegistry,
    LoopbackReplyListener,
    NeedsYouReplySubmissionPort,
    PendingReply,
    ReplyChannel,
    ReplyDeliveryStore,
    ReplyEnvelope,
    ReplyOutcome,
    ReplySubmissionPort,
    ReplySubmissionResult,
    sign_reply,
)
from .reply_listener import (
    ReplyListenerEndpoint,
    ReplyListenerLifecycle,
    ReplyListenerRegistry,
    ReplyListenerStatus,
    discover_reply_endpoints,
    discover_reply_endpoints_for_event,
)
from .sources import GrantedSource, LocalFileSource, SourceItem, SourcePort, SourceSendResult
from .voice import (
    AMBIENT_PUSH_FIELDS,
    AmbientVoiceAdapter,
    FollowOnPlan,
    PlanStep,
    RequestFacts,
    VoiceTurn,
    ambient_push_payload,
    classify_request,
    parse_response,
)

__all__ = [
    "AMBIENT_PUSH_FIELDS",
    "EDITABLE_FIELDS",
    "ActivityRow",
    "AmbientVoiceAdapter",
    "CorrelationTable",
    "DeviceRegistry",
    "FollowOnPlan",
    "Grant",
    "GrantDecision",
    "GrantError",
    "GrantStore",
    "GrantedSource",
    "Interpretation",
    "InterpretationDesk",
    "InterpretationError",
    "LocalFileSource",
    "LocalPrincipal",
    "LoopbackReplyListener",
    "NeedsYouReplySubmissionPort",
    "PendingReply",
    "PlanStep",
    "PrincipalLike",
    "ReplyChannel",
    "ReplyDeliveryStore",
    "ReplyEnvelope",
    "ReplyListenerEndpoint",
    "ReplyListenerLifecycle",
    "ReplyListenerRegistry",
    "ReplyListenerStatus",
    "ReplyOutcome",
    "ReplySubmissionPort",
    "ReplySubmissionResult",
    "RequestFacts",
    "ReversibilityClass",
    "SessionDiscovery",
    "SourceItem",
    "SourcePort",
    "SourceSendResult",
    "VoiceTurn",
    "actor_for",
    "ambient_push_payload",
    "authorize_source",
    "classify_request",
    "consume_grant",
    "discover_reply_endpoints",
    "discover_reply_endpoints_for_event",
    "discover_sessions",
    "parse_response",
    "parse_scope",
    "project_row",
    "session_authz_available",
    "sign_reply",
]
