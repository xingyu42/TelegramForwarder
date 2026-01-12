import logging
import os
import pytz
import asyncio
from utils.constants import TEMP_DIR
from utils.media import get_max_media_size

from filters.base_filter import BaseFilter

logger = logging.getLogger(__name__)

class InitFilter(BaseFilter):
    """
    初始化过滤器，为context添加基本信息
    """
    
    async def _process(self, context):
        """
        添加原始链接和发送者信息
        
        Args:
            context: 消息上下文
            
        Returns:
            bool: 是否继续处理
        """
        rule = context.rule
        event = context.event

        # logger.info(f"InitFilter处理消息前，context: {context.__dict__}")
        try:
            #处理媒体组消息
            if event.message.grouped_id:
                # 等待更长时间让所有媒体消息到达
                # await asyncio.sleep(1)
                
                # 收集媒体组的所有消息
                try:
                    async for message in event.client.iter_messages(
                        event.chat_id,
                        limit=20,
                        min_id=event.message.id - 10,
                        max_id=event.message.id + 10
                    ):
                        if message.grouped_id == event.message.grouped_id:
                            if message.text:    
                                # 保存第一条消息的文本和按钮
                                context.message_text = message.text or ''
                                context.original_message_text = message.text or ''
                                context.check_message_text = message.text or ''
                                context.buttons = message.buttons if hasattr(message, 'buttons') else None
                            logger.info(f'获取到媒体组文本并添加到context: {message.text}')
                        
                except Exception as e:
                    logger.error(f'收集媒体组消息时出错: {str(e)}')
                    context.errors.append(f"收集媒体组消息错误: {str(e)}")
            # 检测评论区消息(通过 reply_to 判断)
            # 仅当已标记为评论区消息时才进行详细检测（避免不必要的 API 调用）
            if context.comment_metadata.get('is_comment') and event.message.reply_to and hasattr(event.message.reply_to, 'reply_to_msg_id'):
                try:
                    replied_message = await event.message.get_reply_message()
                    if replied_message and hasattr(replied_message, 'fwd_from') and replied_message.fwd_from:
                        fwd_from = replied_message.fwd_from
                        if hasattr(fwd_from, 'from_id') and fwd_from.from_id and hasattr(fwd_from.from_id, 'channel_id'):
                            original_message_id = getattr(fwd_from, 'channel_post', None)
                            if original_message_id:
                                context.comment_metadata['original_message_id'] = original_message_id
                                logger.info(f'检测到评论区消息: 原消息 ID {original_message_id}')
                except Exception as e:
                    logger.error(f'检测评论区消息时出错: {str(e)}')
           
        finally:
            # logger.info(f"InitFilter处理消息后，context: {context.__dict__}")
            return True
