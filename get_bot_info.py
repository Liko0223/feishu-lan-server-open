#!/usr/bin/env python3
"""
获取飞书机器人聊天 ID 的辅助脚本。

使用方法：
1. 先在 bots.json 中填写机器人 appId / appSecret
2. 设置环境变量 FEISHU_OPEN_ID=ou_xxx
3. 运行 python3 get_bot_info.py
"""
import json
import os
from pathlib import Path

import requests


YOUR_OPEN_ID = os.environ.get("FEISHU_OPEN_ID", "").strip()
ROOT = Path(__file__).resolve().parent
BOTS_FILE = ROOT / "bots.json"


def load_bots():
    if not BOTS_FILE.exists():
        raise FileNotFoundError(f"未找到 {BOTS_FILE.name}，请先创建并填写机器人配置")
    with BOTS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    bots = []
    for item in data:
        bots.append(
            {
                "name": item.get("name", "未命名机器人"),
                "app_id": item.get("appId", ""),
                "app_secret": item.get("appSecret", ""),
            }
        )
    return bots


def run_bot(bot):
    print(f"\n🤖  {bot['name']}  ({bot['app_id']})")

    # 1. 获取 token
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": bot["app_id"], "app_secret": bot["app_secret"]},
    )
    d = r.json()
    token = d.get("tenant_access_token")
    if not token:
        print(f"  ❌ 获取 token 失败: {d.get('msg')}")
        return

    # 2. 列出已加入的群
    r2 = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 50},
    )
    chats = r2.json().get("data", {}).get("items", [])
    if chats:
        print(f"  📋 群聊：")
        for c in chats:
            print(f"     {c.get('name','无名')}  →  {c['chat_id']}")

    # 3. 获取与你的私聊 oc_
    r3 = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"receive_id_type": "open_id"},
        json={
            "receive_id": YOUR_OPEN_ID,
            "msg_type": "text",
            "content": json.dumps({"text": "🔧 获取私聊 ID 测试消息，可忽略"}),
        },
    )
    d3 = r3.json()
    if d3.get("code") == 0:
        chat_id = d3["data"]["chat_id"]
        print(f"  💬 私聊 oc_：{chat_id}")
    else:
        print(f"  ❌ 私聊获取失败 (code={d3.get('code')}): {d3.get('msg')}")
        if d3.get("code") == 230013:
            print("     ⚠️  需在飞书开放平台把你加入可用范围后重试")


def main():
    if not YOUR_OPEN_ID:
        print("请先设置环境变量 FEISHU_OPEN_ID=ou_xxx 后再运行。")
        return

    try:
        bots = load_bots()
    except Exception as e:
        print(f"读取 bots.json 失败: {e}")
        return

    usable_bots = [bot for bot in bots if bot["app_id"] and bot["app_secret"]]
    if not usable_bots:
        print("bots.json 中没有可用机器人，请先填写 appId 和 appSecret。")
        return

    print(f"你的 open_id: {YOUR_OPEN_ID}")
    print("=" * 55)
    for bot in usable_bots:
        run_bot(bot)
    print("\n" + "=" * 55)
    print("把上面的 oc_ 填到 admin 后台「联系人」里即可。\n")


if __name__ == "__main__":
    main()
