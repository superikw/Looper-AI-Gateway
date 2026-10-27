from __future__ import annotations

import json
import time
import uuid

import anyio
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.db import AsyncSessionLocal
from app.protocol import ResponseAccumulator, normalize_codex_response_to_chat, response_failed
from app.services import (open_upstream, iter_upstream_events, read_upstream_data,
                          format_upstream_result, request_id, record_usage)


def sse(data: dict, event: str | None = None) -> str:
    prefix = f'event: {event}\n' if event else ''
    return prefix + 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'


class ChatStream:
    def __init__(self, model: str, include_usage: bool = False):
        self.id = 'chatcmpl-' + uuid.uuid4().hex
        self.model = model
        self.created = int(time.time())
        self.started = False
        self.text_sent = False
        self.refusal_sent = False
        self.calls = {}
        self.arguments_sent = set()
        self.include_usage = include_usage

    def chunk(self, delta: dict, finish: str | None = None) -> dict:
        return {'id':self.id, 'object':'chat.completion.chunk', 'created':self.created, 'model':self.model,
                'choices':[{'index':0, 'delta':delta, 'finish_reason':finish}]}

    def emit(self, delta: dict) -> list[dict]:
        chunks = []
        if not self.started:
            self.started = True
            chunks.append(self.chunk({'role':'assistant', 'content':''}))
        chunks.append(self.chunk(delta))
        return chunks

    def add_call(self, item: dict, output_index: int) -> list[dict]:
        if output_index in self.calls:
            return []
        index = len(self.calls)
        self.calls[output_index] = index
        arguments = item.get('arguments', '')
        if arguments:
            self.arguments_sent.add(output_index)
        return self.emit({'tool_calls':[{'index':index, 'id':item.get('call_id', item.get('id')), 'type':'function',
                                        'function':{'name':item.get('name'), 'arguments':arguments}}]})

    def feed(self, event: dict) -> list[dict]:
        kind = event.get('type')
        response = event.get('response') or {}
        if not self.started and response.get('id'):
            self.id = response['id']
            self.created = int(response.get('created_at') or self.created)
        if kind == 'response.output_text.delta':
            self.text_sent = True
            return self.emit({'content':event.get('delta', '')})
        if kind == 'response.refusal.delta':
            self.refusal_sent = True
            return self.emit({'refusal':event.get('delta', '')})
        item = event.get('item') or {}
        output_index = event.get('output_index', 0)
        if kind in ('response.output_item.added', 'response.output_item.done') and item.get('type') == 'function_call':
            chunks = self.add_call(item, output_index)
            if kind == 'response.output_item.done' and output_index not in self.arguments_sent and item.get('arguments'):
                self.arguments_sent.add(output_index)
                chunks += self.emit({'tool_calls':[{'index':self.calls[output_index], 'function':{'arguments':item['arguments']}}]})
            return chunks
        if kind in ('response.function_call_arguments.delta', 'response.function_call_arguments.done'):
            if output_index not in self.calls:
                raise HTTPException(502, 'upstream tool arguments arrived before the tool call')
            arguments = event.get('delta', '') if kind.endswith('.delta') else event.get('arguments', '')
            if kind.endswith('.done') and output_index in self.arguments_sent:
                return []
            self.arguments_sent.add(output_index)
            return self.emit({'tool_calls':[{'index':self.calls[output_index], 'function':{'arguments':arguments}}]})
        return []

    def finish(self, response: dict) -> list[dict]:
        chunks = []
        chat = normalize_codex_response_to_chat(response, self.model)
        message = chat['choices'][0]['message']
        if not self.text_sent and message.get('content'):
            chunks += self.emit({'content':message['content']})
        if not self.refusal_sent and message.get('refusal'):
            chunks += self.emit({'refusal':message['refusal']})
        for output_index, item in enumerate(response.get('output') or []):
            if item.get('type') == 'function_call':
                chunks += self.add_call(item, output_index)
                if output_index not in self.arguments_sent and item.get('arguments'):
                    self.arguments_sent.add(output_index)
                    chunks += self.emit({'tool_calls':[{'index':self.calls[output_index], 'function':{'arguments':item['arguments']}}]})
        if not self.started:
            chunks += self.emit({'content':''})
        chunks.append(self.chunk({}, chat['choices'][0]['finish_reason']))
        if self.include_usage:
            usage = self.chunk({})
            usage['choices'] = []
            usage['usage'] = chat['usage']
            chunks.append(usage)
        return chunks


async def stream_to_upstream(path: str, payload: dict, db, api_key):
    response = await open_upstream(path, payload, db)
    rid = request_id(response)
    try:
        if response.status_code >= 400 or 'text/event-stream' not in response.headers.get('content-type', ''):
            data = await read_upstream_data(response)
            code, result = format_upstream_result(path, payload, response.status_code, data)
            if code >= 400:
                await response.aclose()
                await record_usage(db, api_key, path, payload['model'], code, result, rid)
                return JSONResponse(result, status_code=code)
            async def json_events():
                yield {'type':'response.completed', 'response':data}
            events = json_events()
        else:
            events = iter_upstream_events(response)
        first = await anext(events, None)
        initial = ResponseAccumulator()
        if first is not None:
            initial.add(first)
        if first is None or (initial.terminal and response_failed(initial.result())):
            code, result = format_upstream_result(path, payload, 200, initial.result())
            await response.aclose()
            await record_usage(db, api_key, path, payload['model'], code, result, rid)
            return JSONResponse(result, status_code=code)
    except BaseException:
        with anyio.CancelScope(shield=True):
            await response.aclose()
        raise

    accumulator = ResponseAccumulator()
    chat = ChatStream(payload['model'], (payload.get('stream_options') or {}).get('include_usage', False))
    state = {'code':499, 'data':{'error':{'message':'client disconnected'}}, 'recorded':False}

    async def cleanup():
        if state['recorded']:
            return
        state['recorded'] = True
        # FastAPI may close the request-scoped DB session before streaming.
        # A fresh session and a cancellation shield preserve the final audit row.
        with anyio.CancelScope(shield=True):
            await response.aclose()
            async with AsyncSessionLocal() as usage_db:
                await record_usage(usage_db, api_key, path, payload['model'], state['code'], state['data'], rid)

    async def body():
        try:
            async def prefixed():
                yield first
                async for event in events:
                    yield event
            async for event in prefixed():
                accumulator.add(event)
                if response_failed(accumulator.response):
                    break
                if path == '/v1/responses':
                    yield sse(event, event.get('type'))
                else:
                    for chunk in chat.feed(event):
                        yield sse(chunk)
                if accumulator.terminal:
                    break
            data = accumulator.result()
            code, result = format_upstream_result(path, payload, 200, data)
            state.update(code=code, data=result)
            if code >= 400:
                yield sse(result, 'error' if path == '/v1/responses' else None)
            elif path == '/v1/chat/completions':
                for chunk in chat.finish(data):
                    yield sse(chunk)
            if path == '/v1/chat/completions':
                yield 'data: [DONE]\n\n'
        except HTTPException as exc:
            error = {'error':{'message':str(exc.detail), 'type':'upstream_error'}}
            state.update(code=exc.status_code, data=error)
            yield sse(error, 'error' if path == '/v1/responses' else None)
            if path == '/v1/chat/completions':
                yield 'data: [DONE]\n\n'
        finally:
            await cleanup()

    headers = {'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'}
    if rid:
        headers['x-request-id'] = rid
    return StreamingResponse(body(), media_type='text/event-stream', headers=headers, background=BackgroundTask(cleanup))
