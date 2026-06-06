def format_phone_e164(phone: str) -> str:
    if not phone:
        return ""

    phone = phone.strip()
    digits = "".join(char for char in phone if char.isdigit())
    if not digits:
        return ""

    phone = digits

    if len(phone) == 10 and phone.isdigit():
        phone = "91" + phone

    if phone.startswith("0") and len(phone) == 11:
        phone = "91" + phone[1:]

    if not phone.startswith("+"):
        phone = "+" + phone

    return phone
