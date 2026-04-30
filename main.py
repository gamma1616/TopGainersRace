from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import os
import time
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
import uvicorn

# 🌟 한국 표준시(KST) 처리
# Render 서버는 UTC로 동작하므로 명시적 시간대 변환이 필수입니다.
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except ImportError:
    # Python 3.8 이하 대비 폴백
    import pytz
    KST = pytz.timezone("Asia/Seoul")

# .env 파일에서 숨겨둔 API 키를 안전하게 불러옵니다.
load_dotenv()
app = FastAPI()

# ==========================================
# 1. 설정 및 변수 준비
# ==========================================
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

# 🌟 시장 및 섹터 정보
MARKET_NAMES = {"J": "코스피", "W": "코스닥", 0: "코스피", 1: "코스닥"}

# 🌟 정규장 시간 (KST)
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 0      # 09:00
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30  # 15:30

# 🌟 한국 증시 휴장일 (KRX 공식 공지 기준)
# 매년 KRX 발표 후 갱신 필요. 출처: https://www.krx.co.kr
KRX_HOLIDAYS = {
    # ===== 2026년 =====
    "2026-01-01",  # 신정
    "2026-02-16",  # 설날 연휴
    "2026-02-17",  # 설날
    "2026-02-18",  # 설날 연휴
    "2026-03-02",  # 삼일절 대체공휴일 (3/1 일요일)
    "2026-05-01",  # 근로자의 날
    "2026-05-05",  # 어린이날
    "2026-05-25",  # 부처님오신날 대체공휴일 (5/24 일요일)
    "2026-08-17",  # 광복절 대체공휴일 (8/15 토요일)
    "2026-09-24",  # 추석 연휴
    "2026-09-25",  # 추석
    # 9/26 토요일은 주말로 자동 처리됨
    "2026-10-05",  # 개천절 대체공휴일 (10/3 토요일)
    "2026-10-09",  # 한글날
    "2026-12-25",  # 성탄절
    "2026-12-31",  # 연말 휴장 (마지막 거래일은 12/30)
    # ===== 2027년 =====
    # 추후 KRX 공지 후 추가
}

# 주요 종목 섹터 매핑 (예시 데이터)
STOCK_SECTORS = {
    "알루코": "알루미늄", "송원산업": "화학", "문배철강": "철강",
    "한주에이알티": "2차전지/장비", "대호특수강": "철강", "세아메카닉스": "전기차/부품",
    "아주스틸": "철강", "나우IB": "창투사", "대호특수강우": "철강",
    "넥스틸": "철강", "포스코스틸리온": "철강", "디케이앤디": "의류/피혁",
    "금강철강": "철강", "삼성전자": "IT/반도체", "SK하이닉스": "반도체",
    "POSCO홀딩스": "철강/지주", "현대오토에버": "자율주행", "부국철강": "철강",
    "하이스틸": "철강", "세아제강": "철강", "유진투자증권": "증권", "SK증권": "증권",
    "에코프로": "2차전지", "에코프로비엠": "2차전지", "카카오": "IT서비스",
    "NAVER": "IT서비스", "현대차": "자동차", "기아": "자동차",
    "셀트리온": "바이오", "HLB": "바이오/제약"
}

def get_sector(name):
    return STOCK_SECTORS.get(name, "일반")

# 🌟 방문자 및 상한가 데이터 관리
VISITOR_FILE = "visitors.json"
LIMIT_UP_FILE = "limit_up.json"
visitor_stats = {"today": 0, "total": 0, "last_date": ""}

# 오늘의 상한가 명단 (실시간 데이터로 채워지며, 상한가 이탈 시 자동 제거됩니다)
limit_up_stocks = []

# ==========================================
# 1-1. 🌟 시장 시간 헬퍼 함수
# ==========================================
def kst_now():
    """현재 한국 시각(KST)을 반환합니다."""
    return datetime.now(KST)


def kst_today():
    """오늘 날짜를 KST 기준 'YYYY-MM-DD' 형식으로 반환합니다."""
    return kst_now().strftime("%Y-%m-%d")


def is_trading_day(dt):
    """주어진 datetime이 영업일(평일이면서 휴장일이 아닌 날)인지 확인합니다."""
    if dt.weekday() >= 5:  # 토(5), 일(6)
        return False
    if dt.strftime("%Y-%m-%d") in KRX_HOLIDAYS:
        return False
    return True


def next_market_open(from_dt):
    """주어진 시각 이후의 다음 정규장 개장 시각을 반환합니다."""
    candidate = from_dt.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN,
        second=0, microsecond=0
    )
    # 이미 오늘 09:00을 지났으면 다음 날부터 탐색
    if from_dt >= candidate:
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(
            hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN,
            second=0, microsecond=0
        )
    # 영업일을 만날 때까지 하루씩 건너뜀
    while not is_trading_day(candidate):
        candidate = candidate + timedelta(days=1)
    return candidate


def get_market_status():
    """
    현재 한국 정규 주식시장 상태를 반환합니다.
    Returns:
        dict: {
            "is_open": bool,
            "status": "open" | "closed_weekend" | "closed_holiday"
                     | "closed_before_hours" | "closed_after_hours",
            "now_kst": str,
            "next_open_kst": str,
            "message": str,
        }
    """
    now = kst_now()

    # 1. 비영업일(주말 / 휴장일) 체크
    if not is_trading_day(now):
        next_open = next_market_open(now)
        if now.weekday() >= 5:
            status, msg = "closed_weekend", "주말 휴장"
        else:
            status, msg = "closed_holiday", "휴장일"
        return {
            "is_open": False,
            "status": status,
            "now_kst": now.strftime("%Y-%m-%d %H:%M:%S"),
            "next_open_kst": next_open.strftime("%Y-%m-%d %H:%M"),
            "message": msg,
        }

    # 2. 영업일이지만 시간 외
    open_time = now.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN,
        second=0, microsecond=0
    )
    close_time = now.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN,
        second=0, microsecond=0
    )

    if now < open_time:
        return {
            "is_open": False,
            "status": "closed_before_hours",
            "now_kst": now.strftime("%Y-%m-%d %H:%M:%S"),
            "next_open_kst": open_time.strftime("%Y-%m-%d %H:%M"),
            "message": "장 시작 전",
        }

    if now >= close_time:
        next_open = next_market_open(now)
        return {
            "is_open": False,
            "status": "closed_after_hours",
            "now_kst": now.strftime("%Y-%m-%d %H:%M:%S"),
            "next_open_kst": next_open.strftime("%Y-%m-%d %H:%M"),
            "message": "장 마감",
        }

    # 3. 정규장 진행 중
    return {
        "is_open": True,
        "status": "open",
        "now_kst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "next_open_kst": "",
        "message": "정규장 진행중",
    }


# ==========================================
# 1-2. 데이터 파일 로드/저장 (KST 기준)
# ==========================================
def load_data():
    global visitor_stats, limit_up_stocks
    current_date = kst_today()
    # 방문자 데이터 로드
    if os.path.exists(VISITOR_FILE):
        try:
            with open(VISITOR_FILE, "r") as f:
                import json
                visitor_stats = json.load(f)
        except: pass
    # 상한가 데이터 로드
    if os.path.exists(LIMIT_UP_FILE):
        try:
            with open(LIMIT_UP_FILE, "r") as f:
                import json
                data = json.load(f)
                if data.get("date") == current_date:
                    file_stocks = data.get("stocks", [])
                    # 하드코딩된 명단과 파일의 명단을 합칩니다 (중복 제거)
                    existing_names = [s["name"] for s in limit_up_stocks]
                    for fs in file_stocks:
                        if fs["name"] not in existing_names:
                            limit_up_stocks.append(fs)
        except: pass


def save_data():
    import json
    with open(VISITOR_FILE, "w") as f:
        json.dump(visitor_stats, f)
    with open(LIMIT_UP_FILE, "w") as f:
        json.dump({"date": kst_today(), "stocks": limit_up_stocks}, f)


def update_visitors():
    global visitor_stats, limit_up_stocks
    current_date = kst_today()
    if visitor_stats["last_date"] != current_date:
        visitor_stats["today"] = 0
        visitor_stats["last_date"] = current_date
        limit_up_stocks = []  # 날짜 바뀌면 상한가 리스트도 초기화
    visitor_stats["today"] += 1
    visitor_stats["total"] += 1
    save_data()


load_data()

# 🌟 핵심 캐싱 변수
cached_data = []
last_fetched_at = ""  # 마지막 데이터 갱신 시각 (KST)
kis_token_info = {"access_token": "", "expires_at": 0}

# ==========================================
# 2. 한국투자증권 API 연동 로직
# ==========================================
def get_kis_token():
    """한국투자증권에 접속을 허락받는 '접근 토큰'을 발급받습니다."""
    if not KIS_APP_KEY or not KIS_APP_SECRET:
        return False
    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }
    try:
        res = requests.post(url, headers=headers, json=body)
        if res.status_code == 200:
            data = res.json()
            kis_token_info["access_token"] = data.get("access_token")
            # 만료시간 1분 전으로 넉넉하게 설정하여 미리 갱신되도록 함
            kis_token_info["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
            print("✅ [성공] 한투 API 접근 토큰 발급 완료!")
            return True
        else:
            print("❌ [실패] 한투 API 키를 확인해주세요:", res.text)
            return False
    except Exception as e:
        print("❌ [에러] 한투 서버 통신 중 문제 발생:", e)
        return False


def fetch_kis_data():
    """한국투자증권에서 등락률 상위 종목을 가져옵니다."""
    if not kis_token_info["access_token"] or time.time() > kis_token_info["expires_at"]:
        success = get_kis_token()
        if not success: return None

    all_data = []
    for market_code in ["J", "W"]:
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/fluctuation-rank"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {kis_token_info['access_token']}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": "FHPST01700000",
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": market_code,
            "FID_COND_SCR_DIV_CODE": "20170",
            "FID_INPT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_PRC_CLS_CODE": "1",
            "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_RSFL_RATE1": "",
            "FID_RSFL_RATE2": "",
            "FID_COND_MACR_TRPB": "0",
            "FID_COND_AMNT_BKDN": "0",
            "FID_OBJT_AQTV": "0",
            "FID_VRSS_TNRT_CBFR": "0"
        }
        try:
            res = requests.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data_json = res.json()
                if data_json.get("rt_cd") != "0":
                    print(f"❌ [API 응답 에러 - {market_code}]:", data_json.get("msg1"))
                    return None

                data_list = data_json.get("output", [])
                for item in data_list:
                    name = item.get("hts_kor_isnm", "")
                    rate = float(item.get("prdy_ctrt", 0))
                    price = int(item.get("stck_prpr", 0))

                    # ETF, ETN, 레버리지, 인버스, 선물, 스팩, 액티브 등 제외 필터링
                    exclude_keywords = [
                        "ETF", "ETN", "레버리지", "인버스", "선물", "스팩", "액티브",
                        "KODEX", "TIGER", "ACE", "HANARO", "KBSTAR", "SOL", "ARIRANG", "RISE", "RO",
                        "KOSEF", "TREX", "SMART", "FOCUS", "KINDEX", "GL", "TR", "USD", "IS"
                    ]
                    if any(kw in name.upper() for kw in exclude_keywords):
                        continue

                    if name and rate > 0:
                        all_data.append({
                            "name": name,
                            "rate": rate,
                            "price": price,
                            "market": MARKET_NAMES.get(market_code, ""),
                            "sector": get_sector(name)
                        })
            else:
                print(f"❌ [통신 실패 - {market_code}]: 상태코드 {res.status_code}, 내용: {res.text}")
                return None
        except Exception as e:
            print(f"❌ [예외 발생]: {e}")
            return None

    return all_data


def fetch_fallback_data():
    """
    [비상망 가동] 네이버 금융 웹 페이지를 직접 읽어 데이터를 가져옵니다.
    코스피(sosok=0)와 코스닥(sosok=1) 양쪽 모두 가져와서 합칩니다.
    """
    import re

    def crawl_naver_rise(sosok):
        try:
            url = f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}"
            res = requests.get(url, timeout=5)
            res.encoding = 'euc-kr'
            html = res.text

            rows = html.split('</tr>')
            items = []

            for row in rows:
                if 'class="tltle"' not in row:
                    continue
                name_match = re.search(r'class="tltle">([^<]+)</a>', row)
                if not name_match:
                    continue
                name = name_match.group(1).strip()

                exclude_keywords = [
                    "ETF", "ETN", "레버리지", "인버스", "선물", "스팩", "액티브",
                    "KODEX", "TIGER", "ACE", "HANARO", "KBSTAR", "SOL", "ARIRANG", "RISE", "RO",
                    "KOSEF", "TREX", "SMART", "FOCUS", "KINDEX", "GL", "TR", "USD", "IS"
                ]
                if any(kw in name.upper() for kw in exclude_keywords):
                    continue

                tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(tds) < 5:
                    continue

                rate = 0.0
                found_rate = False
                for i in range(3, min(len(tds), 6)):
                    rate_match = re.search(r'([-+]?[\d\.]+)%', tds[i])
                    if rate_match:
                        rate = float(rate_match.group(1))
                        found_rate = True
                        break

                if not found_rate or rate <= 0:
                    continue

                price = 0
                price_match = re.search(r'([\d,]+)', tds[2])
                if price_match:
                    price = int(price_match.group(1).replace(',', ''))

                items.append({
                    "name": name,
                    "rate": rate,
                    "price": price,
                    "market": MARKET_NAMES.get(sosok, ""),
                    "sector": get_sector(name)
                })
            return items
        except Exception as e:
            print(f"❌ [비상망 에러 - {'코스피' if sosok == 0 else '코스닥'}]: {e}")
            return []

    kospi_data = crawl_naver_rise(0)
    kosdaq_data = crawl_naver_rise(1)
    result = kospi_data + kosdaq_data

    if result:
        print(f"✅ [비상망] 네이버에서 코스피 {len(kospi_data)}개 + 코스닥 {len(kosdaq_data)}개 = 총 {len(result)}개 종목 (필터링 완료)")
    return result


# ==========================================
# 3. 🌟 서버 핵심 로직 (백그라운드 캐싱) - 정규장 시간에만 동작
# ==========================================
def update_cache_loop():
    global cached_data, limit_up_stocks, last_fetched_at

    last_logged_status = None  # 상태 변화 시점에만 로그 출력

    while True:
        market = get_market_status()

        # 🛑 정규장 시간이 아니면 데이터 갱신을 건너뜁니다.
        # cached_data는 마지막 마감 직전 스냅샷으로 그대로 유지됩니다.
        if not market["is_open"]:
            if last_logged_status != market["status"]:
                print(f"🛑 [장 외 시간] {market['message']} | 다음 개장: {market['next_open_kst']} | 데이터 갱신 일시 정지")
                last_logged_status = market["status"]
            time.sleep(30)  # 장 외엔 30초 간격으로 상태만 체크
            continue

        if last_logged_status != "open":
            print("🟢 [정규장 시작] 데이터 갱신을 재개합니다.")
            last_logged_status = "open"

        new_data = None
        if KIS_APP_KEY and KIS_APP_SECRET:
            new_data = fetch_kis_data()

        if not new_data or len(new_data) == 0:
            new_data = fetch_fallback_data()

        if new_data:
            new_data.sort(key=lambda x: x["rate"], reverse=True)

            # 🌟 상한가(29.9% 이상) 종목 실시간 갱신
            #    - 현재 상한가 종목만 명단 유지 (이탈 시 자동 제거)
            #    - 신규 진입 종목은 선착순 시각으로 추가
            current_time = kst_now().strftime("%H:%M:%S")
            currently_limit_up = {s["name"] for s in new_data if s["rate"] >= 29.9}
            previous_names = {s["name"] for s in limit_up_stocks}

            # 1. 상한가 이탈 종목 제거
            limit_up_stocks = [s for s in limit_up_stocks if s["name"] in currently_limit_up]

            # 2. 신규 상한가 종목 추가 (선착순 시각 기록)
            existing_names = {s["name"] for s in limit_up_stocks}
            for stock in new_data:
                if stock["rate"] >= 29.9 and stock["name"] not in existing_names:
                    limit_up_stocks.append({
                        "name": stock["name"],
                        "time": current_time,
                        "rate": stock["rate"],
                        "market": stock["market"],
                        "sector": stock["sector"]
                    })
                    existing_names.add(stock["name"])

            # 변경 사항이 있을 때만 파일 저장 (이탈 또는 신규 진입)
            new_names = {s["name"] for s in limit_up_stocks}
            if previous_names != new_names:
                save_data()

            # 🚫 상한가(29.9% 이상) 종목은 차트에서 제외 (명예의 전당에만 표시)
            filtered = [s for s in new_data if s["rate"] < 29.9]
            cached_data = filtered[:30]
            last_fetched_at = kst_now().strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(5)


@app.on_event("startup")
def startup_event():
    market = get_market_status()
    print("🚀 서버 가동 완료!")
    print(f"   현재 KST: {market['now_kst']}")
    print(f"   시장 상태: {market['message']}")
    if not market["is_open"]:
        print(f"   다음 개장: {market['next_open_kst']}")

    import threading
    thread = threading.Thread(target=update_cache_loop, daemon=True)
    thread.start()


# ==========================================
# 4. 화면 및 API 연결 고리
# ==========================================
@app.get("/api/data")
def api_get_data():
    market = get_market_status()
    base = {
        "visitors": visitor_stats,
        "limit_up": limit_up_stocks,
        "market": market,
        "last_fetched_at": last_fetched_at,
    }

    if cached_data:
        base["status"] = "success"
        base["data"] = cached_data
        return base
    else:
        base["status"] = "waiting" if market["is_open"] else "closed_no_data"
        base["data"] = []
        base["message"] = (
            "데이터를 준비 중입니다. 잠시만 기다려주세요."
            if market["is_open"]
            else "장 마감 상태입니다. 다음 개장 시 데이터가 업데이트됩니다."
        )
        return base


@app.get("/", response_class=HTMLResponse)
def read_root():
    # 메인 페이지 접속 시 방문자 수 증가
    update_visitors()
    file_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
