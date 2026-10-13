"""Simulated transport tests for Jules adapter harness integration."""
import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from swe_af.runtime.application_harness import (
    _REGISTERED_HANDLERS,
    HarnessRequest,
    register_backend,
)
from swe_af.runtime.jules_adapter import (
    JulesAdapter,
    JulesHarnessAgent,
    register_jules,
)


class TestJulesHarnessIntegration(unittest.TestCase):
    def setUp(self):
        self.old_key = os.environ.get("JULES_API_KEY")
        os.environ["JULES_API_KEY"] = "mock-harness-key"

    def tearDown(self):
        if self.old_key is not None:
            os.environ["JULES_API_KEY"] = self.old_key
        else:
            os.environ.pop("JULES_API_KEY", None)

    def test_registration_and_handler_selection(self):
        register_jules()
        self.assertIn("jules", _REGISTERED_HANDLERS)

    @patch("urllib.request.urlopen")
    def test_harness_agent_full_invocation_simulated(self, mock_urlopen):
        # Session creation response
        resp_session = MagicMock()
        resp_session.read.return_value = b'{"name": "sessions/sess-harness", "id": "sess-harness", "state": "QUEUED"}'

        # Status response
        resp_status = MagicMock()
        resp_status.read.return_value = b'{"name": "sessions/sess-harness", "state": "ACTIVE"}'

        # Activities/Result response
        resp_result = MagicMock()
        resp_result.read.return_value = b'{"activities": [{"id": "act-1"}]}'

        # Provide side_effect for sequential urlopen calls
        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=resp_session), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_status), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_result), __exit__=MagicMock()),
        ]

        agent = JulesHarnessAgent()
        result = asyncio.run(agent.harness("Run harness task"))

        self.assertEqual(result.session_id, "sess-harness")
        self.assertEqual(result.status["state"], "ACTIVE")
        self.assertEqual(len(result.activities.get("activities", [])), 1)
        self.assertEqual(mock_urlopen.call_count, 3)

        # First call must be POST sessions
        first_call_req = mock_urlopen.call_args_list[0][0][0]
        self.assertEqual(first_call_req.get_method(), "POST")
        self.assertEqual(first_call_req.full_url, "https://jules.googleapis.com/v1alpha/sessions")
        self.assertEqual(first_call_req.headers.get("X-goog-api-key"), "mock-harness-key")

    @patch("urllib.request.urlopen")
    def test_harness_handler_registered_invoked_via_harness_request(self, mock_urlopen):
        resp_session = MagicMock()
        resp_session.read.return_value = b'{"name": "sessions/sess-1", "id": "sess-1"}'
        resp_status = MagicMock()
        resp_status.read.return_value = b'{"state": "QUEUED"}'
        resp_result = MagicMock()
        resp_result.read.return_value = b'{"activities": []}'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=resp_session), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_status), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_result), __exit__=MagicMock()),
        ]

        register_jules()
        handler = _REGISTERED_HANDLERS["jules"]
        req = HarnessRequest.create("Prompt test", {"require_plan_approval": True})

        dummy_agent = MagicMock()
        result = asyncio.run(handler(dummy_agent, req))

        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(mock_urlopen.call_count, 3)


    @patch("urllib.request.urlopen")
    def test_app_operational_flow_with_jules_backend(self, mock_urlopen):
        import swe_af.app
        from swe_af.runtime.application_harness import dispatch_harness

        resp_session = MagicMock()
        resp_session.read.return_value = b'{"name": "sessions/sess-prod", "id": "sess-prod"}'
        resp_status = MagicMock()
        resp_status.read.return_value = b'{"state": "QUEUED"}'
        resp_result = MagicMock()
        resp_result.read.return_value = b'{"activities": [{"id": "act-init"}]}'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=resp_session), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_status), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_result), __exit__=MagicMock()),
        ]

        old_backend = os.environ.get("SWE_HARNESS_BACKEND")
        os.environ["SWE_HARNESS_BACKEND"] = "jules"
        try:
            result = asyncio.run(
                dispatch_harness(
                    swe_af.app.app,
                    prompt="Operational task execution",
                    require_plan_approval=True,
                )
            )
            self.assertEqual(result.session_id, "sess-prod")
            self.assertEqual(result.status["state"], "QUEUED")
            self.assertEqual(len(result.activities["activities"]), 1)
            self.assertEqual(mock_urlopen.call_count, 3)
        finally:
            if old_backend is not None:
                os.environ["SWE_HARNESS_BACKEND"] = old_backend
            else:
                os.environ.pop("SWE_HARNESS_BACKEND", None)

    @patch("urllib.request.urlopen")
    def test_harness_preserves_created_session_when_get_fails(self, mock_urlopen):
        import io, urllib.error
        from swe_af.runtime.application_harness import dispatch_harness
        import swe_af.app

        resp_session = MagicMock()
        resp_session.read.return_value = b'{"name": "sessions/sess-success", "id": "sess-success"}'

        err_body = io.BytesIO(b'{"error": {"code": 404, "message": "Activities not ready yet"}}')
        http_err = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions/sess-success/activities",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=err_body,
        )

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=resp_session), __exit__=MagicMock()),
            http_err,  # get_status fails
            http_err,  # get_result fails
        ]

        old_backend = os.environ.get("SWE_HARNESS_BACKEND")
        os.environ["SWE_HARNESS_BACKEND"] = "jules"
        try:
            result = asyncio.run(
                dispatch_harness(
                    swe_af.app.app,
                    prompt="Verify session preserved",
                    require_plan_approval=True,
                )
            )
            # Session is preserved despite GET errors
            self.assertEqual(result.session_id, "sess-success")
            self.assertTrue(result.status.get("unconfirmed"))
            self.assertIn("404", result.status.get("error", ""))
            self.assertTrue(result.activities.get("unconfirmed"))
        finally:
            if old_backend is not None:
                os.environ["SWE_HARNESS_BACKEND"] = old_backend
            else:
                os.environ.pop("SWE_HARNESS_BACKEND", None)


    @patch("urllib.request.urlopen")
    def test_audit_persists_session_id_before_get_calls(self, mock_urlopen):
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_file = Path(tmpdir) / "audit.jsonl"
            
            resp_session = MagicMock()
            resp_session.read.return_value = b'{"name": "sessions/sess-audit-1", "id": "sess-audit-1"}'
            resp_status = MagicMock()
            resp_status.read.return_value = b'{"state": "ACTIVE"}'
            resp_result = MagicMock()
            resp_result.read.return_value = b'{"activities": []}'

            call_order = []
            def fake_urlopen(req, *args, **kwargs):
                url = req.full_url
                if "/activities" in url:
                    call_order.append("GET_ACTIVITIES")
                    return MagicMock(__enter__=MagicMock(return_value=resp_result), __exit__=MagicMock())
                elif "sessions/" in url:
                    call_order.append("GET_STATUS")
                    return MagicMock(__enter__=MagicMock(return_value=resp_status), __exit__=MagicMock())
                else:
                    call_order.append("POST_CREATE")
                    # Check that audit log does NOT exist before POST completes
                    self.assertFalse(audit_file.exists())
                    return MagicMock(__enter__=MagicMock(return_value=resp_session), __exit__=MagicMock())

            mock_urlopen.side_effect = fake_urlopen

            agent = JulesHarnessAgent(audit_path=audit_file)
            res = asyncio.run(agent.harness("Audit sequence test prompt"))

            # Order must be creation -> persistence -> get calls
            self.assertEqual(call_order, ["POST_CREATE", "GET_STATUS", "GET_ACTIVITIES"])
            self.assertTrue(audit_file.exists())
            
            lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["session_id"], "sess-audit-1")
            self.assertEqual(record["outcome"], "created")
            # Ensure no credentials or prompt text leak in audit record
            self.assertNotIn("mock-harness-key", lines[0])
            self.assertNotIn("Audit sequence test prompt", lines[0])

    @patch("swe_af.runtime.jules_adapter._persist_session_audit")
    @patch("urllib.request.urlopen")
    def test_audit_failure_does_not_repeat_session_creation(self, mock_urlopen, mock_persist):
        mock_persist.side_effect = RuntimeError("Simulated audit disk write failure")

        resp_session = MagicMock()
        resp_session.read.return_value = b'{"name": "sessions/sess-fail-write", "id": "sess-fail-write"}'
        resp_status = MagicMock()
        resp_status.read.return_value = b'{"state": "QUEUED"}'
        resp_result = MagicMock()
        resp_result.read.return_value = b'{"activities": []}'

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=resp_session), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_status), __exit__=MagicMock()),
            MagicMock(__enter__=MagicMock(return_value=resp_result), __exit__=MagicMock()),
        ]

        agent = JulesHarnessAgent()
        res = asyncio.run(agent.harness("Test audit failure isolation"))

        # Still only 1 POST call was made (no retry/second creation)
        post_calls = [c for c in mock_urlopen.call_args_list if c[0][0].get_method() == "POST"]
        self.assertEqual(len(post_calls), 1)
        # Session id is preserved in memory and warning/failure is surfaced
        self.assertEqual(res.session_id, "sess-fail-write")
        self.assertTrue(res.is_error)
        self.assertEqual(res.failure_type, "audit_persistence_failure")


if __name__ == "__main__":
    unittest.main()
