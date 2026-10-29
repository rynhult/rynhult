"""Minimal Cloudflare DNS client for publishing. Only touches records Rynhult created (comment marker)."""
from __future__ import annotations

import ipaddress
import time

import httpx

API = "https://api.cloudflare.com/client/v4"
MARKER = "managed by ACC"


class CloudflareError(Exception):
    pass


class Cloudflare:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(base_url=API, headers={"Authorization": f"Bearer {token}"},
                                        timeout=20, transport=transport)
        self._zones: tuple[float, list[dict]] | None = None

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, path, **kwargs)
        data = response.json() if response.content else {}
        if response.status_code >= 400 or not data.get("success", False):
            errors = "; ".join(e.get("message", "") for e in data.get("errors", [])) or response.text[:200]
            raise CloudflareError(f"{method} {path}: {errors}")
        return data

    async def zones(self, refresh: bool = False) -> list[dict]:
        """Zones the token can edit DNS in (cached 5 minutes)."""
        if self._zones and not refresh and time.monotonic() - self._zones[0] < 300:
            return self._zones[1]
        data = await self._call("GET", "/zones", params={"per_page": 50})
        zones = [{"id": z["id"], "name": z["name"]} for z in data["result"]
                 if z.get("status") == "active" and "#dns_records:edit" in (z.get("permissions") or [])]
        self._zones = (time.monotonic(), zones)
        return zones

    async def zone_for(self, host: str) -> dict:
        matches = [z for z in await self.zones() if host == z["name"] or host.endswith("." + z["name"])]
        if not matches:
            raise CloudflareError(f"no editable Cloudflare zone contains {host}")
        return max(matches, key=lambda z: len(z["name"]))

    async def find(self, zone_id: str, host: str) -> list[dict]:
        data = await self._call("GET", f"/zones/{zone_id}/dns_records", params={"name": host})
        return data["result"]

    async def upsert_a(self, zone_id: str, host: str, ip: str, note: str) -> dict:
        """Create or update the host's A/AAAA record. Refuses records Rynhult did not create."""
        body = {"type": "AAAA" if ipaddress.ip_address(ip).version == 6 else "A", "name": host, "content": ip, "ttl": 300, "proxied": False,
                "comment": f"{MARKER} ({note})"}
        existing = await self.find(zone_id, host)
        for record in existing:
            if MARKER not in (record.get("comment") or ""):
                raise CloudflareError(f"{host} already has a {record['type']} record that Rynhult did not create")
        a_records = [r for r in existing if r["type"] in ("A", "AAAA")]
        if len(a_records) > 1:
            raise CloudflareError(f"{host} has multiple address records; reconcile them before publishing")
        if a_records:
            data = await self._call("PUT", f"/zones/{zone_id}/dns_records/{a_records[0]['id']}", json=body)
            return {**data["result"], "created": False}
        data = await self._call("POST", f"/zones/{zone_id}/dns_records", json=body)
        return {**data["result"], "created": True}

    async def delete_managed(self, zone_id: str, record_id: str) -> bool:
        data = await self._call("GET", f"/zones/{zone_id}/dns_records/{record_id}")
        if MARKER not in (data["result"].get("comment") or ""):
            raise CloudflareError("refusing to delete a DNS record Rynhult did not create")
        await self._call("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        return True

    async def close(self) -> None:
        await self.client.aclose()
