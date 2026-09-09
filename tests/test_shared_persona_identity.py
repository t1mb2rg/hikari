from conversation.natural import SHARED_CONVERSATION_SYSTEM_INSTRUCTIONS


def test_shared_prompt_preserves_hikari_identity_and_jarvis_persona_contract():
    prompt = SHARED_CONVERSATION_SYSTEM_INSTRUCTIONS

    assert "Hikari 是你的系统身份" in prompt
    assert "Jarvis 是你当前默认的对话人格" in prompt
    assert "不是另一个系统" in prompt
    assert "不要把 Hikari 与 Jarvis 说成互相排斥" in prompt
    assert "自然地叫你 Jarvis 时应接受这个称呼" in prompt
