from typing import Any, Literal

from pydantic import BaseModel, Field

from ..task_sdk import TaskContext
from ._common import result_data, run_timeline
from .spec import ProgramSpec

SPEC = ProgramSpec(
    name="browse_only",
    version="1.0.0",
    title="浏览时间线",
    description="打开指定时间线并完成有界浏览。",
)


class Params(BaseModel):
    feed: Literal["for_you", "following"] = "for_you"
    scroll_count: int = Field(default=10, ge=1, le=100)
    scroll_interval_seconds: float = Field(default=1.5, ge=0.25, le=10)
    scroll_distance: int = Field(default=650, ge=200, le=3000)


# 完整执行流程：
# Runner 校验参数并准备浏览器
#   → browse_only.run()
#   → 检查取消状态
#   → run_timeline()
#   → timeline.browse()
#   → 打开 X 首页并选择时间线
#   → 分段执行滚动
#   → 累计实际滚动次数
#   → 返回执行结果
#   → Runner 保存成功、失败或取消状态
async def run(context: TaskContext, params: Params) -> dict[str, Any]:
    await context.cancellation.raise_if_cancelled()
    context.logger.info("开始浏览时间线", feed=params.feed, scroll_count=params.scroll_count)
    result = await run_timeline(
        context,
        feed=params.feed,
        scroll_count=params.scroll_count,
        interval_seconds=params.scroll_interval_seconds,
        distance=params.scroll_distance,
    )
    await context.cancellation.raise_if_cancelled()
    data = result_data(result)
    completed = int(data.get("scrolls", 0))
    context.logger.info("浏览时间线完成", scrolls_completed=completed)
    return {"feed": params.feed, "scrolls_completed": completed}
