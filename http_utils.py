"""Retry wrappers for httpx calls used across MI scripts.

Retryable: network errors (ConnectError, ReadError, RemoteProtocolError,
           timeouts) and 5xx responses — up to max_retries times, 2s/4s backoff.
Non-retryable: 4xx (caller error), json.JSONDecodeError (model output issue).
All failures return (None, error_str); success returns (dict, None).
"""
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
)


def post_with_retry(url: str, *, headers: dict, json_body: dict,
                    timeout: float, max_retries: int = 2) -> tuple:
    """POST url with JSON body; retry on network errors and 5xx.

    Returns (response_dict, None) on success, (None, error_str) on failure.
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(url, headers=headers, json=json_body, timeout=timeout)
            resp.raise_for_status()
            return resp.json(), None
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                return None, f"HTTP {e.response.status_code}: {e}"
            last_err = e
        except json.JSONDecodeError as e:
            return None, f"JSON decode error: {e}"
        except _RETRYABLE as e:
            last_err = e
        except Exception as e:
            last_err = e

        if attempt < max_retries:
            wait = 2 ** (attempt + 1)
            logger.warning("POST attempt %d/%d failed (%s), retrying in %ds",
                           attempt + 1, max_retries + 1, last_err, wait)
            time.sleep(wait)

    return None, f"all {max_retries + 1} attempts failed: {last_err}"


def extract_llm_text(msg: dict) -> str:
    """DeepSeek/reasoning 模型兼容取值：优先 content，为空则退到思考链字段。

    推理模型（V4 Flash/Pro）先在 reasoning_content（或旧字段 reasoning）里写
    思考链，最终答案写入 content；token 预算不够时 content 可能为 null，此时
    最终答案实际落在 reasoning_content 里。所有 DeepSeek 调用点取值都应走这里，
    不要各自手写 `content or reasoning...` 链（见 PITFALLS.md #1/#11/#26，issue #10）。
    """
    return (msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or "").strip()


def get_with_retry(url: str, *, params: dict, timeout: float,
                   max_retries: int = 2) -> tuple:
    """GET url with query params; retry on network errors and 5xx.

    Returns (response_dict, None) on success, (None, error_str) on failure.
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json(), None
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                return None, f"HTTP {e.response.status_code}: {e}"
            last_err = e
        except json.JSONDecodeError as e:
            return None, f"JSON decode error: {e}"
        except _RETRYABLE as e:
            last_err = e
        except Exception as e:
            last_err = e

        if attempt < max_retries:
            wait = 2 ** (attempt + 1)
            logger.warning("GET attempt %d/%d failed (%s), retrying in %ds",
                           attempt + 1, max_retries + 1, last_err, wait)
            time.sleep(wait)

    return None, f"all {max_retries + 1} attempts failed: {last_err}"
