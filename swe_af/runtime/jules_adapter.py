"""Jules remote agent adapter for Stewart-SWE-AF.
Uses JULES_API_KEY from env only; never logs it.
No calls during import/init.
"""
import os, json, urllib.request, urllib.error
from typing import Any, Dict, Optional


class JulesAdapter:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("JULES_API_KEY")
        self.timeout = timeout
        self.base = "https://jules.googleapis.com/v1alpha"

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise RuntimeError("JULES_API_KEY missing")
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _sanitize_error(
        self,
        exc: Exception,
        method: str = "REQUEST",
        path: str = "",
    ) -> RuntimeError:
        """Sanitize network and HTTP errors, logging method, relative path, and API message without secrets."""
        rel_path = path
        if self.base in rel_path:
            rel_path = rel_path.split(self.base, 1)[-1]
        rel_path = rel_path.split("?")[0]  # strip any query string

        if isinstance(exc, urllib.error.HTTPError):
            detail_msg = None
            try:
                raw_body = exc.read().decode("utf-8", errors="ignore")
                if raw_body:
                    body_json = json.loads(raw_body)
                    if isinstance(body_json, dict) and "error" in body_json:
                        err_obj = body_json["error"]
                        if isinstance(err_obj, dict):
                            detail_msg = err_obj.get("message")
                        elif isinstance(err_obj, str):
                            detail_msg = err_obj
            except Exception:
                detail_msg = None

            if detail_msg:
                return RuntimeError(
                    f"Jules API {method} {rel_path} HTTP error {exc.code} ({exc.reason}): {detail_msg}"
                )
            return RuntimeError(
                f"Jules API {method} {rel_path} HTTP error {exc.code}: {exc.reason}"
            )
        if isinstance(exc, urllib.error.URLError):
            return RuntimeError(f"Jules API {method} {rel_path} connection error: {exc.reason}")
        return RuntimeError(f"Jules API {method} {rel_path} error: {type(exc).__name__}")

    def create_session(
        self,
        prompt: str,
        require_plan_approval: bool = True,
        **extra_options: Any,
    ) -> Dict[str, Any]:
        """Creates a Jules session with isolated single invocation and optional requirePlanApproval."""
        if not self.api_key:
            raise RuntimeError("JULES_API_KEY missing")

        payload: Dict[str, Any] = {
            "prompt": prompt,
            "requirePlanApproval": require_plan_approval,
        }
        for k, v in extra_options.items():
            if v is not None:
                payload[k] = v

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/sessions",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise self._sanitize_error(exc, method="POST", path="/sessions") from None
        except Exception as exc:
            raise self._sanitize_error(exc, method="POST", path="/sessions") from None

    def get_status(self, session_id: str) -> Dict[str, Any]:
        """Retrieves session status/state via official GET /sessions/{session_id}."""
        if not self.api_key:
            raise RuntimeError("JULES_API_KEY missing")
        name = session_id if session_id.startswith("sessions/") else f"sessions/{session_id}"
        req_path = f"/{name}"
        req = urllib.request.Request(
            f"{self.base}{req_path}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise self._sanitize_error(exc, method="GET", path=req_path) from None
        except Exception as exc:
            raise self._sanitize_error(exc, method="GET", path=req_path) from None

    def get_result(self, session_id: str) -> Dict[str, Any]:
        """Retrieves session activities/results via official GET /sessions/{session_id}/activities."""
        if not self.api_key:
            raise RuntimeError("JULES_API_KEY missing")
        name = session_id if session_id.startswith("sessions/") else f"sessions/{session_id}"
        req_path = f"/{name}/activities"
        req = urllib.request.Request(
            f"{self.base}{req_path}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise self._sanitize_error(exc, method="GET", path=req_path) from None
        except Exception as exc:
            raise self._sanitize_error(exc, method="GET", path=req_path) from None


# Harness integration: register Jules backend locally
from .application_harness import register_backend, HarnessAgent, HarnessRequest
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace


_AUDIT_LOCK = threading.Lock()
AUDIT_LOG_PATH = Path("logs") / "jules_operational_audit.jsonl"


def _persist_session_audit(session_id: str, outcome: str = "created", log_path: Optional[Path] = None) -> None:
    """Safely persist minimal operational metadata for a created session.

    Logs ONLY UTC timestamp, session identifier, outcome, and schema version.
    Never logs prompts, API keys, headers, or full response bodies.
    """
    path = log_path or AUDIT_LOG_PATH
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "event": "session_created",
        "outcome": outcome,
        "version": "1.0",
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            # Create file with restricted user-only permissions if supported
            if not path.exists():
                path.touch(mode=0o600, exist_ok=True)
                try:
                    os.chmod(path, 0o600)
                except (OSError, NotImplementedError):
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception as exc:
        raise RuntimeError(
            "Jules session created successfully but operational audit persistence failed; "
            "session retained in memory"
        ) from exc


class JulesHarnessAgent:
    def __init__(self, audit_path: Optional[Path] = None):
        self.audit_path = audit_path

    async def harness(self, prompt: str, **options: Any) -> Any:
        adapter = JulesAdapter()
        require_plan_approval = options.get("require_plan_approval", True)
        session = adapter.create_session(prompt, require_plan_approval=require_plan_approval)
        session_id = session.get("id") or session.get("name")

        # Step 1: Guarantee persistence of session identifier immediately after creation
        audit_error = None
        if session_id:
            try:
                _persist_session_audit(str(session_id), outcome="created", log_path=self.audit_path)
            except Exception as exc:
                audit_error = str(exc)

        # Step 2: Query follow-up GET calls only when requested (skip if skip_status_and_result is True)
        skip_queries = options.get("skip_status_and_result", False)
        status: Dict[str, Any] = {}
        result: Dict[str, Any] = {}
        if not skip_queries and session_id:
            try:
                status = adapter.get_status(session_id)
            except Exception as exc:
                status = {"unconfirmed": True, "error": str(exc)}

            try:
                result = adapter.get_result(session_id)
            except Exception as exc:
                result = {"unconfirmed": True, "error": str(exc)}
        else:
            status = {"state": session.get("state") or session.get("status") or "QUEUED", "queried": False}
            result = {"queried": False}

        parsed_data = {
            "session_id": session_id,
            "status": status,
            "result": result,
            "session": session,
        }
        if audit_error:
            parsed_data["audit_warning"] = audit_error

        raw_result_str = json.dumps(parsed_data)

        # Build schema object if a Pydantic schema was passed in options
        schema = options.get("schema")
        parsed_obj = None
        if schema is not None:
            try:
                parsed_obj = schema.model_validate(parsed_data)
            except Exception:
                parsed_obj = None

        return SimpleNamespace(
            result=raw_result_str,
            parsed=parsed_obj or parsed_data,
            is_error=bool(audit_error),
            error_message=audit_error,
            failure_type="audit_persistence_failure" if audit_error else "none",
            session_id=session_id,
            status=status,
            activities=result,
            cost_usd=0.0,
            num_turns=1,
            duration_ms=0,
            messages=[{"role": "user", "content": prompt}],
        )


def _jules_backend_handler(agent: HarnessAgent, req: HarnessRequest) -> Any:
    return JulesHarnessAgent().harness(req.prompt, **req.options)


def register_jules() -> None:
    register_backend("jules", _jules_backend_handler)
