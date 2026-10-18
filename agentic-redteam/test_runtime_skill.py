"""Offline tests for native learned-skill evidence; never invoke a model."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime import run_target, skill_read_observed


class SkillReadTests(unittest.TestCase):
    path = Path("/tmp/skill-evidence/work/learned_skill.md")

    def export(self, path=None):
        path = str(self.path if path is None else path)
        return {"info": {"id": "ses_test"}, "messages": [{
            "info": {"role": "assistant", "agent": "executor"}, "parts": [{
                "type": "tool", "tool": "read", "state": {
                    "status": "completed", "input": {"filePath": path},
                    "output": f"<path>{path}</path>\n<type>file</type>\n<content>1: Check policy.\n</content>"
                }}]}]}

    def test_successful_absolute_and_relative_native_reads(self):
        for path in (str(self.path), "learned_skill.md", "./learned_skill.md"):
            with self.subTest(path=path):
                self.assertTrue(skill_read_observed(self.export(path), self.path))

    def test_failed_pending_or_running_reads_are_not_proof(self):
        for status in ("error", "pending", "running", None):
            with self.subTest(status=status):
                export = self.export()
                export["messages"][0]["parts"][0]["state"]["status"] = status
                self.assertFalse(skill_read_observed(export, self.path))

    def test_completed_read_needs_successful_file_receipt(self):
        for output in (None, "", "Error: file not found", "I read learned_skill.md",
                       "<path>learned_skill.md</path><type>directory</type><content>x</content>",
                       "<path>other.md</path><type>file</type><content>x</content>"):
            with self.subTest(output=output):
                export = self.export()
                export["messages"][0]["parts"][0]["state"]["output"] = output
                self.assertFalse(skill_read_observed(export, self.path))

    def test_error_field_overrides_completed_status(self):
        export = self.export()
        export["messages"][0]["parts"][0]["state"]["error"] = "Permission denied"
        self.assertFalse(skill_read_observed(export, self.path))

    def test_same_basename_elsewhere_is_not_the_supplied_skill(self):
        for path in ("/tmp/other/learned_skill.md", "../other/learned_skill.md", "learned_skill.md.bak"):
            with self.subTest(path=path):
                self.assertFalse(skill_read_observed(self.export(path), self.path))

    def test_prompt_reasoning_and_write_are_not_reads(self):
        export = self.export()
        export["messages"][0]["parts"][0]["tool"] = "write"
        export["messages"][0]["parts"].extend([
            {"type": "text", "text": "Read learned_skill.md"},
            {"type": "reasoning", "text": "I successfully read learned_skill.md"}])
        self.assertFalse(skill_read_observed(export, self.path))
        export = self.export()
        export["messages"][0]["info"]["role"] = "user"
        self.assertFalse(skill_read_observed(export, self.path))

    def test_malformed_exports_do_not_create_evidence(self):
        for export in (None, [], {}, {"messages": None}, {"messages": [None]},
                       {"messages": [{"info": None, "parts": None}]}):
            with self.subTest(export=export):
                self.assertFalse(skill_read_observed(export, self.path))

    def test_later_success_after_failed_attempt_is_observed(self):
        export = self.export()
        failed = copy.deepcopy(export["messages"][0])
        failed["parts"][0]["state"]["status"] = "error"
        export["messages"].insert(0, failed)
        self.assertTrue(skill_read_observed(export, self.path))

    def test_first_line_of_longer_skill_is_not_complete_read(self):
        export = self.export()
        state = export["messages"][0]["parts"][0]["state"]
        state["input"]["limit"] = 1
        self.assertFalse(skill_read_observed(export, self.path))
        self.assertFalse(skill_read_observed(export, self.path, "Check policy.\nThen verify.\n"))
        # Even without an explicit limit, the original text must fully match.
        del state["input"]["limit"]
        self.assertFalse(skill_read_observed(export, self.path, "Check policy.\nThen verify.\n"))

    def test_complete_bounded_read_matches_all_original_lines(self):
        export = self.export()
        state = export["messages"][0]["parts"][0]["state"]
        state["input"]["limit"] = 2
        state["output"] = state["output"].replace(
            "</content>", "2: Then verify.\n\n(End of file - total 2 lines)\n</content>")
        self.assertTrue(skill_read_observed(export, self.path, "Check policy.\nThen verify.\n"))
        self.assertTrue(skill_read_observed(export, self.path))
        self.assertFalse(skill_read_observed(export, self.path, "Check policy.\nDifferent content.\n"))

    def test_offset_read_is_not_full_skill(self):
        export = self.export()
        export["messages"][0]["parts"][0]["state"]["input"]["offset"] = 2
        self.assertFalse(skill_read_observed(export, self.path, "Check policy."))

    def test_truncated_metadata_or_output_is_rejected(self):
        for metadata in ({"truncated": True}, {"display": {"truncated": True}},
                         {"display": {"totalLines": 2}}, {"display": {"lineStart": 2}}):
            with self.subTest(metadata=metadata):
                export = self.export()
                export["messages"][0]["parts"][0]["state"]["metadata"] = metadata
                self.assertFalse(skill_read_observed(export, self.path, "Check policy."))
        for notice in ("(Output truncated)", "(File has more lines. Use offset=2)"):
            export = self.export()
            state = export["messages"][0]["parts"][0]["state"]
            state["output"] = state["output"].replace("</content>", notice + "\n</content>")
            self.assertFalse(skill_read_observed(export, self.path, "Check policy."))

    def test_numbering_and_eof_must_prove_no_missing_lines(self):
        for body in ("2: Check policy.", "1: Check policy.\n3: Then verify.",
                     "1: Check policy.\n(End of file - total 2 lines)"):
            with self.subTest(body=body):
                export = self.export()
                export["messages"][0]["parts"][0]["state"]["output"] = (
                    f"<path>{self.path}</path><type>file</type><content>{body}</content>")
                self.assertFalse(skill_read_observed(export, self.path))


class SkillManifestTests(unittest.TestCase):
    def test_manifest_saved_before_launch_and_hashes_exact_utf8(self):
        for skill in (None, "", "# Recipe\n\u68c0\u67e5\u5f53\u524d\u89c4\u5219。\n"):
            with self.subTest(skill=skill), tempfile.TemporaryDirectory() as temp:
                out = Path(temp)/"out"
                # Stop before environment/process setup: no provider or CLI calls.
                with patch("runtime.make_world", side_effect=RuntimeError("offline stop")):
                    with self.assertRaisesRegex(RuntimeError, "offline stop"):
                        run_target({}, {}, out, {}, learned_skill=skill)
                manifest = json.loads((out/"loaded-skill.json").read_text())
                self.assertEqual(manifest["provided"], skill is not None)
                expected = hashlib.sha256(skill.encode("utf-8")).hexdigest() if skill is not None else None
                self.assertEqual(manifest["skill_sha256"], expected)


if __name__ == "__main__":
    unittest.main()
