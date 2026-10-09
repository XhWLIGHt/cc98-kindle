#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CC98 官方 API 客户端(纯标准库)。

职责:
  - OpenID 登录(password grant)与 token 刷新,持久化到 token.json
  - 只读端点调用(版面 / 帖子列表 / 楼层 / 热帖 / 首页数据 / 用户信息)
  - 响应形态容错(裸数组或包一层)、TTL 缓存、并发安全

对外只抛 ApiError / AuthError / NetworkError,message 均为给用户看的中文。
"""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://openid.cc98.org/connect/token"
API_BASE = "https://api.cc98.org"

# CC98 第三方客户端通用的 password-grant 公共客户端凭据
# (ZJU-CC98/Forum、CC98-CLI、cc98-mcp 等开源项目一致使用)
CLIENT_ID = "9a1fd200-8687-44b1-4c20-08d50a96e5cd"
CLIENT_SECRET = "8b53f727-08e2-4509-8857-e34bf92b27f2"
SCOPE = "cc98-api openid offline_access"

USER_AGENT = "cc98-kindle/1.0"


class ApiError(Exception):
    """API 错误。message 是给用户看的中文说明。"""

    def __init__(self, message, status=None, detail=""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


class AuthError(ApiError):
    """登录失效 / 凭据错误 / 无权限。"""


class NetworkError(ApiError):
    """网络不可达或超时。"""


class _HttpStatus(Exception):
    """内部异常:上游返回非 2xx。"""

    def __init__(self, code, body):
        super().__init__("HTTP %d" % code)
        self.code = code
        self.body = body


def as_list(data, *keys):
    """兼容裸数组 / {"xxx": [...]} 两种返回形态,取列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in list(keys) + ["data", "items", "list", "topics", "posts", "boards"]:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


class CC98API:
    def __init__(self, username="", password="",
                 token_path="token.json", timeout=15, cache_ttl=60, log=print):
        self.username = username or ""
        self.password = password or ""
        self.token_path = token_path
        self.timeout = max(5, int(timeout or 15))
        self.cache_ttl = max(0, int(cache_ttl or 0))
        self.log = log

        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._token_lock = threading.Lock()
        self._net_lock = threading.Lock()
        self._cache = {}
        self._cache_lock = threading.Lock()

        self._load_token_file()

    # ---------------- token 持久化 ----------------

    def _load_token_file(self):
        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._expires_at = float(data.get("expires_at") or 0)
        except (OSError, ValueError):
            pass

    def _save_token_file(self):
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = self.token_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.token_path)
        except OSError as e:
            self.log("[token] 保存 token.json 失败: %s" % e)

    # ---------------- 登录 / 刷新 ----------------

    def _post_token(self, params):
        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(TOKEN_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(text)
            except ValueError:
                err = {}
            code = err.get("error", "")
            desc = err.get("error_description", "")
            if code == "invalid_client":
                raise ApiError("CC98 登录客户端凭据失效(程序内的 CLIENT_ID/SECRET 需要更新)", detail=text)
            if code == "invalid_grant" and "invalid_username_or_password" in desc:
                raise AuthError("CC98 账号或密码错误,请检查 config.json")
            if code == "invalid_grant":
                raise AuthError("登录凭据已失效,请检查 config.json 里的账号密码", detail=text)
            raise ApiError("CC98 登录失败(%s %s)" % (code or e.code, desc), detail=text)
        except urllib.error.URLError as e:
            raise self._wrap_url_error(e)
        except (socket.timeout, TimeoutError):
            raise NetworkError("CC98 登录接口响应超时,请稍后重试")
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ApiError("CC98 登录接口返回了无法解析的数据")
        if not data.get("access_token"):
            raise ApiError("CC98 登录接口没有返回 access_token")
        return data

    def _store_token(self, data):
        self._access_token = data.get("access_token")
        if data.get("refresh_token"):
            # refresh_token 可能轮换,始终保存最新的
            self._refresh_token = data["refresh_token"]
        expires_in = int(data.get("expires_in") or 3600)
        self._expires_at = time.time() + max(60, expires_in - 60)
        self._save_token_file()

    def login(self):
        """用 config.json 里的账号密码登录。"""
        if not self.username or not self.password:
            raise AuthError("尚未配置 CC98 账号密码:请编辑 config.json 的 username/password 后重启")
        data = self._post_token({
            "username": self.username,
            "password": self.password,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "password",
            "scope": SCOPE,
        })
        self._store_token(data)
        return self._access_token

    def refresh(self):
        """用 refresh_token 换新的 access_token。"""
        if not self._refresh_token:
            raise AuthError("没有可用的刷新令牌")
        data = self._post_token({
            "refresh_token": self._refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "scope": SCOPE,
        })
        self._store_token(data)
        return self._access_token

    def ensure_token(self, force=False):
        """确保持有可用 access_token(带锁,防多线程重复刷新)。"""
        with self._token_lock:
            if not force and self._access_token and time.time() < self._expires_at:
                return
            if self._refresh_token:
                try:
                    self.refresh()
                    return
                except AuthError:
                    self.log("[token] 刷新令牌已失效,改用账号密码重新登录")
            self.login()

    # ---------------- 底层 GET ----------------

    def _wrap_url_error(self, e):
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.gaierror):
            return NetworkError("无法连接 api.cc98.org:请确认电脑在浙大内网(或已连 RVPN/WebVPN)")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return NetworkError("CC98 响应超时,请稍后重试")
        return NetworkError("网络错误:%s" % reason)

    def _do_get(self, url, headers):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise _HttpStatus(e.code, e.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            raise self._wrap_url_error(e)
        except (socket.timeout, TimeoutError):
            raise NetworkError("CC98 响应超时,请稍后重试")

    def _http_error(self, e):
        body = (e.body or "").strip()
        if e.code == 401:
            if "cannot_entry_board" in body:
                return AuthError("该版面需要登录或权限(也可能该帖不允许查看)")
            return AuthError("登录已失效,请检查 config.json 里的账号密码后重启")
        if e.code == 403:
            return AuthError("没有权限查看该内容")
        if e.code == 404:
            return ApiError("内容不存在或已被删除", status=404)
        if e.code == 400:
            return ApiError("CC98 拒绝了这次请求(400)", status=400, detail=body[:300])
        if e.code == 429:
            return ApiError("请求太频繁,请稍后再试(429)", status=429)
        return ApiError("CC98 返回异常(HTTP %d)" % e.code, status=e.code, detail=body[:300])

    def get_json(self, path, params=None, auth=True):
        """GET 一个 API 端点。auth=True 时自动附带 Bearer token,
        token 失效(401)会自动强制刷新并重试一次。"""
        url = API_BASE + urllib.parse.quote(path, safe="/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        for attempt in (1, 2):
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
            if auth:
                self.ensure_token()
                headers["Authorization"] = "Bearer " + (self._access_token or "")
            try:
                with self._net_lock:
                    raw = self._do_get(url, headers)
            except _HttpStatus as e:
                if e.code == 401 and auth and attempt == 1:
                    # token 可能过期:强制刷新后重试一次
                    self.ensure_token(force=True)
                    continue
                raise self._http_error(e)
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return {"_raw": text}
        raise ApiError("请求失败")

    # ---------------- 高层端点(带 TTL 缓存) ----------------

    def _cached(self, key, ttl, fn):
        now = time.time()
        if ttl > 0:
            with self._cache_lock:
                hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = fn()
        if ttl > 0:
            with self._cache_lock:
                self._cache[key] = (now + ttl, value)
        return value

    def get_index(self):
        """首页数据:公告 / 站内统计 / 热帖(匿名可用)。"""
        return self._cached("index", min(max(self.cache_ttl * 2, 60), 180),
                            lambda: self.get_json("/config/index", auth=False))

    def get_board_all(self):
        """全部版面树(匿名可用),长期缓存。"""
        return self._cached("board_all", 600,
                            lambda: self.get_json("/board/all", auth=False))

    def get_board(self, board_id):
        """版面详情(匿名可用)。"""
        return self._cached("board:%s" % board_id, 300,
                            lambda: self.get_json("/board/%s" % board_id, auth=False))

    def get_hot(self, size=10):
        """十大热帖(匿名可用;上游固定返回 10 条)。"""
        return self._cached("hot:%s" % size, 120,
                            lambda: self.get_json("/topic/hot", {"from": 0, "size": size}, auth=False))

    def get_board_topics(self, board_id, from_=0, size=20):
        """版面帖子列表(需登录)。size 超上限(400)时自动降级重试。"""
        size = max(1, min(30, int(size)))
        key = "bt:%s:%s:%s" % (board_id, from_, size)

        def fetch():
            try:
                return self.get_json("/board/%s/topic" % board_id,
                                     {"from": from_, "size": size})
            except ApiError as e:
                if e.status == 400 and size > 5:
                    return self.get_json("/board/%s/topic" % board_id,
                                         {"from": from_, "size": 5})
                raise

        return self._cached(key, self.cache_ttl, fetch)

    def get_topic(self, topic_id):
        """帖子详情(需登录)。"""
        return self._cached("topic:%s" % topic_id, self.cache_ttl,
                            lambda: self.get_json("/topic/%s" % topic_id))

    def get_topic_posts(self, topic_id, from_=0, size=10):
        """楼层列表(需登录)。size 超上限(400)时自动降级重试。"""
        size = max(1, min(30, int(size)))
        key = "tp:%s:%s:%s" % (topic_id, from_, size)

        def fetch():
            try:
                return self.get_json("/topic/%s/post" % topic_id,
                                     {"from": from_, "size": size})
            except ApiError as e:
                if e.status == 400 and size > 5:
                    return self.get_json("/topic/%s/post" % topic_id,
                                         {"from": from_, "size": 5})
                raise

        return self._cached(key, self.cache_ttl, fetch)

    def get_new_topics(self, from_=0, size=20):
        """全站最新主题(需登录)。"""
        size = max(1, min(30, int(size)))
        return self._cached("new:%s:%s" % (from_, size), self.cache_ttl,
                            lambda: self.get_json("/topic/new",
                                                  {"from": from_, "size": size}))

    def get_user(self, user_id):
        """用户资料(需登录)。头像等资料变化慢,缓存时间长一些。"""
        return self._cached("user:%s" % user_id, max(self.cache_ttl, 600),
                            lambda: self.get_json("/user/%s" % user_id))

    def get_user_by_name(self, name):
        """按用户名取资料(需登录)。"""
        return self._cached("user:name:%s" % name, self.cache_ttl,
                            lambda: self.get_json("/user/name/%s" % name))

    def get_user_recent_topics(self, user_id, from_=0, size=20):
        """用户最近发表的主题(需登录)。"""
        size = max(1, min(30, int(size)))
        return self._cached("urt:%s:%s:%s" % (user_id, from_, size), self.cache_ttl,
                            lambda: self.get_json("/user/%s/recent-topic" % user_id,
                                                  {"from": from_, "size": size}))

    # ---------------- 写操作(回帖) ----------------

    # 服务端返回的错误码 → 中文说明(取自 CC98 前端)
    REPLY_ERRORS = {
        "content_too_long": "内容太长(超过 8000 字)",
        "content_is_empty": "内容不能为空",
        "wealth_not_enough_for_anonymous_post": "财富值不足,无法匿名回复",
        "cannot_post_in_this_board": "该版面不允许回复",
        "cannot_post_anonymous": "该版面不允许匿名回复",
        "topic_is_locked": "帖子已锁定,无法回复",
        "board_is_locked": "版面已锁定,无法回复",
        "user_state_is_abnormal": "账号状态异常,无法回复(可能被禁言)",
        "cannot_entry_board": "无权在该版面回复",
        "last_post_in_10_seconds": "发帖太快了,请 10 秒后再试",
        "anonymity_not_enough_level": "等级不足,无法匿名回复",
        "topic_not_exists": "主题不存在或已被删除",
        "topic_not_found": "主题不存在或已被删除",
    }

    def post_json(self, path, payload, auth=True):
        """POST JSON。返回 (http状态码, 响应文本);网络错误抛 NetworkError。"""
        url = API_BASE + urllib.parse.quote(path, safe="/")
        data = json.dumps(payload).encode("utf-8")
        for attempt in (1, 2):
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
            if auth:
                self.ensure_token()
                headers["Authorization"] = "Bearer " + (self._access_token or "")
            try:
                with self._net_lock:
                    req = urllib.request.Request(url, data=data, headers=headers,
                                                 method="POST")
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 401 and auth and attempt == 1:
                    self.ensure_token(force=True)
                    continue
                return e.code, body
            except urllib.error.URLError as e:
                raise self._wrap_url_error(e)
            except (socket.timeout, TimeoutError):
                raise NetworkError("CC98 响应超时,请稍后重试")
        raise ApiError("请求失败")

    def invalidate_topic(self, topic_id):
        """回帖成功后清掉该主题的缓存,保证刷新到最新回复。"""
        exact = "topic:%s" % topic_id
        prefix = "tp:%s:" % topic_id
        with self._cache_lock:
            for k in [k for k in self._cache if k == exact or str(k).startswith(prefix)]:
                self._cache.pop(k, None)

    def post_reply(self, topic_id, content, is_anonymous=False):
        """回复主题。成功返回 True,失败抛 ApiError(带中文说明)。"""
        content = (content or "").strip()
        if not content:
            raise ApiError("回复内容不能为空")
        if len(content) > 8000:
            raise ApiError("回复内容太长(超过 8000 字)")
        status, body = self.post_json("/topic/%s/post" % topic_id, {
            "content": content,
            "contentType": 0,          # 纯文本(UBB)
            "title": "",
            "isAnonymous": bool(is_anonymous),
            "notifyAllReplier": False,
            "clientType": 1,
        })
        if status in (200, 201, 204):
            self.invalidate_topic(topic_id)
            return True
        code = (body or "").strip().strip('"')
        msg = self.REPLY_ERRORS.get(code)
        if not msg:
            if status == 401:
                msg = "登录已失效,请重启程序"
            elif status == 403:
                msg = "没有权限回复(可能被禁言或版面受限)"
            elif status == 402:
                msg = "发帖受限(可能当日额度已用完)"
            else:
                msg = "回复失败(HTTP %d)%s" % (status, (": " + code[:100]) if code else "")
        raise ApiError(msg)

    # ---------------- 我的(需登录) ----------------

    def get_me(self):
        """当前登录用户的完整资料。"""
        return self._cached("me", 60, lambda: self.get_json("/me"))

    def get_me_recent_topics(self, from_=0, size=20):
        """我发表的主题。"""
        size = max(1, min(30, int(size)))
        return self._cached("mt:%s:%s" % (from_, size), self.cache_ttl,
                            lambda: self.get_json("/me/recent-topic",
                                                  {"from": from_, "size": size}))

    def get_me_recent_posts(self, from_=0, size=20):
        """我的回复(返回 {count, data:[帖子]} 分页结构)。"""
        size = max(1, min(30, int(size)))
        return self._cached("mp:%s:%s" % (from_, size), self.cache_ttl,
                            lambda: self.get_json("/me/recent-post",
                                                  {"from": from_, "size": size}))

    def get_favorites(self, from_=0, size=20):
        """我收藏的主题。"""
        size = max(1, min(30, int(size)))
        return self._cached("fav:%s:%s" % (from_, size), self.cache_ttl,
                            lambda: self.get_json("/topic/me/favorite",
                                                  {"from": from_, "size": size, "order": 1}))

    def get_followee(self):
        """我关注的人(用户 id 列表)。"""
        return self._cached("followee", 300,
                            lambda: self.get_json("/me/followee", {"from": 0, "size": 30}))

    def get_follower(self):
        """我的粉丝(用户 id 列表)。"""
        return self._cached("follower", 300,
                            lambda: self.get_json("/me/follower", {"from": 0, "size": 30}))

    def whoami(self):
        """当前登录用户(也用于验证 token 有效性,401 会触发刷新重试)。"""
        data = self._cached("me", 60, lambda: self.get_json("/me"))
        if isinstance(data, dict):
            for k in ("name", "userName", "displayName", "id"):
                if data.get(k):
                    return data[k]
        return "已登录"
