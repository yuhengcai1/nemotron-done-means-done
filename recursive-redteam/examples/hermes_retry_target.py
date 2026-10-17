"""Plugin entry point. Uses only the framework-injected model and local worlds."""
from integrations.hermes_retry.target import HermesRetryTarget
from integrations.hermes_retry.scenario import digest, source_hashes
import json


def make_target(*, options, model):
    if set(options) - {"max_steps", "expected_source_sha256"}:
        raise ValueError("unknown Hermes retry option")
    options = dict(options)
    expected = options.pop("expected_source_sha256", None)
    if expected is not None and expected != digest(json.dumps(source_hashes(), sort_keys=True)):
        raise ValueError("Hermes retry plugin sources changed; review and start a new pinned campaign")
    return HermesRetryTarget(model=model, **options)
