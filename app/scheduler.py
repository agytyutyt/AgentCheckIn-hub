"""调度器：APScheduler 封装，cron 存库、热更新、补偿轮询。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import credits, db, renew, runner
from .config import TZ

log = logging.getLogger("checkinhub.scheduler")


class SchedulerManager:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=TZ)
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.scheduler.add_job(
            runner.retry_tick,
            "interval",
            minutes=1,
            id="retry_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            credits.tick,
            "interval",
            minutes=credits.REFRESH_MINUTES,
            id="credits_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            renew.tick,
            "interval",
            minutes=renew.CHECK_MINUTES,
            id="renew_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._cleanup,
            CronTrigger(hour=0, minute=10, timezone=TZ),
            id="cleanup",
            replace_existing=True,
        )
        self.scheduler.start()
        self.started = True
        self.reload()
        db.log(
            0,
            f"调度器已启动（时区 {TZ}），已加载 {len(self.scheduler.get_jobs())} 个作业"
            f"（积分余额每 {credits.REFRESH_MINUTES} 分钟、令牌续期每 {renew.CHECK_MINUTES} 分钟巡检）",
        )
        log.info("scheduler started")

    def shutdown(self) -> None:
        if self.started:
            self.scheduler.shutdown(wait=False)
            self.started = False

    def reload(self) -> None:
        for job in self.scheduler.get_jobs():
            if job.id.startswith("task:"):
                self.scheduler.remove_job(job.id)
        for task in db.query("SELECT * FROM task_defs WHERE enabled=1"):
            self._add(task)
        log.info("scheduler reloaded")

    def _add(self, task: dict) -> None:
        try:
            trigger = CronTrigger.from_crontab(task["cron"], timezone=TZ)
        except Exception as exc:
            db.log(0, f"任务「{task['name']}」cron 表达式无效：{task['cron']}（{exc}）", "ERROR")
            return
        self.scheduler.add_job(
            self._run_job,
            trigger,
            args=[task["id"]],
            id=f"task:{task['id']}",
            replace_existing=True,
            # 6 小时：面板常常是白天才被打开/重启，1 小时宽限会让 08:30 的签到直接丢掉，
            # 当天就再无记录。签到接口幂等，补跑一次不会有副作用。
            misfire_grace_time=6 * 3600,
            coalesce=True,
            max_instances=1,
        )

    async def _run_job(self, task_id: int) -> None:
        task = db.query_one("SELECT * FROM task_defs WHERE id=?", (task_id,))
        if not task or not task.get("enabled"):
            return
        if not runner.within_window(task):
            db.log(0, f"「{task['name']}」不在执行窗口内，跳过本轮", "WARN")
            return
        await runner.run_task(task_id, "cron")

    async def _cleanup(self) -> None:
        cutoff = db.now() - 90 * 86400
        db.execute("DELETE FROM run_logs WHERE ts < ?", (cutoff,))
        db.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        db.execute("DELETE FROM retry_queue WHERE created_at < ? AND status != 'pending'", (cutoff,))

    def next_runs(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for job in self.scheduler.get_jobs():
            if job.id.startswith("task:") and job.next_run_time:
                out[int(job.id.split(":")[1])] = job.next_run_time.strftime("%m-%d %H:%M")
        return out


mgr = SchedulerManager()
