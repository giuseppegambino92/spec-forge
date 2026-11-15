# spec-forge

**English** | [Italiano](README.it.md)

<a href="https://paypal.me/giuseppegambino92"><img src="https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif" alt="Donate with PayPal"></a>

**Neuro-symbolic multi-agent code verification and self-healing synthesis for Python.**

Unit tests check the examples somebody thought of. spec-forge infers what the code *promises*
(contracts, conservation laws, idempotency, round-trip invertibility, purity, atomicity...),
searches adversarially for an input that breaks the promise, shrinks it to a minimal
counterexample, and then repairs the code with a Monte Carlo Tree Search over AST edits. Every
candidate patch is re-verified against every counterexample ever found, in a
Counterexample-Guided Inductive Synthesis (CEGIS) loop.

The whole engine is first-principles Python with **no AI frameworks** (no LangChain, LangGraph,
AutoGen or CrewAI): the cyclic state graph, MCTS planner, delta-debugging shrinker, interval
constraint solver, firewall, sandbox and MCP server are all in this repository. It runs
**100% offline and deterministically** by default. LLMs (Anthropic, OpenAI, Ollama) are optional
proposal policies, and their output is held to the same verifier as every other candidate.

---

## Measured results

The benchmark was run **3 consecutive times** with `python -m evals.repeat_runs` on 2026-11-14,
on Apple Silicon (arm64, 14 cores), macOS 27.2, Python 3.11.18, with the subprocess sandbox
enabled. Each run started only once the machine was quiet: the 1-minute load average was
0.31–0.33 per core, against a threshold of 0.35. Each run took 34.4–35.3 s.

Where a value changed between runs, the table gives the **min–max interval across the 3 runs**.
A single value means all 3 runs agreed exactly. The data is in
[`evals/repeatability_report.json`](evals/repeatability_report.json), and the last run's full
report is [`evals/latest_report.json`](evals/latest_report.json).

| Metric | Target | Naive unit tests | **spec-forge (measured, 3 runs)** |
|---|---|---|---|
| Silent bug discovery | ≥ 90% | 0 / 12 (0%) | **12 / 12 (100%)** in every run. All 15/15 counterexamples were confirmed as real bugs by replaying them against the reference fix. **0 / 12 false alarms** on the fixed versions. |
| Counterexample shrinking | > 80% reduction in < 50 ms | n/a | **96.28–96.30%** aggregate reduction (83.69–83.90% mean per sample) on 36 large inputs. Median **0.33–0.36 ms**, p95 2.94–3.02 ms, worst 27.9–28.5 ms. **100%** of samples finished under 50 ms in every run. |
| Autonomous CEGIS repair | ≥ 85% | n/a | **12 / 12 (100%)** in every run. Every patch passed an independent **1,000-trial** fresh-seed adversarial campaign and agreed **100%** with the reference fix on 1,000 differential inputs (100 thread-harness runs for B11). |
| Sandbox / payload blocking | 100% in < 2 ms | n/a | **20 / 20 blocked (100%)** in every run. Mean **0.074–0.076 ms**, worst single audit **0.13–0.19 ms**. 24 / 24 benign modules allowed. |

Other measured numbers:

- **Time to first failure:** median 3 trials, or 0.65–0.67 ms.
- **Shrinking on ordinary campaign counterexamples:** 83.37–83.57% aggregate reduction, median
  0.15–0.16 ms, 100% under 50 ms.
- **Repair cost:** median 1 verifier call. Healing a module takes a median 103–104 ms end to end.
- **Ablation, MCTS with uniform priors and no fault localisation:** still 12 / 12 repaired, but it
  needs a mean of 4.92 verifier calls instead of 2.0 (25 instead of 1 on B05).

**Repeatability.** Every discrete outcome was identical in all 3 runs: bugs found,
counterexamples and their minimal inputs, trials to first failure, patches and diffs, verifier
calls, oracle agreement, shrink complexities, and firewall verdicts. The one declared exception
is the concurrency case B11, which depends on OS thread scheduling. Its outcome fields (found,
repaired, validated, 100% oracle agreement) matched, but its minimal counterexample varied
between 2 and 4 threads. That variation is also why the aggregate shrink percentages move in the
second decimal place. Timing metrics varied by a coefficient of variation of at most 3.4%, except
the worst single firewall audit (19%, a 0.13 → 0.19 ms outlier).

**Timing depends on machine load.** An earlier single run in a busier session took 52.9 s and
measured a worst shrink of 50.6 ms (97.2% of samples under 50 ms) and a worst single audit of
0.89 ms. The discrete outcomes were identical to the runs above. This is why `evals/repeat_runs.py`
waits for a quiet machine before each run.

### Per bug class

| # | Bug class (domain) | Violated property found | Minimal counterexample | Verified patch |
|---|---|---|---|---|
| B01 | Conservation leak on rounding (financial) | `:ensures: sum(result) == total_cents` | `split_payment(total_cents=1, parties=2)` | contract-guided residual rebalance |
| B02 | Off-by-one sliding-window boundary (rate limiting) | at most `limit` admits in any closed window | `limit=1, timestamps=[0, 4], window=4` | `>=` → `>` (L22) |
| B03 | Non-idempotent normalisation (search) | `f(f(x)) == f(x)` and `"  " not in result` | `text='0   0'` | fixpoint closure |
| B04 | Mutable default state corruption | determinism / no input mutation | `register_event(name='')` called twice | defensive copy on entry |
| B05 | Division by zero on degenerate variance | totality | `zscores(samples=[0.0])` | divisor guard (L15) |
| B06 | Unsigned quota underflow | `:ensures: 0 <= result <= remaining` | `remaining=0, cost=1` | clamp derived from `:ensures:` |
| B07 | Float drift / order dependence (ledger) | permutation invariance | 3 postings | `sum` → `math.fsum` |
| B08 | Off-by-one slice bound (pagination) | partition `:law:` | `items=[0], page=0, size=1` | literal `1` → `0` (L13) |
| B09 | Round-trip invertibility (RLE codec) | `decode(encode(x)) == x` | `data=' '` | `j - i` → `j - i + 1` (12 verifier calls) |
| B10 | Input aliasing (in-place sort) | no input mutation | `scores=[0, 1], k=0` | defensive copy on entry |
| B11 | Race condition, lost update | atomicity (concurrent ≡ sequential) | 2–4 threads × 1 call (varies with scheduling) | per-instance `RLock` around read-modify-write |
| B12 | Index out of bounds at p = 100 | totality | `percentile(values=[0.0], p=100.0)` | index clamp (L14) |

### How to read these numbers

Read the caveats before quoting the results.

- **The 0/12 naive baseline holds by construction.** Each naive suite in
  [`evals/datasets/naive_suites.py`](evals/datasets/naive_suites.py) is the happy-path test that
  realistically ships with such code. The benchmark checks that these suites pass on both the
  buggy and the fixed versions, so they fail to detect the bugs rather than being broken.
- **The corpus and the repair grammar come from the same author.** All 12 bugs fall inside the
  repair grammar's reach, and the MCTS priors map each violation kind to the edit categories
  likely to fix it. That is why the median is 1 verifier call. The uniform-prior ablation shows
  the search still succeeds without that knowledge, at about 2.5× the cost. The benchmark does not
  show general automated-repair performance on arbitrary real-world bugs. Bugs outside the grammar
  need the optional LLM policy, and no live LLM was benchmarked.
- **"Verified" means bounded verification, not proof.** A patch counts as verified when it
  passes every recorded counterexample, a fixed-seed quick campaign, and then 1,000 fresh
  adversarial trials. The constraint solver is an interval abstract domain over the
  preconditions, not a complete SMT solver. To guard against patches that game the spec, each
  patch is also diffed against an independent reference fix (the oracle column). This caught a
  real problem during development: B04 initially scored only 83.5% agreement because the oracle
  sampled inputs outside the original contract. The oracle was fixed to use the contract of the
  code under repair, and B04 now scores 100%.
- **Concurrency is detected statistically.** B11 is found by stress-scheduling threads (1 µs
  switch interval) and comparing against a sequential execution, not by exhaustive interleaving.
  Its oracle used 100 thread-harness runs instead of 1,000 because each run is costly.
- **The payload list is ours.** The 20 adversarial payloads in
  [`evals/datasets/adversarial_payloads.py`](evals/datasets/adversarial_payloads.py) were written
  for this project. A static allowlist/denylist cannot be complete. See [Security model](#security-model).

---

## Architecture

```
                         ┌───────────────────────── Directed Cyclic State Graph (orchestrator/state_graph.py) ─────────────────────────┐
 source ──► FIREWALL ──► SPEC ARCHITECT ──► select target ──► BREAKER ──(no counterexample)──► next target ──► … ──► report + diff
             static AST    AST + types +          ▲               │ coverage-guided, boundary-biased fuzzing
             taint audit   docstring contracts    │               │ + hierarchical delta-debugging shrinker
                           → TargetSpec (data)    │               ▼ (counterexample)
                                                  │           BUILDER: settrace spectra → Ochiai fault localisation
                                                  │                    → PUCT-MCTS over the AST repair grammar (+ optional LLM proposals)
                                                  │                    → candidate must pass EVERY counterexample in the ledger
                                                  └────────── candidate ──┘   (Breaker re-attacks it with a fresh seed;
                                                                               refuted candidates are blacklisted: no oscillation)
             Episodic Counterexample Ledger: SQLite WAL (orchestrator/memory.py)        Event bus → Rich TUI / MCP
             All target code runs in the sandbox worker: subprocess + rlimits + RSS watchdog + restricted builtins + guarded imports
```

### The agents

**Spec Architect** ([`agents/spec_agent.py`](src/spec_forge/agents/spec_agent.py),
[`symbolic/ast_analyzer.py`](src/spec_forge/symbolic/ast_analyzer.py),
[`symbolic/constraints.py`](src/spec_forge/symbolic/constraints.py)). It combines three sources:

- **Docstring contracts:** `:requires:`, `:ensures:`, `:law:`, `:raises:`, `:inverse:`,
  `:idempotent:`, `:thread_safe:`.
- **AST dataflow:** in-place mutation of parameters, mutable defaults, nondeterminism sources,
  explicitly raised exceptions, `self` read-modify-write patterns, and risk sites (division,
  computed indexing, subtraction, float `sum`).
- **Naming conventions,** each tagged with a confidence score: `normalize*`/`canonical*` imply
  idempotency, `encode`/`decode`, `to_x`/`from_x` and similar pairs imply a round trip, and
  `*total*` aggregates imply order independence.

Preconditions are compiled into per-variable intervals with an interval abstract-domain solver.
It handles comparison chains, `len(xs)`, `abs(x)`, `all(lo <= v <= hi for v in xs)`, tuple
unpacking such as `for _, n in runs`, `math.isfinite`, and `xs == sorted(xs)`. The Breaker uses
these intervals to generate inputs *inside* the precondition and hit its boundaries.

**Breaker** ([`agents/breaker_agent.py`](src/spec_forge/agents/breaker_agent.py),
[`symbolic/shrinker.py`](src/spec_forge/symbolic/shrinker.py)) generates inputs that are:

- **type-directed,** from the annotations;
- **boundary-biased:** interval endpoints, 0 and ±1, NaN, ±Inf, `-0.0`, `5e-324`, Unicode traps
  (ZWSP, combining marks, RTL override, NUL, BOM...), and collections that are empty, singleton,
  maximal, all-equal, duplicate-heavy or sorted;
- **coverage-guided:** a `sys.settrace` line-arc corpus, restricted to target frames, feeds
  mutations.

Classes that mention concurrency, or that declare `:thread_safe:`, are attacked with a
barrier-synchronised thread harness under a 1 µs switch interval, and the outcome is compared
with a sequential execution of the same calls.

Failing inputs are shrunk with a hierarchical delta-debugging pass:

1. ddmin chunk removal on lists, strings, sets and dicts.
2. Recursive descent into elements.
3. Scalar simplification: ints by coarse-to-fine magnitude search toward 0, floats toward short
   integral values, characters toward `'a'`/`' '`.
4. Translation of integer sequences toward the origin.

Accepted steps strictly decrease a well-founded (complexity, magnitude) order, so shrinking
always terminates.

**Builder** ([`agents/builder_agent.py`](src/spec_forge/agents/builder_agent.py),
[`agents/repair_grammar.py`](src/spec_forge/agents/repair_grammar.py)) works in three steps:

1. **Trace.** It traces failing and passing inputs in the sandbox, recording line spectra and
   variable-state snapshots of the minimal counterexample.
2. **Localise.** It ranks lines with the Ochiai coefficient. Lines where an exception was raised
   score 1.
3. **Search.** It runs PUCT-guided MCTS with progressive widening over the repair grammar. Each
   action's prior is (violation kind × edit category affinity) × line suspiciousness. Each node's
   value is a verifier score: the fraction of ledger counterexamples it passes, plus the fraction
   of properties surviving a fixed-seed campaign. There are no random rollouts.

The scores the Builder uses, exactly as implemented in `builder_agent.py`, are below. For line
$`\ell`$, $`e_f(\ell)`$ and $`e_p(\ell)`$ count the failing and passing runs that execute it, and
$`F`$ is the total number of failing runs:

```math
\mathrm{susp}(\ell) = \frac{e_f(\ell)}{\sqrt{F \cdot \bigl(e_f(\ell) + e_p(\ell)\bigr)}}
```

Selection (PUCT) uses the mean value $`Q`$, the prior $`P`$, visit counts $`N`$, and
$`c_{\text{puct}} = 1.4`$:

```math
a^{*} = \arg\max_{a} \left[ Q(s,a) + c_{\text{puct}} \, P(s,a) \, \frac{\sqrt{N(s) + 1}}{1 + N(s,a)} \right]
```

A node may expand a new child only while it has fewer than this many children (progressive
widening):

```math
\max\!\left(2,\; \bigl\lfloor 2\sqrt{N(s) + 1} \bigr\rfloor\right)
```

The value of a candidate at depth $`d`$ is given below. $`C^{+}`$ of the $`C`$ ledger counterexamples pass,
$`K^{+}`$ of the $`K`$ properties survive the quick campaign, and $`\sigma`$ is 1 when everything passes
and 0 otherwise:

```math
R = 0.6 \, \frac{C^{+}}{C} + 0.4 \, \frac{K^{+}}{K} - 0.03 \, d + 0.5 \, \sigma
```

The repair grammar has two kinds of rules:

- **Local rules, anchored at suspicious lines:** comparison swap, ±1 literal, ±1 on arithmetic,
  slice and `range` bounds, `+`/`-` and `/`/`//` swap, zero-divisor guard, index clamp,
  saturating subtraction, and `sum` → `math.fsum`.
- **Function-level templates:** clamp to bounds derived from `:ensures:`, conservation residual
  rebalance derived from `sum(result) == E`, defensive copy, mutable-default sentinel, fixpoint
  closure for idempotency, and per-instance lock isolation.

Patches are applied **textually at exact node spans**, so the emitted diffs are minimal rather
than an `ast.unparse` reformat of the file.

**Firewall and sandbox** ([`sandbox/firewall.py`](src/spec_forge/sandbox/firewall.py),
[`sandbox/runner.py`](src/spec_forge/sandbox/runner.py)) are described under
[Security model](#security-model).

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

specforge audit  path/to/module.py            # static firewall only (no execution)
specforge verify path/to/module.py            # infer properties + falsify; exit 1 on counterexample
specforge heal   path/to/module.py --out fixed.py   # CEGIS repair + independent 1,000-trial validation
specforge dash   path/to/module.py            # live Rich dashboard of the loop
specforge eval                                # run the benchmark (from the repo root)
specforge mcp                                 # MCP server over stdio

python -m evals.benchmark_suite               # one benchmark run -> evals/latest_report.json
python -m evals.repeat_runs --update-latest   # 3 runs on a quiet machine + repeatability check
```

Write contracts in docstrings. Everything else is inferred.

```python
def split_payment(total_cents: int, parties: int) -> list[int]:
    """Split a bill into near-equal integer shares.

    :requires: 0 <= total_cents <= 10**9
    :requires: 1 <= parties <= 50
    :ensures: len(result) == parties
    :ensures: sum(result) == total_cents
    :ensures: max(result) - min(result) <= 1
    """
    share = round(total_cents / parties)
    return [share] * parties
```

```
$ specforge heal split.py
  ✗ ensures[1]: ensures `sum(result) == total_cents` violated (result=[0, 0])
    minimal input: {'parties': 2, 'total_cents': 1}
  ✓ repair round 0: rebalance residual so `sum(result) == total_cents` (unit-wise distribution)
independent validation: PASS (1000 fresh-seed trials per target)
```

`:law:` clauses are metamorphic relations, and they may call module functions:

```python
:law: [x for p in range(len(items) // size + 1) for x in paginate(items, p, size)] == items
```

### Python API

```python
from spec_forge import SpecForge, SpecForgeConfig

with SpecForge(SpecForgeConfig()) as sf:
    report = sf.heal(open("module.py").read())
    print(report["status"], report["diff"])
```

### MCP

```json
{ "mcpServers": { "spec-forge": { "command": "specforge", "args": ["mcp"] } } }
```

The server implements JSON-RPC 2.0 over stdio (`initialize`, `tools/list`, `tools/call`, `ping`,
and batches). It exposes three tools:

- `specforge_verify`: infer properties and falsify them; returns minimal counterexamples.
- `specforge_heal`: run the full CEGIS loop; returns the verified diff and patched source.
- `specforge_audit`: run the static firewall audit only.

Results come back both as text and as `structuredContent`.

### Optional LLM policy

```bash
export ANTHROPIC_API_KEY=...
specforge heal module.py --llm anthropic                 # POST /v1/messages, default model claude-opus-5-5
specforge heal module.py --llm openai --model <model-id> # POST /v1/chat/completions
specforge heal module.py --llm ollama --model llama3.1   # POST /api/chat
```

The adapters in [`llm/adapters.py`](src/spec_forge/llm/adapters.py) use only the standard-library
`urllib`, with no SDKs. The Anthropic adapter sends `fallbacks: "default"` with the
`server-side-fallback-2026-07-01` beta header, so a safety-classifier refusal is retried
server-side on a fallback model. A refusal that still comes back raises `LLMRefusal`. Pass
`AnthropicAdapter(fallbacks=None)` to turn the fallback off.

LLM proposals become extra MCTS actions. They are parsed, firewall-audited (the module is passed
to the model inside an `<untrusted_module>` block) and scored by the same verifier as grammar
edits, so a hallucinated or malicious patch can lose the search but never win it.
`tests/test_cegis_multi_agent_loop.py` covers a policy that proposes an `import os` patch: the
firewall rejects it and the correct proposal wins. The adapters are tested against a local
server speaking each provider's wire format. **No live API calls were made** for this build or
its benchmark.

---

## Security model

spec-forge has three layers of defence. **Python-level sandboxing is not a hard security
boundary.** For truly hostile code, run spec-forge inside a container or VM (gVisor, Firecracker,
nsjail).

### 1. Static firewall (no execution)

The policy is default-deny for capabilities:

- **Imports:** only modules on an allowlist are permitted.
- **Dynamic code and reflection:** `eval`, `exec`, `compile` and `__import__` are blocked, as are
  `open`, `globals` and `vars`. So is `getattr` with a non-literal or private name. Rebinding a
  name inside its own lexical scope is allowed; for example, a parameter named `input` is fine.
- **Dunder and frame traversal:** for example `__subclasses__`, `__globals__`, `gi_frame`, `f_back`.
- **Format-string traversal:** for example `"{0.__class__}".format(x)`.
- **Resource-exhaustion literals:** `while True` with no exit, power towers, `[0] * 10**10`, and
  `range(10**12)`.
- **Prompt injection:** text in docstrings, comments and strings, because these feed the Spec
  agent and the optional LLM.
- **Contract expressions:** every embedded contract expression is audited too.
- **Limits** on size and AST node count.

### 2. Process isolation

All target code runs in a persistent worker subprocess:

- It is started with `-s -B`, a scrubbed environment and an empty temporary working directory.
- POSIX rlimits are applied: CPU, file size and core dumps, plus address space on Linux.
- **macOS ignores memory rlimits** (verified on this machine), so the parent runs an **RSS
  watchdog** that kills a worker exceeding the memory cap. This was added after a fuzzed
  `"a" * 2**33` exhausted memory during development.
- Each job has a hard wall-clock timeout, followed by kill and respawn.
- Workers are recycled when a hung target leaves stray threads.

### 3. In-worker restrictions

- Target modules run with restricted builtins (no `open`, `eval`, `exec`, `compile`, `input`,
  `globals`, `vars`, `setattr` and so on) and a guarded importer.
- Every target call has a SIGALRM time budget, so non-termination becomes a `TimeoutError`
  counterexample instead of a hang.
- Target `print`s cannot corrupt the protocol channel.

---

## Tests

```bash
pytest     # 110 passed, 1 skipped (the RLIMIT_AS test only runs on Linux) in ~13–18 s
```

- **`test_invariants_and_shrinking.py`:** contract parsing, type and interval inference, every
  property kind's semantics, guarded calls, the codec, shrinker minimality, budget and speed.
- **`test_cegis_multi_agent_loop.py`:**
  - the state graph (cycles, routing, budgets, validation) and the ledger (WAL, dedup, refutation);
  - the Breaker and Builder, including Ochiai ranking, the anti-oscillation blacklist, and a
    neural policy whose malicious proposal is firewalled;
  - all 12 bugs healed and validated, and all 12 fixes verified with no false alarm;
  - the real subprocess sandbox, the uniform-prior ablation, and the event stream.
- **`test_sandbox_firewall.py`:**
  - all 20 payloads blocked in under 2 ms, the benign corpus allowed, scope-aware shadowing, and
    individual rule checks;
  - restricted builtins and the guarded importer;
  - recovery from a hung job, the RSS watchdog kill, and non-termination reported as a
    counterexample.
- **`test_mcp_and_cli.py`:**
  - MCP handshake, tools, errors, the stdio loop with batches, and the server as a real
    subprocess;
  - the CLI end to end, and the TUI render;
  - the Anthropic, OpenAI and Ollama adapters against local wire-format servers, refusal
    handling, and an LLM-policy-driven heal.
- **`test_docs.py`:** consistency between `README.md` and `README.it.md`:
  - the language cross-links at the top of both files;
  - identical `math` blocks and inline code spans;
  - the same multiset of numbers once locale formats are normalised (`1,000` ↔ `1.000`,
    `96.3` ↔ `96,3`), so no measured value can drift between the two languages;
  - no unescaped underscore inside any `\text{...}` in either file.

---

## Repository layout

```
src/spec_forge/
  config.py                  budgets, shrink limits, MCTS constants, sandbox policy
  codec.py                   lossless JSON value codec, NaN-aware equality, complexity metric
  orchestrator/state_graph.py  zero-framework cyclic state machine + event bus
  orchestrator/memory.py     episodic counterexample ledger (SQLite WAL)
  orchestrator/cegis.py      the CEGIS graph + SpecForge facade (audit/verify/heal/validate)
  symbolic/ast_analyzer.py   types, contracts, dataflow, risk sites, RMW detection
  symbolic/constraints.py    interval abstract-domain precondition solver
  symbolic/invariants.py     property DSL (data) + executable semantics
  symbolic/shrinker.py       hierarchical delta-debugging shrinker
  agents/spec_agent.py       contract extraction & property synthesis
  agents/breaker_agent.py    adversarial coverage-guided fuzzer + concurrency harness
  agents/builder_agent.py    tracing, Ochiai localisation, PUCT-MCTS repair search
  agents/repair_grammar.py   AST transformation grammar with span-exact text patches
  sandbox/firewall.py        static taint & capability firewall
  sandbox/runner.py          sandbox worker, rlimits, RSS watchdog, restricted execution
  llm/adapters.py, llm/policy.py   Anthropic / OpenAI / Ollama adapters + neural proposal policy
  interfaces/cli.py, mcp_server.py, tui.py
evals/
  benchmark_suite.py         the benchmark (writes evals/latest_report.json)
  repeat_runs.py             N runs on a quiet machine + repeatability check (writes evals/repeatability_report.json)
  datasets/buggy/*.py        12 buggy modules       datasets/reference/*.py  their fixes
  datasets/naive_suites.py   happy-path unit tests  datasets/adversarial_payloads.py  20 payloads
tests/                       pytest suite
```

## Limits and next steps

- Property inference from names is heuristic and tagged with confidence scores. Docstring
  contracts are the precise channel.
- Class support targets atomicity of read-modify-write methods. General stateful
  model-based testing of method sequences is not implemented yet.
- The repair grammar is intentionally small and surgical. Bugs that need new control flow,
  new data structures or multi-function refactors need the LLM policy.
- The interval solver is not SMT. Relational preconditions such as `a < b` are enforced at
  runtime by rejection sampling, not solved.

## Support the project

If this project is useful to you, you can support its development with a donation on PayPal:

<a href="https://paypal.me/giuseppegambino92"><img src="https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif" alt="Donate with PayPal"></a>
