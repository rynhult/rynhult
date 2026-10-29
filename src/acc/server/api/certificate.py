"""Settings → Certificate: which certificate Rynhult is served with, and downloads that make a device trust a private one.

A private CA is the file in ACC_NODE_CA_FILE: Caddy's own root when Caddy signs Rynhult's certificate (deploy/install.sh
copies it to data/node-ca.pem), or another root an owner configured. The details are for owners; the downloads are
for anyone signed in (each person adds the root on their own devices).
"""
from __future__ import annotations

import asyncio
import hashlib
import plistlib
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .context import Context
from .nodes import publicly_trusted


def load_root(path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as error:
        raise HTTPException(404, f"Rynhult's root certificate can't be read ({error}). An owner can check the certificate file on the server.") from None


def mobileconfig(certificate: x509.Certificate, label: str) -> bytes:
    """An Apple configuration profile holding only the root (iPhone, iPad, Mac). Its ids follow from the root's
    fingerprint, so downloading it again replaces the profile instead of adding another."""
    der = certificate.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()
    identifier = f"acc.root.{fingerprint[:16]}"
    payload = {
        "PayloadType": "com.apple.security.root",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{identifier}.certificate",
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"acc-root-certificate:{fingerprint}")).upper(),
        "PayloadDisplayName": f"Rynhult root certificate ({label})",
        "PayloadCertificateFileName": "rynhult-root.cer",
        "PayloadContent": der,
    }
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": identifier,
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"acc-root-profile:{fingerprint}")).upper(),
        "PayloadDisplayName": f"Rynhult ({label})",
        "PayloadDescription": "Lets this device trust the private certificate Rynhult is served with. It holds this "
                              "one root certificate and nothing else.",
        "PayloadOrganization": "Rynhult",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [payload],
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML)


def router(ctx: Context) -> APIRouter:
    api = APIRouter()
    settings = ctx.settings

    def primary() -> str:
        return next((o for o in settings.allowed_origins if o.startswith("https://")), settings.allowed_origins[0])

    @api.get("/api/certificate")
    async def certificate(who=Depends(ctx.owner)):
        """public: browsers and nodes trust it as it is; caddy: Caddy's own CA (copied at install); file: another
        private root (ACC_NODE_CA_FILE); unverified: neither (a certificate nothing here can vouch for)."""
        path = settings.node_ca_file
        if path is None:
            trusted = await asyncio.to_thread(publicly_trusted, primary())
            return {"kind": "public" if trusted else "unverified", "primary": primary()}
        root = load_root(path)
        der = root.public_bytes(serialization.Encoding.DER)
        return {"kind": "caddy" if path == settings.data_dir / "node-ca.pem" else "file", "primary": primary(),
                "file": str(path), "subject": root.subject.rfc4514_string(), "issuer": root.issuer.rfc4514_string(),
                "fingerprint": hashlib.sha256(der).hexdigest(),
                "not_before": root.not_valid_before_utc.isoformat(), "not_after": root.not_valid_after_utc.isoformat()}

    def private_root() -> x509.Certificate:
        if settings.node_ca_file is None:
            raise HTTPException(404, "This Rynhult uses a publicly trusted certificate, so your devices trust it already. There's nothing to install.")
        return load_root(settings.node_ca_file)

    # The acc-root names stay as aliases: links, bookmarks and scripts from before the rename keep working.
    @api.get("/api/certificate/rynhult-root.crt")
    @api.get("/api/certificate/acc-root.crt")
    def root_crt(who=Depends(ctx.principal)):
        """The root in PEM (Windows, Linux and Android accept a .crt in PEM)."""
        pem = private_root().public_bytes(serialization.Encoding.PEM)
        return Response(pem, media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="rynhult-root.crt"'})

    @api.get("/api/certificate/rynhult-root.mobileconfig")
    @api.get("/api/certificate/acc-root.mobileconfig")
    def root_profile(who=Depends(ctx.principal)):
        return Response(mobileconfig(private_root(), settings.host_label),
                        media_type="application/x-apple-aspen-config",
                        headers={"Content-Disposition": 'attachment; filename="rynhult-root.mobileconfig"'})

    return api

