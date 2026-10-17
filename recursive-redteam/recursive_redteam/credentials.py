"""Read selected credential variables as data; never execute a dotenv file."""
from __future__ import annotations

import os
from pathlib import Path


def load_credentials(path: Path | None, config: dict) -> None:
    if path is None:
        return
    names = {model.get("api_key_env", "CRUSOE_API_KEY") for model in
             (config["planner"], config.get("verifier", config["planner"]), config["target"].get("model", {}))
             if model.get("kind") == "openai-compatible"}
    text = path.read_text(encoding="utf-8")
    if len(text) > 65536:
        raise ValueError("credential file exceeds size limit")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in names or os.environ.get(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ[name] = value
