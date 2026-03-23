#!/usr/bin/env python3
"""
飞书局域网服务器 - 供 iPhone Siri Shortcuts 调用
iPhone 录音 -> POST /recognize -> STT + LLM 优化 -> 返回文本
用户确认后   -> POST /send     -> 发送到飞书
"""
import json
import asyncio
import logging
import concurrent.futures
from pathlib import Path

_thread_pool = concurrent.futures.ThreadPoolExecutor()

def _to_thread(func, *args, **kwargs):
    """兼容 Python 3.8 的 asyncio.to_thread 替代"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_thread_pool, lambda: func(*args, **kwargs))
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Request, UploadFile, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
from pydantic import BaseModel

import config
from audio_converter import smart_convert, AudioConversionError
from core import stt_recognize, llm_polish, send_feishu_message

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("feishu_lan_server")


# ============================================================
# 应用生命周期
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    import os
    from core import TOKEN_FILE
    lan_ip = _get_lan_ip()
    logger.info(f"服务器启动 → http://{lan_ip}:{config.PORT}")
    if os.path.exists(TOKEN_FILE):
        logger.info("飞书 token 文件已存在")
    else:
        logger.warning("飞书 token 文件不存在，首次发送时会自动打开浏览器完成授权")
    yield
    logger.info("服务器关闭")


app = FastAPI(
    title="飞书局域网服务器",
    description="iPhone Siri Shortcuts → 语音转飞书消息",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# 辅助
# ============================================================
def _get_lan_ip() -> str:
    import subprocess
    for iface in ("en0", "en1", "en2"):
        try:
            result = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=2
            )
            ip = result.stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    return "localhost"


# ============================================================
# 可选 API Key 认证（依赖注入）
# ============================================================
async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")


# ============================================================
# Pydantic 模型
# ============================================================
class SendRequest(BaseModel):
    text: str
    receive_id: Optional[str] = None
    at_user_id: Optional[str] = None
    bot_id: Optional[str] = None


class RecognizeResponse(BaseModel):
    raw_text: str
    polished_text: str


class SendResponse(BaseModel):
    success: bool
    message: str


# ============================================================
# 路由
# ============================================================

@app.get("/health", tags=["系统"])
async def health_check():
    """健康检查，无需认证。Siri Shortcuts 可用此端点测试连通性。"""
    return {
        "status": "ok",
        "version": "1.0.0",
        "lan_ip": _get_lan_ip(),
        "port": config.PORT,
    }


@app.post(
    "/recognize",
    response_model=RecognizeResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["核心功能"],
)
async def recognize_audio(
    audio: UploadFile = File(..., description="M4A 或 WAV 音频文件（Siri Shortcuts 录音）"),
):
    """
    接收 iPhone 录音文件，返回 STT 识别文本和 LLM 优化文本。

    Siri Shortcuts 配置：
    - "Get Contents of URL" → POST，Body 选 Form（multipart）
    - 字段名：audio，值：录音文件
    """
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="音频文件为空")

    logger.info(
        f"收到音频: filename={audio.filename!r} "
        f"size={len(audio_bytes)} bytes "
        f"content_type={audio.content_type!r}"
    )

    # 1. 格式转换（在线程池中运行，避免阻塞事件循环）
    try:
        wav_bytes = await _to_thread(
            smart_convert, audio_bytes, audio.content_type or "audio/m4a"
        )
        logger.info(f"音频转换完成，WAV size={len(wav_bytes)} bytes")
    except AudioConversionError as e:
        logger.error(f"音频转换失败: {e}")
        raise HTTPException(status_code=422, detail=f"音频格式转换失败: {e}")
    except Exception as e:
        logger.error(f"音频转换异常: {e}")
        raise HTTPException(status_code=500, detail=f"音频处理异常: {e}")

    # 2. STT（带超时保护）
    try:
        raw_text = await asyncio.wait_for(
            stt_recognize(wav_bytes, is_wav_file=True),
            timeout=config.STT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"STT 超时（>{config.STT_TIMEOUT}s）")
        raise HTTPException(status_code=504, detail="语音识别超时")
    except Exception as e:
        logger.error(f"STT 失败: {e}")
        raise HTTPException(status_code=502, detail=f"语音识别失败: {e}")

    logger.info(f"STT 结果: {raw_text!r}")

    if not raw_text:
        return RecognizeResponse(raw_text="", polished_text="")

    # 3. LLM 优化（同步函数，to_thread 包装；失败时降级为原始文本）
    try:
        polished_text = await asyncio.wait_for(
            _to_thread(llm_polish, raw_text),
            timeout=config.LLM_TIMEOUT,
        )
        logger.info(f"LLM 结果: {polished_text!r}")
    except Exception as e:
        logger.warning(f"LLM 优化失败，降级使用原始文本: {e}")
        polished_text = raw_text

    return RecognizeResponse(raw_text=raw_text, polished_text=polished_text)


@app.post(
    "/recognize/text",
    response_class=PlainTextResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["核心功能"],
)
async def recognize_audio_text(request: Request):
    """
    同 /recognize，但直接返回纯文本（优化后的文字）。
    专为 Siri Shortcuts 设计，无需解析 JSON，不限制表单字段名。
    """
    # 从表单中取第一个文件，不管键名叫什么
    form = await request.form()
    upload_file = None
    for value in form.values():
        if hasattr(value, "read"):
            upload_file = value
            break
    if upload_file is None:
        raise HTTPException(status_code=400, detail="未找到音频文件")
    result = await recognize_audio(upload_file)
    return result.polished_text or result.raw_text


@app.post(
    "/send",
    response_model=SendResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["核心功能"],
)
async def send_message(req: SendRequest):
    """
    将文本发送到飞书（共享原项目的 OAuth token）。

    at_user_id 特殊值：
    - 不传（null）→ 使用原项目默认的 AT_USER_ID
    - ""（空字符串）→ 明确不 @ 任何人
    - "ou_xxx" → @ 指定用户

    若 token 完全过期需重新 OAuth，接口返回 503，
    届时请在 Mac 终端运行原项目完成授权后再试。
    """
    try:
        logger.info(f"收到发送请求: receive_id={req.receive_id!r}, bot_id={req.bot_id!r}, text={req.text[:80]!r}")
        bot_app_id, bot_app_secret = None, None
        effective_bot_id = req.bot_id

        # 如果 bot_id 为空但 receive_id 不为空，从 contacts.json 自动查找对应 bot
        if not effective_bot_id and req.receive_id:
            try:
                contacts = _read_contacts()
                contact = next((c for c in contacts if c.get("receiveId") == req.receive_id), None)
                if contact and contact.get("botId"):
                    effective_bot_id = contact["botId"]
                    logger.info(f"从联系人 {contact.get('name')!r} 自动匹配 bot_id={effective_bot_id!r}")
            except Exception as e:
                logger.warning(f"自动查找 bot_id 失败: {e}")

        if effective_bot_id:
            bots = _read_bots()
            bot = next((b for b in bots if b.get("id") == effective_bot_id), None)
            if bot:
                bot_app_id = bot.get("appId")
                bot_app_secret = bot.get("appSecret")
                logger.info(f"使用机器人: {bot.get('name')!r} (appId={bot_app_id})")
            else:
                logger.warning(f"未找到 bot_id={effective_bot_id!r} 对应的机器人，使用默认")
        else:
            logger.info("未指定 bot_id 且无法自动匹配，使用默认机器人")
        await _to_thread(
            send_feishu_message,
            req.text,
            receive_id=req.receive_id,
            at_user_id=req.at_user_id,
            app_id=bot_app_id,
            app_secret=bot_app_secret,
        )
        logger.info(f"消息发送成功: {req.text[:80]!r}")
        return SendResponse(success=True, message="发送成功")
    except Exception as e:
        err = str(e)
        logger.error(f"消息发送失败: {err}")
        if "oauth" in err.lower() or "authorize" in err.lower() or "首次" in err:
            raise HTTPException(
                status_code=503,
                detail="飞书 token 已过期，请在 Mac 上运行原项目完成重新授权",
            )
        raise HTTPException(status_code=502, detail=f"飞书发送失败: {err}")


@app.post(
    "/send/text",
    response_class=PlainTextResponse,
    dependencies=[Depends(verify_api_key)],
    tags=["核心功能"],
)
async def send_message_text(request: Request):
    """
    纯文本版发送接口，专为 Siri Shortcuts 设计。
    直接把请求体当作要发送的文本，不需要 JSON 格式。
    """
    body = await request.body()
    raw = body.decode("utf-8").strip()
    # Siri Shortcuts 可能把文本包成 JSON {"":"文本"} 或 {"text":"文本"}
    text = raw
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            text = obj.get("text") or obj.get("") or next(iter(obj.values()), raw)
        except (json.JSONDecodeError, StopIteration):
            pass
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="文本为空")
    # 从 URL 参数获取 receive_id / bot_id（Siri Shortcuts 友好方式）
    receive_id = request.query_params.get("receive_id")
    bot_id = request.query_params.get("bot_id")
    bot_app_id, bot_app_secret = None, None

    # 如果 bot_id 为空但 receive_id 不为空，从 contacts.json 自动查找对应 bot
    if not bot_id and receive_id:
        try:
            contacts = _read_contacts()
            contact = next((c for c in contacts if c.get("receiveId") == receive_id), None)
            if contact and contact.get("botId"):
                bot_id = contact["botId"]
                logger.info(f"[/send/text] 自动匹配 bot_id={bot_id!r} (联系人: {contact.get('name')!r})")
        except Exception:
            pass

    if bot_id:
        bots = _read_bots()
        bot = next((b for b in bots if b.get("id") == bot_id), None)
        if bot:
            bot_app_id = bot.get("appId")
            bot_app_secret = bot.get("appSecret")
    try:
        await _to_thread(send_feishu_message, text,
                         receive_id=receive_id,
                         at_user_id=None,
                         app_id=bot_app_id,
                         app_secret=bot_app_secret)
        logger.info(f"消息发送成功: {text[:80]!r}")
        return "发送成功"
    except Exception as e:
        err = str(e)
        logger.error(f"消息发送失败: {err}")
        raise HTTPException(status_code=502, detail=f"飞书发送失败: {err}")


@app.get(
    "/contacts",
    dependencies=[Depends(verify_api_key)],
    tags=["辅助"],
)
async def get_contacts():
    """
    返回联系人列表（直接读取原项目 contacts.json）。
    供 Siri Shortcuts 的 "Choose from List" 动作使用。
    """
    try:
        data = await _to_thread(_read_contacts)
        # 精简返回字段，只保留跑通流程不可或缺的参数（去除冗余的 icon 和 atUserId 显示）
        minimal_data = []
        for c in data:
            name = f"{c.get('icon', '')} {c.get('name', '')}".strip()
            minimal_data.append({
                "name": name,
                "receiveId": c.get("receiveId", ""),
                "botId": c.get("botId", "")
            })
        return JSONResponse(content=minimal_data)
    except FileNotFoundError:
        return JSONResponse(content=[])
    except Exception as e:
        logger.error(f"读取联系人失败: {e}")
        raise HTTPException(status_code=500, detail=f"读取联系人失败: {e}")


def _read_contacts():
    with open(config.CONTACTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_bots():
    try:
        with open(config.BOTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# ============================================================
# 配置管理 UI
# ============================================================

ENV_FILE = Path(__file__).parent / ".env"

_ADMIN_KEYS = [
    "PORT", "API_KEY", "STT_TIMEOUT", "LLM_TIMEOUT",
    "VOLC_APP_ID", "VOLC_ACCESS_TOKEN",
    "LLM_PROVIDER", "ARK_API_KEY", "ARK_MODEL", "ARK_BASE_URL",
    "MINIMAX_API_KEY", "MINIMAX_MODEL", "MINIMAX_BASE_URL",
    "SYSTEM_PROMPT",
]


@app.get("/admin", response_class=HTMLResponse, tags=["配置"])
async def admin_page():
    return HTMLResponse(_ADMIN_HTML)


@app.get("/admin/config", tags=["配置"])
async def admin_get_config():
    cfg = {}
    for key in _ADMIN_KEYS:
        cfg[key] = getattr(config, key, "")
    return JSONResponse(cfg)


def _env_val(v: str) -> str:
    """对含换行符的值加双引号，确保 .env 格式正确。"""
    if "\n" in v or "\r" in v:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


_INT_KEYS = {"PORT", "STT_TIMEOUT", "LLM_TIMEOUT"}


@app.post("/admin/save", tags=["配置"])
async def admin_save_config(request: Request):
    data = await request.json()
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    updated = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in data:
                new_lines.append(f"{key}={_env_val(str(data[key]))}")
                updated.add(key)
                continue
        new_lines.append(line)
    # 追加 .env 中没有但传入的 key
    for key, val in data.items():
        if key not in updated:
            new_lines.append(f"{key}={_env_val(str(val))}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # 立即更新内存中的 config，测速等操作无需重启即可生效
    for key, val in data.items():
        if hasattr(config, key):
            setattr(config, key, int(val) if key in _INT_KEYS else val)
    return {"ok": True, "message": "已保存并即时生效（PORT 变更仍需重启）"}


@app.get("/admin/contacts", tags=["配置"])
async def admin_get_contacts():
    try:
        data = await _to_thread(_read_contacts)
    except FileNotFoundError:
        data = []
    return JSONResponse(data)


@app.post("/admin/contacts/save", tags=["配置"])
async def admin_save_contacts(request: Request):
    data = await request.json()
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="期望 JSON 数组")
    def _write():
        with open(config.CONTACTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    await _to_thread(_write)
    return {"ok": True, "message": f"已保存 {len(data)} 个联系人"}


@app.get("/admin/bots", tags=["配置"])
async def admin_get_bots():
    return JSONResponse(_read_bots())


@app.post("/admin/bots/save", tags=["配置"])
async def admin_save_bots(request: Request):
    data = await request.json()
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="期望 JSON 数组")
    def _write():
        with open(config.BOTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    await _to_thread(_write)
    return {"ok": True, "message": f"已保存 {len(data)} 个机器人"}


@app.post("/admin/benchmark", tags=["配置"])
async def admin_benchmark(request: Request):
    """测试大模型响应速度，返回延迟和吞吐指标。"""
    import time
    body = await request.json()
    provider = body.get("provider", config.LLM_PROVIDER)
    prompt = body.get("prompt", "用一句话介绍你自己。")

    def _run_benchmark():
        import requests as _req
        t0 = time.perf_counter()
        if provider == "minimax":
            url = config.MINIMAX_BASE_URL
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
            }
            model = config.MINIMAX_MODEL
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        else:  # doubao
            url = config.ARK_BASE_URL
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.ARK_API_KEY}",
            }
            model = config.ARK_MODEL
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        resp = _req.post(url, headers=headers, json=payload, timeout=60)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        completion_tokens = usage.get("completion_tokens", len(content))
        return {
            "ok": True,
            "provider": provider,
            "model": model,
            "latency_ms": round(elapsed_ms),
            "completion_tokens": completion_tokens,
            "chars_per_sec": round(len(content) / (elapsed_ms / 1000), 1),
            "tokens_per_sec": round(completion_tokens / (elapsed_ms / 1000), 1),
            "response": content,
        }

    try:
        result = await _to_thread(_run_benchmark)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# ============================================================
# 捷径文件生成
# ============================================================

def _generate_shortcut(server_url: str) -> bytes:
    import plistlib, uuid

    def _txt(s: str):
        return {"Value": {"attachmentsByRange": {}, "string": s}, "WFSerializationType": "WFTextTokenString"}

    def _var(name: str, uid: str):
        return {"Value": {"Type": "ActionOutput", "Aggrandizements": [], "OutputName": name, "OutputUUID": uid}, "WFSerializationType": "WFTextTokenAttachment"}

    def _txt_var(name: str, uid: str):
        return {"Value": {"attachmentsByRange": {"{0, 1}": {"Type": "ActionOutput", "Aggrandizements": [], "OutputName": name, "OutputUUID": uid}}, "string": "\ufffc"}, "WFSerializationType": "WFTextTokenString"}

    def _json_body(*pairs):
        return {"Value": {"WFDictionaryFieldValueItems": [{"WFItemType": 0, "WFKey": _txt(k), "WFValue": v} for k, v in pairs]}, "WFSerializationType": "WFDictionaryFieldValue"}

    u_rec  = str(uuid.uuid4()).upper()
    u_recg = str(uuid.uuid4()).upper()
    u_con  = str(uuid.uuid4()).upper()
    u_cho  = str(uuid.uuid4()).upper()
    u_rid  = str(uuid.uuid4()).upper()
    u_bid  = str(uuid.uuid4()).upper()

    actions = [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.recordaudio",
         "WFWorkflowActionParameters": {"CustomOutputName": "录音", "UUID": u_rec}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
         "WFWorkflowActionParameters": {"CustomOutputName": "识别结果", "UUID": u_recg,
             "WFURL": f"{server_url}/recognize/text", "WFHTTPMethod": "POST",
             "WFHTTPBodyType": "File", "WFInput": _var("录音", u_rec)}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.alert",
         "WFWorkflowActionParameters": {"WFAlertActionTitle": "确认发送？",
             "WFAlertActionMessage": _txt_var("识别结果", u_recg), "WFAlertActionCancelButtonShown": True}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
         "WFWorkflowActionParameters": {"CustomOutputName": "联系人列表", "UUID": u_con,
             "WFURL": f"{server_url}/contacts", "WFHTTPMethod": "GET"}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.choosefromlist",
         "WFWorkflowActionParameters": {"CustomOutputName": "选中联系人", "UUID": u_cho,
             "WFChooseFromListActionList": _var("联系人列表", u_con), "WFChooseFromListActionPrompt": "发送给谁？"}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.getdictionaryvalue",
         "WFWorkflowActionParameters": {"CustomOutputName": "receiveId", "UUID": u_rid,
             "WFGetDictionaryValueType": "Value",
             "WFDictionaryKey": _txt("receiveId"), "WFInput": _var("选中联系人", u_cho)}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.getdictionaryvalue",
         "WFWorkflowActionParameters": {"CustomOutputName": "botId", "UUID": u_bid,
             "WFGetDictionaryValueType": "Value",
             "WFDictionaryKey": _txt("botId"), "WFInput": _var("选中联系人", u_cho)}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
         "WFWorkflowActionParameters": {"WFURL": f"{server_url}/send", "WFHTTPMethod": "POST",
             "WFHTTPBodyType": "JSON",
             "WFJSONValues": _json_body(
                 ("text",       _txt_var("识别结果", u_recg)),
                 ("receive_id", _txt_var("receiveId", u_rid)),
                 ("bot_id",     _txt_var("botId",     u_bid)),
             )}},

        {"WFWorkflowActionIdentifier": "is.workflow.actions.notification",
         "WFWorkflowActionParameters": {"WFNotificationActionTitle": "发送成功 ✅",
             "WFNotificationActionBody": _txt_var("识别结果", u_recg), "WFNotificationActionSound": True}},
    ]

    data = {"WFWorkflowClientVersion": "1156.14.1", "WFWorkflowMinimumClientVersion": 900,
            "WFWorkflowMinimumClientVersionString": "900", "WFWorkflowHasShortcutInputVariables": False,
            "WFWorkflowIcon": {"WFWorkflowIconStartColor": 431817727, "WFWorkflowIconGlyphNumber": 59511},
            "WFWorkflowInputContentItemClasses": [], "WFWorkflowActions": actions,
            "WFWorkflowImportQuestions": [], "WFWorkflowTypes": []}
    return plistlib.dumps(data, fmt=plistlib.FMT_BINARY)


@app.get("/admin/shortcut.shortcut", tags=["配置"])
async def download_shortcut():
    from fastapi.responses import Response
    lan_ip = _get_lan_ip()
    server_url = f"http://{lan_ip}:{config.PORT}"
    data = await _to_thread(_generate_shortcut, server_url)
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename=feishu.shortcut"})


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>飞书服务器配置</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #f5f5f7; color: #1d1d1f; padding: 24px 16px; }
h1 { font-size: 22px; font-weight: 700; margin-bottom: 24px; }
.section { background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.section h2 { font-size: 13px; font-weight: 600; color: #6e6e73; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 16px; }
.field { margin-bottom: 14px; }
.field:last-child { margin-bottom: 0; }
label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 5px; }
input, select { width: 100%; padding: 9px 12px; border: 1px solid #d2d2d7; border-radius: 8px; font-size: 14px; outline: none; }
input:focus, select:focus { border-color: #0071e3; box-shadow: 0 0 0 3px rgba(0,113,227,.15); }
.row { display: flex; gap: 12px; }
.row .field { flex: 1; }
.btn { display: block; width: 100%; padding: 13px; background: #0071e3; color: #fff; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 20px; }
.btn:hover { background: #0077ed; }
.toast { display: none; position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: #1d1d1f; color: #fff; padding: 12px 24px; border-radius: 20px; font-size: 14px; }
</style>
</head>
<body>
<h1>飞书服务器配置</h1>

<div class="section" style="text-align:center">
  <h2 style="text-align:left">导入捷径</h2>
  <p style="font-size:12px;color:#8e8e93;margin-bottom:16px;text-align:left">选择以下任一方式导入完整的录音→识别→选联系人→发飞书捷径</p>
  <div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap">
    <a id="shortcut-import-btn" href="#" style="flex:1;min-width:140px;display:inline-block;padding:12px 20px;background:#0071e3;color:#fff;border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;text-align:center">📲 直接导入到捷径 App</a>
    <a id="shortcut-download-btn" href="#" style="flex:1;min-width:140px;display:inline-block;padding:12px 20px;background:#34c759;color:#fff;border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;text-align:center" download="feishu.shortcut">⬇️ 下载 .shortcut 文件</a>
  </div>
  <details style="text-align:left;margin-bottom:12px">
    <summary style="font-size:12px;color:#8e8e93;cursor:pointer">扫码导入（备选）</summary>
    <div style="text-align:center;margin-top:12px">
      <div id="qrcode" style="display:inline-block;padding:12px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.12)"></div>
    </div>
  </details>
  <p style="font-size:12px;color:#8e8e93" id="shortcut-url"></p>
</div>

<div class="section">
  <h2>服务器</h2>
  <div class="row">
    <div class="field"><label>PORT</label><input id="PORT" type="number"></div>
    <div class="field"><label>API_KEY（留空=不验证）</label><input id="API_KEY"></div>
  </div>
  <div class="row">
    <div class="field"><label>STT_TIMEOUT（秒）</label><input id="STT_TIMEOUT" type="number"></div>
    <div class="field"><label>LLM_TIMEOUT（秒）</label><input id="LLM_TIMEOUT" type="number"></div>
  </div>
</div>

<div class="section">
  <h2>语音识别（火山引擎 STT）</h2>
  <div class="field"><label>VOLC_APP_ID</label><input id="VOLC_APP_ID"></div>
  <div class="field"><label>VOLC_ACCESS_TOKEN</label><input id="VOLC_ACCESS_TOKEN"></div>
</div>

<div class="section">
  <h2>大模型（LLM）</h2>
  <div class="field">
    <label>LLM_PROVIDER</label>
    <select id="LLM_PROVIDER" onchange="toggleProvider()">
      <option value="doubao">豆包（Doubao）</option>
      <option value="minimax">MiniMax</option>
    </select>
  </div>
  <div id="doubao-fields">
    <div class="field"><label>ARK_API_KEY</label><input id="ARK_API_KEY"></div>
    <div class="row">
      <div class="field"><label>ARK_MODEL</label><input id="ARK_MODEL"></div>
      <div class="field"><label>ARK_BASE_URL（API 地址）</label><input id="ARK_BASE_URL"></div>
    </div>
  </div>
  <div id="minimax-fields" style="display:none">
    <div class="field"><label>MINIMAX_API_KEY</label><input id="MINIMAX_API_KEY"></div>
    <div class="row">
      <div class="field"><label>MINIMAX_MODEL</label><input id="MINIMAX_MODEL"></div>
      <div class="field"><label>MINIMAX_BASE_URL（API 地址）</label><input id="MINIMAX_BASE_URL"></div>
    </div>
  </div>
  <div class="field" style="margin-top:14px">
    <label>优化提示词（SYSTEM_PROMPT）</label>
    <textarea id="SYSTEM_PROMPT" rows="7" style="width:100%;padding:9px 12px;border:1px solid #d2d2d7;border-radius:8px;font-size:13px;outline:none;resize:vertical;font-family:inherit;line-height:1.6"></textarea>
  </div>
</div>

<div class="section">
  <h2>飞书机器人</h2>
  <p style="font-size:12px;color:#8e8e93;margin-bottom:14px">不同群 / 联系人可使用不同机器人发送消息，ID 会在联系人里选择绑定。</p>
  <div id="bots-list"></div>
  <button type="button" onclick="addBot()" style="margin-top:10px;padding:8px 16px;background:#f5f5f7;border:1px solid #d2d2d7;border-radius:8px;font-size:13px;cursor:pointer;color:#0071e3;font-weight:500">＋ 添加机器人</button>
  <button type="button" onclick="saveBots()" style="margin-top:10px;margin-left:8px;padding:8px 16px;background:#0071e3;border:none;border-radius:8px;font-size:13px;cursor:pointer;color:#fff;font-weight:500">保存机器人</button>
  <span id="bots-toast" style="margin-left:10px;font-size:12px;color:#34c759;display:none"></span>
</div>

<div class="section">
  <h2>联系人 / 群组管理</h2>
  <p style="font-size:12px;color:#8e8e93;margin-bottom:14px">Siri Shortcuts 选择列表里的联系人，<code>receiveId</code> 填群 ID（oc_）或用户 ID（ou_），可绑定上方配置的机器人。</p>
  <div id="contacts-list"></div>
  <button type="button" onclick="addContact()" style="margin-top:10px;padding:8px 16px;background:#f5f5f7;border:1px solid #d2d2d7;border-radius:8px;font-size:13px;cursor:pointer;color:#0071e3;font-weight:500">＋ 添加联系人</button>
  <button type="button" onclick="saveContacts()" style="margin-top:10px;margin-left:8px;padding:8px 16px;background:#0071e3;border:none;border-radius:8px;font-size:13px;cursor:pointer;color:#fff;font-weight:500">保存联系人</button>
  <span id="contacts-toast" style="margin-left:10px;font-size:12px;color:#34c759;display:none"></span>
</div>

<button class="btn" onclick="save()">保存配置（PORT 变更需重启，其余即时生效）</button>

<div class="section" style="margin-top:16px">
  <h2>大模型测速</h2>
  <div class="row">
    <div class="field">
      <label>测试服务商</label>
      <select id="bench-provider">
        <option value="doubao">豆包（Doubao）</option>
        <option value="minimax">MiniMax</option>
      </select>
    </div>
    <div class="field">
      <label>测试 Prompt</label>
      <input id="bench-prompt" value="用一句话介绍你自己。">
    </div>
  </div>
  <button class="btn" id="bench-btn" onclick="runBenchmark()" style="margin-top:12px;background:#34c759">开始测速</button>
  <div id="bench-result" style="display:none;margin-top:16px;padding:14px;background:#f5f5f7;border-radius:10px;font-size:13px;line-height:1.8">
    <div class="bench-row"><span class="bench-label">服务商 / 模型</span><span id="b-model">-</span></div>
    <div class="bench-row"><span class="bench-label">总延迟</span><span id="b-latency">-</span></div>
    <div class="bench-row"><span class="bench-label">输出 Token 数</span><span id="b-tokens">-</span></div>
    <div class="bench-row"><span class="bench-label">生成速度</span><span id="b-tps">-</span></div>
    <div class="bench-row"><span class="bench-label">字符速度</span><span id="b-cps">-</span></div>
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid #e0e0e5">
      <span class="bench-label" style="display:block;margin-bottom:4px">模型回复</span>
      <span id="b-response" style="color:#3c3c43;word-break:break-all"></span>
    </div>
  </div>
  <div id="bench-error" style="display:none;margin-top:12px;padding:12px;background:#fff2f2;border-radius:10px;color:#ff3b30;font-size:13px"></div>
</div>

<div class="toast" id="toast"></div>

<style>
.bench-row { display:flex; align-items:baseline; gap:8px; }
.bench-label { min-width:110px; font-size:12px; color:#8e8e93; font-weight:500; }
#bench-result span:not(.bench-label):not(#b-response) { font-weight:600; font-size:14px; }
#bench-btn:disabled { background:#8e8e93; cursor:not-allowed; }
</style>

<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
const KEYS = ["PORT","API_KEY","STT_TIMEOUT","LLM_TIMEOUT","VOLC_APP_ID","VOLC_ACCESS_TOKEN",
  "LLM_PROVIDER","ARK_API_KEY","ARK_MODEL","ARK_BASE_URL",
  "MINIMAX_API_KEY","MINIMAX_MODEL","MINIMAX_BASE_URL",
  "SYSTEM_PROMPT"];

function toggleProvider() {
  const v = document.getElementById("LLM_PROVIDER").value;
  document.getElementById("doubao-fields").style.display = v === "doubao" ? "" : "none";
  document.getElementById("minimax-fields").style.display = v === "minimax" ? "" : "none";
}

async function load() {
  const r = await fetch("/admin/config");
  const d = await r.json();
  KEYS.forEach(k => {
    const el = document.getElementById(k);
    if (el) el.value = d[k] ?? "";
  });
  toggleProvider();
  document.getElementById("bench-provider").value = d["LLM_PROVIDER"] || "doubao";
  await loadBots();
  await loadContacts();
}

// ── 机器人管理 ──────────────────────────────────────────────
let _bots = [];

async function loadBots() {
  const r = await fetch("/admin/bots");
  _bots = await r.json();
  renderBots();
}

function renderBots() {
  const el = document.getElementById("bots-list");
  if (_bots.length === 0) {
    el.innerHTML = '<p style="font-size:13px;color:#8e8e93">暂无机器人</p>';
    return;
  }
  el.innerHTML = _bots.map((b, i) => `
    <div data-bidx="${i}" style="display:flex;gap:8px;align-items:center;margin-bottom:10px;padding:10px;background:#f5f5f7;border-radius:10px;flex-wrap:wrap">
      <input placeholder="名称" value="${_esc(b.name||'')}" oninput="_bots[${i}].name=this.value;renderContacts()"
        style="flex:1;min-width:80px;padding:7px 10px;border:1px solid #d2d2d7;border-radius:7px;font-size:13px">
      <input placeholder="APP ID（cli_xxx）" value="${_esc(b.appId||'')}" oninput="_bots[${i}].appId=this.value"
        style="flex:2;min-width:120px;padding:7px 10px;border:1px solid #d2d2d7;border-radius:7px;font-size:13px;font-family:monospace">
      <input placeholder="App Secret" value="${_esc(b.appSecret||'')}" oninput="_bots[${i}].appSecret=this.value"
        style="flex:2;min-width:120px;padding:7px 10px;border:1px solid #d2d2d7;border-radius:7px;font-size:13px;font-family:monospace">
      <button onclick="removeBot(${i})" style="padding:7px 10px;border:none;background:#ff3b30;color:#fff;border-radius:7px;cursor:pointer;font-size:13px">删除</button>
    </div>`).join("");
}

function addBot() {
  const id = "bot_" + Date.now();
  _bots.push({id, name:"", appId:"", appSecret:""});
  renderBots();
  renderContacts();
}

function removeBot(i) {
  _bots.splice(i, 1);
  renderBots();
  renderContacts();
}

async function saveBots() {
  const r = await fetch("/admin/bots/save", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(_bots),
  });
  const j = await r.json();
  const t = document.getElementById("bots-toast");
  t.textContent = j.message || "已保存";
  t.style.display = "inline";
  setTimeout(() => t.style.display = "none", 3000);
}

// ── 联系人管理 ──────────────────────────────────────────────
let _contacts = [];

async function loadContacts() {
  const r = await fetch("/admin/contacts");
  _contacts = await r.json();
  renderContacts();
}

function _botOptions(selectedId) {
  const none = `<option value="">（默认机器人）</option>`;
  return none + _bots.map(b =>
    `<option value="${_esc(b.id||'')}" ${b.id === selectedId ? 'selected' : ''}>${_esc(b.name || b.id || '未命名')}</option>`
  ).join("");
}

function renderContacts() {
  const el = document.getElementById("contacts-list");
  if (_contacts.length === 0) {
    el.innerHTML = '<p style="font-size:13px;color:#8e8e93">暂无联系人</p>';
    return;
  }
  el.innerHTML = _contacts.map((c, i) => `
    <div data-idx="${i}" style="display:flex;gap:8px;align-items:center;margin-bottom:10px;padding:10px;background:#f5f5f7;border-radius:10px;flex-wrap:wrap">
      <input placeholder="图标 emoji" value="${_esc(c.icon||'')}" oninput="_contacts[${i}].icon=this.value"
        style="width:52px;text-align:center;padding:7px;border:1px solid #d2d2d7;border-radius:7px;font-size:16px">
      <input placeholder="名称" value="${_esc(c.name||'')}" oninput="_contacts[${i}].name=this.value"
        style="flex:1;min-width:80px;padding:7px 10px;border:1px solid #d2d2d7;border-radius:7px;font-size:13px">
      <input placeholder="receiveId（oc_ / ou_）" value="${_esc(c.receiveId||'')}" oninput="_contacts[${i}].receiveId=this.value"
        style="flex:2;min-width:120px;padding:7px 10px;border:1px solid #d2d2d7;border-radius:7px;font-size:13px;font-family:monospace">
      <input placeholder="atUserId（可选）" value="${_esc(c.atUserId||'')}" oninput="_contacts[${i}].atUserId=this.value"
        style="flex:1.5;min-width:100px;padding:7px 10px;border:1px solid #d2d2d7;border-radius:7px;font-size:13px;font-family:monospace">
      <select onchange="_contacts[${i}].botId=this.value"
        style="flex:1;min-width:90px;padding:7px 8px;border:1px solid #d2d2d7;border-radius:7px;font-size:12px">
        ${_botOptions(c.botId||'')}
      </select>
      <button onclick="removeContact(${i})" style="padding:7px 10px;border:none;background:#ff3b30;color:#fff;border-radius:7px;cursor:pointer;font-size:13px">删除</button>
    </div>`).join("");
}

function _esc(s) { return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;"); }

function addContact() {
  _contacts.push({icon:"💬", name:"", receiveId:"", atUserId:"", botId:""});
  renderContacts();
  // 聚焦最后一行的名称输入框
  const rows = document.querySelectorAll("#contacts-list [data-idx]");
  if (rows.length) rows[rows.length-1].querySelectorAll("input")[1].focus();
}

function removeContact(i) {
  _contacts.splice(i, 1);
  renderContacts();
}

async function saveContacts() {
  const r = await fetch("/admin/contacts/save", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(_contacts),
  });
  const j = await r.json();
  const t = document.getElementById("contacts-toast");
  t.textContent = j.message || "已保存";
  t.style.display = "inline";
  setTimeout(() => t.style.display = "none", 3000);
}

async function save() {
  const d = {};
  KEYS.forEach(k => { const el = document.getElementById(k); if (el) d[k] = el.value; });
  const r = await fetch("/admin/save", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(d)});
  const j = await r.json();
  showToast(j.message || "已保存");
}

async function runBenchmark() {
  const btn = document.getElementById("bench-btn");
  const resultDiv = document.getElementById("bench-result");
  const errorDiv = document.getElementById("bench-error");
  const provider = document.getElementById("bench-provider").value;
  const prompt = document.getElementById("bench-prompt").value.trim() || "用一句话介绍你自己。";

  btn.disabled = true;
  btn.textContent = "测速中…";
  resultDiv.style.display = "none";
  errorDiv.style.display = "none";

  try {
    const r = await fetch("/admin/benchmark", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({provider, prompt}),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "未知错误");
    document.getElementById("b-model").textContent = `${d.provider} / ${d.model}`;
    document.getElementById("b-latency").textContent = `${d.latency_ms} ms`;
    document.getElementById("b-tokens").textContent = `${d.completion_tokens} tokens`;
    document.getElementById("b-tps").textContent = `${d.tokens_per_sec} tokens/s`;
    document.getElementById("b-cps").textContent = `${d.chars_per_sec} 字符/s`;
    document.getElementById("b-response").textContent = d.response;
    resultDiv.style.display = "block";
  } catch(e) {
    errorDiv.textContent = "测速失败：" + e.message;
    errorDiv.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "开始测速";
  }
}

function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.display = "block";
  setTimeout(() => t.style.display = "none", 3000);
}

// 生成捷径导入链接和二维码
(function() {
  const downloadUrl = window.location.origin + "/admin/shortcut.shortcut";
  const importUrl = "shortcuts://import-shortcut?url=" + encodeURIComponent(downloadUrl) + "&name=" + encodeURIComponent("飞书语音消息");
  document.getElementById("shortcut-url").textContent = downloadUrl;
  document.getElementById("shortcut-import-btn").href = importUrl;
  document.getElementById("shortcut-download-btn").href = downloadUrl;
  new QRCode(document.getElementById("qrcode"), {text: downloadUrl, width: 180, height: 180, correctLevel: QRCode.CorrectLevel.M});
})();

load();
</script>
</body>
</html>"""
