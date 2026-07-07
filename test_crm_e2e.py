"""
End-to-end test sender for the Cedar Ridge Inbox Agent.

Sends a series of real emails to the agent inbox simulating each scenario.
After running this, start the agent and watch replies arrive in your inbox.

Usage:
    python test_crm_e2e.py             # all scenarios
    python test_crm_e2e.py relationship commands import
"""
from __future__ import annotations
import os
import smtplib
from dotenv import load_dotenv
load_dotenv()
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import make_msgid

SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = os.environ["TEST_SMTP_USER"]
SMTP_PASSWORD = os.environ["TEST_SMTP_PASSWORD"]
AGENT_INBOX   = os.environ["CRM_IMAP_USER"]
OWNER_EMAIL   = os.environ["OWNER_EMAIL"]


def _send(subject: str, body: str, from_addr: str = None, attachments: list[dict] = None):
    from_addr = OWNER_EMAIL  # always send from your account so it shows in your sent folder
    msg = MIMEMultipart("mixed")
    msg["Subject"]    = subject
    msg["From"]       = from_addr
    msg["To"]         = AGENT_INBOX
    msg["Message-ID"] = make_msgid()

    msg.attach(MIMEText(body, "plain"))

    for att in (attachments or []):
        part = MIMEBase("application", "octet-stream")
        part.set_payload(att["content"])
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{att["filename"]}"')
        msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(from_addr, [AGENT_INBOX], msg.as_string())

    print(f"  sent: {subject}")
    time.sleep(1)   # avoid rate limits


# ---------------------------------------------------------------------------
# Scenario groups
# ---------------------------------------------------------------------------

def send_relationship_emails():
    """
    Simulate BCCs and forwards of real relationship conversations.
    Agent should parse each one, create a contact record, and if importance >= 4
    send a high-priority alert back to your inbox.
    """
    print("\n[1] Relationship emails")

    # LP prospect — high importance (should trigger alert)
    _send(
        subject="Re: Nebari Fund III — follow-up",
        body="""
Hi,

Following up from our call last week. We're genuinely interested in Nebari III
and are looking at a $5M allocation from our endowment sleeve.

Can you send over the DDQ and audited financials when you get a chance?
Happy to schedule a follow-up with our investment committee in two weeks.

Best,
Clara Meade
Senior Investment Officer
Horizon Endowment Partners
clara.meade@horizonep.com
+1 212 555 0101
""",
        from_addr=f"Clara Meade <clara.meade@horizonep.com>",
    )

    # Founder intro — warm but not immediate
    _send(
        subject="Introduction: Marcus Webb / Cedar Ridge",
        body="""
Hi,

Marcus — meet the team at Cedar Ridge Capital. They run a long/short equity
strategy focused on emerging tech and have done well through the recent vol.

Cedar Ridge — Marcus is the founder of Wren Ventures, a $200M seed fund with
strong LP relationships in family offices. Worth a conversation.

Best,
Daniel Park
""",
        from_addr=f"Daniel Park <daniel.park@intronetwork.com>",
    )

    # LP who just passed
    _send(
        subject="Re: Cedar Ridge — passing for now",
        body="""
Hi,

Thanks for the time and materials. After internal review we've decided to pass
on Nebari III — the strategy doesn't fit our current allocation framework.

We'd love to stay in touch for future vehicles.

Regards,
Tom Fischer
Acorn Family Office
tom.fischer@acornfo.com
""",
        from_addr=f"Tom Fischer <tom.fischer@acornfo.com>",
    )

    # Call recap from the user themselves (outbound)
    _send(
        subject="Call recap — Eagle Capital, Sarah Ng",
        body="""
Quick note to file: spoke with Sarah Ng at Eagle Capital today. She's interested
in Nebari III, asked for the full DDQ and a reference from our prime broker.
She wants to move to diligence after receiving materials.

Deal size: looking at $3M.

Next step: send DDQ + prime broker reference by end of week.
""",
    )


def send_command_emails():
    """
    Simulate direct commands to the agent — as if you emailed them yourself.
    Agent should reply to each with the relevant output.
    """
    print("\n[2] Command emails")

    commands = [
        ("pipeline",                    "pipeline"),
        ("radar",                       "radar"),
        ("whois clara.meade@horizonep.com", "whois clara.meade@horizonep.com"),
        ("score clara.meade@horizonep.com", "score clara.meade@horizonep.com"),
        ("brief clara.meade@horizonep.com", "brief clara.meade@horizonep.com"),
        ("draft clara.meade@horizonep.com: write a warm follow-up chasing the DDQ she requested",
         "draft clara.meade@horizonep.com: write a warm follow-up chasing the DDQ she requested"),
        ("who is in diligence",         "who is in diligence"),
        ("high priority clara.meade@horizonep.com", "high priority clara.meade@horizonep.com"),
        ("help",                        "help"),
    ]

    for subject, body in commands:
        _send(subject=subject, body=body)

    # Free-form Q&A — should fall through to Ask Layer
    _send(
        subject="question",
        body="Who's the warmest LP right now for Nebari?",
    )


def send_import():
    """
    Simulate a bulk contact spreadsheet attachment.
    Agent should import rows and reply with a count.
    """
    print("\n[3] Bulk import")

    csv_content = b"""Name,Email,Phone,Firm,Role,Stage,Deal Amount,Notes
James Okafor,james.okafor@pinecap.com,+1 646 555 0202,Pine Capital,CIO,Engaged,$2M,Met at conference
Rachel Liu,rachel.liu@aldermanfamily.com,,Alderman Family Office,Principal,Contacted,,Intro from Marcus
David Stern,david.stern@sternfoundation.org,+1 917 555 0303,Stern Foundation,Investments Director,Materials sent,$1.5M,Sent deck last month
"""

    _send(
        subject="Contact list — Q2 pipeline",
        body="Attaching the Q2 contact list for the agent to import.",
        attachments=[{"filename": "q2_contacts.csv", "content": csv_content}],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

SCENARIOS = {
    "relationship": send_relationship_emails,
    "commands":     send_command_emails,
    "import":       send_import,
}

if __name__ == "__main__":
    args = sys.argv[1:] or list(SCENARIOS.keys())
    unknown = [a for a in args if a not in SCENARIOS]
    if unknown:
        print(f"Unknown scenarios: {unknown}. Valid: {list(SCENARIOS.keys())}")
        sys.exit(1)

    print(f"Sending to agent inbox: {AGENT_INBOX}")
    print(f"Replies will arrive at: {OWNER_EMAIL}")
    for name in args:
        SCENARIOS[name]()

    print(f"\nDone. Now run the agent to process them:")
    print(f"  python -c \"import asyncio, crm_agent; asyncio.run(crm_agent.process_once())\"")
