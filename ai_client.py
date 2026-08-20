# -*- coding: utf-8 -*-
"""
AI 客户端:调用 OpenAI 兼容的 Chat Completions 接口(默认 DeepSeek)。

v2 变更:
- complete() 支持动态 temperature(情绪系统用)。
- complete() 支持 tools / tool_calls 工具循环(工具调用能力)。

参数由 config.py 的系统配置注入;system_prompt 由人设 + 用户画像组装。
"""
import json
import time

import requests


class AIClient:
    def __init__(self, api_key, base_url="https://api.deepseek.com",
                 model="deepseek-v4-flash", system_prompt="",
                 temperature=0.7, max_tokens=4096, top_p=1.0, timeout=60,
                 frequency_penalty=None, presence_penalty=None,
                 supported_params=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.timeout = timeout
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        # 该模型支持的采样参数(按模型预设裁剪,不支持的字段不进请求体,避免 400)
        self.supported_params = (list(supported_params)
                                 if supported_params
                                 else ["temperature", "max_tokens", "top_p",
                                       "frequency_penalty", "presence_penalty"])

    def chat(self, history):
        """便捷入口:自动加 system prompt。history 为 role/content 列表。"""
        messages = [{"role": "system", "content": self.system_prompt}] + history
        return self.complete(messages)

    def _post(self, payload):
        """发一次请求,失败重试 3 次(等待 1s/2s/4s),返回 JSON。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=headers, timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
        raise RuntimeError(f"AI 接口调用失败: {last_err}")

    def complete(self, messages, temperature=None, max_tokens=None,
                 tools=None, tool_executor=None, max_tool_rounds=4):
        """发送任意 messages(不自动加 system),返回文本回复。

        - temperature/max_tokens: 传入则覆盖实例默认值(动态温度用)。
        - tools: OpenAI function-calling 工具列表。
        - tool_executor: callable(name:str, args:dict) -> str,执行工具并返回结果。
          收到 tool_calls 时执行并把结果回填,循环直到出最终文本或超轮数。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        # 只发送该模型支持的采样参数(按预设过滤,避免部分模型 400 报错)
        sp = set(self.supported_params)
        if "temperature" in sp:
            payload["temperature"] = self.temperature if temperature is None else temperature
        if "max_tokens" in sp:
            payload["max_tokens"] = self.max_tokens if max_tokens is None else max_tokens
        if "top_p" in sp:
            payload["top_p"] = self.top_p
        if "frequency_penalty" in sp and self.frequency_penalty is not None:
            payload["frequency_penalty"] = self.frequency_penalty
        if "presence_penalty" in sp and self.presence_penalty is not None:
            payload["presence_penalty"] = self.presence_penalty
        if tools:
            payload["tools"] = tools

        msgs = list(messages)
        for _ in range(max_tool_rounds + 1):
            payload["messages"] = msgs
            data = self._post(payload)
            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls or tool_executor is None:
                return (msg.get("content") or "").strip()
            # 回填 assistant 的 tool_calls 与各工具结果
            msgs.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                try:
                    result = str(tool_executor(name, args))
                except Exception as e:
                    result = f"[工具执行失败] {e}"
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })
        raise RuntimeError("工具调用轮数超限,已终止本轮对话")

    # ---- DeepSeek 原生联网搜索(Responses API) ----

    @staticmethod
    def _extract_response_text(data):
        """从 Responses API 返回里取最终回答文本。

        多步搜索流程中 message 会多次出现(边搜边说的中间话),
        最终答案一定是最后一条 message 的 output_text。
        """
        final = ""
        for it in data.get("output") or []:
            if it.get("type") == "message":
                for c in it.get("content") or []:
                    if c.get("type") == "output_text" and c.get("text"):
                        final = str(c["text"]).strip()
        return final

    def complete_with_search(self, messages, temperature=None, max_tokens=None):
        """DeepSeek 官方内置联网搜索(Responses API + web_search 工具)。

        模型自己决定是否搜索、搜什么,并基于搜索结果给出最终回答;
        调用失败抛 RuntimeError(service 层会回退到内置 Bing 搜索工具)。
        """
        payload = {
            "model": self.model,
            "input": messages,
            "tools": [{"type": "web_search"}],
            "stream": False,
        }
        sp = set(self.supported_params)
        if "temperature" in sp:
            payload["temperature"] = self.temperature if temperature is None else temperature
        if "max_tokens" in sp:
            payload["max_output_tokens"] = self.max_tokens if max_tokens is None else max_tokens
        if "top_p" in sp:
            payload["top_p"] = self.top_p
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{self.base_url}/responses",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                text = self._extract_response_text(resp.json())
                if text:
                    return text
                raise RuntimeError("Responses API 返回为空")
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"原生联网搜索调用失败: {last_err}")
