from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum, auto


class Room:
    """Represent an active roulette room."""

    def __init__(self, players: list[str], ban_time: int):
        self.players = players
        self.ban_time = ban_time
        self.bullet = random.randint(1, 6)
        self.round = 0
        self.next_idx: int | None = random.randint(0, 1) if players else None
        self.participated: set[str] = set()

    @property
    def over(self) -> bool:
        """Return whether the bullet round has been reached."""
        return self.round >= self.bullet

    def can_shoot(self, shooter: str) -> bool:
        """Return whether a player may shoot in the current round."""
        if self.players and isinstance(self.next_idx, int):
            return shooter == self.players[self.next_idx]
        if not self.players:
            return shooter not in self.participated
        return True

    def shoot(self, shooter: str) -> bool:
        """Advance the room by one valid shot."""
        if self.over:
            return False

        if not self.players:
            if shooter in self.participated:
                return False
            self.participated.add(shooter)
            self.round += 1
            return self.round == self.bullet

        if shooter not in self.players or not self.can_shoot(shooter):
            return False
        if self.next_idx is None:
            return False

        self.round += 1
        self.next_idx = 1 - self.next_idx
        return self.round == self.bullet

    def get_all_participants(self) -> list[str]:
        """Return all players who participated in the game."""
        if self.players:
            return self.players
        return list(self.participated)


class StartGameError(Enum):
    """Describe why a game could not be created."""

    SELF_TARGET = auto()
    SENDER_BUSY = auto()
    TARGET_BUSY = auto()
    GROUP_BUSY = auto()
    CREATE_FAILED = auto()


@dataclass(slots=True)
class StartGameResult:
    """Contain the outcome of creating a roulette game."""

    room: Room | None = None
    error: StartGameError | None = None
    custom_duration: bool = False
    first_player_id: str | None = None


class ShootError(Enum):
    """Describe why a shot could not be performed."""

    NO_GAME = auto()
    ALREADY_PARTICIPATED = auto()
    NOT_TURN = auto()


@dataclass(slots=True)
class ShootResult:
    """Contain the outcome and next state after a shot."""

    room: Room | None = None
    error: ShootError | None = None
    hit: bool = False
    remaining_shots: int = 0
    next_player_id: str | None = None
    is_last_round: bool = False
    quote: str = ""


class GameActionError(Enum):
    """Describe why a non-shooting game action failed."""

    NO_GAME = auto()
    NOT_TURN = auto()
    LAST_ROUND_LOCKED = auto()
    PVP_GAME = auto()


@dataclass(slots=True)
class GameActionResult:
    """Contain the outcome of surrendering or ending a game."""

    room: Room | None = None
    error: GameActionError | None = None
    quote: str = ""
