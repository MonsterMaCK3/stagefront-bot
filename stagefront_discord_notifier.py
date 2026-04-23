import imaplib
import email
import os
import time
import re
import json
import requests
import sys
from bs4 import BeautifulSoup

sys.stdout.reconfigure(line_buffering=True)

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", 10))

STATE_FILE = "processed_ids.json"
LABEL_NAME = "Discord Bot"
RECENT_EMAIL_LIMIT = 75


def load_state():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(state), f)


def clean_money(val):
    if not val:
        return None

    val = str(val)
    val = val.replace("$", "")
    val = val.replace(",", "")
    val = val.replace("−", "-")
    val = val.replace("–", "-")
    val = val.replace("—", "-")
    val = val.strip()

    m = re.search(r"-?\d+(?:\.\d+)?", val)
    if not m:
        return None

    try:
        return float(m.group(0))
    except Exception:
        return None


def format_money(val):
    if val is None:
        return "—"
    sign = "-" if val < 0 else ""
    return f"{sign}${abs(val):,.2f}"


def format_signed_bold(val):
    if val is None:
        return "—"
    if val < 0:
        return f"🔴 **-${abs(val):,.2f}**"
    return f"🟢 **${val:,.2f}**"


def format_profit(val):
    if val is None:
        return "—"
    if val > 0:
        return f"🟢 **+${val:,.2f}**"
    if val < 0:
        return f"🔴 **-${abs(val):,.2f}**"
    return f"🟡 **$0.00**"


def get_profit_meta(profit):
    if profit is None:
        return {
            "color": 0x95A5A6,
            "title_prefix": "🎟️ SALE",
            "status": "⚪ Unknown",
            "tag": "",
        }

    if profit > 0:
        if profit >= 100:
            return {
                "color": 0x2ECC71,
                "title_prefix": "💰 PROFIT SALE",
                "status": "🟢 Profit",
                "tag": "🚀 **BIG WIN**",
            }
        return {
            "color": 0x2ECC71,
            "title_prefix": "💰 PROFIT SALE",
            "status": "🟢 Profit",
            "tag": "",
        }

    if profit < 0:
        return {
            "color": 0xE74C3C,
            "title_prefix": "📉 LOSS SALE",
            "status": "🔴 Loss",
            "tag": "",
        }

    return {
        "color": 0xF1C40F,
        "title_prefix": "⚖️ BREAKEVEN SALE",
        "status": "🟡 Breakeven",
        "tag": "",
    }


def get_email_body(msg):
    html_body = ""
    text_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()

            if "attachment" in disp:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            decoded = payload.decode(errors="ignore")

            if ctype == "text/html":
                html_body += decoded
            elif ctype == "text/plain":
                text_body += decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(errors="ignore")
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                text_body = decoded

    if "Stage Front Consignment" in html_body or "Invoice Total" in html_body:
        return html_body

    return html_body or text_body or ""


def is_valid_sale_email(body, subject=""):
    text = (subject + "\n" + body).lower()

    blocked = [
        "purchase order",
        "po created",
        "forwarding approval",
        "approval request",
    ]
    if any(x in text for x in blocked):
        return False

    has_stagefront = "stage front consignment" in text
    has_invoice = "invoice #" in text or "invoice" in text
    has_total = "invoice total" in text

    return has_stagefront and has_invoice and has_total


def parse_email(body):
    body = re.split(r"-{5,}\s*Forwarded message\s*-{5,}", body, flags=re.IGNORECASE)[-1]

    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)

    data = {}

    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    def clean_label(label):
        return re.sub(r"\s+", " ", label.strip().lower().replace(":", ""))

    def extract_table_values():
        values = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                label = clean_label(cells[0].get_text(" ", strip=True))
                value = cells[1].get_text(" ", strip=True)
                if label and value:
                    values[label] = value
        return values

    table_values = extract_table_values()

    def table_or_regex(table_key, regex_pattern):
        return table_values.get(table_key) or find(regex_pattern)

    data["invoice"] = table_or_regex("invoice #", r"Invoice\s*#\s*(\d+)")
    data["event"] = table_or_regex("event", r"Event:\s*(.*)")
    data["datetime"] = (
        table_values.get("date/time")
        or table_values.get("date / time")
        or find(r"Date/Time:\s*(.*)")
    )
    data["venue"] = table_or_regex("venue", r"Venue:\s*(.*)")

    sec = re.search(
        r"Section:\s*(.*?)\s*\|\s*Row:\s*(.*?)\s*\|\s*Qty:\s*(\d+)\s*\|\s*Seats:\s*(.*)",
        text,
        re.IGNORECASE,
    )
    if sec:
        data["section"] = sec.group(1).strip()
        data["row"] = sec.group(2).strip()
        data["qty"] = sec.group(3).strip()
        data["seats"] = sec.group(4).strip()
    else:
        data["section"] = table_or_regex("section", r"Section:\s*(.*)")
        data["row"] = table_or_regex("row", r"Row:\s*(.*)")
        data["qty"] = table_or_regex("qty", r"Qty:\s*(\d+)")
        data["seats"] = table_or_regex("seats", r"Seats:\s*(.*)")

    data["price"] = clean_money(table_or_regex("price per", r"Price Per:\s*([^\n]+)"))
    data["total"] = clean_money(table_or_regex("invoice total", r"Invoice Total:\s*([^\n]+)"))
    data["net"] = clean_money(table_or_regex("net amount", r"Net Amount:\s*([^\n]+)"))
    data["total_cost"] = clean_money(table_or_regex("total cost", r"Total Cost:\s*([^\n]+)"))

    commission_raw = table_or_regex("commission", r"Commission:\s*([^\n]+)")
    if commission_raw:
        commission_raw = commission_raw.replace("−", "-").replace("–", "-").replace("—", "-")
    data["commission"] = clean_money(commission_raw)

    roi_raw = table_or_regex("roi $", r"ROI \$:\s*([^\n]+)")
    data["roi_dollar"] = clean_money(roi_raw)
    data["roi_percent"] = table_or_regex("roi %", r"ROI %:\s*([\d\.]+)%")
    data["remaining"] = (
        table_values.get("tickets remaining")
        or table_values.get("tickets remaining for event")
        or find(r"Tickets Remaining.*:\s*(\d+)")
    )

    data["account"] = None
    data["email"] = None

    acct_line = re.search(r"([A-Z0-9\-\/]+)\s*\(([^)@\s]+@[^)\s]+)\)", text, re.IGNORECASE)
    if acct_line:
        data["account"] = acct_line.group(1).strip()
        data["email"] = acct_line.group(2).strip()
    else:
        email_match = re.search(r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", text, re.IGNORECASE)
        if email_match:
            data["email"] = email_match.group(1).strip()

    if not data["account"]:
        data["account"] = (
            table_values.get("account")
            or table_values.get("account ref")
            or table_values.get("account number")
        )

    transfer = re.search(
        r"(Mobile XFER|Mobile Transfer|PDF|AXS|TM Transfer)",
        text,
        re.IGNORECASE,
    )
    if transfer:
        data["transfer"] = transfer.group(1)
    else:
        data["transfer"] = table_values.get("transfer type") or table_values.get("transfer")

    platform = re.search(
        r"(TickPick|Ticketmaster|AXS|SeatGeek|StubHub|Vivid Seats)",
        text,
        re.IGNORECASE,
    )
    if platform:
        data["platform"] = platform.group(1)
    else:
        data["platform"] = table_values.get("platform")

    return data


def send_to_discord(data):
    profit = None
    if data.get("net") is not None and data.get("total_cost") is not None:
        profit = data["net"] - data["total_cost"]

    meta = get_profit_meta(profit)

    roi_value = "—"
    if data.get("roi_dollar") is not None or data.get("roi_percent") is not None:
        roi_value = f"{format_money(data.get('roi_dollar'))} ({data.get('roi_percent') or '—'}%)"

    description_parts = [f"**Status:** {meta['status']}"]
    if meta["tag"]:
        description_parts.append(meta["tag"])

    embed = {
        "title": f"{meta['title_prefix']}: {data.get('event', 'Unknown Event')}",
        "description": "\n".join(description_parts),
        "color": meta["color"],
        "fields": [
            {"name": "Invoice", "value": str(data.get("invoice", "—")), "inline": True},
            {"name": "Venue", "value": str(data.get("venue", "—")), "inline": True},
            {"name": "Date", "value": str(data.get("datetime", "—")), "inline": True},
            {
                "name": "Section / Row",
                "value": f"{data.get('section', '—')} / {data.get('row', '—')}",
                "inline": True,
            },
            {
                "name": "Qty / Seats",
                "value": f"{data.get('qty', '—')} / {data.get('seats', '—')}",
                "inline": True,
            },
            {"name": "Platform", "value": str(data.get("platform", "—")), "inline": True},
            {"name": "Price Per", "value": format_money(data.get("price")), "inline": True},
            {"name": "Total Cost", "value": format_money(data.get("total_cost")), "inline": True},
            {"name": "Invoice Total", "value": format_money(data.get("total")), "inline": True},
            {"name": "Commission", "value": format_signed_bold(data.get("commission")), "inline": True},
            {"name": "Net Amount", "value": format_signed_bold(data.get("net")), "inline": True},
            {"name": "Profit", "value": format_profit(profit), "inline": True},
            {"name": "ROI", "value": f"📊 **{roi_value}**" if roi_value != "—" else "—", "inline": True},
            {"name": "Remaining Tickets", "value": str(data.get("remaining", "—")), "inline": True},
            {"name": "Transfer Type", "value": str(data.get("transfer", "—")), "inline": True},
            {"name": "Account", "value": str(data.get("account", "—")), "inline": False},
            {"name": "Buyer Email", "value": str(data.get("email", "—")), "inline": False},
        ],
        "footer": {"text": "StageFront → Discord Pipeline"},
    }

    r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=20)
    print("Discord status:", r.status_code)
    if r.text:
        print("Discord response:", r.text)


def main():
    print("BOT STARTING...")
    print("GMAIL_ADDRESS set:", bool(GMAIL_ADDRESS))
    print("GMAIL_APP_PASSWORD set:", bool(GMAIL_APP_PASSWORD))
    print("DISCORD_WEBHOOK_URL set:", bool(DISCORD_WEBHOOK_URL))
    print("POLL_SECONDS:", POLL_SECONDS)

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not DISCORD_WEBHOOK_URL:
        print("Missing one or more environment variables.")
        return

    processed = load_state()
    print("Loaded processed IDs:", len(processed))

    while True:
        sleep_time = POLL_SECONDS

        try:
            print("Checking inbox...")
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

            status, mailbox_info = mail.select("inbox")
            print("Select status:", status, mailbox_info)

            status, data = mail.search(None, f'(X-GM-LABELS "{LABEL_NAME}")')
            print("Search status:", status)
            print("Raw search result:", data)

            ids = data[0].split() if data and data[0] else []
            print("Labeled emails found:", len(ids))

            ids = ids[-RECENT_EMAIL_LIMIT:]
            print("Checking most recent labeled emails:", len(ids))

            for msg_id in ids:
                decoded_id = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                print("Processing message ID:", decoded_id)

                if decoded_id in processed:
                    print("Already processed, skipping:", decoded_id)
                    continue

                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = msg.get("Subject", "")
                from_addr = msg.get("From", "")
                print("Subject:", subject)
                print("From:", from_addr)

                body = get_email_body(msg)

                if not is_valid_sale_email(body, subject):
                    print("Skipped email - failed validation")
                    print("Preview:", body[:500])
                    processed.add(decoded_id)
                    save_state(processed)
                    continue

                parsed = parse_email(body)
                print("Parsed data:", parsed)

                send_to_discord(parsed)

                processed.add(decoded_id)
                save_state(processed)
                print("Saved processed ID:", decoded_id)

            mail.logout()

        except Exception as e:
            print("ERROR:", repr(e))
            sleep_time = 20

        print(f"Sleeping {sleep_time} seconds...")
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()