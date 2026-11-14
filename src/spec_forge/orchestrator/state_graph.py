"""Zero-framework directed *cyclic* state graph with an event bus.

Nodes are plain callables ``(state) -> state``. Edges are either static (``add_edge``) or
conditional (``add_conditional_edges`` with a router ``(state) -> next node name``). Cycles are
first-class (the CEGIS Breaker <-> Builder loop is one); a step budget guarantees termination.
Every transition is published on the ``EventBus`` so UIs (TUI, MCP progress) can observe runs.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, TypeVar

END = "__end__"
S = TypeVar("S")


@dataclass
class Event:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, Callable[[Event], None]]] = []
        self._lock = threading.Lock()
        self.history: list[Event] = []
        self.keep_history = True

    def subscribe(self, fn: Callable[[Event], None], prefix: str = "") -> Callable[[], None]:
        with self._lock:
            self._subs.append((prefix, fn))

        def unsubscribe() -> None:
            with self._lock:
                self._subs[:] = [s for s in self._subs if s[1] is not fn]

        return unsubscribe

    def publish(self, kind: str, payload: Optional[dict[str, Any]] = None) -> Event:
        ev = Event(kind, payload or {})
        with self._lock:
            if self.keep_history:
                self.history.append(ev)
            subs = list(self._subs)
        for prefix, fn in subs:
            if kind.startswith(prefix):
                try:
                    fn(ev)
                except Exception:  # noqa: BLE001 - observers must never break the run
                    pass
        return ev


class GraphError(RuntimeError):
    pass


class StateGraph(Generic[S]):
    def __init__(self, name: str, bus: Optional[EventBus] = None) -> None:
        self.name = name
        self.bus = bus or EventBus()
        self.nodes: dict[str, Callable[[S], S]] = {}
        self.edges: dict[str, str] = {}
        self.routers: dict[str, tuple[Callable[[S], str], Optional[set[str]]]] = {}
        self.entry: Optional[str] = None

    def add_node(self, name: str, fn: Callable[[S], S]) -> "StateGraph[S]":
        if name in self.nodes or name == END:
            raise GraphError(f"duplicate or reserved node name {name!r}")
        self.nodes[name] = fn
        return self

    def add_edge(self, src: str, dst: str) -> "StateGraph[S]":
        self._check(src, dst)
        self.edges[src] = dst
        return self

    def add_conditional_edges(self, src: str, router: Callable[[S], str],
                              targets: Optional[set[str]] = None) -> "StateGraph[S]":
        if src not in self.nodes:
            raise GraphError(f"unknown node {src!r}")
        for t in targets or ():
            if t != END and t not in self.nodes:
                raise GraphError(f"unknown router target {t!r}")
        self.routers[src] = (router, targets)
        return self

    def set_entry(self, name: str) -> "StateGraph[S]":
        if name not in self.nodes:
            raise GraphError(f"unknown entry node {name!r}")
        self.entry = name
        return self

    def _check(self, src: str, dst: str) -> None:
        if src not in self.nodes:
            raise GraphError(f"unknown node {src!r}")
        if dst != END and dst not in self.nodes:
            raise GraphError(f"unknown node {dst!r}")

    def validate(self) -> None:
        if self.entry is None:
            raise GraphError("entry node not set")
        for n in self.nodes:
            if n not in self.edges and n not in self.routers:
                raise GraphError(f"node {n!r} has no outgoing edge (use END to terminate)")

    def run(self, state: S, max_steps: int = 100) -> S:
        self.validate()
        current = self.entry
        assert current is not None
        steps = 0
        self.bus.publish("graph.start", {"graph": self.name, "entry": current})
        while current != END:
            if steps >= max_steps:
                self.bus.publish("graph.budget_exhausted", {"graph": self.name, "node": current, "steps": steps})
                break
            steps += 1
            t0 = time.perf_counter()
            self.bus.publish("node.enter", {"node": current, "step": steps})
            state = self.nodes[current](state)
            dt = (time.perf_counter() - t0) * 1000
            if current in self.routers:
                router, targets = self.routers[current]
                nxt = router(state)
                if targets is not None and nxt not in targets:
                    raise GraphError(f"router of {current!r} returned undeclared target {nxt!r}")
            else:
                nxt = self.edges[current]
            self.bus.publish("node.exit", {"node": current, "next": nxt, "ms": round(dt, 2), "step": steps})
            current = nxt
        self.bus.publish("graph.end", {"graph": self.name, "steps": steps})
        return state
