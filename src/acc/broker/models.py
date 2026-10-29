"""Model catalog per provider, refreshed nightly by accd (each provider's own source: see acc.providers)."""
from __future__ import annotations

import time
from pathlib import Path

from .. import providers
from . import image_probe, ops
from .config import BrokerConfig


def model_catalog(config: BrokerConfig) -> dict:
    catalog: dict = {"fetched_at": time.time(), "errors": {}}
    for provider_id, path in ops.installed_providers(config).items():
        try:
            catalog[provider_id] = providers.get(provider_id).models(path or providers.get(provider_id).executable,
                                                                      Path.home()) if path else []
            fingerprint = image_probe.identity(config, provider_id)
            catalog[provider_id] = [{**row, **fingerprint, "image_input": None} for row in catalog[provider_id]]
        except InterruptedError:
            raise  # cancellation ends the catalog probe, rather than advancing to another CLI
        except Exception as error:  # e.g. an app-server that doesn't answer
            catalog[provider_id] = []
            catalog["errors"][provider_id] = f"{type(error).__name__}: {error}"
    return catalog
