"""
核心业务逻辑：STT、LLM 优化、飞书 OAuth、飞书发消息。
从原项目 voice_to_feishu.py 提取，独立运行，不依赖原项目。
"""
import io
import json
import os
import uuid
import wave
import struct
import gzip
import asyncio
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import aiohttp
import requests

import config

# ============================================================
# token 存储路径（放在本项目目录下）
# ============================================================
_TOKEN_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_TOKEN_DIR, ".feishu_user_token.json")  # 向后兼容

def _token_file(app_id: str) -> str:
    """每个机器人独立 token 文件，避免多机器人互相覆盖。"""
    return os.path.join(_TOKEN_DIR, f".feishu_token_{app_id}.json")

# ============================================================
# 音频工具
# ============================================================
SAMPLE_RATE = 16000
CHANNELS = 1

def extract_pcm_from_wav_bytes(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        if channels != CHANNELS or sample_width != 2 or sample_rate != SAMPLE_RATE:
            raise Exception(
                f"不支持的音频格式: {channels}ch/{sample_width * 8}bit/{sample_rate}Hz，"
                f"需要 {CHANNELS}ch/16bit/{SAMPLE_RATE}Hz"
            )
        return wf.readframes(wf.getnframes())

# ============================================================
# 火山引擎 STT（官方二进制协议）
# ============================================================
PROTOCOL_VERSION = 0b0001
MSG_CLIENT_FULL   = 0b0001
MSG_CLIENT_AUDIO  = 0b0010
MSG_SERVER_FULL   = 0b1001
MSG_SERVER_ERROR  = 0b1111
FLAG_POS_SEQ      = 0b0001
FLAG_NEG_WITH_SEQ = 0b0011
SERIAL_JSON       = 0b0001
COMPRESS_GZIP     = 0b0001

def _make_header(msg_type, flags):
    h = bytearray(4)
    h[0] = (PROTOCOL_VERSION << 4) | 1
    h[1] = (msg_type << 4) | flags
    h[2] = (SERIAL_JSON << 4) | COMPRESS_GZIP
    h[3] = 0x00
    return bytes(h)

def _make_full_request(seq):
    payload = {
        "user": {"uid": "feishu_lan_server"},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "enable_ddc": True},
    }
    compressed = gzip.compress(json.dumps(payload).encode())
    header = _make_header(MSG_CLIENT_FULL, FLAG_POS_SEQ)
    return header + struct.pack(">i", seq) + struct.pack(">I", len(compressed)) + compressed

def _make_audio_request(seq, segment, is_last):
    flags = FLAG_NEG_WITH_SEQ if is_last else FLAG_POS_SEQ
    actual_seq = -seq if is_last else seq
    compressed = gzip.compress(segment)
    header = _make_header(MSG_CLIENT_AUDIO, flags)
    return header + struct.pack(">i", actual_seq) + struct.pack(">I", len(compressed)) + compressed

def _parse_response(data):
    header_size = data[0] & 0x0f
    msg_type = data[1] >> 4
    flags = data[1] & 0x0f
    compression = data[2] & 0x0f
    payload = data[header_size * 4:]
    is_last = bool(flags & 0x02)
    code = 0
    if flags & 0x01:
        payload = payload[4:]
    if flags & 0x04:
        payload = payload[4:]
    if msg_type == MSG_SERVER_ERROR:
        code = struct.unpack(">i", payload[:4])[0]
        payload = payload[8:]
    elif msg_type == MSG_SERVER_FULL:
        payload = payload[4:]
    if not payload:
        return None, is_last, code
    if compression == COMPRESS_GZIP:
        try:
            payload = gzip.decompress(payload)
        except Exception:
            return None, is_last, code
    try:
        return json.loads(payload.decode()), is_last, code
    except Exception:
        return None, is_last, code

async def stt_recognize(audio_data, *, is_wav_file=False):
    if is_wav_file:
        pcm_bytes = extract_pcm_from_wav_bytes(audio_data)
    else:
        pcm_bytes = audio_data

    url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    req_id = str(uuid.uuid4())
    headers = {
        "X-Api-App-Key": config.VOLC_APP_ID,
        "X-Api-Access-Key": config.VOLC_ACCESS_TOKEN,
        "X-Api-Resource-Id": "volc.seedasr.sauc.duration",
        "X-Api-Request-Id": req_id,
        "X-Api-Connect-Id": req_id,
    }

    result_text = ""
    seq = 1

    import ssl as _ssl
    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, headers=headers, ssl=ssl_ctx) as ws:
            await ws.send_bytes(_make_full_request(seq))
            seq += 1
            msg = await ws.receive()
            if msg.type != aiohttp.WSMsgType.BINARY:
                raise Exception(f"建连失败: {msg}")

            chunk_ms = 200
            chunk_size = SAMPLE_RATE * 2 * chunk_ms // 1000
            segments = [pcm_bytes[i:i + chunk_size] for i in range(0, len(pcm_bytes), chunk_size)]
            total = len(segments)

            async def send_audio():
                nonlocal seq
                for i, seg in enumerate(segments):
                    is_last = (i == total - 1)
                    await ws.send_bytes(_make_audio_request(seq, seg, is_last))
                    if not is_last:
                        seq += 1

            sender = asyncio.create_task(send_audio())

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    parsed, is_last, code = _parse_response(msg.data)
                    if parsed:
                        text = parsed.get("result", {}).get("text", "")
                        if text:
                            result_text = text
                    if is_last or code != 0:
                        break
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break

            sender.cancel()

    return result_text

# ============================================================
# 大模型文本优化
# ============================================================

def llm_polish(text):
    if config.LLM_PROVIDER == "minimax":
        return _llm_polish_minimax(text)
    return _llm_polish_doubao(text)

def _llm_polish_doubao(text):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.ARK_API_KEY}"}
    payload = {
        "model": config.ARK_MODEL,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    resp = requests.post(config.ARK_BASE_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def _llm_polish_minimax(text):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.MINIMAX_API_KEY}"}
    payload = {
        "model": config.MINIMAX_MODEL,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    resp = requests.post(config.MINIMAX_BASE_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

# ============================================================
# 飞书 OAuth（用户身份）
# ============================================================

def _load_token(app_id=None):
    path = _token_file(app_id) if app_id else TOKEN_FILE
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def _save_token(token_data, app_id=None):
    path = _token_file(app_id) if app_id else TOKEN_FILE
    with open(path, "w") as f:
        json.dump(token_data, f, ensure_ascii=False, indent=2)

def _get_app_access_token(app_id=None, app_secret=None):
    aid  = app_id     or config.FEISHU_APP_ID
    asec = app_secret or config.FEISHU_APP_SECRET
    url = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
    resp = requests.post(url, json={"app_id": aid, "app_secret": asec})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取 app_access_token 失败: {data}")
    return data["app_access_token"]

def _refresh_user_token(refresh_token, app_id=None, app_secret=None):
    app_token = _get_app_access_token(app_id, app_secret)
    url = "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token"
    headers = {"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json={"grant_type": "refresh_token", "refresh_token": refresh_token})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        return None
    return data["data"]

def _oauth_login(app_id=None, app_secret=None):
    aid = app_id or config.FEISHU_APP_ID
    auth_code = None

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            query = parse_qs(urlparse(self.path).query)
            auth_code = query.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("✅ 授权成功！请回到终端继续操作。".encode())

        def log_message(self, format, *args):
            pass

    port = 9876
    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    auth_url = (
        f"https://open.feishu.cn/open-apis/authen/v1/authorize"
        f"?app_id={aid}"
        f"&redirect_uri=http://localhost:9876/callback"
        f"&scope=im:message.send_as_user"
    )
    print("\n🔐 需要飞书授权，正在打开浏览器...")
    webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()

    if not auth_code:
        raise Exception("未获取到授权码，请重试")

    app_token = _get_app_access_token(app_id, app_secret)
    url = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
    headers = {"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json={"grant_type": "authorization_code", "code": auth_code})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"换取 token 失败: {data}")
    token_data = data["data"]
    _save_token(token_data, app_id)
    return token_data["access_token"]

def get_user_access_token(app_id=None, app_secret=None):
    saved = _load_token(app_id)
    if saved and saved.get("refresh_token"):
        new_data = _refresh_user_token(saved["refresh_token"], app_id, app_secret)
        if new_data:
            _save_token(new_data, app_id)
            return new_data["access_token"]
    return _oauth_login(app_id, app_secret)

# ============================================================
# 飞书发消息
# ============================================================

def send_feishu_message(text, receive_id=None, at_user_id=None, app_id=None, app_secret=None):
    rid = receive_id or config.FEISHU_RECEIVE_ID
    at_uid = at_user_id if at_user_id is not None else config.AT_USER_ID

    token = get_user_access_token(app_id, app_secret)
    url = "https://open.feishu.cn/open-apis/im/v1/messages"

    receive_id_type = "chat_id"
    if rid.startswith("ou_"):
        receive_id_type = "open_id"
    elif rid.startswith("on_"):
        receive_id_type = "union_id"
    elif "@" in rid:
        receive_id_type = "email"

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    message_text = text
    if at_uid:
        if at_uid.lower() == "all":
            message_text = f'<at user_id="all">所有人</at> {text}'
        else:
            message_text = f'<at user_id="{at_uid}"></at> {text}'

    payload = {
        "receive_id": rid,
        "msg_type": "text",
        "content": json.dumps({"text": message_text}),
    }
    resp = requests.post(url, headers=headers, json=payload, params={"receive_id_type": receive_id_type})
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 0:
        raise Exception(f"飞书发送失败 (HTTP {resp.status_code}): {json.dumps(data, ensure_ascii=False)}")
    return True
