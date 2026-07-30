# 可复现性说明 (Phase 1)

## 目标

证明相同输入与配置产生相同的 curated 数据与内容哈希；
`fetched_at` 等非确定字段不破坏内容复现测试。

## 方法

1. 对同一份合成原始数据运行两次标准化。
2. 计算内容 SHA-256（排除 `fetched_at`）。
3. 比较两次哈希是否一致。

## 结果

- 第一次内容哈希: `316088a9ddb0f49eaa8f1969a97acb79dfa888a86b8a1b7fbf996279895c7f5d`
- 第二次内容哈希: `316088a9ddb0f49eaa8f1969a97acb79dfa888a86b8a1b7fbf996279895c7f5d`
- 哈希一致: **是**

## 非确定字段处理

`fetched_at` 在内容哈希计算时被排除（见 `config.manifest.content_hash_exclude_fields`），
因此不同抓取时间不会影响复现性结论。

## 数据版本清单

清单文件: `reports\phase-1\manifest.example.json`
