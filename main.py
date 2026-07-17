import json
import os
import random
import asyncio
import time
import re
import sqlite3
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "astrbot_plugin_qq_humanize",
    "user",
    "QQ个人号拟人化与群聊权限管理插件，降低风控风险",
    "1.3.2",
    "https://docs.astrbot.app",
)
class QQHumanizePlugin(Star):
    """QQ 个人号拟人化插件

    功能：
    1. 拟人化：消息发送前添加随机延迟（5秒以内），模拟打字行为，降低腾讯风控检测
    2. 群聊权限管理：可设置每个群谁能和机器人聊天（所有人 / 仅管理员 / 白名单）
    3. 聊天记录存储：SQLite 数据库，保留 1 天，自动清理过期记录
    4. AI 总结：管理员或用户可让 AI 总结群聊记录
    5. 自然语言管理：管理员可用大白话下达管理指令
    6. 无前缀指令：所有指令无需 / 前缀，直接发送即可
    7. 适配 QQ 个人号（aiocqhttp 协议）
    """

    # 模式中英文映射
    MODE_MAP = {
        "everyone": "everyone", "所有人": "everyone", "全员": "everyone", "全部": "everyone",
        "admin": "admin", "管理员": "admin", "仅管理员": "admin",
        "whitelist": "whitelist", "白名单": "whitelist",
    }
    MODE_TEXT = {"everyone": "所有人", "admin": "仅管理员", "whitelist": "白名单"}

    def __init__(self, context: Context):
        super().__init__(context)
        self.plugin_dir = os.path.dirname(__file__)
        self.config_path = os.path.join(self.plugin_dir, "config.json")
        self.db_path = os.path.join(self.plugin_dir, "chat_history.db")
        self.config = self._load_config()
        self._init_db()
        self._cleanup_expired_records()
        # 启动时输出介绍
        logger.info("=" * 50)
        logger.info("QQ拟人化插件已加载 v1.3.2")
        logger.info("发送「帮助」查看使用说明")
        logger.info("所有指令无需 / 前缀，直接发送即可")
        logger.info("新增：私聊黑名单、点赞功能")
        logger.info("=" * 50)

    # ========== 配置管理 ==========

    def _load_config(self) -> dict:
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"加载配置文件失败: {e}")
        return {
            "group_settings": {},
            "humanize_settings": {
                "min_delay": 0.5,
                "max_delay": 4.5,
                "random_typing_delay": True,
            },
        }

    def _save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    # ========== SQLite 数据库 ==========

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT,
                    message_text TEXT,
                    message_type TEXT,
                    timestamp REAL NOT NULL,
                    is_bot INTEGER DEFAULT 0
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_group_timestamp ON chat_history(group_id, timestamp)"
            )
            conn.commit()
            conn.close()
            logger.info("聊天记录数据库初始化完成")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def _cleanup_expired_records(self):
        try:
            cutoff = (datetime.now() - timedelta(hours=24)).timestamp()
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_history WHERE timestamp < ?", (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                logger.info(f"已清理 {deleted} 条过期聊天记录")
        except Exception as e:
            logger.error(f"清理过期记录失败: {e}")

    def _save_message(self, group_id: str, user_id: str, user_name: str,
                      message_text: str, message_type: str = "text",
                      is_bot: bool = False):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO chat_history (group_id, user_id, user_name, message_text, message_type, timestamp, is_bot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(group_id), str(user_id), user_name or "",
                 message_text or "", message_type, time.time(),
                 1 if is_bot else 0),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"保存消息失败: {e}")

    def _save_bot_reply(self, group_id, text: str):
        """保存机器人的回复到数据库，以便总结时包含上下文"""
        if not group_id or not text:
            return
        self._save_message(
            group_id=str(group_id), user_id="bot", user_name="机器人",
            message_text=text, message_type="text", is_bot=True,
        )

    def _get_group_messages(self, group_id: str, hours: int = 24) -> list:
        try:
            self._cleanup_expired_records()
            cutoff = (datetime.now() - timedelta(hours=hours)).timestamp()
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT user_id, user_name, message_text, timestamp, is_bot
                FROM chat_history
                WHERE group_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (str(group_id), cutoff),
            )
            rows = cursor.fetchall()
            conn.close()
            return [
                {"user_id": r[0], "user_name": r[1], "message_text": r[2],
                 "timestamp": r[3], "is_bot": r[4] == 1}
                for r in rows
            ]
        except Exception as e:
            logger.error(f"获取群消息失败: {e}")
            return []

    # ========== 拟人化处理 ==========

    async def _humanize_delay(self, text: str = ""):
        """模拟人类打字延迟（严格控制在5秒以内）"""
        settings = self.config.get("humanize_settings", {})
        min_delay = settings.get("min_delay", 0.5)
        max_delay = settings.get("max_delay", 4.5)
        max_delay = min(max_delay, 4.8)
        min_delay = max(min_delay, 0.1)
        if min_delay > max_delay:
            min_delay = max_delay
        if text and settings.get("random_typing_delay", True):
            char_delay = random.uniform(0.03, 0.1)
            base_delay = len(text) * char_delay
            delay = max(min_delay, min(base_delay, max_delay))
        else:
            delay = random.uniform(min_delay, max_delay)
        jitter = random.uniform(0, 0.2)
        total_delay = min(delay + jitter, 4.95)
        logger.info(f"拟人化延迟: {total_delay:.2f}s (文本长度: {len(text)})")
        await asyncio.sleep(total_delay)

    # ========== 群聊权限检查 ==========

    def _get_group_mode(self, group_id: str) -> str:
        group_settings = self.config.get("group_settings", {})
        return group_settings.get(str(group_id), {}).get("mode", "everyone")

    def _get_group_whitelist(self, group_id: str) -> list:
        group_settings = self.config.get("group_settings", {})
        return group_settings.get(str(group_id), {}).get("whitelist", [])

    def _set_group_mode(self, group_id: str, mode: str):
        if "group_settings" not in self.config:
            self.config["group_settings"] = {}
        gid = str(group_id)
        if gid not in self.config["group_settings"]:
            self.config["group_settings"][gid] = {}
        self.config["group_settings"][gid]["mode"] = mode
        self._save_config()

    def _add_to_whitelist(self, group_id: str, qq_id: str):
        if "group_settings" not in self.config:
            self.config["group_settings"] = {}
        gid = str(group_id)
        if gid not in self.config["group_settings"]:
            self.config["group_settings"][gid] = {}
        if "whitelist" not in self.config["group_settings"][gid]:
            self.config["group_settings"][gid]["whitelist"] = []
        if qq_id not in self.config["group_settings"][gid]["whitelist"]:
            self.config["group_settings"][gid]["whitelist"].append(qq_id)
            self._save_config()

    def _remove_from_whitelist(self, group_id: str, qq_id: str):
        gid = str(group_id)
        if "group_settings" not in self.config or gid not in self.config["group_settings"]:
            return
        wl = self.config["group_settings"][gid].get("whitelist", [])
        if qq_id in wl:
            wl.remove(qq_id)
            self._save_config()

    # ========== 私聊黑名单 ==========

    def _get_pm_blacklist(self) -> list:
        """获取私聊黑名单"""
        return self.config.get("pm_blacklist", [])

    def _add_pm_blacklist(self, qq_id: str):
        """添加私聊黑名单"""
        if "pm_blacklist" not in self.config:
            self.config["pm_blacklist"] = []
        if qq_id not in self.config["pm_blacklist"]:
            self.config["pm_blacklist"].append(qq_id)
            self._save_config()

    def _remove_pm_blacklist(self, qq_id: str):
        """移除私聊黑名单"""
        bl = self.config.get("pm_blacklist", [])
        if qq_id in bl:
            bl.remove(qq_id)
            self._save_config()

    def _is_pm_banned(self, qq_id: str) -> bool:
        """检查是否在私聊黑名单"""
        return qq_id in self._get_pm_blacklist()

    # ========== 点赞功能 ==========

    async def _send_like(self, event: AstrMessageEvent, user_id: str) -> str:
        """调用QQ接口给用户资料卡点赞

        注意：需要 aiocqhttp 协议支持，每天每人10次点赞限制
        """
        try:
            client = None
            # 多种方式获取 OneBot 客户端
            for attr in ("bot", "client", "onebot_client"):
                try:
                    c = getattr(event, attr, None)
                    if c:
                        client = c
                        break
                except Exception:
                    pass

            # 通过 context 获取平台
            if client is None:
                try:
                    platform = self.context.get_platform(event.unified_msg_origin)
                    client = getattr(platform, "bot", None) or platform
                except Exception:
                    pass

            if client is None:
                return "无法获取QQ客户端，点赞失败~（请确认使用aiocqhttp协议）"

            # 尝试多种 API 调用方式
            # NapCat/Lagrange: send_like(user_id, times)
            # go-cqhttp: send_like(user_id, times)
            errors = []
            api_attempts = [
                # 直接方法调用
                ("send_like", "method", {"user_id": int(user_id), "times": 10}),
                ("sendLike", "method", {"user_id": int(user_id), "times": 10}),
                # call_action 方式
                ("send_like", "call_action", {"user_id": int(user_id), "times": 10}),
            ]

            for api_name, call_type, params in api_attempts:
                try:
                    if call_type == "method" and hasattr(client, api_name):
                        await getattr(client, api_name)(**params)
                        return f"已经给 {user_id} 点赞啦~ 今天也是元气满满的一天!"
                    if call_type == "call_action" and hasattr(client, "call_action"):
                        await client.call_action(api_name, **params)
                        return f"已经给 {user_id} 点赞啦~ 今天也是元气满满的一天!"
                except Exception as e:
                    err_msg = str(e)[:80]
                    errors.append(f"{api_name}: {err_msg}")
                    logger.debug(f"点赞API {api_name}({call_type}) 失败: {e}")
                    continue

            # 所有方式都失败
            return (
                f"点赞失败了~ 可能原因：\n"
                f"1. 当前QQ协议端不支持点赞API\n"
                f"2. 今日点赞次数已用完（每天10次）\n"
                f"3. 该QQ不是好友或无法访问资料卡\n"
                f"错误详情: {errors[0] if errors else '无可用API'}"
            )
        except Exception as e:
            logger.error(f"点赞失败: {e}")
            return f"点赞出错了... ({str(e)[:60]})"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            role = event.message_obj.sender.role if event.message_obj and event.message_obj.sender else ""
            return role in ("owner", "admin")
        except Exception:
            return False

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        group_id = event.message_obj.group_id if event.message_obj else None
        sender_id = str(event.get_sender_id()) if event.get_sender_id() else ""
        # 私聊：检查黑名单
        if not group_id:
            if self._is_pm_banned(sender_id):
                return False
            return True
        gid = str(group_id)
        mode = self._get_group_mode(gid)
        if mode == "everyone":
            return True
        elif mode == "admin":
            if self._is_admin(event):
                return True
            return sender_id in self._get_group_whitelist(gid)
        elif mode == "whitelist":
            return sender_id in self._get_group_whitelist(gid)
        return True

    # ========== 提取消息文本 ==========

    def _extract_text(self, event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, "message_str") and event.message_str:
                return event.message_str
            if hasattr(event, "get_plain_text"):
                text = event.get_plain_text()
                if text:
                    return text
            msg_obj = event.message_obj
            if msg_obj and hasattr(msg_obj, "message"):
                msg = msg_obj.message
                if isinstance(msg, str):
                    return msg
                if isinstance(msg, list):
                    parts = []
                    for seg in msg:
                        if isinstance(seg, dict) and seg.get("type") == "text":
                            parts.append(seg.get("data", {}).get("text", ""))
                    return "".join(parts)
        except Exception as e:
            logger.debug(f"提取消息文本失败: {e}")
        return ""

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        try:
            msg_obj = event.message_obj
            if msg_obj and hasattr(msg_obj, "message"):
                msg = msg_obj.message
                if isinstance(msg, list):
                    for seg in msg:
                        if isinstance(seg, dict) and seg.get("type") == "at":
                            return True
            text = self._extract_text(event)
            if text and text.startswith("@"):
                return True
        except Exception:
            pass
        return False

    # ========== 帮助文档 ==========

    def _get_help_text(self) -> str:
        return (
            "📖 QQ拟人化插件 使用说明\n"
            "════════════════════\n"
            "✨ 所有指令无需 / 前缀，直接发送即可\n\n"
            "【🎯 群聊权限管理】（管理员）\n"
            "  设置模式 所有人 — 所有人可聊天\n"
            "  设置模式 管理员 — 仅管理员可聊天\n"
            "  设置模式 白名单 — 仅白名单可聊天\n"
            "  加白 123456 — 添加QQ到白名单\n"
            "  移除 123456 — 从白名单移除\n"
            "  群状态 — 查看当前群设置\n\n"
            "  💬 也可以@机器人用大白话说：\n"
            "   「所有人都能聊」「只有管理员能聊」\n"
            "   「把123456加入白名单」「禁止789聊天」\n\n"
            "【🚫 私聊黑名单】（管理员）\n"
            "  禁私聊 123456 — 禁止该QQ私聊机器人\n"
            "  解禁私聊 123456 — 解除私聊禁止\n"
            "  私聊黑名单 — 查看被禁止的QQ列表\n"
            "  💬 也可@机器人说「禁止123456私聊」\n\n"
            "【👍 点赞】（所有人）\n"
            "  点赞 — 给自己点赞（群聊中直接发）\n"
            "  赞我 — 给自己点赞\n"
            "  点赞 123456 — 给指定QQ点赞\n"
            "  每天10次，需NapCat/Lagrange支持\n\n"
            "【🤖 AI总结】（所有人）\n"
            "  总结 — 总结最近24小时聊天\n"
            "  总结 6 — 总结最近6小时\n\n"
            "【⏱ 拟人化设置】（管理员）\n"
            "  拟人化配置 — 查看当前配置\n"
            "  设置延迟 0.5 3 — 设置延迟范围(秒)\n"
            "  （延迟严格控制在5秒以内）\n\n"
            "【📋 其他】\n"
            "  帮助 — 显示本说明\n\n"
            "════════════════════\n"
            "💡 三种权限模式：\n"
            "  • 所有人 — 任何人都能和机器人聊\n"
            "  • 管理员 — 群管+白名单可聊\n"
            "  • 白名单 — 仅白名单用户可聊\n"
            "════════════════════"
        )

    # ========== 消息统一入口 ==========

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """统一拦截所有消息：存储、指令解析、权限检查

        处理顺序（重要）：
        1. 存储消息到数据库
        2. 管理员指令优先处理（管理员在任何模式下都能管理，避免锁死）
        3. 公共指令处理（帮助、总结，需通过权限检查）
        4. 大白话指令处理（管理员@机器人时）
        5. 权限不通过则拦截，不交给后续LLM处理
        """
        group_id = event.message_obj.group_id if event.message_obj else None
        sender_id = event.get_sender_id() if event.get_sender_id() else ""
        sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
        text = self._extract_text(event)

        # 1. 群聊消息存储（所有消息都存，不管有没有权限）
        if group_id and text:
            self._save_message(
                group_id=str(group_id), user_id=str(sender_id),
                user_name=sender_name, message_text=text,
                message_type="text", is_bot=False,
            )

        if not text:
            return

        # 2. 管理员指令优先处理（管理员在任何权限模式下都能管理，避免锁死）
        if self._is_admin(event):
            result = await self._handle_command(text, event)
            if result is not None:
                await self._humanize_delay(result)
                self._save_bot_reply(group_id, result)
                event.stop_event()
                yield event.plain_result(result)
                return

        # 3. 权限检查（非管理员需通过权限检查才能继续）
        allowed = await self._check_permission(event)
        if not allowed:
            logger.info(f"群 {group_id} 用户 {sender_id} 无权限，已忽略")
            event.stop_event()
            return

        # 4. 非管理员的公共指令（帮助、总结、点赞）
        if not self._is_admin(event):
            result = await self._handle_command(text, event)
            if result is not None:
                await self._humanize_delay(result)
                self._save_bot_reply(group_id, result)
                event.stop_event()
                yield event.plain_result(result)
                return
            # 非管理员@机器人时的点赞大白话
            if group_id and self._is_at_bot(event):
                like_result = await self._parse_like_natural(text, event)
                if like_result:
                    await self._humanize_delay(like_result)
                    self._save_bot_reply(group_id, like_result)
                    event.stop_event()
                    yield event.plain_result(like_result)
                    return

        # 5. 管理员大白话指令（@机器人时触发）
        if group_id and self._is_admin(event) and self._is_at_bot(event):
            nat_result = await self._parse_natural_command(text, event)
            if nat_result:
                await self._humanize_delay(nat_result)
                self._save_bot_reply(group_id, nat_result)
                event.stop_event()
                yield event.plain_result(nat_result)
                return

    # ========== 无前缀指令解析 ==========

    async def _handle_command(self, text: str, event: AstrMessageEvent) -> str | None:
        """解析无前缀指令，返回回复文本，None表示不是指令

        自动去掉开头的 /，支持中英文指令
        """
        raw = text.strip()
        # 兼容带 / 的输入
        if raw.startswith("/"):
            raw = raw[1:].strip()
        if not raw:
            return None

        # 取第一个词作为指令名
        parts = raw.split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:]
        group_id = str(event.message_obj.group_id) if event.message_obj and event.message_obj.group_id else ""

        # ===== 所有人可用 =====

        # 帮助（精确匹配，避免日常聊天误触发）
        if cmd in ("帮助", "help") and len(args) == 0:
            return self._get_help_text()

        # 总结
        if cmd in ("总结", "chat_summary", "聊天总结", "总结群聊"):
            hours = args[0] if args else ""
            return await self._do_summary(event, group_id, hours)

        # 点赞（所有人可用，普通用户可给自己点赞）
        if cmd in ("点赞", "like", "赞", "赞我"):
            # 点赞自己（群聊中）
            if not args or cmd == "赞我":
                sender_id = str(event.get_sender_id()) if event.get_sender_id() else ""
                if not sender_id:
                    return "无法获取你的QQ号，点赞失败~"
                return await self._send_like(event, sender_id)
            # 点赞指定QQ
            qq = args[0]
            if not re.match(r"^\d{5,15}$", qq):
                return "QQ号格式不对呀，请输入5-15位数字"
            return await self._send_like(event, qq)

        # ===== 管理员专用 =====
        if not self._is_admin(event):
            return None

        # 设置模式
        if cmd in ("setmode", "设置模式", "模式"):
            if not group_id:
                return "请在群聊中使用此指令哦"
            if not args:
                current = self._get_group_mode(group_id)
                return (f"当前群模式: {self.MODE_TEXT.get(current, current)}\n"
                        f"用法: 设置模式 <所有人|管理员|白名单>")
            mode = self.MODE_MAP.get(args[0].lower(), args[0].lower())
            if mode not in ("everyone", "admin", "whitelist"):
                return "无效模式哦，请选择: 所有人 / 管理员 / 白名单"
            self._set_group_mode(group_id, mode)
            return f"本群模式已设置为: {self.MODE_TEXT[mode]}~"

        # 添加白名单
        if cmd in ("allow", "加白", "添加白名单", "允许"):
            if not group_id:
                return "请在群聊中使用此指令哦"
            if not args:
                return "用法: 加白 <QQ号>"
            qq = args[0]
            if not re.match(r"^\d{5,15}$", qq):
                return "QQ号格式不对呀，请输入5-15位数字"
            self._add_to_whitelist(group_id, qq)
            return f"已将 {qq} 添加到本群白名单啦~"

        # 移除白名单
        if cmd in ("deny", "移除", "删除白名单", "禁止"):
            if not group_id:
                return "请在群聊中使用此指令哦"
            if not args:
                return "用法: 移除 <QQ号>"
            qq = args[0]
            if not re.match(r"^\d{5,15}$", qq):
                return "QQ号格式不对呀，请输入5-15位数字"
            self._remove_from_whitelist(group_id, qq)
            return f"已将 {qq} 从本群白名单移除啦~"

        # 群状态
        if cmd in ("group_status", "群状态", "状态"):
            if not group_id:
                return "请在群聊中使用此指令哦"
            mode = self._get_group_mode(group_id)
            whitelist = self._get_group_whitelist(group_id)
            msg = f"群 {group_id} 权限状态:\n"
            msg += f"模式: {self.MODE_TEXT.get(mode, mode)}\n"
            if whitelist:
                msg += f"白名单: {', '.join(whitelist)}\n"
            else:
                msg += "白名单: (空)\n"
            msgs = self._get_group_messages(group_id)
            msg += f"聊天记录: {len(msgs)} 条（最近24小时）"
            return msg

        # 私聊黑名单 - 禁止
        if cmd in ("禁私聊", "banpm", "禁止私聊", "封禁私聊"):
            if not args:
                return "用法: 禁私聊 <QQ号>"
            qq = args[0]
            if not re.match(r"^\d{5,15}$", qq):
                return "QQ号格式不对呀，请输入5-15位数字"
            self._add_pm_blacklist(qq)
            return f"已禁止 {qq} 私聊机器人啦~\n（该用户私聊将不会得到回复）"

        # 私聊黑名单 - 解禁
        if cmd in ("解禁私聊", "unbanpm", "允许私聊", "解封私聊"):
            if not args:
                return "用法: 解禁私聊 <QQ号>"
            qq = args[0]
            if not re.match(r"^\d{5,15}$", qq):
                return "QQ号格式不对呀，请输入5-15位数字"
            self._remove_pm_blacklist(qq)
            return f"已解除 {qq} 的私聊禁止~"

        # 私聊黑名单 - 查看列表
        if cmd in ("私聊黑名单", "pm_blacklist", "私聊封禁列表"):
            bl = self._get_pm_blacklist()
            if bl:
                return "私聊黑名单:\n" + "\n".join(f"• {qq}" for qq in bl)
            else:
                return "私聊黑名单为空~"

        # 拟人化配置
        if cmd in ("humanize_config", "拟人化配置", "配置"):
            settings = self.config.get("humanize_settings", {})
            msg = (
                f"=== 拟人化配置 ===\n"
                f"最小延迟: {settings.get('min_delay', 0.5)}s\n"
                f"最大延迟: {settings.get('max_delay', 4.5)}s\n"
                f"随机打字延迟: {'开启' if settings.get('random_typing_delay', True) else '关闭'}\n"
                f"（总延迟严格控制在 5 秒以内）"
            )
            group_settings = self.config.get("group_settings", {})
            if group_settings:
                msg += "\n\n=== 群聊权限设置 ===\n"
                for gid, conf in group_settings.items():
                    mode = conf.get("mode", "everyone")
                    msg += f"群 {gid}: {self.MODE_TEXT.get(mode, mode)}"
                    wl = conf.get("whitelist", [])
                    if wl:
                        msg += f" (白名单: {len(wl)}人)"
                    msg += "\n"
            else:
                msg += "\n\n暂无群聊权限设置（所有群默认所有人可用）"
            return msg

        # 设置延迟
        if cmd in ("humanize_delay", "设置延迟", "延迟"):
            settings = self.config.get("humanize_settings", {})
            if not args or len(args) < 2:
                return (
                    f"当前延迟范围: {settings.get('min_delay', 0.5)}s - {settings.get('max_delay', 4.5)}s\n"
                    f"用法: 设置延迟 <最小秒数> <最大秒数>\n"
                    f"示例: 设置延迟 0.5 3\n"
                    f"注意：最大延迟不超过4.8秒（确保总延迟<5秒）"
                )
            try:
                min_d = float(args[0])
                max_d = float(args[1])
                if min_d < 0 or max_d < 0 or min_d > max_d:
                    return "延迟值必须 >= 0 且最小值 <= 最大值哦"
                max_d = min(max_d, 4.8)
                if "humanize_settings" not in self.config:
                    self.config["humanize_settings"] = {}
                self.config["humanize_settings"]["min_delay"] = min_d
                self.config["humanize_settings"]["max_delay"] = max_d
                self._save_config()
                return f"延迟范围已设置为: {min_d}s - {max_d}s~"
            except ValueError:
                return "请输入有效的数字呀，例如: 设置延迟 0.5 3"

        return None

    # ========== 自然语言管理（大白话解析） ==========

    async def _parse_natural_command(self, text: str, event: AstrMessageEvent) -> str | None:
        """解析管理员的大白话指令"""
        if not self._is_admin(event):
            return None
        group_id = event.message_obj.group_id if event.message_obj else None
        if not group_id:
            return None

        text = text.strip()
        gid = str(group_id)

        # 总结
        if any(kw in text for kw in ["总结一下", "总结群聊", "聊天总结",
                                        "今天聊了什么", "聊了啥", "总结聊天",
                                        "帮我总结", "给个总结"]):
            return await self._do_summary(event, gid, "")

        # 设置模式
        if any(kw in text for kw in ["所有人都能聊", "所有人都可以", "大家都能聊",
                                        "全部开放", "所有人可聊", "谁都可以"]):
            self._set_group_mode(gid, "everyone")
            return "好哒，本群已经开放给所有人啦~大家都可以找我聊天哦"

        if any(kw in text for kw in ["只有管理员能聊", "管理员才能聊", "仅管理员",
                                        "管理员可聊", "只能管理员"]):
            self._set_group_mode(gid, "admin")
            return "明白~已经设置成只有管理员才能和我聊天啦"

        if any(kw in text for kw in ["白名单模式", "只有白名单", "白名单才能聊",
                                        "开启白名单"]):
            self._set_group_mode(gid, "whitelist")
            return "收到~已经切换到白名单模式，只有白名单里的人才能找我聊天"

        # 添加白名单
        add_patterns = [
            r"(?:把|将|加|添加)(\d{5,15})(?:加入|加到|添加到|放进)白名单",
            r"(?:白名单|加白).*?(\d{5,15})",
            r"(\d{5,15})(?:加入白名单|进白名单|加白名单)",
            r"(?:允许|批准|同意)(\d{5,15})(?:聊天|说话)",
        ]
        for pattern in add_patterns:
            match = re.search(pattern, text)
            if match:
                qq = match.group(1)
                self._add_to_whitelist(gid, qq)
                return f"好的~已经把 {qq} 加到白名单里啦"

        # 移除白名单
        remove_patterns = [
            r"(?:把|将|移除|删除|去掉)(\d{5,15})(?:从白名单|出白名单)",
            r"(?:白名单).*?(?:移除|删除|去掉).*?(\d{5,15})",
            r"(\d{5,15})(?:移除白名单|出白名单|从白名单去掉)",
            r"(?:禁止|拉黑|不让)(\d{5,15})(?:聊天|说话)",
        ]
        for pattern in remove_patterns:
            match = re.search(pattern, text)
            if match:
                qq = match.group(1)
                self._remove_from_whitelist(gid, qq)
                return f"明白~已经把 {qq} 从白名单移除了"

        # 私聊禁止（大白话）
        pm_ban_patterns = [
            r"(?:禁止|封禁|拉黑|屏蔽)(\d{5,15})(?:私聊|私信)",
            r"(\d{5,15}).*?(?:禁止|不能|不许)(?:私聊|私信)",
            r"(?:不许|不让|禁止)(\d{5,15})(?:私聊|私信|发消息)",
        ]
        for pattern in pm_ban_patterns:
            match = re.search(pattern, text)
            if match:
                qq = match.group(1)
                self._add_pm_blacklist(qq)
                return f"好的~已禁止 {qq} 私聊机器人，ta私聊我不会有回复啦"

        # 点赞（大白话，已移至公共方法 _parse_like_natural，所有人可用）

        return None

    async def _parse_like_natural(self, text: str, event: AstrMessageEvent) -> str | None:
        """解析点赞大白话（所有人可用，@机器人时触发）"""
        text = text.strip()
        # 给自己点赞
        if any(kw in text for kw in ["给我点赞", "赞我", "点个赞", "给我点个赞", "帮我点赞"]):
            sender_id = str(event.get_sender_id()) if event.get_sender_id() else ""
            if not sender_id:
                return "无法获取你的QQ号，点赞失败~"
            return await self._send_like(event, sender_id)
        # 给指定QQ点赞
        like_patterns = [
            r"(?:给|帮)(\d{5,15})(?:点赞|赞一个|赞)",
            r"(?:点赞|赞一个|赞)(\d{5,15})",
        ]
        for pattern in like_patterns:
            match = re.search(pattern, text)
            if match:
                qq = match.group(1)
                return await self._send_like(event, qq)
        return None

    # ========== AI 总结功能 ==========

    async def _do_summary(self, event: AstrMessageEvent, group_id: str, hours_str: str) -> str:
        """执行总结流程"""
        if not group_id:
            return "请在群聊中使用总结功能哦~"
        h = 24
        if hours_str:
            try:
                h = int(hours_str)
                if h <= 0 or h > 24:
                    h = 24
            except ValueError:
                pass
        return await self._summarize_chat(event, group_id, hours=h)

    async def _summarize_chat(self, event: AstrMessageEvent, group_id: str, hours: int = 24) -> str:
        """使用 AI 总结群聊记录"""
        messages = self._get_group_messages(group_id, hours=hours)
        if not messages:
            return f"最近{hours}小时内没有聊天记录哦~没法总结呢"

        # 计算时间范围
        now = datetime.now()
        start_time = now - timedelta(hours=hours)
        now_str = now.strftime("%Y-%m-%d %H:%M")
        start_str = start_time.strftime("%Y-%m-%d %H:%M")

        chat_lines = []
        user_count = 0
        for msg in messages:
            time_str = datetime.fromtimestamp(msg["timestamp"]).strftime("%m-%d %H:%M")
            if msg["is_bot"]:
                name = "机器人"
            else:
                name = msg["user_name"] or f"用户{msg['user_id']}"
                user_count += 1
            # 截断过长的单条消息
            text = msg['message_text'][:200]
            chat_lines.append(f"[{time_str}] {name}: {text}")

        chat_text = "\n".join(chat_lines)
        if user_count == 0:
            return f"最近{hours}小时内没有有效的用户聊天记录呢"

        if len(chat_text) > 8000:
            chat_text = chat_text[-8000:]

        try:
            umo = event.unified_msg_origin
            provider_id = None
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                pass
            if not provider_id:
                try:
                    provider_id = self.context.provider_manager.get_default_provider_id()
                except Exception:
                    pass
            if not provider_id:
                return "抱歉呀，AI 服务暂时不可用，没法帮你总结了呢"

            prompt = f"""你是一个群聊总结助手。请根据下面的群聊记录，总结聊天内容。

时间范围：{start_str} 至 {now_str}（最近{hours}小时）
参与用户数：{user_count} 人
消息总数：{len(messages)} 条

要求：
1. 先说明这是哪段时间的聊天记录
2. 分点列出主要话题和讨论内容
3. 如果有重要的决定或者共识，特别提一下
4. 语气自然简洁，不要过度寒暄

群聊记录：
{chat_text}

请给出总结："""

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt,
            )
            summary = llm_resp.completion_text if llm_resp and hasattr(llm_resp, "completion_text") else ""
            if not summary:
                return "抱歉呀，AI 好像走神了，没总结出来呢"
            return summary.strip()
        except Exception as e:
            logger.error(f"AI总结失败: {e}")
            return f"总结失败了... ({str(e)[:50]})"

    # ========== 生命周期 ==========

    async def terminate(self):
        self._save_config()
        self._cleanup_expired_records()
        logger.info("QQ拟人化插件已卸载")
