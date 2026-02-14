class BaseEncoder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError