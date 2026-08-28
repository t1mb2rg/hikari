from pathlib import Path
import yaml


IDENTITY_PATH = Path(__file__).parent / "identity.yaml"


class HikariIdentity:
    def __init__(self, data: dict):
        self.name = data.get("name")
        self.name_japanese = data.get("name_japanese")
        self.version = data.get("version")
        self.presentation = dict(data.get("presentation", {}))
        self.purpose = data.get("purpose", [])
        self.principles = data.get("principles", [])

    def describe(self) -> dict:
        return {
            "name": self.name,
            "name_japanese": self.name_japanese,
            "version": self.version,
            "presentation": dict(self.presentation),
            "purpose": self.purpose,
            "principles": self.principles,
        }


def load_identity() -> HikariIdentity:
    with open(IDENTITY_PATH, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    return HikariIdentity(data)


if __name__ == "__main__":
    identity = load_identity()
    print(identity.describe())
