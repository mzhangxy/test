#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg AFK 广告挂机脚本 - 账号密码登录版（GitHub Actions 适配）
================================================
站点:    https://dash.neoheberg.fr/shop/ads.php  （免费游戏/网页托管面板看广告赚 coins）
登录:    https://dash.neoheberg.fr/login （表单带 Cloudflare Turnstile 人机验证）
机制:    DrissionPage 驱动真实 Chrome 完成账号密码登录（含 Turnstile 打勾，
         旧版 cf_turnstile_solver.py 的过盾逻辑已内置本文件），
         登录成功后把 cookie + UA 交给 requests，随后完全复用原纯 HTTP 赚币逻辑：
         POST csrf_token → 302 Location 内嵌回调 URL → 直接 GET 回调即发币
         （浏览器只负责登录这一步，赚币全程无浏览器，快且稳）

流程:
  阶段一 登录（仅启动时 / 会话过期时执行）:
    1. 环境变量带 cookie（NH_REMEMBER 等）→ 先试免浏览器快速通道
    2. 快速通道失效 → 浏览器打开 /login:
       填 identifier / password → 勾选 Se souvenir de moi(30 天)
       → 过 Turnstile → 点 Se connecter → 校验跳转（要求稳定离开 /login）
       → 浏览器内实勘 ads.php 确认登录态真实生效 → 导出 cookie → 关浏览器
    3. 浏览器登录后 UA 与 cookie 一并交给 requests（保证 cf_clearance 口径一致）
  阶段二 赚币（每轮 ~35s, 无冷却）:
    1. GET  /shop/ads.php              → 提取 csrf_token + 当前余额
    2. POST /shop/ads.php              → 302, Location 含 url=<回调URL>
    3. sleep NH_WAIT                   （模拟广告观看时长）
    4. GET  <回调URL>(Referer=clipurl.fr) → 金币到账（入账延迟 2~7s）
收益:  每广告 0.0005~0.05 coins（实测均值 ~0.03/轮）。跑满约 0.3~0.5 coins/小时。

────────────────────────────────────────────────────────────────────────
【GitHub Actions Secrets】
────────────────────────────────────────────────────────────────────────
  NH_IDENTIFIER        登录邮箱或用户名（必填，主登录通道）
  NH_PASSWORD          登录密码（必填）
  NH_REMEMBER          可选。__Host-NH-Remember 的值，仍有效时跳过浏览器登录
  NH_TG_BOT_TOKEN      可选。Telegram 通知 Bot Token
  NH_TG_CHAT_ID        可选。Telegram 接收 ID
  NH_PROXY             可选。http://[user:pass@]ip:port，浏览器登录与 HTTP 挂机
                       走同一代理（Turnstile 在机房 IP 上过不去时的应对手段）

────────────────────────────────────────────────────────────────────────
【本地运行 / 调试】
────────────────────────────────────────────────────────────────────────
  pip install requests DrissionPage
  export NH_IDENTIFIER="邮箱或用户名" NH_PASSWORD="密码"
  python3 neoheberg_afk.py                # 登录 + 挂机
  python3 neoheberg_afk.py --login-only   # 只验证登录并打印余额，不挂机

其他可选环境变量:
  NH_WAIT=25            每轮等待秒数（模拟广告时长）
  NH_MAX_ROUNDS=500     最大轮次（0 不限）
  NH_UA="..."           requests 阶段 UA（仅 cookie 快速通道使用；
                        浏览器登录通道固定跟随浏览器自身 UA，保证口径一致）
  NH_HEADLESS=1         无头模式（过盾成功率低，仅调试用）
  NH_LOGIN_RETRIES=3    浏览器登录尝试次数
  NH_CHROME_PATH=...    手动指定 Chrome 路径（默认自动探测）
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
# 配置（全部来自环境变量，无硬编敏感信息）
# ════════════════════════════════════════════════════════════════════

BASE = "https://dash.neoheberg.fr"
LOGIN_URL = f"{BASE}/login"
ADS_URL = f"{BASE}/shop/ads.php"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neoheberg_state.json")

# 账号密码（主登录通道）
NH_IDENTIFIER = os.environ.get("NH_IDENTIFIER", "")
NH_PASSWORD   = os.environ.get("NH_PASSWORD", "")

# 可选 cookie 快速通道（仍有效时跳过浏览器登录）
NH_REMEMBER      = os.environ.get("NH_REMEMBER", "")       # __Host-NH-Remember cookie 值
NH_SESSION       = os.environ.get("NH_SESSION", "")        # __Host-NH cookie 值（过渡兜底）
NH_COOKIE_HEADER = os.environ.get("NH_COOKIE_HEADER", "")  # 或直接整条 Cookie 头

# 可选代理：浏览器登录与 HTTP 挂机走同一出口，保证 IP 一致
NH_PROXY = os.environ.get("NH_PROXY", "")

NH_TG_BOT_TOKEN  = os.environ.get("NH_TG_BOT_TOKEN", "")
NH_TG_CHAT_ID    = os.environ.get("NH_TG_CHAT_ID", "")
NH_WAIT          = int(os.environ.get("NH_WAIT", "25"))     # 每轮广告等待秒数
NH_MAX_ROUNDS    = int(os.environ.get("NH_MAX_ROUNDS", "300"))  # 最大执行轮次，0 为无限制
NH_HEADLESS      = os.environ.get("NH_HEADLESS", "0") == "1"
NH_LOGIN_RETRIES = int(os.environ.get("NH_LOGIN_RETRIES", "3"))
NH_CHROME_PATH   = os.environ.get("NH_CHROME_PATH", "")
NH_UA            = os.environ.get("NH_UA", "")  # 留空则快速通道用默认桌面 Chrome UA

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

SETTLE_SECONDS = 7   # 回调后等待入账秒数
RETRY_COOLDOWN = 30  # 出错后重试冷却

# ════════════════════════════════════════════════════════════════════
# 日志
# ════════════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("neoheberg-afk")

# ════════════════════════════════════════════════════════════════════
# Telegram 通知
# ════════════════════════════════════════════════════════════════════
def send_tg(text: str) -> None:
    if not NH_TG_BOT_TOKEN or not NH_TG_CHAT_ID:
        return
    try:
        data = json.dumps({"chat_id": NH_TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{NH_TG_BOT_TOKEN}/sendMessage",
                                      data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log.warning("TG 通知失败: %s", e)

# ════════════════════════════════════════════════════════════════════
# 状态持久化（注意：绝不把 cookie 写进 state 文件——它会被提交回仓库）
# ════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    # 补齐缺失字段，兼容旧版状态文件
    state.setdefault("start_balance", None)
    state.setdefault("last_balance", None)
    state.setdefault("total", 0.0)
    state.setdefault("rounds", 0)
    state.setdefault("last_report", 0)
    return state

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def update_earnings(state: dict, balance: float) -> None:
    """维护 total（累计收益）与 last_balance（上次已知余额）。
    只累加正向差值，避免余额被消费/提现导致的下降污染累计统计。"""
    if state.get("start_balance") is None:
        state["start_balance"] = balance
    last = state.get("last_balance")
    if last is not None:
        delta = balance - last
        if delta > 0:
            state["total"] = round(state.get("total", 0.0) + delta, 6)
    state["last_balance"] = balance

# ════════════════════════════════════════════════════════════════════
# 会话 / cookie 工具
# ════════════════════════════════════════════════════════════════════
COOKIE_DOMAIN = "dash.neoheberg.fr"

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
    """判断响应是否为 Cloudflare 拦截页/站点封锁页（200 状态的挑战页也算）。"""
    head = r.text[:4000].lower()
    if r.status_code in (403, 429, 503):
        return True
    marks = ("just a moment", "attention required", "challenge-platform",
             "cf-chl", "checking your browser", "enable javascript and cookies")
    return any(k in head for k in marks)

def _page_title(r: requests.Response) -> str:
    m = re.search(r"<title>([^<]{0,120})", r.text, re.I)
    return m.group(1).strip() if m else "(无标题)"

def _cookie_login_ok(s: requests.Session, verbose: bool = False) -> bool:
    """用现有 cookie 访问一次受保护页面，判断会话是否仍有效。"""
    try:
        r = s.get(ADS_URL, timeout=20)
    except Exception as e:
        log.warning("访问站点异常: %s", e)
        return False
    if _cf_blocked(r):
        log.warning("HTTP 层被 Cloudflare/站点拦截 (status=%s | title=%s)",
                    r.status_code, _page_title(r))
        return False
    ok = not _is_login_page(r)
    if not ok and verbose:
        # 只记名字不记值，cookie 值绝不入日志（Actions 日志可能公开）
        sent = [c.name for c in s.cookies if "neoheberg" in (c.domain or "")]
        log.error("会话验证失败: status=%s | 最终URL=%s | 重定向链=%s | 携带cookie=%s",
                  r.status_code, r.url, [h.status_code for h in r.history], sent or "无")
    return ok

def _make_session(ua: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": ua})
    if NH_PROXY:
        px = NH_PROXY
        if px.startswith("socks5://"):  # requests 走 socks5h：DNS 也从代理出去
            px = "socks5h://" + px[len("socks5://"):]
        s.proxies.update({"http": px, "https": px})
    return s

def session_from_env() -> requests.Session:
    """用环境变量里的旧 cookie 组装会话（快速通道）。
    服务端会通过 Set-Cookie 自动签发新的 __Host-NH，由 requests 自动接管。"""
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
    """兼容新版（list[dict]）/旧版（dict）DrissionPage 的 cookies() 返回。"""
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
    """把浏览器导出的 cookie 全量搬进 requests 会话
    （含 __Host-NH / __Host-NH-Remember / __cf_bm / cf_clearance 等）。"""
    for c in _normalize_cookies(raw_cookies):
        dom = c.get("domain") or COOKIE_DOMAIN
        if "neoheberg" not in dom:
            continue
        try:
            s.cookies.set(c.get("name"), c.get("value") or "",
                          domain=dom, path=c.get("path") or "/", secure=True)
        except Exception:
            pass

def session_from_browser(raw_cookies, ua: str) -> requests.Session:
    s = _make_session(ua)
    _apply_raw_cookies(s, raw_cookies)
    return s

# ════════════════════════════════════════════════════════════════════
# 浏览器登录（DrissionPage）：只为通过 /login 的 Turnstile，拿到 cookie 即关闭
# ════════════════════════════════════════════════════════════════════
def _build_chromium_options():
    co = ChromiumOptions()
    co.auto_port()  # 随机调试端口，避免与残留浏览器实例冲突
    path = NH_CHROME_PATH
    if not path:
        for name in ("google-chrome", "google-chrome-stable", "chrome",
                     "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                break
    if path:
        co.set_browser_path(path)
    # CI/root 环境下 Chrome 必需的参数，本地常规环境无副作用
    for arg in ("--no-sandbox", "--disable-dev-shm-usage",
                "--window-size=1280,900",
                "--disable-blink-features=AutomationControlled"):
        co.set_argument(arg)
    if NH_HEADLESS:
        co.set_argument("--headless=new")  # 无头过盾成功率低，仅调试用
    if NH_PROXY:
        co.set_proxy(NH_PROXY)
    return co

def _turnstile_click(page) -> bool:
    """在 Turnstile iframe 内点击复选框（shadow DOM 穿透）。
    逻辑来自长期实测的 cf_turnstile_solver.py。"""
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
    """点击 Turnstile 并等待 cf-turnstile-response 出现 token（最多 3 轮点击）。"""
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
    """等待登录表单出现；若先撞上 Cloudflare 全页盾则点击尝试通过。"""
    deadline = time.time() + timeout_total
    while time.time() < deadline:
        if page.ele('css:#identifier', timeout=3):
            return True
        _turnstile_click(page)  # 全页盾场景：点完复选框会自动跳回真实页面
        time.sleep(3)
    return bool(page.ele('css:#identifier', timeout=3))

def _check_remember_box(page) -> None:
    """勾选 Se souvenir de moi（30 天 Remember cookie，供后续免浏览器登录）。
    优先点 <label>（对视觉隐藏的 input 也生效），并回读真实字段名便于诊断；
    失败不影响主流程。"""
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
            if target_input is None:
                try:
                    target_input = lbl.ele('tag:input', timeout=1)
                except Exception:
                    target_input = None
        already = False
        if target_input is not None:
            try:
                already = bool(target_input.states.is_checked)
            except Exception:
                already = False
        if already:
            log.info("Se souvenir de moi 已是勾选状态")
            return
        if lbl is not None:
            lbl.click()
            log.info("已勾选 Se souvenir de moi (label)")
            return
        cb = (page.ele('css:input[name="remember"]', timeout=1)
              or page.ele('css:#remember', timeout=1)
              or page.ele('css:input[type="checkbox"]', timeout=1))
        if cb:
            cb.click()
            log.info("已勾选 Se souvenir de moi (input, name=%s)", cb.attr('name'))
    except Exception:
        pass

def _fill_and_submit(page) -> None:
    idf = page.ele('css:#identifier', timeout=5)
    pwd = page.ele('css:#password', timeout=5)
    if not idf or not pwd:
        raise RuntimeError("登录表单元素缺失")
    idf.clear()
    idf.input(NH_IDENTIFIER)
    pwd.clear()
    pwd.input(NH_PASSWORD)
    _check_remember_box(page)
    # Turnstile token 有效期短且一次性，放在提交前最后一步
    if not _turnstile_token(page):
        raise RuntimeError("Turnstile 未取到 token")
    btn = page.ele('css:button[type="submit"]', timeout=5)
    if not btn:
        raise RuntimeError("提交按钮未找到")
    btn.click()

def _wait_after_submit(page, timeout: int = 30):
    """等待登录跳转，并要求稳定离开 /login 至少 3 秒（防止"路过"假象：
    302 短暂经过非登录页后又被打回登录页）。返回 (是否成功, 页面文字片段)。"""
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
            time.sleep(1)  # 等新页面稳定
            return True, ""
    try:
        body_ele = page.ele('tag:body')
        body = ((body_ele.text or "") if body_ele else "")[:300].replace("\n", " ").strip()
    except Exception:
        body = ""
    return False, body

def _browser_check_ads(page, timeout_total: int = 30) -> bool:
    """浏览器内访问 ads.php 做登录态"实勘"（比 URL 判断更接近真值）。
    True  = 浏览器带着当前 cookie 能正常打开广告页（未被踢回 /login）
    False = 被踢回登录页 → 登录实际未生效"""
    page.get(ADS_URL)
    deadline = time.time() + timeout_total
    while time.time() < deadline:
        if page.ele('css:#identifier', timeout=3):
            return False  # 出现登录表单 → 会话无效
        if _turnstile_click(page):  # 此处万一出现 CF 全页盾，先点掉
            time.sleep(3)
            continue
        time.sleep(1)
        if not page.ele('css:#identifier', timeout=2):
            return True
    return False

def browser_login():
    """浏览器账号密码登录。
    成功返回 (raw_cookies, user_agent)，失败返回 (None, '')。
    每次失败会保存现场截图 nh_debug_login_N.png（已清空输入框）供排查。"""
    for attempt in range(1, NH_LOGIN_RETRIES + 1):
        page = None
        try:
            log.info("🌐 浏览器登录尝试 %d/%d ...", attempt, NH_LOGIN_RETRIES)
            page = ChromiumPage(_build_chromium_options())
            page.get(LOGIN_URL)
            if not _wait_login_form(page):
                raise RuntimeError("登录表单未出现（疑似被 Cloudflare 拦截）")
            _fill_and_submit(page)
            ok, snippet = _wait_after_submit(page)
            if not ok:
                raise RuntimeError(f"提交后仍停留在登录页，页面提示: {snippet[:120] or '(无)'}")
            log.info("登录后落地页: %s | 标题: %s", page.url, page.title)
            # 浏览器内"实勘" ads.php：确认登录态真实生效
            # （防止落地页假象：URL 虽离开 /login，服务端会话其实未认证）
            if not _browser_check_ads(page):
                body = ""
                try:
                    body_ele = page.ele('tag:body')
                    body = ((body_ele.text or "") if body_ele else "")[:150].replace("\n", " ").strip()
                except Exception:
                    pass
                raise RuntimeError(f"浏览器内访问 ads.php 被踢回登录页，登录未真正生效。页面提示: {body or '(无)'}")
            raw = _normalize_cookies(page.cookies())
            names = sorted({c.get("name") for c in raw if c.get("name")})
            lens = {c.get("name"): len(c.get("value") or "") for c in raw}
            if not any(c.get("name") == "__Host-NH" for c in raw):
                raise RuntimeError(f"登录后未见 __Host-NH cookie（现有: {', '.join(names) or '无'}）")
            ua = page.user_agent or DEFAULT_UA
            log.info("✅ 浏览器登录成功，cookie: %s",
                     ", ".join(f"{n}({lens.get(n, 0)})" for n in names))
            return raw, ua
        except Exception as e:
            log.warning("浏览器登录失败: %s", e)
            if page:
                try:
                    log.warning("失败时页面: url=%s | title=%s", page.url, page.title)
                except Exception:
                    pass
                try:
                    # 清空输入框避免凭据入镜，再截现场图（workflow 会作为 artifact 上传）
                    for sel in ('css:#identifier', 'css:#password'):
                        try:
                            page.ele(sel, timeout=1).clear()
                        except Exception:
                            pass
                    page.get_screenshot(path=os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        f"nh_debug_login_{attempt}.png"))
                except Exception:
                    pass
            time.sleep(8)
        finally:
            try:
                if page:
                    page.quit()
            except Exception:
                pass
    return None, ""

# ════════════════════════════════════════════════════════════════════
# 统一登录入口：先试 cookie，再试浏览器账号密码
# ════════════════════════════════════════════════════════════════════
def ensure_login(s: requests.Session) -> bool:
    """确保会话可用：先用现有 cookie 试一次（remember 可自动换发新 session），
    失败则走浏览器账号密码登录，并把新 cookie + UA 回填进 s。"""
    if _cookie_login_ok(s):
        log.info("✅ 现有 cookie 会话仍有效")
        return True

    if not (NH_IDENTIFIER and NH_PASSWORD):
        log.error("cookie 会话失效，且未设置 NH_IDENTIFIER/NH_PASSWORD，无法重新登录")
        return False

    log.info("cookie 会话失效，启动浏览器重新登录 ...")
    raw, ua = browser_login()
    if not raw:
        return False
    s.headers.update({"User-Agent": ua})
    s.cookies.clear()
    _apply_raw_cookies(s, raw)
    if _cookie_login_ok(s, verbose=True):
        log.info("✅ 重新登录成功，继续挂机")
        return True
    log.error("重新登录后 HTTP 会话验证仍未通过")
    return False

# ════════════════════════════════════════════════════════════════════
# 核心赚币逻辑（沿用原版，纯 HTTP）
# ════════════════════════════════════════════════════════════════════
def _get_balance(s: requests.Session) -> float:
    r = s.get(ADS_URL, timeout=20)
    if _is_login_page(r):
        raise PermissionError("会话已失效")
    m = re.search(r'font-bold text-lg">([\d.]+) coins', r.text)
    if not m:
        log.error("ads.php 页面异常: status=%s | 最终URL=%s | title=%s",
                  r.status_code, r.url, _page_title(r))
        if _cf_blocked(r):
            raise RuntimeError(f"ads.php 被拦截 (status={r.status_code})")
        raise RuntimeError(f"页面中未找到余额 (status={r.status_code})")
    return float(m.group(1))

def _get_csrf(s: requests.Session) -> str:
    r = s.get(ADS_URL, timeout=20)
    if _is_login_page(r):
        raise PermissionError("会话已失效")
    m = re.search(r'name="csrf_token" value="([a-f0-9]+)"', r.text)
    if not m:
        log.error("ads.php 页面异常: status=%s | 最终URL=%s | title=%s",
                  r.status_code, r.url, _page_title(r))
        if _cf_blocked(r):
            raise RuntimeError(f"ads.php 被拦截 (status={r.status_code})")
        raise RuntimeError(f"csrf token 未找到 (status={r.status_code})")
    return m.group(1)

def gen_callback(s: requests.Session, csrf: str) -> str | None:
    r = s.post(ADS_URL, data={"csrf_token": csrf}, allow_redirects=False, timeout=20)
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

def report(state: dict, balance: float, force: bool = False) -> None:
    now = time.time()
    if not force and now - state.get("last_report", 0) < 3600:
        return
    state["last_report"] = now
    save_state(state)
    total = state.get("total", 0.0)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"🪄 NeoHeberg AFK 已连接\n📅 {ts}\n\n"
        f"💰 <b>余额</b>: {balance:.4f} 🪙\n"
        f"📈 <b>累计收益</b>: +{total:.4f} 🪙（{state['rounds']} 轮）"
    )
    send_tg(msg)
    log.info("TG 报告: 余额=%s 累计收益=%s", balance, total)

# ════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════
def main() -> None:
    login_only = "--login-only" in sys.argv

    has_credentials = bool(NH_IDENTIFIER and NH_PASSWORD)
    has_env_cookie = bool(NH_REMEMBER or NH_SESSION or NH_COOKIE_HEADER)
    if not has_credentials and not has_env_cookie:
        log.error("缺少凭据：请设置 NH_IDENTIFIER/NH_PASSWORD（账号密码登录），"
                  "或 NH_REMEMBER（cookie 快速通道）")
        sys.exit(1)

    # ── 阶段一：登录 ──
    s = None
    if has_env_cookie:
        log.info("🔎 检测到 cookie 环境变量，先尝试免浏览器快速登录 ...")
        s = session_from_env()
        if _cookie_login_ok(s):
            log.info("✅ cookie 快速通道登录成功，跳过浏览器")
        else:
            log.info("cookie 快速通道未通过，转入浏览器登录")
            s = None

    if s is None:
        if not has_credentials:
            log.error("cookie 登录失败且未设置 NH_IDENTIFIER/NH_PASSWORD，无法继续")
            send_tg("❌ NeoHeberg 登录失败：cookie 失效且未配置账号密码")
            sys.exit(1)
        raw, ua = browser_login()
        if not raw:
            log.error("浏览器登录 %d 次尝试均失败", NH_LOGIN_RETRIES)
            send_tg("❌ NeoHeberg 浏览器登录失败（Turnstile/凭据/网络）。"
                    "请检查 Secrets 中的 NH_IDENTIFIER/NH_PASSWORD，"
                    "或配置 NH_PROXY 后重试")
            sys.exit(1)
        s = session_from_browser(raw, ua)
        if not _cookie_login_ok(s, verbose=True):
            log.error("浏览器登录成功但 HTTP 会话验证未通过")
            send_tg("❌ NeoHeberg 浏览器登录成功，但 cookie 交给 requests 后验证失败")
            sys.exit(1)
        log.info("✅ 浏览器登录 → requests 会话验证通过，浏览器已关闭，开始挂机")

    # ── 余额 + 启动报告 ──
    state = load_state()
    try:
        bal = _get_balance(s)
        update_earnings(state, bal)
        log.info("启动，当前余额: %s，历史累计收益: %s", bal, state["total"])
        if login_only:
            log.info("🧪 --login-only 模式验证通过，正常退出")
            sys.exit(0)
        report(state, bal, force=True)
    except Exception as e:
        log.error("启动失败: %s", e)
        send_tg(f"❌ NeoHeberg 启动失败: {e}")
        sys.exit(1)

    # ── 阶段二：赚币主循环（沿用原版逻辑）──
    consecutive_fail = 0
    relogin_cycles = 0
    while True:
        # 退出条件检查：判断是否达到设定的最大轮次
        if NH_MAX_ROUNDS > 0 and state.get("rounds", 0) >= NH_MAX_ROUNDS:
            log.info("🎯 已达到设定的最大轮次 %d，脚本平滑退出", NH_MAX_ROUNDS)
            send_tg(f"✅ NeoHeberg 任务完成，已跑满 {NH_MAX_ROUNDS} 轮，平滑退出。")
            break

        try:
            csrf = _get_csrf(s)
            cb = gen_callback(s, csrf)
            if not cb:
                consecutive_fail += 1
                if consecutive_fail >= 5:
                    raise RuntimeError("连续 5 次生成回调失败")
                time.sleep(20)
                continue
            time.sleep(NH_WAIT)
            st = redeem(s, cb)
            if st != 200:
                log.warning("回调状态异常: %s", st)
            time.sleep(SETTLE_SECONDS)
            state["rounds"] += 1
            consecutive_fail = 0
            relogin_cycles = 0
            log.info("第 %d 轮完成 (回调 %s)", state["rounds"], st)
        except PermissionError as e:
            log.error("会话过期: %s，尝试自动重新登录", e)
            try:
                if ensure_login(s):
                    log.info("✅ 自动重新登录成功，继续运行")
                    continue
                log.error("自动重新登录失败（cookie 与账号密码都未通过），挂机暂停")
                send_tg("⚠️ NeoHeberg 自动重新登录失败（Turnstile/凭据/网络），"
                        "1 小时后重试；建议检查 Secrets 或配置 NH_PROXY")
                time.sleep(3600)
            except Exception as e2:
                log.error("自动重新登录异常: %s", e2)
                send_tg(f"⚠️ NeoHeberg 自动重新登录异常: {e2}")
                time.sleep(3600)
        except Exception as e:
            log.exception("异常: %s", e)
            consecutive_fail += 1
            if consecutive_fail >= 5:
                # 持续失败：浏览器重登刷新会话与 CF cookie（cf_clearance 等）
                relogin_cycles += 1
                if relogin_cycles >= 3:
                    log.error("页面持续异常，%d 轮重登无效，退出等待下次运行", relogin_cycles)
                    send_tg("❌ NeoHeberg 页面持续异常（疑似 Cloudflare 拦截），自动重登无效已退出。"
                            "建议在 workflow 启用 NH_PROXY 优质代理后重跑")
                    sys.exit(1)
                log.warning("已连续失败 %d 次，第 %d 次尝试浏览器重登 ...", consecutive_fail, relogin_cycles)
                try:
                    if ensure_login(s):
                        consecutive_fail = 0
                        continue
                except Exception as e2:
                    log.error("重登异常: %s", e2)
                send_tg("⚠️ NeoHeberg 页面持续异常且重登无效，30 分钟后重试；"
                        "若反复出现建议启用 NH_PROXY 优质代理")
                time.sleep(1800)
            else:
                time.sleep(RETRY_COOLDOWN)

        # 定期报告 + 状态（每次都刷新 total，无论是否到报告间隔）
        try:
            bal = _get_balance(s)
            update_earnings(state, bal)
            report(state, bal)
            save_state(state)
        except Exception:
            pass

    save_state(state)

if __name__ == "__main__":
    main()
