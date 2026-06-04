import json
import random

from players import Player, Team


class Draw:
    def __init__(self, mode="snake", leftover_policy="drop", t1_cap=None, seed=None):
        self.players = []
        self.__teams = {}
        self.mode = mode
        self.leftover_policy = leftover_policy
        self.t1_cap = t1_cap
        self.bonus_pool = []
        self._rng = random.Random(seed)

    def add_players(self, players):
        names = [f"Player {i + 1}" for i in range(players)] if isinstance(players, int) else list(players)
        self.players = [Player(n) for n in names]
        return self.players

    def add_all_teams(self, path="teams.json"):
        with open(path) as f:
            data = json.load(f)
        for t in data["teams"]:
            self.__teams[t["name"]] = Team(
                name=t["name"], confederation=t["confederation"], group=t["group"],
                tier=t["tier"], tier_label=t["tier_label"], weight=t["weight"],
                composite=t["composite"], decimal_odds=t["decimal_odds"],
                fifa_points=t["fifa_points"],
            )
        return self.__teams

    def game_set_up_check(self):
        if not self.players:
            raise ValueError("add players first")
        if not self.__teams:
            raise ValueError("load teams first")

    def sort_teams_to_players(self):
        self.game_set_up_check()

        teams = list(self.__teams.values())
        n = len(self.players)
        per_player = len(teams) // n
        if per_player == 0:
            raise ValueError(f"{n} players but only {len(teams)} teams")

        for p in self.players:
            p.clear_teams()                 # FIX: needs the call ()
        self.bonus_pool = []

        in_play_count = per_player * n
        in_play, leftover = self._split_in_play(teams, in_play_count)

        if self.mode == "weighted" and self.t1_cap is not None:
            t1_in_play = sum(1 for t in in_play if t.tier == 1)
            if t1_in_play > n * self.t1_cap:
                print(f"[warn] t1_cap={self.t1_cap} can't hold: {t1_in_play} Tier-1 teams "
                      f"in play for {n} players. Surplus favourites will land somewhere.")

        if self.mode == "snake":
            self._snake(in_play, per_player)
        elif self.mode == "weighted":
            self._weighted(in_play, per_player)
        elif self.mode == "random":
            self._random(in_play, per_player)
        else:
            raise ValueError(f"unknown mode: {self.mode}")

        if self.leftover_policy == "pool":
            self.bonus_pool = sorted(leftover, key=lambda t: -t.composite)
        return self.players

    def _split_in_play(self, teams, keep):
        """Weighted sample (without replacement) of `keep` survivors; weakest
        teams are likeliest to be the leftovers."""
        if keep >= len(teams):
            return teams[:], []
        keyed = sorted(teams, key=lambda t: self._rng.random() ** (1.0 / t.weight), reverse=True)
        return keyed[:keep], keyed[keep:]

    def _snake(self, in_play, per_player):
        n = len(self.players)
        ordered = sorted(in_play, key=lambda t: -t.composite)
        pots = [ordered[i * n:(i + 1) * n] for i in range(per_player)]
        for i, pot in enumerate(pots):
            pot = pot[:]
            self._rng.shuffle(pot)
            order = self.players if i % 2 == 0 else self.players[::-1]
            for player, team in zip(order, pot):
                player.add_team(team)       # FIX: was addTeam

    def _random(self, in_play, per_player):
        pool = in_play[:]
        order = list(self.players)
        for _ in range(per_player):
            self._rng.shuffle(order)
            for player in order:
                team = self._rng.choice(pool)      # every team equally likely
                pool.remove(team)
                player.add_team(team)

    def _weighted(self, in_play, per_player):
        pool = in_play[:]
        order = list(self.players)
        for _ in range(per_player):
            self._rng.shuffle(order)
            for player in order:
                team = self._weighted_pick(pool, player)
                pool.remove(team)
                player.add_team(team)       # FIX: was addTeam

    def _weighted_pick(self, pool, player):
        candidates = pool
        if self.t1_cap is not None and player.tierCount(1) >= self.t1_cap:
            non_t1 = [t for t in pool if t.tier != 1]
            if non_t1:
                candidates = non_t1
        weights = [t.weight for t in candidates]
        return self._rng.choices(candidates, weights=weights, k=1)[0]

    def _team_dict(self, t):
        return {"name": t.name, "tier": t.tier, "group": t.group,
                "confederation": t.confederation, "composite": t.composite}

    def export_result(self, path="draw_result.json"):
        result = {
            "mode": self.mode, "leftover_policy": self.leftover_policy,
            "players": [{"name": p.name, "strength": p.strength(),
                         "teams": [self._team_dict(t) for t in p.teams]} for p in self.players],
            "bonus_pool": [self._team_dict(t) for t in self.bonus_pool],
        }
        if path:
            with open(path, "w") as f:
                json.dump(result, f, indent=2)
        return result

    def summary(self):
        lines = [f"Draw: mode={self.mode}, leftover={self.leftover_policy}"
                 f"{'' if self.t1_cap is None else f', t1_cap={self.t1_cap}'}"]
        for p in sorted(self.players, key=lambda p: -p.strength()):
            lines.append(repr(p))
        if self.bonus_pool:
            lines.append("Bonus pool : " + ", ".join(f"{t.name}(T{t.tier})" for t in self.bonus_pool))
        return "\n".join(lines)
