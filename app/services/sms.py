import logging
from typing import Dict, Optional

from twilio.rest import Client

from app.services.encryption import decrypt

logger = logging.getLogger(__name__)


def validate_sms_config(account_sid: Optional[str] = None, auth_token: Optional[str] = None, phone_number: Optional[str] = None) -> bool:
    return all([account_sid, auth_token, phone_number])


def prepare_sms_variables(lead: Dict) -> Dict[str, str]:
    address = lead.get("address", "")
    parts = address.split(",") if address else []
    city = parts[-2].strip() if len(parts) >= 2 else "your area"
    return {
        "business_name": lead.get("name", ""),
        "name": lead.get("name", ""),
        "city": city,
        "score": str(lead.get("score", 0)),
        "phone": lead.get("phone", ""),
        "website": lead.get("website", ""),
    }


def render_sms_template(template: str, variables: Dict[str, str]) -> str:
    message = template
    for key, value in variables.items():
        message = message.replace(f"{{{{{key}}}}}", value)
    return message


def send_sms(
    to_phone: str,
    message: str,
    account_sid: str,
    auth_token: str,
    phone_number: str,
) -> Dict:
    if not validate_sms_config(account_sid, auth_token, phone_number):
        raise ValueError("Twilio configuration incomplete. Set up API keys in Settings.")

    if not to_phone:
        raise ValueError("Recipient phone number is required")

    try:
        # auth_token might be encrypted
        decrypted_token = decrypt(auth_token)
        token = decrypted_token if decrypted_token else auth_token

        client = Client(account_sid, token)
        sms_message = client.messages.create(body=message, from_=phone_number, to=to_phone)
        return {"success": True, "message_sid": sms_message.sid, "status": sms_message.status}
    except Exception as e:
        return {"success": False, "error": str(e)}
