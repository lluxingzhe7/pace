"""PACE —— 本地运动数据分析引擎。

纯标准库实现：零第三方依赖，pip install 不需要联网。

模块
----
parsers       GPX / TCX / FIT / CSV / Strava JSON 解析
fitparse      FIT 二进制协议读取
analysis      指标计算（距离、配速、爬升、心率区间、训练负荷、最佳成绩）
analytics     跨活动聚合（周月统计、CTL/ATL/TSB、分段匹配）
store         SQLite 持久化
server        HTTP 服务与 JSON API
"""

__version__ = "1.0.0"
