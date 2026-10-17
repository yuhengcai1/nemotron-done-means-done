# Execution truth and handoff teaching suite

Entry point: `integrations.teacher_student.handoff.suite:build_suite()`.
The public protocol is frozen in `CONTRACT.json`; work status is in `STATUS.json`.
The unified runner should expose only `describe(split)`, `lesson()` and bounded
JSON probe results to the student. Source files, frozen evidence, test cases and
host-only holdout results must not enter the student's messages or file tools.

The teacher reproduced a **real local oracle API defect**, twice, then repaired
the current plugin. In the frozen pre-fix version, optional
diagnostic metadata can replace a computed execution fact used by `judge()`, so
the same wrong staged state changes from rejected to accepted. This uses actual
project code, not a mock judge. The normal plugin currently constructs safe,
non-colliding metadata; this does not establish a remotely reachable issue or a
Nemotron failure. Source bytes are frozen and hashed for future regression tests.

The lesson teaches how to distinguish execution truth, handoff syntax/schema and
evaluator decisions. It gives prerequisites, exploration steps and non-cases,
without a holdout probe or answer. The student must choose bounded operations,
handoff variations and diagnostic values, then produce a probe that the host can
independently replay. Repeating the training operation sequence is not a novel
holdout discovery.

Suite `status` describes the **evaluator contract**, not workload completion.
An oracle correctly rejecting a malformed or incorrect workload is a passing
negative control. A disagreement with the independent invariant is a violation.
Invalid probes and runtime failures are errors. Every evaluation starts fresh.

There are no additional API requests in this work. Historical model output-format
observations remain separately labeled; the previous one-off formatting failure
and successful rerun are not promoted into a reproduced model defect.

The current plugin fix keeps diagnostic metadata in a nested namespace and
copies only explicit framework routing/provenance fields to the top level.
Computed task, read-precondition and handoff facts cannot be overwritten. The
owned guard demonstrates the same mitigation around the frozen judge. The suite
can test the original or guarded adapter as a
trusted host setting; students cannot select or alter that setting in a probe.

Run from `/Users/caiyuheng/Documents/ChatGPT/hackthon/recursive-redteam`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v tests.test_teaching_handoff tests.test_reasoning_handoff_integration
PYTHONDONTWRITEBYTECODE=1 python3 integrations/teacher_student/handoff/verify.py --out /tmp/handoff-teacher-verification.json
```

`frozen-evidence.json` is immutable discovery evidence, including two fresh
training reproductions, negative control, source hashes, model-not-invoked labels
and reproduction command. `frozen/source-map.json` resolves the original source
references to immutable discovery-time copies. `verification.json` is a later
regression run; after the fix it correctly reports that the current plugin no
longer matches the frozen vulnerable version. These meanings must stay separate.

`verify.py` also runs a **scripted offline student action fixture** that explores
allowed diagnostic fields and submits a fresh temporal probe. It tests discovery,
novelty and host-verification plumbing; it does not measure Nemotron learning.
The real student run remains the main runner's responsibility. A student probe
must pass novelty, reproduce the same invariant violation twice from fresh state,
and have a passing paired control. No student-supplied result is trusted.

The fixed plugin changes its source hash. The example and original three profile
configs have been updated to the new pin; fresh copies are also provided under
`repaired-configs/`. Saved old run configurations correctly fail their source pin;
an unfinished old run must not be resumed with changed code.
These configs concern the earlier direct plugin workflow, not the teacher/student
runner's own model settings. No new inference was requested to produce them.
