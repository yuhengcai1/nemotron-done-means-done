# OpenHands + Crusoe/Nemotron live probes

On 2026-10-17, **all 12 autonomous recovery trials completed and verified their
goal**. These fixtures did not reproduce autonomous stuck behavior. The 12
separate diagnostic-sampling trials test detector mechanisms, not autonomous
failure. No runtime or detector fix was made.

| Sampling condition | Native outcome across three trials | What the evidence supports |
| --- | --- | --- |
| Identical action and observation | Stopped at 4 calls in all three | Positive detector control works |
| Requested varying assistant text | Two stopped at 4; one reached 7 then provider timeout | The actual ActionEvent thought varied in only the timeout trial; no stable full-length reproduction |
| Only observation timestamp varies | Finished after 10, 11, 10 calls; no detector flag | This comparator gap is reachable in real trajectories; all three falsely reported 12 samples |
| Requested status/logs/config cycle | Finished after 11, 8, 8 calls; no detector flag | All deviated from the requested cycle; all falsely reported 12. Not a clean period-three reproduction |

The autonomous conditions were stable observations, changing timestamps, circular
diagnostic hints, and a healthy-progress control, each repeated three times. All
were solvable using the described repair or natural-progress path. No false stop
occurred in the healthy-progress controls. The provider timeout remains in the
denominator and its original artifacts are retained.

## Target and evidence

- Official `openhands-sdk==1.55.1`, unchanged native Agent, Conversation, events,
  default system prompt, think/finish tools, and stuck detector.
- Actual live Crusoe endpoint `https://api.inference.crusoecloud.com/v1`, model
  `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B`.
- Detector SHA-256
  `3bf25f2d722404398e4744ae188f575f628463732b3606a8da6049fb8ff0039e`, matching
  [the reviewed upstream source](https://github.com/OpenHands/software-agent-sdk/blob/5966c781ed014efc96c028a99bddd4ff78426741/openhands-sdk/openhands/sdk/conversation/stuck_detector.py).
- `experiment-plan.json` was recorded before inference. `runs/summary.json`
  contains all 24 attempts and their exact paths. Each trial retains native
  events, completion logs, config, task, detector observations, and an independent
  service ledger. The two batch directories contain source snapshots.
- `python3 analyze.py` rebuilds the summary. `test_environment.py` verifies fixture
  solvability and the differences between paired conditions; those tests are not
  counted as model trials.

## Reproduction

Use Python 3.12+ with `openhands-sdk==1.55.1`. A single fresh run:

```sh
python run_live.py --phase recovery --condition circular_diagnostics \
  --trial 1 --out runs/new-run --env-file /absolute/path/to/private.env
```

The environment file must contain `CRUSOE_API_KEY`. Do not put it in an artifact.
Each run is bounded to 18 native iterations, 180 seconds, and 4096 output tokens
per response. Runtime-provided zero cost is not evidence of free inference; this
model was not mapped by SDK cost accounting.

## Interpretation limits

This is the real SDK and model with a synthetic local job tool, not the full
shell-enabled OpenHands CLI. Explicit sampling can expose comparator behavior
but cannot establish autonomous infinite loops. Three repetitions are an
exploratory sample. The repeated false completion statements are evidence for a
new completion-verification experiment, not proof that a general solution works.

The provider's `reasoning_content`, tool summary, and native ActionEvent `thought`
are different fields. Varying reasoning or summaries alone did not make the
native comparator's thought field vary in two of the requested-thought trials.
