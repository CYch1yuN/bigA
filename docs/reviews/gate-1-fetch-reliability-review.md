# Gate 1 抓取可靠性补丁审核

结论：`FAIL`

审核提交：`66a0811`

## 已验证

- Python 3.11.9：93 passed，0 failed，0 skipped。
- 总覆盖率85%；`fetcher.py` 80%，`cli.py` 86%。
- 工作树干净，提交本身无 `git show --check`错误。
- `FetchManager`的主源重试、回退成功、双源失败、合法空数据和成功manifest单元测试通过。

## 阻断问题

### FR-01 CLI忽略YAML中的primary

`fetch --source`默认值被写死为`akshare`，而`cmd_fetch`只在参数值等于配置primary时才传入`None`。当用户在YAML中设置`primary: baostock`且不提供`--source`时，参数仍为`akshare`，最终强制使用AKShare，违反“fetch按YAML读取主源/回退配置”。

修复要求：

- `--source`默认值必须为`None`；仅当用户显式传参时覆盖配置primary。
- CLI帮助中说明省略时使用YAML主源。
- 增加真正调用`cmd_fetch`或`main([...])`的离线测试，证明自定义primary被采用。

### FR-02 max_retries语义与配置不一致

配置注释和字段名表示“失败后的重试次数”，但循环`range(1, max_retries + 1)`把它实现为总尝试次数。默认3实际只包含首次尝试加两次重试。

修复要求：

- 明确并统一语义。建议保留字段名`max_retries`，实现总尝试次数为`1 + max_retries`。
- manifest同时记录`max_retries`和实际尝试次数。
- 增加`max_retries=0`仍执行一次初始请求、`max_retries=2`最多执行三次的测试。

### FR-03 配置缺少边界校验

`max_retries`允许负数，`request_interval_seconds`允许负数；前者会产生零次请求且无尝试记录，后者会在重试等待时触发额外异常。

修复要求：

- Pydantic约束`max_retries >= 0`、`request_interval_seconds >= 0`。
- 增加非法配置加载失败测试。

## 必须补齐的审计行为

- 当前CLI失败后在写manifest之前直接返回，双源失败不会留下任何机器可读尝试记录。为保证抓取审计链，失败也应在metadata层生成manifest，`success=false`、`file=null`并包含全部尝试与错误。
- 新增端到端离线CLI测试：重试后成功、主源失败回退成功、双源失败并生成失败manifest、合法空数据成功manifest、`--no-fallback`。
- 测试manifest中的SHA-256确实等于CLI写出的raw文件，而不只是直接调用`build_fetch_manifest`。

## 复审条件

- 上述问题修复后完整测试0失败，覆盖率不低于当前85%。
- 离线CLI测试不得访问公网，不得通过直接测试内部方法替代CLI接线验证。
- 提交独立commit，保留本报告，并停止在Phase 1等待复审。
