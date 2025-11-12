import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, FloodWaitError, RPCError
from telethon.tl.functions.channels import GetFullChannelRequest

from models.models import Chat, ChannelCommentMapping

logger = logging.getLogger(__name__)


class CommentManager:
    """评论区映射管理器,管理频道和评论区的映射关系"""

    CACHE_DURATION = timedelta(hours=24)

    def __init__(self, session: Session, client: TelegramClient):
        self._session = session
        self._client = client

    async def get_linked_chat_id(self, channel_chat_db_id: int) -> Optional[int]:
        """获取频道关联的评论区 Chat ID (数据库ID),优先使用缓存"""
        if not channel_chat_db_id:
            logger.error("无效的 channel_chat_db_id: %s", channel_chat_db_id)
            return None

        mapping = (
            self._session.query(ChannelCommentMapping)
            .filter(ChannelCommentMapping.channel_chat_id == channel_chat_db_id)
            .one_or_none()
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
        return await self._fetch_and_update(channel_chat_db_id)

    async def _fetch_and_update(self, channel_chat_db_id: int) -> Optional[int]:
        """从 Telegram API 获取评论区信息并更新缓存"""
        channel_chat = self._session.get(Chat, channel_chat_db_id)
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
            channel_telegram_id = int(channel_chat.telegram_chat_id)
        except (TypeError, ValueError):
            logger.error(
                "无法解析频道 telegram_chat_id=%s (chat_db_id=%s)",
                channel_chat.telegram_chat_id,
                channel_chat_db_id,
            )
            return None

        try:
            channel_entity = await self._client.get_entity(channel_telegram_id)
            full_channel = await self._client(
                GetFullChannelRequest(channel=channel_entity)
            )
            linked_chat_telegram_id = getattr(
                full_channel.full_chat, "linked_chat_id", None
            )
        except FloodWaitError as exc:
            logger.error(
                "查询频道 %s 评论区触发 FloodWait, 需要等待 %s 秒: %s",
                channel_telegram_id,
                exc.seconds,
                exc,
            )
            # 向上抛出FloodWaitError,让调用方处理(而非误报为"无评论区")
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
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "未知错误导致获取频道 %s 评论区失败: %s",
                channel_telegram_id,
                exc,
            )
            return None

        if linked_chat_telegram_id is None:
            logger.info(
                "频道 %s (chat_db_id=%s) 没有关联评论区,缓存空结果",
                channel_telegram_id,
                channel_chat_db_id,
            )
            return self._persist_mapping(channel_chat_db_id, None)

        try:
            linked_chat_telegram_id = int(linked_chat_telegram_id)
        except (TypeError, ValueError):
            logger.error(
                "获取到的评论区 telegram_chat_id 非法: %s (channel_chat_db_id=%s)",
                linked_chat_telegram_id,
                channel_chat_db_id,
            )
            return None

        linked_chat = (
            self._session.query(Chat)
            .filter(Chat.telegram_chat_id == str(linked_chat_telegram_id))
            .one_or_none()
        )

        if not linked_chat:
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
                name=getattr(linked_chat_entity, "title", None)
                if linked_chat_entity
                else None,
            )
            self._session.add(linked_chat)
            try:
                self._session.flush()  # 立即获得评论区 Chat.id
            except SQLAlchemyError as exc:
                self._session.rollback()
                logger.error(
                    "保存评论区 Chat 记录失败 channel_chat_db_id=%s: %s",
                    channel_chat_db_id,
                    exc,
                )
                return None

        logger.info(
            "频道 %s (chat_db_id=%s) 关联评论区 chat_db_id=%s",
            channel_telegram_id,
            channel_chat_db_id,
            linked_chat.id,
        )
        return self._persist_mapping(channel_chat_db_id, linked_chat.id)

    def _persist_mapping(
        self, channel_chat_db_id: int, linked_chat_db_id: Optional[int]
    ) -> Optional[int]:
        """更新/创建缓存记录"""
        now = datetime.utcnow()
        mapping = (
            self._session.query(ChannelCommentMapping)
            .filter(ChannelCommentMapping.channel_chat_id == channel_chat_db_id)
            .one_or_none()
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
            self._session.add(mapping)

        try:
            self._session.commit()
            return linked_chat_db_id
        except SQLAlchemyError as exc:
            self._session.rollback()
            logger.error(
                "缓存频道评论区映射失败 channel_chat_db_id=%s: %s",
                channel_chat_db_id,
                exc,
            )
            return None
