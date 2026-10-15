"""Minimal synthetic client for Apache's length-prefixed streaming protocol."""


class Instructions:
    def __init__(self):
        self.pending = ""

    def feed(self, message):
        self.pending += message
        position = 0
        fields = []
        committed = 0
        while position < len(self.pending):
            dot = self.pending.find(".", position)
            if dot < 0:
                break
            length = int(self.pending[position:dot])
            end = dot + 1 + length
            if end >= len(self.pending):
                break
            fields.append(self.pending[dot + 1 : end])
            delimiter = self.pending[end]
            assert delimiter in {",", ";"}
            position = end + 1
            if delimiter == ";":
                yield fields
                fields = []
                committed = position
        self.pending = self.pending[committed:]


def encode(*fields):
    return ",".join(f"{len(field)}.{field}" for field in fields) + ";"
