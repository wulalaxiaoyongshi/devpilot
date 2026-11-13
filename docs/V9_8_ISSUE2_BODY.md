## 需求：增加订单税费计算能力

目前 `shop/pricing.py` 已有折扣计算函数，但是没有统一的含税价格计算函数。请让 Coding Agent 新增：

```python
def total_with_tax(amount: int, tax_percent: int) -> int:
    ...
```

验收标准：

1. 金额以整数分为单位，例如 `total_with_tax(100, 8) == 108`。
2. `total_with_tax(100, 0) == 100`、`total_with_tax(0, 8) == 0`。
3. `amount < 0` 或税率不在 `[0,100]` 时抛出 `ValueError`。
4. 在 `tests/test_pricing.py` 增加真实 pytest 回归测试，覆盖正常、零值和非法参数。
5. 保留原有 `discounted_total` 函数的正确行为，原来 5 条测试必须继续通过。
6. 仅修改 `shop/pricing.py` 与 `tests/test_pricing.py`，不要修改仓库配置、主分支或密钥。

请先运行原有测试，在 V7 审批后执行 Worker 修改，由 V8 Goal 验证；验收通过后经过单独的发布审批创建 PR，不允许自动合并 `main`。
