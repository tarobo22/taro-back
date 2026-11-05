import re

def advice_for(topic,question,answers,tone):
    concern=bool(re.search('걱정|불안|무섭|힘들|막막',question))
    serious=bool(re.search('갈등|결정|고민|그만|진로|직장',question))
    empathy='계속 마음에 걸리는 일이 있었겠어요. 천천히 함께 정리해 봐요.' if concern else '이 마음을 꺼내 준 것부터 의미 있는 시작이에요.'
    advice='오늘 모든 답을 정하기보다, 내가 원하는 것을 한 가지부터 살펴보세요.'
    action='지금 가장 필요한 것을 한 문장으로 적어 보세요.'
    if topic=='연애·관계':
        advice='상대의 마음을 추측하기보다 내가 느낀 점과 바라는 점을 나누어 보는 게 도움이 될 수 있어요.'
        action='보내지 않아도 괜찮으니, 전하고 싶은 말 한 문장만 써보세요.' if len(answers)>1 and not answers[1] else '“나는 그때 이렇게 느꼈어”로 시작하는 짧은 문장을 준비해 보세요.'
    if topic=='일·진로':
        advice='당장 바꿀 수 있는 부분과 시간이 필요한 부분을 나누면 다음 선택이 조금 선명해질 수 있어요.'
        action='지금의 조건에서 지킬 수 있는 내 기준 한 가지를 적어 보세요.' if answers and not answers[0] else '10분 안에 할 수 있는 다음 작업 하나만 정해 보세요.'
    if topic=='마음 돌봄' or concern:
        advice='지금 느끼는 마음을 억지로 없애려고 하지 않아도 괜찮아요. 몸과 마음이 편해지는 작은 조건부터 챙겨 보세요.'
        action='잠시 편한 자세로 앉아 호흡을 고르고, 가까운 사람에게 지금의 마음을 한 줄로 전해 보세요.'
    if len(answers)>2 and not answers[2]:action='오늘은 결정을 미뤄도 괜찮아요. 떠오른 생각 한 줄만 남겨 두세요.'
    if tone=='giga':
        empathy='마음이 무거웠겠지. 혼자 다 떠안을 필요는 없어.' if concern else '좋아. 네 마음을 꺼낸 것부터 이미 한 걸음이야.'
        advice='상대의 속마음을 맞히는 데 너를 쓰지 마. 네가 느낀 것과 원하는 것을 분명하게 보자.' if topic=='연애·관계' else '오늘 전부 해결하려고 하지 마. 네가 바꿀 수 있는 한 가지부터 하자.'
        action='지금 쉬겠다는 선택도 네 선택이야. 오늘 필요한 것 하나만 적자.' if len(answers)>2 and not answers[2] else '지금 할 수 있는 가장 작은 행동을 적고, 10분만 시작해 보자.'
    if re.search(r'자살|죽고\s*싶|나를\s*해치|자해',question):
        return {'empathy':'지금은 이 마음을 혼자 견디지 않아도 괜찮아요.','advice':'우선 스스로를 해칠 수 있는 물건에서 거리를 두고, 혼자 있지 않을 수 있는 안전한 곳으로 이동해 주세요.','action':'가까운 사람에게 지금 함께 있어 달라고 말해 주세요. 당장 위험하다면 지역의 긴급 도움을 요청해 주세요.','expression':'serious'}
    return {'empathy':empathy,'advice':advice,'action':action,'expression':'concern' if concern else 'serious' if serious else 'encourage' if tone=='giga' else 'listen'}


class LocalGuidance:
    def advise(self, topic, question, answers, tone, context):
        return advice_for(topic, question, answers, tone)
