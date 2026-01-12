import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, FloodWaitError, RPCError
from telethon.tl.functions.channels import GetFullChannelRequest

from models.models import Chat, ChannelCommentMapping, get_session

logger = logging.getLogger(__name__)


class CommentManager:
    """评论区映射管理器,管理频道和评论区的映射关系

    注意: 此类使用独立的短事务进行数据库操作，避免跨 await 持有连接
    """

    CACHE_DURATION = timedelta(hours=24)

    def __init__(self, client: TelegramClient):
        self._client = client

    async def get_linked_chat_id(self, channel_chat_db_id: int) -> Optional[int]:
        """获取频道关联的评论区 Chat ID (数据库ID),优先使用缓存"""
        if not channel_chat_db_id:
            logger.error("无效的 channel_chat_db_id: %s", channel_chat_db_id)
            return None

        # 第一个短事务：读取缓存和频道信息
        cache_result = self._check_cache(channel_chat_db_id)
        if cache_result is not None:
            return cache_result

        channel_telegram_id = self._get_channel_telegram_id(channel_chat_db_id)
        if channel_telegram_id is None:
            return None

        # await Telegram API（不持有 DB 连接）
        api_result = await self._fetch_from_telegram(channel_telegram_id, channel_chat_db_id)
        if api_result is None:
            return None

        linked_chat_telegram_id, full_channel = api_result

        # 第二个短事务：写入映射
        return self._save_mapping(channel_chat_db_id, linked_chat_telegram_id, full_channel)

    def _check_cache(self, channel_chat_db_id: int) -> Optional[int]:
        """检查缓存，返回 linked_chat_id 或 None（缓存未命中返回 -1 表示需要刷新）"""
        session = get_session()
        try:
            mapping = (
                session.query(ChannelCommentMapping)
                .filter(ChannelCommentMapping.channel_chat_id == channel_chat_db_id)
                .first()
            )

            if mapping:
                last_checked = mapping.last_checked or datetime.min
                if datetime.utcnow() - last_checked < self.CACHE_DURATION:
                    logger.info(
                        "命中频道评论区缓存 channel_chat_db_id=%s, linked_chat_id=%s",
                        channel_chat_db_id,
                        mapping.linked_chat_id,
                    )
                    return mapping.linked_chat_id

            logger.info("缓存失效/缺失,刷新频道 %s 的评论区映射", channel_chat_db_id)
            return None  # 需要刷新
        finally:
            session.close()

    def _get_channel_telegram_id(self, channel_chat_db_id: int) -> Optional[int]:
        """获取频道的 telegram_chat_id"""
        session = get_session()
        try:
            channel_chat = session.query(Chat).filter(Chat.id == channel_chat_db_id).first()
            if not channel_chat:
                logger.error("未找到频道 Chat 记录 channel_chat_db_id=%s", channel_chat_db_id)
                return None

            if not channel_chat.telegram_chat_id:
                logger.error(
                    "频道 Chat 缺少 telegram_chat_id, channel_chat_db_id=%s",
                    channel_chat_db_id,
                )
                return None

            try:
                return int(channel_chat.telegram_chat_id)
            except (TypeError, ValueError):
                logger.error(
                    "无法解析频道 telegram_chat_id=%s (chat_db_id=%s)",
                    channel_chat.telegram_chat_id,
                    channel_chat_db_id,
                )
                return None
        finally:
            session.close()

    async def _fetch_from_telegram(
        self, channel_telegram_id: int, channel_chat_db_id: int
    ) -> Optional[Tuple[Optional[int], object]]:
        """从 Telegram API 获取评论区信息，返回 (linked_chat_telegram_id, full_channel)"""
        try:
            channel_entity = await self._client.get_entity(channel_telegram_id)
            full_channel = await self._client(
                GetFullChannelRequest(channel=channel_entity)
            )
            linked_chat_telegram_id = getattr(
                full_channel.full_chat, "linked_chat_id", None
            )
            return (linked_chat_telegram_id, full_channel)
        except FloodWaitError as exc:
            logger.error(
                "查询频道 %s 评论区触发 FloodWait, 需要等待 %s 秒: %s",
                channel_telegram_id,
                exc.seconds,
                exc,
            )
            raise
        except ChannelPrivateError as exc:
            logger.error(
                "无法访问频道 %s (chat_db_id=%s): %s",
                channel_telegram_id,
                channel_chat_db_id,
                exc,
            )
            return None
        except RPCError as exc:
            logger.error(
                "调用 Telegram API 获取频道 %s 评论区失败: %s",
                channel_telegram_id,
                exc,
            )
            return None
        except Exception as exc:
            logger.error(
                "未知错误导致获取频道 %s 评论区失败: %s",
                channel_telegram_id,
                exc,
                exc_info=True,
            )
            return None

    def _save_mapping(
        self, channel_chat_db_id: int, linked_chat_telegram_id: Optional[int], full_channel: object
    ) -> Optional[int]:
        """保存映射到数据库"""
        session = get_session()
        try:
            if linked_chat_telegram_id is None:
                logger.info(
                    "频道 (chat_db_id=%s) 没有关联评论区,缓存空结果",
                    channel_chat_db_id,
                )
                return self._persist_mapping(session, channel_chat_db_id, None)

            try:
                linked_chat_telegram_id = int(linked_chat_telegram_id)
            except (TypeError, ValueError):
                logger.error(
                    "获取到的评论区 telegram_chat_id 非法: %s (channel_chat_db_id=%s)",
                    linked_chat_telegram_id,
                    channel_chat_db_id,
                )
                return None

            # 查找或创建评论区 Chat
            linked_chat = (
                session.query(Chat)
                .filter(Chat.telegram_chat_id == str(linked_chat_telegram_id))
                .first()
            )

            if not linked_chat:
                linked_chat = self._create_linked_chat(
                    session, linked_chat_telegram_id, full_channel, channel_chat_db_id
                )
                if linked_chat is None:
                    return None

            logger.info(
                "频道 (chat_db_id=%s) 关联评论区 chat_db_id=%s",
                channel_chat_db_id,
                linked_chat.id,
            )
            return self._persist_mapping(session, channel_chat_db_id, linked_chat.id)
        finally:
            session.close()

    def _create_linked_chat(
        self, session, linked_chat_telegram_id: int, full_channel: object, channel_chat_db_id: int
    ) -> Optional[Chat]:
        """创建评论区 Chat 记录，处理并发冲突"""
        linked_chat_entity = next(
            (
                chat
                for chat in getattr(full_channel, "chats", [])
                if getattr(chat, "id", None) == linked_chat_telegram_id
            ),
            None,
        )

        linked_chat = Chat(
            telegram_chat_id=str(linked_chat_telegram_id),
            name=getattr(linked_chat_entity, "title", None) if linked_chat_entity else None,
        )
        session.add(linked_chat)
        try:
            session.flush()
            return linked_chat
        except IntegrityError:
            # 并发冲突：其他进程已创建，回滚后重新查询
            session.rollback()
            logger.info("Chat 已存在(并发创建)，重新查询 telegram_chat_id=%s", linked_chat_telegram_id)
            return (
                session.query(Chat)
                .filter(Chat.telegram_chat_id == str(linked_chat_telegram_id))
                .first()
            )
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(
                "保存评论区 Chat 记录失败 channel_chat_db_id=%s: %s",
                channel_chat_db_id,
                exc,
                exc_info=True,
            )
            return None

    def _persist_mapping(
        self, session, channel_chat_db_id: int, linked_chat_db_id: Optional[int]
    ) -> Optional[int]:
        """更新/创建缓存记录，处理并发冲突"""
        now = datetime.utcnow()
        mapping = (
            session.query(ChannelCommentMapping)
            .filter(ChannelCommentMapping.channel_chat_id == channel_chat_db_id)
            .first()
        )

        if mapping:
            mapping.linked_chat_id = linked_chat_db_id
            mapping.last_checked = now
        else:
            mapping = ChannelCommentMapping(
                channel_chat_id=channel_chat_db_id,
                linked_chat_id=linked_chat_db_id,
                last_checked=now,
            )
            session.add(mapping)

        try:
            session.commit()
            return linked_chat_db_id
        except IntegrityError:
            # 并发冲突：其他进程已创建映射，回滚后重新查询
            session.rollback()
            logger.info("映射已存在(并发创建)，重新查询 channel_chat_db_id=%s", channel_chat_db_id)
            existing = (
                session.query(ChannelCommentMapping)
                .filter(ChannelCommentMapping.channel_chat_id == channel_chat_db_id)
                .first()
            )
            return existing.linked_chat_id if existing else None
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error(
                "缓存频道评论区映射失败 channel_chat_db_id=%s: %s",
                channel_chat_db_id,
                exc,
                exc_info=True,
            )
            return None
