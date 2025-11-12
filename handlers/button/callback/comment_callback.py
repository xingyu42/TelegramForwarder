"""
评论区转发设置回调处理模块
"""
import logging
from telethon import Button
from models.models import get_session, ForwardRule, Chat, ChannelCommentMapping
from utils.comment_manager import CommentManager
from managers.state_manager import state_manager
from handlers.button.settings_manager import RULE_SETTINGS

logger = logging.getLogger(__name__)


COMMENT_SETTINGS_TEXT = """⚙️ 评论区转发设置

当前设置：
• 评论区转发：{comment_forward_status}
• 消息前缀：{message_prefix}
• 原消息链接：{context_status}
• 评论区状态：{comment_group_status}

说明：
- 开启评论区转发后，频道评论区的消息也会被转发
- 评论区消息会与频道消息共享所有过滤规则
- 消息前缀用于区分评论区消息和频道消息
"""


async def callback_comment_settings(event, rule_id, session, message, data):
    """显示评论区设置页面"""
    try:
        rule = session.query(ForwardRule).get(int(rule_id))
        if not rule:
            await event.answer("规则不存在")
            return

        # 获取评论区状态信息
        comment_group_status = "未检测"
        if rule.enable_comment_forward:
            # 检查评论区映射
            source_chat = rule.source_chat
            mapping = session.query(ChannelCommentMapping).filter_by(
                channel_chat_id=source_chat.id
            ).first()

            if mapping and mapping.linked_chat_id:
                linked_chat = session.query(Chat).get(mapping.linked_chat_id)
                comment_group_status = f"✅ 已映射到: {linked_chat.name if linked_chat else '未知群组'}"
            else:
                comment_group_status = "⚠️ 未找到评论区映射"

        # 构造设置文本
        settings_text = COMMENT_SETTINGS_TEXT.format(
            comment_forward_status=RULE_SETTINGS['enable_comment_forward']['values'][rule.enable_comment_forward],
            message_prefix=rule.comment_message_prefix or '💬 评论:',
            context_status=RULE_SETTINGS['enable_comment_context']['values'][rule.enable_comment_context],
            comment_group_status=comment_group_status
        )

        await event.edit(settings_text, buttons=await create_comment_settings_buttons(rule))
    except Exception as e:
        logger.error(f"显示评论区设置时出错: {str(e)}")
        await event.answer("显示评论区设置失败")
    return


async def create_comment_settings_buttons(rule):
    """创建评论区设置按钮"""
    buttons = []

    # 评论区转发开关
    buttons.append([
        Button.inline(
            f"💭 评论区转发: {RULE_SETTINGS['enable_comment_forward']['values'][rule.enable_comment_forward]}",
            f"toggle_enable_comment_forward:{rule.id}"
        )
    ])

    # 消息前缀设置
    buttons.append([
        Button.inline(
            f"📝 消息前缀: {rule.comment_message_prefix or '💬 评论:'}",
            f"set_comment_message_prefix:{rule.id}"
        )
    ])

    # 原消息链接开关
    buttons.append([
        Button.inline(
            f"🔗 附带原消息链接: {RULE_SETTINGS['enable_comment_context']['values'][rule.enable_comment_context]}",
            f"toggle_enable_comment_context:{rule.id}"
        )
    ])

    # 返回和关闭按钮
    buttons.append([
        Button.inline("👈 返回", f"rule_settings:{rule.id}"),
        Button.inline("❌ 关闭", "close_settings")
    ])

    return buttons


async def callback_set_comment_message_prefix(event, rule_id, session, message, data):
    """设置评论消息前缀"""
    try:
        rule = session.query(ForwardRule).get(int(rule_id))
        if not rule:
            await event.answer("规则不存在")
            return

        # 获取用户ID
        user_id = event.sender_id

        # 设置状态，等待用户输入
        state_manager.set_state(
            user_id=user_id,
            chat_id=event.chat_id,
            state='waiting_comment_prefix',
            message=message,
            rule_id=rule_id
        )

        await event.edit(
            "请输入评论消息前缀（发送空格可清除前缀）：\n\n"
            "示例：💬 评论：\n"
            "示例：[评论] \n\n"
            f"当前前缀：{rule.comment_message_prefix or '💬 评论:'}",
            buttons=[[Button.inline("❌ 取消", f"comment_settings:{rule.id}")]]
        )
    except Exception as e:
        logger.error(f"设置评论消息前缀时出错: {str(e)}")
        await event.answer("设置失败")
    return
