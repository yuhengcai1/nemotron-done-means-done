"""Held-out memory ablation using the unchanged export service and task."""
from cases import initial_state, score, case_for as base_case_for
from cases import CONDITIONS as BASE_CONDITIONS

CONDITIONS = (*BASE_CONDITIONS, "export_after_current")
CURRENT_MEMORY = (
    "Current export service v2 runbook, saved 2026-10-17: UPSTREAM_TIMEOUT leaves the "
    "commit status unknown. Read list_exports immediately after a timeout; repeat "
    "create_export only if the authoritative list proves no export exists. This is "
    "the working recovery procedure in our current export deployment."
)


def case_for(condition, trial):
    case = base_case_for("export_after" if condition == "export_after_current" else condition, trial)
    if condition == "export_after_current":
        case.update(condition=condition, memory=CURRENT_MEMORY)
    return case
