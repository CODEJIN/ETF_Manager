# 1. 베이스 이미지 설정
FROM python:3.10-slim

# 2. 작업 디렉토리 설정
WORKDIR /app

# 파이썬 버퍼링 비활성화 (로그 실시간 확인용)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 3. 의존성 파일 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 소스 코드 복사
COPY . .

# 5. Flask 기본 포트 개방
EXPOSE 5000

# 6. 애플리케이션 실행
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app", "--workers", "1", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]