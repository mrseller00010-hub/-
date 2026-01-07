"""Telegram bot entrypoint (single-file version)."""
import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import psutil
import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from Boltnew.sheerid_verifier import SheerIDVerifier as BoltnewVerifier
from k12.sheerid_verifier import SheerIDVerifier as K12Verifier
from one.sheerid_verifier import SheerIDVerifier as OneVerifier
from spotify.sheerid_verifier import SheerIDVerifier as SpotifyVerifier
from youtube.sheerid_verifier import SheerIDVerifier as YouTubeVerifier

# 加载 .env 文件
load_dotenv()

# Telegram Bot 配置
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "pk_oa")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/pk_oa")

# 管理员配置
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "123456789"))

# 积分配置
VERIFY_COST = 1  # 验证消耗的积分
CHECKIN_REWARD = 1  # 签到奖励积分
INVITE_REWARD = 2  # 邀请奖励积分
REGISTER_REWARD = 1  # 注册奖励积分

# Help link
HELP_NOTION_URL = "https://rhetorical-era-3f3.notion.site/dd78531dbac745af9bbac156b51da9cc"

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class MySQLDatabase:
    """MySQL 数据库管理类"""

    def __init__(self):
        """初始化数据库连接"""
        # 从环境变量读取配置（推荐）或使用默认值
        self.config = {
            "host": os.getenv("MYSQL_HOST", "localhost"),
            "port": int(os.getenv("MYSQL_PORT", 3306)),
            "user": os.getenv("MYSQL_USER", "tgbot_user"),
            "password": os.getenv("MYSQL_PASSWORD", "your_password_here"),
            "database": os.getenv("MYSQL_DATABASE", "tgbot_verify"),
            "charset": "utf8mb4",
            "autocommit": False,
        }
        logger.info(
            "MySQL 数据库初始化: %s@%s/%s",
            self.config["user"],
            self.config["host"],
            self.config["database"],
        )
        self.init_database()

    def get_connection(self):
        """获取数据库连接"""
        return pymysql.connect(**self.config)

    def init_database(self):
        """初始化数据库表结构"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            # 用户表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    full_name VARCHAR(255),
                    balance INT DEFAULT 1,
                    is_blocked TINYINT(1) DEFAULT 0,
                    invited_by BIGINT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_checkin DATETIME NULL,
                    INDEX idx_username (username),
                    INDEX idx_invited_by (invited_by)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # 邀请记录表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS invitations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    inviter_id BIGINT NOT NULL,
                    invitee_id BIGINT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_inviter (inviter_id),
                    INDEX idx_invitee (invitee_id),
                    FOREIGN KEY (inviter_id) REFERENCES users(user_id),
                    FOREIGN KEY (invitee_id) REFERENCES users(user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # 验证记录表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS verifications (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    verification_type VARCHAR(50) NOT NULL,
                    verification_url TEXT,
                    verification_id VARCHAR(255),
                    status VARCHAR(50) NOT NULL,
                    result TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_id (user_id),
                    INDEX idx_type (verification_type),
                    INDEX idx_created (created_at),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # 卡密表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS card_keys (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    key_code VARCHAR(100) UNIQUE NOT NULL,
                    balance INT NOT NULL,
                    max_uses INT DEFAULT 1,
                    current_uses INT DEFAULT 0,
                    expire_at DATETIME NULL,
                    created_by BIGINT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_key_code (key_code),
                    INDEX idx_created_by (created_by)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            # 卡密使用记录
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS card_key_usage (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    key_code VARCHAR(100) NOT NULL,
                    user_id BIGINT NOT NULL,
                    used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_key_code (key_code),
                    INDEX idx_user_id (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

            conn.commit()
            logger.info("MySQL 数据库表初始化完成")

        except Exception as e:
            logger.error("初始化数据库失败: %s", e)
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def create_user(
        self, user_id: int, username: str, full_name: str, invited_by: Optional[int] = None
    ) -> bool:
        """创建新用户"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                INSERT INTO users (user_id, username, full_name, invited_by, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                """,
                (user_id, username, full_name, invited_by),
            )

            if invited_by:
                cursor.execute(
                    "UPDATE users SET balance = balance + 2 WHERE user_id = %s",
                    (invited_by,),
                )

                cursor.execute(
                    """
                    INSERT INTO invitations (inviter_id, invitee_id, created_at)
                    VALUES (%s, %s, NOW())
                    """,
                    (invited_by, user_id),
                )

            conn.commit()
            return True

        except pymysql.err.IntegrityError:
            conn.rollback()
            return False
        except Exception as e:
            logger.error("创建用户失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def get_user(self, user_id: int) -> Optional[Dict]:
        """获取用户信息"""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()

            if row:
                # 创建新字典并转换datetime为ISO格式字符串
                result = dict(row)
                if result.get("created_at"):
                    result["created_at"] = result["created_at"].isoformat()
                if result.get("last_checkin"):
                    result["last_checkin"] = result["last_checkin"].isoformat()
                return result
            return None

        finally:
            cursor.close()
            conn.close()

    def user_exists(self, user_id: int) -> bool:
        """检查用户是否存在"""
        return self.get_user(user_id) is not None

    def is_user_blocked(self, user_id: int) -> bool:
        """检查用户是否被拉黑"""
        user = self.get_user(user_id)
        return user and user["is_blocked"] == 1

    def block_user(self, user_id: int) -> bool:
        """拉黑用户"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("UPDATE users SET is_blocked = 1 WHERE user_id = %s", (user_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error("拉黑用户失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def unblock_user(self, user_id: int) -> bool:
        """取消拉黑用户"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("UPDATE users SET is_blocked = 0 WHERE user_id = %s", (user_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error("取消拉黑失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def get_blacklist(self) -> List[Dict]:
        """获取黑名单列表"""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            cursor.execute("SELECT * FROM users WHERE is_blocked = 1")
            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    def add_balance(self, user_id: int, amount: int) -> bool:
        """增加用户积分"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                (amount, user_id),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error("增加积分失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def set_balance(self, user_id: int, amount: int) -> bool:
        """Set user balance to a specific amount."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "UPDATE users SET balance = %s WHERE user_id = %s",
                (amount, user_id),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error("Failed to set balance: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def deduct_balance(self, user_id: int, amount: int) -> bool:
        """扣除用户积分"""
        user = self.get_user(user_id)
        if not user or user["balance"] < amount:
            return False

        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s",
                (amount, user_id),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error("扣除积分失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def can_checkin(self, user_id: int) -> bool:
        """检查用户今天是否可以签到"""
        user = self.get_user(user_id)
        if not user:
            return False

        last_checkin = user.get("last_checkin")
        if not last_checkin:
            return True

        last_date = datetime.fromisoformat(last_checkin).date()
        today = datetime.now().date()

        return last_date < today

    def checkin(self, user_id: int) -> bool:
        """用户签到（修复无限签到bug）"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            # 使用SQL原子操作，避免竞态条件
            # 只有当 last_checkin 是NULL 或者日期 < 今天时才更新
            cursor.execute(
                """
                UPDATE users
                SET balance = balance + 1, last_checkin = NOW()
                WHERE user_id = %s
                AND (
                    last_checkin IS NULL
                    OR DATE(last_checkin) < CURDATE()
                )
                """,
                (user_id,),
            )
            conn.commit()

            # 检查是否真的更新了（affected_rows > 0 表示签到成功）
            success = cursor.rowcount > 0
            return success

        except Exception as e:
            logger.error("签到失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def add_verification(
        self,
        user_id: int,
        verification_type: str,
        verification_url: str,
        status: str,
        result: str = "",
        verification_id: str = "",
    ) -> bool:
        """添加验证记录"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                INSERT INTO verifications
                (user_id, verification_type, verification_url, verification_id, status, result, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (user_id, verification_type, verification_url, verification_id, status, result),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error("添加验证记录失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def get_user_verifications(self, user_id: int) -> List[Dict]:
        """获取用户的验证记录"""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            cursor.execute(
                """
                SELECT * FROM verifications
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    def get_recent_verifications(self, limit: int = 10) -> List[Dict]:
        """Get recent verification records."""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            cursor.execute(
                """
                SELECT * FROM verifications
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    def get_verification_status_counts(self) -> Dict[str, int]:
        """Get verification counts grouped by status."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT status, COUNT(*) FROM verifications GROUP BY status")
            rows = cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            cursor.close()
            conn.close()

    def get_total_users(self) -> int:
        """Get total user count."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT COUNT(*) FROM users")
            result = cursor.fetchone()
            return int(result[0]) if result else 0
        finally:
            cursor.close()
            conn.close()

    def get_total_verifications(self) -> int:
        """Get total verification count."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT COUNT(*) FROM verifications")
            result = cursor.fetchone()
            return int(result[0]) if result else 0
        finally:
            cursor.close()
            conn.close()

    def get_verifications_last_days(self, days: int = 1) -> int:
        """Get verification count in the last N days."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT COUNT(*) FROM verifications WHERE created_at >= NOW() - INTERVAL %s DAY",
                (days,),
            )
            result = cursor.fetchone()
            return int(result[0]) if result else 0
        finally:
            cursor.close()
            conn.close()

    def health_check(self) -> bool:
        """Check database connectivity."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT 1")
            return True
        except Exception as e:
            logger.error("Database health check failed: %s", e)
            return False
        finally:
            cursor.close()
            conn.close()
    def create_card_key(
        self,
        key_code: str,
        balance: int,
        created_by: int,
        max_uses: int = 1,
        expire_days: Optional[int] = None,
    ) -> bool:
        """创建卡密"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            expire_at = None
            if expire_days:
                expire_at = datetime.now() + timedelta(days=expire_days)

            cursor.execute(
                """
                INSERT INTO card_keys (key_code, balance, max_uses, created_by, created_at, expire_at)
                VALUES (%s, %s, %s, %s, NOW(), %s)
                """,
                (key_code, balance, max_uses, created_by, expire_at),
            )
            conn.commit()
            return True

        except pymysql.err.IntegrityError:
            logger.error("卡密已存在: %s", key_code)
            conn.rollback()
            return False
        except Exception as e:
            logger.error("创建卡密失败: %s", e)
            conn.rollback()
            return False
        finally:
            cursor.close()
            conn.close()

    def use_card_key(self, key_code: str, user_id: int) -> Optional[int]:
        """使用卡密，返回获得的积分数量"""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            # 查询卡密
            cursor.execute(
                "SELECT * FROM card_keys WHERE key_code = %s",
                (key_code,),
            )
            card = cursor.fetchone()

            if not card:
                return None

            # 检查是否过期
            if card["expire_at"] and datetime.now() > card["expire_at"]:
                return -2

            # 检查使用次数
            if card["current_uses"] >= card["max_uses"]:
                return -1

            # 检查用户是否已使用过此卡密
            cursor.execute(
                "SELECT COUNT(*) as count FROM card_key_usage WHERE key_code = %s AND user_id = %s",
                (key_code, user_id),
            )
            count = cursor.fetchone()
            if count["count"] > 0:
                return -3

            # 更新使用次数
            cursor.execute(
                "UPDATE card_keys SET current_uses = current_uses + 1 WHERE key_code = %s",
                (key_code,),
            )

            # 记录使用记录
            cursor.execute(
                "INSERT INTO card_key_usage (key_code, user_id, used_at) VALUES (%s, %s, NOW())",
                (key_code, user_id),
            )

            # 增加用户积分
            cursor.execute(
                "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                (card["balance"], user_id),
            )

            conn.commit()
            return card["balance"]

        except Exception as e:
            logger.error("使用卡密失败: %s", e)
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def get_card_key_info(self, key_code: str) -> Optional[Dict]:
        """获取卡密信息"""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            cursor.execute("SELECT * FROM card_keys WHERE key_code = %s", (key_code,))
            return cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

    def get_all_card_keys(self, created_by: Optional[int] = None) -> List[Dict]:
        """获取所有卡密（可按创建者筛选）"""
        conn = self.get_connection()
        cursor = conn.cursor(DictCursor)

        try:
            if created_by:
                cursor.execute(
                    "SELECT * FROM card_keys WHERE created_by = %s ORDER BY created_at DESC",
                    (created_by,),
                )
            else:
                cursor.execute("SELECT * FROM card_keys ORDER BY created_at DESC")

            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    def get_all_user_ids(self) -> List[int]:
        """获取所有用户ID"""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT user_id FROM users")
            rows = cursor.fetchall()
            return [row[0] for row in rows]
        finally:
            cursor.close()
            conn.close()


Database = MySQLDatabase


def is_group_chat(update: Update) -> bool:
    """判断是否为群聊"""
    chat = update.effective_chat
    return chat and chat.type in ("group", "supergroup")


async def reject_group_command(update: Update) -> bool:
    """群聊限制：仅允许 /verify /verify2 /verify3 /verify4 /verify5 /verify6 /qd"""
    if is_group_chat(update):
        await update.message.reply_text(
            "Groups only support /verify /verify2 /verify3 /verify4 /verify5 /verify6 /qd. Please use private chat for other commands."
        )
        return True
    return False


async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查用户是否加入了频道"""
    try:
        member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ["member", "administrator", "creator"]
    except TelegramError as e:
        logger.error("检查频道成员失败: %s", e)
        return False


def get_welcome_message(full_name: str, invited_by: bool = False) -> str:
    """获取欢迎消息"""
    msg = (
        f"🎉 Welcome, {full_name}!\n"
        "You have successfully registered and received 1 credit.\n"
    )
    if invited_by:
        msg += "Thanks for joining via the invite link. The inviter received 2 credits.\n"

    msg += (
        "\nThis bot can automatically complete SheerID verification.\n"
        "Quick start:\n"
        "/about - Learn what the bot does\n"
        "/balance - Check your balance\n"
        "/help - View all commands\n\n"
        "Get more credits:\n"
        "/qd - Daily check-in\n"
        "/invite - Invite friends\n"
        f"Join the channel: {CHANNEL_URL}"
    )
    return msg


def get_about_message() -> str:
    """获取关于消息"""
    return (
        "🤖 SheerID Auto Verification Bot\n"
        "\n"
        "Features:\n"
        "- Automates SheerID student/teacher verification\n"
        "- Supports Gemini One Pro, ChatGPT Teacher K12, Spotify Student, YouTube Student, Bolt.new Teacher, and ChatGPT Plus for Veterans\n"
        "\n"
        "Credits:\n"
        "- Register to get 1 credit\n"
        "- Daily check-in +1 credit\n"
        "- Invite friends +2 credits per user\n"
        "- Use a key (based on key rules)\n"
        f"- Join the channel: {CHANNEL_URL}\n"
        "\n"
        "How to use:\n"
        "1. Start verification on the web and copy the full verification link\n"
        "2. Send /verify, /verify2, /verify3, /verify4, /verify5, or /verify6 with the link\n"
        "3. Wait for processing and see the result\n"
        "4. Bolt.new verification can auto-fetch the reward code; use /getV4Code <verification_id> to check manually\n"
        "\n"
        "Send /help for the full command list."
    )


def get_help_message(is_admin: bool = False) -> str:
    """获取帮助消息"""
    msg = (
        "📖 SheerID Auto Verification Bot - Help\n"
        "\n"
        "User commands:\n"
        "/start - Start (register)\n"
        "/about - Learn about the bot\n"
        "/balance - Check balance\n"
        "/qd - Daily check-in (+1 credit)\n"
        "/invite - Generate invite link (+2 credits per user)\n"
        "/use <key> - Redeem a key\n"
        f"/verify <link> - Gemini One Pro (-{VERIFY_COST} credit)\n"
        f"/verify2 <link> - ChatGPT Teacher K12 (-{VERIFY_COST} credit)\n"
        f"/verify3 <link> - Spotify Student (-{VERIFY_COST} credit)\n"
        f"/verify4 <link> - Bolt.new Teacher (-{VERIFY_COST} credit)\n"
        f"/verify5 <link> - YouTube Student Premium (-{VERIFY_COST} credit)\n"
        f"/verify6 <link> - ChatGPT Plus for Veterans (-{VERIFY_COST} credit)\n"
        "/getV4Code <verification_id> - Fetch Bolt.new reward code\n"
        "/help - Show this help message\n"
        f"Verification failure guide: {HELP_NOTION_URL}\n"
    )

    if is_admin:
        msg += (
            "\nAdmin commands:\n"
            "/admin - Open admin panel\n"
            "/addbalance <user_id> <credits> - Add credits\n"
            "/block <user_id> - Block user\n"
            "/white <user_id> - Unblock user\n"
            "/blacklist - View blacklist\n"
            "/genkey <key> <credits> [uses] [days] - Create key\n"
            "/listkeys - List keys\n"
            "/broadcast <text> - Broadcast a message\n"
        )

    return msg


def get_insufficient_balance_message(current_balance: int) -> str:
    """获取积分不足消息"""
    return (
        f"Insufficient credits. {VERIFY_COST} needed, you have {current_balance}.\n\n"
        "Get more credits:\n"
        "- Daily check-in /qd\n"
        "- Invite friends /invite\n"
        "- Redeem a key /use <key>"
    )


def get_verify_usage_message(command: str, service_name: str) -> str:
    """获取验证命令使用说明"""
    return (
        f"Usage: {command} <SheerID link>\n\n"
        "Example:\n"
        f"{command} https://services.sheerid.com/verify/xxx/?verificationId=xxx\n\n"
        "How to get the link:\n"
        f"1. Open the {service_name} verification page\n"
        "2. Start the verification flow\n"
        "3. Copy the full URL from your browser\n"
        f"4. Submit with {command}"
    )


def build_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 User Management", callback_data="admin:user")],
            [InlineKeyboardButton("📢 Broadcast Tools", callback_data="admin:broadcast")],
            [InlineKeyboardButton("📜 Verification Logs", callback_data="admin:logs")],
            [InlineKeyboardButton("📊 Stats & Charts", callback_data="admin:stats")],
            [InlineKeyboardButton("🔑 Key Management", callback_data="admin:keys")],
            [InlineKeyboardButton("🧪 DB Monitor", callback_data="admin:db")],
            [InlineKeyboardButton("❌ Close", callback_data="admin:close")],
        ]
    )


def build_user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚫 Block User", callback_data="admin:user:block"),
                InlineKeyboardButton("✅ Unblock User", callback_data="admin:user:unblock"),
            ],
            [
                InlineKeyboardButton("➕ Add Credits", callback_data="admin:user:addcredits"),
                InlineKeyboardButton("🎯 Set Balance", callback_data="admin:user:setbalance"),
            ],
            [InlineKeyboardButton("🔍 User Info", callback_data="admin:user:info")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")],
        ]
    )


def build_logs_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🕒 Recent Logs", callback_data="admin:logs:recent")],
            [InlineKeyboardButton("👤 Logs by User", callback_data="admin:logs:user")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")],
        ]
    )


def build_keys_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Create Key", callback_data="admin:keys:create")],
            [InlineKeyboardButton("📋 List Keys", callback_data="admin:keys:list")],
            [InlineKeyboardButton("🔎 Key Info", callback_data="admin:keys:info")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")],
        ]
    )


def build_broadcast_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📝 New Broadcast", callback_data="admin:broadcast:new")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")],
        ]
    )


def build_db_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Health Check", callback_data="admin:db:health")],
            [InlineKeyboardButton("📦 Table Counts", callback_data="admin:db:counts")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")],
        ]
    )


async def send_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=build_admin_menu())
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=build_admin_menu())


async def send_admin_submenu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    menu: InlineKeyboardMarkup,
) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=menu)
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=menu)

def _calculate_max_concurrency() -> int:
    """根据系统资源计算最大并发数"""
    try:
        cpu_count = psutil.cpu_count() or 4
        memory_gb = psutil.virtual_memory().total / (1024**3)

        # 基于 CPU 和内存计算
        # 每个 CPU 核心支持 3-5 个并发任务
        # 每 GB 内存支持 2 个并发任务
        cpu_based = cpu_count * 4
        memory_based = int(memory_gb * 2)

        # 取两者的最小值，并设置上下限
        max_concurrent = min(cpu_based, memory_based)
        max_concurrent = max(10, min(max_concurrent, 100))  # 10-100 之间

        logger.info(
            "系统资源: CPU=%s, Memory=%.1fGB, 计算并发数=%s",
            cpu_count,
            memory_gb,
            max_concurrent,
        )

        return max_concurrent

    except Exception as e:
        logger.warning("无法获取系统资源信息: %s, 使用默认值", e)
        return 20  # 默认值


_base_concurrency = _calculate_max_concurrency()

_verification_semaphores: Dict[str, asyncio.Semaphore] = {
    "gemini_one_pro": asyncio.Semaphore(_base_concurrency // 5),
    "chatgpt_teacher_k12": asyncio.Semaphore(_base_concurrency // 5),
    "spotify_student": asyncio.Semaphore(_base_concurrency // 5),
    "youtube_student": asyncio.Semaphore(_base_concurrency // 5),
    "bolt_teacher": asyncio.Semaphore(_base_concurrency // 5),
    "military_veteran": asyncio.Semaphore(_base_concurrency // 5),
}


def get_verification_semaphore(verification_type: str) -> asyncio.Semaphore:
    """获取指定验证类型的信号量

    Args:
        verification_type: 验证类型

    Returns:
        asyncio.Semaphore: 对应的信号量
    """
    semaphore = _verification_semaphores.get(verification_type)

    if semaphore is None:
        # 未知类型，创建默认信号量
        semaphore = asyncio.Semaphore(_base_concurrency // 3)
        _verification_semaphores[verification_type] = semaphore
        logger.info(
            "为新验证类型 %s 创建信号量: limit=%s",
            verification_type,
            _base_concurrency // 3,
        )

    return semaphore


def get_concurrency_stats() -> Dict[str, Dict[str, int]]:
    """获取并发统计信息

    Returns:
        dict: 各验证类型的并发信息
    """
    stats = {}
    for vtype, semaphore in _verification_semaphores.items():
        # 注意：_value 是内部属性，可能在不同 Python 版本中变化
        try:
            available = semaphore._value if hasattr(semaphore, "_value") else 0
            limit = _base_concurrency // 3
            in_use = limit - available
        except Exception:
            available = 0
            limit = _base_concurrency // 3
            in_use = 0

        stats[vtype] = {
            "limit": limit,
            "in_use": in_use,
            "available": available,
        }

    return stats


async def monitor_system_load() -> Dict[str, float]:
    """监控系统负载

    Returns:
        dict: 系统负载信息
    """
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory_percent = psutil.virtual_memory().percent

        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "concurrency_limit": _base_concurrency,
        }
    except Exception as e:
        logger.error("监控系统负载失败: %s", e)
        return {
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "concurrency_limit": _base_concurrency,
        }


def adjust_concurrency_limits(multiplier: float = 1.0):
    """动态调整并发限制

    Args:
        multiplier: 调整倍数（0.5-2.0）
    """
    global _verification_semaphores, _base_concurrency

    # 限制倍数范围
    multiplier = max(0.5, min(multiplier, 2.0))

    new_base = int(_base_concurrency * multiplier)
    new_limit = max(5, min(new_base // 3, 50))  # 每种类型 5-50

    logger.info(
        "调整并发限制: multiplier=%s, new_base=%s, per_type=%s",
        multiplier,
        new_base,
        new_limit,
    )

    # 创建新的信号量
    for vtype in _verification_semaphores.keys():
        _verification_semaphores[vtype] = asyncio.Semaphore(new_limit)


_monitor_task = None


async def start_load_monitoring(interval: float = 60.0):
    """启动负载监控任务

    Args:
        interval: 监控间隔（秒）
    """
    global _monitor_task

    if _monitor_task is not None:
        return

    async def monitor_loop():
        while True:
            try:
                await asyncio.sleep(interval)

                load_info = await monitor_system_load()
                cpu = load_info["cpu_percent"]
                memory = load_info["memory_percent"]

                logger.info("系统负载: CPU=%.1f%%, Memory=%.1f%%", cpu, memory)

                # 自动调整并发限制
                if cpu > 80 or memory > 85:
                    # 负载过高，降低并发
                    adjust_concurrency_limits(0.7)
                    logger.warning("系统负载过高，降低并发限制")
                elif cpu < 40 and memory < 60:
                    # 负载较低，可以提高并发
                    adjust_concurrency_limits(1.2)
                    logger.info("系统负载较低，提高并发限制")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("负载监控异常: %s", e)

    _monitor_task = asyncio.create_task(monitor_loop())
    logger.info("负载监控已启动: interval=%ss", interval)


MILITARY_ORGANIZATIONS = [
    {"id": 4070, "name": "Army"},
    {"id": 4073, "name": "Air Force"},
    {"id": 4072, "name": "Navy"},
    {"id": 4071, "name": "Marine Corps"},
    {"id": 4074, "name": "Coast Guard"},
    {"id": 4544268, "name": "Space Force"},
]


def _generate_simple_name() -> Tuple[str, str]:
    first_names = [
        "Alex",
        "Jordan",
        "Taylor",
        "Morgan",
        "Riley",
        "Casey",
        "Avery",
        "Jamie",
        "Cameron",
        "Drew",
    ]
    last_names = [
        "Smith",
        "Johnson",
        "Williams",
        "Brown",
        "Jones",
        "Garcia",
        "Miller",
        "Davis",
        "Rodriguez",
        "Martinez",
    ]
    return random.choice(first_names), random.choice(last_names)


def _generate_birth_date() -> str:
    year = random.randint(1960, 2004)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _generate_discharge_date() -> str:
    year = random.randint(2005, 2024)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _generate_email(first_name: str, last_name: str) -> str:
    domain = random.choice(["gmail.com", "outlook.com", "yahoo.com"])
    suffix = random.randint(10, 9999)
    return f"{first_name.lower()}.{last_name.lower()}{suffix}@{domain}"


async def run_with_retries(
    task: Callable[[], Any],
    retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 6.0,
) -> Any:
    """Run a callable with retries and exponential backoff."""
    attempt = 0
    while True:
        try:
            return await asyncio.to_thread(task)
        except Exception as e:
            if attempt >= retries:
                raise
            sleep_for = min(max_delay, base_delay * (2**attempt))
            logger.warning("Retrying after error: %s (sleep %.1fs)", e, sleep_for)
            await asyncio.sleep(sleep_for)
            attempt += 1


class MilitaryVeteranVerifier:
    """SheerID verification flow for US military veterans."""

    def __init__(self, verification_id: str):
        self.verification_id = verification_id
        self.http_client = httpx.Client(timeout=30.0)

    def __del__(self):
        if hasattr(self, "http_client"):
            self.http_client.close()

    @staticmethod
    def parse_verification_id(url: str) -> Optional[str]:
        match = re.search(r"verificationId=([a-f0-9]+)", url, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _sheerid_request(self, method: str, url: str, body: Optional[Dict] = None) -> Tuple[Dict, int]:
        headers = {"Content-Type": "application/json"}
        response = self.http_client.request(method=method, url=url, json=body, headers=headers)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}
        return data, response.status_code

    def verify(self) -> Dict:
        first_name, last_name = _generate_simple_name()
        birth_date = _generate_birth_date()
        discharge_date = _generate_discharge_date()
        email = _generate_email(first_name, last_name)
        organization = random.choice(MILITARY_ORGANIZATIONS)

        step1_body = {"status": "VETERAN"}
        step1_url = (
            "https://services.sheerid.com/rest/v2/verification/"
            f"{self.verification_id}/step/collectMilitaryStatus"
        )
        step1_data, step1_status = self._sheerid_request("POST", step1_url, step1_body)
        if step1_status != 200:
            return {"success": False, "message": f"collectMilitaryStatus failed: {step1_data}"}

        submission_url = step1_data.get("submissionUrl")
        if not submission_url:
            return {"success": False, "message": "No submission URL returned from SheerID."}

        step2_body = {
            "firstName": first_name,
            "lastName": last_name,
            "birthDate": birth_date,
            "email": email,
            "phoneNumber": "",
            "organization": {"id": organization["id"], "name": organization["name"]},
            "dischargeDate": discharge_date,
            "locale": "en-US",
            "country": "US",
            "metadata": {
                "marketConsentValue": False,
                "refererUrl": "",
                "verificationId": self.verification_id,
                "flags": (
                    '{"doc-upload-considerations":"default","doc-upload-may24":"default",'
                    '"doc-upload-redesign-use-legacy-message-keys":false,'
                    '"docUpload-assertion-checklist":"default","include-cvec-field-france-student"'
                    ':"not-labeled-optional","org-search-overlay":"default","org-selected-display":"default"}'
                ),
                "submissionOptIn": (
                    "By submitting the personal information above, I acknowledge that my personal "
                    "information is being collected under the privacy policy of the business from "
                    "which I am seeking a discount, and I understand that my personal information "
                    "will be shared with SheerID as a processor/third-party service provider."
                ),
            },
        }

        step2_data, step2_status = self._sheerid_request("POST", submission_url, step2_body)
        if step2_status != 200:
            return {"success": False, "message": f"collectInactiveMilitaryPersonalInfo failed: {step2_data}"}

        return {"success": True, "pending": True, "data": step2_data}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /start 命令"""
    if await reject_group_command(update):
        return

    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    full_name = user.full_name or ""

    # 已初始化直接返回
    if db.user_exists(user_id):
        await update.message.reply_text(
            f"Welcome back, {full_name}!\n"
            "You are already registered.\n"
            "Send /help to see available commands."
        )
        return

    # 邀请参与
    invited_by: Optional[int] = None
    if context.args:
        try:
            invited_by = int(context.args[0])
            if not db.user_exists(invited_by):
                invited_by = None
        except Exception:
            invited_by = None

    # 创建用户
    if db.create_user(user_id, username, full_name, invited_by):
        welcome_msg = get_welcome_message(full_name, bool(invited_by))
        await update.message.reply_text(welcome_msg)
    else:
        await update.message.reply_text("Registration failed. Please try again later.")


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /about 命令"""
    if await reject_group_command(update):
        return

    await update.message.reply_text(get_about_message())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /help 命令"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id
    is_admin = user_id == ADMIN_USER_ID
    await update.message.reply_text(get_help_message(is_admin))


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /balance 命令"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    user = db.get_user(user_id)
    if not user:
        await update.message.reply_text("Please register first with /start.")
        return

    await update.message.reply_text(f"💰 Balance\n\nCurrent credits: {user['balance']}")


async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /qd 签到命令 - 临时禁用"""
    user_id = update.effective_user.id

    # 临时禁用签到功能（修复bug中）
    # await update.message.reply_text(
    #     "⚠️ 签到功能临时维护中\n\n"
    #     "由于发现bug，签到功能暂时关闭，正在修复。\n"
    #     "预计很快恢复，给您带来不便敬请谅解。\n\n"
    #     "💡 您可以通过以下方式获取积分：\n"
    #     "• 邀请好友 /invite（+2积分）\n"
    #     "• 使用卡密 /use <卡密>"
    # )
    # return

    # ===== 以下代码已禁用 =====
    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    # 第1层检查：在命令处理器层面检查
    if not db.can_checkin(user_id):
        await update.message.reply_text("❌ You have already checked in today. Please try again tomorrow.")
        return

    # 第2层检查：在数据库层面执行（SQL原子操作）
    if db.checkin(user_id):
        user = db.get_user(user_id)
        await update.message.reply_text(
            f"✅ Check-in successful!\nCredits gained: +1\nCurrent credits: {user['balance']}"
        )
    else:
        # 如果数据库层面返回False，说明今天已签到（双重保险）
        await update.message.reply_text("❌ You have already checked in today. Please try again tomorrow.")


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /invite 邀请命令"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    bot_username = context.bot.username
    invite_link = f"https://t.me/{bot_username}?start={user_id}"

    await update.message.reply_text(
        f"🎁 Your invite link:\n{invite_link}\n\n"
        "Each successful registration gives you 2 credits."
    )


async def use_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /use 命令 - 使用卡密"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /use <key>\n\nExample: /use wandouyu")
        return

    key_code = context.args[0].strip()
    result = db.use_card_key(key_code, user_id)

    if result is None:
        await update.message.reply_text("Key not found. Please check and try again.")
    elif result == -1:
        await update.message.reply_text("This key has reached its usage limit.")
    elif result == -2:
        await update.message.reply_text("This key has expired.")
    elif result == -3:
        await update.message.reply_text("You have already used this key.")
    else:
        user = db.get_user(user_id)
        await update.message.reply_text(
            f"Key redeemed successfully!\nCredits gained: {result}\nCurrent credits: {user['balance']}"
        )


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify 命令 - Gemini One Pro"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text(get_verify_usage_message("/verify", "Gemini One Pro"))
        return

    url = context.args[0]
    user = db.get_user(user_id)
    if user["balance"] < VERIFY_COST:
        await update.message.reply_text(get_insufficient_balance_message(user["balance"]))
        return

    verification_id = OneVerifier.parse_verification_id(url)
    if not verification_id:
        await update.message.reply_text("Invalid SheerID link. Please check and try again.")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("Failed to deduct credits. Please try again later.")
        return

    processing_msg = await update.message.reply_text(
        f"Starting Gemini One Pro verification...\n"
        f"Verification ID: {verification_id}\n"
        f"Deducted {VERIFY_COST} credit(s)\n\n"
        "Please wait. This may take 1-2 minutes..."
    )

    try:
        verifier = OneVerifier(verification_id)
        result = await run_with_retries(verifier.verify)

        db.add_verification(
            user_id,
            "gemini_one_pro",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ Verification successful!\n\n"
            if result.get("pending"):
                result_msg += "Documents submitted, awaiting review.\n"
            if result.get("redirect_url"):
                result_msg += f"Redirect link:\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Verification failed: {result.get('message', 'Unknown error')}\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
    except Exception as e:
        logger.error("验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ Error during processing: {str(e)}\n\n"
            f"Refunded {VERIFY_COST} credit(s)"
        )


async def verify2_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify2 命令 - ChatGPT Teacher K12"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text(
            get_verify_usage_message("/verify2", "ChatGPT Teacher K12")
        )
        return

    url = context.args[0]
    user = db.get_user(user_id)
    if user["balance"] < VERIFY_COST:
        await update.message.reply_text(get_insufficient_balance_message(user["balance"]))
        return

    verification_id = K12Verifier.parse_verification_id(url)
    if not verification_id:
        await update.message.reply_text("Invalid SheerID link. Please check and try again.")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("Failed to deduct credits. Please try again later.")
        return

    processing_msg = await update.message.reply_text(
        f"Starting ChatGPT Teacher K12 verification...\n"
        f"Verification ID: {verification_id}\n"
        f"Deducted {VERIFY_COST} credit(s)\n\n"
        "Please wait. This may take 1-2 minutes..."
    )

    try:
        verifier = K12Verifier(verification_id)
        result = await run_with_retries(verifier.verify)

        db.add_verification(
            user_id,
            "chatgpt_teacher_k12",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ Verification successful!\n\n"
            if result.get("pending"):
                result_msg += "Documents submitted, awaiting review.\n"
            if result.get("redirect_url"):
                result_msg += f"Redirect link:\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Verification failed: {result.get('message', 'Unknown error')}\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
    except Exception as e:
        logger.error("验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ Error during processing: {str(e)}\n\n"
            f"Refunded {VERIFY_COST} credit(s)"
        )


async def verify3_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify3 命令 - Spotify Student"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text(get_verify_usage_message("/verify3", "Spotify Student"))
        return

    url = context.args[0]
    user = db.get_user(user_id)
    if user["balance"] < VERIFY_COST:
        await update.message.reply_text(get_insufficient_balance_message(user["balance"]))
        return

    # 解析 verificationId
    verification_id = SpotifyVerifier.parse_verification_id(url)
    if not verification_id:
        await update.message.reply_text("Invalid SheerID link. Please check and try again.")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("Failed to deduct credits. Please try again later.")
        return

    processing_msg = await update.message.reply_text(
        f"🎵 Starting Spotify Student verification...\n"
        f"Deducted {VERIFY_COST} credit(s)\n\n"
        "📝 Generating student profile...\n"
        "🎨 Creating student ID PNG...\n"
        "📤 Submitting documents..."
    )

    # 使用信号量控制并发
    semaphore = get_verification_semaphore("spotify_student")

    try:
        async with semaphore:
            verifier = SpotifyVerifier(verification_id)
            result = await run_with_retries(verifier.verify)

        db.add_verification(
            user_id,
            "spotify_student",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ Spotify Student verification successful!\n\n"
            if result.get("pending"):
                result_msg += "✨ Documents submitted, awaiting SheerID review\n"
                result_msg += "⏱️ Estimated review time: a few minutes\n\n"
            if result.get("redirect_url"):
                result_msg += f"🔗 Redirect link:\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Verification failed: {result.get('message', 'Unknown error')}\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
    except Exception as e:
        logger.error("Spotify 验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ Error during processing: {str(e)}\n\n"
            f"Refunded {VERIFY_COST} credit(s)"
        )


async def verify4_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify4 命令 - Bolt.new Teacher（自动获取code版）"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text(get_verify_usage_message("/verify4", "Bolt.new Teacher"))
        return

    url = context.args[0]
    user = db.get_user(user_id)
    if user["balance"] < VERIFY_COST:
        await update.message.reply_text(get_insufficient_balance_message(user["balance"]))
        return

    # 解析 externalUserId 或 verificationId
    external_user_id = BoltnewVerifier.parse_external_user_id(url)
    verification_id = BoltnewVerifier.parse_verification_id(url)

    if not external_user_id and not verification_id:
        await update.message.reply_text("Invalid SheerID link. Please check and try again.")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("Failed to deduct credits. Please try again later.")
        return

    processing_msg = await update.message.reply_text(
        f"🚀 Starting Bolt.new Teacher verification...\n"
        f"Deducted {VERIFY_COST} credit(s)\n\n"
        "📤 Submitting documents..."
    )

    # 使用信号量控制并发
    semaphore = get_verification_semaphore("bolt_teacher")

    try:
        async with semaphore:
            # 第1步：提交文档
            verifier = BoltnewVerifier(url, verification_id=verification_id)
            result = await run_with_retries(verifier.verify)

        if not result.get("success"):
            # 提交失败，退款
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Document submission failed: {result.get('message', 'Unknown error')}\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
            return

        vid = result.get("verification_id", "")
        if not vid:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Could not retrieve verification ID\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
            return

        # 更新消息
        await processing_msg.edit_text(
            f"✅ Documents submitted!\n"
            f"📋 Verification ID: `{vid}`\n\n"
            f"🔍 Auto-fetching reward code...\n"
            f"(Waiting up to 20 seconds)"
        )

        # 第2步：自动获取认证码（最多20秒）
        code = await _auto_get_reward_code(vid, max_wait=20, interval=5)

        if code:
            # 成功获取
            result_msg = (
                f"🎉 Verification successful!\n\n"
                f"✅ Documents submitted\n"
                f"✅ Review approved\n"
                f"✅ Reward code retrieved\n\n"
                f"🎁 Reward code: `{code}`\n"
            )
            if result.get("redirect_url"):
                result_msg += f"\n🔗 Redirect link:\n{result['redirect_url']}"

            await processing_msg.edit_text(result_msg)

            # 保存成功记录
            db.add_verification(
                user_id,
                "bolt_teacher",
                url,
                "success",
                f"Code: {code}",
                vid,
            )
        else:
            # 20秒内未获取到，让用户稍后查询
            await processing_msg.edit_text(
                f"✅ Documents submitted successfully!\n\n"
                f"⏳ Reward code not ready yet (review may take 1-5 minutes)\n\n"
                f"📋 Verification ID: `{vid}`\n\n"
                f"💡 Check later with:\n"
                f"`/getV4Code {vid}`\n\n"
                f"Note: credits were consumed; checking later is free"
            )

            # 保存待处理记录
            db.add_verification(
                user_id,
                "bolt_teacher",
                url,
                "pending",
                "Waiting for review",
                vid,
            )

    except Exception as e:
        logger.error("Bolt.new 验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ Error during processing: {str(e)}\n\n"
            f"Refunded {VERIFY_COST} credit(s)"
        )


async def _auto_get_reward_code(
    verification_id: str, max_wait: int = 20, interval: int = 5
) -> Optional[str]:
    """自动获取认证码（轻量级轮询，不影响并发）

    Args:
        verification_id: 验证ID
        max_wait: 最大等待时间（秒）
        interval: 轮询间隔（秒）

    Returns:
        str: 认证码，如果获取失败返回None
    """
    start_time = time.time()

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            elapsed = int(time.time() - start_time)

            # 检查是否超时
            if elapsed >= max_wait:
                logger.info("自动获取code超时(%s秒)，让用户手动查询", elapsed)
                return None

            try:
                # 查询验证状态
                response = await client.get(
                    f"https://my.sheerid.com/rest/v2/verification/{verification_id}"
                )

                if response.status_code == 200:
                    data = response.json()
                    current_step = data.get("currentStep")

                    if current_step == "success":
                        # 获取认证码
                        code = data.get("rewardCode") or data.get("rewardData", {}).get(
                            "rewardCode"
                        )
                        if code:
                            logger.info("✅ 自动获取code成功: %s (耗时%s秒)", code, elapsed)
                            return code
                    elif current_step == "error":
                        # 审核失败
                        logger.warning("审核失败: %s", data.get("errorIds", []))
                        return None
                    # else: pending，继续等待

                # 等待下次轮询
                await asyncio.sleep(interval)

            except Exception as e:
                logger.warning("查询认证码出错: %s", e)
                await asyncio.sleep(interval)

    return None


async def verify5_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify5 命令 - YouTube Student Premium"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text(
            get_verify_usage_message("/verify5", "YouTube Student Premium")
        )
        return

    url = context.args[0]
    user = db.get_user(user_id)
    if user["balance"] < VERIFY_COST:
        await update.message.reply_text(get_insufficient_balance_message(user["balance"]))
        return

    # 解析 verificationId
    verification_id = YouTubeVerifier.parse_verification_id(url)
    if not verification_id:
        await update.message.reply_text("Invalid SheerID link. Please check and try again.")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("Failed to deduct credits. Please try again later.")
        return

    processing_msg = await update.message.reply_text(
        f"📺 Starting YouTube Student Premium verification...\n"
        f"Deducted {VERIFY_COST} credit(s)\n\n"
        "📝 Generating student profile...\n"
        "🎨 Creating student ID PNG...\n"
        "📤 Submitting documents..."
    )

    # 使用信号量控制并发
    semaphore = get_verification_semaphore("youtube_student")

    try:
        async with semaphore:
            verifier = YouTubeVerifier(verification_id)
            result = await run_with_retries(verifier.verify)

        db.add_verification(
            user_id,
            "youtube_student",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ YouTube Student Premium verification successful!\n\n"
            if result.get("pending"):
                result_msg += "✨ Documents submitted, awaiting SheerID review\n"
                result_msg += "⏱️ Estimated review time: a few minutes\n\n"
            if result.get("redirect_url"):
                result_msg += f"🔗 Redirect link:\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Verification failed: {result.get('message', 'Unknown error')}\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
    except Exception as e:
        logger.error("YouTube 验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ Error during processing: {str(e)}\n\n"
            f"Refunded {VERIFY_COST} credit(s)"
        )


async def verify6_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """Handle /verify6 - ChatGPT Plus for Veterans."""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    if not context.args:
        await update.message.reply_text(
            get_verify_usage_message("/verify6", "ChatGPT Plus for Veterans")
        )
        return

    url = context.args[0]
    user = db.get_user(user_id)
    if user["balance"] < VERIFY_COST:
        await update.message.reply_text(get_insufficient_balance_message(user["balance"]))
        return

    verification_id = MilitaryVeteranVerifier.parse_verification_id(url)
    if not verification_id:
        await update.message.reply_text("Invalid SheerID link. Please check and try again.")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("Failed to deduct credits. Please try again later.")
        return

    processing_msg = await update.message.reply_text(
        f"🎖️ Starting ChatGPT Plus for Veterans verification...\n"
        f"Verification ID: {verification_id}\n"
        f"Deducted {VERIFY_COST} credit(s)\n\n"
        "Please wait. This may take 1-2 minutes..."
    )

    semaphore = get_verification_semaphore("military_veteran")

    try:
        async with semaphore:
            verifier = MilitaryVeteranVerifier(verification_id)
            result = await run_with_retries(verifier.verify)

        db.add_verification(
            user_id,
            "chatgpt_plus_veterans",
            url,
            "success" if result.get("success") else "failed",
            str(result),
        )

        if result.get("success"):
            result_msg = "✅ Verification submitted successfully!\n\n"
            if result.get("pending"):
                result_msg += "Documents submitted, awaiting review.\n"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ Verification failed: {result.get('message', 'Unknown error')}\n\n"
                f"Refunded {VERIFY_COST} credit(s)"
            )
    except Exception as e:
        logger.error("Veterans verification error: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ Error during processing: {str(e)}\n\n"
            f"Refunded {VERIFY_COST} credit(s)"
        )


async def getV4Code_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /getV4Code 命令 - 获取 Bolt.new Teacher 认证码"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("You are blocked and cannot use this feature.")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("Please register first with /start.")
        return

    # 检查是否提供了 verification_id
    if not context.args:
        await update.message.reply_text(
            "Usage: /getV4Code <verification_id>\n\n"
            "Example: /getV4Code 6929436b50d7dc18638890d0\n\n"
            "The verification_id is returned after using /verify4."
        )
        return

    verification_id = context.args[0].strip()

    processing_msg = await update.message.reply_text("🔍 Fetching reward code, please wait...")

    try:
        # 查询 SheerID API 获取认证码
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://my.sheerid.com/rest/v2/verification/{verification_id}"
            )

            if response.status_code != 200:
                await processing_msg.edit_text(
                    f"❌ Query failed, status code: {response.status_code}\n\n"
                    "Please try again later or contact admin."
                )
                return

            data = response.json()
            current_step = data.get("currentStep")
            reward_code = data.get("rewardCode") or data.get("rewardData", {}).get(
                "rewardCode"
            )
            redirect_url = data.get("redirectUrl")

            if current_step == "success" and reward_code:
                result_msg = "✅ Verification successful!\n\n"
                result_msg += f"🎉 Reward code: `{reward_code}`\n\n"
                if redirect_url:
                    result_msg += f"Redirect link:\n{redirect_url}"
                await processing_msg.edit_text(result_msg)
            elif current_step == "pending":
                await processing_msg.edit_text(
                    "⏳ Verification is still under review. Please try again later.\n\n"
                    "It usually takes 1-5 minutes."
                )
            elif current_step == "error":
                error_ids = data.get("errorIds", [])
                await processing_msg.edit_text(
                    f"❌ Verification failed\n\n"
                    f"Errors: {', '.join(error_ids) if error_ids else 'Unknown error'}"
                )
            else:
                await processing_msg.edit_text(
                    f"⚠️ Current status: {current_step}\n\n"
                    "Reward code not ready yet. Please try again later."
                )

    except Exception as e:
        logger.error("获取 Bolt.new 认证码失败: %s", e)
        await processing_msg.edit_text(
            f"❌ Error while querying: {str(e)}\n\n"
            "Please try again later or contact admin."
        )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """Open the admin panel."""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    context.user_data["admin_action"] = None
    await send_admin_menu(update, context, "🛠️ Admin Panel\nSelect an option:")


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """Handle admin panel callbacks."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_USER_ID:
        await query.message.reply_text("You do not have permission to use this command.")
        return

    data = query.data
    context.user_data["admin_action"] = None

    if data == "admin:back":
        await send_admin_menu(update, context, "🛠️ Admin Panel\nSelect an option:")
        return

    if data == "admin:close":
        await query.message.delete()
        return

    if data == "admin:user":
        await send_admin_submenu(update, context, "👤 User Management", build_user_menu())
        return

    if data == "admin:broadcast":
        await send_admin_submenu(update, context, "📢 Broadcast Tools", build_broadcast_menu())
        return

    if data == "admin:logs":
        await send_admin_submenu(update, context, "📜 Verification Logs", build_logs_menu())
        return

    if data == "admin:keys":
        await send_admin_submenu(update, context, "🔑 Key Management", build_keys_menu())
        return

    if data == "admin:db":
        await send_admin_submenu(update, context, "🧪 DB Monitor", build_db_menu())
        return

    if data == "admin:stats":
        total_users = db.get_total_users()
        total_verifications = db.get_total_verifications()
        recent_verifications = db.get_verifications_last_days(1)
        status_counts = db.get_verification_status_counts()
        success = status_counts.get("success", 0)
        failed = status_counts.get("failed", 0)
        pending = status_counts.get("pending", 0)

        message = (
            "📊 Stats Summary\n\n"
            f"Total users: {total_users}\n"
            f"Total verifications: {total_verifications}\n"
            f"Verifications (last 24h): {recent_verifications}\n"
            f"✅ Success: {success}\n"
            f"❌ Failed: {failed}\n"
            f"⏳ Pending: {pending}\n"
        )
        await query.message.reply_text(message)
        return

    if data == "admin:logs:recent":
        logs = db.get_recent_verifications(10)
        if not logs:
            await query.message.reply_text("No recent verification logs.")
            return
        lines = ["📜 Recent Verifications"]
        for log in logs:
            lines.append(
                f"- {log['created_at']} | {log['user_id']} | {log['verification_type']} | {log['status']}"
            )
        await query.message.reply_text("\n".join(lines))
        return

    if data == "admin:logs:user":
        context.user_data["admin_action"] = "logs_user"
        await query.message.reply_text("Send: <user_id> to view verification logs.")
        return

    if data == "admin:user:block":
        context.user_data["admin_action"] = "block_user"
        await query.message.reply_text("Send: <user_id> to block.")
        return

    if data == "admin:user:unblock":
        context.user_data["admin_action"] = "unblock_user"
        await query.message.reply_text("Send: <user_id> to unblock.")
        return

    if data == "admin:user:addcredits":
        context.user_data["admin_action"] = "add_credits"
        await query.message.reply_text("Send: <user_id> <credits> to add.")
        return

    if data == "admin:user:setbalance":
        context.user_data["admin_action"] = "set_balance"
        await query.message.reply_text("Send: <user_id> <credits> to set balance.")
        return

    if data == "admin:user:info":
        context.user_data["admin_action"] = "user_info"
        await query.message.reply_text("Send: <user_id> to view user info.")
        return

    if data == "admin:broadcast:new":
        context.user_data["admin_action"] = "broadcast"
        await query.message.reply_text(
            "Send the broadcast message text. Use {username} to personalize."
        )
        return

    if data == "admin:keys:create":
        context.user_data["admin_action"] = "key_create"
        await query.message.reply_text(
            "Send: <key> <credits> [uses] [days]\nExample: vip100 50 10 30"
        )
        return

    if data == "admin:keys:list":
        keys = db.get_all_card_keys()
        if not keys:
            await query.message.reply_text("No keys found.")
            return
        lines = ["🔑 Key List (first 20):"]
        for key in keys[:20]:
            lines.append(
                f"- {key['key_code']} | {key['balance']} credits | {key['current_uses']}/{key['max_uses']} uses"
            )
        await query.message.reply_text("\n".join(lines))
        return

    if data == "admin:keys:info":
        context.user_data["admin_action"] = "key_info"
        await query.message.reply_text("Send: <key> to view key info.")
        return

    if data == "admin:db:health":
        ok = db.health_check()
        await query.message.reply_text("✅ DB healthy." if ok else "❌ DB health check failed.")
        return

    if data == "admin:db:counts":
        total_users = db.get_total_users()
        total_verifications = db.get_total_verifications()
        message = (
            "📦 Table Counts\n\n"
            f"Users: {total_users}\n"
            f"Verifications: {total_verifications}\n"
            f"Keys: {len(db.get_all_card_keys())}"
        )
        await query.message.reply_text(message)
        return


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """Handle follow-up admin input messages."""
    if update.effective_user.id != ADMIN_USER_ID:
        return

    action = context.user_data.get("admin_action")
    if not action:
        return

    text = update.message.text.strip()
    context.user_data["admin_action"] = None

    if action == "block_user":
        try:
            target_user_id = int(text)
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        if db.block_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} has been blocked.")
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
        return

    if action == "unblock_user":
        try:
            target_user_id = int(text)
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        if db.unblock_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} has been unblocked.")
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
        return

    if action == "add_credits":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Usage: <user_id> <credits>")
            return
        try:
            target_user_id = int(parts[0])
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text("Invalid parameters.")
            return
        if db.add_balance(target_user_id, amount):
            await update.message.reply_text(
                f"✅ Added {amount} credits to user {target_user_id}."
            )
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
        return

    if action == "set_balance":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Usage: <user_id> <credits>")
            return
        try:
            target_user_id = int(parts[0])
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text("Invalid parameters.")
            return
        if db.set_balance(target_user_id, amount):
            await update.message.reply_text(
                f"✅ Set balance for user {target_user_id} to {amount}."
            )
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
        return

    if action == "user_info":
        try:
            target_user_id = int(text)
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        user = db.get_user(target_user_id)
        if not user:
            await update.message.reply_text("User not found.")
            return
        await update.message.reply_text(
            "👤 User Info\n\n"
            f"ID: {user['user_id']}\n"
            f"Username: @{user.get('username')}\n"
            f"Name: {user.get('full_name')}\n"
            f"Balance: {user.get('balance')}\n"
            f"Blocked: {bool(user.get('is_blocked'))}"
        )
        return

    if action == "broadcast":
        user_ids = db.get_all_user_ids()
        success, failed = 0, 0
        status_msg = await update.message.reply_text(
            f"📢 Starting broadcast to {len(user_ids)} users..."
        )
        for uid in user_ids:
            try:
                username = ""
                user = db.get_user(uid)
                if user and user.get("username"):
                    username = user["username"]
                personalized = text.replace("{username}", username)
                await context.bot.send_message(chat_id=uid, text=personalized)
                success += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning("Broadcast failed to %s: %s", uid, e)
                failed += 1
        await status_msg.edit_text(f"✅ Broadcast complete!\nSent: {success}\nFailed: {failed}")
        return

    if action == "logs_user":
        try:
            target_user_id = int(text)
        except ValueError:
            await update.message.reply_text("Invalid user ID.")
            return
        logs = db.get_user_verifications(target_user_id)
        if not logs:
            await update.message.reply_text("No verification logs for this user.")
            return
        lines = [f"📜 Logs for user {target_user_id} (latest 10):"]
        for log in logs[:10]:
            lines.append(
                f"- {log['created_at']} | {log['verification_type']} | {log['status']}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    if action == "key_create":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("Usage: <key> <credits> [uses] [days]")
            return
        key_code = parts[0]
        try:
            balance = int(parts[1])
            max_uses = int(parts[2]) if len(parts) > 2 else 1
            expire_days = int(parts[3]) if len(parts) > 3 else None
        except ValueError:
            await update.message.reply_text("Invalid parameters.")
            return
        if db.create_card_key(key_code, balance, ADMIN_USER_ID, max_uses, expire_days):
            await update.message.reply_text("✅ Key created successfully.")
        else:
            await update.message.reply_text("Key creation failed or already exists.")
        return

    if action == "key_info":
        key_code = text.strip()
        key = db.get_card_key_info(key_code)
        if not key:
            await update.message.reply_text("Key not found.")
            return
        await update.message.reply_text(
            "🔑 Key Info\n\n"
            f"Key: {key['key_code']}\n"
            f"Credits: {key['balance']}\n"
            f"Uses: {key['current_uses']}/{key['max_uses']}\n"
            f"Expires: {key['expire_at'] or 'never'}"
        )
        return

async def addbalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /addbalance 命令 - 管理员增加积分"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /addbalance <user_id> <credits>\n\nExample: /addbalance 123456789 10"
        )
        return

    try:
        target_user_id = int(context.args[0])
        amount = int(context.args[1])

        if not db.user_exists(target_user_id):
            await update.message.reply_text("User not found.")
            return

        if db.add_balance(target_user_id, amount):
            user = db.get_user(target_user_id)
            await update.message.reply_text(
                f"✅ Added {amount} credits to user {target_user_id}.\n"
                f"Current credits: {user['balance']}"
            )
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
    except ValueError:
        await update.message.reply_text("Invalid parameters. Please enter valid numbers.")


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /block 命令 - 管理员拉黑用户"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /block <user_id>\n\nExample: /block 123456789")
        return

    try:
        target_user_id = int(context.args[0])

        if not db.user_exists(target_user_id):
            await update.message.reply_text("User not found.")
            return

        if db.block_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} has been blocked.")
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
    except ValueError:
        await update.message.reply_text("Invalid parameter. Please enter a valid user ID.")


async def white_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /white 命令 - 管理员取消拉黑"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /white <user_id>\n\nExample: /white 123456789")
        return

    try:
        target_user_id = int(context.args[0])

        if not db.user_exists(target_user_id):
            await update.message.reply_text("User not found.")
            return

        if db.unblock_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} has been unblocked.")
        else:
            await update.message.reply_text("Operation failed. Please try again later.")
    except ValueError:
        await update.message.reply_text("Invalid parameter. Please enter a valid user ID.")


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /blacklist 命令 - 查看黑名单"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    blacklist = db.get_blacklist()

    if not blacklist:
        await update.message.reply_text("Blacklist is empty.")
        return

    msg = "📋 Blacklist:\n\n"
    for user in blacklist:
        msg += f"User ID: {user['user_id']}\n"
        msg += f"Username: @{user['username']}\n"
        msg += f"Name: {user['full_name']}\n"
        msg += "---\n"

    await update.message.reply_text(msg)


async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /genkey 命令 - 管理员生成卡密"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /genkey <key> <credits> [uses] [days]\n\n"
            "Examples:\n"
            "/genkey wandouyu 20 - 20-credit key (one-time, no expiry)\n"
            "/genkey vip100 50 10 - 50-credit key (10 uses, no expiry)\n"
            "/genkey temp 30 1 7 - 30-credit key (one-time, expires in 7 days)"
        )
        return

    try:
        key_code = context.args[0].strip()
        balance = int(context.args[1])
        max_uses = int(context.args[2]) if len(context.args) > 2 else 1
        expire_days = int(context.args[3]) if len(context.args) > 3 else None

        if balance <= 0:
            await update.message.reply_text("Credits must be greater than 0.")
            return

        if max_uses <= 0:
            await update.message.reply_text("Uses must be greater than 0.")
            return

        if db.create_card_key(key_code, balance, user_id, max_uses, expire_days):
            msg = (
                "✅ Key created successfully!\n\n"
                f"Key: {key_code}\n"
                f"Credits: {balance}\n"
                f"Uses: {max_uses}\n"
            )
            if expire_days:
                msg += f"Expires in: {expire_days} day(s)\n"
            else:
                msg += "Expires: never\n"
            msg += f"\nUser usage: /use {key_code}"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(
                "Key already exists or creation failed. Please choose another key."
            )
    except ValueError:
        await update.message.reply_text("Invalid parameters. Please enter valid numbers.")


async def listkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /listkeys 命令 - 管理员查看卡密列表"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    keys = db.get_all_card_keys()

    if not keys:
        await update.message.reply_text("No keys found.")
        return

    msg = "📋 Key list:\n\n"
    for key in keys[:20]:  # 只显示前20个
        msg += f"Key: {key['key_code']}\n"
        msg += f"Credits: {key['balance']}\n"
        msg += f"Uses: {key['current_uses']}/{key['max_uses']}\n"

        if key["expire_at"]:
            expire_time = datetime.fromisoformat(key["expire_at"])
            if datetime.now() > expire_time:
                msg += "Status: expired\n"
            else:
                days_left = (expire_time - datetime.now()).days
                msg += f"Status: active ({days_left} day(s) left)\n"
        else:
            msg += "Status: never expires\n"

        msg += "---\n"

    if len(keys) > 20:
        msg += f"\n(Showing first 20 of {len(keys)})"

    await update.message.reply_text(msg)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /broadcast 命令 - 管理员群发通知"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("You do not have permission to use this command.")
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    if not text:
        await update.message.reply_text(
            "Usage: /broadcast <text>, or reply to a message and send /broadcast"
        )
        return

    user_ids = db.get_all_user_ids()
    success, failed = 0, 0

    status_msg = await update.message.reply_text(
        f"📢 Starting broadcast to {len(user_ids)} users..."
    )

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            success += 1
            await asyncio.sleep(0.05)  # 适当限速避免触发限制
        except Exception as e:
            logger.warning("广播到 %s 失败: %s", uid, e)
            failed += 1

    await status_msg.edit_text(f"✅ Broadcast complete!\nSent: {success}\nFailed: {failed}")


async def error_handler(update: object, context) -> None:
    """全局错误处理"""
    logger.exception("Exception while handling update: %s", context.error, exc_info=context.error)


def main():
    """主函数"""
    # 初始化数据库
    db = Database()

    # 创建应用 - 启用并发处理
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # 🔥 关键：启用并发处理多个命令
        .build()
    )

    # 注册用户命令（使用 partial 传递 db 参数）
    application.add_handler(CommandHandler("start", partial(start_command, db=db)))
    application.add_handler(CommandHandler("about", partial(about_command, db=db)))
    application.add_handler(CommandHandler("help", partial(help_command, db=db)))
    application.add_handler(CommandHandler("balance", partial(balance_command, db=db)))
    application.add_handler(CommandHandler("qd", partial(checkin_command, db=db)))
    application.add_handler(CommandHandler("invite", partial(invite_command, db=db)))
    application.add_handler(CommandHandler("use", partial(use_command, db=db)))

    # 注册验证命令
    application.add_handler(CommandHandler("verify", partial(verify_command, db=db)))
    application.add_handler(CommandHandler("verify2", partial(verify2_command, db=db)))
    application.add_handler(CommandHandler("verify3", partial(verify3_command, db=db)))
    application.add_handler(CommandHandler("verify4", partial(verify4_command, db=db)))
    application.add_handler(CommandHandler("verify5", partial(verify5_command, db=db)))
    application.add_handler(CommandHandler("verify6", partial(verify6_command, db=db)))
    application.add_handler(CommandHandler("getV4Code", partial(getV4Code_command, db=db)))

    # 注册管理员命令
    application.add_handler(CommandHandler("admin", partial(admin_command, db=db)))
    application.add_handler(CommandHandler("addbalance", partial(addbalance_command, db=db)))
    application.add_handler(CommandHandler("block", partial(block_command, db=db)))
    application.add_handler(CommandHandler("white", partial(white_command, db=db)))
    application.add_handler(CommandHandler("blacklist", partial(blacklist_command, db=db)))
    application.add_handler(CommandHandler("genkey", partial(genkey_command, db=db)))
    application.add_handler(CommandHandler("listkeys", partial(listkeys_command, db=db)))
    application.add_handler(CommandHandler("broadcast", partial(broadcast_command, db=db)))
    application.add_handler(CallbackQueryHandler(partial(handle_admin_callback, db=db)))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, partial(handle_admin_input, db=db))
    )

    # 注册错误处理器
    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
