from uuid import UUID, uuid4

import pytest

from api.tests.integration.test_p0_ai_jobs import RESULT, ai_env, consent, turn


pytestmark = pytest.mark.integration


@pytest.mark.parametrize('change', [{'summary': ' \n '}, {'advice': '당신은 반드시 실패합니다.'}])
def test_invalid_required_text_or_explicit_prediction_never_becomes_a_saved_answer(ai_env, change):
    service, provider, pool, owners, _ = ai_env
    owner = owners[0]
    def generate(payload):
        provider.calls.append(payload)
        return {**RESULT, **change}
    provider.generate = generate
    conversation = service.create_conversation(owner, uuid4(), 'care', 'warm')
    accepted = turn(service, owner, conversation, consent(service, owner))
    assert service.run_next('output-policy-worker') is True
    job = service.get_job(owner, UUID(accepted['job_id']))
    assert job['state'] == 'failed' and job['error']['code'] == 'AI_OUTPUT_INVALID'
    assert job['result_url'] is None and len(provider.calls) == 1
    saved = service.get_turn(owner, UUID(conversation['id']), UUID(accepted['resource_id']))
    assert saved['state'] == 'failed' and saved['assistant_result'] is None
