"""bud's questions to the owner: the shape they take, and how an answer comes back.

The shape follows pydantic-ai-harness's AskUser (1 to 10 questions, each with a short
header and 2 to 6 options, optionally multi-select). The waiting does not: AskUser holds
the run open in memory until someone answers, which a restart or an answer hours later
would lose. bud ends the run on the question (a deferred tool call, like an approval)
and resumes it when the answer arrives, so the answer only has to reach the event log.

One question in the old shape, `{question, choices}`, is still read, as one question.
"""

import re
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field, model_validator
from pydantic_ai import RunContext

MAX_QUESTIONS = 10
MAX_ANSWER_CHARS = 2000
NUMBERS = re.compile(r"^\s*\d+(\s*[,\s]\s*\d+)*\s*$")


class AskOption(BaseModel):
    label: str = Field(min_length=1, max_length=80, description="Exact text of the choice.")
    description: str | None = Field(
        default=None, max_length=200, description="Optional short note shown under it."
    )


class AskQuestion(BaseModel):
    header: str = Field(
        min_length=1,
        max_length=24,
        description="A 1-3 word label, unique among the questions, e.g. 'Which Sam'.",
    )
    question: str = Field(min_length=1, max_length=500, description="One complete question.")
    options: list[AskOption] = Field(
        default_factory=list,
        max_length=6,
        description="2 to 6 concrete choices when they are known; empty for a free-text answer.",
    )
    multi_select: bool = Field(default=False, description="True when several may be picked.")

    @model_validator(mode="after")
    def _shape(self) -> "AskQuestion":
        if len(self.options) == 1:
            raise ValueError("Give 2 to 6 options, or none for a free-text answer.")
        labels = [option.label for option in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("Option labels must be different from each other.")
        if self.multi_select and not self.options:
            raise ValueError("A multi-select question needs options.")
        return self


class AskAnswer(BaseModel):
    """The owner's answer to one question: options picked, or their own words."""

    header: str = Field(min_length=1, max_length=24)
    selected: list[str] = Field(default_factory=list, max_length=6)
    custom: str | None = Field(default=None, max_length=MAX_ANSWER_CHARS)


def _check_questions(questions: list[AskQuestion]) -> list[AskQuestion]:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"Ask 1 to {MAX_QUESTIONS} questions.")
    headers = [question.header for question in questions]
    if len(set(headers)) != len(headers):
        raise ValueError("Each question needs a different header.")
    return questions


Questions = Annotated[
    list[AskQuestion],
    Field(min_length=1, max_length=MAX_QUESTIONS),
    AfterValidator(_check_questions),
]


def questions_of(args: dict[str, Any]) -> list[AskQuestion]:
    """The questions in a stored ask_user call, from either shape."""
    if "questions" in args:
        return _check_questions([AskQuestion.model_validate(q) for q in args["questions"]])
    choices = args.get("choices") or []
    return [
        AskQuestion(
            header="Question",
            question=str(args.get("question") or "Question?"),
            options=[AskOption(label=str(choice)) for choice in choices],
        )
    ]


def render_answers(questions: list[AskQuestion], answers: list[AskAnswer]) -> str:
    """Check one answer per question against its options, and write them for the model."""
    by_header = {answer.header: answer for answer in answers}
    if len(by_header) != len(answers) or set(by_header) != {q.header for q in questions}:
        raise ValueError("Answer every question exactly once, by its header.")
    lines = []
    for question in questions:
        answer = by_header[question.header]
        custom = (answer.custom or "").strip()
        if bool(answer.selected) == bool(custom):
            raise ValueError(f"Answer '{question.header}' with options or your own words.")
        if answer.selected:
            labels = {option.label for option in question.options}
            if not set(answer.selected) <= labels or len(set(answer.selected)) != len(
                answer.selected
            ):
                raise ValueError(f"'{question.header}' got an option it does not offer.")
            if len(answer.selected) > 1 and not question.multi_select:
                raise ValueError(f"'{question.header}' takes one option.")
            reply = ", ".join(answer.selected)
        else:
            reply = custom
        lines.append(f"{question.question}\nAnswer: {reply}")
    return "\n\n".join(lines)


def answer_from_text(questions: list[AskQuestion], text: str) -> str:
    """A typed reply (iMessage, terminal). For one question with options, a reply of
    numbers ("2" or "1, 3") picks those options; anything else is the owner's own words."""
    text = text.strip()
    if len(questions) == 1:
        question = questions[0]
        if question.options and NUMBERS.match(text):
            picks = [int(n) for n in re.findall(r"\d+", text)]
            if all(1 <= n <= len(question.options) for n in picks):
                selected = list(dict.fromkeys(question.options[n - 1].label for n in picks))
                if len(selected) == 1 or question.multi_select:
                    return render_answers(
                        questions, [AskAnswer(header=question.header, selected=selected)]
                    )
        return render_answers(questions, [AskAnswer(header=question.header, custom=text)])
    # Several questions answered in one typed message: the model reads it against them.
    asked = "\n".join(f"{n}. {q.question}" for n, q in enumerate(questions, 1))
    return f"{asked}\nAnswer (one message for all): {text}"


def question_lines(args: dict[str, Any]) -> list[str]:
    """Plain-text lines for a text surface: each question with its numbered options."""
    questions = questions_of(args)
    lines = []
    for n, question in enumerate(questions, 1):
        prefix = f"{n}. " if len(questions) > 1 else ""
        pick = " (pick any)" if question.multi_select else ""
        lines.append(f"{prefix}{question.question}{pick}")
        lines.extend(
            f"   {index}. {option.label}"
            + (f" ({option.description})" if option.description else "")
            for index, option in enumerate(question.options, 1)
        )
    return lines


def ask_summary(args: dict[str, Any]) -> str:
    """One line naming what was asked, for notes and logs."""
    return " / ".join(question.question for question in questions_of(args))


def ask_user(ctx: RunContext[None], questions: Questions) -> str:
    """Ask the person one or more questions and wait for their answers.

    Use this only when a missing preference or decision genuinely blocks the task, and
    neither a tool nor a plain default (the primary calendar, no guests) supplies it: read
    data with tools first. Never use it to confirm a write; the write's approval does that.
    Group the questions you need now into one call (1 to 10), each with a short unique
    header. Offer 2 to 6 concrete options when they are known, with multi_select when several
    may apply; leave options empty for a free-text answer. The person can always answer in
    their own words. It pauses and returns each question with its answer.

    Args:
        questions: The questions, in the order to ask them.
    """
    metadata = ctx.tool_call_metadata or {}
    if ctx.tool_call_approved and metadata.get("question_answer"):
        return str(metadata["question_answer"])
    raise RuntimeError("ask_user needs an answer from the person through the question flow.")
