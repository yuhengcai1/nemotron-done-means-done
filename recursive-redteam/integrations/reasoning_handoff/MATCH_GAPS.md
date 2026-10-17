# Input / tool / timing alignment

## Matches implemented and tested

| Surface | Check |
| --- | --- |
| Legitimate task | TASK and TOOLS compare exactly to AST constants in the historical source snapshot |
| Model-visible conversation | All 33 historical request bodies matched at every step, including messages, tool results, sampling and template parameters |
| Transport normalization | Only explicit `stream=false` versus omission, and empty/null assistant tool-call content are normalized |
| Provider parameters | Three generated configs; actual request envelopes checked; profile/config mismatch rejected at config validation and plugin construction |
| Tools | The same two function schemas, automatic tool choice, read receipts, stage receipts, and one bounded post-stage change |
| Tool outcomes | Each replayed tool-event sequence equals its original `tool-events.json` |
| Final format vs task state | Strict JSON/schema validity and staged-state correctness are recorded separately |
| Historical outcomes | Preserve six correct staged states, one invalid final JSON, its passing replay, all-JSON incomplete, and the final-only JSON check |
| Gateway | Every target call uses the injected `BudgetedModel`; budget exhaustion propagates; calls and tools appear in its journal |
| CLI / engine | Factory exercised; model fixtures pass through real Engine/ModelVerifier; format error becomes inconclusive, not a finding |
| Prior experience | Four bounded, source-hashed external observations reach both coordinator and worker via `describe()` |
| Exposure | Candidate stable/changed conditions distinguished from controls; a notice not yet consumed by a successful model response is not exposure |
| Source identity | Plugin + experience hashes pinned in configs and included in description; mismatches fail closed |

## Gaps and limits retained explicitly

1. **Separate-phase live coverage is absent.** Historical full tasks use inline
   tools. The one historical request with removed tools/JSON mode is only final
   response replay. New full separate-phase execution has local fixture coverage,
   not fresh Nemotron validation. Run the one bounded campaign in README first.
2. **This is a single-session microtask.** The planner's old draft is fixed task
   text; it is not a new planner/executor/verifier deployment. No context-length,
   handoff-depth or sustained-throughput result is claimed.
3. **The default framework config does not match the experiment.** Its response
   cap/temperature may differ and it may omit `low_effort` and
   `force_nonempty_content`. Use this integration's generated target configs.
   Configuration verification does not establish internal provider flag behavior.
4. **Profile switching is per campaign.** `Model.complete(messages, tools,
   json_mode)` has no per-call trusted reasoning override. Adaptive within-trial
   switching would require an explicit shared gateway interface; do not mutate
   the provider or create an unbudgeted client inside this plugin.
5. **Original accounting differs.** The old script had a 49152 cumulative
   completion-token reservation cap. Shared runtime budgets logical model calls;
   generated configs use 40 calls plus per-response caps. No equivalent global
   token or dollar cap is claimed. Historical wall times/reasoning text are not
   recreated by a saved-response replay.
6. **Prompt scope in the shared engine has sandbox-specific language.** Current
   coordinator/verifier system messages mention private markers and document
   injection even for this plugin. Its `describe()` states the non-privacy oracle
   and valid payload precisely, but real-model alignment is untested. Suggested
   shared change: add those instructions only for the relevant target family.
7. **Adaptive feedback loses useful diagnostics.** `Engine._feedback()` supplies
   status and oracle signatures but omits the error reason and separate task/
   format metrics. Suggested bounded shared fields: `error`, `injection_reached`,
   `task_state_correct`, `final_format_valid`, `handoff_schema_valid`, and verified
   profile configuration. The verifier already receives the full trial result.
8. **Provider parsing can hide invalid JSON-mode content.** It raises a scrubbed
   `ProviderError` before target judging; the gateway keeps the diagnostic and
   usage. This remains inconclusive with unknown handoff-format validity, not a
   fabricated task violation. Historical prose-before-JSON used inline mode and
   is preserved exactly through the parser.
9. **Strict schema checks are stronger than the old judge.** Duplicate JSON keys,
   extra fields and wrong primitive types are rejected. None changes the mapped
   outcome of these 33 recorded requests; tests cover the new checks separately.
10. **Framework-wide plugin source discovery remains limited.** Shared
    `source_manifest()` fingerprints the core package only. This adapter's own
    config pins close that gap for these generated configs. A general plugin
    source-manifest hook would make it reusable across integrations.
11. **Historical observations remain external.** `import-evidence` records only
    selected claims and provenance. Full bounded observations are loaded directly
    from this package into `describe()`. They do not refresh old timestamps, bypass
    replay requirements, or seed current verified deduplication entries.

No shared interface changes are required to run the current plugin. The suggested
shared changes above were documented for the integrator rather than implemented
in another owner's files.
