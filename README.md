# Zioon reusable workflows

共享构建规则；产品测试命令和组件归属在各产品仓库维护。使用此仓库的 **commit SHA** 调用工作流，更新依赖通过 PR 完成。

- `node-ci.yml`：固定 Node 22.18.0，使用锁文件安装依赖，运行调用方指定的检查与构建，可上传独立组件制品。
- `secret-scan.yml`：校验后运行固定版本 Gitleaks，检查当前提交源码；对 PR 无写权限。

默认 `contents: read`。CI 不接触生产凭据，也不部署。生产部署位于私有 `infra`，只能选择产品仓库已成功构建的制品，校验 commit、SHA256、磁盘空间、健康检查和回滚。
