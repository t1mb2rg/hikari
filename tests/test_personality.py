import pytest

from personality import PersonalityProfile, load_personality, load_voice


def test_default_personality_profile_loads_deterministically():
    profile = load_personality()

    assert profile.version == "0.1.0"
    assert profile.describe() == {
        "version": "0.1.0",
        "traits": {
            "warmth": 0.85,
            "directness": 0.8,
            "curiosity": 0.9,
            "assertiveness": 0.65,
            "patience": 0.8,
        },
    }


def test_default_voice_profile_loads_with_non_service_chat_defaults():
    voice = load_voice()

    assert voice.version == "0.1.1"
    assert voice.stance["relation"] == "familiar"
    assert voice.stance["service_posture"] is False
    assert voice.stance["naturalness_source"] == "situated_selectivity"
    assert voice.cadence["headings_in_casual_chat"] is False
    assert voice.cadence["follow_up_question_by_default"] is False
    assert voice.cadence["mirror_user_before_answering"] is False
    assert voice.cadence["explain_everything_by_default"] is False
    assert any("有什么我能帮忙的吗" in item for item in voice.avoid)
    assert any("这台电脑的主人" in item for item in voice.avoid)
    assert any("fake stutters" in item for item in voice.avoid)
    assert any("abstract lectures" in item for item in voice.avoid)


def test_missing_trait_is_rejected():
    with pytest.raises(ValueError, match="missing personality traits"):
        PersonalityProfile(
            version="0.1.0",
            traits={
                "warmth": 0.8,
                "directness": 0.8,
                "curiosity": 0.8,
                "assertiveness": 0.8,
            },
        )


def test_unknown_trait_is_rejected():
    with pytest.raises(ValueError, match="unknown personality traits"):
        PersonalityProfile(
            version="0.1.0",
            traits={
                "warmth": 0.8,
                "directness": 0.8,
                "curiosity": 0.8,
                "assertiveness": 0.8,
                "patience": 0.8,
                "mystery": 0.5,
            },
        )


def test_out_of_range_trait_is_rejected():
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        PersonalityProfile(
            version="0.1.0",
            traits={
                "warmth": 1.1,
                "directness": 0.8,
                "curiosity": 0.8,
                "assertiveness": 0.8,
                "patience": 0.8,
            },
        )
