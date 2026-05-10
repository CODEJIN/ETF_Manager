# ETF Portfolio Manager (ETF 포트폴리오 관리 시스템)

⚠️ **Notice / 중요 공지**
> **English**: This project was developed exclusively as a **test and demonstration version by Vibe Coding**. It is intended for educational and testing purposes and should be used with caution in real investment environments.
>
> **한국어**: 본 프로젝트는 **바이브코딩(Vibe Coding)의 테스트 및 데모용**으로 제작되었습니다. 실제 투자 환경에서 사용 시 주의가 필요하며, 학습 및 기능 확인을 목적으로 구성되었습니다.

---

## [English Version]

### Overview
The **ETF Portfolio Manager** is a web-based application designed to track and manage ETF investments across multiple Korean tax-advantaged accounts (ISA, IRP, Pension Savings). It provides real-time price updates, portfolio rebalancing insights, and automated Telegram notifications.

### Key Features
- **Multi-Account Management**: Separate ledgers for ISA, IRP, and Pension Savings.
- **Real-Time Data**: Automatically crawls current ETF prices and exchange rates (USD/JPY/CNY) from Naver Finance.
- **Portfolio Analysis**: Visualizes target vs. actual asset allocation using Chart.js.
- **Smart Alerts**: 
  - Price alerts (relative to average cost, 52-week high, etc.).
  - Rebalancing alerts (notifies when deviation exceeds 3%p).
  - Trade execution summaries.
- **Security**: Environment variable (.env) management and hashed password authentication.
- **Dockerized**: Easy deployment using Docker and Docker Compose.

### Tech Stack
- **Backend**: Python, Flask, Pandas, BeautifulSoup4
- **Frontend**: HTML5, CSS3, JavaScript, Chart.js
- **Deployment**: Docker, Docker Compose

### Quick Start
1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd ETF_Manager
   ```
2. **Set up Environment Variables**: Create a `.env` file based on your credentials (secret keys, bot tokens).
3. **Run with Docker**:
   ```bash
   bash run.sh
   ```

---

## [한국어 버전]

### 프로젝트 개요
**ETF 포트폴리오 관리 시스템**은 한국의 주요 절세 계좌(ISA, IRP, 연금저축)에 분산된 ETF 투자 현황을 통합적으로 관리하고 모니터링하기 위한 웹 애플리케이션입니다.

### 주요 기능
- **다중 계좌 관리**: ISA, IRP, 연금저축 계좌별 독립적인 거래 원장 관리.
- **실시간 데이터**: 네이버 금융 크롤링을 통한 실시간 시세 및 주요 환율(USD/JPY/CNY) 정보 업데이트.
- **포트폴리오 분석**: Chart.js를 활용하여 목표 비중 대비 현재 자산 배분 현황 시각화.
- **스마트 알림 (텔레그램)**:
  - **가격 알림**: 평단가, 52주 최고가 대비 특정 비율 도달 시 알림.
  - **리밸런싱 알림**: 목표 비중과 현재 비중의 괴리가 3%p 이상 발생 시 알림.
  - **거래 브리핑**: 매수/매도 내역 즉시 전송.
- **보안**: `.env` 파일을 통한 민감 정보 관리 및 해시 기반 비밀번호 인증.
- **간편한 배포**: Docker 및 Docker Compose를 활용한 일관된 실행 환경 제공.

### 기술 스택
- **백엔드**: Python, Flask, Pandas, BeautifulSoup4
- **프론트엔드**: HTML5, CSS3, JavaScript, Chart.js
- **배포**: Docker, Docker Compose

### 시작하기
1. **저장소 복제**:
   ```bash
   git clone <repository-url>
   cd ETF_Manager
   ```
2. **환경 변수 설정**: `.env` 파일을 생성하여 `FLASK_SECRET_KEY`, `ADMIN_PASSWORD_HASH`, 텔레그램 봇 토큰 등을 설정합니다.
3. **실행**:
   ```bash
   bash run.sh
   ```

---

## 🔒 Security Notice (보안 주의사항)

This project contains sensitive configuration files that are ignored by Git. Ensure you do not upload the following files to public repositories:
본 프로젝트는 보안을 위해 다음 파일들을 Git 추적에서 제외하고 있습니다. 공개 저장소 업로드 시 주의하세요:
- `.env`
- `*.tsv` (거래 데이터 및 개인 설정 파일)

---
**Created by Vibe Coding**
*Testing & Demo Version*