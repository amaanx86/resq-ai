"""WhatsApp messaging: one internal message format, two providers (Meta Cloud API, AWS End User
Messaging Social) plus a mock, switched only by the WHATSAPP_PROVIDER setting.

* `inbound`: raw provider payloads -> `InboundWhatsAppMessage` (shared `messages[]` parsing).
* `providers`: outbound send, media download and media upload per provider.
* `factory`: provider selection and startup validation.
Callers use the `app.services.whatsapp_service.whatsapp_service` facade for outbound messages.
"""
from app.messaging.inbound import (
    UnrecognizedMessageError,
    parse_eum_social_event,
    parse_meta_webhook,
    parse_queue_message,
    verify_meta_signature,
)
from app.messaging.providers import (
    EumSocialProvider,
    MediaFetchError,
    MediaUploadError,
    MetaCloudProvider,
    MockProvider,
    WhatsAppProvider,
)
from app.messaging.factory import build_provider

__all__ = [
    "UnrecognizedMessageError",
    "parse_eum_social_event",
    "parse_meta_webhook",
    "parse_queue_message",
    "verify_meta_signature",
    "EumSocialProvider",
    "MediaFetchError",
    "MediaUploadError",
    "MetaCloudProvider",
    "MockProvider",
    "WhatsAppProvider",
    "build_provider",
]
