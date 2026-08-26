import pytest

from personality import PersonalityProfile, load_personality


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
