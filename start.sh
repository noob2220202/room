#!/bin/bash
set -e

# .env 파일이 있으면 로드
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

if [ -z "$BOT_TOKEN" ]; then
    echo "오류: BOT_TOKEN 환경변수를 설정해 주세요."
    echo "  cp .env.example .env 후 토큰 입력"
    exit 1
fi

python bot.py
