#!/bin/bash

# .env 파일 존재 확인
if [ ! -f .env ]; then
    echo "⚠️  .env 파일이 없습니다. 보안을 위해 .env 파일을 먼저 생성해주세요."
    exit 1
fi

echo "🚀 Docker 컨테이너를 빌드하고 실행합니다..."
docker compose down
docker compose up -d --build
