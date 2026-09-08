#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg AFK 广告挂机脚本 - 账号密码登录版
================================================
机制:    DrissionPage 驱动真实 Chrome 完成账号密码登录及 Turnstile 人机验证。
         登录成功后将 Cookie 交给 requests 进行纯 HTTP 高效挂机。
收益:    每广告实测均值 ~0.03/轮。每次启动固定执行 100 轮。
"""

import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import requests

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    print("缺少依赖，请先安装: pip install requests DrissionPage")
    sys.exit(1)

# ════════════════════════════════════════════════════════════════════
# 核心配置
# ════════════════════════════════════════════════════════════════════
BASE = "https://dash.neoheberg.fr"
LOGIN_URL = f"{BASE}/login"
ADS_URL = f"{BASE}/shop/ads"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neoheberg_state.json")

NH_IDENTIFIER = os.environ.get("NH_IDENTIFIER", "")
NH_PASSWORD   = os.environ.get("NH_PASSWORD", "")
NH_REMEMBER      = os.environ.get("NH_REMEMBER", "")       
NH_SESSION       = os.environ.get("NH_SESSION", "")        
NH_COOKIE_HEADER = os.environ.get("NH_COOKIE_HEADER", "")  
NH_PROXY         = os.environ.get("NH_PROXY", "")
NH_TG_BOT_TOKEN  = os.environ.get("NH_TG_BOT_TOKEN", "")
NH_TG_CHAT_ID    = os.environ.get("NH_TG_CHAT_ID", "")
NH_WAIT          = int(os.environ.get("NH_WAIT", "25"))     
NH_HEADLESS      = os.environ.get("NH_HEADLESS", "0") == "1"
NH_LOGIN_RETRIES = int(os.environ.get("NH_LOGIN_RETRIES", "3"))
NH_CHROME_PATH   = os.environ.get("NH_CHROME_PATH", "")
NH_UA            = os.environ.get("NH_UA", "")

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
SETTLE_SECONDS = 7   
RETRY_COOLDOWN = 30  
COOKIE_DOMAIN = "dash.neoheberg.fr"

# ════════════════════════════════════════════════════════════════════
# 日志 & TG 通知
# ════════════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("neoheberg-afk")

def send_tg(text: str) -> None:
    if not NH_TG_BOT_TOKEN or not NH_TG_CHAT_ID:
        return
    try:
        data = json.dumps({"chat_id": NH_TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{NH_TG_BOT_TOKEN}/sendMessage", data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log.warning("TG 通知失败: %s", e)

# ════════════════════════════════════════════════════════════════════
# 状态持久化
# ════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("start_balance", None)
    state.setdefault("last_balance", None)
    state.setdefault("total", 0.0)
    state.setdefault("last_report", 0)
    state.pop("rounds", None)  # 清理旧版轮次冗余
    return state

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def update_earnings(state: dict, balance: float) -> None:
    if state.get("start_balance") is None:
        state["start_balance"] = balance
    last = state.get("last_balance")
    if last is not None:
        delta = balance - last
        if delta > 0:
            state["total"] = round(state.get("total", 0.0) + delta, 6)
    state["last_balance"] = balance

# ════════════════════════════════════════════════════════════════════
# 会话 / Cookie 工具
# ════════════════════════════════════════════════════════════════════
def _parse_cookie_header(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out

def _is_login_page(r: requests.Response) -> bool:
    return "/login" in r.url or "Connexion" in r.text[:600].replace(" ", "")

def _cf_blocked(r: requests.Response) -> bool:
    head = r.text[:4000].lower()
    if r.status_code in (403, 429, 503):
        return True
    marks = ("just a moment", "attention required", "challenge-platform", "cf-chl", "checking your browser")
    return any(k in head for k in marks)

def _page_title(r: requests.Response) -> str:
    m = re.search(r"<title>([^<]{0,120})", r.text, re.I)
    return m.group(1).strip() if m else "(无标题)"

def _cookie_login_ok(s: requests.Session, verbose: bool = False) -> bool:
    try:
        r = s.get(ADS_URL, timeout=20)
    except Exception as e:
        log.warning("访问站点异常: %s", e)
        return False
    if _cf_blocked(r):
        log.warning("HTTP 层被拦截 (status=%s | title=%s)", r.status_code, _page_title(r))
        return False
    ok = not _is_login_page(r)
    return ok

def _make_session(ua: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": ua})
    if NH_PROXY:
        px = NH_PROXY
        if px.startswith("socks5://"):
            px = "socks5h://" + px[len("socks5://"):]
        s.proxies.update({"http": px, "https": px})
    return s

def session_from_env() -> requests.Session:
    s = _make_session(NH_UA or DEFAULT_UA)
    if NH_COOKIE_HEADER:
        for k, v in _parse_cookie_header(NH_COOKIE_HEADER).items():
            s.cookies.set(k, v, domain=COOKIE_DOMAIN, path="/", secure=True)
        return s
    if NH_REMEMBER:
        s.cookies.set("__Host-NH-Remember", NH_REMEMBER, domain=COOKIE_DOMAIN, path="/", secure=True)
    if NH_SESSION:
        s.cookies.set("__Host-NH", NH_SESSION, domain=COOKIE_DOMAIN, path="/", secure=True)
    return s

def _normalize_cookies(raw) -> list:
    out = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            out.append({"name": k, "value": v, "domain": COOKIE_DOMAIN, "path": "/"})
    else:
        for c in raw:
            try:
                out.append(dict(c))
            except Exception:
                pass
    return out

def _apply_raw_cookies(s: requests.Session, raw_cookies) -> None:
    for c in _normalize_cookies(raw_cookies):
        dom = c.get("domain") or COOKIE_DOMAIN
        if "neoheberg" not in dom:
            continue
        try:
            s.cookies.set(c.get("name"), c.get("value") or "", domain=dom, path=c.get("path") or "/", secure=True)
        except Exception:
            pass

def session_from_browser(raw_cookies, ua: str) -> requests.Session:
    s = _make_session(ua)
    _apply_raw_cookies(s, raw_cookies)
    return s

# ════════════════════════════════════════════════════════════════════
# 浏览器登录（DrissionPage）
# ════════════════════════════════════════════════════════════════════
def _build_chromium_options():
    co = ChromiumOptions()
    co.auto_port()
    path = NH_CHROME_PATH
    if not path:
        for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                break
    if path:
        co.set_browser_path(path)
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,900", "--disable-blink-features=AutomationControlled"):
        co.set_argument(arg)
    if NH_HEADLESS:
        co.set_argument("--headless=new")
    if NH_PROXY:
        if NH_PROXY.startswith("socks5"):
            co.set_argument(f"--proxy-server={NH_PROXY}")
            co.set_argument("--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1")
        else:
            co.set_proxy(NH_PROXY)
    return co

def _turnstile_click(page) -> bool:
    try:
        iframe = page.get_frame('css:iframe[src^="https://challenges.cloudflare.com"]', timeout=5)
    except Exception:
        return False
    if not iframe:
        return False
    time.sleep(2)
    try:
        sr = iframe.ele('tag:body').shadow_root
        if sr:
            target = sr.ele('css:input[type="checkbox"]') or sr.ele('css:div.main-wrapper')
            if target:
                target.click.at(offset_x=10, offset_y=10)
                return True
    except Exception:
        pass
    try:
        iframe.frame_ele.click.at(offset_x=25, offset_y=30)
        return True
    except Exception:
        return False

def _turnstile_token(page) -> bool:
    for _ in range(3):
        _turnstile_click(page)
        for _ in range(15):
            time.sleep(1)
            try:
                resp = page.ele('css:[name="cf-turnstile-response"]', timeout=1)
                if resp and len(resp.value) > 10:
                    return True
            except Exception:
                pass
    return False

def _wait_login_form(page, timeout_total: int = 60) -> bool:
    deadline = time.time() + timeout_total
    while time.time() < deadline:
        if page.ele('css:#identifier', timeout=3):
            return True
        _turnstile_click(page)
        time.sleep(3)
    return bool(page.ele('css:#identifier', timeout=3))

def _check_remember_box(page) -> None:
    try:
        lbl, target_input = None, None
        try:
            for el in page.eles('tag:label', timeout=2):
                if 'souvenir' in (el.text or '').lower():
                    lbl = el
                    break
        except Exception:
            lbl = None
        if lbl is not None:
            try:
                fid = lbl.attr('for')
                if fid:
                    target_input = page.ele(f'css:#{fid}', timeout=1)
            except Exception:
                target_input = None
        
        if target_input and bool(target_input.states.is_checked):
            return
        if lbl is not None:
            lbl.click()
            return
        cb = page.ele('css:input[name="remember"]', timeout=1) or page.ele('css:#remember', timeout=1)
        if cb:
            cb.click()
    except Exception:
        pass

def _fill_and_submit(page) -> None:
    idf = page.ele('css:#identifier', timeout=5)
    if not idf:
        raise RuntimeError("第一步：未找到用户名输入框")
    idf.clear()
    idf.input(NH_IDENTIFIER)
    
    continue_btn = page.ele('css:#goToPassword', timeout=5)
    if not continue_btn:
        raise RuntimeError("第一步：未找到 Continuer 按钮")
    continue_btn.click()
    time.sleep(1.5)

    pwd = page.ele('css:#password', timeout=5)
    if not pwd:
        raise RuntimeError("第二步：未找到密码输入框")
    pwd.clear()
    pwd.input(NH_PASSWORD)
    
    _check_remember_box(page)
    if not _turnstile_token(page):
        raise RuntimeError("Turnstile 未取到 token")
        
    btn = page.ele('css:button[type="submit"]', timeout=5)
    if not btn:
        raise RuntimeError("提交按钮未找到")
    btn.click()

def _wait_after_submit(page, timeout: int = 30):
    stable_at = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        if "/login" in (page.url or ""):
            stable_at = None
            continue
        if stable_at is None:
            stable_at = time.time()
        elif time.time() - stable_at >= 3:
            time.sleep(1)
            return True, ""
    return False, ""

def _browser_check_ads(page, timeout_total: int = 30) -> bool:
    page.get(ADS_URL)
    deadline = time.time() + timeout_total
    while time.time() < deadline:
        if page.ele('css:#identifier', timeout=3):
            return False
        if _turnstile_click(page):
            time.sleep(3)
            continue
        time.sleep(1)
        if not page.ele('css:#identifier', timeout=2):
            return True
    return False

def browser_login():
    for attempt in range(1, NH_LOGIN_RETRIES + 1):
        page = None
        try:
            log.info("🌐 浏览器登录尝试 %d/%d ...", attempt, NH_LOGIN_RETRIES)
            page = ChromiumPage(_build_chromium_options())
            page.get(LOGIN_URL)
            if not _wait_login_form(page):
                raise RuntimeError("登录表单未出现（疑似被 Cloudflare 拦截）")
            _fill_and_submit(page)
            ok, _ = _wait_after_submit(page)
            if not ok:
                raise RuntimeError("提交后仍停留在登录页")
            log.info("登录落地页: %s | 标题: %s", page.url, page.title)
            
            if not _browser_check_ads(page):
                raise RuntimeError("访问 ads 被踢回登录页，登录未生效")
                
            raw = _normalize_cookies(page.cookies())
            if not any(c.get("name") == "__Host-NH" for c in raw):
                raise RuntimeError("未见 __Host-NH cookie")
            ua = page.user_agent or DEFAULT_UA
            log.info("✅ 浏览器登录成功")
            return raw, ua
        except Exception as e:
            log.warning("浏览器登录失败: %s", e)
            time.sleep(8)
        finally:
            if page:
                page.quit()
    return None, ""

# ════════════════════════════════════════════════════════════════════
# HTTP 业务
# ════════════════════════════════════════════════════════════════════
def ensure_login(s: requests.Session, force_browser: bool = False) -> bool:
    if not force_browser and _cookie_login_ok(s):
        log.info("✅ 现有 cookie 会话仍有效")
        return True

    if not (NH_IDENTIFIER and NH_PASSWORD):
        log.error("未配置账号密码，无法启动浏览器重新登录")
        return False

    log.info("启动浏览器进行硬登录 ...")
    raw, ua = browser_login()
    if not raw:
        return False
    s.headers.update({"User-Agent": ua})
    s.cookies.clear()
    _apply_raw_cookies(s, raw)
    if _cookie_login_ok(s, verbose=True):
        log.info("✅ 重新登录成功")
        return True
    return False

def _get_balance(s: requests.Session) -> float:
    r = s.get(ADS_URL, timeout=20)
    if _is_login_page(r):
        raise PermissionError("会话已失效")
    
    m = re.search(r'([\d.,]+)\s*(?:</[^>]+>\s*)?coins', r.text, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"未找到余额 (status={r.status_code})")
    return float(m.group(1).replace(',', '').strip())

def _get_csrf(s: requests.Session) -> str:
    r = s.get(ADS_URL, timeout=20)
    if _is_login_page(r):
        raise PermissionError("会话已失效")
        
    m = re.search(r'name="csrf[_-]token" (?:content|value)="([a-f0-9]+)"', r.text, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"未找到 csrf token (status={r.status_code})")
    return m.group(1)

def gen_callback(s: requests.Session, csrf: str) -> str | None:
    r = s.post(ADS_URL, data={"csrf_token": csrf, "ads_action": "toggle"}, allow_redirects=False, timeout=20)
    loc = r.headers.get("Location")
    if not loc:
        return None
    m = re.search(r"url=([^&]+)", loc)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1))

def redeem(s: requests.Session, callback: str) -> int:
    r = s.get(callback, headers={"Referer": "https://clipurl.fr/"}, timeout=20)
    return r.status_code

def report(state: dict, balance: float, current_round: int = 0, force: bool = False) -> None:
    now = time.time()
    if not force and now - state.get("last_report", 0) < 3600:
        return
    state["last_report"] = now
    save_state(state)
    total = state.get("total", 0.0)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"🪄 NeoHeberg AFK 已连接\n📅 {ts}\n\n💰 <b>余额</b>: {balance:.4f} 🪙\n📈 <b>历史累计收益</b>: +{total:.4f} 🪙（本次已跑 {current_round}/100 轮）"
    send_tg(msg)
    log.info("TG 报告: 余额=%s 历史累计收益=%s (本次进度: %s/100)", balance, total, current_round)

# ════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════
def main() -> None:
    login_only = "--login-only" in sys.argv
    has_credentials = bool(NH_IDENTIFIER and NH_PASSWORD)
    has_env_cookie = bool(NH_REMEMBER or NH_SESSION or NH_COOKIE_HEADER)
    
    if not has_credentials and not has_env_cookie:
        log.error("缺少凭据：请设置环境变量")
        sys.exit(1)

    # 阶段一：初次登录验证
    s = session_from_env() if has_env_cookie else None
    if s and _cookie_login_ok(s):
        log.info("✅ 环境变量 Cookie 有效，跳过浏览器")
    else:
        if not has_credentials:
            log.error("Cookie 失效且无账号密码")
            sys.exit(1)
        raw, ua = browser_login()
        if not raw:
            log.error("浏览器初次登录失败")
            sys.exit(1)
        s = session_from_browser(raw, ua)
        if not _cookie_login_ok(s):
            log.error("请求层会话验证失败")
            sys.exit(1)

    # 初始化运行状态
    state = load_state()
    try:
        bal = _get_balance(s)
        update_earnings(state, bal)
        if login_only:
            log.info("🧪 测试验证通过")
            sys.exit(0)
        report(state, bal, force=True)
    except Exception as e:
        log.error("启动获取余额失败: %s", e)
        sys.exit(1)

    # 阶段二：核心赚币循环
    TARGET_ROUNDS = 100
    current_round = 0
    consecutive_fail = 0
    relogin_cycles = 0  # 追踪浏览器重启次数，上限 1 次

    while True:
        if current_round >= TARGET_ROUNDS:
            log.info("🎯 本次运行完成 %d 轮，正常退出", TARGET_ROUNDS)
            send_tg(f"✅ NeoHeberg 任务完成，已跑满 {TARGET_ROUNDS} 轮。")
            break

        try:
            csrf = _get_csrf(s)
            cb = gen_callback(s, csrf)
            if not cb:
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    raise RuntimeError("生成回调连续失败达到 3 次")
                time.sleep(20)
                continue
            
            time.sleep(NH_WAIT)
            st = redeem(s, cb)
            time.sleep(SETTLE_SECONDS)
            
            current_round += 1
            consecutive_fail = 0
            relogin_cycles = 0  # 只要成功一次，就重置重启名额
            log.info("第 %d/%d 轮完成 (HTTP %s)", current_round, TARGET_ROUNDS, st)

        except PermissionError as e:
            log.error("HTTP 响应提示会话被踢: %s", e)
            if ensure_login(s):
                continue
            send_tg("❌ NeoHeberg Cookie 失效且自动重登失败，程序退出。")
            sys.exit(1)

        except Exception as e:
            log.warning("业务逻辑异常: %s", e)
            consecutive_fail += 1
            if consecutive_fail >= 3:
                # 若已经重启过 1 次浏览器，且依然连续失败 3 次，立刻退出
                if relogin_cycles >= 1:
                    log.error("连续失败且已重置过浏览器，直接报错退出。")
                    send_tg("❌ NeoHeberg 连续异常，重启浏览器无效，程序已退出。")
                    sys.exit(1)
                
                # 若未重启过浏览器，执行唯一一次强制重登
                log.info("连续失败 3 次，执行强制浏览器重启 ...")
                relogin_cycles += 1
                if ensure_login(s, force_browser=True):
                    consecutive_fail = 0
                    continue
                else:
                    log.error("强制重启浏览器失败，直接退出。")
                    send_tg("❌ NeoHeberg 重启浏览器失败，程序已退出。")
                    sys.exit(1)
            else:
                time.sleep(RETRY_COOLDOWN)

        # 状态持久化
        try:
            bal = _get_balance(s)
            update_earnings(state, bal)
            report(state, bal, current_round)
            save_state(state)
        except Exception:
            pass

if __name__ == "__main__":
    main()
