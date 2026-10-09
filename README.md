# CC98 Kindle 阅读器

用 Kindle(特别是老款,如 Paperwhite 3)刷 CC98 的小工具:在电脑上跑一个本地服务,
Kindle 自带浏览器打开一个网址就能用 —— **不用越狱、不用刷机、Kindle 端零安装**。

> 新版 cc98.org 是前端单页应用,而且会检测浏览器是否支持 flex 布局,不通过就直接显示
> "浏览器不受支持"。Kindle 自带浏览器(约 Safari 5 时代的内核)因此完全打不开 98。
> 本项目绕开网页版,直接调用 CC98 官方 API,在电脑上渲染成极简大字页面发给 Kindle。

## 为什么用这种方案

| 方案 | 问题 |
|---|---|
| Kindle 浏览器直接上 98 | 老浏览器被网站检测拦截,直接无解 |
| 越狱装第三方阅读器 | 折腾、有风险、新客户端未必适配墨水屏 |
| **本地服务中转(本项目)** | 电脑上跑个 Python 服务,Kindle 只访问局域网地址,零改动零风险 |

## 功能

- **浏览**:热门(十大热帖)/ 新帖 / 关注(收藏的帖子、关注的人、粉丝)/ 版面 / 我(我的主题、我的回复)
- **帖子**:楼层卡片式排版、楼主标签、点赞/踩数、楼主头像、点用户名进个人主页
- **回帖**:支持回复主题、匿名回复;失败提示已中文化
- **图片**:帖子图片、头像自动经本机代理加载;CC98 的 WebP 图片自动转 JPEG(需 Pillow)
- **表情**:CC98 的 em/+ac/tb/ms/CC98 表情渲染为图片;Unicode emoji(😭)用系统字体本地渲染成小图,不联网
- **排版**:专为墨水屏和老浏览器设计 —— 无 JS、无 flex、大字号、黑白高对比

## 环境要求

- 一台**在浙大内网**(或已连 RVPN/WebVPN)的电脑,Windows 上实测通过
- Python 3.8+(无第三方依赖也能跑)
- Kindle 与电脑在同一个 Wi-Fi(校园 Wi-Fi 有 AP 隔离时见下文)
- 可选依赖 [Pillow](https://pypi.org/project/pillow/):用于图片 WebP→JPEG 转码和 emoji 渲染

## 快速开始

1. 下载本项目,进入目录安装可选依赖:
   ```bash
   python -m pip install pillow
   ```
2. 复制配置模板并填入你的 CC98 账号:
   ```bash
   cp config.example.json config.json
   ```
   编辑 `config.json`,填入学号和密码(只保存在本地):
   ```json
   "username": "312xxxxxxxx",
   "password": "你的密码"
   ```
3. 启动:
   ```bash
   python cc98_kindle.py
   ```
4. 启动横幅会打印可访问地址(首次运行会自动生成访问口令),抄进 Kindle 自带浏览器,例如:
   ```
   http://192.168.x.x:8000/k/xxxxxx/
   ```
   (结尾的 `/k/xxxxxx/` 是访问口令,别漏掉;可以在 Kindle 浏览器里存成书签)

> 首次运行 Windows 会弹防火墙窗口 → 勾选并点"允许访问"(公用网络也要允许)。
> **服务开着才能看** —— 电脑睡眠或程序关闭后 Kindle 就打不开了。

## 打不开怎么办

按顺序试启动横幅里列出的全部地址。如果都不行,多半是校园 Wi-Fi 禁止设备互相访问(AP 隔离):

1. 电脑:设置 → 网络和 Internet → 移动热点 → 打开
2. Kindle:连上电脑的热点
3. Kindle 浏览器改用横幅里的热点地址(`http://192.168.137.1:8000/k/xxxxxx/`)

## 配置说明(config.json)

| 配置项 | 说明 |
|---|---|
| `username` / `password` | CC98 学号和密码(只在本地用于登录换令牌) |
| `access_key` | 访问口令,首次运行自动生成;所有页面都在 `/k/口令/` 之下,防止同网段他人访问 |
| `port` | 服务端口,默认 8000(被占用时可改) |
| `font_size_px` | 字号,默认 34;Kindle 上觉得字小调大(如 38),字大调小(如 30),改完重启 |
| `show_images` | 是否显示帖子图片和头像,默认 `true`;关掉最流畅 |
| `show_emoticons` | 是否显示 CC98 表情,默认 `true` |
| `emoji_as_image` | Unicode emoji 是否渲染为图片,默认 `true`;关掉则显示 `[大哭]` 文字标签 |
| `image_max_kb` | 单张图片代理上限,默认 2048KB,超限不显示 |
| `posts_per_page` / `topics_per_page` | 每页楼层数 / 列表条数,默认 10 / 20 |

## 项目结构

```
cc98_kindle.py    入口:配置、网络探测、HTTP 服务与路由(含回帖的 POST 处理)
cc98_api.py       CC98 API 客户端:登录、token 自动刷新、各端点、回帖、错误码中文化
cc98_ubb.py       UBB / Markdown 正文渲染 + Unicode emoji 处理(安全:先转义再生成标签)
cc98_emoji.py     Unicode emoji → PNG 本地渲染(用系统 emoji 字体,结果缓存到 emoji_cache/)
cc98_page.py      面向老浏览器/墨水屏的页面模板与样式(无 JS、无 flex)
config.example.json  配置模板
```

## 安全与隐私

- 账号密码只写在本地 `config.json`,程序仅用它调用 CC98 官方登录接口换令牌,
  不会上传到任何其他服务器;登录后的 refresh token 存在本地 `token.json`
- `config.json`、`token.json`、`emoji_cache/` 已在 `.gitignore` 中,不要提交/分享
- 服务带访问口令(access_key):因为电脑可能处在校园网公网地址上,
  没有口令的话同网段的人也能通过你的服务访问 98
- 本项目是只读浏览 + 回帖,不会修改账号设置、不会自动签到、不会做任何批量操作

## 常见问题

- **提示"账号或密码错误"**:检查 `config.json`,改完需重启程序
- **提示"登录已失效"**:一般会自动刷新;仍失败就删掉 `token.json` 后重启
- **提示"无法连接 api.cc98.org"**:电脑不在浙大内网,需连 RVPN/WebVPN
- **图片显示成小方块**:没装 Pillow 或字体缺失,`python -m pip install pillow` 后重启
- **Kindle 上看到旧内容/旧样式**:Kindle 浏览器缓存很顽固,重新打开书签或彻底退出浏览器再进
- **回帖失败**:页面上会显示具体原因(如"发帖太快了,请 10 秒后再试""帖子已锁定")

## 已知限制

- 回帖只支持纯文本(换行可用),不支持富文本/图片/表情插入
- 楼中楼、编辑帖子、私信等功能未实现
- 未登录时只能看热帖和版面目录(CC98 接口限制)
- 主要针对 Kindle Paperwhite 3 实测,其他老 Kindle/墨水屏设备理论上也可用

## 免责声明

本项目是非官方的第三方工具,仅供**个人学习与技术交流**使用,与 CC98 官方无关;
使用时请遵守 CC98 论坛章程与相关规定,不要用于爬取、批量发布或其他违规用途。
请勿向他人分享你的账号配置。

## License

[MIT](LICENSE)
