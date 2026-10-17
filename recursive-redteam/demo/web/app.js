/* Recursive Lab: evidence-led playback. Model inference is never simulated here. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const stages = ["plan", "inspect", "test", "patch", "verify", "memory"];
  const stageNames = { plan: "Planning the repair", inspect: "Inspecting source", test: "Reproducing the failure", patch: "Editing the framework", verify: "Checking the repair", memory: "Saving verified memory" };
  let data = null;
  let events = [];
  let current = 0;
  let playing = false;
  let timer = null;
  let toastTimer = null;
  let apiAvailable = false;
  let storyIndex = 0;
  let view = "story";
  let freshResult = null;
  let freshRecall = null;
  let operationBusy = null;

  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2);
  }
  function set(id, value, fallback = "—") { $(id).textContent = text(value) || fallback; }
  function source(value) {
    if (!value) return "Saved run artifact";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(source).join(" · ");
    return value.path || value.file || value.artifact || value.label || text(value);
  }
  function kind(event) {
    const raw = String(event.kind || event.stage || "plan").toLowerCase();
    if (/memor|remember/.test(raw)) return "memory";
    if (/verif|review|accept/.test(raw)) return "verify";
    if (/patch|edit|repair/.test(raw)) return "patch";
    if (/inspect|read|source/.test(raw)) return "inspect";
    if (/test|execut|repro/.test(raw)) return "test";
    return "plan";
  }
  function getMetric(names) {
    const metrics = data.metrics || [];
    if (Array.isArray(metrics)) {
      return metrics.find((metric) => names.some((name) => (String(metric.id || "") + " " + String(metric.label || "")).toLowerCase().includes(name)));
    }
    for (const name of names) if (metrics[name] !== undefined) return { value: metrics[name] };
    return null;
  }
  function metricValue(metric, fallback) {
    if (!metric) return fallback;
    if (metric.value === null || metric.value === undefined) return "—";
    if (metric.total !== undefined && metric.total !== null) return `${metric.value}/${metric.total}`;
    if (typeof metric.value === "object") {
      if (metric.value.passed !== undefined && metric.value.total !== undefined) return `${metric.value.passed}/${metric.value.total}`;
      return text(metric.value);
    }
    return String(metric.value) + (metric.unit && !["calls", "entries", "tests", "checks"].includes(metric.unit) ? ` ${metric.unit}` : "");
  }
  function receiptStatus(receipt) {
    const status = String(receipt?.status || "pending");
    return status.replaceAll("_", " ").toUpperCase();
  }
  function renderMetrics() {
    const before = getMetric(["before", "tests_before"]);
    const after = getMetric(["after", "tests_after"]);
    const calls = getMetric(["model calls", "model_calls", "inference", "calls"]);
    const memory = getMetric(["memor", "verified memory"]);
    const evidence = data.evidence || {};
    const beforeTests = testReceipt("before"), afterTests = testReceipt("after");
    set("metric-before", beforeTests.failed ?? "—");
    set("metric-after", afterTests.failed ?? "—");
    set("metric-calls", metricValue(calls, data.run?.model_calls ?? "—"));
    set("metric-memory", metricValue(memory, data.run?.memory_entries ?? "—"));
    set("metric-before-caption", beforeTests.total ? `${beforeTests.passed}/${beforeTests.total} acceptance checks pass` : "Awaiting test evidence");
    set("metric-after-caption", afterTests.total ? `${afterTests.passed}/${afterTests.total} acceptance checks pass` : "Awaiting test evidence");
    set("metric-calls-caption", calls?.description || calls?.caption || "Logical calls, including errors");
    set("metric-memory-caption", memory?.description || memory?.caption || "Evidence before recall");
    for (const [id, metric] of [["metric-before", before], ["metric-after", after], ["metric-calls", calls], ["metric-memory", memory]]) {
      if (metric?.source) $(id).title = `Source: ${source(metric.source)}`;
    }
    const memoryCount = Number(memory?.value ?? data.run?.memory_entries ?? 0);
    const memoryKnown = Number.isFinite(memoryCount) && memoryCount > 0;
    set("memory-status", memoryKnown ? "RECORDED" : "PENDING");
    set("memory-evidence", memoryKnown ? `${memoryCount} evidence-backed record${memoryCount === 1 ? "" : "s"}` : "Awaiting validated record");
    set("memory-note", data.memory?.summary || (memoryKnown ? "Source, repair, and validation receipts retained together." : "A memory entry is a claim until its receipts check out."));
  }
  function renderCodeAndTests() {
    const code = data.code || {};
    const dedent = (value) => {
      if (typeof value !== "string") return value;
      const lines = value.split("\n");
      const indents = lines.filter((line) => line.trim()).map((line) => (line.match(/^ */) || [""])[0].length);
      const amount = indents.length ? Math.min(...indents) : 0;
      return lines.map((line) => line.slice(amount)).join("\n");
    };
    set("code-before", dedent(code.before?.text), "No source artifact loaded.");
    set("code-after", dedent(code.after?.text), "No repair artifact loaded.");
    set("code-before-label", code.before?.label, "");
    set("code-after-label", /inconclusive|failed|error/i.test(data.run?.status || "") ? "Nemotron-proposed change" : code.after?.label, "");
    set("code-source", `Source: ${source(code.after?.source || code.before?.source)} · Isolated framework snapshot`);
    const evidence = data.evidence || {};
    ["before", "after"].forEach((phase) => {
      const receipt = evidence[phase] || {};
      set(`${phase}-status`, receiptStatus(receipt));
      const failed = /fail|inconclusive|error|unverified/i.test(String(receipt.status || ""));
      const passed = /pass|verified|success/i.test(String(receipt.status || "")) && !failed && (phase !== "after" || qualifiedRepair());
      $(`${phase}-status`).classList.toggle("repair-failed", failed);
      $(`${phase}-status`).classList.toggle("repair-passed", passed);
      $(`metric-${phase}`).classList.toggle("repair-failed", failed);
      $(`metric-${phase}`).classList.toggle("repair-passed", passed);
      set(`${phase}-summary`, receipt.summary, "Awaiting test evidence.");
      set(`${phase}-output`, receipt.output || receipt.details, "No test receipt loaded.");
    });
    set("tests-source", `Source: ${source(evidence.after?.source || evidence.before?.source)}`);
  }
  function activateTab(tab) {
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      $(`panel-${button.dataset.tab}`).hidden = !active;
    });
  }
  function detailView(event) {
    const raw = event.detail || event.details || event.receipt || event.output;
    let parsed = raw;
    if (typeof raw === "string") { try { parsed = JSON.parse(raw); } catch { return raw; } }
    const result = parsed?.result;
    if (result && typeof result.source === "string") {
      const lines = result.source.split("\n");
      const start = lines.findIndex((line) => /def finish\(/.test(line));
      if (start >= 0) {
        let end = start + 1;
        while (end < lines.length && !/^        def /.test(lines[end])) end += 1;
        return `${result.path || "Framework source"} · function excerpt\n\n${lines.slice(start, end).join("\n").trimEnd()}`;
      }
      return result.source;
    }
    if (result && Array.isArray(result.cases) && result.total !== undefined) {
      const failed = result.cases.filter((item) => item.passed === false);
      const lines = [`${String(result.suite || "Acceptance").toUpperCase()} CHECKS`, `${result.passed} passed · ${result.failed} failed · ${result.total} total`, ""];
      if (failed.length) {
        lines.push("FAILING CASES");
        failed.forEach((item) => lines.push(`✕ ${item.name || item.id}\n  Expected: ${item.expected_status}  ·  Observed: ${item.actual_status}`));
      } else { lines.push("✓ Every recorded acceptance check passed."); }
      if (result.source_sha256) lines.push("", `Source SHA-256: ${result.source_sha256}`);
      lines.push("", "Full execution receipt available in Test receipts.");
      return lines.join("\n");
    }
    return raw;
  }

  function decoded(value) {
    if (typeof value !== "string") return value || {};
    try { return JSON.parse(value); } catch { return {}; }
  }
  function testReceipt(phase) { return decoded(data.evidence?.[phase]?.output); }
  function qualifiedRepair() {
    const after = testReceipt("after");
    return data.showcase?.verified === true && after.total > 0 && after.failed === 0 && after.passed === after.total;
  }
  function patchEvidence() {
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const detail = decoded(events[i].detail);
      if (kind(events[i]) === "patch" && typeof detail.result?.diff === "string" && detail.result.diff.trim()) {
        return { event: events[i], diff: detail.result.diff, action: detail.action || {} };
      }
    }
    return null;
  }
  function renderEvidenceLines(value, isDiff = false) {
    const container = $("story-evidence-content");
    container.replaceChildren();
    String(value || "No evidence is available for this step yet.").split("\n").forEach((line) => {
      const span = document.createElement("span");
      span.textContent = line + "\n";
      if (isDiff && line.startsWith("+") && !line.startsWith("+++")) span.className = "diff-added";
      if (isDiff && line.startsWith("-") && !line.startsWith("---")) span.className = "diff-removed";
      if (isDiff && line.startsWith("@@")) span.className = "diff-location";
      container.appendChild(span);
    });
  }
  function renderStory() {
    if (!data) return;
    const before = testReceipt("before"), after = testReceipt("after");
    const qualified = qualifiedRepair();
    const patch = patchEvidence();
    const memory = data.memory || {};
    const recall = memory.recall || {};
    const replays = data.showcase?.replay_count ?? getMetric(["independent replays"])?.value;
    const stored = qualified && memory.stored === true;
    const recallVerified = stored && recall.verified === true;
    const memoryCount = stored ? (memory.entry_count ?? data.run?.memory_entries ?? 0) : 0;
    let title, summary, number, numberLabel, evidence, evidenceLabel, evidenceStatus, evidenceSource, kicker;
    let good = false;
    $("story-action").hidden = true;
    $("story-live-result").hidden = true;
    if (storyIndex === 0) {
      kicker = "THE STARTING POINT";
      title = "Start with a reproducible failure.";
      summary = data.showcase?.before_summary || (before.total ? `${before.failed} of ${before.total} acceptance checks fail on the isolated framework snapshot. Nemotron receives the source and the test evidence.` : "The saved acceptance checks establish the starting state.");
      number = before.failed ?? "—"; numberLabel = "checks failing before repair";
      const failure = before.cases?.find((item) => item.passed === false);
      evidence = failure ? `FAILING CASE\n${failure.name || failure.id}\n\nExpected: ${failure.expected_status}\nObserved: ${failure.actual_status}\n\n${before.passed} passed · ${before.failed} failed · ${before.total} total` : (data.evidence?.before?.output || "Awaiting the original test receipt.");
      evidenceLabel = "ORIGINAL TEST RECEIPT"; evidenceStatus = before.failed > 0 ? "FAILURE REPRODUCED" : "PENDING";
      evidenceSource = data.evidence?.before?.source;
    } else if (storyIndex === 1) {
      kicker = "WRITTEN BY NEMOTRON";
      title = patch ? "The model writes the change." : "The model proposes a change.";
      summary = data.showcase?.patch_summary || (patch?.event.summary ? "Nemotron's recorded rationale: " + patch.event.summary : "The recorded model response and applied diff identify exactly what Nemotron changed.");
      if (summary.length > 280) summary = summary.slice(0, 277) + "…";
      const added = patch?.diff.split("\n").filter((line) => line.startsWith("+") && !line.startsWith("+++")).length;
      const removed = patch?.diff.split("\n").filter((line) => line.startsWith("-") && !line.startsWith("---")).length;
      number = patch ? `+${added} / −${removed}` : "—"; numberLabel = "lines in the recorded code change";
      const changedLines = patch?.diff.split("\n").filter((line) => (line.startsWith("+") && !line.startsWith("+++")) || (line.startsWith("-") && !line.startsWith("---")));
      if (changedLines?.length) {
        const indent = Math.min(...changedLines.filter((line) => line.slice(1).trim()).map((line) => (line.slice(1).match(/^ */) || [""])[0].length));
        evidence = changedLines.slice(0, 12).map((line) => line[0] + " " + line.slice(1 + indent)).join("\n");
        if (changedLines.length > 12) evidence += "\n… More changed lines in the full trace.";
      } else evidence = "No nonempty model-authored diff has been recorded.";
      evidenceLabel = "MODEL-AUTHORED DIFF · CHANGED LINES"; evidenceStatus = patch ? "RECORDED CHANGE" : "PENDING"; evidenceSource = patch?.event.source;
    } else if (storyIndex === 2) {
      kicker = "INDEPENDENT EXECUTION";
      title = qualified ? "The change passes independent checks." : "Execution decides whether it works.";
      summary = data.showcase?.verification_summary || (qualified ? "The saved source change passes the acceptance suite and independent replays. Run the checks again from this browser." : "A proposed patch earns no success claim until the acceptance checks and independent replays agree.");
      number = after.total ? `${after.passed}/${after.total}` : "—"; numberLabel = "acceptance checks passing after repair";
      const hashes = data.showcase?.source_hashes_valid;
      evidence = `ACCEPTANCE CHECKS   ${after.total ? `${after.passed} passed · ${after.failed} failed` : "Pending"}\nINDEPENDENT REPLAYS ${replays ?? "Not recorded"}\nSOURCE HASHES       ${hashes === true ? "Match" : hashes === false ? "Mismatch" : "See retained verification receipt"}\nQUALIFIED REPAIR    ${qualified ? "Yes" : "Not established"}\n\nSaved results above. The button runs fresh local checks.\nNo new model inference is used for this verification.`;
      evidenceLabel = "RETAINED VERIFICATION RECEIPT"; evidenceStatus = qualified ? "VERIFIED" : "NOT QUALIFIED"; evidenceSource = data.showcase?.verification_source || data.evidence?.after?.source; good = qualified;
      $("story-action").hidden = false;
      $("story-action").disabled = !!operationBusy || !apiAvailable || !data.verification?.available;
      $("story-action").textContent = operationBusy === "verify" ? "Running local checks…" : "Verify again, now ↗";
      if (freshResult) {
        $("story-live-result").hidden = false;
        set("story-live-result", freshResult.summary);
        $("story-live-result").classList.toggle("passed", freshResult.passed);
        $("story-live-result").classList.toggle("failed", !freshResult.passed);
      }
    } else {
      kicker = stored ? "PROVEN WORK, READY TO REUSE" : "QUALIFIED MEMORY ONLY";
      title = recallVerified ? "The next run starts from evidence." : stored ? "The verified change enters memory." : "Only verified repairs enter memory.";
      summary = data.showcase?.memory_summary || recall.summary || (stored ? "The stored source identity and test receipts let the next run check whether this repair still applies." : "This run has no qualified repair to promote. Failed attempts remain in history without becoming trusted memory.");
      number = recallVerified ? (recall.model_calls ?? "—") : memoryCount;
      numberLabel = recallVerified ? "new model calls during verified recall" : "qualified memory entries from this run";
      evidence = `MEMORY STORED      ${stored ? "Yes" : "No"}\nQUALIFIED ENTRIES  ${memoryCount}\nRECALL VERIFIED   ${recallVerified ? "Yes" : "Not established"}\nMODEL CALLS       ${recall.verified === true ? (recall.model_calls ?? "Not recorded") : "No verified recall receipt"}\n\nRecall checks the stored record and source identity.\nOld failures remain inspectable in the run history.`;
      evidenceLabel = recall.verified === true ? "RECORDED MEMORY RECALL" : "MEMORY QUALIFICATION"; evidenceStatus = recallVerified ? "RECALL VERIFIED" : stored ? "STORED" : "NOT PROMOTED"; evidenceSource = recall.source || memory.source || data.provenance?.source; good = recallVerified;
      $("story-action").hidden = false;
      $("story-action").disabled = !!operationBusy || !apiAvailable || !stored;
      $("story-action").textContent = operationBusy === "recall" ? "Checking memory…" : "Recall again, now ↗";
      if (freshRecall) {
        $("story-live-result").hidden = false;
        set("story-live-result", freshRecall.summary);
        $("story-live-result").classList.toggle("passed", freshRecall.passed);
        $("story-live-result").classList.toggle("failed", !freshRecall.passed);
      }
    }
    set("story-kicker", kicker); set("story-title", title); set("story-summary", summary);
    set("story-number", number); set("story-number-label", numberLabel);
    set("story-evidence-label", evidenceLabel); set("story-evidence-status", evidenceStatus);
    set("story-evidence-source", `Source: ${source(evidenceSource)}`);
    set("story-position", `${String(storyIndex + 1).padStart(2, "0")} / 04`);
    set("story-next-cue", ["Next → Inspect Nemotron's actual code change.", "Next → Check whether the change survives execution.", "Next → Inspect the evidence retained for the next run.", "The full trace and prior attempts remain available."][storyIndex]);
    set("story-action-note", storyIndex === 2 ? "Fresh local execution · no new model calls" : storyIndex === 3 ? (recallVerified ? "Recorded recall · use the button for a fresh check" : stored ? "Qualified memory · recall awaits verification" : "No qualified memory · recall unavailable") : "Recorded Nemotron run · preserved tool receipts");
    $("story-next").textContent = storyIndex === 3 ? "Restart story ↺" : "Next evidence →";
    $("story-number").classList.toggle("qualified", good);
    $("story-evidence-status").classList.toggle("qualified", good);
    $("showcase").classList.toggle("showcase-patch", storyIndex === 1);
    renderEvidenceLines(evidence, storyIndex === 1);
    document.querySelectorAll("[data-story]").forEach((button, index) => {
      button.classList.toggle("active", index === storyIndex);
      button.setAttribute("aria-current", index === storyIndex ? "step" : "false");
    });
    if (view === "story") $("next-button").disabled = storyIndex >= 3;
  }
  function setStory(index, pause = true) {
    if (pause) stop();
    storyIndex = Math.max(0, Math.min(index, 3));
    renderStory();
    if (!playing) set("play-label", storyIndex === 3 ? "Replay story" : "Play recorded story");
  }
  function setView(nextView) {
    stop(); view = nextView;
    $("showcase").hidden = view !== "story";
    $("full-trace").hidden = view !== "trace";
    set("view-toggle", view === "story" ? `Open full trace · ${events.length} events ↗` : "Return to 4-step story ↙");
    set("story-guide", view === "story" ? "A 3-minute walkthrough · four pieces of evidence" : "Every recorded decision and tool receipt");
    set("play-label", view === "story" ? "Play recorded story" : "Play recorded run");
    if (view === "story") renderStory(); else renderEvent();
  }

  function renderEvent() {
    if (!events.length) return;
    const event = events[current];
    const stage = kind(event);
    set("event-title", event.title || stageNames[stage]);
    set("event-summary", event.summary, "Actual decision or tool response from the saved run.");
    set("event-detail", detailView(event), "This event has no additional detail in the recorded artifact.");
    set("event-role", stageNames[stage]);
    const actor = event.actor || "Recorded execution";
    set("event-actor", actor.toUpperCase());
    set("agent-avatar", actor.toLowerCase().includes("nemotron") ? "N" : "↳");
    set("event-position", `${String(current + 1).padStart(2, "0")} / ${String(events.length).padStart(2, "0")}${event.call_id ? ` · CALL ${event.call_id}` : ""}`);
    set("event-source", `Source: ${source(event.source)}${event.timestamp ? ` · ${event.timestamp}` : ""}`);
    set("output-label", stage === "patch" ? "MODEL-AUTHORED REPAIR" : stage === "test" || stage === "verify" ? "EXECUTION RECEIPT" : stage === "memory" ? "PERSISTED EVIDENCE" : "DECISION & EVIDENCE");
    set("panel-status", stage.toUpperCase());
    set("stage-counter", `${String(stages.indexOf(stage) + 1).padStart(2, "0")} / 06`);
    document.querySelectorAll("[data-stage]").forEach((button) => {
      const index = stages.indexOf(button.dataset.stage);
      button.classList.toggle("active", button.dataset.stage === stage);
      button.classList.toggle("past", button.dataset.stage !== stage && events.slice(0, current).some((previous) => kind(previous) === button.dataset.stage));
      button.setAttribute("aria-current", button.dataset.stage === stage ? "step" : "false");
      button.disabled = !events.some((item) => kind(item) === button.dataset.stage);
    });
    $("event-dots").querySelectorAll("button").forEach((button, index) => {
      button.classList.toggle("active", index === current);
      button.setAttribute("aria-current", index === current ? "step" : "false");
    });
    $("next-button").disabled = current >= events.length - 1;
  }
  function stop() {
    playing = false;
    clearTimeout(timer);
    set("play-icon", "▶");
    set("play-label", view === "story" ? (storyIndex === 3 ? "Replay story" : "Play recorded story") : (current === events.length - 1 && events.length ? "Replay run" : "Play recorded run"));
    $("play-button").setAttribute("aria-pressed", "false");
  }
  function stepTo(index, pause = true) {
    if (pause) stop();
    current = Math.max(0, Math.min(index, events.length - 1));
    renderEvent();
    if (!playing) set("play-label", current === events.length - 1 ? "Replay run" : "Play recorded run");
  }
  function play() {
    if (!events.length) return;
    if (playing) { stop(); return; }
    if (view === "story" && storyIndex === 3) setStory(0);
    if (view === "trace" && current === events.length - 1) stepTo(0);
    playing = true;
    if (view === "trace") activateTab("trace");
    set("play-icon", "Ⅱ");
    set("play-label", "Pause playback");
    $("play-button").setAttribute("aria-pressed", "true");
    const next = () => {
      if (!playing) return;
      if (view === "story") {
        if (storyIndex >= 3) { stop(); return; }
        setStory(storyIndex + 1, false);
      } else {
        if (current >= events.length - 1) { stop(); return; }
        stepTo(current + 1, false);
      }
      timer = setTimeout(next, view === "story" ? 6500 : 4500);
    };
    timer = setTimeout(next, view === "story" ? 6500 : 4500);
  }
  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 2600);
  }
  function renderRun(payload) {
    data = payload;
    events = Array.isArray(data.events) ? data.events : [];
    const run = data.run || {};
    const pending = !events.length || ["pending", "awaiting", "not_started", "running"].includes(String(run.status).toLowerCase());
    const model = run.model || "Nemotron";
    set("model-short", model.toLowerCase().includes("nemotron") ? "Nemotron 3 Super" : model);
    $("model-short").title = model;
    set("memory-model", "Nemotron on Crusoe");
    set("mission-title", run.mission || "Debug the framework.\nLeave verified evidence.");
    $("mission-title").classList.add("preserve-lines");
    set("recording-label", pending ? (events.length ? "Nemotron run in progress" : "Awaiting recorded run") : "Recorded Nemotron run" + (/inconclusive|failed|error/i.test(run.status || "") ? " · unqualified" : ""));
    set("trace-count", events.length);
    set("run-id", `RUN ${run.id || "—"}`);
    set("run-provenance", "Scaffold: Codex · Runtime: Nemotron on Crusoe · No teachers · Isolated framework snapshot");
    renderMetrics();
    renderCodeAndTests();
    const afterMetric = getMetric(["after", "tests_after"]);
    const failedCount = afterMetric?.total !== undefined && Number.isFinite(Number(afterMetric.value)) ? Number(afterMetric.total) - Number(afterMetric.value) : null;
    const unqualified = /inconclusive|failed|error/i.test(run.status || "");
    if (qualifiedRepair()) {
      set("run-outcome", `Repair verified · ${testReceipt("after").passed}/${testReceipt("after").total} acceptance checks pass · independent replays agree`);
      $("run-outcome").classList.add("qualified");
      $("run-outcome").hidden = false;
    } else if (unqualified) {
      set("run-outcome", "Repair not qualified" + (failedCount !== null ? ` · ${failedCount} check${failedCount === 1 ? "" : "s"} still failing` : " · acceptance checks did not pass"));
      $("run-outcome").hidden = false;
    }
    const provenanceNotes = data.provenance?.notes;
    const priorNote = data.provenance?.prior_attempt_note || (Array.isArray(provenanceNotes) ? provenanceNotes.join("\n\n") : "");
    if (priorNote) {
      set("prior-run-note", priorNote);
      $("run-history").hidden = false;
    }
    $("event-dots").replaceChildren();
    events.forEach((event, index) => {
      const dot = document.createElement("button");
      dot.setAttribute("aria-label", `Event ${index + 1}: ${event.title || stageNames[kind(event)]}`);
      dot.title = `${index + 1}. ${event.title || kind(event)}`;
      dot.addEventListener("click", () => { stepTo(index); activateTab("trace"); });
      $("event-dots").appendChild(dot);
    });
    $("play-button").disabled = !events.length;
    $("next-button").disabled = events.length < 2;
    $("restart-button").disabled = !events.length;
    $("verify-button").disabled = !apiAvailable || !data.verification?.available;
    if (!apiAvailable) $("verify-button").title = "Start the local demo server to execute a fresh verification.";
    else if (!data.verification?.available) $("verify-button").title = "Verification becomes available when the saved run is complete.";
    else $("verify-button").title = data.verification.command || "Run the fixed local verification command";
    document.querySelectorAll("[data-stage]").forEach((button) => { button.disabled = !events.some((event) => kind(event) === button.dataset.stage); });
    if (events.length) renderEvent();
    setView("story");
    if (!events.length) {
      set("event-summary", run.summary || "The timeline will show actual model decisions and tool receipts from Crusoe-hosted inference.");
      set("event-detail", data.provenance?.notes || "No completed self-debug artifact is available. No results are simulated.");
    }
  }
  async function load() {
    let payload;
    if (location.protocol !== "file:") {
      try {
        const response = await fetch("/api/demo", { cache: "no-store", signal: AbortSignal.timeout(8000) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
        apiAvailable = true;
      } catch (error) { console.info("Using saved local demo artifact:", error.message); }
    }
    payload = payload || window.DEMO_DATA || { run: { status: "pending", summary: "Start the local demo server to load the saved Nemotron run." }, events: [] };
    renderRun(payload);
  }
  async function verify() {
    if (!apiAvailable || !data?.verification?.available || operationBusy) return;
    operationBusy = "verify";
    stop();
    const button = $("verify-button");
    button.disabled = true;
    $("story-action").disabled = true;
    $("story-action").textContent = "Running local checks…";
    button.textContent = "Running local checks…";
    $("verification-result").hidden = false;
    set("verification-result-title", "Executing a fresh local verification…");
    set("verification-output", data.verification.command || "Running the saved repair's acceptance checks.");
    set("verification-summary", "Checking source hashes and replaying the acceptance suite…");
    set("verification-result-note", "This runs local tests against the saved repair. It makes no model API calls.");
    try {
      const response = await fetch("/api/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: data.run?.id }), signal: AbortSignal.timeout(120000) });
      const result = await response.json();
      const successful = response.ok && result.ok === true && result.result?.verified !== false && result.result?.source_hashes_valid !== false && (result.exit_code === undefined || result.exit_code === 0);
      set("verification-result-title", successful ? "Verified again. Fresh local execution passed." : "Verification did not pass. Inspect the receipt below.");
      $("verification-result-title").classList.toggle("repair-passed", successful);
      $("verification-result-title").classList.toggle("repair-failed", !successful);
      const checks = result.result || {};
      const summary = [];
      if (checks.source_hashes_valid !== undefined) summary.push(`Source hashes ${checks.source_hashes_valid ? "match" : "do not match"}`);
      if (checks.before?.total !== undefined) summary.push(`Before: ${checks.before.passed}/${checks.before.total} checks pass`);
      if (checks.after?.total !== undefined) summary.push(`After: ${checks.after.passed}/${checks.after.total} checks pass`);
      if (Array.isArray(checks.replays)) summary.push(`${checks.replays.length} independent replays`);
      if (checks.hosted_model_calls !== undefined) summary.push(`${checks.hosted_model_calls} new model calls`);
      set("verification-summary", summary.join(" · "), "Inspect the full execution receipt below.");
      freshResult = { passed: successful, summary: (successful ? "Fresh execution passed. " : "Fresh execution did not pass. ") + summary.join(" · ") };
      if (view === "story") renderStory();
      const blocks = [];
      if (result.stdout) { try { blocks.push(JSON.stringify(JSON.parse(result.stdout), null, 2)); } catch { blocks.push(result.stdout); } }
      if (result.stderr) blocks.push(`STDERR\n${result.stderr}`);
      if (!blocks.length) blocks.push(text(result.result || result));
      if (result.exit_code !== undefined) blocks.push(`PROCESS EXIT CODE: ${result.exit_code}`);
      set("verification-output", blocks.join("\n\n"));
      set("verification-result-note", result.note || "Fresh local verification of recorded artifacts. No new model inference.");
    } catch (error) {
      set("verification-result-title", "Local verification could not complete.");
      set("verification-output", error.message);
      set("verification-summary", "No fresh verification result was received.");
      freshResult = { passed: false, summary: "No fresh verification result was received. Check the local server." };
      if (view === "story") renderStory();
      set("verification-result-note", "The recorded run is unchanged. Check that the local demo server is available.");
    } finally {
      operationBusy = null;
      button.disabled = false;
      button.textContent = "Verify repair locally  ↗";
      renderStory();
    }
  }
  async function recallAgain() {
    if (!apiAvailable || !qualifiedRepair() || data.memory?.stored !== true || operationBusy) return;
    operationBusy = "recall";
    stop();
    $("verify-button").disabled = true;
    renderStory();
    const runId = data.run.id;
    try {
      const response = await fetch("/api/recall", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId }), signal: AbortSignal.timeout(120000) });
      const payload = await response.json();
      const result = payload.result || {};
      const checks = Array.isArray(result.checks) ? result.checks : [];
      const success = response.ok && payload.ok === true && result.reusable === true && result.matched === true && result.chain_valid === true && result.hosted_model_calls === 0 && checks.length === 2 && checks.every((check) => check.total > 0 && check.passed === check.total && check.failed === 0) && (payload.exit_code === undefined || payload.exit_code === 0);
      const validation = checks.map((check) => `${check.passed}/${check.total}`).join(" + ");
      freshRecall = { passed: success, summary: (success ? "Fresh memory recall verified. " : "Fresh memory recall did not qualify. ") + (checks.length ? `${checks.length} local checks (${validation}) · ` : "") + `${result.hosted_model_calls ?? "Unknown"} new model calls` };
      try {
        const updated = await fetch("/api/demo", { cache: "no-store", signal: AbortSignal.timeout(8000) });
        if (updated.ok) {
          const newData = await updated.json();
          if (newData.run?.id === runId) { data = newData; renderMetrics(); renderCodeAndTests(); }
        }
      } catch { console.info("Fresh recall result retained; saved overview could not be refreshed."); }
    } catch (error) {
      freshRecall = { passed: false, summary: "No fresh memory recall result was received. " + error.message };
    } finally {
      operationBusy = null;
      $("verify-button").disabled = !data.verification?.available;
      renderStory();
    }
  }
  $("stage-view-button").addEventListener("click", () => {
    const enabled = document.body.classList.toggle("stage-view");
    $("stage-view-button").setAttribute("aria-pressed", String(enabled));
    $("stage-view-button").textContent = enabled ? "Full view ↙" : "Stage view ↗";
  });
  $("play-button").addEventListener("click", play);
  $("restart-button").addEventListener("click", () => { if (view === "story") setStory(0); else { stepTo(0); activateTab("trace"); } });
  $("next-button").addEventListener("click", () => { if (view === "story") setStory(storyIndex + 1); else { stepTo(current + 1); activateTab("trace"); } });
  $("view-toggle").addEventListener("click", () => setView(view === "story" ? "trace" : "story"));
  $("story-next").addEventListener("click", () => setStory((storyIndex + 1) % 4));
  $("story-action").addEventListener("click", () => storyIndex === 3 ? recallAgain() : verify());
  document.querySelectorAll("[data-story]").forEach((button) => button.addEventListener("click", () => setStory(Number(button.dataset.story))));
  $("verify-button").addEventListener("click", verify);
  $("close-verification").addEventListener("click", () => { $("verification-result").hidden = true; });
  document.querySelectorAll("[data-stage]").forEach((button) => {
    button.addEventListener("click", () => { const index = events.findIndex((event) => kind(event) === button.dataset.stage); if (index >= 0) { stepTo(index); activateTab("trace"); } });
  });
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("[data-tab]")];
      const index = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      activateTab(tabs[next].dataset.tab);
      tabs[next].focus();
    });
  });
  $("copy-button").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("event-detail").textContent); toast("Evidence copied"); }
    catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($("event-detail")); selection.removeAllRanges(); selection.addRange(range); toast("Evidence selected — press ⌘C / Ctrl+C to copy"); }
  });
  document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => {
    document.querySelectorAll(".nav-link").forEach((other) => other.classList.toggle("selected", other === link));
  }));
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  if (new URLSearchParams(location.search).get("stage") === "1") {
    document.body.classList.add("stage-view");
    $("stage-view-button").setAttribute("aria-pressed", "true");
    $("stage-view-button").textContent = "Full view ↙";
  }
  load();
})();
