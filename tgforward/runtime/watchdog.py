"""独立线程观察事件循环心跳，超时退出进程，由容器 restart policy 接管。"""

import os
import threading
import time


class Watchdog:
    def __init__(self, timeout=180, interval=5, exit_process=os._exit):
        self.timeout = timeout
        self.interval = interval
        self.exit_process = exit_process
        self.last_beat = time.monotonic()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="event-loop-watchdog", daemon=True)

    def beat(self):
        self.last_beat = time.monotonic()

    def start(self):
        self.beat()
        self.thread.start()

    def _watch(self):
        while not self.stopped.wait(self.interval):
            if time.monotonic() - self.last_beat > self.timeout:
                # 不依赖可能被阻塞协程持有的 logging 锁。
                try:
                    os.write(2, b"event-loop heartbeat expired; exiting for restart\n")
                finally:
                    self.exit_process(70)
                return

    def close(self):
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)
