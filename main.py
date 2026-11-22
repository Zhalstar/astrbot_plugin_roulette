import asyncio

from astrbot import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import At as Comp_At
from astrbot.api.message_components import Plain as Comp_Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.config import PluginConfig
from .core.model import (
    GameActionError,
    Room,
    ShootError,
    StartGameError,
)
from .core.service import RouletteGameService
from .core.utils import ban, get_at_id, get_name


class RoulettePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config)
        self.game_service = RouletteGameService(self.cfg)
        # 用于存储超时任务，键为 group_id，值为 asyncio.Task 对象
        self.timeout_tasks: dict[str, asyncio.Task] = {}
        # 用于存储游戏超时任务
        self.game_timeout_tasks: dict[str, asyncio.Task] = {}

    def _set_game_timeout(self, group_id: str, room: Room) -> None:
        """设置游戏超时任务。

        Args:
            group_id: 群组 ID。
            room: 当前游戏房间。
        """
        if self.cfg.game_timeout <= 0:
            return

        # 取消已有的超时任务
        if group_id in self.game_timeout_tasks:
            self.game_timeout_tasks[group_id].cancel()

        async def game_timeout_coro():
            try:
                await asyncio.sleep(self.cfg.game_timeout)
                logger.info(f"群 {group_id} 的游戏超时，自动结束")
                self.game_service.expire_game(group_id, room)

                if group_id in self.game_timeout_tasks:
                    del self.game_timeout_tasks[group_id]
            except asyncio.CancelledError:
                logger.info(f"群 {group_id} 的游戏超时任务被取消")

        self.game_timeout_tasks[group_id] = asyncio.create_task(game_timeout_coro())

    def _cancel_timeout_tasks(self, group_id: str) -> None:
        """Cancel all timeout tasks associated with a group.

        Args:
            group_id: Group identifier.
        """
        if task := self.timeout_tasks.pop(group_id, None):
            task.cancel()
        if task := self.game_timeout_tasks.pop(group_id, None):
            task.cancel()

    @filter.command("转盘", alias={"轮盘", "开启转盘"})
    async def start_wheel(self, event: AstrMessageEvent):
        """转盘@某人 [秒数] 不@表示进入多人模式"""
        args = event.message_str.split()

        target_id = get_at_id(event)
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        requested_duration = None
        if len(args) >= 2 and args[-1].isdigit():
            requested_duration = int(args[-1])

        result = self.game_service.start_game(
            sender_id=sender_id,
            target_id=target_id,
            group_id=group_id,
            requested_duration=requested_duration,
        )
        if result.error is not None:
            error_messages = {
                StartGameError.SELF_TARGET: "不能和自己玩哦！",
                StartGameError.SENDER_BUSY: "你在游戏中...",
                StartGameError.TARGET_BUSY: "对方游戏中...",
                StartGameError.GROUP_BUSY: "本群游戏中...",
                StartGameError.CREATE_FAILED: "",
            }
            yield event.plain_result(error_messages[result.error])
            return

        room = result.room
        assert room is not None

        if room.players:
            user_name = await get_name(event, sender_id)
            target_name = await get_name(event, target_id) if target_id else ""
            # 使用chain格式，@随机先手并说明规则
            chain = []
            first_player_id = result.first_player_id or sender_id
            if result.custom_duration:
                text = f"🎲 {user_name} VS {target_name}\n双人转盘对决开始！惩罚时长：{room.ban_time}秒\n随机先手，"
            else:
                text = (
                    f"🎲 {user_name} VS {target_name}\n双人转盘对决开始！\n随机先手，"
                )
            chain.append(Comp_Plain(text))
            chain.append(Comp_At(qq=first_player_id))
            chain.append(Comp_Plain(" 请先开枪！"))
            yield event.chain_result(chain)
        else:
            if result.custom_duration:
                yield event.plain_result(
                    f"本群转盘开始，惩罚时长：{room.ban_time}秒，请开枪！"
                )
            else:
                yield event.plain_result("本群转盘开始，请开枪！")

        self._set_game_timeout(group_id, room)

    @filter.command("开枪")
    async def shoot_wheel(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        result = self.game_service.shoot(sender_id, group_id)
        if result.error is not None:
            error_messages = {
                ShootError.NO_GAME: "请先开启转盘",
                ShootError.ALREADY_PARTICIPATED: "你已经参与过本局游戏了！",
                ShootError.NOT_TURN: "本轮不是你的回合",
            }
            yield event.plain_result(error_messages[result.error])
            return

        self._cancel_timeout_tasks(group_id)
        room = result.room
        assert room is not None
        user_name = await get_name(event, sender_id)

        if result.hit:
            await ban(event, room.ban_time)
            reply = f"Bang！{user_name}被禁言{room.ban_time}秒！{result.quote}"
            yield event.plain_result(reply)
        else:
            reply = f"【{user_name}】开了一枪没响，还剩【{result.remaining_shots}】发"

            if result.next_player_id:
                if result.is_last_round:
                    # 最后一发，@玩家并给出警告
                    chain = []
                    chain.append(Comp_Plain(reply + "，"))
                    chain.append(Comp_At(qq=result.next_player_id))
                    chain.append(
                        Comp_Plain(
                            "，你只剩下最后一发子弹，命运掌握在自己手中！\n请在3分钟内【开枪】或【认输】，否则将自动判负。"
                        )
                    )
                    yield event.chain_result(chain)
                else:
                    # 普通回合，@下一个玩家
                    chain = []
                    chain.append(Comp_Plain(reply + "，"))
                    chain.append(Comp_At(qq=result.next_player_id))
                    chain.append(Comp_Plain(" 该你了！"))
                    yield event.chain_result(chain)

                if result.is_last_round:
                    task = asyncio.create_task(
                        self._task_auto_surrender(
                            event,
                            result.next_player_id,
                            group_id,
                            room,
                        )
                    )
                    self.timeout_tasks[group_id] = task
            else:
                # 多人模式，没有指定下一个玩家
                yield event.plain_result(reply)

            self._set_game_timeout(group_id, room)

    async def _task_auto_surrender(
        self,
        event: AstrMessageEvent,
        next_player_id: str,
        group_id: str,
        room: Room,
    ):
        """最后一发超时后自动认输。

        Args:
            event: AstrBot 消息事件。
            next_player_id: 超时玩家 ID。
            group_id: 群组 ID。
            room: 当前游戏房间。
        """
        try:
            player_name = await get_name(event, next_player_id)
            await asyncio.sleep(180)
            logger.info(
                f"玩家 {player_name}({next_player_id}) 在群 {group_id} 的游戏超时。"
            )
            self.game_service.auto_surrender(next_player_id, group_id, room)
            await ban(event, room.ban_time, user_id=next_player_id)

            timeout_reply = (
                f"玩家 {player_name} 在命运抉择面前犹豫了过久，已降下神罚！\n"
                f"被禁言 {room.ban_time} 秒！"
            )
            await event.send(MessageChain([Comp_Plain(timeout_reply)]))

            if group_id in self.timeout_tasks:
                del self.timeout_tasks[group_id]
            if group_id in self.game_timeout_tasks:
                self.game_timeout_tasks[group_id].cancel()
                del self.game_timeout_tasks[group_id]
        except asyncio.CancelledError:
            logger.info(f"群 {group_id} 的超时任务被取消。")
        except Exception as e:
            logger.error(f"执行超时自动认输任务时出错: {e}", exc_info=True)

    @filter.command("认输", alias={"玩不起"})
    async def surrender_game(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        result = self.game_service.surrender(user_id, group_id)
        if result.error is not None:
            error_messages = {
                GameActionError.NO_GAME: "你没有正在进行的转盘游戏",
                GameActionError.NOT_TURN: "还没轮到你，不能认输哦！",
            }
            yield event.plain_result(error_messages[result.error])
            return

        self._cancel_timeout_tasks(group_id)
        room = result.room
        assert room is not None
        user_name = await get_name(event, user_id)
        await ban(event, room.ban_time)
        reply = (
            f"{user_name} 选择了认输，直面惩罚！"
            f"被禁言 {room.ban_time} 秒！{result.quote}"
        )
        yield event.plain_result(reply)

    @filter.command("退出", alias={"结束游戏"})
    async def exit_game(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        result = self.game_service.exit_game(user_id, group_id)
        if result.error is not None:
            error_messages = {
                GameActionError.NO_GAME: "你没有正在进行的转盘游戏",
                GameActionError.LAST_ROUND_LOCKED: (
                    "只剩最后一发，命运已定，无法退出！请选择【开枪】或【认输】。"
                ),
            }
            yield event.plain_result(error_messages[result.error])
            return

        self._cancel_timeout_tasks(group_id)
        yield event.plain_result("游戏已由玩家主动退出，无人受罚。")

    @filter.command("结束转盘")
    async def admin_end_game(self, event: AstrMessageEvent):
        """管理员强制结束当前群的多人转盘游戏（不影响双人对决）"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用此指令")
            return

        result = self.game_service.end_multiplayer_game(group_id)
        if result.error is not None:
            error_messages = {
                GameActionError.NO_GAME: "当前群没有进行中的转盘游戏",
                GameActionError.PVP_GAME: (
                    "当前是双人对决模式，无法强制结束。请让玩家自行【退出】或【认输】。"
                ),
            }
            yield event.plain_result(error_messages[result.error])
            return

        self._cancel_timeout_tasks(group_id)
        yield event.plain_result("管理员已强制结束当前群的多人转盘游戏，无人受罚。")

    @filter.command("我的战绩", alias={"转盘战绩", "查看战绩"})
    async def my_stats(self, event: AstrMessageEvent):
        """查看个人转盘战绩"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        stats = self.game_service.get_user_stats(user_id, group_id)

        if not stats:
            yield event.plain_result("你还没有参与过转盘游戏哦！")
            return

        user_name = await get_name(event, user_id)
        total = stats["total"]
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = (wins / total * 100) if total > 0 else 0
        max_streak = stats["max_win_streak"]
        current_streak = stats["current_streak"]

        reply = f"🎰 {user_name} 的转盘战绩\n\n"
        reply += f"参与局数: {total}\n"
        reply += f"获胜次数: {wins} (未中枪)\n"
        reply += f"失败次数: {losses} (中枪)\n"
        reply += f"胜率: {win_rate:.1f}%\n"
        reply += f"最高连胜: {max_streak} 连胜\n"
        reply += (
            f"当前连胜: {current_streak} 连胜" if current_streak > 0 else "当前连胜: 0"
        )

        yield event.plain_result(reply)

    @filter.command("对战记录")
    async def pvp_stats(self, event: AstrMessageEvent):
        """查看与某人的对战记录，需要@对方"""
        sender_id = event.get_sender_id()
        target_id = get_at_id(event)

        if not target_id:
            yield event.plain_result("请@你想查看对战记录的人")
            return

        if sender_id == target_id:
            yield event.plain_result("不能查看与自己的对战记录哦！")
            return

        group_id = event.get_group_id()
        pvp_stats = self.game_service.get_pvp_stats(sender_id, target_id, group_id)

        if not pvp_stats:
            sender_name = await get_name(event, sender_id)
            target_name = await get_name(event, target_id)
            yield event.plain_result(f"{sender_name} 和 {target_name} 还没有对战记录")
            return

        sender_name = await get_name(event, sender_id)
        target_name = await get_name(event, target_id)

        total = pvp_stats["total"]
        sender_wins = pvp_stats.get(f"{sender_id}_wins", 0)
        target_wins = pvp_stats.get(f"{target_id}_wins", 0)
        sender_win_rate = pvp_stats.get(f"{sender_id}_win_rate", 0)
        target_win_rate = pvp_stats.get(f"{target_id}_win_rate", 0)

        reply = "⚔️ 对战记录\n\n"
        reply += f"{sender_name} VS {target_name}\n\n"
        reply += f"总对战: {total} 局\n"
        reply += f"{sender_name} 获胜: {sender_wins} 局\n"
        reply += f"{target_name} 获胜: {target_wins} 局\n"

        if sender_wins > target_wins:
            reply += f"\n{sender_name} 以胜率 {sender_win_rate:.1f}% 暂时领先！"
        elif target_wins > sender_wins:
            reply += f"\n{target_name} 以胜率 {target_win_rate:.1f}% 暂时领先！"
        else:
            reply += "\n双方势均力敌！"

        yield event.plain_result(reply)

    @filter.command("赌圣榜", alias={"赌圣排行榜", "胜率排行"})
    async def top_players(self, event: AstrMessageEvent):
        """查看胜率排行榜（至少参与5局）"""
        group_id = event.get_group_id()
        # 获取全局前 100 名，然后从中筛选出在当前群的
        all_top_list = self.game_service.get_top_players(
            group_id="",
            min_games=5,
            limit=100,
        )

        qualified_list = []
        for user_id, win_rate, stats in all_top_list:
            user_name = await get_name(event, user_id, group_id)
            if user_name:
                qualified_list.append((user_id, win_rate, stats, user_name))
            if len(qualified_list) >= 5:
                break

        if not qualified_list:
            yield event.plain_result("当前群暂时还没有符合条件的赌圣（至少参与5局）")
            return

        reply = "🏆 赌圣排行榜 TOP5\n\n"

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

        for idx, (user_id, win_rate, stats, user_name) in enumerate(qualified_list):
            total = stats["total"]
            wins = stats["wins"]
            max_streak = stats["max_win_streak"]

            reply += f"{medals[idx]} {user_name}\n"
            reply += f"   胜率: {win_rate * 100:.1f}% ({wins}/{total})\n"
            reply += f"   最高连胜: {max_streak}\n\n"

        yield event.plain_result(reply)

    @filter.command("散财榜", alias={"散财排行榜", "倒霉榜", "输家榜"})
    async def unlucky_players(self, event: AstrMessageEvent):
        """查看散财排行榜（胜率最低，至少参与5局）"""
        group_id = event.get_group_id()
        # 获取全局排名，筛选出在当前群的
        all_top_list = self.game_service.get_unlucky_players(
            group_id="", min_games=5, limit=100
        )

        qualified_list = []
        for user_id, win_rate, stats in all_top_list:
            user_name = await get_name(event, user_id, group_id)
            if user_name:
                qualified_list.append((user_id, win_rate, stats, user_name))
            if len(qualified_list) >= 5:
                break

        if not qualified_list:
            yield event.plain_result(
                "当前群暂时还没有符合条件的散财达人（至少参与5局）"
            )
            return

        reply = "💸 散财排行榜 TOP5\n\n"

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

        for idx, (user_id, win_rate, stats, user_name) in enumerate(qualified_list):
            wins = stats["wins"]
            losses = stats["losses"]

            reply += f"{medals[idx]} {user_name}\n"
            reply += f"   胜率: {win_rate * 100:.1f}% (胜{wins}/负{losses})\n\n"

        yield event.plain_result(reply)

    @filter.command("赌狗榜", alias={"赌狗排行榜"})
    async def active_players(self, event: AstrMessageEvent):
        """查看赌狗排行榜（参与局数最多）"""
        group_id = event.get_group_id()
        # 获取全局排名，筛选出在当前群的
        all_top_list = self.game_service.get_active_players(group_id="", limit=100)

        qualified_list = []
        for user_id, total, stats in all_top_list:
            user_name = await get_name(event, user_id, group_id)
            if user_name:
                qualified_list.append((user_id, total, stats, user_name))
            if len(qualified_list) >= 5:
                break

        if not qualified_list:
            yield event.plain_result("暂时还没有战绩记录")
            return

        reply = "🐶 赌狗排行榜 TOP5\n\n"

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

        for idx, (user_id, total, stats, user_name) in enumerate(qualified_list):
            wins = stats["wins"]
            losses = stats["losses"]

            reply += f"{medals[idx]} {user_name}\n"
            reply += f"   总局数: {total} (胜{wins}/负{losses})\n\n"

        yield event.plain_result(reply)
