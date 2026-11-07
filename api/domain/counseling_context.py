"""Evidence for one confirmed counseling turn, drawn only from its user messages."""
import re
from typing import Literal

from pydantic import Field

from api.domain.counseling import Analysis, CounselResult, CounselingError, StrictModel


class StoryAnchor(StrictModel):
    slot: Literal['actor', 'time', 'event', 'feeling', 'constraint', 'attempt', 'preference']
    turn: int = Field(ge=0, le=10)
    quote: str = Field(min_length=1, max_length=280)


class StoryAnalysis(Analysis):
    story_anchors: list[StoryAnchor] = Field(default_factory=list, max_length=12)


class ReviewedReply(StrictModel):
    reply: CounselResult | None


_TIME = re.compile(r'(?:이번|다음|지난)\s*(?:주말|주|달)|오늘|내일|어제|주말|\d+\s*(?:분|시간)')
_INSTRUCTION = re.compile(r'보\s*세요|주\s*세요|하\s*세요|하십시오|해야\s*(?:해요|합니다)')
_ACTION_SEQUENCE = re.compile(r'하고\s+(?:나서|난\s*뒤)|그\s*다음|동시에|한\s*다음')


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


def _times(text):
    return {''.join(value.split()) for value in _TIME.findall(text)}


def _story_times(story):
    return _times(' '.join([story['latest_message'], *(
        anchor['quote'] for anchor in story['anchors'] if anchor['slot'] == 'time'
    )]))


def reply_violations(story, reply):
    prose = '\n'.join([reply['empathy'], reply['advice'], *(
        card['reflection'] for card in reply['tarot']
    )])
    issues = []
    if '?' in prose or '？' in prose:
        issues.append('extra_question')
    if _INSTRUCTION.search(prose):
        issues.append('extra_instruction')
    if _times(prose) - _story_times(story):
        issues.append('unanchored_time')
    if story['mode'] == 'listen' and reply['facts']:
        issues.append('listening_fact_dump')
    return issues


def review_reasons(story, reply, history):
    reasons = reply_violations(story, reply)
    slots = {anchor['slot'] for anchor in story['anchors']}
    if reply['safety_mode'] == 'support':
        reasons.append('check_safety_context')
    if len(history) > 1 and slots & {'actor', 'time', 'event'}:
        reasons.append('check_latest_correction')
    if story['mode'] == 'advice':
        action = reply['actions'][0]
        if _ACTION_SEQUENCE.search(action):
            reasons.append('check_one_action')
        confirmed_times = _story_times(story)
        if confirmed_times and _times(action) - confirmed_times:
            reasons.append('check_action_timing')
        if slots & {'attempt', 'constraint'}:
            reasons.append('check_previous_attempt_and_constraints')
    return reasons
