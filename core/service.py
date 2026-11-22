from __future__ import annotations

import random

from astrbot import logger

from .config import PluginConfig
from .model import (
    GameActionError,
    GameActionResult,
    Room,
    ShootError,
    ShootResult,
    StartGameError,
    StartGameResult,
)
from .room import GameManager
from .stats import StatsManager


class RouletteGameService:
    """Coordinate roulette game state and statistics."""

    def __init__(self, config: PluginConfig):
        """Initialize the game service.

        Args:
            config: Roulette plugin configuration.
        """
        self.cfg = config
        self._rooms = GameManager()
        self._stats = StatsManager(config)

    def start_game(
        self,
        sender_id: str,
        target_id: str,
        group_id: str,
        requested_duration: int | None = None,
    ) -> StartGameResult:
        """Create a multiplayer or two-player roulette game.

        Args:
            sender_id: Player who starts the game.
            target_id: Opponent identifier, or an empty string for multiplayer.
            group_id: Group identifier.
            requested_duration: Custom mute duration requested by the player.

        Returns:
            Structured game creation result.
        """
        custom_duration = False
        if requested_duration is not None and target_id:
            custom_duration = True
            duration = requested_duration
            if duration > self.cfg.max_ban_duration:
                duration = self.cfg.max_ban_duration
                logger.info(
                    "禁言时长不能超过24小时，"
                    f"已设置为最大值 {self.cfg.max_ban_duration} 秒"
                )
        else:
            duration = random.randint(0, self.cfg.max_ban_duration)

        if sender_id == target_id:
            return StartGameResult(error=StartGameError.SELF_TARGET)

        room = self._rooms.create_room(
            kids=[sender_id, target_id, group_id],
            ban_time=duration,
        )
        if room is None:
            error = StartGameError.CREATE_FAILED
            if self._rooms.has_room(sender_id, group_id):
                error = StartGameError.SENDER_BUSY
            if target_id and self._rooms.has_room(target_id, group_id):
                error = StartGameError.TARGET_BUSY
            if not target_id and self._rooms.get_room(["", "", group_id]):
                error = StartGameError.GROUP_BUSY
            return StartGameResult(error=error)

        first_player_id = None
        if room.players and room.next_idx is not None:
            first_player_id = room.players[room.next_idx]
        logger.info(
            f"转盘游戏创建成功：子弹在第{room.bullet}轮，禁言时长为{room.ban_time}秒"
        )
        return StartGameResult(
            room=room,
            custom_duration=custom_duration,
            first_player_id=first_player_id,
        )

    def shoot(self, shooter_id: str, group_id: str) -> ShootResult:
        """Perform a shot and settle the game when the bullet fires.

        Args:
            shooter_id: Player taking the shot.
            group_id: Group identifier.

        Returns:
            Structured shot result.
        """
        room = self._rooms.get_room([shooter_id, "", group_id])
        if room is None:
            return ShootResult(error=ShootError.NO_GAME)
        if not room.can_shoot(shooter_id):
            if not room.players and shooter_id in room.participated:
                return ShootResult(error=ShootError.ALREADY_PARTICIPATED)
            return ShootResult(error=ShootError.NOT_TURN)

        hit = room.shoot(shooter_id)
        if hit:
            self._settle_loss(shooter_id, group_id, room)
            return ShootResult(
                room=room,
                hit=True,
                quote=random.choice(self.cfg.persuasion_quotes),
            )

        remaining_shots = 6 - room.round
        next_player_id = None
        if room.players and room.next_idx is not None:
            next_player_id = room.players[room.next_idx]
        return ShootResult(
            room=room,
            remaining_shots=remaining_shots,
            next_player_id=next_player_id,
            is_last_round=remaining_shots == 1,
        )

    def surrender(self, user_id: str, group_id: str) -> GameActionResult:
        """Surrender the current game and settle the loss.

        Args:
            user_id: Surrendering player identifier.
            group_id: Group identifier.

        Returns:
            Structured surrender result.
        """
        room = self._rooms.get_room([user_id, "", group_id])
        if room is None:
            return GameActionResult(error=GameActionError.NO_GAME)
        if not room.can_shoot(user_id):
            return GameActionResult(room=room, error=GameActionError.NOT_TURN)

        self._settle_loss(user_id, group_id, room)
        return GameActionResult(
            room=room,
            quote=random.choice(self.cfg.persuasion_quotes),
        )

    def exit_game(self, user_id: str, group_id: str) -> GameActionResult:
        """Exit an active game without recording a result.

        Args:
            user_id: Player requesting the exit.
            group_id: Group identifier.

        Returns:
            Structured exit result.
        """
        room = self._rooms.get_room([user_id, "", group_id])
        if room is None:
            return GameActionResult(error=GameActionError.NO_GAME)
        if 6 - room.round == 1 and room.can_shoot(user_id):
            return GameActionResult(
                room=room,
                error=GameActionError.LAST_ROUND_LOCKED,
            )

        self._rooms.del_room(group_id=group_id, players=room.players)
        return GameActionResult(room=room)

    def end_multiplayer_game(self, group_id: str) -> GameActionResult:
        """Force-end the multiplayer game in a group.

        Args:
            group_id: Group identifier.

        Returns:
            Structured force-end result.
        """
        room = self._rooms.get_room(["", "", group_id])
        if room is None:
            return GameActionResult(error=GameActionError.NO_GAME)
        if room.players:
            return GameActionResult(room=room, error=GameActionError.PVP_GAME)

        self._rooms.del_room(group_id=group_id)
        return GameActionResult(room=room)

    def expire_game(self, group_id: str, room: Room) -> None:
        """Remove a game after its inactivity timeout.

        Args:
            group_id: Group identifier.
            room: Room captured when the timeout was scheduled.
        """
        self._rooms.del_room(group_id=group_id, players=room.players)

    def auto_surrender(self, user_id: str, group_id: str, room: Room) -> None:
        """Settle a loss caused by the final-round timeout.

        Args:
            user_id: Timed-out player identifier.
            group_id: Group identifier.
            room: Active room being settled.
        """
        self._settle_loss(user_id, group_id, room)

    def get_user_stats(self, user_id: str, group_id: str = "") -> dict | None:
        """Return a player's statistics.

        Args:
            user_id: Player identifier.
            group_id: Optional group identifier.

        Returns:
            Player statistics when available.
        """
        return self._stats.get_user_stats(user_id, group_id)

    def get_pvp_stats(
        self,
        user1_id: str,
        user2_id: str,
        group_id: str = "",
    ) -> dict | None:
        """Return statistics between two players.

        Args:
            user1_id: First player identifier.
            user2_id: Second player identifier.
            group_id: Optional group identifier.

        Returns:
            PVP statistics when available.
        """
        return self._stats.get_pvp_stats(user1_id, user2_id, group_id)

    def get_top_players(
        self,
        group_id: str = "",
        min_games: int = 5,
        limit: int = 5,
    ) -> list[tuple[str, float, dict]]:
        """Return players ordered by win rate.

        Args:
            group_id: Optional group identifier.
            min_games: Minimum games required.
            limit: Maximum number of entries.

        Returns:
            Ranked player statistics.
        """
        return self._stats.get_top_players(group_id, min_games, limit)

    def get_unlucky_players(
        self,
        group_id: str = "",
        min_games: int = 5,
        limit: int = 5,
    ) -> list[tuple[str, float, dict]]:
        """Return players ordered by lowest win rate.

        Args:
            group_id: Optional group identifier.
            min_games: Minimum games required.
            limit: Maximum number of entries.

        Returns:
            Ranked player statistics.
        """
        return self._stats.get_unlucky_players(group_id, min_games, limit)

    def get_active_players(
        self,
        group_id: str = "",
        limit: int = 5,
    ) -> list[tuple[str, int, dict]]:
        """Return players ordered by game count.

        Args:
            group_id: Optional group identifier.
            limit: Maximum number of entries.

        Returns:
            Ranked player statistics.
        """
        return self._stats.get_active_players(group_id, limit)

    def _settle_loss(self, loser_id: str, group_id: str, room: Room) -> None:
        """Record a loss and remove the completed room.

        Args:
            loser_id: Losing player identifier.
            group_id: Group identifier.
            room: Room being settled.
        """
        winner_ids = [
            player_id
            for player_id in room.get_all_participants()
            if player_id != loser_id
        ]
        is_pvp = len(room.players) == 2
        if not is_pvp:
            winner_ids = []
        self._stats.record_game_result(loser_id, winner_ids, is_pvp, group_id)
        self._rooms.del_room(group_id=group_id, players=room.players)
