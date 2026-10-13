"""Local tests for Jules adapter: no remote calls."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from swe_af.runtime.jules_adapter import JulesAdapter


class TestJulesLocal(unittest.TestCase):
    def test_import_no_external_calls(self):
        # Simply importing or creating JulesAdapter does not make any network calls
        adapter = JulesAdapter(api_key="mock-key")
        self.assertEqual(adapter.base, "https://jules.googleapis.com/v1alpha")
        self.assertEqual(adapter.timeout, 30.0)

    def test_no_key_raises(self):
        old = os.environ.pop("JULES_API_KEY", None)
        try:
            a = JulesAdapter()
            with self.assertRaises(RuntimeError) as ctx:
                a.create_session("test")
            self.assertIn("JULES_API_KEY missing", str(ctx.exception))
        finally:
            if old is not None:
                os.environ["JULES_API_KEY"] = old

    def test_headers_use_x_goog_api_key_and_no_leakage(self):
        adapter = JulesAdapter(api_key="secret-key-12345")
        headers = adapter._headers()
        self.assertIn("x-goog-api-key", headers)
        self.assertEqual(headers["x-goog-api-key"], "secret-key-12345")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers.get("Content-Type"), "application/json")


class TestJulesSimulatedTransport(unittest.TestCase):
    def setUp(self):
        self.adapter = JulesAdapter(api_key="secret-key-12345", timeout=12.5)
        self.adapter.base = "https://jules.googleapis.com/v1alpha"

    @patch("urllib.request.urlopen")
    def test_create_session_payload_url_headers_default_require_plan_approval(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "name": "sessions/sess-123",
            "id": "sess-123",
            "state": "QUEUED"
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.adapter.create_session("Implement feature X")

        self.assertEqual(mock_urlopen.call_count, 1)
        req, kwargs = mock_urlopen.call_args[0][0], mock_urlopen.call_args[1]
        self.assertEqual(kwargs.get("timeout"), 12.5)
        self.assertEqual(req.full_url, "https://jules.googleapis.com/v1alpha/sessions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.headers.get("X-goog-api-key"), "secret-key-12345")
        self.assertEqual(req.headers.get("Content-type"), "application/json")

        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload.get("prompt"), "Implement feature X")
        self.assertEqual(payload.get("requirePlanApproval"), True)
        self.assertEqual(res.get("name"), "sessions/sess-123")
        self.assertEqual(res.get("state"), "QUEUED")

    @patch("urllib.request.urlopen")
    def test_create_session_custom_options(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"name": "sessions/sess-custom"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        self.adapter.create_session(
            "Do task",
            require_plan_approval=False,
            title="Custom Title",
            automationMode="AUTO_CREATE_PR",
        )

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload.get("requirePlanApproval"), False)
        self.assertEqual(payload.get("title"), "Custom Title")
        self.assertEqual(payload.get("automationMode"), "AUTO_CREATE_PR")

    @patch("urllib.request.urlopen")
    def test_get_status_official_endpoint(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "name": "sessions/sess-123",
            "state": "ACTIVE"
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.adapter.get_status("sess-123")

        self.assertEqual(mock_urlopen.call_count, 1)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://jules.googleapis.com/v1alpha/sessions/sess-123")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.headers.get("X-goog-api-key"), "secret-key-12345")
        self.assertEqual(res.get("state"), "ACTIVE")

    @patch("urllib.request.urlopen")
    def test_get_result_official_activities_endpoint(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "activities": [{"id": "act-1", "description": "Plan generated"}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = self.adapter.get_result("sess-123")

        self.assertEqual(mock_urlopen.call_count, 1)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://jules.googleapis.com/v1alpha/sessions/sess-123/activities")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.headers.get("X-goog-api-key"), "secret-key-12345")
        self.assertEqual(len(res.get("activities", [])), 1)

    @patch("urllib.request.urlopen")
    def test_http_error_sanitization_no_key_leakage(self, mock_urlopen):
        error = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = error

        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.create_session("any")

        err_msg = str(ctx.exception)
        self.assertIn("Jules API POST /sessions HTTP error 401", err_msg)
        self.assertNotIn("secret-key-12345", err_msg)
        self.assertNotIn("x-goog-api-key", err_msg.lower())
        # Confirms only 1 call attempt occurred (no automatic retries)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_http_error_json_message_extraction(self, mock_urlopen):
        import io
        error_body = io.BytesIO(b'{"error": {"code": 404, "message": "Session resource not found"}}')
        error = urllib.error.HTTPError(
            url="https://jules.googleapis.com/v1alpha/sessions/sess-123",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=error_body,
        )
        mock_urlopen.side_effect = error

        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.get_status("sess-123")

        err_msg = str(ctx.exception)
        self.assertIn("Jules API GET /sessions/sess-123 HTTP error 404 (Not Found): Session resource not found", err_msg)

    @patch("urllib.request.urlopen")
    def test_timeout_propagates_without_retries(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("Timed out")

        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.create_session("any")

        err_msg = str(ctx.exception)
        self.assertIn("TimeoutError", err_msg)
        self.assertNotIn("secret-key-12345", err_msg)
        # Confirms zero automatic retries
        self.assertEqual(mock_urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
