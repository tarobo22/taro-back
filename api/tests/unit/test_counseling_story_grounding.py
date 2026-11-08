"""The reply must receive the original user evidence, not only an AI paraphrase.

These are context-delivery regressions; real model correctness is checked separately.
"""
import pytest

from api.application.counseling import CounselingService, memory_revision


@pytest.mark.parametrize('text', [
    '먹고살기넘힘들다 쓰리잡뛰어야해',
    '지금 쓰리잡 뛰고 있어',
    '작년에는 쓰리잡 뛰었는데 지금은 하나만 해',
    '쓰리잡은 아직 안 뛰어. 다음 달부터 해야 할 것 같아',
    '동생이 쓰리잡을 뛰어야 한대. 나는 일이 하나야',
    '월세가 오르면 쓰리잡을 뛰어야 할까 봐 걱정돼',
])
def test_reply_preserves_latest_user_words_despite_wrong_ai_summary(text):
    class Model:
        payload = None

        def compose(self, payload):
            self.payload = payload
            return dict(empathy='마음이 쓰이시는군요.', facts=[], tarot=[],
                        advice='편하게 이야기해 주세요.', actions=[],
                        follow_up_question=None, safety_mode='standard')

    model = Model()
    service = CounselingService(None, model, [])
    service._guard = lambda *args: None
    history = [dict(role='assistant', text='이미 세 가지 일을 하고 계시군요.'),
               dict(role='user', text=text),
               dict(role='assistant', text='제가 이해한 내용이 맞을까요?')]
    state = dict(analysis=dict(understanding='이미 세 가지 일을 병행하고 있다.', response_mode='listen'), history=history)
    # Confirmation jobs have no text. The original message still has to reach composition.
    reply = service._reply({'payload': {}}, state, [], memory_revision([]))
    assert model.payload['latest_user_text'] == text
    assert model.payload['history'] == history
    assert reply['history'][:-1] == history


def test_user_evidence_survives_bounded_history_without_using_assistant_as_source():
    class Model:
        payload = None

        def compose(self, payload):
            self.payload = payload
            return dict(empathy='그 부담을 함께 살펴봐요.', facts=[], tarot=[],
                        advice='편하게 이야기해 주세요.', actions=[],
                        follow_up_question=None, safety_mode='standard')

    model = Model()
    service = CounselingService(None, model, [])
    service._guard = lambda *args: None
    original = '아직 시작한 게 아니라 다음 달부터 해야 할 것 같아.'
    history = [dict(role='user', text=original)] + [dict(role='assistant', text='이전 답변') for _ in range(11)]
    state = dict(analysis=dict(understanding='일의 부담', response_mode='listen'), history=history)
    service._reply({'payload': {}}, state, [], memory_revision([]))
    assert model.payload['latest_user_text'] == original
    assert len(model.payload['history']) == 10
    assert all(item['role'] == 'assistant' for item in model.payload['history'])
