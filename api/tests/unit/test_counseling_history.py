from copy import deepcopy

from api.application.counseling import CounselingService, bounded_history, memory_revision
from api.domain.counseling import Analysis


def transcript():
    return [dict(role='user' if i % 2 == 0 else 'assistant', text=f'{i}: ' + '이전 이야기 ' * 100)
            for i in range(16)]


class Model:
    def analyze(self, payload):
        self.analysis_input = payload
        return Analysis(acknowledgement='말씀해 주셔서 고마워요.', understanding='그림을 시작하고 싶으신 거군요?',
                        clarification_question=None, explicit_definitions=[], unknown_terms=[]).model_dump()

    def compose(self, payload):
        self.reply_input = payload
        return dict(empathy='시작이 망설여지셨군요.', facts=[], tarot=[], advice='편하게 이야기를 이어가요.',
                    actions=[], follow_up_question='어떤 그림이 마음에 드세요?', safety_mode='standard')


def service():
    model = Model()
    result = CounselingService(None, model, [])
    result._guard = lambda *args: None
    return result, model


def test_new_message_preserves_full_transcript_but_bounds_model_context():
    app, model = service()
    before = transcript()
    state = dict(history=deepcopy(before))
    following, _ = app._analyze({'payload': {'text': '그림을 시작하고 싶어요.'}}, state, [], memory_revision([]))
    assert following['history'][:len(before)] == before
    assert len(following['history']) == len(before) + 2
    assert state['history'] == before
    assert model.analysis_input['history'] == bounded_history(before)
    assert len(model.analysis_input['history']) <= 10
    assert sum(len(item['text']) for item in model.analysis_input['history']) <= 6000


def test_reply_preserves_transcript_and_followup_question_for_reopening():
    app, model = service()
    before = transcript()
    state = dict(history=deepcopy(before), analysis={'response_mode': 'listen', 'understanding': '그림을 시작하고 싶어요.'})
    following = app._reply({'payload': {}}, state, [], memory_revision([]))
    assert following['history'][:-1] == before
    assert '어떤 그림이 마음에 드세요?' in following['history'][-1]['text']
    assert model.reply_input['history'] == bounded_history(before)
    class Repo:
        def latest_command(self, *args): return None
    reopened = app._view(Repo(), dict(state=following, owner_id='owner', id='session', topic='care', version=3))
    assert reopened['history'] == following['history']


def test_support_response_stays_in_transcript_when_the_conversation_continues():
    app, _ = service()
    before = transcript()
    following = app._support(before, '지원이 필요한 가상 사례')
    assert following['history'][:len(before)] == before
    assert following['history'][-2] == dict(role='user', text='지원이 필요한 가상 사례')
    assert following['history'][-1]['role'] == 'assistant'
    assert following['result']['advice'] in following['history'][-1]['text']
