"""
Webhooks module for AIMScribe
Handles outbound webhooks to external systems like CMED.
"""
from .cmed_webhook import send_ner_webhook, send_status_webhook, WebhookResult

__all__ = ['send_ner_webhook', 'send_status_webhook', 'WebhookResult']
