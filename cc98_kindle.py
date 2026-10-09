#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CC98 Kindle 阅读器 —— 本地服务入口。

用法:
    python cc98_kindle.py

启动后,把横幅里打印的地址抄进 Kindle 自带浏览器即可阅读 CC98。
只读、零第三方依赖(纯标准库)。

模块分工:
    cc98_api.py    CC98 API 客户端与 token 管理
    cc98_ubb.py    UBB / Markdown 正文渲染
    cc98_page.py   面向 KPW3 的页面模板
    cc98_kindle.py 本文件:配置、网络探测、HTTP 服务与路由
"""

import base64
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from cc98_api import CC98API, ApiError, AuthError, NetworkError, as_list  # noqa: E402
from cc98_ubb import RenderCtx, render_content, plain_text  # noqa: E402
import cc98_emoji  # noqa: E402
import cc98_page as P  # noqa: E402

# 可选依赖 Pillow:CC98 现在上传的图片基本都是 WebP,KPW3 的老浏览器解不了,
# 装了 Pillow 才能在服务端把 WebP 转成 JPEG 给 Kindle 显示。
# 没装也不影响运行,只是图片会显示成破损图标。
try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None

CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = BASE_DIR / "token.json"

DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "host": "0.0.0.0",
    "port": 8000,
    "access_key": "",
    "posts_per_page": 10,
    "topics_per_page": 20,
    "font_size_px": 34,
    "line_height": 1.8,
    "show_images": True,
    "show_image_url": False,
    "show_emoticons": True,
    "emoji_as_image": True,
    "image_max_kb": 2048,
    "allow_external_links": True,
    "request_timeout": 15,
    "cache_ttl": 60,
}

HOTSPOT_IP = "192.168.137.1"  # Windows 移动热点(ICS)的电脑侧固定地址


class KindleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 允许两个进程同时绑定同一端口、互相抢占请求,
    # 会导致"改了代码没生效"的假象,必须关掉;POSIX 上保留以避开 TIME_WAIT 误报
    allow_reuse_address = (os.name != "nt")


# ==================== 配置 ====================

def load_config():
    if not CONFIG_PATH.exists():
        cfg = dict(DEFAULT_CONFIG)
        cfg["access_key"] = secrets.token_hex(3)
        save_config(cfg)
        print("=" * 56)
        print("  已生成配置文件:%s" % CONFIG_PATH)
        print("  请填入 CC98 账号(学号)和密码:")
        print('    "username": "你的学号",')
        print('    "password": "你的密码",')
        print("  然后重新运行:python cc98_kindle.py")
        print("=" * 56)
        sys.exit(0)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print("config.json 读取失败(%s),请检查文件格式。" % e)
        print("可以删除该文件让程序重新生成模板(注意会丢失现有设置)。")
        sys.exit(1)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})
    if not merged.get("access_key"):
        merged["access_key"] = secrets.token_hex(3)
        save_config(merged)
        print("[配置] 已自动生成访问口令 access_key")
    return merged


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ==================== 网络地址探测 ====================

def candidate_ips():
    """枚举本机候选 IP:[(ip, 提示语)],按推荐顺序排列。"""
    found = []

    def add(ip):
        if ip and not ip.startswith("127.") and ip not in found:
            found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(res[4][0])
    except OSError:
        pass
    add(HOTSPOT_IP)  # 兜底:Windows 移动热点

    def rank(ip):
        if ip == HOTSPOT_IP:
            return 3
        try:
            return 1 if ipaddress.ip_address(ip).is_private else 2
        except ValueError:
            return 2

    found.sort(key=rank)  # 稳定排序:内网地址 -> 公网地址 -> 热点兜底

    labels = {
        HOTSPOT_IP: "(Windows 移动热点地址:Kindle 连上电脑热点后用这个)",
    }
    out = []
    for ip in found:
        label = labels.get(ip, "")
        if not label:
            try:
                private = ipaddress.ip_address(ip).is_private
            except ValueError:
                private = False
            label = "(推荐,内网地址)" if private else "(校园网公网地址,可能被防火墙拦截)"
        out.append((ip, label))
    return out


# ==================== 应用逻辑 ====================

class App:
    def __init__(self, cfg, api, base):
        self.cfg = cfg
        self.api = api
        self.base = base
        self.login_state = ("none", "未初始化")
        self._board_names = None
        self._board_names_lock = threading.Lock()

    # ---- 启动时验证登录 ----

    def try_login(self):
        if not (self.cfg.get("username") and self.cfg.get("password")):
            self.login_state = ("none",
                                "未配置账号:只能浏览热帖/版面目录,请编辑 config.json 填入学号和密码后重启")
        else:
            try:
                name = self.api.whoami()
                self.login_state = ("ok", "已登录:%s" % name)
            except ApiError as e:
                self.login_state = ("fail", e.message)
        return self.login_state

    # ---- 共享上下文 ----

    def emoji_base(self):
        """Unicode emoji 是否渲染为图片;不可用时返回 None(回退文字标签)。"""
        if not self.cfg.get("emoji_as_image", True):
            return None
        if not cc98_emoji.available():
            return None
        return self.base

    def ctx(self):
        return RenderCtx(
            base=self.base,
            show_images=self.cfg.get("show_images"),
            show_image_url=self.cfg.get("show_image_url"),
            show_emoticons=self.cfg.get("show_emoticons"),
            allow_external_links=self.cfg.get("allow_external_links", True),
            board_names=self.board_names(),
            emoji_base=self.emoji_base(),
        )

    def board_names(self):
        if self._board_names is not None:
            return self._board_names
        with self._board_names_lock:
            if self._board_names is not None:
                return self._board_names
            names = {}
            try:
                for group in as_list(self.api.get_board_all()):
                    if not isinstance(group, dict):
                        continue
                    for b in group.get("boards") or []:
                        if isinstance(b, dict) and b.get("id") is not None:
                            names[str(b.get("id"))] = b.get("name") or str(b.get("id"))
            except ApiError:
                pass
            self._board_names = names
            return names

    # ---- 各页面 ----

    def render_home(self):
        base = self.base
        parts = [P.nav_bar(base, "hot")]
        kind, msg = self.login_state
        if kind != "ok":
            parts.append('<p class="meta">[%s]</p>' % html.escape(msg))
        parts.append("<h1>十大热帖</h1>")
        hot = []
        try:
            hot = as_list(self.api.get_hot(10))
        except ApiError:
            try:
                index = self.api.get_index()
                if isinstance(index, dict):
                    hot = as_list(index.get("hotTopic"))
            except ApiError:
                pass
        if hot:
            for t in hot[:10]:
                if isinstance(t, dict):
                    parts.append(P.topic_item(base, t, emoji_base=self.emoji_base(),
                                              avatar=self.item_avatar(t)))
        else:
            parts.append("<p>暂时没有热帖。</p>")
        parts.append(P.footer(base))
        return "".join(parts), "CC98"

    def render_boards(self, gid=None):
        base = self.base
        parts = [P.nav_bar(base, "boards"), "<h1>版面列表</h1>"]
        if gid:
            parts.append('<p><a href="%s">« 全部分区</a></p>' % P.page_url(base, "/boards"))
        for group in as_list(self.api.get_board_all()):
            if not isinstance(group, dict):
                continue
            if gid and str(group.get("id")) != str(gid):
                continue
            parts.append("<h2>%s</h2>" % html.escape(str(group.get("name") or "")))
            for b in group.get("boards") or []:
                if isinstance(b, dict):
                    parts.append(P.board_item(base, b, emoji_base=self.emoji_base()))
        parts.append(P.footer(base))
        return "".join(parts), "版面列表"

    def render_hot(self):
        base = self.base
        parts = [P.nav_bar(base, "hot"), "<h1>十大热帖</h1>"]
        hot = as_list(self.api.get_hot(10))
        for t in hot:
            if isinstance(t, dict):
                parts.append(P.topic_item(base, t, emoji_base=self.emoji_base(),
                                          avatar=self.item_avatar(t)))
        if not hot:
            parts.append("<p>暂时没有热帖。</p>")
        parts.append(P.footer(base))
        return "".join(parts), "热帖"

    def render_me(self, from_):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("topics_per_page", 20))
        me = self.api.get_me()
        if not isinstance(me, dict) or not me.get("id"):
            raise AuthError("获取个人资料失败,请检查登录状态")
        uid = me.get("id")
        parts = [P.nav_bar(base, "me")]
        info = []
        for key, label in (("prestige", "威望"), ("popularity", "风评"),
                           ("postCount", "发帖"), ("fanCount", "粉丝"),
                           ("receivedLikeCount", "获赞")):
            v = me.get(key)
            if isinstance(v, int):
                info.append("%s %d" % (label, v))
        rt = P.fmt_date(me.get("registerTime"))
        if rt:
            info.append("注册 %s" % rt)
        parts.append('<div class="card thead"><h1>%s</h1><p class="meta">%s%s</p></div>'
                     % (P.text(me.get("name") or "我", self.emoji_base()),
                        self.avatar_html(uid),
                        html.escape(" · ".join(info))))
        parts.append('<p><a class="btn" href="%s">我的回复 »</a></p>'
                     % html.escape(P.page_url(base, "/me/posts")))
        parts.append("<h2>我的主题</h2>")
        topics = as_list(self.api.get_me_recent_topics(from_, size), "topics")
        parts.append(P.pager(base, "/me", {}, from_, size,
                             total=None, has_next=len(topics) >= size))
        if topics:
            for t in topics:
                if isinstance(t, dict):
                    parts.append(P.topic_item(base, t, emoji_base=self.emoji_base(),
                                              avatar=self.item_avatar(t)))
        else:
            parts.append('<p class="meta">还没有发表过主题。</p>')
        parts.append(P.pager(base, "/me", {}, from_, size,
                             total=None, has_next=len(topics) >= size))
        parts.append(P.footer(base))
        return "".join(parts), "我"

    def render_me_posts(self, from_):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("topics_per_page", 20))
        data = self.api.get_me_recent_posts(from_, size)
        items = (data.get("data") if isinstance(data, dict) else None) or []
        total = None
        if isinstance(data, dict):
            try:
                total = int(data.get("count"))
            except (TypeError, ValueError):
                total = None
        parts = [P.nav_bar(base, "me"), "<h1>我的回复</h1>"]
        parts.append('<p><a class="btn" href="%s">« 我的主题</a></p>'
                     % html.escape(P.page_url(base, "/me")))
        parts.append(P.pager(base, "/me/posts", {}, from_, size, total=total))
        if items:
            for it in items:
                if not isinstance(it, dict):
                    continue
                tid = it.get("topicId")
                floor = it.get("floor") or 1
                try:
                    frm = max(0, (int(floor) - 1) // 10 * 10)
                except (TypeError, ValueError):
                    frm = 0
                url = P.page_url(base, "/topic/%s" % tid, **{"from": frm})
                preview = plain_text(it.get("content"), 70) or "(空)"
                meta = "主题 %s · 第 %s 楼 · %s" % (tid, floor,
                                                  P.fmt_time(it.get("time")))
                parts.append('<a class="item" href="%s"><span class="t">%s</span>'
                             '<span class="meta">%s</span></a>'
                             % (html.escape(url), P.text(preview, self.emoji_base()),
                                html.escape(meta)))
        else:
            parts.append('<p class="meta">还没有回复过。</p>')
        parts.append(P.pager(base, "/me/posts", {}, from_, size, total=total))
        parts.append(P.footer(base))
        return "".join(parts), "我的回复"

    def render_follow(self, from_):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("topics_per_page", 20))
        parts = [P.nav_bar(base, "follow")]
        parts.append("<h1>关注</h1>")
        parts.append("<h2>我收藏的帖子</h2>")
        favs = as_list(self.api.get_favorites(from_, size), "topics")
        parts.append(P.pager(base, "/follow", {}, from_, size,
                             total=None, has_next=len(favs) >= size))
        if favs:
            for t in favs:
                if isinstance(t, dict):
                    parts.append(P.topic_item(base, t, emoji_base=self.emoji_base(),
                                              avatar=self.item_avatar(t)))
        else:
            parts.append('<p class="meta">还没有收藏的帖子。</p>')
        parts.append(P.pager(base, "/follow", {}, from_, size,
                             total=None, has_next=len(favs) >= size))
        for title, getter in (("我关注的人", self.api.get_followee),
                              ("我的粉丝", self.api.get_follower)):
            parts.append("<h2>%s</h2>" % title)
            links = []
            try:
                ids = getter()
            except ApiError:
                ids = []
            for uid in (ids or [])[:20]:
                if not isinstance(uid, int):
                    continue
                name = None
                try:
                    u = self.api.get_user(uid)
                    if isinstance(u, dict):
                        name = u.get("name")
                except ApiError:
                    pass
                label = name or ("用户 %d" % uid)
                links.append('<a href="%s">%s</a>'
                             % (html.escape(P.page_url(base, "/user/%d" % uid)),
                                P.text(label, self.emoji_base())))
            parts.append('<p class="meta">%s</p>'
                         % (" · ".join(links) if links else "暂无"))
        parts.append(P.footer(base))
        return "".join(parts), "关注"

    def render_new(self, from_):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("topics_per_page", 20))
        items = as_list(self.api.get_new_topics(from_, size), "topics")
        names = self.board_names()
        parts = [P.nav_bar(base, "new"), "<h1>新帖</h1>"]
        parts.append(P.pager(base, "/new", {}, from_, size,
                             total=None, has_next=len(items) >= size))
        if items:
            for t in items:
                if not isinstance(t, dict):
                    continue
                t = dict(t)
                if not t.get("boardName") and t.get("boardId") is not None:
                    t["boardName"] = names.get(str(t.get("boardId")))
                parts.append(P.topic_item(base, t, emoji_base=self.emoji_base(),
                                          avatar=self.item_avatar(t)))
        else:
            parts.append("<p>本页没有新帖。</p>")
        parts.append(P.pager(base, "/new", {}, from_, size,
                             total=None, has_next=len(items) >= size))
        parts.append(P.footer(base))
        return "".join(parts), "新帖"

    def render_board(self, board_id, from_):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("topics_per_page", 20))
        board = None
        try:
            data = self.api.get_board(board_id)
            if isinstance(data, dict) and data.get("name"):
                board = data
        except ApiError:
            pass
        bname = board.get("name") if board else None

        topics = as_list(self.api.get_board_topics(board_id, from_, size), "topics")

        parts = [P.nav_bar(base)]
        parts.append('<h1>%s</h1>' % P.text(bname or ("版面 %s" % board_id),
                                           self.emoji_base()))
        if board and board.get("description"):
            parts.append('<p class="meta">%s</p>'
                         % html.escape(str(board.get("description"))))
        parts.append(P.pager(base, "/board/%s" % board_id, {}, from_, size,
                             total=None, has_next=len(topics) >= size))
        if topics:
            for t in topics:
                if isinstance(t, dict):
                    parts.append(P.topic_item(base, t, board_name=bname,
                                              emoji_base=self.emoji_base(),
                                              avatar=self.item_avatar(t)))
        else:
            parts.append("<p>本页没有帖子(可能已到末尾,或该版面没有权限)。</p>")
        parts.append(P.pager(base, "/board/%s" % board_id, {}, from_, size,
                             total=None, has_next=len(topics) >= size))
        parts.append(P.footer(base))
        return "".join(parts), (bname or "版面")

    def render_topic(self, topic_id, from_):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("posts_per_page", 10))
        topic = self.api.get_topic(topic_id)
        if not isinstance(topic, dict) or topic.get("_raw"):
            raise ApiError("帖子数据异常,无法显示")

        title = topic.get("title") or "(无标题)"
        reply = topic.get("replyCount") or 0
        try:
            reply = int(reply)
        except (TypeError, ValueError):
            reply = 0
        total = reply + 1  # 总楼层 = 主题帖 + 回复(官方前端算法)

        posts = as_list(self.api.get_topic_posts(topic_id, from_, size), "posts")

        parts = [P.nav_bar(base)]
        board_id = topic.get("boardId")
        board_html = ""
        if board_id is not None:
            bname = self.board_names().get(str(board_id)) or ("版面 %s" % board_id)
            board_html = '<a href="%s">%s</a>' % (
                html.escape(P.page_url(base, "/board/%s" % board_id)),
                html.escape(str(bname)))
        # 楼主信息(头像 + 名字),放在卡片外面、标题下方
        t_uid = topic.get("userId")
        t_name = topic.get("userName")
        t_anon = bool(topic.get("isAnonymous"))
        if not t_name:
            avatar = ""
            author_html = P.text("匿名" if t_anon else "?", self.emoji_base())
        elif t_anon:
            avatar = ""
            author_html = P.text(t_name, self.emoji_base())
        else:
            if isinstance(t_uid, int) and t_uid > 0:
                avatar = self.avatar_html(t_uid)
                author_html = '<a href="%s">%s</a>' % (
                    html.escape(P.page_url(base, "/user/%s" % t_uid)),
                    P.text(t_name, self.emoji_base()))
            else:
                avatar = self.avatar_by_name(t_name)
                author_html = P.text(t_name, self.emoji_base())
        head_meta = [author_html, "回复 %d" % reply]
        tm = P.fmt_time(topic.get("time"))
        if tm:
            head_meta.append(html.escape(tm))
        if board_html:
            head_meta.append(board_html)
        parts.append('<div class="card thead"><h1>%s</h1>'
                     '<p class="meta">%s%s</p></div>'
                     % (P.text(title, self.emoji_base()), avatar,
                        " · ".join(head_meta)))
        reply_btn = ('<p><a class="btn" href="%s">回复本主题</a></p>'
                     % html.escape(P.page_url(base, "/reply", topic=topic_id)))
        parts.append(reply_btn)
        parts.append(P.pager(base, "/topic/%s" % topic_id, {}, from_, size, total=total))

        ctx = self.ctx()
        if posts:
            for p in posts:
                if isinstance(p, dict):
                    parts.append(self.render_floor(p, ctx))
        else:
            parts.append("<p>本页没有楼层。</p>")

        parts.append(P.pager(base, "/topic/%s" % topic_id, {}, from_, size, total=total))
        parts.append(reply_btn)
        parts.append(P.footer(base))
        return "".join(parts), str(title)

    def _avatar_img(self, url):
        """portraitUrl → <img> 标签(经图片代理加载)。"""
        if not url or not self.cfg.get("show_images"):
            return ""
        url = str(url)
        if url.startswith("/"):
            url = "https://www.cc98.org" + url
        return '<img class="avatar" src="%s" alt=""> ' % html.escape(
            P.page_url(self.base, "/img", u=encode_img_url(url)))

    def avatar_html(self, uid):
        """按用户 id 取头像;匿名/无头像/关闭图片时返回空串。"""
        if not (isinstance(uid, int) and uid > 0):
            return ""
        try:
            u = self.api.get_user(uid)
        except ApiError:
            return ""
        return self._avatar_img(u.get("portraitUrl") if isinstance(u, dict) else None)

    def avatar_by_name(self, name):
        """按用户名取头像(热帖等只有名字的场景)。"""
        if not name:
            return ""
        try:
            u = self.api.get_user_by_name(str(name))
        except ApiError:
            return ""
        return self._avatar_img(u.get("portraitUrl") if isinstance(u, dict) else None)

    def item_avatar(self, t):
        """列表条目头像:优先用 userId,只有名字时按名字查。"""
        if not self.cfg.get("show_images") or t.get("isAnonymous"):
            return ""
        uid = t.get("userId") or t.get("authorUserId")
        if isinstance(uid, int) and uid > 0:
            return self.avatar_html(uid)
        name = t.get("userName") or t.get("authorName")
        return self.avatar_by_name(name) if name else ""

    def render_floor(self, p, ctx):
        base = self.base
        # 楼层编号按 CC98 原值:楼主就是第 1 楼,回复依次 2、3、4…
        try:
            floor_no = int(p.get("floor"))
        except (TypeError, ValueError):
            floor_no = None
        if floor_no is None:
            floor_label = "楼层"
        elif floor_no < 1:
            floor_label = "楼主"
        else:
            floor_label = "第 %d 楼" % floor_no
        # 作者名链到用户主页(匿名或没有 userId 时不加链接)
        uid = p.get("userId")
        uname = p.get("userName")
        if uname and not p.get("isAnonymous") and isinstance(uid, int) and uid > 0:
            name_html = '<a href="%s">%s</a>' % (
                html.escape(P.page_url(base, "/user/%s" % uid)),
                P.text(uname, self.emoji_base()))
        else:
            name_html = P.text(uname or ("匿名" if p.get("isAnonymous") else "?"),
                               self.emoji_base())
        tag = '<span class="tag">楼主</span>' if p.get("isLZ") else ""
        avatar = "" if p.get("isAnonymous") else self.avatar_html(uid)
        tm = P.fmt_time(p.get("time"))
        content = render_content(p.get("content"), p.get("contentType", 0), ctx)
        if p.get("isDeleted"):
            content = '<p class="meta">[该楼层已被删除]</p>' + (content or "")
        # 点赞/踩
        foot = []
        like = p.get("likeCount")
        dislike = p.get("dislikeCount")
        if isinstance(like, int) and like > 0:
            foot.append("赞 %d" % like)
        if isinstance(dislike, int) and dislike > 0:
            foot.append("踩 %d" % dislike)
        foot_html = ('<div class="cfoot">%s</div>' % " · ".join(foot)) if foot else ""
        return ('<div class="card"><table class="chead"><tr>'
                '<td>%s%s%s</td>'
                '<td class="cmeta">%s%s</td>'
                '</tr></table>'
                '<div class="cbody">%s</div>%s</div>'
                % (avatar, name_html, tag,
                   floor_label, ("<br>" + html.escape(tm)) if tm else "",
                   content, foot_html))

    def render_user(self, user_id=None, name=None, from_=0):
        cfg, base = self.cfg, self.base
        size = int(cfg.get("topics_per_page", 20))
        if name and not user_id:
            user = self.api.get_user_by_name(name)
        else:
            user = self.api.get_user(user_id)
        if not isinstance(user, dict) or not user.get("name"):
            raise ApiError("用户不存在或无法查看")
        uid = user.get("id") or user_id
        uname = str(user.get("name"))

        parts = [P.nav_bar(base)]
        avatar = ""
        if cfg.get("show_images") and user.get("portraitUrl"):
            avatar = ('<img class="avatar" src="%s" alt=""> '
                      % html.escape(P.page_url(base, "/img",
                                               u=encode_img_url(str(user["portraitUrl"])))))
        parts.append("<h1>%s%s</h1>" % (avatar, P.text(uname, self.emoji_base())))

        info = []
        if user.get("groupName"):
            info.append("用户组 %s" % user["groupName"])
        if user.get("levelTitle"):
            info.append("等级 %s" % user["levelTitle"])
        for key, label in (("prestige", "威望"), ("popularity", "风评"),
                           ("postCount", "发帖"), ("fanCount", "粉丝"),
                           ("receivedLikeCount", "获赞")):
            v = user.get(key)
            if isinstance(v, int):
                info.append("%s %d" % (label, v))
        rt = P.fmt_date(user.get("registerTime"))
        if rt:
            info.append("注册 %s" % rt)
        if info:
            parts.append('<p class="meta">%s</p>' % html.escape(" · ".join(info)))

        intro = user.get("introduction")
        if intro:
            parts.append("<h2>签名</h2>")
            parts.append(render_content(intro, 0, self.ctx()))

        parts.append("<h2>最近发帖</h2>")
        path = "/user/%s" % uid
        try:
            topics = as_list(self.api.get_user_recent_topics(uid, from_, size))
        except ApiError as e:
            topics = []
            parts.append('<p class="meta">%s</p>' % html.escape(e.message))
        parts.append(P.pager(base, path, {}, from_, size,
                             total=None, has_next=len(topics) >= size))
        if topics:
            for t in topics:
                if isinstance(t, dict):
                    parts.append(P.topic_item(base, t, emoji_base=self.emoji_base(),
                                              avatar=self.item_avatar(t)))
        else:
            parts.append('<p class="meta">没有更多内容(或该用户设置了隐私)。</p>')
        parts.append(P.pager(base, path, {}, from_, size,
                             total=None, has_next=len(topics) >= size))
        parts.append(P.footer(base))
        return "".join(parts), ("用户 %s" % uname)


# ==================== HTTP 服务 ====================

def parse_from(query, size):
    try:
        v = int((query.get("from") or ["0"])[0])
    except (TypeError, ValueError):
        v = 0
    v = max(0, v)
    if size > 0 and v % size:
        v = (v // size) * size
    return v


_PIL_HINTED = False

# 1x1 透明 PNG(emoji 渲染失败时的占位,Kindle 上显示为"空"而不是破损图标)
_EMPTY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _looks_like_webp(data, ctype):
    ct = (ctype or "").split(";")[0].strip().lower()
    if ct == "image/webp":
        return True
    return len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _webp_to_jpeg(data, max_width=1000, quality=82):
    """把 WebP 转成 JPEG;顺带缩到 KPW3 屏幕宽度以内省流量。失败返回 None。"""
    import io
    if _PILImage is None:
        return None
    try:
        img = _PILImage.open(io.BytesIO(data))
        try:
            img.seek(0)  # 动图只取第一帧
        except Exception:
            pass
        img.load()
    except Exception:
        return None
    try:
        if img.width > max_width:
            height = max(1, round(img.height * max_width / img.width))
            img = img.resize((max_width, height), _PILImage.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return None


def encode_img_url(url):
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def decode_img_url(u):
    if not u:
        return ""
    pad = "=" * (-len(u) % 4)
    try:
        return base64.urlsafe_b64decode(u + pad).decode("utf-8", "replace")
    except Exception:
        return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cc98-kindle"

    # ---- 响应工具 ----

    def send_bytes(self, code, data, ctype, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def send_page(self, code, html_text):
        self.send_bytes(code, html_text.encode("utf-8"),
                        "text/html; charset=utf-8")

    def send_redirect(self, location):
        """303 跳转:回帖成功后用 GET 打开结果页,避免刷新时重复提交。"""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    # ---- POST(回帖) ----

    def do_POST(self):
        try:
            self.route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                app = self.server.app
                self.send_page(200, P.error_page(app.cfg, app.base,
                                                 "服务器内部错误,请返回重试"))
            except Exception:
                pass

    def route_post(self):
        app = self.server.app
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path or "/")
        base = app.base
        if base:
            if path == base or path == base + "/":
                path = "/"
            elif path.startswith(base + "/"):
                path = path[len(base):]
            else:
                self.send_page(200, P.landing_page(app.cfg))
                return
        if path != "/reply":
            self.send_page(404, P.error_page(app.cfg, base, "页面不存在"))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 200000:
            self.send_page(200, P.error_page(app.cfg, base, "提交的数据异常"))
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(body, keep_blank_values=True)
        topic_id = (form.get("topic") or [""])[0]
        content = (form.get("content") or [""])[0]
        anonymous = bool(form.get("anonymous"))
        if not topic_id.isdigit():
            self.send_page(200, P.error_page(app.cfg, base, "主题参数错误"))
            return
        title = "帖子 %s" % topic_id
        try:
            t = app.api.get_topic(topic_id)
            if isinstance(t, dict) and t.get("title"):
                title = t["title"]
        except ApiError:
            pass
        try:
            app.api.post_reply(topic_id, content, anonymous)
        except (ApiError, NetworkError) as e:
            # 失败时回到表单并保留已输入的内容
            self.send_page(200, P.reply_page(app.cfg, app.base, topic_id, title,
                                             content=content, anonymous=anonymous,
                                             error=e.message))
            return
        # 成功:跳到该帖显示新回复的那一页
        size = int(app.cfg.get("posts_per_page", 10))
        frm = 0
        try:
            t = app.api.get_topic(topic_id)
            total = int((t or {}).get("replyCount") or 0) + 1
            frm = max(0, (total - 1) // size * size)
        except (ApiError, TypeError, ValueError):
            pass
        self.send_redirect("%s/topic/%s?from=%d" % (app.base, topic_id, frm))

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 路由 ----

    def do_GET(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                app = self.server.app
                self.send_page(500, P.error_page(app.cfg, app.base,
                                                 "服务器内部错误,请回到首页重试"))
            except Exception:
                pass

    def route(self):
        app = self.server.app
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path or "/")
        query = urllib.parse.parse_qs(parsed.query)
        base = app.base

        if path == "/favicon.ico":
            self.send_bytes(204, b"", "text/plain")
            return

        if base:
            if path == base or path == base + "/":
                path = "/"
            elif path.startswith(base + "/"):
                path = path[len(base):]
            else:
                self.send_page(200, P.landing_page(app.cfg))
                return

        if path in ("", "/"):
            self.handle_page(app.render_home)
            return
        if path == "/boards":
            gid = (query.get("g") or [None])[0]
            self.handle_page(lambda: app.render_boards(gid))
            return
        if path == "/hot":
            self.handle_page(app.render_hot)
            return

        if path == "/new":
            size = int(app.cfg.get("topics_per_page", 20))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_new(from_))
            return

        if path == "/follow":
            size = int(app.cfg.get("topics_per_page", 20))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_follow(from_))
            return

        if path == "/me":
            size = int(app.cfg.get("topics_per_page", 20))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_me(from_))
            return

        if path == "/me/posts":
            size = int(app.cfg.get("topics_per_page", 20))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_me_posts(from_))
            return

        if path == "/reply":
            tid = (query.get("topic") or [""])[0]
            if not tid.isdigit():
                self.send_page(200, P.error_page(app.cfg, base, "缺少主题参数"))
                return
            title = "帖子 %s" % tid
            try:
                t = app.api.get_topic(tid)
                if isinstance(t, dict) and t.get("title"):
                    title = t["title"]
            except ApiError:
                pass
            self.send_page(200, P.reply_page(app.cfg, app.base, tid, title))
            return

        m = re.match(r"^/board/(\d+)$", path)
        if m:
            size = int(app.cfg.get("topics_per_page", 20))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_board(m.group(1), from_))
            return

        m = re.match(r"^/topic/(\d+)$", path)
        if m:
            size = int(app.cfg.get("posts_per_page", 10))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_topic(m.group(1), from_))
            return

        m = re.match(r"^/user/(\d+)$", path)
        if m:
            size = int(app.cfg.get("topics_per_page", 20))
            from_ = parse_from(query, size)
            self.handle_page(lambda: app.render_user(user_id=m.group(1), from_=from_))
            return

        m = re.match(r"^/user/name/(.+)$", path)
        if m:
            self.handle_page(lambda: app.render_user(name=m.group(1)))
            return

        if path == "/img":
            self.handle_img(query)
            return

        if path == "/emoji":
            self.handle_emoji(query)
            return

        self.send_page(404, P.error_page(app.cfg, base, "页面不存在"))

    def handle_page(self, fn):
        app = self.server.app
        try:
            body, title = fn()
            self.send_page(200, P.page(title, body, app.cfg))
        except AuthError as e:
            hint = ""
            if "config.json" not in e.message:
                hint = "如果账号有变动:请修改 config.json 后重启程序"
            self.send_page(200, P.error_page(app.cfg, app.base, e.message, hint))
        except NetworkError as e:
            self.send_page(200, P.error_page(
                app.cfg, app.base, e.message,
                "请确认电脑在浙大内网(或已连 RVPN/WebVPN)后刷新本页"))
        except ApiError as e:
            self.send_page(200, P.error_page(app.cfg, app.base, e.message))
        except Exception as e:
            traceback.print_exc()
            self.send_page(200, P.error_page(app.cfg, app.base,
                                             "服务器内部错误:%s" % type(e).__name__))

    def handle_emoji(self, query):
        """本地渲染 Unicode emoji 为 PNG(如 /emoji?n=1f62d)。"""
        name = (query.get("n") or [""])[0].lower()
        if not re.fullmatch(r"[0-9a-f]{1,6}(-[0-9a-f]{1,6}){0,7}", name):
            self.send_bytes(400, b"bad name", "text/plain")
            return
        data = cc98_emoji.render_png(name)
        if not data:
            # 渲染失败返回 1x1 透明 PNG,避免 Kindle 显示破损图标
            self.send_bytes(200, _EMPTY_PNG, "image/png", cache=True)
            return
        self.send_bytes(200, data, "image/png", cache=True)

    def handle_img(self, query):
        app = self.server.app
        url = decode_img_url((query.get("u") or [""])[0])
        low = url.lower()
        if not url or not low.startswith(("http://", "https://")):
            self.send_bytes(400, b"bad url", "text/plain")
            return
        max_kb = int(app.cfg.get("image_max_kb", 2048))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "cc98-kindle/1.0", "Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(max_kb * 1024 + 1)
                ctype = resp.headers.get("Content-Type", "image/jpeg")
        except Exception:
            self.send_bytes(404, b"image fetch failed", "text/plain")
            return
        if len(data) > max_kb * 1024:
            self.send_bytes(413, b"image too large", "text/plain")
            return
        # WebP → JPEG:KPW3 老浏览器不支持 WebP,必须转码
        if _looks_like_webp(data, ctype):
            jpeg = _webp_to_jpeg(data)
            if jpeg:
                self.send_bytes(200, jpeg, "image/jpeg", cache=True)
                return
            global _PIL_HINTED
            if _PILImage is None and not _PIL_HINTED:
                _PIL_HINTED = True
                print("[图片] 检测到 WebP 图片但未安装 Pillow,无法转码显示。"
                      "安装方法:python -m pip install pillow")
        self.send_bytes(200, data, ctype, cache=True)


# ==================== 启动 ====================

def print_banner(app, port):
    kind, msg = app.login_state
    print("")
    print("=" * 60)
    print("  CC98 Kindle 阅读器已启动(只读)")
    print("=" * 60)
    print("  登录状态:%s" % msg)
    if _PILImage is None:
        print("  图片转码:Pillow 未安装,WebP 图片将无法显示")
        print("           安装命令:python -m pip install pillow")
    else:
        print("  图片转码:Pillow 已就绪(WebP 自动转 JPEG)")
    if app.emoji_base() is not None:
        print("  表情渲染:emoji 显示为图片(本地渲染,不联网)")
    else:
        print("  表情渲染:emoji 显示为文字标签(需要 Pillow + 系统 emoji 字体)")
    print("")
    print("  在 Kindle 自带浏览器输入以下地址(按顺序试):")
    for i, (ip, label) in enumerate(candidate_ips(), 1):
        print("   %d) http://%s:%d%s/" % (i, ip, port, app.base))
        if label:
            print("      %s" % label)
    print("")
    print("  提示:")
    print("   - Kindle 需和电脑连同一个 Wi-Fi")
    print("   - 首次运行如弹出 Windows 防火墙窗口,请勾选\"专用/公用\"并点\"允许访问\"")
    print("   - 若所有地址都打不开(校园 Wi-Fi 可能禁止设备互访):")
    print("     打开 Windows 设置 > 网络和 Internet > 移动热点,让 Kindle 连电脑热点,")
    print("     再用上面列表里的 %s 地址访问" % HOTSPOT_IP)
    print("   - 停止服务:在本窗口按 Ctrl+C")
    print("=" * 60)
    print("")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    cfg = load_config()
    base = ("/k/" + str(cfg["access_key"])) if cfg.get("access_key") else ""

    api = CC98API(
        username=cfg.get("username"),
        password=cfg.get("password"),
        token_path=str(TOKEN_PATH),
        timeout=cfg.get("request_timeout", 15),
        cache_ttl=cfg.get("cache_ttl", 60),
        log=lambda m: print(m, flush=True),
    )
    app = App(cfg, api, base)
    app.try_login()

    host = cfg.get("host") or "0.0.0.0"
    try:
        port = int(cfg.get("port") or 8000)
    except (TypeError, ValueError):
        port = 8000

    try:
        httpd = KindleHTTPServer((host, port), Handler)
    except OSError as e:
        print("启动失败:无法监听 %s:%d(%s)" % (host, port, e))
        print("端口可能被另一个还在运行的本程序占用 —— 请先关掉那个窗口(或按 Ctrl+C),")
        print("也可以在 config.json 里改一个别的 port。")
        sys.exit(1)
    httpd.app = app

    print_banner(app, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
