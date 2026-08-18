# 长期投资策略网页

这是 `v1test` 正式 MA250 ±3% 滞回策略的轻量网页版本，适合在 NAS 上用 Docker 运行。

网页提供：

- 手动输入红利低波、纳斯达克100、黄金、境内长期国债和人民币现金的当前市值；
- 使用最新已完成交易日的信号，计算目标比例、目标金额和增加/减少金额；
- 展示信号价格、MA250、±3% 上下轨、滞回状态、释放资金去向和完整计算过程；
- 按 ETF 每笔万 0.6、最低 0.3 元估算交易佣金；
- 每日定时刷新，以及设置页的立即刷新按钮；
- 按资产保存刷新成功、警告和失败原因；
- 所有运行数据写入 `data/`，通过 Docker volume 持久化到 NAS。

## 本地运行

需要 Python 3.12 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

打开 <http://127.0.0.1:8000>。新版本不会自动使用旧的 `seed/prices/` 代理数据；首次启动会把旧的 `data/prices/` 移到 `data/legacy_prices/`，点击“设置与刷新”中的立即刷新按钮后才会写入官方数据。

## Docker / NAS

默认 `docker-compose.yml` 直接使用已经发布到 GHCR 的 v1.3.0 镜像：

```powershell
docker compose pull
docker compose up -d
```

不需要 `.env` 文件，默认端口是 `8000`，持久化目录是项目下的 `data/`。如需修改端口或时区，可复制 `.env.example` 为 `.env` 后编辑。

NAS 上建议把 `./data` 映射到 NAS 的固定目录，并只在局域网或反向代理认证后访问；这个轻量版本没有用户登录系统。

如果需要从源码本地构建：

```powershell
docker compose -f docker-compose.local.yml up -d --build
```

`docker-compose.ghcr.yml` 保留为兼容旧部署的同版本 GHCR 配置；新部署直接使用默认的 `docker-compose.yml` 即可。

## GitHub Actions / GHCR

`.github/workflows/publish.yml` 会在 `main` 分支推送、版本 tag 推送和手动运行时构建并发布镜像。GitHub 仓库需要允许 Actions 写入 Packages；工作流使用内置 `GITHUB_TOKEN`，不需要把个人 token 写进仓库。推送到 `main` 会生成 `:latest` 和 `:main` 标签。

建议把 `项目` 文件夹本身作为 Git 仓库根目录上传，这样 `.github/workflows/publish.yml` 会自动生效。镜像名称会按 GitHub 仓库生成：`ghcr.io/<owner>/<repo>`。

## 正式策略

- 基础权重：红利低波 30%、纳斯达克100 50%、黄金 10%、境内长期国债 5%、人民币现金 5%。
- 每个交易日判断一次。风险资产价格高于 `MA250 × 1.03` 时恢复 100% 基础仓位；已有风险仓位且价格低于 `MA250 × 0.97` 时降至 50% 基础仓位；在上下轨之间保持当前状态。
- 没有持仓且价格位于区间内或低于下轨时不主动建仓，等待价格突破上轨；已经持有半仓或满仓的风险资产在区间内维持原状态。
- 使用最新已完成交易日的信号，在下一个交易日执行；风险资产状态为 0%、50% 或 100% 基础仓位。
- 风险资产从满仓降至半仓时，释放其基础仓位的 50%，资金各 50% 转入境内长期国债和人民币现金；基础 5% 国债与 5% 现金始终保留。
- 风险资产有效历史不足 250 个交易日时，其基础权重全部转入境内长期国债。

## 数据口径

- 红利低波：策略序列直接使用[中证指数官方 H20269 历史表现接口](https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=H20269&startDate=20050101&endDate=20260819)的中证红利低波全收益指数收盘点位；分红已嵌入全收益指数，不再把 ETF 分红单独记账，也不再使用 `512890` ETF 原始收盘价作为网页策略序列。
- 境内长期国债：策略序列直接使用[上交所官方日线接口](http://yunhq.sse.com.cn:32041/v1/sh1/dayk/511260?begin=-10000&end=-1&period=day)的 ETF `511260` 原始收盘价；ETF 的基金身份、上市日期和标的指数可在[上交所产品资料概要](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-19/511260_20260319_NNHW.pdf)核验。
- 纳斯达克100：使用[Nasdaq 官方历史接口](https://api.nasdaq.com/api/quote/QQQ/historical?assetclass=etf&fromdate=2005-01-01&todate=2100-01-01&limit=5000)的 `QQQ` 原始收盘价；QQQ 的跟踪标的和基金信息见[发行人页面](https://www.invesco.com/us/financial-products/etfs/product-detail?audienceType=investors&productId=QQQ&ticker=QQQ)。官方公开历史接口最多提供约 10 年，策略不再伪造更早的 ETF 历史。
- 黄金：使用[State Street 官方 GLD 历史归档](https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en&product=gld)中的 `Closing Price`；基金身份、交易所和 NAV 口径见[发行人页面](https://www.ssga.com/us/en/individual/etfs/spdr-gold-shares-gld)。
- USD/CNY：首选美联储 H.10 的[FRED `DEXCHUS` CSV](https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXCHUS)，失败时只回退到[ECB 官方历史参考汇率](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip)。`DEXCHUS` 的单位是人民币/美元，属于日度纽约午间参考价，不是逐笔实时汇率。
- 现金：只作为手动输入的人民币金额和 5% 基础防守仓位，不使用价格序列、均线或远程刷新。
- 应用启动时会自动清理旧版本生成的 `data/prices/cash.csv`；不会删除网页中手动输入的现金金额。
- 境内长期国债不是美债。`511260` 的历史从其上市日开始；如果官方历史少于 250 个已完成交易日，策略会按既有规则暂不生成完整信号，不会用旧指数代理补齐。

每次官方刷新都会整段替换对应本地历史，并在 `data/refresh_log.json` 记录实际 provider、URL、检查时间、最新值、行数和序列 SHA-256。旧的比例校准逻辑已停用；兼容接口遇到明显尺度差异会拒绝写入，避免错误历史给后续数据定标。

官方公开接口可能限流或延迟。刷新任务按顺序执行，首轮完成后只对失败项等待 1 分钟再试，最多重试 5 次；失败时保留上一份已经通过官方质量检查的本地文件并标记警告。上交所和 Nasdaq 的公开行情页面可能存在延迟，真正的逐笔实时 feed 需要交易所授权/订阅。实际交易仍需人工核对 ETF 的 IOPV、溢价/折价、流动性、汇率成本和交易权限。
