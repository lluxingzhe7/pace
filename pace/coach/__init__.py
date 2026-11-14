"""教练分析：评估、计划与数据质量说明。

用法::

    from pace.coach import evaluate, plan
    report = evaluate.report(store, tz_offset=8)
    weekly = plan.build_plan(store, report, goal="improve", days_per_week=4)
"""

from . import activity, evaluate, plan  # noqa: F401
