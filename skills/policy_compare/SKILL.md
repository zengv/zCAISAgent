---
name: policy_compare
version: 1.0.0
description: 制度对比与差异说明
when_to_use: 用户要求对比两项制度、条款或政策差异
tags: [对比, 比较, 区别]
trigger_keywords: [对比, 比较, 区别, 差异]
allowed_sources: [inmemory, bm25, vector]
required_scopes: [retrieve]
min_citation_support: 0.75
---
只依据白名单来源对比两项内容，逐条列出相同点与差异点，并标注各自出处与生效版本。
