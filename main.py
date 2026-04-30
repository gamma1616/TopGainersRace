from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
import os
import time
import asyncio
from dotenv import load_dotenv
import uvicorn

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
# 사용자 요청으로 오늘의 상한가 명단 13개를 고정값으로 시작합니다.
limit_up_stocks = [
    {"name": "알루코", "time": "09:05:12", "rate": 30.0, "market": "코스피", "sector": "알루미늄"},
    {"name": "송원산업", "time": "09:12:45", "rate": 30.0, "market": "코스피", "sector": "화학"},
    {"name": "문배철강", "time": "09:20:10", "rate": 30.0, "market": "코스피", "sector": "철강"},
    {"name": "한주에이알티", "time": "09:35:22", "rate": 30.0, "market": "코스닥", "sector": "2차전지/장비"},
    {"name": "대호특수강", "time": "09:48:15", "rate": 30.0, "market": "코스피", "sector": "철강"},
    {"name": "세아메카닉스", "time": "10:05:30", "rate": 30.0, "market": "코스닥", "sector": "전기차/부품"},
    {"name": "아주스틸", "time": "10:15:40", "rate": 30.0, "market": "코스피", "sector": "철강"},
    {"name": "나우IB", "time": "10:42:11", "rate": 30.0, "market": "코스닥", "sector": "창투사"},
    {"name": "대호특수강우", "time": "11:02:55", "rate": 30.0, "market": "코스피", "sector": "철강"},
    {"name": "넥스틸", "time": "11:25:34", "rate": 30.0, "market": "코스피", "sector": "철강"},
    {"name": "포스코스틸리온", "time": "13:10:20", "rate": 30.0, "market": "코스피", "sector": "철강"},
    {"name": "디케이앤디", "time": "14:05:15", "rate": 30.0, "market": "코스닥", "sector": "의류/피혁"},
    {"name": "금강철강", "time": "14:50:40", "rate": 30.0, "market": "코스피", "sector": "철강"}
]

def load_data():
    global visitor_stats, limit_up_stocks
    current_date = time.strftime("%Y-%m-%d")
    
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
        json.dump({"date": time.strftime("%Y-%m-%d"), "stocks": limit_up_stocks}, f)

def update_visitors():
    global visitor_stats, limit_up_stocks
    current_date = time.strftime("%Y-%m-%d")
    
    if visitor_stats["last_date"] != current_date:
        visitor_stats["today"] = 0
        visitor_stats["last_date"] = current_date
        limit_up_stocks = [] # 날짜 바뀌면 상한가 리스트도 초기화
    
    visitor_stats["today"] += 1
    visitor_stats["total"] += 1
    save_data()

load_data()

# 🌟 핵심 캐싱 변수
cached_data = []
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
                    
                    # ETF, ETN, 레버리지, 인버스, 선물, 스팩, 액티브 등 제외 필터링 (가독성 및 순수 주식 랭킹 목적)
                    exclude_keywords = ["ETF", "ETN", "레버리지", "인버스", "선물", "스팩", "액티브", "KODEX", "TIGER", "ACE", "HANARO", "KBSTAR", "SOL", "ARIRANG", "RISE", "RO"]
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
    각 행(TR)에서 종목명과 등락률을 함께 추출하여 매칭 오류를 방지합니다.
    """
    import re
    
    def crawl_naver_rise(sosok):
        """네이버 금융 상승 종목 페이지를 크롤링합니다.
        sosok: 0=코스피, 1=코스닥
        """
        try:
            url = f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}"
            res = requests.get(url, timeout=5)
            res.encoding = 'euc-kr'
            html = res.text
            
            # 행(TR) 단위로 분할하여 개별적으로 파싱
            rows = html.split('</tr>')
            
            items = []
            for row in rows:
                if 'class="tltle"' not in row:
                    continue
                    
                name_match = re.search(r'class="tltle">([^<]+)</a>', row)
                if not name_match:
                    continue
                
                name = name_match.group(1).strip()
                
                # ETF, ETN, 레버리지, 인버스, 선물, 스팩, 액티브 등 제외 필터링 (순수 주식 랭킹 목적)
                exclude_keywords = ["ETF", "ETN", "레버리지", "인버스", "선물", "스팩", "액티브", "KODEX", "TIGER", "ACE", "HANARO", "KBSTAR", "SOL", "ARIRANG", "RISE", "RO"]
                if any(kw in name.upper() for kw in exclude_keywords):
                    continue
                
                # TD 데이터 추출
                tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(tds) < 5:
                    continue
                
                # 등락률 추출 (보통 4-5번째 TD에 위치함, % 기호 기준 검색)
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
                
                # 현재가 추출 (숫자만 추출)
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
    
    # 코스피(0)와 코스닥(1) 양쪽 모두 크롤링하여 합침
    kospi_data = crawl_naver_rise(0)
    kosdaq_data = crawl_naver_rise(1)
    result = kospi_data + kosdaq_data
    
    if result:
        print(f"✅ [비상망] 네이버에서 코스피 {len(kospi_data)}개 + 코스닥 {len(kosdaq_data)}개 = 총 {len(result)}개 종목 (필터링 완료)")
    return result


# ==========================================
# 3. 🌟 서버 핵심 로직 (백그라운드 캐싱)
# ==========================================
def update_cache_loop():
    global cached_data, limit_up_stocks
    while True:
        new_data = None
        if KIS_APP_KEY and KIS_APP_SECRET:
            new_data = fetch_kis_data()
            
        if not new_data or len(new_data) == 0:
            new_data = fetch_fallback_data()
            
        if new_data:
            new_data.sort(key=lambda x: x["rate"], reverse=True)
            
            # 🌟 상한가(29.9% 이상) 종목 감지 및 선착순 기록
            current_time = time.strftime("%H:%M:%S")
            limit_up_names = [s["name"] for s in limit_up_stocks]
            
            changed = False
            for stock in new_data:
                if stock["rate"] >= 29.9 and stock["name"] not in limit_up_names:
                    limit_up_stocks.append({
                        "name": stock["name"],
                        "time": current_time,
                        "rate": stock["rate"],
                        "market": stock["market"],
                        "sector": stock["sector"]
                    })
                    limit_up_names.append(stock["name"])
                    changed = True
            
            if changed:
                save_data()
            
            # 🚫 상한가(29.9% 이상) 종목은 차트에서 제외 (명예의 전당에만 표시)
            filtered = [s for s in new_data if s["rate"] < 29.9]
            cached_data = filtered[:30]
            
        time.sleep(5)

@app.on_event("startup")
def startup_event():
    print("🚀 서버 가동 완료! 데이터를 수집하기 시작합니다.")
    import threading
    thread = threading.Thread(target=update_cache_loop, daemon=True)
    thread.start()


# ==========================================
# 4. 화면 및 API 연결 고리
# ==========================================
@app.get("/api/data")
def api_get_data():
    if cached_data:
        return {
            "status": "success", 
            "data": cached_data,
            "visitors": visitor_stats,
            "limit_up": limit_up_stocks # 상한가 명예의 전당 데이터 추가
        }
    else:
        return {"status": "error", "message": "데이터를 준비 중입니다. 잠시만 기다려주세요."}

@app.get("/", response_class=HTMLResponse)
def read_root():
    # 메인 페이지 접속 시 방문자 수 증가
    update_visitors()
    
    file_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
