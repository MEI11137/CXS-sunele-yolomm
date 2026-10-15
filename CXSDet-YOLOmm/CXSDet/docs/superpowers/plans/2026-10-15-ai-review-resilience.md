# AI Review Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI review request strict JSON, recover once from malformed model output, persist a safe human-review fallback on failure, and report real failures to single-record and batch callers.

**Architecture:** Keep the existing Flask route, review service, and OpenAI-compatible client boundaries. The client enforces the transport-level response contract, the service owns parsing, one parse-only retry, persistence, and redacted diagnostics, and the routes translate typed service failures into safe HTTP and batch results.

**Tech Stack:** Python 3.12.15, Flask 3.1.3, OpenAI Python 1.108.0 Chat Completions, pytest 9.1.1, SQLite, standard-library `logging` and `hashlib`

## Global Constraints

- Preserve `LLMVLClient.review_detection_structured(image_bytes: bytes, detection_results: list) -> str`.
- Use Chat Completions JSON Mode with `response_format={"type": "json_object"}` and keep the prompt's explicit JSON instruction.
- Retry exactly once only after `AIReviewParseError`; do not retry client, network, refusal, truncation, or content-filter errors.
- Persist `needs_human_review` before surfacing a terminal parse or upstream failure.
- Use exactly `AI返回格式异常，需要人工复核` for parse-failure persistence and `AI服务调用失败，需要人工复核` for upstream-failure persistence.
- Never log API keys, image bytes, complete prompts, complete model responses, or detection results.
- Do not change the configured model, API URL, credentials, detection inference, human-review flow, or front-end layout.
- Do not add a runtime dependency.
- Live verification may use only a generated synthetic image; do not send a stored user image to the external model.

---

## File Map

- `web-flask/algo/llm/llm_vl_client.py`: request JSON Mode and reject incomplete or unsafe Chat Completion responses before returning content.
- `web-flask/services/ai_review_service.py`: define typed review errors, parse strictly, retry parse failures once, persist fallback state, and emit redacted diagnostics.
- `web-flask/routes/detection.py`: return actual HTTP 502 for a typed single-record AI review failure.
- `web-flask/services/batch_review_service.py`: convert typed review failures into safe failed items while continuing the batch.
- `web-flask/tests/test_llm_client_contract.py`: client request and response-contract tests.
- `web-flask/tests/test_ai_review_service.py`: parser, retry, persistence, call-count, and log-redaction tests.
- `web-flask/tests/test_ai_review_routes.py`: single-route HTTP status and response-body contract tests.
- `web-flask/tests/test_batch_review_service.py`: per-item typed failure and continuation test.

### Task 1: Enforce the Structured Chat Completion Contract

**Files:**
- Modify: `web-flask/tests/test_llm_client_contract.py:1-58`
- Modify: `web-flask/algo/llm/llm_vl_client.py:20-90`

**Interfaces:**
- Consumes: `OpenAI.chat.completions.create(**kwargs)` and the existing client configuration dictionary.
- Produces: `LLMResponseError(RuntimeError)` and the unchanged `LLMVLClient.review_detection_structured(image_bytes: bytes, detection_results: list) -> str` method.

- [ ] **Step 1: Add failing tests for JSON Mode and normal content**

Update the fake choice to expose a successful finish reason and assert the exact JSON Mode argument:

```python
def test_structured_review_requests_image_and_json_contract(monkeypatch):
    import algo.llm.llm_vl_client as module
    from algo.llm.config import DEFAULT_VL_MODEL

    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = type(
                "Message",
                (),
                {"content": '{"status":"consistent","summary":"ok"}', "refusal": None},
            )()
            choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
            return type("Completion", (), {"choices": [choice]})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": Completions()})()

    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)
    client = module.LLMVLClient(DEFAULT_VL_MODEL)
    response = client.review_detection_structured(
        _image_bytes(),
        [{"class_name": "crack", "confidence": 0.8, "bbox": [1, 1, 3, 3]}],
    )

    assert '"status":"consistent"' in response
    assert captured["response_format"] == {"type": "json_object"}
    content = captured["messages"][0]["content"]
    assert any(item["type"] == "image_url" for item in content)
    assert "JSON" in content[0]["text"]
    assert "possible_false_positive" in content[0]["text"]
```

- [ ] **Step 2: Add failing tests for every rejected response shape**

Add `pytest`, a constructor that bypasses the real network client, and a parameterized contract test:

```python
import pytest


def _client_with_completion(completion):
    from algo.llm.llm_vl_client import LLMVLClient

    class Completions:
        def create(self, **kwargs):
            return completion

    client = object.__new__(LLMVLClient)
    client.config = {"model": "vision-test"}
    client.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    return client


@pytest.mark.parametrize(
    ("completion", "reason"),
    [
        (type("Completion", (), {"choices": []})(), "没有返回候选结果"),
        (
            type(
                "Completion",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "length",
                                "message": type("Message", (), {"content": "{}", "refusal": None})(),
                            },
                        )()
                    ]
                },
            )(),
            "响应被截断",
        ),
        (
            type(
                "Completion",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "content_filter",
                                "message": type("Message", (), {"content": "{}", "refusal": None})(),
                            },
                        )()
                    ]
                },
            )(),
            "内容过滤",
        ),
        (
            type(
                "Completion",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type(
                                    "Message",
                                    (),
                                    {"content": "{}", "refusal": "cannot comply"},
                                )(),
                            },
                        )()
                    ]
                },
            )(),
            "拒绝处理",
        ),
        (
            type(
                "Completion",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type("Message", (), {"content": "   ", "refusal": None})(),
                            },
                        )()
                    ]
                },
            )(),
            "空内容",
        ),
    ],
)
def test_structured_review_rejects_incomplete_responses(completion, reason):
    from algo.llm.llm_vl_client import LLMResponseError

    client = _client_with_completion(completion)
    with pytest.raises(LLMResponseError, match=reason):
        client.review_detection_structured(_image_bytes(), [])
```

- [ ] **Step 3: Run the client tests and verify the new assertions fail**

Run from `web-flask`:

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_llm_client_contract.py -q
```

Expected: failures because `response_format` and `LLMResponseError` do not exist yet.

- [ ] **Step 4: Add safe client response validation and JSON Mode**

Add the exception near the imports and replace the tail of `review_detection_structured` with this implementation:

```python
class LLMResponseError(RuntimeError):
    """Raised when a structured completion cannot safely be consumed."""


class LLMVLClient:
    # Existing constructor and prompt stay unchanged.

    def review_detection_structured(self, image_bytes: bytes, detection_results: list) -> str:
        # Keep the existing prompt verbatim.
        completion = self.client.chat.completions.create(
            model=self.config["model"],
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": self._image_data_url(image_bytes)}},
                ],
            }],
        )
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise LLMResponseError("模型没有返回候选结果")

        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            raise LLMResponseError("模型响应被截断")
        if finish_reason == "content_filter":
            raise LLMResponseError("模型响应触发内容过滤")

        message = getattr(choice, "message", None)
        if message is None:
            raise LLMResponseError("模型没有返回消息")
        if getattr(message, "refusal", None):
            raise LLMResponseError("模型拒绝处理复核请求")

        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("模型返回空内容")
        return content
```

Do not change `analyze_defects` or `generate_improvement_suggestions`; this contract applies only to structured review.

- [ ] **Step 5: Run the client tests and the existing prompt-coordinate test**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_llm_client_contract.py tests/test_ai_review_service.py::test_vl_review_prompt_defines_box_coordinates_and_zero_based_detection_indexes -q
```

Expected: all selected tests pass. The prompt-coordinate fake intentionally omits `finish_reason` and `refusal`; `getattr(..., None)` keeps it compatible.

- [ ] **Step 6: Commit the client contract**

```powershell
git add web-flask/algo/llm/llm_vl_client.py web-flask/tests/test_llm_client_contract.py
git commit -m "fix: enforce structured AI review responses"
```

### Task 2: Add Parse-Only Retry, Fallback Persistence, and Redacted Logs

**Files:**
- Modify: `web-flask/tests/test_ai_review_service.py:1-117`
- Modify: `web-flask/services/ai_review_service.py:1-115`

**Interfaces:**
- Consumes: `client.review_detection_structured(image_bytes, detections) -> str`, `database.query`, `database.execute`, and `storage_client.get`.
- Produces: `AIReviewError`, `AIReviewParseError`, `AIReviewUpstreamError`, strict `_parse_review_payload(raw: Any) -> AIReviewResult`, and the unchanged `review_detection(record_id, client, database, storage_client) -> AIReviewResult` success interface.

- [ ] **Step 1: Replace the old malformed-output expectation with retry-success coverage**

Replace `test_malformed_ai_output_becomes_human_review_instead_of_ground_truth` with:

```python
def test_parse_failure_retries_once_then_persists_success():
    from services.ai_review_service import review_detection

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.responses = iter([
                "not-json",
                '{"status":"consistent","summary":"second attempt worked"}',
            ])
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections):
            self.calls += 1
            return next(self.responses)

    client = FakeClient()
    database = FakeDatabase(_record())
    result = review_detection("record-1", client, database, FakeStorage())

    assert client.calls == 2
    assert result.status == "consistent"
    assert result.summary == "second attempt worked"
    assert json.loads(database.updated[1][1])["status"] == "consistent"
```

- [ ] **Step 2: Add a failing test for two malformed responses**

```python
def test_repeated_parse_failure_persists_fallback_then_raises():
    import pytest
    from services.ai_review_service import AIReviewParseError, review_detection

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections):
            self.calls += 1
            return "still-not-json"

    client = FakeClient()
    database = FakeDatabase(_record())

    with pytest.raises(AIReviewParseError, match="AI返回格式异常"):
        review_detection("record-1", client, database, FakeStorage())

    persisted = json.loads(database.updated[1][1])
    assert client.calls == 2
    assert persisted["status"] == "needs_human_review"
    assert persisted["summary"] == "AI返回格式异常，需要人工复核"
```

- [ ] **Step 3: Add a failing test for a non-object top-level payload**

```python
def test_parser_rejects_non_object_top_level_payload():
    import pytest
    from services.ai_review_service import AIReviewParseError, _parse_review_payload

    with pytest.raises(AIReviewParseError, match="JSON对象"):
        _parse_review_payload('[{"status":"consistent"}]')
```

- [ ] **Step 4: Add a failing upstream test that proves no retry**

```python
def test_upstream_failure_persists_fallback_without_retry():
    import pytest
    from services.ai_review_service import AIReviewUpstreamError, review_detection

    class UpstreamFailure(RuntimeError):
        status_code = 503
        code = "service_unavailable"

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections):
            self.calls += 1
            raise UpstreamFailure("provider body must stay private")

    client = FakeClient()
    database = FakeDatabase(_record())

    with pytest.raises(AIReviewUpstreamError, match="AI服务调用失败"):
        review_detection("record-1", client, database, FakeStorage())

    persisted = json.loads(database.updated[1][1])
    assert client.calls == 1
    assert persisted["status"] == "needs_human_review"
    assert persisted["summary"] == "AI服务调用失败，需要人工复核"
```

- [ ] **Step 5: Add a failing log-redaction test**

```python
def test_parse_failure_logs_metadata_without_sensitive_values(caplog):
    import logging
    import pytest
    from services.ai_review_service import AIReviewParseError, review_detection

    class FakeClient:
        config = {
            "model": "vision-test",
            "api_key": "SECRET-API-KEY",
        }

        def review_detection_structured(self, image_bytes, detections):
            assert image_bytes == b"image"
            return "SECRET-RAW-RESPONSE"

    database = FakeDatabase(_record())
    with caplog.at_level(logging.WARNING, logger="services.ai_review_service"):
        with pytest.raises(AIReviewParseError):
            review_detection("record-1", FakeClient(), database, FakeStorage())

    assert "record-1" in caplog.text
    assert "vision-test" in caplog.text
    assert "attempt=1" in caplog.text
    assert "attempt=2" in caplog.text
    assert "raw_type=str" in caplog.text
    assert "raw_length=19" in caplog.text
    assert "SECRET-API-KEY" not in caplog.text
    assert "SECRET-RAW-RESPONSE" not in caplog.text
    assert "class_name" not in caplog.text
```

- [ ] **Step 6: Run the new service tests and verify they fail**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_ai_review_service.py -q
```

Expected: failures because the typed errors, retry, strict parser, and redacted logs are not implemented.

- [ ] **Step 7: Add typed service errors and strict parsing**

At the top of `ai_review_service.py`, add imports, logger, errors, and safe messages:

```python
import hashlib
import json
import logging
import re

LOGGER = logging.getLogger(__name__)
PARSE_FAILURE_SUMMARY = "AI返回格式异常，需要人工复核"
UPSTREAM_FAILURE_SUMMARY = "AI服务调用失败，需要人工复核"


class AIReviewError(RuntimeError):
    """Base class for safe, caller-visible AI review failures."""

    public_message = "AI辅助复核失败"


class AIReviewParseError(AIReviewError):
    """Raised when model output cannot become a review object."""

    public_message = PARSE_FAILURE_SUMMARY


class AIReviewUpstreamError(AIReviewError):
    """Raised when the model client or provider call fails."""

    public_message = UPSTREAM_FAILURE_SUMMARY
```

Replace `_parse_review_payload`'s fallback returns with explicit failures and retain the existing field normalization after decoding:

```python
def _parse_review_payload(raw: Any) -> AIReviewResult:
    if isinstance(raw, dict):
        payload = raw
    else:
        text = raw.strip() if isinstance(raw, str) else ""
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                raise AIReviewParseError("AI响应中没有JSON对象")
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise AIReviewParseError("AI响应不是有效JSON对象") from exc

    if not isinstance(payload, dict):
        raise AIReviewParseError("AI响应顶层必须是JSON对象")

    status = str(payload.get("status") or "needs_human_review")
    if status not in ALLOWED_STATUSES:
        status = "needs_human_review"
    summary = str(payload.get("summary") or "AI未提供明确说明，需要人工复核")
    false_positives = [
        int(value)
        for value in payload.get("possible_false_positives", [])
        if isinstance(value, (int, float))
    ]
    missed = payload.get("possible_missed_detections", [])
    regions = payload.get("attention_regions", [])
    return AIReviewResult(
        status=status,
        summary=summary,
        possible_false_positives=false_positives,
        possible_missed_detections=missed if isinstance(missed, list) else [],
        attention_regions=regions if isinstance(regions, list) else [],
    )
```

- [ ] **Step 8: Add focused persistence and diagnostic helpers**

Place these helpers below the parser:

```python
def _persist_review(database, record_id: str, result: AIReviewResult) -> None:
    database.execute(
        """
        UPDATE detection_records
        SET ai_review_status = ?, ai_review_result = ?
        WHERE id = ?
        """,
        (
            result.status,
            json.dumps(result.to_dict(), ensure_ascii=False),
            record_id,
        ),
    )


def _configured_model(client) -> str:
    config = getattr(client, "config", None)
    if isinstance(config, dict) and config.get("model"):
        return str(config["model"])
    return str(getattr(client, "model_name", "unknown"))


def _response_metadata(raw: Any) -> tuple[str, int, str]:
    raw_type = type(raw).__name__
    if isinstance(raw, str):
        encoded = raw.encode("utf-8", errors="replace")
        length = len(raw)
    elif isinstance(raw, bytes):
        encoded = raw
        length = len(raw)
    else:
        encoded = repr(type(raw)).encode("utf-8")
        length = 0
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return raw_type, length, digest


def _public_exception_field(exc: Exception, field_name: str) -> str:
    value = getattr(exc, field_name, None)
    if value is None and field_name == "status_code":
        value = getattr(getattr(exc, "response", None), "status_code", None)
    return str(value)[:64] if isinstance(value, (str, int)) else "unknown"
```

The non-string hash is deliberately based only on its type representation; it never serializes arbitrary provider objects into logs.

- [ ] **Step 9: Implement the parse-only retry and terminal fallback persistence**

Replace the current broad `try/except` and inline update in `review_detection` with:

```python
    model = _configured_model(client)
    for attempt in (1, 2):
        try:
            raw = client.review_detection_structured(image_bytes, detections)
        except Exception as exc:
            LOGGER.warning(
                "ai_review_upstream_failure record_id=%s model=%s exception_type=%s "
                "status_code=%s error_code=%s",
                record_id,
                model,
                type(exc).__name__,
                _public_exception_field(exc, "status_code"),
                _public_exception_field(exc, "code"),
            )
            fallback = AIReviewResult("needs_human_review", UPSTREAM_FAILURE_SUMMARY)
            _persist_review(database, record_id, fallback)
            raise AIReviewUpstreamError(UPSTREAM_FAILURE_SUMMARY) from exc

        try:
            result = _parse_review_payload(raw)
        except AIReviewParseError as exc:
            raw_type, raw_length, response_hash = _response_metadata(raw)
            LOGGER.warning(
                "ai_review_parse_failure record_id=%s model=%s attempt=%s "
                "raw_type=%s raw_length=%s response_hash=%s",
                record_id,
                model,
                attempt,
                raw_type,
                raw_length,
                response_hash,
            )
            if attempt == 1:
                continue
            fallback = AIReviewResult("needs_human_review", PARSE_FAILURE_SUMMARY)
            _persist_review(database, record_id, fallback)
            raise AIReviewParseError(PARSE_FAILURE_SUMMARY) from exc

        _persist_review(database, record_id, result)
        return result

    raise AssertionError("AI review retry loop exhausted unexpectedly")
```

Only `_parse_review_payload` failures reach `continue`. `LLMResponseError`, SDK errors, network errors, storage errors, and database errors must not be silently retried. Keep record lookup and storage access outside the loop.

- [ ] **Step 10: Run service tests**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_ai_review_service.py -q
```

Expected: all service tests pass, including the existing fenced-JSON, health, and prompt-coordinate tests.

- [ ] **Step 11: Commit the service behavior**

```powershell
git add web-flask/services/ai_review_service.py web-flask/tests/test_ai_review_service.py
git commit -m "fix: retry malformed AI review output safely"
```

### Task 3: Surface Typed Failures Through Single and Batch APIs

**Files:**
- Modify: `web-flask/tests/test_ai_review_routes.py:1-96`
- Modify: `web-flask/tests/test_batch_review_service.py:58-109`
- Modify: `web-flask/routes/detection.py:19-20,505-542`
- Modify: `web-flask/services/batch_review_service.py:9-10,64-104`

**Interfaces:**
- Consumes: `AIReviewError.public_message` from Task 2 and existing Flask `error(message, code)` response construction.
- Produces: single-review failures as `(JSON body with code 502, HTTP status 502)` and batch typed failures as safe per-record failed items.

- [ ] **Step 1: Add a failing single-route 502 test**

Append to `test_ai_review_routes.py`:

```python
def test_record_ai_review_returns_real_http_502_for_typed_failure(monkeypatch):
    import routes.detection as detection_route
    from services.ai_review_service import AIReviewParseError

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return {"id": "r1", "user_id": 3}

    def fail_review(*args, **kwargs):
        raise AIReviewParseError("private implementation detail")

    monkeypatch.setattr(detection_route, "db", FakeDb())
    monkeypatch.setattr(detection_route, "is_admin", lambda: False)
    monkeypatch.setattr(detection_route, "LLMVLClient", lambda: object(), raising=False)
    monkeypatch.setattr(detection_route, "review_detection", fail_review, raising=False)

    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/r1/ai-review",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
    )
    body = response.get_json()

    assert response.status_code == 502
    assert body["code"] == 502
    assert body["msg"] == "AI返回格式异常，需要人工复核"
    assert "private implementation detail" not in response.get_data(as_text=True)
```

- [ ] **Step 2: Add a failing batch typed-error continuation test**

Append to `test_batch_review_service.py`:

```python
def test_batch_ai_review_reports_typed_failure_and_continues(tmp_path):
    from services.ai_review_service import AIReviewUpstreamError
    from services.batch_review_service import run_batch_ai_reviews

    db, _ = _database(tmp_path)

    class Review:
        def to_dict(self):
            return {"status": "consistent"}

    def runner(record_id, client, database, storage):
        if record_id == "r1":
            raise AIReviewUpstreamError("provider detail must stay private")
        return Review()

    result = run_batch_ai_reviews(
        ["r1", "r2"],
        user_id=7,
        is_admin_user=False,
        database=db,
        storage_client=object(),
        review_runner=runner,
        client_factory=lambda: object(),
    )

    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["items"][0] == {
        "record_id": "r1",
        "status": "failed",
        "error": "AI服务调用失败，需要人工复核",
    }
    assert result["items"][1]["record_id"] == "r2"
    assert result["items"][1]["status"] == "success"
```

- [ ] **Step 3: Run the route and batch tests and verify failures**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_ai_review_routes.py tests/test_batch_review_service.py -q
```

Expected: the route currently returns HTTP 200 with a body-only error code, and the batch currently exposes only the exception class name.

- [ ] **Step 4: Catch typed errors before the generic route handler**

Change the service import to include `AIReviewError`:

```python
from services.ai_review_service import AIReviewError, check_vl_health, review_detection
```

Change only the exception tail of `review_detection_record`:

```python
    except AIReviewError as exc:
        return error(exc.public_message, 502), 502
    except Exception as exc:
        return error(f"AI辅助复核失败: {type(exc).__name__}")
```

Keep the generic handler for local errors outside the typed model-review path; it must not include `str(exc)`.

- [ ] **Step 5: Return a safe typed failure message from the batch orchestrator**

Import the shared base error:

```python
from services.ai_review_service import AIReviewError
```

Insert this handler before the existing generic `except Exception` in `run_batch_ai_reviews`:

```python
        except AIReviewError as exc:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": exc.public_message,
                }
            )
```

Retain the generic handler for unexpected local failures. It reports only `type(exc).__name__` and continues to the next record.

- [ ] **Step 6: Run route and batch tests**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_ai_review_routes.py tests/test_batch_review_service.py -q
```

Expected: all selected tests pass, including existing authorization, request-size, and per-record summary behavior.

- [ ] **Step 7: Commit API failure visibility**

```powershell
git add web-flask/routes/detection.py web-flask/services/batch_review_service.py web-flask/tests/test_ai_review_routes.py web-flask/tests/test_batch_review_service.py
git commit -m "fix: expose AI review failures safely"
```

### Task 4: Run Regression, Build, Dependency, and Safe Live Verification

**Files:**
- Verify: `web-flask/tests/`
- Verify: `web-vue/`
- Verify: `web-flask/requirements.txt`
- Create temporarily at runtime only: a synthetic in-memory image; do not save or send a user image.

**Interfaces:**
- Consumes: the completed client, service, route, and batch contracts from Tasks 1-3.
- Produces: test, build, dependency, GPU, and synthetic live-call evidence; no production code changes are expected.

- [ ] **Step 1: Run all AI review focused tests together**

From `web-flask`:

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest tests/test_llm_client_contract.py tests/test_ai_review_service.py tests/test_ai_review_routes.py tests/test_batch_review_service.py -q
```

Expected: all focused tests pass with no network access.

- [ ] **Step 2: Run the complete backend test suite**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pytest -q
```

Expected: the entire backend suite passes. If an unrelated pre-existing failure appears, record the exact test and output; do not weaken or delete the test.

- [ ] **Step 3: Verify the locked Python environment**

```powershell
C:\Users\yao35\.conda\envs\py12\python.exe -m pip check
C:\Users\yao35\.conda\envs\py12\python.exe -c "import torch, torchvision; print(torch.__version__, torchvision.__version__, torch.cuda.is_available()); assert torch.__version__ == '2.5.1+cu121'; assert torchvision.__version__ == '0.20.1+cu121'; assert torch.cuda.is_available()"
```

Expected: `pip check` reports no broken requirements, versions are `2.5.1+cu121` and `0.20.1+cu121`, and CUDA availability is `True`.

- [ ] **Step 4: Build the front end from its lockfile**

From `web-vue`, prepend the bundled Node directory only for this shell and run:

```powershell
$env:Path = 'C:\Users\yao35\Desktop\ele-yolomm\.tools\node-v22.2.0-win-x64;' + $env:Path
npm run build
```

Expected: the production build exits with code 0. Existing advisory bundle-size warnings are acceptable; compile errors are not.

- [ ] **Step 5: Run a safe synthetic live JSON Mode smoke check**

Run from `web-flask` only if network access is approved. This creates a plain gray JPEG in memory and does not read the database or storage client:

```powershell
@'
import io
import json
from PIL import Image
from algo.llm.llm_vl_client import LLMVLClient

buffer = io.BytesIO()
Image.new("RGB", (32, 32), "gray").save(buffer, "JPEG")
raw = LLMVLClient().review_detection_structured(buffer.getvalue(), [])
payload = json.loads(raw)
assert isinstance(payload, dict)
assert payload.get("status") in {
    "consistent",
    "possible_false_positive",
    "possible_missed_detection",
    "needs_human_review",
}
print({"ok": True, "status": payload.get("status")})
'@ | C:\Users\yao35\.conda\envs\py12\python.exe -
```

Expected: a safe dictionary containing `ok=True` and an allowed status. Never replace the generated image with a stored detection record or user upload.

- [ ] **Step 6: Inspect the final diff for secrets and unintended files**

From the repository root:

```powershell
git status --short
git diff --check
git diff -- web-flask/algo/llm/llm_vl_client.py web-flask/services/ai_review_service.py web-flask/routes/detection.py web-flask/services/batch_review_service.py web-flask/tests/test_llm_client_contract.py web-flask/tests/test_ai_review_service.py web-flask/tests/test_ai_review_routes.py web-flask/tests/test_batch_review_service.py
```

Expected: no whitespace errors, no API keys, no image bytes, no full prompts or model responses in logs, and no unrelated user files staged.

- [ ] **Step 7: Commit any verification-only correction, or record that no final code commit was needed**

If verification exposed a defect, first add a focused failing regression test, make the minimal correction, rerun the affected test and Steps 1-6, then commit only the touched review files:

```powershell
git add web-flask/algo/llm/llm_vl_client.py web-flask/services/ai_review_service.py web-flask/routes/detection.py web-flask/services/batch_review_service.py web-flask/tests/test_llm_client_contract.py web-flask/tests/test_ai_review_service.py web-flask/tests/test_ai_review_routes.py web-flask/tests/test_batch_review_service.py
git commit -m "test: complete AI review resilience verification"
```

If no correction was needed, do not create an empty commit.
