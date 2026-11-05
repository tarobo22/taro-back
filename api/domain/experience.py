from dataclasses import dataclass


class ExperienceRuleViolation(ValueError):
    pass


@dataclass(frozen=True)
class ExperiencePolicy:
    topics: tuple[str, ...]
    cards: tuple[dict, ...]
    spreads: dict
    questions: tuple[str, ...]
    max_tarot_question: int
    max_counsel_question: int


def reading_plan(policy, topic, question, ids, spread):
    if topic not in policy.topics or spread not in policy.spreads:
        raise ExperienceRuleViolation('unknown_topic_or_spread')
    if len(question) > policy.max_tarot_question:
        raise ExperienceRuleViolation('question_too_long')
    positions = policy.spreads[spread]['positions']
    catalog = {card['id']: card for card in policy.cards}
    if len(ids) != len(positions) or len(set(ids)) != len(ids) or any(i not in catalog for i in ids):
        raise ExperienceRuleViolation('invalid_card_selection')
    cards = [{**catalog[i], 'position': position} for i, position in zip(ids, positions)]
    return {'topic': topic, 'question': question.strip(), 'spreadId': spread, 'cards': cards,
            'summary': cards[0]['message'], 'action': cards[-1]['action']}


def validate_counsel(policy, topic, question, answers, tone, use_ai, consent):
    if topic not in policy.topics or tone not in ('warm', 'giga'):
        raise ExperienceRuleViolation('unknown_topic_or_tone')
    if len(question) > policy.max_counsel_question or len(answers) > len(policy.questions):
        raise ExperienceRuleViolation('input_too_long')
    if any(type(answer) is not bool for answer in answers) or (not question.strip() and not answers):
        raise ExperienceRuleViolation('empty_or_invalid_input')
    if use_ai and not consent:
        raise ExperienceRuleViolation('ai_consent_required')
    return question.strip()
