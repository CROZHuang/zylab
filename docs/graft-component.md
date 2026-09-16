# Managed Graft component

zylab 核心只依赖 Python 标准库。Graft 是同一持久化根上的可选本地 sidecar：
zylab 拥有工具 schema、路径/凭据边界、network namespace、cache 和 UI；Graft
只负责构建与查询静态结构图，不调用 provider。

## Lock contract

`graft-component.lock.json` 固定以下可执行供应链：

- Node executable 的相对路径、版本和 SHA-256；
- Graft checkout 的完整 Git revision，以及其 upstream base；
- `package-lock.json` 与 `dist/cli.js` 的 SHA-256；
- 可重定位的 launcher 路径。

所有路径必须留在 lock 所在 zylab 仓库的持久化父目录内，不能指向受保护的只读路径。
`scripts/manage_graft.py check --json` 只读核验这些证据。`repair` 只在 Node、源码、
lockfile 和构建产物全部匹配时原子重建 launcher；它不会联网、安装依赖、构建源码或
修改 checkout。任一核心检查失败时，修复会拒绝执行。

pod bootstrap 调用 `repair`。这覆盖了“原 PVC 接到新 pod 后 launcher 丢失/漂移”的
恢复场景；它不是从空磁盘安装 Graft 的网络 installer。当前 hosted-mode revision
`fd57aea5fda60efb883fce8c2111bf08c9caa01b` 是持久化 checkout 中、位于 upstream
`ec3fc4151b391e229b859b7244572fbf4f7fdf7f` 之上的本地提交。在该提交被发布或另行
归档前，不能声称只靠公开 upstream 能从零恢复它。

## Update procedure（维护者专用）

这一节只对**持有 Graft checkout 的维护者**成立：lock 钉的 revision 是本地提交，
尚未发布，所以别人无法据此从零恢复组件。**没有 sidecar 时 zylab 照常工作** ——
它会把全部 `graft_*` 工具从模型的 schema 里摘掉（fail-closed，不会退化成
一个联网执行的路径），其余功能不受影响。

1. 在你的 Graft checkout 里完成 scoped 修改、测试与 `npm run build`，保持 tracked
   worktree clean；不要直接修改 lock 来掩盖漂移。
2. 记录完整 `git rev-parse HEAD`、Node `--version`，并分别计算 Node、
   `package-lock.json`、`dist/cli.js` 的 SHA-256。
3. 人工复核差异和测试证据后更新 `graft-component.lock.json`。
4. 运行：

```bash
python3 scripts/manage_graft.py check --json
python3 -m unittest tests.test_graft_component tests.test_graft
```

5. 最后执行 zylab 全量测试，并单独提交 Graft 与 zylab 的 Git 历史；不要把
   两个仓库伪装成一个原子 commit。
