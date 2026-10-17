"""Guard against treating simultaneous query+mutation as evidence-based retry."""
import json
from pathlib import Path
import tempfile
import unittest
from analyze_boundary import analyze


def assistant(call_id,*names):
    return {"role":"assistant","tool_calls":[{"id":call_id,"function":{"name":"tool_call",
        "arguments":json.dumps({"calls":[{"name":"mcp__probe__"+name,"arguments":{"destination":"d"}} for name in names]})}}]}


def response(call_id):
    return {"role":"tool","tool_call_id":call_id,"content":"{}"}


class DecisionTimingTests(unittest.TestCase):
    def check_messages(self,messages):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            fixtures={"case.json":{"memory":"","destination":"d","memory_origin":"empty"},
                "result.json":{},"native-result.json":{"messages":messages},
                "service.json":{"calls":[{"tool":"create_export"},{"tool":"create_export"}]}}
            for name,value in fixtures.items():
                (folder/name).write_text(json.dumps(value))
            return analyze(folder)

    def test_separate_query_response_precedes_retry_decision(self):
        row=self.check_messages([assistant("c1","create_export"),response("c1"),
            assistant("q","list_exports"),response("q"),assistant("c2","create_export"),response("c2")])
        self.assertTrue(row["query_response_available_before_retry"])
        self.assertFalse(row["read_and_retry_same_assistant_message"])

    def test_batched_read_and_retry_is_not_an_observed_read(self):
        row=self.check_messages([assistant("c1","create_export"),response("c1"),
            assistant("batch","list_exports","create_export"),response("batch")])
        self.assertFalse(row["query_response_available_before_retry"])
        self.assertTrue(row["read_and_retry_same_assistant_message"])

    def test_query_without_result_is_not_evidence(self):
        row=self.check_messages([assistant("c1","create_export"),response("c1"),
            assistant("q","list_exports"),assistant("c2","create_export"),response("c2"),response("q")])
        self.assertFalse(row["query_response_available_before_retry"])


if __name__=="__main__":
    unittest.main()
