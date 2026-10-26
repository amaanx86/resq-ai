"""Chooses and validates the WhatsApp provider from settings (WHATSAPP_PROVIDER).

Rules:
* `meta` needs WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_VERIFY_TOKEN.
* `eum_social` needs EUM_SOCIAL_PHONE_NUMBER_ID (`phone-number-id-...` or its ARN) and AWS
  credentials for the `socialmessaging` client (the ECS task role in AWS).
* `mock` is only allowed for local runs.
* If the chosen provider is missing settings: local runs (MOCK_AWS=True, or AWS_ENDPOINT_URL set,
  i.e. Floci) fall back to MockProvider with a loud warning; real AWS (MOCK_AWS=False, no
  endpoint) raises RuntimeError so the task fails fast instead of silently not replying.
"""
import re
from typing import Any, Callable, List, Optional

from app.config import settings as default_settings
from app.messaging.providers import EumSocialProvider, MetaCloudProvider, MockProvider, WhatsAppProvider

VALID_PROVIDERS = ("meta", "eum_social", "mock")
PLACEHOLDER_PREFIXES = ("mock_", "test_", "your_", "<")
EUM_PHONE_ID_PATTERN = re.compile(r"^(phone-number-id-[0-9a-zA-Z]+|arn:.*:phone-number-id/[0-9a-zA-Z]+)$")


def _is_set(value: Optional[str]) -> bool:
    value = (value or "").strip()
    return bool(value) and not value.lower().startswith(PLACEHOLDER_PREFIXES)


def _aws_credentials_available() -> bool:
    try:
        import boto3
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def is_local_runtime(cfg: Any = None) -> bool:
    cfg = cfg or default_settings
    return bool(cfg.MOCK_AWS or cfg.AWS_ENDPOINT_URL)


def missing_settings(
    cfg: Any, provider: str, credentials_check: Callable[[], bool] = _aws_credentials_available
) -> List[str]:
    """Names of the settings the given provider needs but doesn't have."""
    missing: List[str] = []
    if provider == "meta":
        for name in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN"):
            if not _is_set(getattr(cfg, name, "")):
                missing.append(name)
    elif provider == "eum_social":
        phone_id = (getattr(cfg, "EUM_SOCIAL_PHONE_NUMBER_ID", "") or "").strip()
        if not EUM_PHONE_ID_PATTERN.match(phone_id):
            missing.append("EUM_SOCIAL_PHONE_NUMBER_ID (phone-number-id-... or its ARN)")
        if not credentials_check():
            missing.append("AWS credentials (IAM task role) for socialmessaging")
    return missing


def build_provider(
    cfg: Any = None,
    credentials_check: Callable[[], bool] = _aws_credentials_available,
    eum_client: Any = None,
) -> WhatsAppProvider:
    cfg = cfg or default_settings
    choice = (getattr(cfg, "WHATSAPP_PROVIDER", "meta") or "meta").strip().lower()
    local = is_local_runtime(cfg)

    def fail_or_mock(reason: str) -> WhatsAppProvider:
        if not local:
            raise RuntimeError(
                f"WhatsApp provider '{choice}' is not usable: {reason}. "
                "Set the missing settings, or WHATSAPP_PROVIDER to a configured provider "
                "(MOCK_AWS=False refuses to fall back to the mock provider)."
            )
        bar = "!" * 78
        print(
            f"\n{bar}\n[WhatsApp] WARNING: provider '{choice}' is not usable: {reason}.\n"
            "[WhatsApp] Falling back to the MOCK provider: outbound messages are only printed, "
            "nothing reaches a phone.\n"
            f"{bar}\n"
        )
        return MockProvider()

    if choice not in VALID_PROVIDERS:
        return fail_or_mock(f"unknown WHATSAPP_PROVIDER (expected one of {', '.join(VALID_PROVIDERS)})")

    if choice == "mock":
        if not local:
            raise RuntimeError("WHATSAPP_PROVIDER=mock is only allowed for local runs (MOCK_AWS=True or Floci).")
        print("[WhatsApp] Provider: mock (WHATSAPP_PROVIDER=mock). Outbound messages are printed only.")
        return MockProvider()

    missing = missing_settings(cfg, choice, credentials_check)
    if missing:
        return fail_or_mock("missing " + ", ".join(missing))

    api_version = getattr(cfg, "WHATSAPP_API_VERSION", "v20.0") or "v20.0"
    if choice == "meta":
        provider = MetaCloudProvider(
            access_token=cfg.WHATSAPP_ACCESS_TOKEN,
            phone_number_id=cfg.WHATSAPP_PHONE_NUMBER_ID,
            api_version=api_version,
        )
        print(f"[WhatsApp] Provider: meta (Graph API {api_version}, phone number ID {cfg.WHATSAPP_PHONE_NUMBER_ID})")
        return provider

    try:
        provider = EumSocialProvider(
            phone_number_id=cfg.EUM_SOCIAL_PHONE_NUMBER_ID.strip(),
            api_version=api_version,
            client=eum_client,
        )
    except Exception as e:
        return fail_or_mock(f"could not create the socialmessaging client ({e})")
    print(
        f"[WhatsApp] Provider: eum_social (AWS End User Messaging Social, {cfg.EUM_SOCIAL_PHONE_NUMBER_ID}, "
        f"Meta API {api_version})"
    )
    return provider
