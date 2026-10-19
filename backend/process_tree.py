"""Track only a collection subprocess and its descendants, including detached Chrome."""

import asyncio
import os
import signal

import psutil


class CrawlerProcessTree:
    def __init__(self, pid):
        self.processes = {}
        self.closed = False
        try:
            self.remember(psutil.Process(pid))
        except psutil.Error:
            pass

    def remember(self, process):
        self.processes[(process.pid, process.create_time())] = process

    def scan(self):
        for process in tuple(self.processes.values()):
            try:
                if process.is_running():
                    for child in process.children(recursive=True):
                        self.remember(child)
            except (psutil.Error, PermissionError):
                pass

    async def watch(self):
        while not self.closed:
            # psutil's recursive process enumeration can block briefly on macOS;
            # keep it off the collection event loop so other slots continue.
            await asyncio.to_thread(self.scan)
            await asyncio.sleep(0.25)

    def kill(self):
        if self.closed:
            return
        self.closed = True
        self.scan()
        # Freeze parents before killing so they cannot launch replacement
        # children between the process-tree snapshot and termination.
        for process in tuple(self.processes.values()):
            try:
                process.suspend()
            except psutil.Error:
                pass
        self.scan()
        for process in reversed(tuple(self.processes.values())):
            try:
                if not process.is_running():
                    continue
                # Playwright puts Chrome in a separate process group on macOS.
                # Kill such groups only when this tracked process owns the group.
                if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (psutil.Error, ProcessLookupError, PermissionError):
                pass
