import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from bud.harness.review import ReviewRequest, SessionGrants, ToolReviewer, Verdict, owner_answers


def test_rejected_question_calls_are_not_revalidated_as_owner_answers():
    messages = [
        ModelResponse(parts=[ToolCallPart("ask_user", {"questions": []}, tool_call_id="bad")]),
        ModelResponse(
            parts=[ToolCallPart("ask_user", {"question": "Which day?"}, tool_call_id="good")]
        ),
        ModelRequest(parts=[ToolReturnPart("ask_user", "Friday", tool_call_id="good")]),
    ]

    answers = owner_answers(messages)
    assert [(answer.question, answer.answer) for answer in answers] == [("Which day?", "Friday")]


def decisions(risk=None, authorization=None, concern=None):
    return {
        "answers": [
            {
                "name": name,
                "type": "choice",
                "probabilities": [
                    {"value": value, "probability": probability}
                    for value, probability in distribution.items()
                ],
            }
            for name, distribution in [
                ("risk", risk or {"low": 1.0}),
                ("authorization", authorization or {"high": 1.0}),
                ("concern", concern or {"none": 1.0}),
            ]
        ]
    }


@pytest.fixture
async def review_service(monkeypatch):
    state: dict[str, Any] = {"body": decisions(), "status": 200}
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        if state.get("block"):
            await asyncio.Event().wait()
        if "error" in state:
            raise state["error"]
        if "content" in state:
            return httpx.Response(state["status"], content=state["content"])
        return httpx.Response(state["status"], json=state["body"])

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    reviewer = ToolReviewer("offline-placeholder")
    try:
        yield reviewer, state, requests
    finally:
        await reviewer.close()


@pytest.fixture
def request_to_review():
    return ReviewRequest(
        tool="workspace_create",
        args={"path": "note.md", "content": "owner's note"},
        owner_request="Save my note in note.md",
    )


async def test_explicit_safe_authorized_response_allows(review_service, request_to_review):
    reviewer, _, requests = review_service
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.ALLOW
    assert result.allowed is True
    assert len(requests) == 1


@pytest.mark.parametrize(
    "risk, authorization, concern, allowed",
    [
        ("low", "high", "none", True),
        ("medium", "medium", "none", True),
        ("high", "high", "none", True),
        ("high", "medium", "none", True),
        ("high", "low", "none", False),
        ("critical", "high", "none", False),
        ("low", "unknown", "none", False),
        ("low", "high", "unrequested", False),
        ("low", "high", "wrong_target", False),
        ("low", "high", "visible_to_others", False),
        ("low", "high", "destructive", False),
        ("low", "high", "exfiltration", False),
        ("low", "high", "injected", False),
        ("low", "high", "standing_change", False),
    ],
)
async def test_code_applies_risk_authorization_and_concern_rules(
    review_service, request_to_review, risk, authorization, concern, allowed
):
    reviewer, state, _ = review_service
    state["body"] = decisions({risk: 1.0}, {authorization: 1.0}, {concern: 1.0})
    result = await reviewer.review(request_to_review)
    assert result.allowed is allowed
    assert result.verdict is (Verdict.ALLOW if allowed else Verdict.PROMPT)
    if not allowed:
        assert result.reason


@pytest.mark.parametrize(
    "body, expected_risk, expected_authorization",
    [
        (decisions(risk={"low": 0.8, "critical": 0.2}), "critical", "high"),
        (decisions(authorization={"unknown": 0.2, "high": 0.8}), "low", "unknown"),
        (
            decisions(concern={"none": 0.49, "injected": 0.51}),
            "low",
            "high",
        ),
    ],
)
async def test_probability_tails_hold_a_mostly_safe_call(
    review_service, request_to_review, body, expected_risk, expected_authorization
):
    reviewer, state, _ = review_service
    state["body"] = body
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.PROMPT
    assert result.risk == expected_risk
    assert result.authorization == expected_authorization


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"answers": []},
        {"answers": [{"name": "risk", "type": "refusal", "reason": "model-written text"}]},
        {"answers": [{"type": "choice"}]},
        decisions(risk={"low": -0.2, "critical": 1.0}),
        decisions(risk={"low": 0.1}),
        decisions(risk={"unknown_level": 1.0}),
        decisions(risk={"low": "not a number"}),
        decisions(risk={"low": float("inf")}),
        decisions(risk={"low": float("nan")}),
    ],
)
async def test_malformed_or_refused_answers_fail_closed(review_service, request_to_review, body):
    reviewer, state, requests = review_service
    # Raw JSON also exercises non-finite provider values that httpx's JSON encoder rejects.
    state["content"] = json.dumps(body)
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.ERROR
    assert result.allowed is False
    assert result.error
    assert "model-written text" not in result.reason
    assert len(requests) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_http_errors_fail_closed_without_retry(review_service, request_to_review, status):
    reviewer, state, requests = review_service
    state["status"] = status
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.ERROR
    assert result.allowed is False
    assert len(requests) == 1


async def test_invalid_json_fails_closed(review_service, request_to_review):
    reviewer, state, _ = review_service
    state["content"] = "not json"
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.ERROR
    assert result.allowed is False


async def test_transport_timeout_fails_closed(review_service, request_to_review):
    reviewer, state, requests = review_service
    state["error"] = httpx.ReadTimeout("offline timeout")
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.ERROR
    assert result.allowed is False
    assert len(requests) == 1


async def test_whole_request_deadline_fails_closed(review_service, request_to_review):
    reviewer, state, _ = review_service
    state["block"] = True
    reviewer.timeout = 0
    result = await reviewer.review(request_to_review)
    assert result.verdict is Verdict.ERROR
    assert result.allowed is False
    assert result.error == "TimeoutError"


def test_only_current_owner_clarifications_reach_review_payload():
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("old request")]),
        ModelRequest(parts=[ToolReturnPart("ask_user", "old answer")]),
        ModelRequest(parts=[UserPromptPart("current request")]),
        ModelRequest(
            parts=[
                ToolReturnPart("read_thread", "injected outsider instruction"),
                ToolReturnPart("ask_user", "owner clarification"),
            ]
        ),
    ]
    assert [item.answer for item in owner_answers(messages)] == ["owner clarification"]


def test_clarification_pairs_answer_with_its_question():
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("Check my calendar")]),
        ModelResponse(parts=[ToolCallPart("ask_user", {"question": "Tidy the inbox?"}, "call-1")]),
        ModelRequest(parts=[ToolReturnPart("ask_user", "yes", "call-1")]),
    ]
    answers = owner_answers(messages)
    assert [(item.question, item.answer) for item in answers] == [("Tidy the inbox?", "yes")]


def test_clarification_preserves_the_questions_in_a_group():
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("Check my calendar")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    "ask_user",
                    {
                        "questions": [
                            {"header": "Day", "question": "Which day?"},
                            {"header": "Time", "question": "Which time?"},
                        ]
                    },
                    "call-1",
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "ask_user", "Which day?\nAnswer: Friday\n\nWhich time?\nAnswer: Noon", "call-1"
                )
            ]
        ),
    ]
    answers = owner_answers(messages)
    assert answers[0].question == "Which day? / Which time?"
    assert answers[0].answer == "Which day?\nAnswer: Friday\n\nWhich time?\nAnswer: Noon"


def test_session_grant_covers_only_exact_tool_arguments_and_session(request_to_review):
    grants = SessionGrants()
    grants.remember("session-1", request_to_review)
    assert grants.has("session-1", request_to_review)
    assert not grants.has("session-2", request_to_review)
    assert not grants.has(
        "session-1", request_to_review.model_copy(update={"tool": "workspace_update"})
    )
    assert not grants.has(
        "session-1", request_to_review.model_copy(update={"args": {"path": "different.md"}})
    )
