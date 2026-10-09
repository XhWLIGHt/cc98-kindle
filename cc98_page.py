#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面渲染:面向 Kindle Paperwhite 3 的内联 CSS、导航、分页与错误页。

KPW3 约束(约 Safari 5 时代 WebKit + 16 级灰度墨水屏):
  - 零 JS、零 flex/grid;布局只用 block 和 table
  - 视口按物理像素上报,字号必须显式放大
  - 不靠颜色/背景色区分信息,靠边框、缩进、字号
  - CSS 内联进每个页面(Kindle 浏览器缓存顽固)
"""

import html
import urllib.parse

from cc98_ubb import replace_emoji


def text(s, emoji_base=None):
    """标题/名称等短文本通用:先转义,再把 emoji 换成图片或文字标签。"""
    return replace_emoji(html.escape(str(s if s is not None else "")), emoji_base)

_CSS = """
html { font-size: __FONT__px; -webkit-text-size-adjust: none; }
body { margin: 0; padding: 0.4em 0.8em 1.6em 0.5em; font-family: serif;
       color: #000; background: #fff; line-height: __LH__; word-wrap: break-word; }
a { color: #000; text-decoration: underline; }
h1, h2, h3 { font-size: 1em; font-weight: normal; margin: 0.8em 0 0.3em 0;
             border-bottom: 2px solid #000; padding-bottom: 0.12em; }
h1 { font-size: 1.12em; }
p { margin: 0.3em 0 0.7em 0; }
hr { border: 0; border-top: 2px solid #000; margin: 0.7em 0; }
/* 顶部导航:加大字号,当前页加粗下划线 */
.nav { border-top: 3px solid #000; border-bottom: 3px solid #000; padding: 0.05em 0;
       margin-bottom: 0.6em; font-size: 1.18em; }
.nav a { display: inline-block; padding: 0.4em 0.85em 0.4em 0; }
.nav-on { display: inline-block; padding: 0.4em 0.85em 0.4em 0;
          border-bottom: 5px solid #000; }
.meta { font-size: 0.78em; }
/* 列表条目:App 式一行一条,左侧头像 */
a.item { display: block; border-bottom: 1px solid #000; padding: 0.55em 0;
         text-decoration: none; }
a.item .t { display: block; font-size: 1.05em; text-decoration: underline; }
a.item .meta { display: block; margin-top: 0.2em; }
table.irow { width: 100%; border-collapse: collapse; }
table.irow td { border: 0; padding: 0; vertical-align: middle; }
td.iav { width: 2.9em; }
/* 楼层卡片:App 卡片式 */
.card { border: 1px solid #000; margin: 0.6em 0; padding: 0.5em 0.65em;
        border-radius: 0.15em; }
.card.thead { margin-top: 0.4em; }
.card.thead h1 { border: 0; margin: 0 0 0.35em 0; padding: 0; }
.card.thead .avatar { max-height: 3em; }
table.chead { width: 100%; border-collapse: collapse; }
table.chead td { border: 0; padding: 0; vertical-align: top; }
.cname { font-size: 1.06em; }
.cmeta { font-size: 0.78em; text-align: right; line-height: 1.45; }
.cmeta .fl { font-size: 1.05em; }
.tag { font-size: 0.72em; border: 1px solid #000; padding: 0 0.25em;
       margin-left: 0.2em; }
.cbody { border-top: 1px solid #000; margin-top: 0.35em; padding-top: 0.35em; }
.cfoot { border-top: 1px dotted #000; margin-top: 0.35em; padding-top: 0.15em;
         font-size: 0.75em; }
blockquote { margin: 0.4em 0 0.4em 0; padding-left: 0.6em;
             border-left: 4px solid #000; font-size: 0.92em; }
pre { white-space: pre-wrap; word-wrap: break-word; border: 1px solid #000;
      padding: 0.3em; font-size: 0.85em; }
code { font-size: 0.9em; }
table { border-collapse: collapse; margin: 0.4em 0; max-width: 100%; }
td, th { border: 1px solid #000; padding: 0.15em 0.4em; }
img.pic { max-width: 100%; }
img.emo { height: 1.25em; vertical-align: middle; }
img.avatar { max-height: 2.2em; vertical-align: middle; margin-right: 0.15em; }
.pager { margin: 0.7em 0 0.2em 0; border-top: 2px solid #000; padding-top: 0.35em;
         width: 100%; }
.pager td { border: 0; padding: 0.3em 0; }
.pager a { display: inline-block; padding: 0.5em 0.6em 0.5em 0;
           font-size: 1.05em; }
.foot { margin-top: 1.2em; border-top: 2px solid #000; padding-top: 0.4em; }
/* 按钮与表单 */
a.btn, button.btn { display: inline-block; font-size: 0.95em; border: 2px solid #000;
        background: #fff; color: #000; padding: 0.4em 1.2em; text-decoration: none;
        font-family: serif; }
textarea { width: 95%; font-size: 0.85em; line-height: 1.5; padding: 0.35em;
        font-family: serif; border: 2px solid #000; }
.err { border: 2px solid #000; padding: 0.45em 0.55em; }
"""


def css(cfg):
    return (_CSS
            .replace("__FONT__", str(int(cfg.get("font_size_px", 34))))
            .replace("__LH__", str(cfg.get("line_height", 1.8))))


def page(title, body, cfg):
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>%s</title>'
        '<style>%s</style></head><body>%s</body></html>'
        % (text(title), css(cfg), body)
    )


def page_url(base, path, **params):
    url = "%s%s" % (base, path)
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if clean:
        url += "?" + urllib.parse.urlencode(clean)
    return url


_NAV_TABS = (
    ("hot", "/", "热门"),
    ("new", "/new", "新帖"),
    ("follow", "/follow", "关注"),
    ("boards", "/boards", "版面"),
    ("me", "/me", "我"),
)


def nav_bar(base, current=""):
    parts = ['<div class="nav">']
    for key, path, label in _NAV_TABS:
        if key == current:
            parts.append('<span class="nav-on">%s</span>' % label)
        else:
            parts.append('<a href="%s%s">%s</a>' % (base, path, label))
    parts.append("</div>")
    return "".join(parts)


def footer(base):
    return ('<div class="foot"><a href="%s/">返回首页</a></div>' % base)


def pager(base, path, params, from_, size, total=None, has_next=None):
    """分页控件。total 已知时显示总页数,否则用 has_next 决定是否给"下一页"。"""
    page_no = from_ // size + 1
    left = ""
    if from_ > 0:
        p = dict(params)
        p["from"] = max(0, from_ - size)
        left = '<a href="%s">← 上一页</a>' % page_url(base, path, **p)
    if total is not None:
        total_pages = max(1, (total + size - 1) // size)
        middle = "第 %d / %d 页" % (page_no, total_pages)
        can_next = from_ + size < total
    else:
        middle = "第 %d 页" % page_no
        can_next = bool(has_next)
    right = ""
    if can_next:
        p = dict(params)
        p["from"] = from_ + size
        right = '<a href="%s">下一页 →</a>' % page_url(base, path, **p)
    return ('<table class="pager"><tr>'
            '<td>%s</td><td align="center">%s</td><td align="right">%s</td>'
            '</tr></table>') % (left, middle, right)


def topic_item(base, t, board_name=None, emoji_base=None, avatar=""):
    """帖子列表里的一条(整行可点,左侧可带头像)。"""
    tid = t.get("id")
    title = t.get("title") or "(无标题)"
    author = t.get("authorName") or t.get("userName") or t.get("lastPostUser")
    if not author:
        author = "匿名" if t.get("isAnonymous") else "?"
    metas = [author]
    rc = t.get("replyCount")
    if rc is not None:
        metas.append("回复 %s" % rc)
    bname = t.get("boardName") or board_name
    if bname:
        metas.append(bname)
    tm = fmt_time(t.get("time") or t.get("createTime"))
    if tm:
        metas.append(tm)
    return ('<a class="item" href="%s">'
            '<table class="irow"><tr>'
            '<td class="iav">%s</td>'
            '<td><span class="t">%s</span><span class="meta">%s</span></td>'
            '</tr></table></a>'
            % (html.escape(page_url(base, "/topic/%s" % tid)),
               avatar,
               text(title, emoji_base),
               html.escape(" · ".join(str(x) for x in metas))))


def board_item(base, b, show_group=False, emoji_base=None):
    metas = []
    if b.get("todayCount") is not None:
        metas.append("今日 %s" % b.get("todayCount"))
    if b.get("topicCount") is not None:
        metas.append("主题 %s" % b.get("topicCount"))
    name = b.get("name") or "(未命名版面)"
    label = ("%s · %s" % (b.get("groupName"), name)) if show_group and b.get("groupName") else name
    return ('<a class="item" href="%s"><span class="t">%s</span>'
            '<span class="meta">%s</span></a>'
            % (html.escape(page_url(base, "/board/%s" % b.get("id"))),
               text(label, emoji_base),
               html.escape(" · ".join(metas))))


def fmt_time(s):
    if not s:
        return ""
    s = str(s).replace("T", " ")
    return s[5:16] if len(s) >= 16 else s


def fmt_date(s):
    """只要日期部分(注册时间等场景需要保留年份)。"""
    if not s:
        return ""
    return str(s)[:10]


def reply_page(cfg, base, topic_id, title, content="", anonymous=False, error=""):
    """回帖表单页(提交到 POST {base}/reply)。"""
    checked = ' checked="checked"' if anonymous else ""
    parts = [nav_bar(base)]
    parts.append('<div class="card thead"><h1>回复:%s</h1></div>' % text(title))
    if error:
        parts.append('<p class="err">%s</p>' % html.escape(error))
    parts.append(
        '<form method="post" action="%s">'
        '<input type="hidden" name="topic" value="%s">'
        '<textarea name="content" rows="10">%s</textarea>'
        '<p><label><input type="checkbox" name="anonymous" value="1"%s>'
        ' 匿名回复</label></p>'
        '<p><button class="btn" type="submit">发表回复</button></p>'
        '</form>'
        % (html.escape(page_url(base, "/reply")), html.escape(str(topic_id)),
           html.escape(content), checked))
    parts.append('<p><a class="btn" href="%s">返回帖子</a></p>'
                 % html.escape(page_url(base, "/topic/%s" % topic_id)))
    return page("回复", "".join(parts), cfg)


def error_page(cfg, base, message, hint=""):
    hint_html = '<p class="meta">%s</p>' % html.escape(hint) if hint else ""
    body = ('<h1>出错了</h1><p>%s</p>%s<p><a href="%s/">返回首页</a></p>'
            % (html.escape(str(message)), hint_html, base))
    return page("出错了", body, cfg)


def notice_page(cfg, base, title, message):
    body = ('<h1>%s</h1><p>%s</p><p><a href="%s/">返回首页</a></p>'
            % (html.escape(title), html.escape(str(message)), base))
    return page(title, body, cfg)


def landing_page(cfg):
    """访问地址没带访问口令时显示的说明页(不泄露口令)。"""
    body = (
        '<h1>CC98 Kindle 阅读器</h1>'
        '<p>本服务需要带访问口令的地址才能打开。</p>'
        '<p>请查看电脑上程序启动时打印的完整网址(形如 '
        '<b>http://电脑IP:端口/k/xxxxxx/</b>),把它输入 Kindle 浏览器。</p>'
    )
    return page("CC98 Kindle 阅读器", body, cfg)
