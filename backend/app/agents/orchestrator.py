"""Agent orchestrator: crew assembly and event-bus wiring.

The :class:`Orchestrator` is the orchestration backbone described in the
design's *Component: Agent Orchestrator*. It has three jobs:

1. **Assemble the CrewAI crew** (:meth:`Orchestrator.build_crew`) — define the
   five named agents (Guardian, Architect, Sentinel, Analyst, Herald) with their
   CrewAI ``role`` / ``goal`` / ``backstory`` primitives. ``crewai`` is a heavy
   optional dependency, so it is imported *lazily inside* ``build_crew`` and
   guarded: the rest of this module (and the event-bus wiring in particular)
   works whether or not ``crewai`` is installed. This keeps the module
   import-safe at app startup (``app/main.py`` imports the Orchestrator).

2. **Wire the event bus** (:meth:`Orchestrator.wire_event_bus`) — subscribe each
   agent's handlers to its designated :class:`EventType` values *before any
   event is published* (Requirement 11.1). Concrete agent classes (Guardian,
   Architect, etc.) land in later tasks, so wiring accepts handler registrations
   that those tasks supply; until then the orchestrator wires lightweight
   placeholder handlers so the ordering guarantee and the wiring mechanism are
   in place and testable.

3. **Surface inter-agent communication** (:meth:`Orchestrator.emit_agent_message`)
   — whenever an agent emits an inter-agent communication, publish an
   ``agent.message`` event onto the bus so it reaches the dashboard feed
   (Requirement 11.5).

Design references: design.md "Multi-Agent Orchestration" (agent
subscribe/emit table), "Component: Agent Orchestrator", and the ``agent.message``
WebSocket payload example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.events import Event, EventBus, EventType, Handler
from app.core.logging import get_logger

logger = get_logger(__name__)


# --- Agent specifications ---------------------------------------------------

# The five P1/P2 named agents and the events each subscribes to. This mirrors
# the design's "Agent Roles & Responsibilities" table. The optional Auditor is
# intentionally omitted here (P3) and can be added the same way later.
@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Static description of an agent for crew assembly and wiring.

    ``name`` is the dashboard-facing agent name (e.g. ``"Guardian"``).
    ``subscribes`` lists the event types whose handlers the agent registers when
    the bus is wired.
    """

    name: str
    role: str
    goal: str
    backstory: str
    subscribes: tuple[EventType, ...]


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="Guardian",
        role="Identity & Presence Integrity Officer",
        goal=(
            "Confirm identity and presence integrity by authoritatively "
            "verifying escalated proctoring frames."
        ),
        backstory=(
            "A vigilant proctor who screens locally first and escalates only "
            "genuinely suspicious frames for cloud confirmation."
        ),
        subscribes=(EventType.FRAME_ESCALATED,),
    ),
    AgentSpec(
        name="Architect",
        role="Examination Paper Designer",
        goal=(
            "Generate a unique yet equivalently fair exam paper for every "
            "student from a blueprint."
        ),
        backstory=(
            "A meticulous exam author who guarantees no two students receive "
            "identical papers while preserving topic and difficulty balance."
        ),
        subscribes=(EventType.EXAM_PROVISION,),
    ),
    AgentSpec(
        name="Sentinel",
        role="Behavioral Fraud Analyst",
        goal=(
            "Detect behavioral fraud from session telemetry and produce "
            "explainable anomaly scores."
        ),
        backstory=(
            "A data-driven watcher who turns tab switches, pastes, and timing "
            "into human-readable suspicion with clear reasons."
        ),
        subscribes=(EventType.SESSION_EVENT,),
    ),
    AgentSpec(
        name="Analyst",
        role="Post-Exam Intelligence Officer",
        goal=(
            "Produce post-exam analytics: score distributions, difficulty "
            "heatmaps, and per-student improvement suggestions."
        ),
        backstory=(
            "A reflective analyst who turns completed exams into actionable "
            "intelligence for educators."
        ),
        subscribes=(EventType.EXAM_COMPLETED,),
    ),
    AgentSpec(
        name="Herald",
        role="Real-Time Alert Broadcaster",
        goal=(
            "Broadcast confirmed anomalies to humans in real time over "
            "WebSocket and optional email."
        ),
        backstory=(
            "The action arm of the crew who makes sure no confirmed violation "
            "goes unnoticed by invigilators and admins."
        ),
        subscribes=(EventType.ANOMALY_DETECTED,),
    ),
)


# Stable orchestrator source name used on emitted events and agent messages.
ORCHESTRATOR_SOURCE = "orchestrator"


@dataclass(slots=True)
class Orchestrator:
    """Assemble the agent crew and wire agents onto the shared event bus.

    The orchestrator holds a registry mapping each :class:`EventType` to the
    handlers that should be subscribed for it. Later tasks (Guardian, Architect,
    Herald, ...) call :meth:`register_handler` to supply their real handlers
    before :meth:`wire_event_bus` runs; any event type without a registered
    handler falls back to a lightweight placeholder so the wiring mechanism and
    the ordering guarantee (Requirement 11.1) hold from day one.
    """

    # event type -> ordered list of handlers to subscribe (registration order
    # is preserved so bus delivery order is deterministic, Requirement 11.3).
    _handlers: dict[EventType, list[Handler]] = field(default_factory=dict)
    _bus: EventBus | None = field(default=None, init=False)
    _wired: bool = field(default=False, init=False)
    # Concrete agent references kept alive for the process lifetime (e.g. the
    # Architect, whose background generation tasks must not be GC'd). Populated
    # by :meth:`_register_default_agents` during wiring.
    _agents: dict[str, Any] = field(default_factory=dict, init=False)

    # -- handler registration ------------------------------------------------

    def register_handler(self, event_type: EventType, handler: Handler) -> None:
        """Register ``handler`` to be subscribed for ``event_type`` on wiring.

        Real agent handlers (implemented in later tasks) register here before
        :meth:`wire_event_bus` is called. Handlers are appended so their
        subscription order is preserved.
        """
        self._handlers.setdefault(event_type, []).append(handler)

    def register_agent(
        self, spec: AgentSpec, handler: Handler
    ) -> None:
        """Register ``handler`` for every event type ``spec`` subscribes to.

        Convenience for later tasks that wire a concrete agent with a single
        handler covering all of the agent's subscribed event types.
        """
        for event_type in spec.subscribes:
            self.register_handler(event_type, handler)

    # -- crew assembly -------------------------------------------------------

    def build_crew(self) -> Any:
        """Assemble and return the CrewAI crew (lazy, guarded import).

        ``crewai`` is imported here rather than at module load so this module
        stays import-safe when the package is not installed. When ``crewai`` is
        unavailable this returns ``None`` and logs a warning; the event-bus
        wiring does not depend on the crew, so the system still runs.
        """
        try:
            from crewai import Agent, Crew  # type: ignore import-not-found
        except Exception as exc:  # noqa: BLE001 - optional heavy dependency
            logger.warning(
                "crewai unavailable; building crew skipped",
                extra={"error": repr(exc)},
            )
            return None

        agents = [
            Agent(
                role=spec.role,
                goal=spec.goal,
                backstory=spec.backstory,
                allow_delegation=False,
                verbose=False,
            )
            for spec in AGENT_SPECS
        ]
        crew = Crew(agents=agents, verbose=False)
        logger.info("crew.built", extra={"agentCount": len(agents)})
        return crew

    # -- event-bus wiring ----------------------------------------------------

    def wire_event_bus(self, bus: EventBus) -> None:
        """Subscribe every agent's handlers to its event types (Requirement 11.1).

        Called during startup *before any event is published*. For each agent
        spec, the registered handlers for each subscribed event type are
        subscribed in registration order; an event type with no registered
        handler is wired with a placeholder handler so the subscription exists
        and can be replaced by a later task.
        """
        self._bus = bus

        # Register the concrete P1 agents whose handlers depend on the bus
        # (e.g. the Architect, which publishes paper.generated / generation.failed
        # onto it). This runs before the subscribe pass below so the real
        # handlers — not placeholders — are wired (Requirement 11.1). It is
        # guarded so a missing optional dependency or absent secret never aborts
        # wiring; the placeholder pass then keeps the subscription slot.
        self._register_default_agents(bus)

        # Track which event types already have a real handler subscribed so the
        # placeholder pass below only fills genuinely empty agent slots.
        wired_types: set[EventType] = set()

        # 1) Subscribe every registered handler in registration order. This
        #    covers both concrete agent handlers (e.g. Architect on
        #    exam.provision) and feed listeners on agent.message (the dashboard
        #    transport, Requirement 11.5) which is not in any agent spec.
        for event_type, handlers in self._handlers.items():
            for handler in handlers:
                bus.subscribe(event_type, handler)
                wired_types.add(event_type)
                logger.debug(
                    "orchestrator.wire.handler",
                    extra={
                        "eventType": str(event_type),
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                    },
                )

        # 2) For each agent's designated event types that still have no real
        #    handler, install a named placeholder so the subscription exists and
        #    can be replaced by a later task. This preserves the wiring
        #    mechanism and the "subscribe before publish" guarantee (Req 11.1).
        for spec in AGENT_SPECS:
            for event_type in spec.subscribes:
                if event_type in wired_types:
                    continue
                placeholder = _make_placeholder_handler(spec.name, event_type)
                bus.subscribe(event_type, placeholder)
                logger.debug(
                    "orchestrator.wire.placeholder",
                    extra={"agent": spec.name, "eventType": str(event_type)},
                )

        self._wired = True
        logger.info(
            "orchestrator.wired",
            extra={"agentCount": len(AGENT_SPECS)},
        )

    @property
    def is_wired(self) -> bool:
        """Whether :meth:`wire_event_bus` has completed."""
        return self._wired

    # -- service entry points ------------------------------------------------

    async def provision_exam(self, exam_id: str) -> None:
        """Trigger paper provisioning for ``exam_id`` (skeleton).

        Publishes an ``exam.provision`` event onto the bus so the Architect
        (wired in a later task) can generate a paper per enrolled student. The
        per-student fan-out and blueprint lookup are implemented alongside the
        exam service and Architect agent in later tasks; this skeleton
        establishes the orchestration entry point and the event it emits.
        """
        if self._bus is None:
            raise RuntimeError(
                "Orchestrator.provision_exam called before wire_event_bus"
            )

        logger.info("orchestrator.provision_exam", extra={"examId": exam_id})
        await self._bus.publish(
            Event(
                type=EventType.EXAM_PROVISION,
                payload={"examId": exam_id},
                source=ORCHESTRATOR_SOURCE,
            )
        )

    async def emit_agent_message(
        self,
        source: str,
        to: str,
        text: str,
        level: str = "info",
        session_id: str | None = None,
    ) -> None:
        """Publish an ``agent.message`` event to the dashboard feed (Req 11.5).

        Whenever an agent emits an inter-agent communication, the orchestrator
        publishes an :data:`EventType.AGENT_MESSAGE` event whose payload mirrors
        the design's dashboard ``agent.message`` payload (``to``, ``text``,
        ``level``). The transport layer streams this onto the dashboard room.
        """
        if self._bus is None:
            raise RuntimeError(
                "Orchestrator.emit_agent_message called before wire_event_bus"
            )

        await self._bus.publish(
            Event(
                type=EventType.AGENT_MESSAGE,
                payload={"to": to, "text": text, "level": level},
                source=source,
                session_id=session_id,
            )
        )

    # -- concrete agent registration -----------------------------------------

    def _register_default_agents(self, bus: EventBus) -> None:
        """Register the concrete P1 agents that publish onto the bus.

        Currently wires the :class:`~app.agents.architect.ArchitectAgent` to
        ``exam.provision`` so a provisioning fan-out actually generates papers.
        Each registration is best-effort and guarded: a missing optional
        dependency, an absent secret, or an already-registered handler must
        never abort wiring (the placeholder pass keeps the subscription slot).
        Idempotent — re-wiring does not double-register an agent.
        """
        if "Architect" not in self._agents:
            try:
                from app.agents.architect import ArchitectAgent, GenerationConfig
                from app.agents.llm import get_default_llm_client
                from app.core.db import get_session_factory
                from app.repositories.paper import PaperRepository

                def _paper_repo_factory() -> PaperRepository:
                    # A fresh session per background generation so concurrent
                    # tasks never share a session.
                    return PaperRepository(get_session_factory()())

                architect = ArchitectAgent(
                    llm=get_default_llm_client(),
                    bus=bus,
                    paper_repo_factory=_paper_repo_factory,
                    config=GenerationConfig(),
                )
                self.register_handler(
                    EventType.EXAM_PROVISION, architect.on_exam_provision
                )
                self._agents["Architect"] = architect
                logger.info("orchestrator.agent.registered", extra={"agent": "Architect"})
            except Exception as exc:  # noqa: BLE001 - optional/secret-guarded
                logger.warning(
                    "orchestrator.agent.register_skipped",
                    extra={"agent": "Architect", "error": repr(exc)},
                )

        if "Guardian" not in self._agents:
            try:
                from app.agents.guardian import GuardianAgent
                from app.agents.vision import get_default_vision_client
                from app.core.db import get_session_factory
                from app.repositories.anomaly import AnomalyRepository

                def _anomaly_repo_factory() -> AnomalyRepository:
                    # A fresh session per escalation so concurrent confirmations
                    # never share a session.
                    return AnomalyRepository(get_session_factory()())

                guardian = GuardianAgent(
                    vision=get_default_vision_client(),
                    bus=bus,
                    anomaly_repo_factory=_anomaly_repo_factory,
                    orchestrator=self,
                )
                self.register_handler(
                    EventType.FRAME_ESCALATED, guardian.on_frame_escalated
                )
                self._agents["Guardian"] = guardian
                logger.info("orchestrator.agent.registered", extra={"agent": "Guardian"})
            except Exception as exc:  # noqa: BLE001 - optional/secret-guarded
                logger.warning(
                    "orchestrator.agent.register_skipped",
                    extra={"agent": "Guardian", "error": repr(exc)},
                )

        if "Sentinel" not in self._agents:
            try:
                from app.agents.sentinel import SentinelAgent, SentinelConfig
                from app.core.db import get_session_factory
                from app.repositories.anomaly import AnomalyRepository

                def _sentinel_anomaly_repo_factory() -> AnomalyRepository:
                    # A fresh session per detected anomaly so concurrent
                    # sessions never share a session.
                    return AnomalyRepository(get_session_factory()())

                sentinel = SentinelAgent(
                    bus=bus,
                    anomaly_repo_factory=_sentinel_anomaly_repo_factory,
                    config=SentinelConfig(),
                )
                self.register_handler(
                    EventType.SESSION_EVENT, sentinel.on_session_event
                )
                self._agents["Sentinel"] = sentinel
                logger.info("orchestrator.agent.registered", extra={"agent": "Sentinel"})
            except Exception as exc:  # noqa: BLE001 - optional/secret-guarded
                logger.warning(
                    "orchestrator.agent.register_skipped",
                    extra={"agent": "Sentinel", "error": repr(exc)},
                )

        if "Herald" not in self._agents:
            try:
                from app.agents.herald import HeraldAgent
                from app.core.db import get_session_factory
                from app.core.ws import get_ws_manager
                from app.repositories.alert import AlertRepository
                from app.repositories.session import ExamSessionRepository

                def _alert_repo_factory() -> AlertRepository:
                    # A fresh session per anomaly so concurrent broadcasts never
                    # share a session.
                    return AlertRepository(get_session_factory()())

                def _session_repo_factory() -> ExamSessionRepository:
                    return ExamSessionRepository(get_session_factory()())

                herald = HeraldAgent(
                    alert_repo_factory=_alert_repo_factory,
                    session_repo_factory=_session_repo_factory,
                    ws_manager=get_ws_manager(),
                )
                self.register_handler(
                    EventType.ANOMALY_DETECTED, herald.on_anomaly_detected
                )
                self._agents["Herald"] = herald
                logger.info("orchestrator.agent.registered", extra={"agent": "Herald"})
            except Exception as exc:  # noqa: BLE001 - optional/secret-guarded
                logger.warning(
                    "orchestrator.agent.register_skipped",
                    extra={"agent": "Herald", "error": repr(exc)},
                )

        if "Analyst" not in self._agents:
            try:
                from app.agents.analyst import AnalystAgent, AnalystConfig
                from app.agents.llm import get_default_llm_client
                from app.core.db import get_session_factory

                def _analyst_session_factory():
                    # A fresh DB session per report build so concurrent
                    # exam completions never share a session.
                    return get_session_factory()()

                analyst = AnalystAgent(
                    llm=get_default_llm_client(),
                    bus=bus,
                    session_factory=_analyst_session_factory,
                    config=AnalystConfig(),
                )
                self.register_handler(
                    EventType.EXAM_COMPLETED, analyst.on_exam_completed
                )
                self._agents["Analyst"] = analyst
                logger.info("orchestrator.agent.registered", extra={"agent": "Analyst"})
            except Exception as exc:  # noqa: BLE001 - optional/secret-guarded
                logger.warning(
                    "orchestrator.agent.register_skipped",
                    extra={"agent": "Analyst", "error": repr(exc)},
                )

        # The optional sixth agent (P3): the Auditor reviews each generated
        # paper for fairness on ``paper.generated`` and sets its audit status.
        if "Auditor" not in self._agents:
            try:
                from app.agents.auditor import AuditConfig, AuditorAgent
                from app.agents.llm import get_default_llm_client
                from app.core.db import get_session_factory
                from app.repositories.paper import PaperRepository

                def _auditor_paper_repo_factory() -> PaperRepository:
                    # A fresh session per audit so concurrent reviews never
                    # share a session.
                    return PaperRepository(get_session_factory()())

                auditor = AuditorAgent(
                    llm=get_default_llm_client(),
                    bus=bus,
                    paper_repo_factory=_auditor_paper_repo_factory,
                    config=AuditConfig(),
                )
                self.register_handler(
                    EventType.PAPER_GENERATED, auditor.on_paper_generated
                )
                self._agents["Auditor"] = auditor
                logger.info("orchestrator.agent.registered", extra={"agent": "Auditor"})
            except Exception as exc:  # noqa: BLE001 - optional/secret-guarded
                logger.warning(
                    "orchestrator.agent.register_skipped",
                    extra={"agent": "Auditor", "error": repr(exc)},
                )

    def get_agent(self, name: str) -> Any:
        """Return a registered concrete agent by name, or ``None``."""
        return self._agents.get(name)


def _make_placeholder_handler(agent_name: str, event_type: EventType) -> Handler:
    """Build a no-op async handler tagged with its agent for observability.

    The placeholder keeps a subscription slot for an agent whose concrete
    handler is implemented in a later task. It simply logs at debug level so
    wiring is observable; it never raises, so it cannot disrupt delivery.
    """

    async def _placeholder(event: Event) -> None:
        logger.debug(
            "orchestrator.placeholder.received",
            extra={
                "agent": agent_name,
                "eventType": str(event_type),
                "eventId": event.id,
            },
        )

    # A readable identifier so the event bus's central error/debug logs and the
    # wiring tests can attribute this subscription to the right agent.
    _placeholder.__qualname__ = f"placeholder[{agent_name}:{event_type}]"
    _placeholder.__name__ = _placeholder.__qualname__
    return _placeholder


# Process-wide orchestrator instance used by the app factory (task 7.1).
_default_orchestrator = Orchestrator()


def get_orchestrator() -> Orchestrator:
    """Return the process-wide orchestrator instance."""
    return _default_orchestrator
