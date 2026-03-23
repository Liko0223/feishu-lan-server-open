#!/bin/bash
# 飞书局域网服务器启动脚本

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 检查 ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "错误: 未找到 ffmpeg，请先安装: brew install ffmpeg"
    exit 1
fi

# 安装依赖（仅在缺少时）
python3 -c "import fastapi" 2>/dev/null || pip3 install -r requirements.txt

# 读取配置
PORT="${PORT:-5005}"
HOST="${HOST:-0.0.0.0}"

# 获取局域网 IP
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || \
         ipconfig getifaddr en1 2>/dev/null || \
         echo "localhost")

echo "================================"
echo "  飞书局域网服务器"
echo "================================"
echo "  局域网地址: http://${LAN_IP}:${PORT}"
echo "  健康检查:   http://${LAN_IP}:${PORT}/health"
echo "  API 文档:   http://${LAN_IP}:${PORT}/docs"
echo "================================"
echo "  在 iPhone Siri Shortcuts 中使用 IP: ${LAN_IP}"
echo "================================"

exec python3 -m uvicorn server:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level info
