# 长期投资策略网页

这是 `v1test` 正式 20/250 日均线基线的轻量网页版本，适合在 NAS 上用 Docker 运行。

网页提供：

- 手动输入沪深300、红利低波、科创50、纳斯达克100、黄金、境内长期国债和人民币现金的当前市值；
- 按打开网页当天，使用不晚于当天的最新本地数据计算目标比例、目标金额和增加/减少金额；
- 展示价格、MA20、MA250、信号得分、释放资金去向和完整计算过程；
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

打开 <http://127.0.0.1:8000>。首次启动会把 `seed/prices/` 复制到 `data/prices/`。点击“设置与刷新”中的立即刷新按钮后，网页才会访问远程数据源。

## Docker / NAS

默认 `docker-compose.yml` 直接使用已经发布到 GHCR 的 v1 镜像：

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

## 数据口径

- 沪深300、红利低波、科创50、境内长期国债：分别使用场内 ETF `510300`、`512890`、`588000`、`511260`。境内日线按“腾讯财经前复权 -> Yahoo Finance 复权价 -> 新浪财经 -> 东方财富”的顺序回退；这与 [a-stock-data 的数据源分层建议](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md)一致，避免单一东方财富接口暂时拒绝连接时只能使用旧缓存。
- 纳斯达克100：Yahoo Finance 的 QQQ 复权价乘 USD/CNY，按人民币计算。
- 黄金：Yahoo Finance 的 GLD 复权价乘 USD/CNY，按人民币计算。
- USD/CNY 按“Yahoo Finance `CNY=X` -> ECB 官方历史参考汇率 -> [Frankfurter ECB reference rates](https://frankfurter.dev/v1/) -> FRED `DEXCHUS`”依次回退；只要任一远程源成功就记录实际来源并视为成功，只有全部远程源失败且沿用本地缓存时才显示警告。ECB 参考汇率说明见 [ECB reference rates](https://data.ecb.europa.eu/data/data-categories/ecbeurosystem-policy_and_exchange_rates/exchange-rates/reference-rates)。
- 现金：只作为手动输入的人民币金额和 5% 基础防守仓位，不使用价格序列、均线或远程刷新。
- 升级到 v1.0.2 后，应用启动时会自动清理旧版本生成的 `data/prices/cash.csv`；不会删除网页中手动输入的现金金额。
- 境内长期国债不是美债。511260 上市前的历史沿用 `v1test` 已归档的长期国债指数/公开锚点代理；刷新只会合并新的 511260 数据，不会伪造上市前的场内 ETF 历史。

刷新时会以本地历史序列最近 20 个重叠交易日的中位数比例校准新来源的价格尺度，再只追加更晚日期。这样既能接入真实 ETF 的最新交易日，也不会把 ETF 的价格单位直接拼到历史指数代理上。

远程接口可能限流、改字段或暂时不可访问。刷新任务按顺序执行，当前是 7 个远程数据任务，不会同时发起 10 个请求；每个任务内部的数据源也按顺序回退，不会并发请求多个源。定时刷新和手动刷新也由全局锁串行化。首轮完成后只对失败项等待 1 分钟再试，最多重试 5 次，成功项不会重复请求。刷新失败会保留上一份本地文件，并在设置页标记为警告；没有本地数据时才标记为失败。历史刷新时间在网页中统一显示为 UTC+8。实际交易仍需人工核对 ETF 的 IOPV、溢价/折价、流动性、汇率成本和交易权限。
