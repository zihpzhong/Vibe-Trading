#!/bin/bash
# Vibe-Trading 一键部署脚本
# Usage: ./deploy.sh

set -e

HOST="root@43.156.100.108"
DIR="/root/vibe-trading"
IMAGE="vibe-trading-live-trading:latest"

echo "📦 部署 Vibe-Trading 到生产服务器..."

ssh "$HOST" "
  cd $DIR
  git fetch origin
  git reset --hard origin/dev
  docker compose build --no-cache live-trading
  docker compose up -d live-trading
  echo '✅ 部署完成'
  docker compose logs --tail=15 live-trading
"

echo "✨ 部署成功!"