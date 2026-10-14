# 输入 value 为整数，本次不讨论非整数输入。
# 返回 value 的绝对值：负数取相反数，零返回零，正数返回其本身。
def absolute_value(value):
    if value < 0:
        return -value
    return value
