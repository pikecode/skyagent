"""Cooperative, offline simulation runner. All SQLite work stays on GUI thread."""

from PySide6.QtCore import QObject, QTimer, Signal

from skyagent_manager.inventory import Inventory


class LocalTaskRunner(QObject):
    progress = Signal(int, int)
    completed = Signal(str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, database, store_id, task_id, *, mode="success", parent=None):
        super().__init__(parent)
        if mode not in {"success", "failure", "alternating"}:
            raise ValueError("模拟结果模式无效。")
        self.repository = Inventory(database)
        self.store_id, self.task_id, self.mode = store_id, task_id, mode
        tasks = self.repository.tasks(store_id)
        task = next((row for row in tasks if row["id"] == task_id), None)
        if task is None or task["state"] != "running":
            raise ValueError("请选择未结束的本地模拟任务。")
        database._require_active_store(store_id)
        self.ids = [
            row["stock_id"]
            for row in self.repository.task_items(store_id, task_id)
            if row["state"] == "reserved"
        ]
        if not self.ids:
            raise ValueError("没有待处理的模拟任务项。")
        self.index, self.interrupted, self.ended = 0, False, False
        self.started = False
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.step)

    def start(self):
        if self.started or self.ended:
            raise ValueError("模拟执行器不能重复启动。")
        self.started = True
        self.timer.start()

    def requestInterruption(self):
        self.interrupted = True

    def step(self):
        if self.ended:
            return
        try:
            if self.interrupted:
                self.repository.finish(self.store_id, self.task_id, "canceled")
                self._end("模拟执行已取消；已成功项保留，剩余预占已释放。")
                return
            succeeded = self.mode == "success" or (
                self.mode == "alternating" and self.index % 2 == 0
            )
            self.repository.complete_item(
                self.store_id, self.task_id, self.ids[self.index], succeeded=succeeded
            )
            self.index += 1
            self.progress.emit(self.index, len(self.ids))
            if self.index == len(self.ids):
                self._end("本地模拟执行完成；没有请求第三方接口或上传账号。")
        except Exception:
            self.ended = True
            self.timer.stop()
            # Do not claim release/finish on persistence failure. Recovery on
            # reopen handles only local, side-effect-free pending simulations.
            self.error.emit(
                "模拟结果保存失败，已停止；未完成预占保留，请检查存储后取消任务或重启恢复。"
            )
            self._end("模拟执行已停止，部分本地结果可能已保存，请核对任务。")

    def _end(self, summary):
        self.ended = True
        self.timer.stop()
        self.completed.emit(summary)
        self.finished.emit()
