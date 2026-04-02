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


# ── 환율 크롤링 ──────────────────────────────────────────────
def get_exchange_rates():
    now = datetime.now()
    if 'EXCHANGE_RATES' in price_cache:
        if now - price_cache['EXCHANGE_RATES']['time'] < timedelta(minutes=10):
            return price_cache['EXCHANGE_RATES']['data']

    rates = {
        'USD': {'name': '미국 USD',        'value': '0.00', 'change': '0.00', 'status': 'same'},
        'JPY': {'name': '일본 JPY (100엔)', 'value': '0.00', 'change': '0.00', 'status': 'same'},
        'CNY': {'name': '중국 CNY',        'value': '0.00', 'change': '0.00', 'status': 'same'},
    }
    try:
        res = requests.get('https://finance.naver.com/marketindex/', timeout=3)
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
    except Exception as e:
        print(f'❌ [환율] {e}')
    return rates


# ── 현재가 크롤링 ────────────────────────────────────────────
def get_current_price(ticker):
    now = datetime.now()
    if ticker in price_cache:
        if now - price_cache[ticker]['time'] < timedelta(minutes=5):
            return price_cache[ticker]['price']
    try:
        # 네이버 금융 실시간 시세 JSON 엔드포인트 (더 가볍고 빠름)
        url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{ticker}"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        
        if res.status_code == 200:
            data_json = res.json()
            item = data_json.get('result', {}).get('areas', [{}])[0].get('datas', [{}])[0]
            
            price = int(item.get('nv', 0)) # 현재가
            if price > 0:
                cache_data = {
                    'price': price,
                    'high52': int(item.get('h52', 0)), # 52주 최고가
                    'low52':  int(item.get('l52', 0)), # 52주 최저가
                    'time': now
                }
                price_cache[ticker] = cache_data
                return price
    except Exception as e:
        print(f'❌ [{ticker}] {e}')
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
            # 5번째 컬럼(드롭다운 표시 여부)이 없으면 기본값 'Y'
            show_flag = rows[i][4].strip() if len(rows[i]) >= 5 else 'Y'
            row_data = rows[i][:4] + [show_flag]
            parsed.append((i, row_data))
            
    # 정렬: 표시('Y') 우선, 그 다음 티커 오름차순
    return sorted(parsed, key=lambda x: (0 if x[1][4] == 'Y' else 1, x[1][0]))


def get_portfolio_settings():
    """설정을 {ticker: {name, class, weight, is_active}} dict 로 반환합니다."""
    settings = {}
    for _, row in read_settings_rows():
        ticker = str(row[0]).strip()
        if not ticker:
            continue
        try:
            weight = float(str(row[3]).strip().replace('%', ''))
        except ValueError:
            weight = 0.0
            
        # 5번째 열이 있으면 활성화 상태를 읽고, 없으면 기본값 True(Y)
        is_active = (str(row[4]).strip() == 'Y') if len(row) > 4 else True
        
        settings[ticker] = {
            'name':   str(row[1]).strip(),
            'class':  str(row[2]).strip(),
            'weight': weight,
            'is_active': is_active,
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
    default = {'token': '', 'chat_id': '', 'enabled': 'N', 'interval': '3600'}
    if not os.path.exists(TELEGRAM_FILE):
        return default
    with open(TELEGRAM_FILE, encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        rows = list(reader)
        if not rows: return default
        res = rows[0]
        if 'interval' not in res: res['interval'] = '3600'
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

def save_telegram_config(token, chat_id, enabled, interval):
    with open(TELEGRAM_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['token', 'chat_id', 'enabled', 'interval'], delimiter='\t')
        w.writeheader()
        w.writerow({'token': token, 'chat_id': chat_id, 'enabled': enabled, 'interval': interval})

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

def background_alert_worker():
    """주기적으로 조건을 체크하여 알림을 보냅니다."""
    print("🚀 알림 감시 워커 시작")
    while True:
        try:
            config = get_telegram_config()
            if config.get('enabled') == 'Y' and config.get('token'):
                alerts = get_alert_settings()
                status = get_portfolio_status()
                total_status = status.get('통합', [])
                summary = get_portfolio_summary_stats(status).get('통합', {})
                
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
                        elif alert['ref_type'] == 'HIGH52':
                            ref_val = price_cache.get(ticker, {}).get('high52', 0)
                            ref_name = "52주 최고가"
                        elif alert['ref_type'] == 'FIXED':
                            ref_val = float(alert['value'])
                            ref_name = "지정가"

                        if ref_val <= 0: continue

                        # 조건 비교
                        if alert['condition'] == 'UP_PCT':
                            threshold = ref_val * (1 + float(alert['value'])/100)
                            if cur_price >= threshold:
                                msg = f"🔔 <b>[가격 알림]</b> {ticker}\n현재가({cur_price:,}원)가 {ref_name} 대비 {alert['value']}% 이상 상승했습니다."
                        elif alert['condition'] == 'DOWN_PCT':
                            threshold = ref_val * (1 - float(alert['value'])/100)
                            if cur_price <= threshold:
                                msg = f"🔔 <b>[가격 알림]</b> {ticker}\n현재가({cur_price:,}원)가 {ref_name} 대비 {alert['value']}% 이상 하락했습니다."

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

def get_portfolio_summary_stats(portfolio_status):
    """수익률 및 비중 오차 통계를 계산합니다."""
    summary_stats = {}
    total_net_deposit = 0
    
    account_deposits = {}
    for acc in ACCOUNTS:
        ledger = read_ledger(acc)
        acc_deposit = sum(float(str(r.get('cash_delta', 0)).replace(',','')) for r in ledger if r.get('type') == 'DEPOSIT')
        account_deposits[acc] = acc_deposit
        total_net_deposit += acc_deposit

    for acc in ['통합'] + ACCOUNTS:
        status_list = portfolio_status.get(acc, [])
        if not status_list: continue
            
        df_res = pd.DataFrame(status_list)
        acc_eval = df_res['평가금액'].sum()
        turnover = round(df_res['비중오차(%p)'].abs().sum() / 2, 2)
        
        deposit = total_net_deposit if acc == '통합' else account_deposits.get(acc, 0)
        roi = round(((acc_eval / deposit) - 1) * 100, 2) if deposit > 0 else 0
        summary_stats[acc] = {'deposit': deposit, 'eval': acc_eval, 'roi': roi, 'turnover': turnover}
        
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
    df['quantity'] = pd.to_numeric(df['quantity'], errors='coerce').fillna(0)
    df['price']    = pd.to_numeric(df['price'],    errors='coerce').fillna(0)
    df['amount']   = df['quantity'] * df['price']
    df['account']  = account
    return df


def get_portfolio_status():
    settings_data = get_portfolio_settings()

    # 전체 거래 DataFrame
    dfs   = [build_equity_df(acc) for acc in ACCOUNTS]
    dfs   = [d for d in dfs if not d.empty]
    df_all = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    # 1. 필요한 모든 고유 티커 추출
    held_tickers = set(df_all['ticker'].unique()) if not df_all.empty else set()
    target_tickers = {t for t, v in settings_data.items() if v['weight'] > 0 and v.get('is_active', True)}
    all_unique_tickers = {t for t in (held_tickers | target_tickers) if t and t not in ('-', 'NAN', 'nan', 'CASH')}

    # 2. 병렬로 현재가 가져오기 (성능 개선 핵심)
    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(get_current_price, all_unique_tickers)

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

            if not df_acc.empty:
                df_t     = df_acc[df_acc['ticker'] == ticker]
                qty_buy  = df_t[df_t['type'] == 'BUY' ]['quantity'].sum()
                qty_sell = df_t[df_t['type'] == 'SELL']['quantity'].sum()
                buy_sum  = df_t[df_t['type'] == 'BUY' ]['amount'].sum()
            else:
                qty_buy = qty_sell = buy_sum = 0

            current_qty = qty_buy - qty_sell
            if current_qty <= 0 and target_w <= 0:
                continue

            avg_price = buy_sum / qty_buy if qty_buy > 0 else 0
            cur_price = get_current_price(ticker) or avg_price

            grouped.append({
                '티커':        ticker,
                '종목명':      settings_data.get(ticker, {}).get('name', ticker),
                '목표비중(%)': target_w,
                '보유수량':    round(current_qty, 2),
                '평단가':      round(avg_price),
                '현재가':      cur_price,
                '매수금액':    round(current_qty * avg_price),
                '평가금액':    round(current_qty * cur_price),
                '손익률(%)':   round(((cur_price / avg_price) - 1) * 100, 2) if avg_price > 0 else 0,
            })

        # 현금 행
        cash_target = settings_data.get('CASH', {}).get('weight', 0)
        grouped.append({
            '티커': 'CASH', '종목명': '현금(예수금)', '목표비중(%)': cash_target,
            '보유수량': round(cash_balance), '평단가': 1, '현재가': 1,
            '매수금액': round(cash_balance), '평가금액': round(cash_balance), '손익률(%)': 0.0,
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
    portfolio_status = get_portfolio_status()
    exchange_data    = get_exchange_rates()

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
            elif alert.get('ref_type') == 'HIGH52':
                ref_val = price_cache.get(ticker, {}).get('high52', 0)
            
            if ref_val > 0:
                try:
                    val_f = float(alert.get('value', 0))
                    if alert.get('condition') == 'UP_PCT':
                        alert['target_price'] = ref_val * (1 + val_f / 100)
                    elif alert.get('condition') == 'DOWN_PCT':
                        alert['target_price'] = ref_val * (1 - val_f / 100)
                except: pass

    cash_balances    = {acc: get_cash_balance(acc) for acc in ACCOUNTS}
    summary_stats    = get_portfolio_summary_stats(portfolio_status)

    return render_template('index.html',
                           ledger_data=ledger_data,
                           settings_rows=settings_rows,
                           settings_data=settings_data,
                           status=portfolio_status,
                           summary_stats=summary_stats, # 추가됨
                           cash_balances=cash_balances,
                           exchange=exchange_data,
                           telegram_config=telegram_config,
                           alerts=alerts,
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
    ticker = request.form.get('set_ticker', '').strip()
    name   = request.form.get('set_name',   '').strip()
    cls    = request.form.get('set_class',  '').strip()
    show   = request.form.get('set_show', 'Y').strip() # 체크박스 값
    try:
        weight = float(request.form.get('set_weight', '0'))
    except ValueError:
        weight = 0.0
    if ticker and name:
        write_header = not os.path.exists(SETTINGS_FILE)
        with open(SETTINGS_FILE, 'a', encoding='utf-8', newline='') as f:
            w = csv.writer(f, delimiter='\t')
            if write_header:
                w.writerow(['티커', '종목명', '자산군', '목표비중', '표시'])
            w.writerow([ticker, name, cls, weight, show])
    return redirect(url_for('index') + '?tab=settings')


@app.route('/delete_setting/<int:row_idx>', methods=['POST'])
def delete_setting(row_idx):
    delete_setting_row(row_idx)
    return redirect(url_for('index') + '?tab=settings')

@app.route('/update_all_settings', methods=['POST'])
def update_all_settings():
    """설정 탭에서 체크박스 및 비중 변경 사항을 일괄 저장합니다."""
    tickers = request.form.getlist('ticker')
    names = request.form.getlist('name')
    classes = request.form.getlist('class')
    weights = request.form.getlist('weight')
    actives = request.form.getlist('active')  # 체크된 티커들의 리스트

    with open(SETTINGS_FILE, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['티커', '종목명', '자산군', '목표비중', '활성화'])
        for i in range(len(tickers)):
            t = tickers[i]
            is_active = 'Y' if t in actives else 'N'
            w.writerow([t, names[i], classes[i], weights[i], is_active])
            
    return redirect(url_for('index') + '?tab=settings')

@app.route('/update_telegram', methods=['POST'])
def update_telegram():
    token = request.form.get('token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    interval = request.form.get('interval', '3600').strip()
    enabled = 'Y' if request.form.get('enabled') == 'on' else 'N'
    save_telegram_config(token, chat_id, enabled, interval)
    return redirect(url_for('index') + '?tab=bot')

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
