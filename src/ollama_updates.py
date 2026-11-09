"""Ollama-specific explicit pulls while shared update checks retain their contract."""

from typing import Any

from updates import Updates


class OllamaUpdates(Updates):
    """An explicit Download pulls even when the local digest has not changed."""

    async def _check(self) -> dict[str, Any]:
        await super()._check()
        # Only the installed digest is known; this flag offers refresh, not a newer release.
        for row in self._status["weights"]:
            row["update"] = row["cached"] is None
        return self.status()

    async def update_weights(self) -> dict[str, Any]:
        async with self._lock:
            try:
                self._unload_pending = True
                await self._download()
                await self._unload()
                self._unload_pending = False
                return await self._check()
            except Exception as error:
                self._status = {**self._status, "error": f"{type(error).__name__}: {error}"}
                raise
