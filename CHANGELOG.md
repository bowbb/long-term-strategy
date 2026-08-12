# 更新日志

## v1.0.2 - 2026-08-12

### 修复

- 删除现金收益代理序列，不再把人民币现金显示为需要更新的行情资产。
- 最新数据概览只显示沪深300、红利低波、科创50、纳斯达克100、黄金和境内长期国债。
- 现金仍保留为手动输入金额、5%基础仓位和调仓去向，不参与价格或均线计算。
- 启动时自动清理旧版本留下的 `data/prices/cash.csv`。
- 默认 Docker Compose 镜像更新为 `ghcr.io/bowbb/long-term-strategy:1.0.2`。

## v1.0.1 - 2026-08-12

### 修复

- USD/CNY 不再依赖 FRED 单一远程接口。
- 增加 Yahoo Finance `CNY=X`、ECB 官方历史参考汇率、Frankfurter/ECB 和 FRED 的顺序回退。
- 任一远程汇率源成功时，记录实际数据源并将纳斯达克100、黄金标记为成功；只有使用本地缓存时才显示警告。

### 工程

- 增加不依赖外网的汇率解析和人民币合成单元测试。
- GitHub Actions 在构建镜像前执行单元测试。
- 默认 Docker Compose 镜像更新为 `ghcr.io/bowbb/long-term-strategy:1.0.1`。
