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
