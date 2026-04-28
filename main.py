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
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443" # 실전투자 전용 주소

# 🌟 핵심 캐싱 변수: 수천 명이 동시 접속해도 이 메모리 안의 데이터만 나눠주어 서버를 보호합니다.
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
                    if name and rate > 0:
                        all_data.append({"name": name, "rate": rate, "price": price})
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
    공식 API가 막혀도 웹사이트가 떠 있는 한 작동하는 강력한 방식입니다.
    """
    try:
        import re
        url = "https://finance.naver.com/sise/sise_rise.naver"
        res = requests.get(url, timeout=5)
        res.encoding = 'euc-kr'
        html = res.text
        
        # 종목명과 등락률 추출
        names = re.findall(r'class="tltle">([^<]+)</a>', html)
        rates = re.findall(r'<span class="tah p11 (?:red01|nv01)">\s*([+-]?[\d\.]+)%\s*</span>', html)
        
        result = []
        for name, rate in zip(names, rates):
            result.append({
                "name": name,
                "rate": float(rate),
                "price": 0 # 웹에서는 가격 정보 파싱이 더 복잡하여 생략 (화면에는 영향 없음)
            })
        
        if result:
            print(f"✅ [비상망] 네이버에서 {len(result)}개 종목을 성공적으로 가져왔습니다.")
        return result
    except Exception as e:
        print(f"❌ [비상망 에러]: {e}")
    return []


# ==========================================
# 3. 🌟 서버 핵심 로직 (백그라운드 캐싱)
# ==========================================
def update_cache_loop():
    """
    서버가 켜지면 무한 루프를 돌면서 스스로 5초마다 최신 데이터를 가져옵니다.
    """
    global cached_data
    while True:
        new_data = None
        
        # 1. 한투 API 우선 시도
        if KIS_APP_KEY and KIS_APP_SECRET:
            new_data = fetch_kis_data()
            
        # 2. 한투 API 실패 시 비상망 가동
        if not new_data or len(new_data) == 0:
            new_data = fetch_fallback_data()
            
        if new_data:
            new_data.sort(key=lambda x: x["rate"], reverse=True)
            cached_data = new_data[:30]
            
        time.sleep(5)

@app.on_event("startup")
def startup_event():
    """서버 전원이 켜지자마자 백그라운드 캐싱 루프 스위치를 올립니다."""
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
        return {"status": "success", "data": cached_data}
    else:
        return {"status": "error", "message": "데이터를 준비 중입니다. 잠시만 기다려주세요."}

@app.get("/", response_class=HTMLResponse)
def read_root():
    file_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
