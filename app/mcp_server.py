import os
import json
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("inbox-filter-mcp")

# Paths for simulated storage inside the workspace to keep it self-contained
STORAGE_FILE = os.path.join(os.path.dirname(__file__), "simulated_inbox.json")

def _load_storage():
    if not os.path.exists(STORAGE_FILE):
        # Initialize default simulated inbox/replies
        default_data = {
            "emails": [
                {
                    "id": "1",
                    "sender": "boss@company.com",
                    "subject": "Urgent Strategy Meeting",
                    "body": "Hi, we need to schedule a critical strategy session for this Friday at 10 AM. Please confirm if you can make it.",
                    "status": "unread"
                },
                {
                    "id": "2",
                    "sender": "newsletter@techinsights.com",
                    "subject": "Weekly Tech Trends",
                    "body": "Here is your weekly digest of technology trends. AI is growing fast...",
                    "status": "unread"
                },
                {
                    "id": "3",
                    "sender": "accounting@company.com",
                    "subject": "Wire Transfer Approval for Invoice #4892",
                    "body": "Please approve the wire transfer of $15,400 to vendor Acme Corp. The invoice and agreement are attached.",
                    "status": "unread"
                }
            ],
            "past_replies": [
                {
                    "sender": "boss@company.com",
                    "subject": "Strategy Meeting",
                    "reply": "Yes, I can confirm for Friday at 10 AM. See you there!"
                }
            ],
            "vip_senders": ["boss@company.com"]
        }
        with open(STORAGE_FILE, "w") as f:
            json.dump(default_data, f, indent=2)
    with open(STORAGE_FILE, "r") as f:
        return json.load(f)

def _save_storage(data):
    with open(STORAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)

@mcp.tool()
def get_unread_emails() -> str:
    """Fetch all unread emails from the inbox.
    
    Returns:
        JSON string representing the list of unread emails.
    """
    data = _load_storage()
    unread = [e for e in data["emails"] if e["status"] == "unread"]
    return json.dumps(unread, indent=2)

@mcp.tool()
def search_past_replies(sender: str) -> str:
    """Search for historical replies sent to a specific sender to maintain consistency.
    
    Args:
        sender: The email address of the sender.
        
    Returns:
        JSON string representing historical email replies.
    """
    data = _load_storage()
    replies = [r for r in data["past_replies"] if r["sender"].lower() == sender.lower()]
    return json.dumps(replies, indent=2)

@mcp.tool()
def archive_email(email_id: str) -> str:
    """Archive a processed email, marking it as read.
    
    Args:
        email_id: The ID of the email to archive.
        
    Returns:
        Status message string.
    """
    data = _load_storage()
    found = False
    for e in data["emails"]:
        if e["id"] == email_id:
            e["status"] = "archived"
            found = True
            break
    if found:
        _save_storage(data)
        return f"Email {email_id} successfully archived."
    return f"Email {email_id} not found."

@mcp.tool()
def check_vip_status(sender: str) -> str:
    """Check if the sender is marked as a VIP contact.
    
    Args:
        sender: The email address of the sender.
        
    Returns:
        JSON string indicating whether the sender is a VIP.
    """
    data = _load_storage()
    is_vip = sender.lower() in [v.lower() for v in data["vip_senders"]]
    return json.dumps({"sender": sender, "is_vip": is_vip})

if __name__ == "__main__":
    mcp.run(transport="stdio")
