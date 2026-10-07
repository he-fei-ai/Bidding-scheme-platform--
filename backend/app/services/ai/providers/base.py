from dataclasses import dataclass


@dataclass
class AIMessage:
    role: str
    content: str
