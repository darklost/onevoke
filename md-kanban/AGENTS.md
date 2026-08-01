# Repository Guidelines

## Project Structure & Module Organization

- `bin/kanban` 是 Python 3 CLI 的唯一实现, 包含看板定位、任务校验、状态迁移和命令解析.
- `tests/test-kanban.py` 使用临时目录覆盖小任务、大任务、非法迁移、归档及初始化流程.
- `KANBAN-RULES.md` 定义面向用户和 Agent 的行为契约. 修改 CLI 行为时, 同步检查规则与测试是否仍一致.
- 运行时创建的 `kanban/` 是本机共享数据, 不属于仓库源码, 不得提交.

## Build, Test, and Development Commands

本项目仅依赖 Python 标准库, 无构建步骤或依赖安装.

```sh
./install.sh
python3 bin/kanban --help
KANBAN_COMMAND="$PWD/bin/kanban" python3 tests/test-kanban.py
python3 -m py_compile bin/kanban tests/test-kanban.py
```

安装脚本把命令复制到 `~/.local/bin/`, 把规则复制到 `~/.agents/`. 后续命令依次检查入口、测试当前工作树脚本和执行快速语法检查. 手工试验应设置临时 `KANBAN_DIR`, 不要污染真实看板.

## Coding Style & Naming Conventions

使用 Python 3、UTF-8、4 空格缩进及标准库优先的实现. 函数和变量采用 `snake_case`, 类采用 `PascalCase`, 常量采用 `UPPER_SNAKE_CASE`. 保持函数职责单一, 对无效输入抛出 `KanbanError`, 不静默忽略失败.

任务 ID 必须匹配 `YYYYMMDD-short-slug-task`; slug 仅使用小写 ASCII 字母、数字和连字符. 用户可见错误信息及规则文档沿用中文和 ASCII 标点.

## Testing Guidelines

测试框架为 `unittest`; 测试方法命名为 `test_<behavior>`. 每项行为变更至少覆盖成功路径和相关拒绝路径. 使用 `TemporaryDirectory` 隔离文件系统状态, 不依赖或改写用户真实看板. 提交前运行完整测试命令; 当前项目未设置覆盖率阈值.

## Commit & Pull Request Guidelines

仓库目前没有可归纳的提交历史. 新提交使用简短中文动宾 subject, 每个 commit 只包含一个关注点, 例如 `修复重复任务检测`. PR 应说明行为变化、原因和实际验证命令; 关联任务或 issue. CLI 输出变化附终端示例, 无界面改动时无需截图.

## Security & Configuration

`KANBAN_DIR` 仅用于测试、非 Git 项目或明确覆盖. 不提交 token、凭据、敏感服务地址、真实任务卡片或本机路径. 文件写入和状态迁移必须继续经过现有校验, 不得绕过 `scan()` 或 `validate_target()` 直接操作任务入口.
