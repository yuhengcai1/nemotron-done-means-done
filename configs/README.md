# Configuration boundaries

`native-environment.json` contains non-secret model catalogue metadata captured
for the native experiment on 2026-10-17. `lab.py evolve` uses this instead of a
developer-specific historical run path. It is not a fresh availability check.

Credentials come from the root `.env` or `CRUSOE_API_KEY`. `OPENCODE_BIN` is an
exported environment variable, not a value read from that credential file.

The shared evaluation engine keeps its existing validated configuration format:

```sh
python3 lab.py evaluate validate --config recursive-redteam/examples/crusoe-validation.json
```

Other profiles remain under `recursive-redteam/examples/`. We do not silently
merge them with native evolution settings: their model, tool, budget and source
identities define different experiments. New runs may choose new configurations;
resuming existing runs must obey their original implementation's identity rules.
