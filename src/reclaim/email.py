"""Email stub — log the would-be message so a real provider can be wired later.

Reclaim never sends money-moving content without it being traceable; this stub
is where a provider (SendGrid/Postmark/Resend...) would be injected. For now it
logs the full would-be email so the demo's audit trail shows exactly what would
have been sent, and swaps in with one function body.
"""

from __future__ import annotations

import logging

from .config import Settings

logger = logging.getLogger("reclaim.email")


def send_email_message(
    *,
    to: str,
    template: str,
    context: dict[str, object],
    settings: Settings | None = None,
) -> None:
    """Log (and, in stub mode, nothing more) a transactional email.

    ``template`` + ``context`` describe the would-be message; a real provider
    would render ``context`` into ``template`` and deliver it. Never raises —
    email is a best-effort side channel, failures are logged not fatal.
    """
    body = f"template={template} context={context}"
    logger.info(
        "EMAIL_STUB to=%s sub=%s (stub; wire to a real provider): %s",
        to,
        f"[{template}]",
        body,
    )
    # NOTE: when wiring a real provider, invoke it here. Keep this function the
    # single seam Reclaim uses for outbound email.
    return None
