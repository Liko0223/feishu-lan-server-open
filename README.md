# 飞书局域网语音服务器

一个适合个人或家庭内网场景的 FastAPI 服务：

- iPhone Siri Shortcuts 录音后直接上传
- 调用火山引擎 STT 转文字
- 调用大模型优化口语化文本
- 选择联系人或群聊后发送到飞书
- 自带一个简单的 `/admin` 管理页面，可配置机器人、联系人和环境变量

这个目录是可分享的开源版本，不包含任何私有密钥、机器人配置、联系人信息或 OAuth token。

## 功能特性

- 支持 `/recognize` 和 `/recognize/text` 两种语音识别接口
- 支持 `/send` 和 `/send/text` 两种发送接口
- 支持按联系人自动匹配机器人
- 支持多机器人配置
- 支持 Doubao 和 MiniMax 两种 LLM 提供方
- 支持从管理页一键导入 Siri Shortcuts

## 运行要求

- Python 3.8+
- `ffmpeg`
- 一个可用的飞书应用
- 一个可用的 STT 配置
- 至少一个可用的大模型配置

macOS 安装 `ffmpeg`：

```bash
brew install ffmpeg
```

## 快速开始

1. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 复制环境变量模板

```bash
cp .env.example .env
```

3. 编辑 `.env`

至少填写下面这些配置：

- `VOLC_APP_ID`
- `VOLC_ACCESS_TOKEN`
- `LLM_PROVIDER`
- `ARK_API_KEY` 或 `MINIMAX_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

4. 编辑机器人和联系人

项目默认附带的是模板数据，请按需修改：

- `bots.json`
- `contacts.json`

5. 启动服务

```bash
./run.sh
```

或使用 `uvicorn`：

```bash
python3 -m uvicorn server:app --host 0.0.0.0 --port 5005 --log-level info
```

6. 打开管理页

启动后访问：

- `http://<你的局域网 IP>:5005/admin`

你可以在这里：

- 修改 `.env` 中的大部分配置
- 管理机器人列表
- 管理联系人 / 群组
- 下载或导入 Siri Shortcuts
- 测试大模型响应速度

## 配置说明

### 服务器

- `PORT`: 服务端口，默认 `5005`
- `HOST`: 监听地址，默认 `0.0.0.0`
- `API_KEY`: 可选的接口认证头，客户端通过 `X-API-Key` 传入
- `STT_TIMEOUT`: 语音识别超时秒数
- `LLM_TIMEOUT`: 文本优化超时秒数

### 火山引擎 STT

- `VOLC_APP_ID`: 火山引擎应用 ID
- `VOLC_ACCESS_TOKEN`: 火山引擎访问令牌

### 大模型

- `LLM_PROVIDER`: `doubao` 或 `minimax`
- `ARK_API_KEY`: Doubao API Key
- `ARK_MODEL`: Doubao 模型名
- `ARK_BASE_URL`: Doubao 接口地址
- `MINIMAX_API_KEY`: MiniMax API Key
- `MINIMAX_MODEL`: MiniMax 模型名
- `MINIMAX_BASE_URL`: MiniMax 接口地址
- `SYSTEM_PROMPT`: 可选，自定义文本优化提示词

### 飞书

- `FEISHU_APP_ID`: 飞书应用 App ID
- `FEISHU_APP_SECRET`: 飞书应用 App Secret
- `FEISHU_RECEIVE_ID`: 默认发送目标，可留空
- `AT_USER_ID`: 默认 @ 的用户 ID，可留空

## 数据文件

### `bots.json`

用于配置多个发送机器人。示例结构：

```json
[
  {
    "id": "bot_default",
    "name": "示例机器人",
    "appId": "cli_xxx_your_app_id",
    "appSecret": "your_app_secret"
  }
]
```

### `contacts.json`

用于配置 Siri Shortcuts 中显示的联系人或群聊。示例结构：

```json
[
  {
    "icon": "💬",
    "name": "示例群聊",
    "receiveId": "oc_xxx_your_chat_id",
    "atUserId": "",
    "botId": "bot_default"
  }
]
```

其中：

- `receiveId` 支持群聊 `oc_...` 或用户 `ou_...`
- `botId` 对应 `bots.json` 中的 `id`

## OAuth 说明

第一次真正发送飞书消息时，程序可能会自动打开浏览器，引导你完成飞书授权。授权成功后会在本地生成 token 文件，这些文件已经被 `.gitignore` 忽略，不应提交到仓库。

## 常用接口

### `GET /health`

健康检查。

### `POST /recognize`

上传音频并返回：

- `raw_text`
- `polished_text`

### `POST /recognize/text`

上传音频，直接返回优化后的纯文本，适合 Siri Shortcuts。

### `POST /send`

发送 JSON 文本到飞书。

### `POST /send/text`

发送纯文本到飞书，适合 Siri Shortcuts。

### `GET /contacts`

返回给快捷指令使用的精简联系人列表。

## 项目结构

```text
.
├── audio_converter.py
├── bots.json
├── config.py
├── contacts.json
├── core.py
├── get_bot_info.py
├── requirements.txt
├── run.sh
└── server.py
```

## 安全建议

- 不要提交 `.env`
- 不要提交 `.feishu_user_token.json` 或 `.feishu_token_*.json`
- 不要在 `bots.json`、`contacts.json` 中保留真实生产数据后再公开分享
- 如果你怀疑旧密钥已经暴露，先去对应平台轮换密钥再公开仓库

## 发布到 GitHub

如果你要把这个目录作为一个全新的开源仓库发布，最简单的方式是：

```bash
cd /path/to/feishu-lan-server-open
git init
git add .
git commit -m "Initial open source release"
git branch -M main
git remote add origin <你的 GitHub 仓库地址>
git push -u origin main
```

如果你还没创建 GitHub 仓库：

1. 打开 GitHub
2. 点击右上角 `New repository`
3. 仓库名可用 `feishu-lan-server-open`
4. 选择 `Public`
5. 不要勾选自动创建 README、`.gitignore` 或 License
6. 创建后复制仓库地址，替换上面命令里的 `<你的 GitHub 仓库地址>`

## License

本项目附带 MIT License，见 `LICENSE`。
