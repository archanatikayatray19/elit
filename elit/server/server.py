# -*- coding:utf-8 -*-
# Author: hankcs
# Date: 2020-12-13 19:02
import asyncio
import functools
import traceback
from typing import List, Any

import uvicorn
from fastapi import FastAPI
from fastapi.logger import logger
from elit.common.document import Document
from elit.server.en import en
from elit.server.format import Input
from elit.server.service import Service

app = FastAPI()


class HandlingError(Exception):
    def __init__(self, msg, code=500):
        super().__init__()
        self.code = code
        self.msg = msg


class ModelRunner(object):
    def __init__(self, service:Service, max_queue_size=128, max_batch_size=32, max_wait=0.05):
        """

        Args:
            max_queue_size: we accept a backlog of MAX_QUEUE_SIZE before handing out "Too busy" errors
            max_batch_size: we put at most MAX_BATCH_SIZE things in a single batch
            max_wait: we wait at most MAX_WAIT seconds before running for more inputs to arrive in batching
        """
        self.service = service
        self.max_wait = max_wait
        self.max_batch_size = max_batch_size
        self.max_queue_size = max_queue_size
        self.queue = []
        self.queue_lock = None
        self.model = None
        self.needs_processing = None
        self.needs_processing_timer = None

    def schedule_processing_if_needed(self, loop):
        if len(self.queue) >= self.max_batch_size:
            logger.info("next batch ready when processing a batch")
            self.needs_processing.set()
        elif self.queue:
            logger.info("queue nonempty when processing a batch, setting next timer")
            self.needs_processing_timer = loop.call_at(self.queue[0]["time"] + self.max_wait, self.needs_processing.set)

    async def process_input(self, input):
        loop = asyncio.get_running_loop()
        our_task = {"done_event": asyncio.Event(loop=loop),
                    "input": input,
                    "time": loop.time()}
        async with self.queue_lock:
            if len(self.queue) >= self.max_queue_size:
                raise HandlingError("I'm too busy", code=503)
            self.queue.append(our_task)
            logger.info("enqueued task. new queue size {}".format(len(self.queue)))
            self.schedule_processing_if_needed(loop)

        await our_task["done_event"].wait()
        return our_task["output"]

    def run_model(self, batch: List[Input]) -> List[Any]:  # runs in other thread
        try:
            return self.service.parse(batch)
        except Exception as e:
            traceback.print_exc()
            return [e for _ in batch]

    async def model_runner(self):
        loop = asyncio.get_running_loop()
        self.queue_lock = asyncio.Lock(loop=loop)
        self.needs_processing = asyncio.Event(loop=loop)
        logger.info("started model runner")
        while True:
            logger.info('Waiting for needs_processing')
            await self.needs_processing.wait()
            self.needs_processing.clear()
            if self.needs_processing_timer is not None:
                self.needs_processing_timer.cancel()
                self.needs_processing_timer = None
            logger.info('Locking queue_lock')
            async with self.queue_lock:
                if self.queue:
                    longest_wait = loop.time() - self.queue[0]["time"]
                else:  # oops
                    longest_wait = None
                logger.info(
                    "launching processing. queue size: {}. longest wait: {}".format(len(self.queue), longest_wait))
                to_process = self.queue[:self.max_batch_size]
                del self.queue[:len(to_process)]
                self.schedule_processing_if_needed(loop)
            # so here we copy, it would be neater to avoid this
            batch = [t["input"] for t in to_process]
            result = await loop.run_in_executor(
                None, functools.partial(self.run_model, batch)
            )
            for t, r in zip(to_process, result):
                t["output"] = r
                t["done_event"].set()
            del to_process


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(runner.model_runner())


runner = ModelRunner(en)


# noinspection PyShadowingBuiltins
@app.post("/parse")
async def parse(input: Input):
    output: Document = await runner.process_input(input)
    if not isinstance(output, Document):
        raise HandlingError("Internal Server Error", code=500)
    return output.to_dict()


@app.get("/parse")
async def parse(text: str):
    input = Input(text=text)
    output: Document = await runner.process_input(input)
    if not isinstance(output, Document):
        raise HandlingError("Internal Server Error", code=500)
    return output.to_dict()


def run(host='0.0.0.0', port=8000, reload=False):
    uvicorn.run('elit.server.server:app', host=host, port=port, reload=reload)


def main():
    run(reload=True)


if __name__ == '__main__':
    main()
