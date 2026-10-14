def ticket_price(age):
    """根据游客年龄计算门票价格，返回整数金额，单位为元。

    R1：age 仅讨论整数，合法年龄为 0 至 120，包含两个端点。
    R2：在合法年龄范围内，小于 12 岁时票价为 50 元；大于等于 12 岁时票价为 100 元。
    R3：年龄小于 0 或大于 120 时抛出 ValueError。
    非整数输入不在本次测试范围内。
    """
    if age < 0 or age > 120:
        raise ValueError("age out of range")
    return 50 if age <= 12 else 100
