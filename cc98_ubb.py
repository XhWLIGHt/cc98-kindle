#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UBB / Markdown 正文渲染:把 CC98 帖子正文转成 KPW3 老浏览器可用的极简 HTML。

安全模型:所有文本一律 html.escape,HTML 标签全部由本模块生成,
不做白名单过滤 —— 构造上无法被注入。

UBB 方言参照 ZJU-CC98/Forum 的 Ubb/ 标签处理器。
"""

import base64
import html
import re
import urllib.parse

_TAG_RE = re.compile(r"\[(/?)([A-Za-z][A-Za-z0-9_]*)(?:=([^\]]*))?\]")

_INTERNAL_HOSTS = ("www.cc98.org", "cc98.org", "cc98.zju.edu.cn")
_DEFAULT_HOST = "https://www.cc98.org"

# 带闭合标签、语义可保留的标签
_WRAP_TAGS = {
    "b": ("<b>", "</b>"),
    "i": ("<i>", "</i>"),
    "u": ("<u>", "</u>"),
    "del": ("<del>", "</del>"),
    "quote": ("<blockquote>", "</blockquote>"),
    "quotex": ("<blockquote>", "</blockquote>"),
    "code": ("<pre>", "</pre>"),
    "table": ('<table border="1" cellspacing="0" cellpadding="4">', "</table>"),
    "tr": ("<tr>", "</tr>"),
    "td": ("<td>", "</td>"),
    "th": ("<th>", "</th>"),
}

# 丢弃标签本身、保留内容(墨水屏上没有意义的排版类标签)
_DROP_KEEP = {
    "color", "size", "font", "align", "center", "left", "right",
    "english", "cursor", "needreply", "posteronly", "allowviewer",
    "math", "md",
}

# 所有"已知标签名"——用于判断闭合标签是否合法、向上传导
_ALL_NAMES = (set(_WRAP_TAGS) | set(_DROP_KEEP) | {
    "img", "upload", "url", "code", "noubb", "sandbox",
    "line", "topic", "board", "bili", "bilitext", "video", "audio",
    "user", "pm", "mahjong",
    "ac", "em", "cc98", "tb", "ms",
})

_EMOTICON_RE = re.compile(r"^(ac|em|cc98|tb|ms)([a-z0-9]{0,4})$")
_CLOSE_RE_CACHE = {}

# 表情图片地址规则(取自 CC98 前端打包文件 main-*.js 的 Ubb Emoji 处理器,全部实测存在):
#   [emNN]   → /static/images/em/emNN.gif     NN 两位,0-91
#   [tbNN]   → /static/images/tb/tbNN.png     NN 两位,1-33
#   [msNN]   → /static/images/ms/msNN.png     NN 两位,1-54
#   [acNN]   → /static/images/ac/{id}.png     注意文件名不带 ac 前缀;1-54(个位补零)、1001-1040、2001-2055
#   [CC98n]  → .png 为 15-30、36、37;.gif 为 10-14、31-35(1-9 不存在)
_EMO_BASE = "https://www.cc98.org/static/images/"
_CC98_EMO_PNG = frozenset(list(range(15, 31)) + [36, 37])
_CC98_EMO_GIF = frozenset(list(range(10, 15)) + list(range(31, 36)))


def _emoticon_url(name):
    """表情标签名 → 图片 URL;无法确定有效地址时返回空串。"""
    m = _EMOTICON_RE.match(name)
    if not m:
        return ""
    grp, num = m.group(1), m.group(2)
    if not num.isdigit():
        return ""
    n = int(num)
    if grp == "em" and 0 <= n <= 91:
        return "%sem/em%02d.gif" % (_EMO_BASE, n)
    if grp == "tb" and 1 <= n <= 33:
        return "%stb/tb%02d.png" % (_EMO_BASE, n)
    if grp == "ms" and 1 <= n <= 54:
        return "%sms/ms%02d.png" % (_EMO_BASE, n)
    if grp == "ac" and (1 <= n <= 54 or 1001 <= n <= 1040 or 2001 <= n <= 2055):
        return "%sac/%s.png" % (_EMO_BASE, ("%02d" % n) if n < 10 else n)
    if grp == "cc98":
        if n in _CC98_EMO_PNG:
            return "%sCC98/CC98%d.png" % (_EMO_BASE, n)
        if n in _CC98_EMO_GIF:
            return "%sCC98/CC98%d.gif" % (_EMO_BASE, n)
    return ""


def _emoticon_html(name, ctx):
    """表情标签 → HTML(开开关关的判定都在这)。"""
    if not ctx.show_emoticons:
        return ""
    url = _emoticon_url(name)
    if url:
        src = _local(ctx, "/img?u=" + _b64url(url))
        return '<img class="emo" src="%s" alt="[表情]">' % html.escape(src)
    return "[表情]"


class RenderCtx:
    """渲染上下文:本地路径前缀与各项开关。"""

    __slots__ = ("base", "show_images", "show_image_url", "show_emoticons",
                 "allow_external_links", "board_names", "emoji_base")

    def __init__(self, base="", show_images=False, show_image_url=False,
                 show_emoticons=False, allow_external_links=True, board_names=None,
                 emoji_base=None):
        self.base = base or ""
        self.show_images = bool(show_images)
        self.show_image_url = bool(show_image_url)
        self.show_emoticons = bool(show_emoticons)
        self.allow_external_links = bool(allow_external_links)
        self.board_names = board_names or {}
        # 非空时 Unicode emoji 渲染成图片(如 "/k/xxxx")
        self.emoji_base = emoji_base or None


def render_content(content, content_type, ctx):
    """入口:按 contentType(0=UBB, 1=markdown)渲染正文。"""
    if not content:
        return ""
    try:
        ctype = int(content_type or 0)
    except (TypeError, ValueError):
        ctype = 0
    if ctype == 1:
        return render_markdown(str(content), ctx)
    return render_ubb(str(content), ctx)


# ==================== Unicode emoji → 文字标签 ====================
# Kindle 系统没有任何 emoji 字体,彩色 emoji 字符会渲染成空框,
# 这里统一转换成一两个字的方括号标签(如 😭 → [大哭])。

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # 主表情区(笑脸/手势/动物/食物/国旗/符号…)
    (0x2600, 0x27BF),    # 杂项符号与装饰(⚠ ✅ ✂ ☀ ☁ ☕ …)
    (0x2B00, 0x2BFF),    # ⭐ ⭕ ⬛ ⬜ …
    (0x231A, 0x231B), (0x23E9, 0x23FA),  # ⌚ ⌛ ⏰ ⏳ …
)
_EMOJI_KEEP = frozenset(range(0x2660, 0x2670))  # ♠♥♦♣ ♪♫ 老设备字体有,保持原样
_EMOJI_MODIFIERS = "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF"
_FLAG_BASE = (0x1F1E6, 0x1F1FF)

_FLAG_LABELS = {
    "\U0001F1E8\U0001F1F3": "[中国]", "\U0001F1FA\U0001F1F8": "[美国]",
    "\U0001F1EF\U0001F1F5": "[日本]", "\U0001F1EC\U0001F1E7": "[英国]",
    "\U0001F1F0\U0001F1F7": "[韩国]", "\U0001F1E9\U0001F1EA": "[德国]",
    "\U0001F1EB\U0001F1F7": "[法国]", "\U0001F1F7\U0001F1FA": "[俄罗斯]",
}

_EMOJI_LABELS = {
    # 表情脸
    "\U0001F600": "[开心]", "\U0001F603": "[开心]", "\U0001F604": "[大笑]",
    "\U0001F601": "[大笑]", "\U0001F606": "[大笑]", "\U0001F605": "[尬笑]",
    "\U0001F602": "[笑哭]", "\U0001F923": "[笑哭]", "\U0001F60A": "[微笑]",
    "\U0001F607": "[天使]", "\U0001F642": "[微笑]", "\U0001F643": "[倒脸]",
    "\U0001F609": "[眨眼]", "\U0001F60C": "[安心]", "\U0001F60D": "[喜欢]",
    "\U0001F970": "[喜欢]", "\U0001F618": "[亲亲]", "\U0001F60B": "[馋]",
    "\U0001F61B": "[吐舌]", "\U0001F61C": "[吐舌]", "\U0001F61D": "[吐舌]",
    "\U0001F92A": "[疯狂]", "\U0001F928": "[挑眉]", "\U0001F9D0": "[斟酌]",
    "\U0001F913": "[学霸]", "\U0001F60E": "[酷]", "\U0001F973": "[庆祝]",
    "\U0001F60F": "[得意]", "\U0001F612": "[无语]", "\U0001F61E": "[失望]",
    "\U0001F614": "[失落]", "\U0001F61F": "[担心]", "\U0001F615": "[疑惑]",
    "\U0001F641": "[不悦]", "\U0001F62E": "[惊讶]",
    "\U0001F626": "[忍耐]", "\U0001F627": "[纠结]", "\U0001F62B": "[疲惫]",
    "\U0001F629": "[疲惫]", "\U0001F97A": "[可怜]", "\U0001F622": "[流泪]",
    "\U0001F62D": "[大哭]", "\U0001F624": "[生气]", "\U0001F620": "[生气]",
    "\U0001F621": "[愤怒]", "\U0001F92C": "[愤怒]", "\U0001F92F": "[爆炸头]",
    "\U0001F633": "[脸红]", "\U0001F975": "[热]", "\U0001F976": "[冷]",
    "\U0001F631": "[惊恐]", "\U0001F628": "[害怕]", "\U0001F630": "[冷汗]",
    "\U0001F625": "[担心]", "\U0001F613": "[冷汗]", "\U0001F917": "[拥抱]",
    "\U0001F914": "[思考]", "\U0001F92D": "[偷笑]", "\U0001F92B": "[嘘]",
    "\U0001F925": "[说谎]", "\U0001F636": "[无言]", "\U0001F610": "[无语]",
    "\U0001F611": "[无语]", "\U0001F62C": "[尴尬]", "\U0001F644": "[白眼]",
    "\U0001F62F": "[惊讶]", "\U0001F632": "[惊讶]", "\U0001F971": "[哈欠]",
    "\U0001F634": "[睡着]", "\U0001F924": "[流口水]", "\U0001F62A": "[困]",
    "\U0001F635": "[晕]", "\U0001F910": "[闭嘴]", "\U0001F974": "[醉]",
    "\U0001F922": "[恶心]", "\U0001F92E": "[呕吐]", "\U0001F927": "[打喷嚏]",
    "\U0001F637": "[口罩]", "\U0001F912": "[生病]", "\U0001F915": "[受伤]",
    "\U0001F920": "[牛仔]",
    # 手势
    "\U0001F44D": "[赞]", "\U0001F44E": "[踩]", "\U0001F44C": "[OK]",
    "\U0001F596": "[瓦肯]", "\U0001F91E": "[祝福]", "\U0001F91F": "[爱你]",
    "\U0001F918": "[摇滚]", "\U0001F919": "[打call]", "\U0001F448": "[左]",
    "\U0001F449": "[右]", "\U0001F446": "[上]", "\U0001F447": "[下]",
    "\U0001F44B": "[挥手]", "\U0001F91D": "[握手]", "\U0001F64F": "[合十]",
    "\U0001F4AA": "[加油]", "\U0001F44F": "[鼓掌]", "\U0001F64C": "[欢呼]",
    "\U0001F450": "[张开手]", "\U0001F932": "[捧]", "\U0001F590": "[举手]",
    "\U0000270B": "[举手]", "\U0001F91A": "[举手]",
    # 爱心
    "❤": "[爱心]", "❤️": "[爱心]", "\U0001F9E1": "[橙心]",
    "\U0001F49B": "[黄心]", "\U0001F49A": "[绿心]", "\U0001F499": "[蓝心]",
    "\U0001F49C": "[紫心]", "\U0001F5A4": "[黑心]", "\U0001F90D": "[白心]",
    "\U0001F494": "[心碎]", "❣️": "[爱心]", "\U0001F495": "[爱心]",
    "\U0001F49E": "[爱心]", "\U0001F493": "[心跳]", "\U0001F497": "[爱心]",
    "\U0001F496": "[爱心]", "\U0001F498": "[爱心]", "\U0001F49D": "[爱心]",
    # 符号
    "\U0001F525": "[火]", "⭐": "[星]", "\U0001F31F": "[星]",
    "✨": "[闪光]", "⚡": "[闪电]", "\U0001F4A5": "[爆炸]",
    "\U0001F4AF": "[满分]", "\U0001F389": "[庆祝]", "\U0001F38A": "[庆祝]",
    "\U0001F381": "[礼物]", "\U0001F3C6": "[奖杯]", "\U0001F947": "[金牌]",
    "✅": "[对]", "✔": "[对]",
    "✔️": "[对]", "❌": "[错]", "❎": "[错]",
    "❗": "[感叹]", "❓": "[疑问]", "⚠": "[警告]",
    "⚠️": "[警告]", "\U0001F4A4": "[睡觉]", "\U0001F4A2": "[生气]",
    "\U0001F4AC": "[对话]", "\U0001F4AD": "[想法]", "⭕": "[圈]",
    "\U0001F6AB": "[禁止]", "♻️": "[回收]", "\U0001F51E": "[禁止]",
    "\U0001F4E3": "[喇叭]", "\U0001F4A1": "[灯泡]", "\U0001F511": "[钥匙]",
    "\U0001F512": "[锁]", "\U0001F513": "[开锁]", "\U0001F50D": "[搜索]",
    "\U0001F4CC": "[图钉]", "\U0001F4CE": "[回形针]", "\U0001F4B0": "[钱]",
    "\U0001F4B5": "[钱]", "\U0001F4B4": "[钱]", "\U0001F4B6": "[钱]",
    "\U0001F4B7": "[钱]", "\U0001F4B3": "[卡]", "\U0001F48E": "[钻石]",
    "⌚": "[手表]", "⏰": "[闹钟]", "⏳": "[沙漏]",
    # 设备与生活
    "\U0001F4F1": "[手机]", "\U0001F4BB": "[电脑]", "⌨️": "[键盘]",
    "\U0001F5A5": "[电脑]", "\U0001F4F7": "[相机]", "\U0001F4F9": "[摄像机]",
    "\U0001F3A5": "[摄影机]", "\U0001F3B5": "[音乐]", "\U0001F3B6": "[音乐]",
    "\U0001F3A4": "[麦克风]", "\U0001F3A7": "[耳机]", "\U0001F4DA": "[书]",
    "\U0001F4D6": "[书]", "✏️": "[铅笔]", "\U0001F4DD": "[笔记]",
    "\U0001F4C4": "[文件]", "\U0001F4CA": "[图表]", "\U0001F4C8": "[上涨]",
    "\U0001F4C9": "[下跌]", "\U0001F4C5": "[日历]", "\U0001F3EB": "[学校]",
    "\U0001F3E0": "[家]", "\U0001F3E2": "[大楼]", "\U0001F697": "[汽车]",
    "\U0001F695": "[出租车]", "\U0001F68C": "[公交]", "\U0001F6B2": "[自行车]",
    "⚽": "[足球]", "\U0001F3C0": "[篮球]", "\U0001F3D3": "[乒乓]",
    "\U0001F3F8": "[羽毛球]", "\U0001F3AE": "[游戏]", "\U0001F3B2": "[骰子]",
    "\U0001F3C3": "[跑步]", "\U0001F6B6": "[走路]", "\U0001F3CA": "[游泳]",
    # 食物
    "\U0001F35A": "[饭]", "\U0001F35C": "[面]", "\U0001F354": "[汉堡]",
    "\U0001F355": "[披萨]", "\U0001F35F": "[薯条]", "\U0001F357": "[鸡腿]",
    "\U0001F356": "[肉]", "\U0001F34E": "[苹果]", "\U0001F34A": "[橘子]",
    "\U0001F349": "[西瓜]", "\U0001F347": "[葡萄]", "\U0001F353": "[草莓]",
    "\U0001F34C": "[香蕉]", "\U0001F34B": "[柠檬]", "\U0001F351": "[桃子]",
    "\U0001F352": "[樱桃]", "\U0001F95D": "[猕猴桃]", "\U0001F370": "[蛋糕]",
    "\U0001F382": "[蛋糕]", "\U0001F366": "[冰淇淋]", "\U0001F36C": "[糖]",
    "\U0001F36B": "[巧克力]", "\U0001F37F": "[爆米花]", "\U0001F37A": "[啤酒]",
    "\U0001F37B": "[干杯]", "☕": "[咖啡]", "\U0001F375": "[茶]",
    "\U0001F95B": "[牛奶]", "\U0001F376": "[酒]", "\U0001F377": "[红酒]",
    "\U0001F379": "[饮料]", "\U0001F942": "[干杯]", "\U0001F943": "[酒]",
    # 动物
    "\U0001F436": "[狗]", "\U0001F431": "[猫]", "\U0001F42D": "[鼠]",
    "\U0001F439": "[仓鼠]", "\U0001F430": "[兔]", "\U0001F98A": "[狐狸]",
    "\U0001F43B": "[熊]", "\U0001F43C": "[熊猫]", "\U0001F428": "[考拉]",
    "\U0001F42F": "[老虎]", "\U0001F981": "[狮子]", "\U0001F42E": "[牛]",
    "\U0001F437": "[猪]", "\U0001F438": "[青蛙]", "\U0001F435": "[猴]",
    "\U0001F648": "[捂眼]", "\U0001F649": "[捂耳]", "\U0001F64A": "[捂嘴]",
    "\U0001F414": "[鸡]", "\U0001F427": "[企鹅]", "\U0001F426": "[鸟]",
    "\U0001F424": "[小鸡]", "\U0001F986": "[鸭子]", "\U0001F985": "[鹰]",
    "\U0001F989": "[猫头鹰]", "\U0001F434": "[马]", "\U0001F984": "[独角兽]",
    "\U0001F41D": "[蜜蜂]", "\U0001F41B": "[虫]", "\U0001F98B": "[蝴蝶]",
    "\U0001F40C": "[蜗牛]", "\U0001F41E": "[瓢虫]", "\U0001F422": "[乌龟]",
    "\U0001F40D": "[蛇]", "\U0001F419": "[章鱼]", "\U0001F980": "[螃蟹]",
    "\U0001F420": "[鱼]", "\U0001F41F": "[鱼]", "\U0001F42C": "[海豚]",
    "\U0001F433": "[鲸]", "\U0001F988": "[鲨鱼]", "\U0001F40A": "[鳄鱼]",
    "\U0001F418": "[大象]", "\U0001F992": "[长颈鹿]", "\U0001F42A": "[骆驼]",
    "\U0001F411": "[羊]", "\U0001F410": "[山羊]", "\U0001F98C": "[鹿]",
    "\U0001F415": "[狗]", "\U0001F408": "[猫]", "\U0001F413": "[鸡]",
    "\U0001F54A": "[鸽]", "\U0001F407": "[兔]", "\U0001F994": "[刺猬]",
    # 天气与自然
    "☀️": "[晴天]", "☀": "[晴天]", "\U0001F31E": "[晴天]",
    "⛅": "[多云]", "☁️": "[阴天]", "☁": "[阴天]",
    "\U0001F327": "[下雨]", "⛈️": "[雷雨]", "❄️": "[雪]",
    "❄": "[雪]", "☃️": "[雪人]", "⛄": "[雪人]",
    "\U0001F308": "[彩虹]", "☂️": "[伞]", "☔": "[雨伞]",
    "\U0001F4A7": "[水]", "\U0001F30A": "[海浪]", "\U0001F319": "[月亮]",
    "\U0001F315": "[满月]", "\U0001F31D": "[月亮]",
    # 其它常见
    "\U0001F47B": "[鬼]", "\U0001F480": "[骷髅]", "☠️": "[骷髅]",
    "\U0001F4A9": "[大便]", "\U0001F916": "[机器人]", "\U0001F47D": "[外星人]",
    "\U0001F47E": "[外星怪]", "\U0001F383": "[南瓜]", "\U0001F9DF": "[僵尸]",
    "\U0001F9D9": "[巫师]", "\U0001F9B8": "[英雄]",
    "\U0001F340": "[四叶草]", "\U0001F339": "[玫瑰]", "\U0001F338": "[花]",
    "\U0001F337": "[郁金香]", "\U0001F33B": "[向日葵]", "\U0001F33C": "[花]",
    "\U0001F490": "[花束]", "\U0001F384": "[圣诞树]", "\U0001F393": "[毕业]",
    "\U0001F451": "[皇冠]", "\U0001F48D": "[戒指]", "\U0001F457": "[裙子]",
    "\U0001F454": "[衬衫]", "\U0001F45F": "[鞋]", "\U0001F9E2": "[帽子]",
    "\U0001F453": "[眼镜]", "\U0001F576": "[墨镜]", "\U0001F484": "[口红]",
    "\U0001F45C": "[包]", "\U0001F4E6": "[包裹]", "\U0001F4E8": "[邮件]",
    "✈️": "[飞机]", "\U0001F680": "[火箭]", "\U0001F388": "[气球]",
    "\U0001F386": "[烟花]", "\U0001F387": "[烟花]", "\U0001F56F": "[蜡烛]",
    "\U0001F526": "[手电]", "\U0001F9E8": "[鞭炮]", "\U0001F9E7": "[红包]",
    "\U0001F3EE": "[灯笼]", "\U0001F96E": "[月饼]", "\U0001F361": "[丸子]",
    # 时间/其它符号补充
    "⌛": "[沙漏]", "\U0001F4E2": "[喇叭]", "\U0001F4A3": "[炸弹]",
    "\U0001F52B": "[枪]", "\U0001F6B4": "[骑车]",
    # ZWJ 组合的组成部分(取第一成分时用到)
    "\U0001F468": "[男]", "\U0001F469": "[女]", "\U0001F466": "[男孩]",
    "\U0001F467": "[女孩]", "\U0001F9D1": "[人]", "\U0001F9B0": "[红发]",
    "\U0001F9B1": "[卷发]", "\U0001F9D4": "[胡须]", "\U0001F474": "[老人]",
    "\U0001F475": "[老奶奶]", "\U0001F476": "[婴儿]", "\U0001F9D2": "[小孩]",
    "\U0001F199": "[UP]", "\U0001F51D": "[TOP]", "\U0001F6A8": "[警车]",
    "\U0001F46A": "[一家人]",
    # 常见 ZWJ 组合(整串匹配,键里保留 ZWJ)
    "\U0001F468‍\U0001F469‍\U0001F467": "[一家人]",
    "\U0001F468‍\U0001F469‍\U0001F466": "[一家人]",
    "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466": "[一家人]",
    "\U0001F469‍\U0001F469‍\U0001F466": "[一家人]",
    "\U0001F468‍\U0001F468‍\U0001F466": "[一家人]",
    "\U0001F468‍\U00002695️": "[男医生]",
    "\U0001F469‍\U00002695️": "[女医生]",
    "\U0001F468‍\U0001F3A4": "[男歌手]",
    "\U0001F469‍\U0001F3A4": "[女歌手]",
    "\U0001F468‍\U0001F4BB": "[程序员]",
    "\U0001F469‍\U0001F4BB": "[女程序员]",
    "\U0001F468‍\U0001F393": "[男学生]",
    "\U0001F469‍\U0001F393": "[女学生]",
    "\U0001F468‍\U0001F373": "[厨师]",
    "\U0001F469‍\U0001F373": "[女厨师]",
    "\U0001F468‍\U0001F680": "[宇航员]",
    "\U0001F469‍\U0001F680": "[女宇航员]",
    "\U0001F46B": "[情侣]", "\U0001F46D": "[两个女生]", "\U0001F46C": "[两个男生]",
    "\U0001F491": "[情侣]", "\U0001F48F": "[结婚]",
}


# 统一化:字典键去掉变体选择符(U+FE0F/U+FE0E),与查找时的规范化一致
_EMOJI_LABELS = {k.replace("\ufe0f", "").replace("\ufe0e", ""): v
                 for k, v in _EMOJI_LABELS.items()}


def _is_emoji_char(ch):
    o = ord(ch)
    if o in _EMOJI_KEEP:
        return False
    for a, b in _EMOJI_RANGES:
        if a <= o <= b:
            return True
    return False


def _emoji_label(cluster):
    key = cluster.replace("️", "").replace("︎", "")
    key = "".join(c for c in key if c not in _EMOJI_MODIFIERS)
    if len(key) == 2 and all(_FLAG_BASE[0] <= ord(c) <= _FLAG_BASE[1] for c in key):
        return _FLAG_LABELS.get(key, "[国旗]")
    if key in _EMOJI_LABELS:
        return _EMOJI_LABELS[key]
    if key and key[0] in _EMOJI_LABELS:
        return _EMOJI_LABELS[key[0]]  # ZWJ 组合(一家人/职业等)取第一个成分
    return "[表情]"


def plain_text(content, limit=80):
    """把 UBB/Markdown 正文压成一行纯文本摘要(列表用)。"""
    s = str(content or "")
    s = re.sub(r"\[quote(?:x)?\].*?\[/quote(?:x)?\]", " ", s, flags=re.S | re.I)
    s = re.sub(r"\[/?(?:b|i|u|del|color|size|font|align|center|left|right|url|img|line"
               r"|english|math|md|user|pm|needreply|posteronly|allowviewer|cursor"
               r"|table|tr|td|th|upload)(?:=[^\]]*)?\]", " ", s, flags=re.I)
    s = re.sub(r"\[(?:ac|em|cc98|tb|ms)[a-z0-9]{0,4}\]", "", s, flags=re.I)
    s = replace_emoji(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def _scan_emoji(text, emit):
    """扫描所有 emoji 簇,逐一交给 emit(cluster) 生成替换内容。"""
    if not text:
        return text
    # 键帽序列 1️⃣ → [1](U+20E3 组合键帽)
    text = re.sub(r"([0-9#*])️?⃣", r"[\1]", text)
    if not any(_is_emoji_char(c) for c in text):
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if _is_emoji_char(ch):
            j = i + 1
            # 国旗 = 两个连续的区域指示符
            if (_FLAG_BASE[0] <= ord(ch) <= _FLAG_BASE[1] and j < n
                    and _FLAG_BASE[0] <= ord(text[j]) <= _FLAG_BASE[1]):
                j += 1
            while j < n:
                c = text[j]
                if c in ("️", "︎") or c in _EMOJI_MODIFIERS:
                    j += 1
                elif c == "‍" and j + 1 < n:  # ZWJ 组合
                    j += 2
                else:
                    break
            out.append(emit(text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _emoji_key(cluster):
    """emoji 簇 → 码点名(去掉变体选择符/ZWJ/肤色修饰/键帽符)。"""
    keep = []
    for c in cluster:
        o = ord(c)
        if o in (0xFE0F, 0xFE0E, 0x200D, 0x20E3):
            continue
        if 0x1F3FB <= o <= 0x1F3FF:
            continue
        keep.append("%x" % o)
    return "-".join(keep)


def _emoji_render(cluster, base=None):
    """一个 emoji 簇 → HTML。base 为本地路径前缀时输出图片,否则输出文字标签。"""
    if base:
        key = _emoji_key(cluster)
        if key:
            return ('<img class="emo" src="%s/emoji?n=%s" alt="%s">'
                    % (base, key, html.escape(_emoji_label(cluster))))
    return _emoji_label(cluster)


def replace_emoji(text, base=None):
    """把所有 Unicode emoji 换成图片(base 非空)或文字标签。"""
    return _scan_emoji(text, lambda c: _emoji_render(c, base))


# ==================== UBB ====================

def _has_close(name, s, pos):
    rx = _CLOSE_RE_CACHE.get(name)
    if rx is None:
        rx = re.compile(r"\[/%s\]" % re.escape(name), re.IGNORECASE)
        _CLOSE_RE_CACHE[name] = rx
    return rx.search(s, pos) is not None


def _parse(s, pos=0, end_tag=None):
    """递归下降解析 UBB,返回 (nodes, pos, closed_by)。

    nodes 元素:
      ("t", 文本)                     原文,渲染时转义
      ("el", 标签名, 属性, 子节点)    普通标签
      ("self", 标签名, 属性)           自闭合标签
      ("img", 属性, url)              图片/附件
      ("link", href属性, 子节点)       链接

    容错原则:未闭合的标签一律按"自闭合"处理,绝不吞掉后文。
    """
    nodes = []
    n = len(s)
    while pos < n:
        idx = s.find("[", pos)
        if idx < 0:
            if pos < n:
                nodes.append(("t", s[pos:]))
            return nodes, n, None
        if idx > pos:
            nodes.append(("t", s[pos:idx]))
        m = _TAG_RE.match(s, idx)
        if not m:
            nodes.append(("t", "["))
            pos = idx + 1
            continue
        closing, name, attr = m.group(1), (m.group(2) or "").lower(), m.group(3)
        pos = m.end()

        # ---- 闭合标签 ----
        if closing:
            if end_tag is not None and (name == end_tag or name in _ALL_NAMES):
                return nodes, pos, name
            nodes.append(("t", m.group(0)))
            continue

        # ---- 开标签 ----
        if name == "noubb":
            end = s.lower().find("[/noubb]", pos)
            if end < 0:
                nodes.append(("t", s[pos:]))
                return nodes, n, None
            nodes.append(("t", s[pos:end]))
            pos = end + 8
            continue

        if name == "sandbox":  # 丢弃内部全部内容
            end = s.lower().find("[/sandbox]", pos)
            if end >= 0:
                pos = end + 10
            else:
                end2 = s.find("[/sandbox", pos)
                pos = end2 if end2 >= 0 else n
            continue

        if name in ("code",):
            end = s.lower().find("[/code]", pos)
            if end >= 0:
                inner = s[pos:end]
                pos = end + 7
            else:
                inner = s[pos:]
                pos = n
            nodes.append(("el", "code", attr, [("t", inner)]))
            continue

        if name in ("img", "upload"):
            end = s.lower().find("[/%s]" % name, pos)
            if end >= 0:
                inner = s[pos:end]
                pos = end + len(name) + 3
            else:
                # 无闭合的 [upload]url 变体:吃一个不含空白/[ 的连续串
                m2 = re.match(r"[^\s\[]+", s[pos:])
                inner = m2.group(0) if m2 else ""
                pos += len(inner)
            nodes.append(("img", attr, inner))
            continue

        if _EMOTICON_RE.match(name):  # [ac01] [em02] 等表情,永远自闭合
            nodes.append(("self", name, attr))
            continue

        if name == "url":
            if _has_close("url", s, pos):
                children, pos, closed = _parse(s, pos, "url")
                nodes.append(("link", attr, children))
                if closed is not None and closed != "url":
                    if end_tag is not None:
                        return nodes, pos, closed
                    nodes.append(("t", "[/%s]" % closed))
            else:
                m2 = re.match(r"[^\s\[]+", s[pos:])
                inner = m2.group(0) if m2 else ""
                pos += len(inner)
                nodes.append(("link", attr, [("t", inner)]))
            continue

        if name not in _ALL_NAMES:
            nodes.append(("t", m.group(0)))  # 未知标签原样显示
            continue

        if not _has_close(name, s, pos):
            nodes.append(("self", name, attr))
            continue

        children, pos, closed = _parse(s, pos, name)
        nodes.append(("el", name, attr, children))
        if closed is not None and closed != name:
            if end_tag is not None:
                return nodes, pos, closed  # 被更外层的闭合标签终结,向上传导
            nodes.append(("t", "[/%s]" % closed))  # 顶层:多余闭合标签当文本,继续解析
            continue
    return nodes, pos, None


def render_ubb(text, ctx):
    text = (text or "").replace("\x00", "")
    nodes, _, _ = _parse(text, 0, None)
    return "".join(_render_node(node, ctx) for node in nodes)


def _render_node(node, ctx):
    kind = node[0]
    if kind == "t":
        return replace_emoji(html.escape(node[1]), ctx.emoji_base).replace("\n", "<br>")
    if kind == "self":
        return _render_self(node[1], node[2], ctx)
    if kind == "img":
        return _render_img(node[1], node[2], ctx)
    if kind == "link":
        return _render_link(node[1], node[2], ctx)
    # ("el", ...)
    name, attr, children = node[1], node[2], node[3]
    open_html, close_html = _WRAP_TAGS.get(name, ("", ""))
    inner = "".join(_render_node(c, ctx) for c in children)
    if not inner:
        return ""
    return open_html + inner + close_html


def _render_self(name, attr, ctx):
    attr = (attr or "").strip()
    if _EMOTICON_RE.match(name):
        return _emoticon_html(name, ctx)
    if name == "line":
        return "<hr>"
    if name == "topic":
        if not attr.isdigit():
            return ""
        label = "帖子 %s" % attr
        return '<a href="%s">%s</a>' % (html.escape(_local(ctx, "/topic/%s" % attr)), label)
    if name == "board":
        if not attr.isdigit():
            return ""
        label = ctx.board_names.get(attr) or ("版面 %s" % attr)
        return '<a href="%s">%s</a>' % (html.escape(_local(ctx, "/board/%s" % attr)), html.escape(label))
    if name in ("bili", "video"):
        return "[视频]"
    if name == "audio":
        return "[音频]"
    if name == "user":
        if not attr:
            return ""
        label = html.escape("@" + attr)
        if attr.isdigit():
            target = _local(ctx, "/user/%s" % attr)
        else:
            target = _local(ctx, "/user/name/" + urllib.parse.quote(attr))
        return '<a href="%s">%s</a>' % (html.escape(target), label)
    if name == "pm":
        return html.escape("@" + attr) if attr else ""
    return ""  # 其余自闭合标签(needreply 等)直接丢弃


def _render_img(attr, url, ctx):
    url = _normalize_url((url or "").strip())
    if not url:
        return ""
    if ctx.show_images and _fetchable(url):
        src = _local(ctx, "/img?u=" + _b64url(url))
        return '<img class="pic" src="%s" alt="[图片]">' % html.escape(src)
    if ctx.show_image_url and _fetchable(url):
        return '<a href="%s">[图片]</a>' % html.escape(url)
    return "[图片]"


def _render_link(attr, children, ctx):
    href = (attr or "").strip().strip('"').strip("'")
    inner_html = "".join(_render_node(c, ctx) for c in children)
    if not href:
        href = _plain_text(children).strip()
        inner_html = ""
    if not href:
        return inner_html
    kind, target = _rewrite_link(href, ctx)
    label = inner_html or html.escape(href)
    if kind == "plain":
        return inner_html or html.escape(href)
    if kind == "external" and not ctx.allow_external_links:
        return '%s <span class="meta">(%s)</span>' % (label, html.escape(href))
    return '<a href="%s">%s</a>' % (html.escape(target), label)


def _rewrite_link(href, ctx):
    """返回 (kind, target):kind ∈ internal / external / plain。"""
    href = href.strip().strip('"').strip("'")
    if not href:
        return "plain", ""
    low = href.lower()
    for bad in ("javascript:", "data:", "vbscript:", "file:"):
        if low.startswith(bad):
            return "plain", ""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        m = re.match(r"^/(topic|board)/(\d+)", href)
        if m:
            return "internal", _local(ctx, "/%s/%s" % (m.group(1), m.group(2)))
        return "plain", ""  # 其他站内路径本服务没有,降级为纯文本
    parts = urllib.parse.urlsplit(href)
    host = (parts.hostname or "").lower()
    if host in _INTERNAL_HOSTS:
        m = re.match(r"^/(topic|board)/(\d+)", parts.path)
        if m:
            return "internal", _local(ctx, "/%s/%s" % (m.group(1), m.group(2)))
        return "plain", ""
    if parts.scheme in ("http", "https") and parts.netloc:
        return "external", href
    return "plain", ""


def _local(ctx, path):
    return ctx.base + path


def _normalize_url(url):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return _DEFAULT_HOST + url
    return url


def _fetchable(url):
    return url.lower().startswith(("http://", "https://"))


def _b64url(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _plain_text(nodes):
    out = []
    for node in nodes:
        kind = node[0]
        if kind == "t":
            out.append(node[1])
        elif kind == "el":
            out.append(_plain_text(node[3]))
        elif kind == "link":
            out.append(_plain_text(node[2]))
        elif kind == "img":
            out.append(node[2])
    return "".join(out)


# ==================== Markdown(contentType=1) ====================

_MD_RAW_HTML_RE = re.compile(r"<[^>]{0,200}>")
_MD_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")
_MD_HR_RE = re.compile(r"^\s{0,3}([-*_])\s?(\1\s?){2,}$")
_MD_UL_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_MD_OL_RE = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+(.*)$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def render_markdown(text, ctx):
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    para = []
    i = 0
    n = len(lines)

    def flush_para():
        if para:
            out.append("<p>%s</p>" % "<br>".join(para))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 围栏代码块
        if stripped.startswith("```"):
            flush_para()
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结束围栏(或 EOF)
            out.append("<pre>%s</pre>" % html.escape("\n".join(code_lines)))
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        if _MD_HR_RE.match(line):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            out.append("<h3>%s</h3>" % _md_inline(m.group(2), ctx))
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            flush_para()
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>"
                       % "<br>".join(_md_inline(q, ctx) for q in quote_lines))
            continue

        # 表格(表头 + 分隔行)
        if "|" in line and i + 1 < n and _MD_TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            header = _md_table_cells(line)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_md_table_cells(lines[i]))
                i += 1
            parts = ["<table border=\"1\" cellspacing=\"0\" cellpadding=\"4\"><tr>"]
            parts += ["<th>%s</th>" % _md_inline(c, ctx) for c in header]
            parts.append("</tr>")
            for row in rows:
                parts.append("<tr>")
                parts += ["<td>%s</td>" % _md_inline(c, ctx) for c in row]
                parts.append("</tr>")
            parts.append("</table>")
            out.append("".join(parts))
            continue

        # 列表
        if _MD_UL_RE.match(line) or _MD_OL_RE.match(line):
            flush_para()
            ordered = bool(_MD_OL_RE.match(line))
            parts = ["<ol>" if ordered else "<ul>"]
            while i < n:
                m2 = (_MD_OL_RE if ordered else _MD_UL_RE).match(lines[i])
                if not m2:
                    break
                parts.append("<li>%s</li>" % _md_inline(m2.group(1), ctx))
                i += 1
            parts.append("</ol>" if ordered else "</ul>")
            out.append("".join(parts))
            continue

        # 普通段落(连续非空行合并,保留软换行)
        para.append(_md_inline(line, ctx))
        i += 1

    flush_para()
    return "".join(out)


def _md_table_cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _md_inline(raw, ctx):
    """行内元素:先摘出代码/图片/链接,再转义,再处理强调,最后还原。"""
    raw = raw.replace("\x00", "")
    # 抹掉原生 HTML 标签(markdown 帖子里偶有 <br> 等,直接丢弃)
    raw = _MD_RAW_HTML_RE.sub("", raw)

    store = []

    def stash(html_str):
        store.append(html_str)
        return "\x00%d\x00" % (len(store) - 1)

    # 行内代码(先摘出,保护里面的表情标签不被替换)
    raw = re.sub(r"`([^`]+)`",
                 lambda m: stash("<code>%s</code>" % html.escape(m.group(1))), raw)
    # Unicode emoji → 图片/文字标签(代码段之外的)
    raw = _scan_emoji(raw, lambda c: stash(_emoji_render(c, ctx.emoji_base)))
    # UBB 表情标签(代码段之外的)
    raw = re.sub(r"\[((?:ac|em|cc98|tb|ms)[a-z0-9]{0,4})\]",
                 lambda m: stash(_emoticon_html(m.group(1), ctx)), raw)
    # 图片
    raw = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)",
                 lambda m: stash(_render_img(None, m.group(2), ctx)), raw)
    # 链接
    raw = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                 lambda m: stash(_md_link_html(m.group(1), m.group(2), ctx)), raw)

    # 余下文本转义
    raw = html.escape(raw, quote=False)

    # 强调
    raw = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", raw)
    raw = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", raw)
    raw = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", raw)

    # 还原摘出的片段
    raw = _MD_PLACEHOLDER_RE.sub(lambda m: store[int(m.group(1))], raw)
    return raw


def _md_link_html(label_text, href, ctx):
    kind, target = _rewrite_link(href, ctx)
    label = html.escape(label_text)
    if kind == "plain":
        return label
    if kind == "external" and not ctx.allow_external_links:
        return '%s <span class="meta">(%s)</span>' % (label, html.escape(href))
    return '<a href="%s">%s</a>' % (html.escape(target), label)
