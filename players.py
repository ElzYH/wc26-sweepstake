from dataclasses import dataclass

@dataclass(frozen=True)
class Team:
    name: str
    confederation: str
    group: str
    tier: int
    tier_label: str
    weight: int
    composite: float
    decimal_odds: float
    fifa_points: float

class Player:
    def __init__(self, name):
        self.name = name
        self.__teams = []


    def add_team(self, team):
        self.__teams.append(team)

    def clear_teams(self):
        self.__teams.clear()

    @property
    def teams(self):
        return list(self.__teams)

    def tierCount(self, tier):
        return sum(1 for t in self.__teams if t.tier == tier)

    def strength(self):
        return round(sum(t.composite for t in self.__teams), 1)

    def __repr__(self):
        picks = ", ".join(f"{t.name}(T{t.tier})" for t in self.__teams)
        return f"{self.name:14} [{self.strength():>5}] {picks}"