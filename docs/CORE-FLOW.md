# Recursive Lab — Core Architecture

Nemotron chooses actions. Native tools execute them. Independent checks determine the outcome. Evidence and version history are retained.

```mermaid
flowchart TB
    U["USER GOAL<br/>Execute a task or investigate a real codebase"]

    subgraph A["A · AUTONOMOUS TASK EXECUTION — LIVE VALIDATED"]
      C["Nemotron Controller<br/>Choose tasks, inspect evidence, propose improvements, stop"]
      B["Host Execution Bounds<br/>Controller request ledger · Native runtime limits"]
      L["Load Accepted Playbook<br/>Version identity + content hash"]
      LEAD["Nemotron Lead<br/>Native OpenCode task delegation"]
      P["Planner · Separate Session<br/>Read policy and source data<br/>Return a plan and constraints"]
      E["Executor · Separate Session<br/>Recheck current state<br/>Perform actual operations"]
      V["Verifier · Separate Session<br/>Independently read policy, data and results<br/>Report pass or specific problems"]
      W[("Local Task Environment<br/>MCP tools + SQLite<br/>State and action history")]
      CLAIM["Lead Completion Claim<br/>done / incomplete"]
      O["Independent Python Oracle<br/>Task state · Historical violations<br/>Native execution completeness"]
      R["Evidence-backed Outcome<br/>Pass / Fail / Inconclusive"]
      C --> B --> L --> LEAD
      LEAD -->|Delegate| P
      P -->|Plan| LEAD
      LEAD -->|Delegate| E
      E -->|Execution result| LEAD
      LEAD -->|Delegate| V
      V -->|Verification feedback| LEAD
      P <-->|Read only| W
      E <-->|Read and write| W
      V <-->|Read only| W
      LEAD --> CLAIM --> O
      W --> O --> R
      R -->|Inspect, re-plan, continue or stop| C
    end

    subgraph M["DURABLE EVIDENCE AND MEMORY"]
      T[("Raw Evidence<br/>Model responses · Tool events · Native sessions<br/>Test receipts · Source identities")]
      X[("Experiment Memory<br/>Successful and failed attempts<br/>Conditions and applicability")]
      VS[("Version Store<br/>Parent version · Candidate content<br/>Evaluation evidence · Active version")]
      T --> X
    end

    subgraph N["B · VERSION N+1 — IMPLEMENTED; LIVE IMPROVEMENT NOT YET DEMONSTRATED"]
      I["Nemotron Inspects Exploration Traces"]
      S["Nemotron Writes a Candidate Playbook<br/>Linked to its parent and observed evidence"]
      H["Host Freezes Fresh Evaluation Instances<br/>Held-out traces unavailable to the controller"]
      AB["Paired Baseline / Candidate Execution<br/>Equal configuration and budget · Separate worlds"]
      G{"Promotion Gate<br/>Complete executions + proven skill reads<br/>All candidate tasks pass without violations<br/>At least one baseline task fails"}
      YES["Accept Candidate<br/>Advance active version"]
      NO["Reject or Mark Inconclusive<br/>Keep previous active version and evidence"]
      NEXT["Next Real Task Loads Accepted Version<br/>Verify full-read receipt and content hash"]
      F["Independent Follow-up Check<br/>Record loaded and task passed separately"]
      I --> S --> H --> AB --> G
      G -->|Meets criteria| YES
      G -->|Regression, no gain or insufficient evidence| NO
      YES --> NEXT --> F
    end

    subgraph D["C · OPEN-ENDED CODE INVESTIGATION — SEPARATE RUNNER"]
      CP["Copy Current Real Source<br/>Preserve original code and baseline tests<br/>No planted defect or supplied patch"]
      AG["Nemotron + OpenCode Coding Agent<br/>Inspect code · Select hypotheses · Run experiments"]
      TEST["Model Writes a Reproducer"]
      PATCH["Model Edits the Source Copy"]
      CHECK["Host Reruns Checks<br/>New test on original and modified code<br/>Full existing regression suite"]
      REV["Defect Validity and Compatibility Review<br/>Currently operator-assisted"]
      DQ{"Sufficient Evidence to Accept?"}
      FB["Retain Candidate and Counterexample<br/>Return feedback for correction or withdrawal"]
      HOLD["Retain Reviewable Candidate<br/>No automatic installation into subsequent tasks"]
      CP --> AG --> TEST --> PATCH --> CHECK --> REV --> DQ
      DQ -->|Regression or insufficient evidence| FB --> AG
      DQ -->|Review passes| HOLD
    end

    U -->|Task execution| C
    U -->|Code investigation| CP
    P --> T
    E --> T
    V --> T
    R --> T
    AG --> T
    CHECK --> T
    REV --> T
    X -->|Historical context, not automatic execution rules| C
    C -->|Choose improvement tools| I
    S --> VS
    YES --> VS
    NO --> VS
    VS --> L
    NO -->|Evaluation summary| C
    F -->|Continue working| C
    HOLD -. "NOT CONNECTED: unified repair acceptance, deployment and next-task verification" .-> VS

    classDef model fill:#e7efe8,stroke:#386552,color:#153c31;
    classDef gate fill:#153c31,stroke:#153c31,color:#ffffff;
    classDef pending fill:#f3ecdd,stroke:#a57a43,color:#4d371c;
    class C,LEAD,P,E,V,I,S,AG model;
    class O,G,CHECK,REV gate;
    class HOLD,NEXT,F pending;
```

**Status boundaries:** Path A has a verified live native-team run. Path B is implemented with offline tests, but a live N→N+1 improvement has not been demonstrated. Path C uses a separate coding-agent runner and operator-assisted compatibility review; it does not automatically deploy repairs.

Solid arrows show implemented interactions. The dashed arrow marks an integration that is not connected. Memory retains failures; it does not turn unverified changes into accepted execution rules.
