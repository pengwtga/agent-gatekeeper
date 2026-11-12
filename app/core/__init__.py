"""基础设施：与具体业务无关的通用能力

- llm_client   LLM 容灾链（多 Provider 依序降级 / JSON 模式兼容 / token 统计）
- budget       请求级时间预算（按 Provider 切时间片）
- embeddings   embedding 客户端（配额耗尽识别，不重试）
- vector_store 内存向量存储（接口对齐向量库，可无缝替换）
"""
