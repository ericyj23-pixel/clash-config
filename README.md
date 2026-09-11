# Clash 订阅合并（单仓库版）

多源订阅 → 解析 → 去重 → 地区分组 → 策略组 → 输出 Clash.Meta 配置，供 BettBox / FlClash 使用。

**仓库**：`ericyj23-pixel/clash-config`（公开，一个仓库搞定）
**最终订阅链接**（跑通一次 Actions 后生效）：

```
https://cdn.jsdelivr.net/gh/ericyj23-pixel/clash-config@main/config.yaml
```

## 部署步骤

### 1. 把本目录所有文件传进 clash-config 仓库

保留仓库里已有的其他文件（如 `clash_3in1.yaml`），新增这些：

```
merge.py  parsers.py  regions.py  crawler.py  requirements.txt
sources.txt  config/  .github/workflows/update.yml  README.md
```

两种方式任选：

- **网页上传**：仓库页面 → Add file → Upload files → 把上述文件和 `config`、`.github/workflows` 两个文件夹拖进去 → Commit
- **命令行**：

```bash
git clone https://github.com/ericyj23-pixel/clash-config.git
# 把本目录内容复制进 clash-config/ 后：
cd clash-config
git add . && git commit -m "添加订阅合并" && git push
```

### 2. 触发一次

仓库 → **Actions** → Update Subscription → **Run workflow**。
跑完后仓库根目录会出现 `config.yaml`，订阅链接即生效。

之后每天自动跑两次（北京 08:23 / 20:23），BettBox / FlClash 里填上面那条链接即可。

> 首次进 Actions 页面如果提示 "Workflows aren't being run"，点 "I understand my workflows, go ahead and enable them"。

## 延迟优化（连通性探测）

抓到的免费节点里有大量已失效的，直接导入会出现"点了没反应"。所以合并后会**并发 TCP 探测**
每个节点的连通性和耗时：

- 连不上的直接丢弃（实测 623 个里 383 个能连上，约 62%）
- 存活节点**按延迟升序**排列，节点名后标注实测值，如 `🇸🇬 SG-xxx [78ms]`
- 新增 `⚡ 低延迟优选` 策略组：取实测最快的 30 个，客户端选它就行
- `♻️ 自动选择` 组按地区轮询取样，但现在每个地区优先取最快的

**注意**：`hysteria2` / `tuic` 走 UDP，TCP 探测必然失败，这类节点原样保留、不参与过滤。

### 关于延迟数值准不准

- **Actions（每天自动）**：出口在美国，测出的延迟只代表"美国到节点"，
  主要用于**剔除死节点**（死节点在哪都是死的），数值别当真
- **你本机跑**：才是你实际感受到的延迟，排序最准

想拿到最准的排序，在你自己电脑上跑一次：

```bash
pip install -r requirements.txt
python merge.py -o my.yaml      # 本机测速，延迟就是你真实延迟
```

参数在 `config/options.yaml` 的 `speedtest` 段：

| 参数 | 说明 |
|---|---|
| `enabled` / `--no-speedtest` | 关掉测速，只做合并去重 |
| `timeout` | 单次连接超时（秒），网络差可调到 5 |
| `max_latency_ms` | 延迟上限，超过丢弃；`0` = 不限制 |
| `keep_top` / `keep_per_region` | 只留最快的 N 个 / 每个地区最多 N 个 |
| `tag_name` | 节点名后是否标注延迟 |

## 订阅源

`sources.txt` 里一行一条，支持两种写法（当前三个源都是公开发布站，无隐私，可随仓库公开）：

```
# 1) 发布站：自动抓当天最新文章，再从文章里找出订阅地址
page:https://yoyapai.com/category/mianfeijiedian

# 2) 直接订阅地址（Clash YAML / base64 / 分享链接 / 本地文件路径）
https://example.com/api/v1/client/subscribe?token=xxx
```

**带 token 的私人订阅不要写进 sources.txt**（仓库是公开的），放到 Secrets：
Settings → Secrets and variables → Actions → New secret → Name: `SUB_URLS`（多条用换行分隔）。
工作流会自动把 SUB_URLS 和 sources.txt 合起来用。

`page:` 的抓取逻辑（crawler.py）：抓页面 → 收集所有候选 URL → **实地下载验证**（能解析出节点才算）
→ 列表页没找到就自动跟进最新文章。站点改订阅地址格式也不会失效。

> 注意：填发布站请填**分类页/首页**，不要填某篇具体文章——文章内容停在发布那天，
> 只有列表页才会每天指向最新一篇。

## 目录说明

```
merge.py                     主程序：抓取 / 去重 / 分组 / 输出
parsers.py                   协议解析：ss vmess vless trojan hysteria2 tuic
crawler.py                   发布站爬虫：page: 源自动定位当天最新订阅地址
speedtest.py                 连通性/延迟探测：并发 TCP 握手计时
regions.py                   地区识别与国旗
config/base.yaml             配置模板：全局项、策略组、分流规则
config/options.yaml          合并行为：过滤词、去重、测速参数
sources.txt                  订阅源清单（仅公开页面地址）
.github/workflows/update.yml 定时任务
config.yaml                  生成产物（由 Actions 自动更新，勿手改）
```

## 已验证

2026-09-10 实测：3 个发布站全部抓通，5 个订阅地址 → 原始 1117 个节点 → 去重过滤后 604 个。

- 6 种协议解析（含 legacy 格式 SS）、跨源去重、假节点过滤（剩余流量/官网）、地区识别、emoji 直出
- 节点为 0 时不会覆盖已有配置，避免订阅挂掉导致客户端空配置
- 自动测速组默认只放 80 个节点（`options.yaml` 的 `max_nodes`），跨地区取样

## 已知限制

- jsDelivr 缓存最长约 12 小时，想立刻生效在链接后加 `?t=日期` 重新拉取
- 不做测速优选，节点排序按订阅原顺序，快慢由客户端「♻️ 自动选择」负责
- 604 个是公开免费节点，可用率和安全性无保障，别用来登录账号或支付

## 踩过的坑（改动代码前先看）

1. **编码**：多数订阅服务不声明 charset，requests 按 latin-1 解出非法控制字符会让 YAML 解析直接崩。
   统一走 `crawler.response_text()` 做 UTF-8 兜底。
2. **UA**：requests 默认自带 `python-requests` UA，会被部分站点拦截；
   爬虫必须显式覆盖为浏览器 UA，且不能用 `setdefault`（默认已有值，设不进去）。
3. **分享链接正则**：不能用「非空白字符」匹配节点名，`#` 后的名称常含空格会被截断。
   改为按行提取，以「下一个协议头或行尾」为边界。
4. **地区识别**：节点名有简繁体差异（`法国` / `法國`）和 ISO 国家码写法（`RO_34`、`IE-54.x`），
   两套规则都要有，国家码需加 `(?<![A-Za-z])` 前视断言，否则 `MATCH1` 里的 `AT`、`LINK` 里的 `IN` 会误判。
