"""Bounded JSON transport for a trusted, explicitly configured local command.

This is process management, not an OS security sandbox. The configured program
has the user's filesystem/network permissions. Generated input is JSON data and
is never interpolated into a shell command.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .contracts import Candidate, TrialResult


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    # Also stop descendants that inherited pipes after the direct child exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


class CommandTarget:
    """Run one protocol-v1 request per local subprocess.

    ``idempotent=True`` is a declaration by the plugin author that repeating a
    trial_id cannot repeat external side effects. The adapter does not provide
    such idempotency itself. Interrupted non-idempotent work requires review.
    Only explicitly named environment variables are inherited, in addition to a
    minimal PATH/locale. stdout and stderr share a byte limit; stderr is never
    surfaced in errors because third-party programs may print credentials.
    """

    def __init__(self, command: Sequence[str], timeout: float = 60,
                 cwd: str | os.PathLike[str] | None = None, *,
                 idempotent: bool = False,
                 env_allowlist: Sequence[str] = (),
                 description: dict[str, Any] | None = None,
                 max_output_bytes: int = 1_048_576) -> None:
        if (isinstance(command, (str, bytes)) or not command
                or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
                or not command[0]):
            raise ValueError("command must be a nonempty argv sequence of strings")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a positive finite number")
        if type(idempotent) is not bool:
            raise ValueError("idempotent must be a boolean")
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 16_777_216:
            raise ValueError("max_output_bytes must be between 1 and 16777216")
        if (isinstance(env_allowlist, (str, bytes))
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                       for name in env_allowlist)):
            raise ValueError("env_allowlist must contain environment variable names")
        self.command = tuple(command)
        self.timeout = float(timeout)
        self.cwd = str(Path(cwd).resolve()) if cwd is not None else None
        self.idempotent = idempotent
        self.env_allowlist = tuple(env_allowlist)
        self.max_output_bytes = max_output_bytes
        self.description = dict(description or {})

    def describe(self) -> dict[str, Any]:
        fingerprint = hashlib.sha256(json.dumps(self.command).encode()).hexdigest()
        return {
            **self.description,
            "name": "command-target", "protocol_version": 1,
            "executable": Path(self.command[0]).name,
            "command_sha256": fingerprint, "timeout": self.timeout,
            "max_output_bytes": self.max_output_bytes,
            "env_allowlist": list(self.env_allowlist),
            "idempotent": self.idempotent, "resume_safe": self.idempotent,
            "recovery": "idempotent" if self.idempotent else "manual_review",
            "isolation": "trusted local process; not an OS sandbox",
        }

    def evaluate(self, candidate: Candidate | None, *, trial_id: str) -> TrialResult:
        return self._evaluate(candidate, trial_id=trial_id)

    def evaluate_control(self, candidate: Candidate, *, trial_id: str) -> TrialResult:
        return self._evaluate(None, trial_id=trial_id, control_of=candidate)

    def _evaluate(self, candidate: Candidate | None, *, trial_id: str,
                  control_of: Candidate | None = None) -> TrialResult:
        if not isinstance(trial_id, str) or not trial_id or len(trial_id) > 512:
            return TrialResult("error", error="invalid trial_id")
        if candidate is not None and not isinstance(candidate, Candidate):
            return TrialResult("error", error="invalid candidate")
        try:
            request = json.dumps({"protocol_version": 1, "trial_id": trial_id,
                                  "candidate": candidate.to_dict() if candidate else None,
                                  "control_of": control_of.to_dict() if control_of else None},
                                 ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return TrialResult("error", error="request is not valid JSON")
        if len(request) > 1_048_576:
            return TrialResult("error", error="command request exceeds byte limit")
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        environment.update({name: os.environ[name] for name in self.env_allowlist if name in os.environ})
        started = time.monotonic()
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=self.cwd, env=environment,
                                       shell=False, start_new_session=True, close_fds=True)
        except (OSError, ValueError):
            return TrialResult("error", error="could not start configured command")
        stdout = bytearray()
        output_bytes = 0
        failure: str | None = None
        selector = selectors.DefaultSelector()
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        streams = (process.stdin, process.stdout, process.stderr)
        offset = 0
        try:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_WRITE if stream is process.stdin else selectors.EVENT_READ)
            while selector.get_map():
                remaining = self.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    failure = "command timed out"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            offset += os.write(stream.fileno(), request[offset:offset + 65536])
                        except BrokenPipeError:
                            offset = len(request)
                        if offset == len(request):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        output_bytes += len(chunk)
                        if output_bytes > self.max_output_bytes:
                            failure = "command output exceeds byte limit"
                            break
                        if stream is process.stdout:
                            stdout.extend(chunk)
                if failure:
                    break
            if failure is None:
                remaining = self.timeout - (time.monotonic() - started)
                try:
                    process.wait(timeout=max(remaining, 0.001))
                except subprocess.TimeoutExpired:
                    failure = "command timed out"
        except (OSError, ValueError):
            failure = "command transport failed"
        finally:
            selector.close()
            if failure is not None or process.poll() is None:
                _stop_process_group(process)
            for stream in streams:
                if not stream.closed:
                    stream.close()
        adapter_metrics = {"elapsed_seconds": round(time.monotonic() - started, 6),
                           "output_bytes": output_bytes, "exit_code": process.returncode}
        if failure is not None:
            return TrialResult("error", error=failure, metrics={"command_adapter": adapter_metrics})
        if process.returncode != 0:
            return TrialResult("error", error=f"command exited with status {process.returncode}",
                               metrics={"command_adapter": adapter_metrics})
        try:
            data = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_no_constant)
            result = self._parse_result(data)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            return TrialResult("error", error="command returned an invalid TrialResult JSON object",
                               metrics={"command_adapter": adapter_metrics})
        result.metrics["command_adapter"] = adapter_metrics
        return result

    @staticmethod
    def _parse_result(data: Any) -> TrialResult:
        fields = {"status", "evidence", "trace", "metrics", "error"}
        if not isinstance(data, dict) or not set(data) <= fields or "status" not in data:
            raise ValueError("invalid result fields")
        if not isinstance(data["status"], str):
            raise ValueError("invalid status")
        for key in ("evidence", "trace"):
            value = data.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("invalid result collection")
        if not isinstance(data.get("metrics", {}), dict):
            raise ValueError("invalid metrics")
        if data.get("error") is not None and not isinstance(data["error"], str):
            raise ValueError("invalid error")
        # Suppress potentially secret-bearing provider/command error details.
        if data["status"] == "error":
            return TrialResult("error", error="command target reported an error")
        if data.get("error") is not None:
            raise ValueError("successful results cannot have an error")
        return TrialResult(**data)
