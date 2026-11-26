---
name: model-routing
description: 按任务类型给 ZCode 会话挑模型：别名分层、通道能力画像、额度冷却、失败换道规则。派单前读本技能，避免"一把钥匙开所有门"。
metadata:
  version: "0.1.0"
---

# 模型路由（派单前必读）

裁决依据：`config/ROUTING_POLICY.md`（能力矩阵 + 决策顺序 + 改写规则）。
实测数据：`GET http://127.0.0.1:8788/admin/ledger`（成功率/延迟/冷却）。

## 一分钟决策

| 任务特征 | 用哪个 |
|---|---|
| 翻译、改写、格式化、短问答、批量小活 | `auto-cheap` |
| 单文件改代码、写脚本、读文档 | `auto` |
| 跨文件重构、长日志、>200k token 上下文 | `auto-heavy` |
| 方案设计、根因、评审、取舍 | `auto-reason` |
| 有图片/截图输入 | 必须带 `image` 标签的 lane（`r4-dsv41` / `mercury-3`） |
| 网关离线 | 直连 `new-provider-6/deepseek-v4.1-flash`，次选 `new-provider/deepseek-v4.1-flash-1017` |

## 硬规则

1. **上下文优先于价格**：估计超通道窗口就先分片或换大窗口，截断重跑比贵模型更贵。
2. **同 lane 连续失败 2 次就换**：不要用同一个通道重试第三次。
3. **冷却中的 lane 不派活**：台账里 `cooldown_left_sec > 0` 直接跳过。
4. **一次派单一个模型**：不要在 worker 会话里再让它"自己选模型"。
5. **新通道先实测再入池**：`python -X utf8 scripts/selftest.py --live` 通过才写 `routing.json`。

## 能力画像怎么更新

- 每次网关请求自动落 `logs/gateway-YYYYMMDD.jsonl`。
- 管家每周（或用户要求）汇总：某 lane 成功率 <0.8、平均延迟翻倍、频繁 `quota` → 降权或移出首选。
- 模型升级/换名后必须重跑 `selftest.py --live`，模型 ID 大小写敏感、照抄网关返回。

## 反模式

- 给快模型塞长上下文；给强模型写死步骤。
- 用"auto"跑明确的重活（多花一次分类不确定性）。
- 为了省钱把 COMPLEX 任务丢给便宜通道，失败后重跑三次——总成本更高。
