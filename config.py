"""
统一配置读取，优先读取 .env，回退到环境变量，再回退到安全默认值。
开源版本不内置任何私有密钥、机器人配置或默认接收目标。
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# 服务器监听配置
PORT = int(os.environ.get("PORT", "5005"))
HOST = os.environ.get("HOST", "0.0.0.0")

# 可选 API Key 认证（空字符串 = 局域网信任模式）
API_KEY = os.environ.get("API_KEY", "")

# 超时（秒）
STT_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "60"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "30"))

# 联系人文件（放在本项目目录下）
CONTACTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contacts.json")
BOTS_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots.json")

# ============================================================
# 火山引擎 STT
# ============================================================
VOLC_APP_ID       = os.environ.get("VOLC_APP_ID",       "")
VOLC_ACCESS_TOKEN = os.environ.get("VOLC_ACCESS_TOKEN", "")

# ============================================================
# 大模型（LLM_PROVIDER: doubao | minimax）
# ============================================================
LLM_PROVIDER   = os.environ.get("LLM_PROVIDER",   "doubao")

# 豆包
ARK_API_KEY    = os.environ.get("ARK_API_KEY",    "")
ARK_MODEL      = os.environ.get("ARK_MODEL",      "doubao-seed-2-0-lite-260215")
ARK_BASE_URL   = os.environ.get("ARK_BASE_URL",   "https://ark.cn-beijing.volces.com/api/v3/chat/completions")

# MiniMax
MINIMAX_API_KEY  = os.environ.get("MINIMAX_API_KEY",  "")
MINIMAX_MODEL    = os.environ.get("MINIMAX_MODEL",    "abab6.5s-chat")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1/text/chatcompletion_v2")

# 大模型优化提示词
_DEFAULT_PROMPT = """你是一个文本优化助手。
用户会给你一段语音识别出来的文字，可能包含口语化表达、语气词（嗯、啊、那个等）、重复内容。
请你：
1. 去除口语化语气词和填充词
2. 修正明显的识别错误
3. 优化句子结构，使其更简洁清晰
4. 保持原意，不要过度修改
5. 只返回优化后的文本，不要加任何解释
"""
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", _DEFAULT_PROMPT)

# ============================================================
# 飞书
# ============================================================
FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID",     "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# 默认接收者和 @ 目标
FEISHU_RECEIVE_ID = os.environ.get("FEISHU_RECEIVE_ID", "")
AT_USER_ID        = os.environ.get("AT_USER_ID",        "")
