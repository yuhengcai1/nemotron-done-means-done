# Recursive Lab — Architecture

**The model chooses the work. Tools execute it. Verification decides what counts.**

## High-level loop

```mermaid
flowchart LR
    A["DECIDE<br/>Nemotron selects the next action"] --> B["EXECUTE<br/>Native tools and agent sessions"]
    B --> C["VERIFY<br/>Check actual outcomes"]
    C --> D["REMEMBER<br/>Keep evidence and version history"]
    D -->|Feedback: revise, re-plan, continue or stop| A
```

| Nemotron owns | Our framework owns |
|---|---|
| Planning, tool selection, execution and proposed improvements | Execution bounds, evidence capture, outcome checks and version gates |

**Current boundaries:** native team execution has a verified live run. Code investigation uses a separate runner with operator-assisted compatibility review. A successful live N→N+1 improvement has not yet been demonstrated.

## Low-level details

Expand only the part you want to inspect.

<details>
<summary><strong>1. Execution team — planner, executor and verifier</strong></summary>

```mermaid
flowchart TB
    C["Nemotron controller selects a task"] --> L["Load accepted playbook"] --> LEAD["Lead · Native OpenCode coordinator"]
    LEAD --> P["Planner · Read only<br/>Inspect rules and sources; return a plan"]
    LEAD --> E["Executor · Read + write<br/>Recheck state; perform operations"]
    LEAD --> V["Verifier · Read only<br/>Independently inspect rules, data and result"]
    P --> F["Feedback to lead"]
    E --> F
    V --> F
    F -->|Re-delegate when needed| LEAD
    F --> O["Completion claim + actual state<br/>Independent Python oracle"]
```

All roles use Nemotron on Crusoe. These are actual native child sessions, not one response pretending to contain three agents. They access a local MCP / SQLite task environment. The lead can re-delegate after feedback.

A live rehearsal completed in **67.66 seconds**, with all three roles observed and no violations. This is one local case, not a latency guarantee or general benchmark.

</details>

<details>
<summary><strong>2. Verification — model reasoning versus host checks</strong></summary>

```mermaid
flowchart LR
    A["Actual state + tool receipts"] --> B["Verifier agent<br/>Model-based reasoning"]
    A --> C["Host checks<br/>State, history and execution completeness"]
    B --> D["Lead feedback"]
    C --> E["Pass / Fail / Inconclusive"]
```

The verifier agent can be wrong. Its conclusion does not replace the host oracle. Code experiments separately run a reproducer on original and modified source, then rerun the existing regression suite.

A candidate passed 43 tests but failed an additional default-parameter compatibility check. It was rejected and the counterexample returned to the model. Defect validity and compatibility review currently include an operator.

</details>

<details>
<summary><strong>3. Memory and evolution — retaining evidence versus accepting a change</strong></summary>

| Store | Contents | Use |
|---|---|---|
| Raw evidence | Model responses, tool events, native sessions, test receipts, source identities | Inspect and reproduce the actual run |
| Experiment memory | Successes, failures and their conditions | Inform future decisions; not automatically become execution rules |
| Version store | Parent version, candidate playbook, evaluations, active pointer | Load only the accepted version into subsequent tasks |

```mermaid
flowchart LR
    A["Inspect exploration traces"] --> B["Model proposes playbook"] --> C["Fresh paired evaluations"] --> D{"Promotion gate"}
    D -->|Accept| E["Advance active version"] --> F["Next task loads version"] --> G["Verify read receipt and task outcome"]
    D -->|Reject / inconclusive| H["Keep old version + failed evidence"]
```

**Gate:** complete runs, matching skill-read evidence, all candidate tasks pass without violations, and at least one baseline task fails. Evaluation uses equal settings and budgets with separate task worlds. Hidden evaluation traces are not returned to the controller. Loading and succeeding on the next task are separate checks.

**Status:** implemented and offline-tested; live improvement through the full chain is not yet demonstrated. Two fresh instances of one task family do not establish broad generalization.

</details>

<details>
<summary><strong>4. Open-ended code investigation — a separate experimental runner</strong></summary>

```mermaid
flowchart LR
    A["Copy actual source<br/>Preserve baseline"] --> B["Nemotron investigates<br/>No supplied defect or patch"]
    B --> C["Write reproducer + edit code"] --> D["Host reruns tests"] --> E["Compatibility and defect review<br/>Operator-assisted"]
    E -->|Regression / insufficient evidence| F["Retain evidence + return counterexample"] --> B
    E -->|Review passes| G["Retain reviewable candidate"]
```

This coding-agent path does not automatically install repairs into subsequent agents. Connecting repair acceptance, deployment and next-task verification remains unfinished.

</details>

[Full connection diagram](CORE-FLOW-FULL.md) · [Technical architecture](ARCHITECTURE.md) · [Interactive presentation source](architecture.html)
