"""Gong API client.

Two endpoints: the call list and call details. Gong's transcript endpoint is
deliberately not called — Quorum stores meeting metadata and attendees, not
transcript text, and `meetings` has no column to put it in.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any, Optional

import requests

TIMEOUT = 60


class GongError(RuntimeError):
    pass


class GongClient:
    def __init__(self, access_key: str, access_key_secret: str,
                 base_url: str = "https://api.gong.io") -> None:
        token = base64.b64encode(f"{access_key}:{access_key_secret}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self._base_url = base_url.rstrip("/")

    def list_calls(
        self, from_date: str, to_date: str, cursor: Optional[str] = None
    ) -> dict[str, Any]:
        """GET /v2/calls — one page of the call list for a date range."""
        params = {
            "fromDateTime": _iso_start(from_date),
            "toDateTime": _iso_end(to_date),
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            f"{self._base_url}/v2/calls", headers=self._headers, params=params, timeout=TIMEOUT
        )
        if not resp.ok:
            # An empty range is a 404 with a specific body, not an error.
            if resp.status_code == 404 and "No calls found" in resp.text:
                return {"calls": [], "records": {"totalRecords": 0, "currentPageSize": 0}}
            raise GongError(f"Gong listCalls failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def get_calls_extensive(self, call_ids: list[str]) -> dict[str, Any]:
        """POST /v2/calls/extensive — call metadata plus parties."""
        resp = requests.post(
            f"{self._base_url}/v2/calls/extensive",
            headers=self._headers,
            json={
                "filter": {"callIds": call_ids},
                "contentSelector": {"exposedFields": {"parties": True}},
            },
            timeout=TIMEOUT,
        )
        if not resp.ok:
            raise GongError(
                f"Gong getCallsExtensive failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def iter_call_ids(self, from_date: str, to_date: str):
        """Every call id in the range, following the cursor."""
        cursor: Optional[str] = None
        while True:
            page = self.list_calls(from_date, to_date, cursor)
            for call in page.get("calls") or []:
                if call.get("id"):
                    yield call["id"]
            cursor = (page.get("records") or {}).get("cursor")
            if not cursor:
                return


def _iso_start(date: str) -> str:
    return dt.datetime.fromisoformat(f"{date}T00:00:00+00:00").isoformat().replace(
        "+00:00", "Z"
    )


def _iso_end(date: str) -> str:
    return dt.datetime.fromisoformat(f"{date}T23:59:59+00:00").isoformat().replace(
        "+00:00", "Z"
    )
