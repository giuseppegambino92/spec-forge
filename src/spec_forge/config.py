"""Central configuration: fuzzing budgets, shrinking limits, MCTS constants, sandbox policy.

Every knob is a plain dataclass field so configs are trivially serialisable (they are
shipped to the sandbox worker as JSON) and reproducible (all randomness is seeded).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FuzzConfig:
    trials: int = 600                 # generated inputs per campaign
    seed: int = 1337
    max_collection_len: int = 40      # upper bound on generated list/str/dict sizes
    precondition_retries: int = 30    # attempts to satisfy :requires: per trial
    extra_trials_after_failure: int = 60  # keep exploring to collect more counterexamples
    max_counterexamples: int = 4
    collect_passing: int = 24         # passing inputs kept for spectrum fault localisation
    coverage_guided: bool = True
    per_call_timeout_s: float = 0.5   # wall clock per target invocation (non-termination check)
    concurrency_threads: tuple[int, int] = (2, 8)
    concurrency_calls: tuple[int, int] = (1, 60)
    min_size: int = 0                 # size floor (stress protocol: force large inputs from trial 1)


@dataclass
class ShrinkConfig:
    max_evaluations: int = 4000
    time_budget_ms: float = 250.0     # hard stop; typical shrinks finish far below 50 ms
    flaky_retries: int = 4            # for nondeterministic (concurrency) predicates


@dataclass
class MCTSConfig:
    iterations: int = 80              # candidate evaluations (verifier calls) per repair
    max_depth: int = 3                # max composed AST edits per candidate
    c_puct: float = 1.4
    quick_trials: int = 160           # property trials when scoring a candidate
    top_suspicious_lines: int = 8
    seed: int = 7
    use_priors: bool = True           # False = ablation: uniform priors, no fault localisation


@dataclass
class CegisConfig:
    max_rounds: int = 6               # Breaker <-> Builder refinement iterations
    final_validation_trials: int = 1000


@dataclass
class SandboxPolicy:
    isolation: str = "subprocess"     # "subprocess" (default) | "inprocess" (tests / speed)
    job_timeout_s: float = 60.0
    cpu_seconds: int = 300
    memory_mb: int = 1024
    max_source_bytes: int = 200_000
    max_ast_nodes: int = 50_000
    allowed_imports: tuple[str, ...] = (
        "__future__", "abc", "bisect", "collections", "collections.abc", "copy", "dataclasses",
        "decimal", "enum", "fractions", "functools", "heapq", "itertools", "json", "math",
        "numbers", "operator", "re", "statistics", "string", "threading", "time", "typing",
        "unicodedata",
    )


@dataclass
class SpecForgeConfig:
    fuzz: FuzzConfig = field(default_factory=FuzzConfig)
    shrink: ShrinkConfig = field(default_factory=ShrinkConfig)
    mcts: MCTSConfig = field(default_factory=MCTSConfig)
    cegis: CegisConfig = field(default_factory=CegisConfig)
    sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    ledger_path: str = ".specforge/ledger.db"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpecForgeConfig":
        def tup(d: dict[str, Any]) -> dict[str, Any]:
            return {k: tuple(v) if isinstance(v, list) else v for k, v in d.items()}

        return cls(
            fuzz=FuzzConfig(**tup(data.get("fuzz", {}))),
            shrink=ShrinkConfig(**data.get("shrink", {})),
            mcts=MCTSConfig(**data.get("mcts", {})),
            cegis=CegisConfig(**data.get("cegis", {})),
            sandbox=SandboxPolicy(**tup(data.get("sandbox", {}))),
            ledger_path=data.get("ledger_path", ".specforge/ledger.db"),
        )
