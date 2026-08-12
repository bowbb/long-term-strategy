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

本地构建运行：

```powershell
docker compose up -d --build
```

默认端口是 `8000`，持久化目录是项目下的 `data/`。NAS 上建议把 `./data` 映射到 NAS 的固定目录，并只在局域网或反向代理认证后访问；这个轻量版本没有用户登录系统。

如果直接使用 GHCR 镜像：

```powershell
Copy-Item .env.example .env
# 编辑 .env，把 IMAGE_NAME 改成实际镜像地址
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

NAS 只需要 `docker-compose.ghcr.yml`、`.env` 和同目录的 `data/` 文件夹；也可以直接克隆整个 GitHub 仓库后运行上面两条命令。

## GitHub Actions / GHCR

`.github/workflows/publish.yml` 会在 `main` 分支推送、版本 tag 推送和手动运行时构建并发布镜像。GitHub 仓库需要允许 Actions 写入 Packages；工作流使用内置 `GITHUB_TOKEN`，不需要把个人 token 写进仓库。推送到 `main` 会生成 `:latest` 和 `:main` 标签。

建议把 `项目` 文件夹本身作为 Git 仓库根目录上传，这样 `.github/workflows/publish.yml` 会自动生效。镜像名称会按 GitHub 仓库生成：`ghcr.io/<owner>/<repo>`。

## 数据口径

- 沪深300、红利低波、科创50、境内长期国债：东方财富场内 ETF 日线接口，分别使用 `510300`、`512890`、`588000`、`511260`；复权价用于均线和历史连续性。
- 纳斯达克100：Yahoo Finance 的 QQQ 复权价乘 FRED `DEXCHUS`，按人民币计算。
- 黄金：Yahoo Finance 的 GLD 复权价乘 FRED `DEXCHUS`，按人民币计算。
- 现金：保留 `v1test` 的本地现金收益代理，不作为风险资产择时信号。
- 境内长期国债不是美债。511260 上市前的历史沿用 `v1test` 已归档的长期国债指数/公开锚点代理；刷新只会合并新的 511260 数据，不会伪造上市前的场内 ETF 历史。

远程接口可能限流、改字段或暂时不可访问。刷新失败会保留上一份本地文件，并在设置页标记为警告；没有本地数据时才标记为失败。实际交易仍需人工核对 ETF 的 IOPV、溢价/折价、流动性、汇率成本和交易权限。
