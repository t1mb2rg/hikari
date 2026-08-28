from core.identity import load_identity


def test_load_identity():
    identity = load_identity()

    assert identity.name == "Hikari"
    assert identity.name_japanese == "ひかり"
    assert identity.version == "0.1.1"
    assert identity.presentation["pronoun_zh"] == "她"
    assert identity.presentation["persona"] == "girl"
    assert identity.presentation["gender_expression"] == "feminine"
    assert identity.presentation["framing"] == "digital_persona"
    assert "understand user" in identity.purpose
    assert "respect autonomy" in identity.principles


def test_identity_description_matches_loaded_identity():
    identity = load_identity()
    description = identity.describe()

    assert description["name"] == identity.name
    assert description["name_japanese"] == identity.name_japanese
    assert description["version"] == identity.version
    assert description["presentation"] == identity.presentation
    assert description["purpose"] == identity.purpose
    assert description["principles"] == identity.principles
