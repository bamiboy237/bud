import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart

from bud.composio.tools import ask_user as account_ask_user
from bud.core.controls import approval_text
from bud.harness.ask import AskAnswer, answer_from_text, questions_of, render_answers
from bud.harness.ask import ask_user as shared_ask_user
from bud.photon_ui import action_preview
from tests.runner_support import reply, running_bud

QUESTIONS = {
    "questions": [
        {
            "header": "Which Sam",
            "question": "Which Sam do you mean?",
            "options": [{"label": "Sam Lee", "description": "your manager"}, {"label": "Sam Park"}],
        },
        {
            "header": "Day",
            "question": "Which days work?",
            "options": [{"label": "Wed"}, {"label": "Thu"}],
            "multi_select": True,
        },
    ]
}


def test_real_account_questions_use_the_shared_grouped_contract():
    assert account_ask_user is shared_ask_user


@pytest.mark.parametrize("render", [approval_text, action_preview])
def test_text_interfaces_show_grouped_questions_and_options(render):
    text = render({"tool": "ask_user", "approval_id": "ask-1", "args": QUESTIONS})
    assert "Which Sam do you mean?" in text
    assert "Sam Lee (your manager)" in text
    assert "Sam Park" in text
    assert "Which days work? (pick any)" in text
    assert "Wed" in text and "Thu" in text
    assert "/answer ask-1" in text


def test_legacy_question_reads_as_one_question_and_numbers_pick_options():
    questions = questions_of({"question": "Which city?", "choices": ["Tulsa", "Chicago"]})
    assert [q.question for q in questions] == ["Which city?"]
    assert answer_from_text(questions, "2") == "Which city?\nAnswer: Chicago"
    assert answer_from_text(questions, "  Paris ") == "Which city?\nAnswer: Paris"
    # Two numbers on a single-select question are the owner's words, not two picks.
    assert answer_from_text(questions, "1, 2").endswith("Answer: 1, 2")


@pytest.mark.parametrize(
    "answers",
    [
        [AskAnswer(header="Which Sam", selected=["Sam Lee"])],  # one missing
        [
            AskAnswer(header="Which Sam", selected=["Sam Lee", "Sam Park"]),  # single-select
            AskAnswer(header="Day", selected=["Wed"]),
        ],
        [
            AskAnswer(header="Which Sam", selected=["Sam Kim"]),  # not offered
            AskAnswer(header="Day", selected=["Wed"]),
        ],
        [
            AskAnswer(header="Which Sam", selected=["Sam Lee"], custom="both"),  # both kinds
            AskAnswer(header="Day", selected=["Wed"]),
        ],
    ],
)
def test_answers_must_match_the_questions(answers):
    with pytest.raises(ValueError):
        render_answers(questions_of(QUESTIONS), answers)


async def test_card_answers_resume_the_run_with_each_question_beside_its_answer(tmp_path):
    seen = []

    def respond(messages, info):
        if not seen:
            seen.append(True)
            return ModelResponse(parts=[ToolCallPart("ask_user", QUESTIONS, tool_call_id="ask")])
        returns = [
            part
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolReturnPart) and part.tool_name == "ask_user"
        ]
        seen.append(returns[-1].content)
        return reply()

    async with running_bud(tmp_path, respond, ask=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Move my meeting with Sam", "message-1")
        await bud.settle()
        request = bud.runner.snapshot("owner").pending_approvals[0]
        assert request.tool == "ask_user"
        with pytest.raises(ValueError):
            bud.runner.answer("owner", request.approval_id, "x", answers=[])
        assert bud.runner.answer(
            "owner",
            request.approval_id,
            None,
            answers=[
                AskAnswer(header="Which Sam", selected=["Sam Lee"]),
                AskAnswer(header="Day", selected=["Wed", "Thu"]),
            ],
        )
        await bud.settle()
        assert seen[-1] == (
            "Which Sam do you mean?\nAnswer: Sam Lee\n\nWhich days work?\nAnswer: Wed, Thu"
        )


@pytest.mark.parametrize(
    "questions",
    [
        [],
        [QUESTIONS["questions"][0]] * 2,
        [{"header": str(index), "question": "Which day?"} for index in range(11)],
    ],
)
async def test_invalid_question_groups_are_rejected_before_becoming_pending(tmp_path, questions):
    called = []

    def respond(messages, info):
        called.append(True)
        if len(called) == 1:
            return ModelResponse(
                parts=[ToolCallPart("ask_user", {"questions": questions}, tool_call_id="ask")]
            )
        return reply()

    async with running_bud(tmp_path, respond, ask=True) as bud:
        bud.log.append("owner", "session.created")
        bud.runner.start("owner", "Move my meeting", "message-1")
        await bud.settle()
        assert len(called) == 2
        assert not bud.runner.snapshot("owner").pending_approvals
        assert not any(event.type == "approval.requested" for event in bud.log.read("owner"))
