"""Evidence for one confirmed counseling turn, drawn only from its user messages."""
from typing import Literal

from pydantic import Field

from api.domain.counseling import Analysis, CounselingError, StrictModel


class StoryAnchor(StrictModel):
    slot: Literal['actor', 'time', 'event', 'feeling', 'constraint', 'attempt', 'preference']
    turn: int = Field(ge=0, le=10)
    quote: str = Field(min_length=1, max_length=280)


class StoryAnalysis(Analysis):
    story_anchors: list[StoryAnchor] = Field(default_factory=list, max_length=12)


def user_turns(history):
    return [item['text'] for item in reversed(history) if item['role'] == 'user']


def verified_anchors(anchors, turns):
    chosen = {}
    for anchor in sorted(anchors, key=lambda item: item.turn):
        if anchor.slot in chosen or anchor.turn >= len(turns):
            continue
        if anchor.quote.strip() and anchor.quote in turns[anchor.turn]:
            chosen[anchor.slot] = anchor
    return list(chosen.values())


def confirm_story(analysis, history):
    draft = StoryAnalysis.model_validate(analysis)
    turns = user_turns(history)
    if not turns or draft.clarification_question:
        raise CounselingError('CONFIRMATION_REQUIRED', 409)
    return dict(
        mode=draft.response_mode, understanding=draft.understanding,
        perspective=draft.perspective, latest_message=turns[0],
        anchors=[anchor.model_dump() for anchor in verified_anchors(draft.story_anchors, turns)],
    )
