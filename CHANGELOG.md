# 更新日志

## v1.0.1 - 2026-08-12

### 修复

- USD/CNY 不再依赖 FRED 单一远程接口。
- 增加 Yahoo Finance `CNY=X`、ECB 官方历史参考汇率、Frankfurter/ECB 和 FRED 的顺序回退。
- 任一远程汇率源成功时，记录实际数据源并将纳斯达克100、黄金标记为成功；只有使用本地缓存时才显示警告。

### 工程

- 增加不依赖外网的汇率解析和人民币合成单元测试。
- GitHub Actions 在构建镜像前执行单元测试。
- 默认 Docker Compose 镜像更新为 `ghcr.io/bowbb/long-term-strategy:1.0.1`。
