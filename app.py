from flask import Flask, render_template, request, redirect, url_for, session, flash
import csv
import os
import pandas as pd
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or os.urandom(24).hex()

# .env에서 해시된 비밀번호를 가져옵니다. 없으면 기본값(admin1234)의 해시를 사용합니다.
ADMIN_PASSWORD_HASH = os.getenv('ADMIN_PASSWORD_HASH', generate_password_hash('admin1234'))

# ── 파일 상수 ────────────────────────────────────────────────
LEDGER_FILES = {
    'ISA':    'ISA_ledger.tsv',
    'IRP':    'IRP_ledger.tsv',
    '연금저축': 'Pension_Savings_Ledger.tsv',
}
SETTINGS_FILE = 'portfolio_settings.tsv'
TELEGRAM_FILE = 'telegram_config.tsv'
ALERT_FILE    = 'alert_settings.tsv'
HISTORY_FILE  = 'portfolio_history.tsv'
ACCOUNTS      = ['ISA', 'IRP', '연금저축']

TSV_HEADER = ['timestamp', 'type', 'ticker', 'quantity', 'price',
              'cash_delta', 'balance_after', 'ref_id', 'note']

TYPE_KO = {
    'BUY':      '매수',
    'SELL':     '매도',
    'DEPOSIT':  '입금',
    'DIVIDEND': '분배금',
    'INTEREST': '이자',
    'OTHER':    '기타',
}

price_cache = {}
# HTTP 연결 재사용을 위한 글로벌 세션 설정
http_session = requests.Session()
http_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})

# API 호출 타임아웃 설정 (초)
API_TIMEOUT = 10
# API 재시도 횟수
API_MAX_RETRIES = 2
# 재시도 대기 시간 (초)
API_RETRY_DELAY = 2

# ── 환율 크롤링 ──────────────────────────────────────────────
def get_exchange_rates():
    now = datetime.now()
    config = get_telegram_config()
    try:
        interval_sec = int(config.get('interval', 600))
    except:
        interval_sec = 600

    if 'EXCHANGE_RATES' in price_cache:
        if now - price_cache['EXCHANGE_RATES']['time'] < timedelta(seconds=interval_sec):
            return price_cache['EXCHANGE_RATES']['data']

    rates = {
        'USD': {'name': '미국 USD',        'value': '0.00', 'change': '0.00', 'status': 'same'},
        'JPY': {'name': '일본 JPY (100엔)', 'value': '0.00', 'change': '0.00', 'status': 'same'},
        'CNY': {'name': '중국 CNY',        'value': '0.00', 'change': '0.00', 'status': 'same'},
    }
    # 재시도 로직
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            res = http_session.get('https://finance.naver.com/marketindex/', timeout=API_TIMEOUT)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                for item in soup.select('#exchangeList > li'):
                    title_el = item.select_one('.h_lst')
                    title    = title_el.text if title_el else ''
                    sym = ('USD' if '미국' in title else
                           'JPY' if '일본' in title else
                           'CNY' if '중국' in title else None)
                    if not sym:
                        continue
                    val_el = item.select_one('.value')
                    chg_el = item.select_one('.change')
                    hi_el  = item.select_one('.head_info')
                    status = ('up'   if hi_el and '상승' in hi_el.text else
                              'down' if hi_el and '하락' in hi_el.text else 'same')
                    rates[sym] = {
                        'name':   '일본 JPY (100엔)' if sym == 'JPY' else title.strip(),
                        'value':  val_el.text if val_el else '0.00',
                        'change': chg_el.text if chg_el else '0.00',
                        'status': status,
                    }
                price_cache['EXCHANGE_RATES'] = {'data': rates, 'time': now}
                return rates
            else:
                print(f'⚠️ [환율] HTTP {res.status_code} (시도 {attempt+1}/{API_MAX_RETRIES+1})')
        except Exception as e:
            print(f'⚠️ [환율] {e} (시도 {attempt+1}/{API_MAX_RETRIES+1})')
        if attempt < API_MAX_RETRIES:
            print(f'⏳ [환율] {API_RETRY_DELAY}초 후 재시도...')
            time.sleep(API_RETRY_DELAY)
    # 모든 재시도 실패 시 마지막 rates 반환
    print('❌ [환율] 모든 재시도 실패')
    return rates


# ── 현재가 크롤링 ────────────────────────────────────────────
def _fetch_52week(ticker):
    """네이버 금융 종목 페이지에서 52주 최고/최저가를 스크래핑합니다."""
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            url = f"https://finance.naver.com/item/main.naver?code={ticker}"
            res = http_session.get(url, timeout=API_TIMEOUT)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                for th in soup.find_all('th'):
                    if '52주' in th.get_text(strip=True):
                        td = th.find_next_sibling('td')
                        if td:
                            # 형식: "223,000l53,700" ('l'로 최고/최저 구분)
                            parts = td.get_text(strip=True).replace(',', '').split('l')
                            if len(parts) == 2:
                                try:
                                    return int(float(parts[0])), int(float(parts[1]))
                                except:
                                    pass
            else:
                print(f'⚠️ [52주 {ticker}] HTTP {res.status_code} (시도 {attempt+1}/{API_MAX_RETRIES+1})')
        except Exception as e:
            print(f'⚠️ [52주 {ticker}] {e} (시도 {attempt+1}/{API_MAX_RETRIES+1})')
        if attempt < API_MAX_RETRIES:
            time.sleep(API_RETRY_DELAY)
    print(f'❌ [52주 {ticker}] 모든 재시도 실패')
    return 0, 0


def get_current_price(ticker):
    now = datetime.now()
    # 가격 캐시 주기는 텔레그램 알림 주기와 별개로 짧게(예: 2분) 설정합니다.
    # 알림 주기가 1시간으로 설정되어 있어도 UI에서는 최신가를 더 자주 확인할 수 있게 합니다.
    interval_sec = 120 

    if ticker in price_cache:
        if now - price_cache[ticker]['time'] < timedelta(seconds=interval_sec):
            return price_cache[ticker]['price']

    def safe_int(val):
        try:
            return int(float(str(val).replace(',', '')))
        except (ValueError, TypeError):
            return 0

    # 재시도 로직
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            # 종목 코드는 반드시 6자리여야 합니다 (예: 5930 -> 005930)
            ticker_code = ticker.strip().zfill(6) if ticker.isdigit() else ticker
            
            # polling API: Referer 헤더를 추가하여 차단을 방지합니다.
            url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{ticker_code}"
            headers = {
                'Referer': 'https://finance.naver.com/'
            }
            res = http_session.get(url, headers=headers, timeout=API_TIMEOUT)

            if res.status_code == 200:
                data = res.json()
                if data.get('resultCode') == 'success':
                    item = data.get('result', {}).get('areas', [{}])[0].get('datas', [{}])[0]
                    price = safe_int(item.get('nv', 0))
                    if price > 0:
                        old_data = price_cache.get(ticker, {})
                        session_high = max(price, old_data.get('session_high', 0))

                        # 52주 고/저가는 자주 변하지 않으므로 12시간마다 한 번만 갱신합니다.
                        last_52w_update = old_data.get('52w_time', datetime.min)
                        if now - last_52w_update > timedelta(hours=12):
                            high52, low52 = _fetch_52week(ticker_code)
                            last_52w_update = now
                        else:
                            high52 = old_data.get('high52', 0)
                            low52  = old_data.get('low52',  0)

                        high52 = high52 or old_data.get('high52', 0)
                        low52  = low52  or old_data.get('low52',  0)

                        price_cache[ticker] = {
                            'price':        price,
                            'high52':       high52,
                            'low52':        low52,
                            'session_high': session_high,
                            'time':         now,
                            '52w_time':     last_52w_update
                        }
                        return price
                else:
                    print(f'⚠️ [{ticker}] API error (시도 {attempt+1}/{API_MAX_RETRIES+1})')
            else:
                print(f'⚠️ [{ticker}] HTTP {res.status_code} (시도 {attempt+1}/{API_MAX_RETRIES+1})')
        except Exception as e:
            print(f'⚠️ [{ticker}] {e} (시도 {attempt+1}/{API_MAX_RETRIES+1})')
        if attempt < API_MAX_RETRIES:
            time.sleep(API_RETRY_DELAY)
    # 모든 재시도 실패 시 캐시된 값 반환
    print(f'❌ [{ticker}] 모든 재시도 실패')
    return price_cache.get(ticker, {}).get('price', 0)


# ── 원장(Ledger) I/O ─────────────────────────────────────────
def read_ledger(account):
    """계좌 원장 TSV를 읽어 dict 리스트로 반환합니다."""
    path = LEDGER_FILES.get(account, '')
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter='\t')
        return [dict(r) for r in reader]


def get_cash_balance(account):
    """원장 마지막 행의 balance_after를 현재 예수금으로 반환합니다."""
    rows = read_ledger(account)
    if not rows:
        return 0.0
    try:
        return float(str(rows[-1].get('balance_after', 0)).replace(',', ''))
    except ValueError:
        return 0.0


def append_ledger_row(account, tx_type, ticker, quantity, price, cash_delta, note):
    """계좌 원장에 새 행을 추가합니다. balance_after와 ref_id는 자동 계산됩니다."""
    path = LEDGER_FILES[account]
    rows = read_ledger(account)

    last_balance  = float(str(rows[-1]['balance_after']).replace(',', '')) if rows else 0.0
    balance_after = round(last_balance + cash_delta)

    today     = datetime.now().strftime('%Y-%m-%d')
    today_cnt = sum(1 for r in rows if str(r.get('timestamp', '')).startswith(today))
    ref_id    = today_cnt + 1
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    write_header = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        if write_header:
            w.writerow(TSV_HEADER)
        w.writerow([timestamp, tx_type, ticker,
                    quantity, price, cash_delta,
                    balance_after, ref_id, note])
    print(f'✅ [{account}] {tx_type} 기록 완료 (잔고 {balance_after:,}원)')


def delete_last_ledger_row(account):
    """원장의 마지막 데이터 행만 삭제합니다 (정합성 보장)."""
    path = LEDGER_FILES.get(account, '')
    if not path or not os.path.exists(path):
        return False
    rows = read_ledger(account)
    if not rows:
        return False
    rows = rows[:-1]
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(TSV_HEADER)
        for r in rows:
            w.writerow([r.get(h, '') for h in TSV_HEADER])
    print(f'✅ [{account}] 마지막 행 삭제 완료')
    return True


# ── 포트폴리오 설정 ──────────────────────────────────────────
def read_settings_rows():
    """설정 TSV를 읽어 (파일 내 행 인덱스, 행 리스트) 형태로 반환합니다."""
    if not os.path.exists(SETTINGS_FILE):
        return []
    with open(SETTINGS_FILE, encoding='utf-8-sig') as f:
        rows = list(csv.reader(f, delimiter='\t'))

    parsed = []
    for i in range(1, len(rows)):
        if len(rows[i]) >= 4:
            show_flag = str(rows[i][4]).strip() if len(rows[i]) >= 5 else 'Y'
            currency  = rows[i][5].strip() if len(rows[i]) >= 6 else 'KRW'
            row_data  = rows[i][:4] + [show_flag, currency]
            parsed.append((i, row_data))

    # 정렬: 표시('Y') 우선, 그 다음 티커 오름차순
    return sorted(parsed, key=lambda x: (0 if x[1][4] == 'Y' else 1, x[1][0]))


def get_portfolio_settings():
    """설정을 {ticker: {name, class, weight, is_active, currency}} dict 로 반환합니다."""
    settings = {}
    for _, row in read_settings_rows():
        ticker = str(row[0]).strip().upper()
        if not ticker:
            continue
        try:
            weight = float(str(row[3]).strip().replace('%', ''))
        except ValueError:
            weight = 0.0

        is_active = (str(row[4]).strip() == 'Y') if len(row) > 4 else True
        currency  = str(row[5]).strip() if len(row) > 5 else 'KRW'

        settings[ticker] = {
            'name':      str(row[1]).strip(),
            'class':     str(row[2]).strip(),
            'weight':    weight,
            'is_active': is_active,
            'currency':  currency,
        }
    return settings


def save_setting_append(ticker, name, cls, weight):
    write_header = not os.path.exists(SETTINGS_FILE)
    with open(SETTINGS_FILE, 'a', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        if write_header:
            w.writerow(['티커', '종목명', '자산군', '목표비중', '활성화'])
        w.writerow([ticker, name, cls, weight, 'Y'])

def delete_setting_row(row_idx):
    """설정 파일에서 row_idx 행을 삭제합니다."""
    if not os.path.exists(SETTINGS_FILE):
        return
    with open(SETTINGS_FILE, encoding='utf-8-sig') as f:
        rows = list(csv.reader(f, delimiter='\t'))
    if 0 < row_idx < len(rows):
        del rows[row_idx]
        with open(SETTINGS_FILE, 'w', encoding='utf-8', newline='') as f:
            csv.writer(f, delimiter='\t').writerows(rows)


# ── 텔레그램 알림 ──────────────────────────────────────────
def get_telegram_config():
    """텔레그램 설정(Token, Chat ID)을 읽어옵니다."""
    # 환경 변수에서 먼저 확인
    env_token = os.getenv('TELEGRAM_TOKEN')
    env_chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if env_token and env_chat_id:
        return {
            'token': env_token,
            'chat_id': env_chat_id,
            'enabled': os.getenv('TELEGRAM_ENABLED', 'Y'),
            'interval': os.getenv('TELEGRAM_INTERVAL', '3600'),
            'managed_by_env': True # 환경 변수로 관리 중임을 표시
        }

    # 환경 변수가 없으면 TSV 파일 확인 (하위 호환성)
    default = {'token': '', 'chat_id': '', 'enabled': 'N', 'interval': '3600',
               'warn_threshold': '3', 'danger_threshold': '5'}
    if not os.path.exists(TELEGRAM_FILE):
        return default
    with open(TELEGRAM_FILE, encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        rows = list(reader)
        if not rows: return default
        res = rows[0]
        if 'interval'         not in res: res['interval']         = '3600'
        if 'warn_threshold'   not in res: res['warn_threshold']   = '3'
        if 'danger_threshold' not in res: res['danger_threshold'] = '5'
        return res

def send_telegram_notification(message):
    """설정된 텔레그램 봇으로 메시지를 전송합니다."""
    config = get_telegram_config()
    if config.get('enabled') != 'Y' or not config.get('token') or not config.get('chat_id'):
        return False
    
    try:
        url = f"https://api.telegram.org/bot{config['token']}/sendMessage"
        payload = {
            'chat_id': config['chat_id'],
            'text': message,
            'parse_mode': 'HTML'
        }
        res = requests.post(url, data=payload, timeout=5)
        return res.status_code == 200
    except Exception as e:
        print(f"❌ [텔레그램] 전송 실패: {e}")
        return False

def save_telegram_config(token, chat_id, enabled, interval, warn_threshold=None, danger_threshold=None):
    config = get_telegram_config()
    if warn_threshold   is None: warn_threshold   = config.get('warn_threshold',   '3')
    if danger_threshold is None: danger_threshold = config.get('danger_threshold', '5')
    fieldnames = ['token', 'chat_id', 'enabled', 'interval', 'warn_threshold', 'danger_threshold']
    with open(TELEGRAM_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
        w.writeheader()
        w.writerow({'token': token, 'chat_id': chat_id, 'enabled': enabled,
                    'interval': interval, 'warn_threshold': warn_threshold,
                    'danger_threshold': danger_threshold})

def update_alert_triggered(alert_id):
    """알림 발송 시각을 기록하여 중복 발송을 방지합니다."""
    alerts = get_alert_settings()
    for a in alerts:
        if a['id'] == str(alert_id):
            a['last_triggered'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(ALERT_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'type', 'ticker', 'ref_type', 'condition', 'value', 'last_triggered'], delimiter='\t')
        w.writeheader()
        w.writerows(alerts)

# ── 알림 조건 설정 관리 ─────────────────────────────────────
def get_alert_settings():
    if not os.path.exists(ALERT_FILE):
        return []
    with open(ALERT_FILE, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))

def save_alert_setting(alert_data):
    fieldnames = ['id', 'type', 'ticker', 'ref_type', 'condition', 'value', 'last_triggered']
    alerts = get_alert_settings()
    alert_data['id'] = str(int(time.time()))
    alert_data['last_triggered'] = ''
    alerts.append(alert_data)
    with open(ALERT_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter='\t')
        w.writeheader()
        w.writerows(alerts)

def delete_alert_setting(alert_id):
    alerts = [a for a in get_alert_settings() if a['id'] != str(alert_id)]
    with open(ALERT_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'type', 'ticker', 'ref_type', 'condition', 'value', 'last_triggered'], delimiter='\t')
        w.writeheader()
        w.writerows(alerts)

def get_last_purchase_price(ticker):
    """가장 최근 매수 가격을 찾습니다."""
    last_buy = None
    for acc in ACCOUNTS:
        rows = read_ledger(acc)
        for r in reversed(rows):
            if r.get('ticker') == ticker and r.get('type') == 'BUY':
                # 전체 계좌 중 시간상 가장 늦은(최신) 기록을 저장
                current_time = r.get('timestamp', '')
                if not last_buy or current_time > last_buy['time']:
                    last_buy = {'price': float(r.get('price', 0)), 'time': current_time}
                break # 해당 계좌의 가장 최신 기록을 찾았으므로 다음 계좌로
    return last_buy['price'] if last_buy else 0

def get_last_sale_price(ticker):
    """가장 최근 매도 가격을 찾습니다."""
    last_sell = None
    for acc in ACCOUNTS:
        rows = read_ledger(acc)
        for r in reversed(rows):
            if r.get('ticker') == ticker and r.get('type') == 'SELL':
                current_time = r.get('timestamp', '')
                if not last_sell or current_time > last_sell['time']:
                    last_sell = {'price': float(r.get('price', 0)), 'time': current_time}
                break
    return last_sell['price'] if last_sell else 0

def save_portfolio_snapshot(eval_total, deposit_total):
    """총 평가액·입금액을 기록합니다. 최소 1시간 간격으로 저장합니다."""
    if eval_total <= 0:
        return
    now = datetime.now()
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding='utf-8') as f:
            rows = list(csv.DictReader(f, delimiter='\t'))
        if rows:
            try:
                last_ts = datetime.strptime(rows[-1]['timestamp'], '%Y-%m-%d %H:%M:%S')
                if (now - last_ts).total_seconds() < 3600:
                    return
            except Exception:
                pass
    write_header = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, 'a', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        if write_header:
            w.writerow(['timestamp', 'eval', 'deposit'])
        w.writerow([now.strftime('%Y-%m-%d %H:%M:%S'), round(eval_total), round(deposit_total)])


def get_portfolio_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def background_alert_worker():
    """주기적으로 조건을 체크하여 알림을 보냅니다."""
    print("🚀 알림 감시 워커 시작")
    while True:
        try:
            config = get_telegram_config()

            # 텔레그램 활성화 여부와 무관하게 항상 포트폴리오 스냅샷 저장
            status      = get_portfolio_status()
            summary_all = get_portfolio_summary_stats(status)
            total_s     = summary_all.get('통합', {})
            save_portfolio_snapshot(total_s.get('eval', 0), total_s.get('deposit', 0))

            if config.get('enabled') == 'Y' and config.get('token'):
                alerts = get_alert_settings()
                total_status = status.get('통합', [])
                summary = total_s
                
                for alert in alerts:
                    # 중복 알림 방지 (최근 6시간 내 발송 여부 체크)
                    last_t_str = alert.get('last_triggered', '')
                    if last_t_str:
                        last_t = datetime.strptime(last_t_str, '%Y-%m-%d %H:%M:%S')
                        if datetime.now() - last_t < timedelta(hours=6):
                            continue

                    msg = None
                    # 1. 가격 알림 체크
                    if alert['type'] == 'PRICE':
                        ticker = alert['ticker']
                        cur_price = get_current_price(ticker)
                        if cur_price <= 0: continue
                        
                        # 기준가(Ref) 결정
                        ref_val = 0
                        ref_name = ""
                        if alert['ref_type'] == 'AVG':
                            item = next((i for i in total_status if i['티커'] == ticker), None)
                            ref_val = item['평단가'] if item else 0
                            ref_name = "통합 평단가"
                        elif alert['ref_type'] == 'LAST':
                            ref_val = get_last_purchase_price(ticker)
                            ref_name = "마지막 구매가"
                        elif alert['ref_type'] == 'LAST_SELL':
                            ref_val = get_last_sale_price(ticker)
                            ref_name = "마지막 판매가"
                        elif alert['ref_type'] == 'HIGH52':
                            ref_val = price_cache.get(ticker, {}).get('high52', 0)
                            ref_name = "52주 최고가"
                        elif alert['ref_type'] == 'LOW52':
                            ref_val = price_cache.get(ticker, {}).get('low52', 0)
                            ref_name = "52주 최저가"
                        elif alert['ref_type'] == 'SESSION_HIGH':
                            ref_val = price_cache.get(ticker, {}).get('session_high', 0)
                            ref_name = "최근 고점"
                        elif alert['ref_type'] == 'ROI':
                            item = next((i for i in total_status if i['티커'] == ticker), None)
                            ref_val = item['평단가'] if item else 0
                            ref_name = "평단가 수익률"
                        elif alert['ref_type'] == 'FIXED':
                            ref_val = float(alert['value'])
                            ref_name = "지정가"

                        if ref_val <= 0: continue

                        # 조건 비교
                        cur_roi = round(((cur_price / ref_val) - 1) * 100, 2) if alert['ref_type'] in ['ROI', 'AVG'] else 0
                        if alert['ref_type'] == 'ROI':
                            # ROI 모드는 +5%, +15% 등 목표 지점을 직접 계산
                            threshold = ref_val * (1 + float(alert['value'])/100)
                            if alert['condition'] == 'UP_PCT' and cur_price >= threshold:
                                msg = f"🔔 <b>[수익률 도달]</b> {ticker}\n현재가: {cur_price:,}원 (ROI: {cur_roi}%)\n목표: 평단가 대비 <b>+{alert['value']}%</b> 지점 상향 돌파"
                            elif alert['condition'] == 'DOWN_PCT' and cur_price <= threshold:
                                msg = f"⚠️ <b>[수익률 이탈]</b> {ticker}\n현재가: {cur_price:,}원 (ROI: {cur_roi}%)\n목표: 평단가 대비 <b>+{alert['value']}%</b> 지점 하향 돌파"
                        else:
                            # 일반 PCT 모드 (기준가 대비 변동폭)
                            if alert['condition'] == 'UP_PCT':
                                threshold = ref_val * (1 + float(alert['value'])/100)
                                if cur_price >= threshold:
                                    msg = f"🔔 <b>[가격 알림]</b> {ticker}\n현재가({cur_price:,}원)가 {ref_name} 대비 {alert['value']}% 상승했습니다."
                            elif alert['condition'] == 'DOWN_PCT':
                                threshold = ref_val * (1 - float(alert['value'])/100)
                                if cur_price <= threshold:
                                    msg = f"⚠️ <b>[가격 알림]</b> {ticker}\n현재가({cur_price:,}원)가 {ref_name} 대비 {alert['value']}% 하락했습니다."

                    # 2. 비중 알림 체크
                    elif alert['type'] == 'PORTFOLIO':
                        if alert['ref_type'] == 'TURNOVER':
                            # 전체 포트폴리오 비중 오차 합계 (Turnover)
                            cur_turnover = summary.get('turnover', 0)
                            if cur_turnover >= float(alert['value']):
                                msg = f"⚖️ <b>[비중 알림]</b> 통합 포트폴리오 오차가 {cur_turnover}%에 도달했습니다. (기준: {alert['value']}%)"
                        elif alert['ref_type'] == 'ITEM_DEV':
                            # 특정 종목 비중 오차 (티커 지정 가능)
                            target_ticker = alert.get('ticker')
                            for item in total_status:
                                if target_ticker and item['티커'] != target_ticker:
                                    continue
                                dev = item.get('비중오차(%p)', 0)
                                target_w = item.get('목표비중(%)', 0)
                                
                                if alert['condition'] == 'REL_OVER':
                                    # 상대적 괴리율 계산 (목표비중 대비 몇 %나 벗어났는가)
                                    rel_dev = (abs(dev) / target_w * 100) if target_w > 0 else 0
                                    if rel_dev >= float(alert['value']):
                                        msg = f"⚖️ <b>[비중 알림]</b> {item['종목명']} 비중이 목표({target_w}%) 대비 {round(rel_dev, 1)}% 괴리되었습니다."
                                        break
                                else: # OVER (절대적 %p 오차)
                                    if abs(dev) >= float(alert['value']):
                                        direction = "초과" if dev > 0 else "미달"
                                        msg = f"⚖️ <b>[비중 알림]</b> {item['종목명']} 비중이 목표 대비 {abs(dev)}%p {direction} 상태입니다."
                                        break

                    if msg:
                        if send_telegram_notification(msg):
                            update_alert_triggered(alert['id'])

            # 다음 감시까지 대기 (설정값 사용)
            try:
                sleep_interval = int(config.get('interval', 3600))
            except:
                sleep_interval = 3600
            time.sleep(max(10, sleep_interval)) # 최소 10초 제한

        except Exception as e:
            print(f"❌ [워커 오류] {e}")

def calc_dollar_exposure(portfolio_status, settings_data, exchange_rates):
    """USD 자산의 평가금액 비율과 환율 민감도를 계산합니다."""
    items      = portfolio_status.get('통합', [])
    total_eval = sum(i['평가금액'] for i in items)
    if total_eval <= 0:
        return None
    usd_eval = sum(
        i['평가금액'] for i in items
        if settings_data.get(i['티커'], {}).get('currency', 'KRW') == 'USD'
    )
    usd_pct = round(usd_eval / total_eval * 100, 1)
    try:
        usd_rate = float(str(exchange_rates.get('USD', {}).get('value', '0')).replace(',', ''))
    except Exception:
        usd_rate = 0
    sensitivity = round(usd_eval * 0.01)  # 환율 1% 변동 시 평가액 영향
    return {
        'usd_pct':     usd_pct,
        'usd_eval':    round(usd_eval),
        'total_eval':  round(total_eval),
        'sensitivity': sensitivity,
        'usd_rate':    usd_rate,
    }


def calc_period_pnl(history):
    """히스토리 TSV를 기반으로 월별/연도별 손익을 계산합니다.
    pnl = (평가액 변동) - (입금액 변동)  → 신규 입금 효과를 제거한 실제 운용 손익
    """
    from collections import defaultdict

    if len(history) < 2:
        return {'monthly': [], 'yearly': []}

    monthly_rows = defaultdict(list)
    yearly_rows  = defaultdict(list)
    for row in history:
        try:
            ts  = str(row.get('timestamp', ''))
            ym  = ts[:7]   # 'YYYY-MM'
            yr  = ts[:4]   # 'YYYY'
            ev  = int(row['eval'])
            dep = int(row['deposit'])
            monthly_rows[ym].append({'eval': ev, 'deposit': dep})
            yearly_rows[yr].append({'eval': ev, 'deposit': dep})
        except Exception:
            continue

    def compute(groups, limit=None):
        result = []
        for period, rows in sorted(groups.items()):
            first = rows[0]
            last  = rows[-1]
            pnl   = (last['eval'] - first['eval']) - (last['deposit'] - first['deposit'])
            roi   = round(pnl / first['eval'] * 100, 2) if first['eval'] > 0 else 0
            result.append({'period': period, 'pnl': round(pnl), 'roi': roi,
                           'eval_end': last['eval']})
        return result[-limit:] if limit else result

    return {'monthly': compute(monthly_rows, limit=12), 'yearly': compute(yearly_rows)}


def get_rebalance_status(portfolio_status, warn_threshold=3.0, danger_threshold=5.0):
    """비중 오차 기반 리밸런싱 신호등 상태를 반환합니다."""
    items = [i for i in portfolio_status.get('통합', []) if i['티커'] != 'CASH']
    if not items:
        return None
    max_dev      = max((abs(i['비중오차(%p)']) for i in items), default=0)
    count_warn   = sum(1 for i in items if abs(i['비중오차(%p)']) >= warn_threshold)
    count_danger = sum(1 for i in items if abs(i['비중오차(%p)']) >= danger_threshold)
    level = 'danger' if count_danger > 0 else 'warning' if count_warn > 0 else 'good'
    return {
        'level':           level,
        'max_dev':         round(max_dev, 2),
        'count_warn':      count_warn,
        'count_danger':    count_danger,
        'total':           len(items),
        'warn_threshold':  warn_threshold,
        'danger_threshold': danger_threshold,
    }


def xirr_calc(cashflows):
    """
    XIRR: 입금 날짜를 반영한 연환산 내부수익률 (Newton-Raphson)
    cashflows: list of (datetime, float) — 입금은 음수, 평가액은 양수
    returns: 연환산 수익률(%) 또는 None
    """
    if len(cashflows) < 2:
        return None
    dates, amounts = zip(*cashflows)
    if not any(a < 0 for a in amounts) or not any(a > 0 for a in amounts):
        return None

    t0 = min(dates)
    years = [(d - t0).days / 365.25 for d in dates]

    def npv(r):
        return sum(a / (1 + r) ** t for a, t in zip(amounts, years))

    def d_npv(r):
        return -sum(t * a / (1 + r) ** (t + 1) for a, t in zip(amounts, years))

    for r0 in [0.1, 0.0, 0.5, -0.05]:
        r = r0
        try:
            for _ in range(200):
                f, df = npv(r), d_npv(r)
                if abs(df) < 1e-12:
                    break
                r_new = max(r - f / df, -0.9999)
                if abs(r_new - r) < 1e-7:
                    r = r_new
                    break
                r = r_new
            if -0.9999 < r < 100:
                return round(r * 100, 2)
        except Exception:
            continue
    return None


def get_portfolio_summary_stats(portfolio_status):
    """수익률 및 비중 오차 통계를 계산합니다."""
    summary_stats = {}
    total_net_deposit = 0

    account_deposits  = {}
    account_cashflows = {}  # {acc: [(datetime, amount), ...]}  입금은 음수

    for acc in ACCOUNTS:
        ledger = read_ledger(acc)
        acc_deposit = 0
        acc_flows   = []
        for r in ledger:
            if r.get('type') == 'DEPOSIT':
                amount = float(str(r.get('cash_delta', 0)).replace(',', ''))
                try:
                    ts = datetime.strptime(str(r.get('timestamp', ''))[:10], '%Y-%m-%d')
                    acc_flows.append((ts, -amount))  # 투자자 입장에서 지출 → 음수
                except ValueError:
                    pass
                acc_deposit += amount
        account_deposits[acc]  = acc_deposit
        account_cashflows[acc] = acc_flows
        total_net_deposit += acc_deposit

    today          = datetime.now()
    total_cashflows = [flow for flows in account_cashflows.values() for flow in flows]

    for acc in ['통합'] + ACCOUNTS:
        status_list = portfolio_status.get(acc, [])
        if not status_list:
            continue

        df_res    = pd.DataFrame(status_list)
        acc_eval  = df_res['평가금액'].sum()
        turnover  = round(df_res['비중오차(%p)'].abs().sum() / 2, 2)
        deposit   = total_net_deposit if acc == '통합' else account_deposits.get(acc, 0)
        roi       = round(((acc_eval / deposit) - 1) * 100, 2) if deposit > 0 else 0

        # XIRR: 입금 내역(음수) + 오늘 평가액(양수)을 합쳐 연환산 수익률 계산
        flows = (total_cashflows if acc == '통합' else account_cashflows.get(acc, []))
        xirr  = xirr_calc(flows + [(today, acc_eval)]) if flows else None

        summary_stats[acc] = {
            'deposit': deposit, 'eval': acc_eval,
            'roi': roi, 'turnover': turnover, 'xirr': xirr,
        }

    return summary_stats

# ── 포트폴리오 현황 계산 ─────────────────────────────────────
def build_equity_df(account):
    """BUY/SELL 행만 추출합니다. ticker='-' (MMF 등 현금성)는 제외합니다."""
    rows = read_ledger(account)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[(df['ticker'] != '-') & (df['type'].isin(['BUY', 'SELL']))]
    if df.empty:
        return pd.DataFrame()
    df['ticker'] = df['ticker'].str.strip().str.upper()
    df['quantity'] = pd.to_numeric(df['quantity'], errors='coerce').fillna(0)
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='mixed', errors='coerce')
    df['price']    = pd.to_numeric(df['price'],    errors='coerce').fillna(0)
    df['amount']   = df['quantity'] * df['price']
    df['account']  = account
    return df


def get_portfolio_status(extra_tickers=None):
    settings_data = get_portfolio_settings()

    # 전체 거래 DataFrame
    dfs   = [build_equity_df(acc) for acc in ACCOUNTS]
    dfs   = [d for d in dfs if not d.empty]
    df_all = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    # 1. 필요한 모든 고유 티커 추출
    held_tickers = set(df_all['ticker'].unique()) if not df_all.empty else set()
    target_tickers = {t for t, v in settings_data.items() if v['weight'] > 0 and v.get('is_active', True)}
    all_unique_tickers = {t for t in (held_tickers | target_tickers) if t and t not in ('-', 'NAN', 'nan', 'CASH')}
    if extra_tickers:
        all_unique_tickers |= {t for t in extra_tickers if t and t not in ('-', 'CASH')}

    # 2. 병렬로 현재가 가져오기 (성능 개선 핵심)
    with ThreadPoolExecutor(max_workers=10) as executor:
        # list()로 감싸서 모든 스레드가 완료될 때까지 기다립니다 (Lazy map 방지)
        list(executor.map(get_current_price, all_unique_tickers))

    # 계좌별 현금 잔고
    cash_by_acc = {acc: get_cash_balance(acc) for acc in ACCOUNTS}
    cash_by_acc['통합'] = sum(cash_by_acc.values())

    result = {}
    for acc in ['통합'] + ACCOUNTS:
        cash_balance = cash_by_acc[acc]
        
        df_acc = pd.DataFrame()
        if not df_all.empty:
            df_acc = df_all.copy() if acc == '통합' else df_all[df_all['account'] == acc]

        grouped = []
        acc_held_tickers = set(df_acc['ticker'].unique()) if not df_acc.empty else set()
        # 해당 계좌에서 관리해야 할 대상 티커 (보유 중이거나 목표 비중이 있는 경우)
        acc_target_tickers = target_tickers if acc == '통합' else target_tickers
        acc_all_tickers = acc_held_tickers | acc_target_tickers

        for ticker in acc_all_tickers:
            if not ticker or ticker in ('-', 'NAN', 'nan'):
                continue
            target_w = settings_data.get(ticker, {}).get('weight', 0)

            # 해당 티커의 거래 내역 추출 및 시간순 정렬
            current_quantity = 0.0
            current_cost_basis = 0.0
            
            if not df_acc.empty:
                ticker_df = df_acc[df_acc['ticker'] == ticker].sort_values('timestamp', na_position='first')
                
                # 이동평균법에 따른 평단가 계산
                for _, tx in ticker_df.iterrows():
                    tx_qty = float(str(tx['quantity']).replace(',', ''))
                    tx_price = float(str(tx['price']).replace(',', ''))
                    
                    if tx['type'] == 'BUY':
                        current_cost_basis += (tx_qty * tx_price)
                        current_quantity += tx_qty
                    elif tx['type'] == 'SELL':
                        if current_quantity > 0:
                            # 매도 시점의 유닛당 원가 계산
                            unit_cost = current_cost_basis / current_quantity
                            current_cost_basis -= (tx_qty * unit_cost)
                            current_quantity -= tx_qty
                        
                        # 수량이 0 이하가 되면 원가 기반 초기화 (완전 매도 처리)
                        if current_quantity < 0.001: # 부동소수점 오차 방지
                            current_quantity = 0.0
                            current_cost_basis = 0.0
            
            # 필터링 로직: 
            # 1. 잔고가 있고 목표 비중이 있으면 표시
            # 2. 통합 탭에서는 잔고가 0이라도 목표 비중이 있으면 표시 (살 종목 확인용)
            # 3. 개별 계좌 탭에서는 잔고가 0이면 무조건 숨김 (불필요한 노출 방지)
            if current_quantity < 0.001:
                if acc == '통합':
                    if target_w <= 0: continue
                else:
                    continue

            avg_price = current_cost_basis / current_quantity if current_quantity > 0 else 0
            cur_price = get_current_price(ticker) or avg_price

            buy_amt = round(current_quantity * avg_price)
            eval_amt = round(current_quantity * cur_price)

            grouped.append({
                '티커':        ticker,
                '종목명':      settings_data.get(ticker, {}).get('name', ticker),
                '목표비중(%)': target_w,
                '보유수량':    round(current_quantity, 2),
                '평단가':      round(avg_price),
                '현재가':      cur_price,
                'high52':      price_cache.get(ticker, {}).get('high52', 0),
                'low52':       price_cache.get(ticker, {}).get('low52', 0),
                '매수금액':    buy_amt,
                '평가금액':    eval_amt,
                '손익금액':    eval_amt - buy_amt,
                '손익률(%)':   round(((cur_price / avg_price) - 1) * 100, 2) if avg_price > 0 else 0,
            })

        # 현금 행
        cash_target = settings_data.get('CASH', {}).get('weight', 0)
        grouped.append({
            '티커': 'CASH', '종목명': '현금(예수금)', '목표비중(%)': cash_target,
            '보유수량': round(cash_balance), '평단가': 1, '현재가': 1,
            'high52': 0, 'low52': 0,
            '매수금액': round(cash_balance), '평가금액': round(cash_balance), '손익률(%)': 0.0, '손익금액': 0,
        })

        df_res    = pd.DataFrame(grouped)
        total_val = df_res['평가금액'].sum()
        df_res['현재비중(%)']  = round(df_res['평가금액'] / total_val * 100, 2) if total_val > 0 else 0
        df_res['비중오차(%p)'] = round(df_res['현재비중(%)'] - df_res['목표비중(%)'], 2)
        result[acc] = df_res.to_dict('records')

    return result


# ── 라우트 ───────────────────────────────────────────────────
@app.before_request
def require_login():
    """로그인이 필요한 페이지에 대한 접근 제어"""
    allowed_routes = ['login', 'static', 'favicon']
    if request.endpoint not in allowed_routes and not session.get('logged_in'):
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['logged_in'] = True
            session.permanent = True # 브라우저 종료 시 세션 유지 여부 (필요에 따라 설정)
            return redirect(url_for('index'))
        else:
            flash('비밀번호가 일치하지 않습니다.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/favicon.ico')
def favicon():
    # 브라우저의 아이콘 요청에 대해 '내용 없음(204)' 응답을 보내 로그 노출 방지
    return '', 204

@app.route('/', methods=['GET'])
def index():
    settings_data = get_portfolio_settings()
    telegram_config = get_telegram_config()
    alerts = get_alert_settings()

    # 원장 데이터: 계좌별, 최신순
    ledger_data = {}
    for acc in ACCOUNTS:
        rows    = read_ledger(acc)
        display = []
        for i, r in enumerate(reversed(rows)):
            ticker = r.get('ticker', '-')
            name   = settings_data.get(ticker, {}).get('name', '') if ticker != '-' else ''
            display.append({
                'is_last':      (i == 0),   # 파일 내 마지막 행만 삭제 허용
                'timestamp':    str(r.get('timestamp', ''))[:16],
                'type_ko':      TYPE_KO.get(r.get('type', ''), r.get('type', '')),
                'ticker':       ticker,
                'name':         name or r.get('note', ''),
                'quantity':     r.get('quantity', ''),
                'price':        r.get('price', ''),
                'cash_delta':   r.get('cash_delta', ''),
                'balance_after': r.get('balance_after', ''),
            })
        ledger_data[acc] = display

    settings_rows    = read_settings_rows()
    
    # 알림 대상 종목들도 시세 조회 대상에 포함하여 52주 데이터 등을 확보
    alert_tickers = [a['ticker'] for a in alerts if a.get('type') == 'PRICE']
    portfolio_status = get_portfolio_status(extra_tickers=alert_tickers)
    
    exchange_data    = get_exchange_rates()

    # ── 정보 갱신 시간 계산 ──
    update_times = [v['time'] for v in price_cache.values() if isinstance(v, dict) and 'time' in v]
    last_updated_dt = max(update_times) if update_times else None
    
    try:
        # UI 상단의 '다음 갱신' 시간 표시를 위해 get_current_price와 동일한 주기를 사용합니다.
        ui_interval = 120
    except:
        ui_interval = 120
        
    last_updated_str = last_updated_dt.strftime('%H:%M:%S') if last_updated_dt else "-"
    next_update_str = (last_updated_dt + timedelta(seconds=ui_interval)).strftime('%H:%M:%S') if last_updated_dt else "즉시"

    # 알림 목록에 표시할 목표 가격 계산
    for alert in alerts:
        if alert.get('type') == 'PRICE':
            ticker = alert.get('ticker')
            ref_val = 0
            if alert.get('ref_type') == 'AVG':
                item = next((i for i in portfolio_status.get('통합', []) if i['티커'] == ticker), None)
                ref_val = item['평단가'] if item else 0
            elif alert.get('ref_type') == 'LAST':
                ref_val = get_last_purchase_price(ticker)
            elif alert.get('ref_type') == 'LAST_SELL':
                ref_val = get_last_sale_price(ticker)
            elif alert.get('ref_type') == 'HIGH52':
                ref_val = price_cache.get(ticker, {}).get('high52', 0)
            elif alert.get('ref_type') == 'LOW52':
                ref_val = price_cache.get(ticker, {}).get('low52', 0)
            elif alert.get('ref_type') == 'SESSION_HIGH':
                ref_val = price_cache.get(ticker, {}).get('session_high', 0)
            elif alert.get('ref_type') == 'ROI':
                item = next((i for i in portfolio_status.get('통합', []) if i['티커'] == ticker), None)
                ref_val = item['평단가'] if item else 0
            
            if ref_val > 0:
                try:
                    # 기준가 저장 (화면 표시용)
                    alert['ref_price'] = ref_val
                    val_f = float(alert.get('value', 0))
                    if alert.get('ref_type') == 'ROI':
                        alert['target_price'] = ref_val * (1 + val_f / 100)
                    else:
                        if alert.get('condition') == 'UP_PCT':
                            alert['target_price'] = ref_val * (1 + val_f / 100)
                        elif alert.get('condition') == 'DOWN_PCT':
                            alert['target_price'] = ref_val * (1 - val_f / 100)
                except: pass

    cash_balances    = {acc: get_cash_balance(acc) for acc in ACCOUNTS}
    cash_balances['통합'] = sum(cash_balances.values())
    summary_stats    = get_portfolio_summary_stats(portfolio_status)

    # 포트폴리오 스냅샷 저장 (1시간 간격 rate limit 적용)
    total_s = summary_stats.get('통합', {})
    save_portfolio_snapshot(total_s.get('eval', 0), total_s.get('deposit', 0))
    history = get_portfolio_history()

    # ── 인사이트 카드 계산 ──
    dollar_exposure  = calc_dollar_exposure(portfolio_status, settings_data, exchange_data)
    period_pnl       = calc_period_pnl(history)
    try:
        warn_th   = float(telegram_config.get('warn_threshold',   3))
        danger_th = float(telegram_config.get('danger_threshold', 5))
    except Exception:
        warn_th, danger_th = 3.0, 5.0
    rebalance_status = get_rebalance_status(portfolio_status, warn_th, danger_th)

    # ── 리밸런싱 추천 계산 (통합 계좌 기준) ──
    # 모든 계좌를 미리 초기화하여 추천 내역이 없어도 표시되도록 함
    rebalance_by_account = {acc: {'items': [], 'total_buy': 0, 'cash': cash_balances.get(acc, 0)} for acc in ACCOUNTS}
    total_buy_amt_all = 0
    t_status = portfolio_status.get('통합', [])
    t_val = sum(item['평가금액'] for item in t_status) if t_status else 0

    if t_val > 0:
        underweight_items = []
        total_positive_gap = 0
        
        # 1. 전역적으로 비중이 부족한 종목과 총 부족액 계산
        for item in t_status:
            if item['티커'] == 'CASH' or item['현재가'] <= 0:
                continue
            target_amt = t_val * (item['목표비중(%)'] / 100)
            gap = target_amt - item['평가금액']
            if gap > 0:
                underweight_items.append({'ticker': item['티커'], 'name': item['종목명'], 'price': item['현재가'], 'gap': gap})
                total_positive_gap += gap

        # 2. 각 계좌별로 가용 예수금을 부족분 비율에 따라 배분
        if total_positive_gap > 0:
            for acc in ACCOUNTS:
                acc_cash = rebalance_by_account[acc]['cash']
                if acc_cash <= 0:
                    continue
                
                acc_recs = []
                acc_total_buy = 0
                for info in underweight_items:
                    # 이 계좌의 예수금을 전체 부족분 중 해당 종목이 차지하는 비율만큼 할당
                    share_of_cash = acc_cash * (info['gap'] / total_positive_gap)
                    qty = int(share_of_cash // info['price'])
                    
                    if qty > 0:
                        amt = qty * info['price']
                        acc_recs.append({
                            'ticker': info['ticker'], 'name': info['name'],
                            'price': info['price'], 'qty': qty, 'amt': amt
                        })
                        acc_total_buy += amt
                
                rebalance_by_account[acc]['items'] = acc_recs
                rebalance_by_account[acc]['total_buy'] = acc_total_buy
                total_buy_amt_all += acc_total_buy

    return render_template('index.html',
                           ledger_data=ledger_data,
                           settings_rows=settings_rows,
                           settings_data=settings_data,
                           status=portfolio_status,
                           summary_stats=summary_stats,
                           cash_balances=cash_balances,
                           exchange=exchange_data,
                           telegram_config=telegram_config,
                           alerts=alerts,
                           rebalance_by_account=rebalance_by_account,
                           total_buy_amt=total_buy_amt_all,
                           last_updated=last_updated_str,
                           next_update=next_update_str,
                           history=history,
                           dollar_exposure=dollar_exposure,
                           period_pnl=period_pnl,
                           rebalance_status=rebalance_status,
                           accounts=ACCOUNTS,
                           type_ko=TYPE_KO)


@app.route('/add_tx/<account>', methods=['POST'])
def add_tx(account):
    if account not in ACCOUNTS:
        return redirect(url_for('index'))

    settings_data = get_portfolio_settings()
    tx_type = request.form.get('tx_type', '').strip()
    ticker  = request.form.get('ticker', '').strip() or '-'

    try:
        quantity = float(request.form.get('quantity', '0').replace(',', ''))
    except ValueError:
        quantity = 0.0
    try:
        price = float(request.form.get('price', '0').replace(',', ''))
    except ValueError:
        price = 0.0
    try:
        cash_amount = float(request.form.get('cash_amount', '0').replace(',', ''))
    except ValueError:
        cash_amount = 0.0

    # 거래 유형별 현금 변동 계산
    if tx_type == 'BUY':
        cash_delta = -round(quantity * price)
    elif tx_type == 'SELL':
        cash_delta = round(quantity * price)
    elif tx_type == 'DIVIDEND':
        # 분배금: 티커 유지, 수량/단가 없음, 금액만
        quantity   = 0
        price      = 0
        cash_delta = round(cash_amount)
    else:
        # DEPOSIT / INTEREST / OTHER: 티커 없음
        ticker     = '-'
        quantity   = 0
        price      = 0
        cash_delta = round(cash_amount)

    note = request.form.get('note', '').strip()
    if not note and ticker != '-':
        note = settings_data.get(ticker, {}).get('name', ticker)

    if tx_type:
        append_ledger_row(account, tx_type, ticker,
                          quantity, price, cash_delta, note)
        
        # 알림 전송
        msg = (f"<b>[거래 발생: {account}]</b>\n"
               f"유형: {TYPE_KO.get(tx_type, tx_type)}\n"
               f"종목: {note} ({ticker if ticker != '-' else '현금'})\n"
               f"금액/수량: {cash_delta:+,}원 / {quantity}")
        send_telegram_notification(msg)

    tab = {'ISA': 'ISA', 'IRP': 'IRP', '연금저축': 'pension'}.get(account, 'status')
    return redirect(url_for('index') + f'?tab={tab}')


@app.route('/delete_last/<account>', methods=['POST'])
def delete_last(account):
    if account in ACCOUNTS:
        delete_last_ledger_row(account)
    tab = {'ISA': 'ISA', 'IRP': 'IRP', '연금저축': 'pension'}.get(account, 'status')
    return redirect(url_for('index') + f'?tab={tab}')


@app.route('/add_setting', methods=['POST'])
def add_setting():
    ticker   = request.form.get('set_ticker',   '').strip()
    name     = request.form.get('set_name',     '').strip()
    cls      = request.form.get('set_class',    '').strip()
    show     = request.form.get('set_show',    'Y').strip()
    currency = request.form.get('set_currency', 'KRW').strip()
    try:
        weight = float(request.form.get('set_weight', '0'))
    except ValueError:
        weight = 0.0
    if ticker and name:
        write_header = not os.path.exists(SETTINGS_FILE)
        with open(SETTINGS_FILE, 'a', encoding='utf-8', newline='') as f:
            w = csv.writer(f, delimiter='\t')
            if write_header:
                w.writerow(['티커', '종목명', '자산군', '목표비중', '활성화', '통화'])
            w.writerow([ticker, name, cls, weight, show, currency])
    return redirect(url_for('index') + '?tab=settings')


@app.route('/delete_setting/<int:row_idx>', methods=['POST'])
def delete_setting(row_idx):
    delete_setting_row(row_idx)
    return redirect(url_for('index') + '?tab=settings')

@app.route('/update_all_settings', methods=['POST'])
def update_all_settings():
    """설정 탭에서 체크박스 및 비중 변경 사항을 일괄 저장합니다."""
    tickers    = request.form.getlist('ticker')
    names      = request.form.getlist('name')
    classes    = request.form.getlist('class')
    weights    = request.form.getlist('weight')
    actives    = request.form.getlist('active')
    currencies = request.form.getlist('currency')

    with open(SETTINGS_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['티커', '종목명', '자산군', '목표비중', '활성화', '통화'])
        for i in range(len(tickers)):
            t        = tickers[i]
            is_active = 'Y' if t in actives else 'N'
            currency  = currencies[i] if i < len(currencies) else 'KRW'
            w.writerow([t, names[i], classes[i], weights[i], is_active, currency])

    return redirect(url_for('index') + '?tab=settings')

@app.route('/update_telegram', methods=['POST'])
def update_telegram():
    config = get_telegram_config()
    token = request.form.get('token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    # interval과 enabled는 기존 설정을 유지
    save_telegram_config(token, chat_id, config['enabled'], config['interval'])
    return redirect(url_for('index') + '?tab=bot')

@app.route('/update_global_config', methods=['POST'])
def update_global_config():
    """시스템 전반의 감시 및 캐시 주기를 업데이트합니다."""
    interval        = request.form.get('interval',        '3600').strip()
    warn_threshold  = request.form.get('warn_threshold',  '3').strip()
    danger_threshold = request.form.get('danger_threshold', '5').strip()
    config = get_telegram_config()
    save_telegram_config(config['token'], config['chat_id'], config['enabled'],
                         interval, warn_threshold, danger_threshold)
    flash("설정이 저장되었습니다.")
    return redirect(url_for('index') + '?tab=settings')

@app.route('/add_alert', methods=['POST'])
def add_alert():
    alert_data = {
        'type':      request.form.get('alert_type'),
        'ticker':    request.form.get('ticker', ''),
        'ref_type':  request.form.get('ref_type'),
        'condition': request.form.get('condition'),
        'value':     request.form.get('value', '0')
    }
    save_alert_setting(alert_data)
    return redirect(url_for('index') + '?tab=bot')

@app.route('/delete_alert/<alert_id>', methods=['POST'])
def delete_alert(alert_id):
    delete_alert_setting(alert_id)
    return redirect(url_for('index') + '?tab=bot')

@app.route('/test_telegram', methods=['POST'])
def test_telegram():
    success = send_telegram_notification("🔔 포트폴리오 관리 시스템 테스트 메시지입니다.")
    return redirect(url_for('index') + '?tab=bot')

@app.route('/check_rebalancing', methods=['POST'])
def check_rebalancing():
    """비중 오차가 3%p 이상인 종목이 있다면 알림을 보냅니다."""
    status = get_portfolio_status()
    total_status = status.get('통합', [])
    alerts = []
    for item in total_status:
        if abs(item.get('비중오차(%p)', 0)) >= 3.0:
            direction = "초과" if item['비중오차(%p)'] > 0 else "미달"
            alerts.append(f"- {item['종목명']}: {item['비중오차(%p)']} %p ({direction})")
    
    if alerts:
        msg = "<b>⚠️ 리밸런싱 필요 알림 (오차 3%p 초과)</b>\n\n" + "\n".join(alerts)
        send_telegram_notification(msg)
    else:
        send_telegram_notification("✅ 현재 모든 종목의 비중 오차가 3%p 이내입니다.")
    return redirect(url_for('index') + '?tab=bot')

@app.route('/refresh_prices', methods=['POST'])
def refresh_prices():
    price_cache.clear()
    print('🔄 현재가 및 환율 강제 갱신 (캐시 초기화)')
    return redirect(url_for('index'))


# 백엔드 알림 워커 시작 (Gunicorn 등 WSGI 서버 환경에서도 스레드가 시작되도록 메인 블록 밖으로 이동)
threading.Thread(target=background_alert_worker, daemon=True).start()

if __name__ == '__main__':
    # 운영 환경 설정
    # os.getenv의 두 번째 인자는 키가 아예 없을 때만 작동하므로, 빈 문자열 체크를 추가합니다.
    host = os.getenv('FLASK_HOST') or '0.0.0.0'
    port_env = os.getenv('FLASK_PORT')
    port = int(port_env) if port_env and port_env.isdigit() else 5000
    debug_mode = (os.getenv('FLASK_DEBUG') or 'False').lower() in ['true', '1', 't']
    
    print(f"🚀 서버를 시작합니다: http://{host}:{port} (Debug: {debug_mode})")
    app.run(host=host, port=port, debug=debug_mode)
