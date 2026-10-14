def shipping_fee(amount):
    """计算订单运费。

    输入 amount 仅讨论整数，合法金额为 0 至 1000，包含两个端点。
    金额大于等于 99 时运费为 0，否则运费为 10。
    金额小于 0 或大于 1000 时抛出 ValueError。
    非整数输入不在本次讨论范围内。
    """
    if amount < 0 or amount > 1000:
        raise ValueError("amount out of range")
    return 0 if amount > 99 else 10
