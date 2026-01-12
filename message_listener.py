from telethon import events
from models.models import get_session, Chat, ForwardRule, ChannelCommentMapping
import logging
from handlers import user_handler, bot_handler
from handlers.prompt_handlers import handle_prompt_setting
import asyncio
import os
from dotenv import load_dotenv
from telethon.tl.types import ChannelParticipantsAdmins
from managers.state_manager import state_manager
from telethon.tl import types
from filters.process import process_forward_rule
from utils.comment_manager import CommentManager
# 加载环境变量
load_dotenv()

# 获取logger
logger = logging.getLogger(__name__)

# 添加一个缓存来存储已处理的媒体组
PROCESSED_GROUPS = set()

BOT_ID = None

async def setup_listeners(user_client, bot_client):
    """
    设置消息监听器
    
    Args:
        user_client: 用户客户端（用于监听消息和转发）
        bot_client: 机器人客户端（用于处理命令和转发）
    """
    global BOT_ID
    
    # 直接获取机器人ID
    try:
        me = await bot_client.get_me()
        BOT_ID = me.id
        logger.info(f"获取到机器人ID: {BOT_ID} (类型: {type(BOT_ID)})")
    except Exception as e:
        logger.error(f"获取机器人ID时出错: {str(e)}")
    
    # 过滤器，排除机器人自己的消息
    async def not_from_bot(event):
        if BOT_ID is None:
            return True  # 如果未获取到机器人ID，不进行过滤
        
        sender = event.sender_id
        try:
            sender_id = int(sender) if sender is not None else None
            is_not_bot = sender_id != BOT_ID
            if not is_not_bot:
                logger.info(f"过滤器识别到机器人消息，忽略处理: {sender_id}")
            return is_not_bot
        except (ValueError, TypeError):
            return True  # 转换失败时不过滤
    
    # 用户客户端监听器 - 使用过滤器，避免处理机器人消息
    @user_client.on(events.NewMessage(func=not_from_bot))
    async def user_message_handler(event):
        await handle_user_message(event, user_client, bot_client)
    
    # 机器人客户端监听器 - 使用过滤器
    @bot_client.on(events.NewMessage(func=not_from_bot))
    async def bot_message_handler(event):
        # logger.info(f"机器人收到非自身消息, 发送者ID: {event.sender_id}")
        await handle_bot_message(event, bot_client)
        
    # 注册机器人回调处理器
    bot_client.add_event_handler(bot_handler.callback_handler)

async def handle_user_message(event, user_client, bot_client):
    """处理用户客户端收到的消息"""
    # logger.info("handle_user_message:开始处理用户消息")
    
    chat = await event.get_chat()
    chat_id = abs(chat.id)
    # logger.info(f"handle_user_message:获取到聊天ID: {chat_id}")

    # 检查是否频道消息
    if isinstance(event.chat, types.Channel) and state_manager.check_state():
        # logger.info("handle_user_message:检测到频道消息且存在状态")
        sender_id = os.getenv('USER_ID')
        # 频道ID需要加上100前缀
        chat_id = int(f"100{chat_id}")
        # logger.info(f"handle_user_message:频道消息处理: sender_id={sender_id}, chat_id={chat_id}")
    else:
        sender_id = event.sender_id
        # logger.info(f"handle_user_message:非频道消息处理: sender_id={sender_id}")

    # 检查用户状态
    current_state, message, state_type = state_manager.get_state(sender_id, chat_id)
    # logger.info(f'handle_user_message：当前是否有状态: {state_manager.check_state()}')
    # logger.info(f"handle_user_message：当前用户ID和聊天ID: {sender_id}, {chat_id}")
    # logger.info(f"handle_user_message：获取当前聊天窗口的用户状态: {current_state}")
    
    if current_state:
        # logger.info(f"检测到用户状态: {current_state}")
        # 处理提示词设置
        # logger.info("准备处理提示词设置")
        if await handle_prompt_setting(event, bot_client, sender_id, chat_id, current_state, message):
            # logger.info("提示词设置处理完成，返回")
            return
        # logger.info("提示词设置处理未完成，继续执行")

    # 检查是否是媒体组消息
    if event.message.grouped_id:
        # 如果这个媒体组已经处理过，就跳过
        group_key = f"{chat_id}:{event.message.grouped_id}"
        if group_key in PROCESSED_GROUPS:
            return
        # 标记这个媒体组为已处理
        PROCESSED_GROUPS.add(group_key)
        asyncio.create_task(clear_group_cache(group_key))
    
    # 首先检查数据库中是否有该聊天的转发规则
    session = get_session()
    try:
        # 查询源聊天
        source_chat = session.query(Chat).filter(
            Chat.telegram_chat_id == str(chat_id)
        ).first()
        
        if not source_chat:
            return
            
        logger.info(f'找到源聊天: {source_chat.name} (ID: {source_chat.id})')

        rules_to_process = []

        # 1. 查找直接匹配的规则（当前聊天作为源），预加载 target_chat 避免 N+1
        from sqlalchemy.orm import joinedload
        direct_rules = session.query(ForwardRule).options(
            joinedload(ForwardRule.target_chat)
        ).filter(
            ForwardRule.source_chat_id == source_chat.id,
            ForwardRule.enable_rule == True
        ).all()

        # 检查是否有任何规则启用了评论区转发（每个源聊天只调用一次）
        has_comment_forward = any(rule.enable_comment_forward for rule in direct_rules)
        if has_comment_forward:
            try:
                comment_manager = CommentManager(user_client)
                await comment_manager.get_linked_chat_id(source_chat.id)
            except Exception as e:
                logger.warning(f'建立评论区映射时出错(非致命): {str(e)}', exc_info=True)

        for rule in direct_rules:
            rules_to_process.append({
                'rule': rule,
                'is_comment': False,
                'parent_channel_id': None
            })

        if direct_rules:
            logger.info(f'找到 {len(direct_rules)} 条直接转发规则')

        # 2. 查找评论区匹配的规则
        mapping = session.query(ChannelCommentMapping).filter(
            ChannelCommentMapping.linked_chat_id == source_chat.id
        ).first()

        parent_channel_telegram_id = None  # 缓存父频道 ID，避免重复查询
        if mapping:
            logger.info(f'通过评论区映射找到频道 Chat ID: {mapping.channel_chat_id}')
            # 预加载 target_chat 避免 N+1
            comment_rules = session.query(ForwardRule).options(
                joinedload(ForwardRule.target_chat)
            ).filter(
                ForwardRule.source_chat_id == mapping.channel_chat_id,
                ForwardRule.enable_comment_forward == True,
                ForwardRule.enable_rule == True
            ).all()

            # 预先获取父频道的 telegram_chat_id（只查询一次）
            if comment_rules:
                parent_channel = session.query(Chat).filter(Chat.id == mapping.channel_chat_id).first()
                parent_channel_telegram_id = int(parent_channel.telegram_chat_id) if parent_channel else None

            for rule in comment_rules:
                rules_to_process.append({
                    'rule': rule,
                    'is_comment': True,
                    'parent_channel_id': mapping.channel_chat_id,
                    'parent_channel_telegram_id': parent_channel_telegram_id
                })

            if comment_rules:
                logger.info(f'找到 {len(comment_rules)} 条评论区转发规则')

        if not rules_to_process:
            logger.info(f'聊天 {source_chat.name} 没有任何转发规则')
            return

        # 记录消息信息
        if event.message.grouped_id:
            logger.info(f'[用户] 收到媒体组消息 来自聊天: {source_chat.name} ({chat_id}) 组ID: {event.message.grouped_id}')
        else:
            logger.info(f'[用户] 收到新消息 来自聊天: {source_chat.name} ({chat_id}) 内容: {event.message.text}')

        # 3. 处理所有匹配的规则
        for item in rules_to_process:
            rule = item['rule']
            target_chat = rule.target_chat

            # 构造 metadata
            metadata = None
            if item['is_comment']:
                metadata = {
                    'comment_metadata': {
                        'is_comment': True,
                        'original_channel_chat_id': item['parent_channel_telegram_id'],
                        'original_message_id': None  # 这个在 InitFilter 中通过 reply_to 获取
                    }
                }
                logger.info(f'处理评论区转发规则 ID: {rule.id} (从评论区 {source_chat.name} 转发到: {target_chat.name})')
            else:
                logger.info(f'处理转发规则 ID: {rule.id} (从 {source_chat.name} 转发到: {target_chat.name})')

            # 调用处理函数
            if rule.use_bot:
                await process_forward_rule(bot_client, event, str(chat_id), rule, metadata)
            else:
                await user_handler.process_forward_rule(user_client, event, str(chat_id), rule, metadata)
        
    except Exception as e:
        logger.error(f'处理用户消息时发生错误: {str(e)}')
        logger.exception(e)  # 添加详细的错误堆栈
    finally:
        session.close()

async def handle_bot_message(event, bot_client):
    """处理机器人客户端收到的消息（命令）"""
    try:
            
        # logger.info("handle_bot_message:开始处理机器人消息")
        
        chat = await event.get_chat()
        chat_id = abs(chat.id)
        # logger.info(f"handle_bot_message:获取到聊天ID: {chat_id}")

        # 检查是否频道消息
        if isinstance(event.chat, types.Channel) and state_manager.check_state():
            # logger.info("handle_bot_message:检测到频道消息且存在状态")
            sender_id = os.getenv('USER_ID')
            # 频道ID需要加上100前缀
            chat_id = int(f"100{chat_id}")
            # logger.info(f"handle_bot_message:频道消息处理: sender_id={sender_id}, chat_id={chat_id}")
        else:
            sender_id = event.sender_id
            # logger.info(f"handle_bot_message:非频道消息处理: sender_id={sender_id}")

        # 检查用户状态
        current_state, message, state_type = state_manager.get_state(sender_id, chat_id)
        # logger.info(f'handle_bot_message：当前是否有状态: {state_manager.check_state()}')
        # logger.info(f"handle_bot_message：当前用户ID和聊天ID: {sender_id}, {chat_id}")
        # logger.info(f"handle_bot_message：获取当前聊天窗口的用户状态: {current_state}")

        
        
        # 处理提示词设置
        if current_state:
            await handle_prompt_setting(event, bot_client, sender_id, chat_id, current_state, message)
            return

        # 如果没有特殊状态，则处理常规命令
        await bot_handler.handle_command(bot_client, event)
    except Exception as e:
        logger.error(f'处理机器人命令时发生错误: {str(e)}')
        logger.exception(e)

async def clear_group_cache(group_key, delay=300):  # 5分钟后清除缓存
    """清除已处理的媒体组记录"""
    await asyncio.sleep(delay)
    PROCESSED_GROUPS.discard(group_key) 

