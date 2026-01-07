"""Telegram 机器人主程序（单文件版）"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from functools import partial
from typing import Dict, List, Optional

import httpx
import psutil
import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

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

# 帮助链接
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
    """群聊限制：仅允许 /verify /verify2 /verify3 /verify4 /verify5 /qd"""
    if is_group_chat(update):
        await update.message.reply_text(
            "群聊仅支持 /verify /verify2 /verify3 /verify4 /verify5 /qd，请私聊使用其他命令。"
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
        f"🎉 欢迎，{full_name}！\n"
        "您已成功注册，获得 1 积分。\n"
    )
    if invited_by:
        msg += "感谢通过邀请链接加入，邀请人已获得 2 积分。\n"

    msg += (
        "\n本机器人可自动完成 SheerID 认证。\n"
        "快速开始：\n"
        "/about - 了解机器人功能\n"
        "/balance - 查看积分余额\n"
        "/help - 查看完整命令列表\n\n"
        "获取更多积分：\n"
        "/qd - 每日签到\n"
        "/invite - 邀请好友\n"
        f"加入频道：{CHANNEL_URL}"
    )
    return msg


def get_about_message() -> str:
    """获取关于消息"""
    return (
        "🤖 SheerID 自动认证机器人\n"
        "\n"
        "功能介绍:\n"
        "- 自动完成 SheerID 学生/教师认证\n"
        "- 支持 Gemini One Pro、ChatGPT Teacher K12、Spotify Student、YouTube Student、Bolt.new Teacher 认证\n"
        "\n"
        "积分获取:\n"
        "- 注册赠送 1 积分\n"
        "- 每日签到 +1 积分\n"
        "- 邀请好友 +2 积分/人\n"
        "- 使用卡密（按卡密规则）\n"
        f"- 加入频道：{CHANNEL_URL}\n"
        "\n"
        "使用方法:\n"
        "1. 在网页开始认证并复制完整的验证链接\n"
        "2. 发送 /verify、/verify2、/verify3、/verify4 或 /verify5 携带该链接\n"
        "3. 等待处理并查看结果\n"
        "4. Bolt.new 认证会自动获取认证码，如需手动查询使用 /getV4Code <verification_id>\n"
        "\n"
        "更多命令请发送 /help"
    )


def get_help_message(is_admin: bool = False) -> str:
    """获取帮助消息"""
    msg = (
        "📖 SheerID 自动认证机器人 - 帮助\n"
        "\n"
        "用户命令:\n"
        "/start - 开始使用（注册）\n"
        "/about - 了解机器人功能\n"
        "/balance - 查看积分余额\n"
        "/qd - 每日签到（+1积分）\n"
        "/invite - 生成邀请链接（+2积分/人）\n"
        "/use <卡密> - 使用卡密兑换积分\n"
        f"/verify <链接> - Gemini One Pro 认证（-{VERIFY_COST}积分）\n"
        f"/verify2 <链接> - ChatGPT Teacher K12 认证（-{VERIFY_COST}积分）\n"
        f"/verify3 <链接> - Spotify Student 认证（-{VERIFY_COST}积分）\n"
        f"/verify4 <链接> - Bolt.new Teacher 认证（-{VERIFY_COST}积分）\n"
        f"/verify5 <链接> - YouTube Student Premium 认证（-{VERIFY_COST}积分）\n"
        "/getV4Code <verification_id> - 获取 Bolt.new 认证码\n"
        "/help - 查看此帮助信息\n"
        f"认证失败查看：{HELP_NOTION_URL}\n"
    )

    if is_admin:
        msg += (
            "\n管理员命令:\n"
            "/addbalance <用户ID> <积分> - 增加用户积分\n"
            "/block <用户ID> - 拉黑用户\n"
            "/white <用户ID> - 取消拉黑\n"
            "/blacklist - 查看黑名单\n"
            "/genkey <卡密> <积分> [次数] [天数] - 生成卡密\n"
            "/listkeys - 查看卡密列表\n"
            "/broadcast <文本> - 向所有用户群发通知\n"
        )

    return msg


def get_insufficient_balance_message(current_balance: int) -> str:
    """获取积分不足消息"""
    return (
        f"积分不足！需要 {VERIFY_COST} 积分，当前 {current_balance} 积分。\n\n"
        "获取积分方式:\n"
        "- 每日签到 /qd\n"
        "- 邀请好友 /invite\n"
        "- 使用卡密 /use <卡密>"
    )


def get_verify_usage_message(command: str, service_name: str) -> str:
    """获取验证命令使用说明"""
    return (
        f"使用方法: {command} <SheerID链接>\n\n"
        "示例:\n"
        f"{command} https://services.sheerid.com/verify/xxx/?verificationId=xxx\n\n"
        "获取验证链接:\n"
        f"1. 访问 {service_name} 认证页面\n"
        "2. 开始认证流程\n"
        "3. 复制浏览器地址栏中的完整 URL\n"
        f"4. 使用 {command} 命令提交"
    )


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
            f"欢迎回来，{full_name}！\n" "您已经初始化过了。\n" "发送 /help 查看可用命令。"
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
        await update.message.reply_text("注册失败，请稍后重试。")


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
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    user = db.get_user(user_id)
    if not user:
        await update.message.reply_text("请先使用 /start 注册。")
        return

    await update.message.reply_text(f"💰 积分余额\n\n当前积分：{user['balance']} 分")


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
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
        return

    # 第1层检查：在命令处理器层面检查
    if not db.can_checkin(user_id):
        await update.message.reply_text("❌ 今天已经签到过了，明天再来吧。")
        return

    # 第2层检查：在数据库层面执行（SQL原子操作）
    if db.checkin(user_id):
        user = db.get_user(user_id)
        await update.message.reply_text(
            f"✅ 签到成功！\n获得积分：+1\n当前积分：{user['balance']} 分"
        )
    else:
        # 如果数据库层面返回False，说明今天已签到（双重保险）
        await update.message.reply_text("❌ 今天已经签到过了，明天再来吧。")


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /invite 邀请命令"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
        return

    bot_username = context.bot.username
    invite_link = f"https://t.me/{bot_username}?start={user_id}"

    await update.message.reply_text(
        f"🎁 您的专属邀请链接：\n{invite_link}\n\n" "每邀请 1 位成功注册，您将获得 2 积分。"
    )


async def use_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /use 命令 - 使用卡密"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
        return

    if not context.args:
        await update.message.reply_text("使用方法: /use <卡密>\n\n示例: /use wandouyu")
        return

    key_code = context.args[0].strip()
    result = db.use_card_key(key_code, user_id)

    if result is None:
        await update.message.reply_text("卡密不存在，请检查后重试。")
    elif result == -1:
        await update.message.reply_text("该卡密已达到使用次数上限。")
    elif result == -2:
        await update.message.reply_text("该卡密已过期。")
    elif result == -3:
        await update.message.reply_text("您已经使用过该卡密。")
    else:
        user = db.get_user(user_id)
        await update.message.reply_text(
            f"卡密使用成功！\n获得积分：{result}\n当前积分：{user['balance']}"
        )


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify 命令 - Gemini One Pro"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
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
        await update.message.reply_text("无效的 SheerID 链接，请检查后重试。")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("扣除积分失败，请稍后重试。")
        return

    processing_msg = await update.message.reply_text(
        f"开始处理 Gemini One Pro 认证...\n"
        f"验证ID: {verification_id}\n"
        f"已扣除 {VERIFY_COST} 积分\n\n"
        "请稍候，这可能需要 1-2 分钟..."
    )

    try:
        verifier = OneVerifier(verification_id)
        result = await asyncio.to_thread(verifier.verify)

        db.add_verification(
            user_id,
            "gemini_one_pro",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ 认证成功！\n\n"
            if result.get("pending"):
                result_msg += "文档已提交，等待人工审核。\n"
            if result.get("redirect_url"):
                result_msg += f"跳转链接：\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ 认证失败：{result.get('message', '未知错误')}\n\n"
                f"已退回 {VERIFY_COST} 积分"
            )
    except Exception as e:
        logger.error("验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ 处理过程中出现错误：{str(e)}\n\n" f"已退回 {VERIFY_COST} 积分"
        )


async def verify2_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify2 命令 - ChatGPT Teacher K12"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
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
        await update.message.reply_text("无效的 SheerID 链接，请检查后重试。")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("扣除积分失败，请稍后重试。")
        return

    processing_msg = await update.message.reply_text(
        f"开始处理 ChatGPT Teacher K12 认证...\n"
        f"验证ID: {verification_id}\n"
        f"已扣除 {VERIFY_COST} 积分\n\n"
        "请稍候，这可能需要 1-2 分钟..."
    )

    try:
        verifier = K12Verifier(verification_id)
        result = await asyncio.to_thread(verifier.verify)

        db.add_verification(
            user_id,
            "chatgpt_teacher_k12",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ 认证成功！\n\n"
            if result.get("pending"):
                result_msg += "文档已提交，等待人工审核。\n"
            if result.get("redirect_url"):
                result_msg += f"跳转链接：\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ 认证失败：{result.get('message', '未知错误')}\n\n"
                f"已退回 {VERIFY_COST} 积分"
            )
    except Exception as e:
        logger.error("验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ 处理过程中出现错误：{str(e)}\n\n" f"已退回 {VERIFY_COST} 积分"
        )


async def verify3_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify3 命令 - Spotify Student"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
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
        await update.message.reply_text("无效的 SheerID 链接，请检查后重试。")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("扣除积分失败，请稍后重试。")
        return

    processing_msg = await update.message.reply_text(
        f"🎵 开始处理 Spotify Student 认证...\n"
        f"已扣除 {VERIFY_COST} 积分\n\n"
        "📝 正在生成学生信息...\n"
        "🎨 正在生成学生证 PNG...\n"
        "📤 正在提交文档..."
    )

    # 使用信号量控制并发
    semaphore = get_verification_semaphore("spotify_student")

    try:
        async with semaphore:
            verifier = SpotifyVerifier(verification_id)
            result = await asyncio.to_thread(verifier.verify)

        db.add_verification(
            user_id,
            "spotify_student",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ Spotify 学生认证成功！\n\n"
            if result.get("pending"):
                result_msg += "✨ 文档已提交，等待 SheerID 审核\n"
                result_msg += "⏱️ 预计审核时间：几分钟内\n\n"
            if result.get("redirect_url"):
                result_msg += f"🔗 跳转链接：\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ 认证失败：{result.get('message', '未知错误')}\n\n"
                f"已退回 {VERIFY_COST} 积分"
            )
    except Exception as e:
        logger.error("Spotify 验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ 处理过程中出现错误：{str(e)}\n\n" f"已退回 {VERIFY_COST} 积分"
        )


async def verify4_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /verify4 命令 - Bolt.new Teacher（自动获取code版）"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
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
        await update.message.reply_text("无效的 SheerID 链接，请检查后重试。")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("扣除积分失败，请稍后重试。")
        return

    processing_msg = await update.message.reply_text(
        f"🚀 开始处理 Bolt.new Teacher 认证...\n"
        f"已扣除 {VERIFY_COST} 积分\n\n"
        "📤 正在提交文档..."
    )

    # 使用信号量控制并发
    semaphore = get_verification_semaphore("bolt_teacher")

    try:
        async with semaphore:
            # 第1步：提交文档
            verifier = BoltnewVerifier(url, verification_id=verification_id)
            result = await asyncio.to_thread(verifier.verify)

        if not result.get("success"):
            # 提交失败，退款
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ 文档提交失败：{result.get('message', '未知错误')}\n\n"
                f"已退回 {VERIFY_COST} 积分"
            )
            return

        vid = result.get("verification_id", "")
        if not vid:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(f"❌ 未获取到验证ID\n\n" f"已退回 {VERIFY_COST} 积分")
            return

        # 更新消息
        await processing_msg.edit_text(
            f"✅ 文档已提交！\n"
            f"📋 验证ID: `{vid}`\n\n"
            f"🔍 正在自动获取认证码...\n"
            f"（最多等待20秒）"
        )

        # 第2步：自动获取认证码（最多20秒）
        code = await _auto_get_reward_code(vid, max_wait=20, interval=5)

        if code:
            # 成功获取
            result_msg = (
                f"🎉 认证成功！\n\n"
                f"✅ 文档已提交\n"
                f"✅ 审核已通过\n"
                f"✅ 认证码已获取\n\n"
                f"🎁 认证码: `{code}`\n"
            )
            if result.get("redirect_url"):
                result_msg += f"\n🔗 跳转链接:\n{result['redirect_url']}"

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
                f"✅ 文档已提交成功！\n\n"
                f"⏳ 认证码尚未生成（可能需要1-5分钟审核）\n\n"
                f"📋 验证ID: `{vid}`\n\n"
                f"💡 请稍后使用以下命令查询:\n"
                f"`/getV4Code {vid}`\n\n"
                f"注意：积分已消耗，稍后查询无需再付费"
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
            f"❌ 处理过程中出现错误：{str(e)}\n\n" f"已退回 {VERIFY_COST} 积分"
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
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
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
        await update.message.reply_text("无效的 SheerID 链接，请检查后重试。")
        return

    if not db.deduct_balance(user_id, VERIFY_COST):
        await update.message.reply_text("扣除积分失败，请稍后重试。")
        return

    processing_msg = await update.message.reply_text(
        f"📺 开始处理 YouTube Student Premium 认证...\n"
        f"已扣除 {VERIFY_COST} 积分\n\n"
        "📝 正在生成学生信息...\n"
        "🎨 正在生成学生证 PNG...\n"
        "📤 正在提交文档..."
    )

    # 使用信号量控制并发
    semaphore = get_verification_semaphore("youtube_student")

    try:
        async with semaphore:
            verifier = YouTubeVerifier(verification_id)
            result = await asyncio.to_thread(verifier.verify)

        db.add_verification(
            user_id,
            "youtube_student",
            url,
            "success" if result["success"] else "failed",
            str(result),
        )

        if result["success"]:
            result_msg = "✅ YouTube Student Premium 认证成功！\n\n"
            if result.get("pending"):
                result_msg += "✨ 文档已提交，等待 SheerID 审核\n"
                result_msg += "⏱️ 预计审核时间：几分钟内\n\n"
            if result.get("redirect_url"):
                result_msg += f"🔗 跳转链接：\n{result['redirect_url']}"
            await processing_msg.edit_text(result_msg)
        else:
            db.add_balance(user_id, VERIFY_COST)
            await processing_msg.edit_text(
                f"❌ 认证失败：{result.get('message', '未知错误')}\n\n"
                f"已退回 {VERIFY_COST} 积分"
            )
    except Exception as e:
        logger.error("YouTube 验证过程出错: %s", e)
        db.add_balance(user_id, VERIFY_COST)
        await processing_msg.edit_text(
            f"❌ 处理过程中出现错误：{str(e)}\n\n" f"已退回 {VERIFY_COST} 积分"
        )


async def getV4Code_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /getV4Code 命令 - 获取 Bolt.new Teacher 认证码"""
    user_id = update.effective_user.id

    if db.is_user_blocked(user_id):
        await update.message.reply_text("您已被拉黑，无法使用此功能。")
        return

    if not db.user_exists(user_id):
        await update.message.reply_text("请先使用 /start 注册。")
        return

    # 检查是否提供了 verification_id
    if not context.args:
        await update.message.reply_text(
            "使用方法: /getV4Code <verification_id>\n\n"
            "示例: /getV4Code 6929436b50d7dc18638890d0\n\n"
            "verification_id 在使用 /verify4 命令后会返回给您。"
        )
        return

    verification_id = context.args[0].strip()

    processing_msg = await update.message.reply_text("🔍 正在查询认证码，请稍候...")

    try:
        # 查询 SheerID API 获取认证码
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://my.sheerid.com/rest/v2/verification/{verification_id}"
            )

            if response.status_code != 200:
                await processing_msg.edit_text(
                    f"❌ 查询失败，状态码：{response.status_code}\n\n" "请稍后重试或联系管理员。"
                )
                return

            data = response.json()
            current_step = data.get("currentStep")
            reward_code = data.get("rewardCode") or data.get("rewardData", {}).get(
                "rewardCode"
            )
            redirect_url = data.get("redirectUrl")

            if current_step == "success" and reward_code:
                result_msg = "✅ 认证成功！\n\n"
                result_msg += f"🎉 认证码：`{reward_code}`\n\n"
                if redirect_url:
                    result_msg += f"跳转链接：\n{redirect_url}"
                await processing_msg.edit_text(result_msg)
            elif current_step == "pending":
                await processing_msg.edit_text(
                    "⏳ 认证仍在审核中，请稍后再试。\n\n" "通常需要 1-5 分钟，请耐心等待。"
                )
            elif current_step == "error":
                error_ids = data.get("errorIds", [])
                await processing_msg.edit_text(
                    f"❌ 认证失败\n\n"
                    f"错误信息：{', '.join(error_ids) if error_ids else '未知错误'}"
                )
            else:
                await processing_msg.edit_text(
                    f"⚠️ 当前状态：{current_step}\n\n" "认证码尚未生成，请稍后重试。"
                )

    except Exception as e:
        logger.error("获取 Bolt.new 认证码失败: %s", e)
        await processing_msg.edit_text(
            f"❌ 查询过程中出现错误：{str(e)}\n\n" "请稍后重试或联系管理员。"
        )


async def addbalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /addbalance 命令 - 管理员增加积分"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "使用方法: /addbalance <用户ID> <积分数量>\n\n示例: /addbalance 123456789 10"
        )
        return

    try:
        target_user_id = int(context.args[0])
        amount = int(context.args[1])

        if not db.user_exists(target_user_id):
            await update.message.reply_text("用户不存在。")
            return

        if db.add_balance(target_user_id, amount):
            user = db.get_user(target_user_id)
            await update.message.reply_text(
                f"✅ 成功为用户 {target_user_id} 增加 {amount} 积分。\n"
                f"当前积分：{user['balance']}"
            )
        else:
            await update.message.reply_text("操作失败，请稍后重试。")
    except ValueError:
        await update.message.reply_text("参数格式错误，请输入有效的数字。")


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /block 命令 - 管理员拉黑用户"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    if not context.args:
        await update.message.reply_text("使用方法: /block <用户ID>\n\n示例: /block 123456789")
        return

    try:
        target_user_id = int(context.args[0])

        if not db.user_exists(target_user_id):
            await update.message.reply_text("用户不存在。")
            return

        if db.block_user(target_user_id):
            await update.message.reply_text(f"✅ 已拉黑用户 {target_user_id}。")
        else:
            await update.message.reply_text("操作失败，请稍后重试。")
    except ValueError:
        await update.message.reply_text("参数格式错误，请输入有效的用户ID。")


async def white_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /white 命令 - 管理员取消拉黑"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    if not context.args:
        await update.message.reply_text("使用方法: /white <用户ID>\n\n示例: /white 123456789")
        return

    try:
        target_user_id = int(context.args[0])

        if not db.user_exists(target_user_id):
            await update.message.reply_text("用户不存在。")
            return

        if db.unblock_user(target_user_id):
            await update.message.reply_text(f"✅ 已将用户 {target_user_id} 移出黑名单。")
        else:
            await update.message.reply_text("操作失败，请稍后重试。")
    except ValueError:
        await update.message.reply_text("参数格式错误，请输入有效的用户ID。")


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /blacklist 命令 - 查看黑名单"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    blacklist = db.get_blacklist()

    if not blacklist:
        await update.message.reply_text("黑名单为空。")
        return

    msg = "📋 黑名单列表：\n\n"
    for user in blacklist:
        msg += f"用户ID: {user['user_id']}\n"
        msg += f"用户名: @{user['username']}\n"
        msg += f"姓名: {user['full_name']}\n"
        msg += "---\n"

    await update.message.reply_text(msg)


async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /genkey 命令 - 管理员生成卡密"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "使用方法: /genkey <卡密> <积分> [使用次数] [过期天数]\n\n"
            "示例:\n"
            "/genkey wandouyu 20 - 生成20积分的卡密（单次使用，永不过期）\n"
            "/genkey vip100 50 10 - 生成50积分的卡密（可使用10次，永不过期）\n"
            "/genkey temp 30 1 7 - 生成30积分的卡密（单次使用，7天后过期）"
        )
        return

    try:
        key_code = context.args[0].strip()
        balance = int(context.args[1])
        max_uses = int(context.args[2]) if len(context.args) > 2 else 1
        expire_days = int(context.args[3]) if len(context.args) > 3 else None

        if balance <= 0:
            await update.message.reply_text("积分数量必须大于0。")
            return

        if max_uses <= 0:
            await update.message.reply_text("使用次数必须大于0。")
            return

        if db.create_card_key(key_code, balance, user_id, max_uses, expire_days):
            msg = (
                "✅ 卡密生成成功！\n\n"
                f"卡密：{key_code}\n"
                f"积分：{balance}\n"
                f"使用次数：{max_uses}次\n"
            )
            if expire_days:
                msg += f"有效期：{expire_days}天\n"
            else:
                msg += "有效期：永久\n"
            msg += f"\n用户使用方法: /use {key_code}"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("卡密已存在或生成失败，请更换卡密名称。")
    except ValueError:
        await update.message.reply_text("参数格式错误，请输入有效的数字。")


async def listkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /listkeys 命令 - 管理员查看卡密列表"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id

    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    keys = db.get_all_card_keys()

    if not keys:
        await update.message.reply_text("暂无卡密。")
        return

    msg = "📋 卡密列表：\n\n"
    for key in keys[:20]:  # 只显示前20个
        msg += f"卡密：{key['key_code']}\n"
        msg += f"积分：{key['balance']}\n"
        msg += f"使用次数：{key['current_uses']}/{key['max_uses']}\n"

        if key["expire_at"]:
            expire_time = datetime.fromisoformat(key["expire_at"])
            if datetime.now() > expire_time:
                msg += "状态：已过期\n"
            else:
                days_left = (expire_time - datetime.now()).days
                msg += f"状态：有效（剩余{days_left}天）\n"
        else:
            msg += "状态：永久有效\n"

        msg += "---\n"

    if len(keys) > 20:
        msg += f"\n（仅显示前20个，共{len(keys)}个）"

    await update.message.reply_text(msg)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database):
    """处理 /broadcast 命令 - 管理员群发通知"""
    if await reject_group_command(update):
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("您没有权限使用此命令。")
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    if not text:
        await update.message.reply_text(
            "使用方法: /broadcast <文本>，或回复一条消息后发送 /broadcast"
        )
        return

    user_ids = db.get_all_user_ids()
    success, failed = 0, 0

    status_msg = await update.message.reply_text(f"📢 开始广播，共 {len(user_ids)} 个用户...")

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            success += 1
            await asyncio.sleep(0.05)  # 适当限速避免触发限制
        except Exception as e:
            logger.warning("广播到 %s 失败: %s", uid, e)
            failed += 1

    await status_msg.edit_text(f"✅ 广播完成！\n成功：{success}\n失败：{failed}")


async def error_handler(update: object, context) -> None:
    """全局错误处理"""
    logger.exception("处理更新时发生异常: %s", context.error, exc_info=context.error)


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
    application.add_handler(CommandHandler("getV4Code", partial(getV4Code_command, db=db)))

    # 注册管理员命令
    application.add_handler(CommandHandler("addbalance", partial(addbalance_command, db=db)))
    application.add_handler(CommandHandler("block", partial(block_command, db=db)))
    application.add_handler(CommandHandler("white", partial(white_command, db=db)))
    application.add_handler(CommandHandler("blacklist", partial(blacklist_command, db=db)))
    application.add_handler(CommandHandler("genkey", partial(genkey_command, db=db)))
    application.add_handler(CommandHandler("listkeys", partial(listkeys_command, db=db)))
    application.add_handler(CommandHandler("broadcast", partial(broadcast_command, db=db)))

    # 注册错误处理器
    application.add_error_handler(error_handler)

    logger.info("机器人启动中...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
