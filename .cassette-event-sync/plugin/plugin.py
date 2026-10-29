"""DroppedNeedle v1 subscriber: a notification only, no library mutation."""

class CassetteProfileEvents:
    def __init__(self, context):
        self.ctx = context

    async def on_event(self, event):
        if getattr(event, "kind", None) != "import_finished":
            return
        url = str(self.ctx.settings.get("receiver_url") or "").strip()
        secret = str(self.ctx.settings.get("shared_secret") or "").strip()
        if not url or not secret or secret.startswith("plugin****"):
            self.ctx.logger.warning("Cassette sync not configured")
            return
        payload = getattr(event, "payload", None)
        rg = getattr(payload, "release_group_mbid", None)
        causation = getattr(event, "causation_id", None)
        data = {"kind": "import_finished", "release_group_mbid": str(rg or "")[:100],
                "causation_id": str(causation or "")[:128]}
        try:
            resp = await self.ctx.http.post(url, json=data, headers={"X-Event-Secret": secret}, timeout=3)
            if not 200 <= resp.status_code < 300:
                self.ctx.logger.warning("Cassette sync receiver HTTP %s", resp.status_code)
        except Exception as exc:
            self.ctx.logger.warning("Cassette sync receiver unreachable (%s)", type(exc).__name__)
