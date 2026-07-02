import os
import re
import json
import sys
import logging
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.workflow import Workflow, START
from google.adk.tools import AgentTool
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.genai import types

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from .config import config

# Set up logging for audit log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("inbox_filter_audit")

# --- Define MCP Toolset ---
mcp_server_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "mcp_server.py"))

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_server_path],
        )
    )
)

# --- Define specialized sub-agents ---

categorizer = LlmAgent(
    name="categorizer",
    model=config.model,
    tools=[mcp_toolset],
    instruction="""You are an expert email classifier. Analyze the email subject and body.
You can use the 'check_vip_status' tool to check if the sender is a VIP contact (which can help classify the email).

Classify the email into exactly one of the following categories:
- Urgent: Requires immediate response/action (e.g. server outage, client crisis).
- Routine: Standard business communication, follow-ups, regular questions.
- Social/Spam: Newsletters, promotional items, casual greetings.
- Sensitive: Financial transactions, passwords, confidential contracts, legal concerns.

Return the category name, followed by a one-sentence summary of the email. Do not output anything else.
""",
    description="Categorizes emails and provides a short summary."
)

responder = LlmAgent(
    name="responder",
    model=config.model,
    tools=[mcp_toolset],
    instruction="""You are an expert email responder. Based on the email details and its category,
draft a professional, polite, and helpful response.
You can use the 'search_past_replies' tool to find historical replies for the sender to ensure consistency.

If the email contains a request you cannot fulfill, politely explain why and offer alternative help.

Return only the drafted reply text. Do not include any greeting or signature headers except the email body.
""",
    description="Drafts responses to emails."
)

# --- Define the Orchestrator ---

inbox_orchestrator = LlmAgent(
    name="inbox_orchestrator",
    model=config.model,
    tools=[AgentTool(categorizer), AgentTool(responder)],
    instruction="""You are the Inbox Orchestrator. You manage incoming email routing and auto-drafting.
When a new email is received, perform these tasks:
1. Delegate to the 'categorizer' sub-agent to get the email category and summary.
2. Delegate to the 'responder' sub-agent to get a drafted reply.
3. Consolidate these findings and return a JSON object containing the fields: 'category', 'summary', and 'draft'.

You must return ONLY a JSON code block in this format:
```json
{
  "category": "<Urgent | Routine | Social/Spam | Sensitive>",
  "summary": "<one-sentence summary>",
  "draft": "<drafted reply text>"
}
```
""",
    description="Orchestrates email analysis and reply drafting."
)

# --- Workflow Node Functions ---

# Phase 4 Security Checkpoint
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """Security node to check for PII, prompt injections, and domain restrictions."""
    # Convert input to string
    email_text = ""
    if hasattr(node_input, 'parts') and node_input.parts:
        email_text = "".join(part.text for part in node_input.parts if part.text)
    elif isinstance(node_input, str):
        email_text = node_input
    else:
        email_text = str(node_input)

    # 1. PII Scrubbing (Regex)
    scrubbed_text = email_text
    phone_pattern = r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    
    scrubbed_text = re.sub(phone_pattern, "[PHONE_REDACTED]", scrubbed_text)
    scrubbed_text = re.sub(ssn_pattern, "[SSN_REDACTED]", scrubbed_text)

    # 2. Prompt Injection Detection (Keyword detection)
    injection_keywords = ["ignore previous instructions", "system prompt", "dan mode", "you must now act as"]
    has_injection = any(kw in email_text.lower() for kw in injection_keywords)

    # 3. Domain-specific rule (Size limit)
    size_exceeded = len(email_text) > 5000

    # Structured Audit Log
    audit_data = {
        "timestamp": ctx.session.state.get("temp:timestamp", ""),
        "input_length": len(email_text),
        "has_pii_scrubbed": scrubbed_text != email_text,
        "has_injection": has_injection,
        "size_exceeded": size_exceeded
    }

    if has_injection or size_exceeded:
        audit_data["severity"] = "CRITICAL"
        audit_data["action"] = "BLOCKED"
        logger.warning(json.dumps(audit_data))
        
        reason = "Prompt injection detected" if has_injection else "Email body size limit exceeded"
        return Event(output=reason, route="security_event")
    
    audit_data["severity"] = "INFO"
    audit_data["action"] = "ALLOWED"
    logger.info(json.dumps(audit_data))

    # Save scrubbed input to state
    return Event(output=scrubbed_text, route="clean", state={"cleaned_input": scrubbed_text})


def security_event(node_input: str) -> Event:
    """Security alert handler when an injection or size violation is caught."""
    msg = f"⚠️ SECURITY EXCEPTION: The email could not be processed due to: {node_input}."
    content = types.Content(role="model", parts=[types.Part.from_text(text=msg)])
    return Event(output=msg, content=content)


def check_approval_requirement(ctx: Context, node_input: Any) -> Event:
    """Parses the orchestrator's output and determines if human approval is needed."""
    text_content = ""
    if hasattr(node_input, 'parts') and node_input.parts:
        text_content = "".join(part.text for part in node_input.parts if part.text)
    elif isinstance(node_input, str):
        text_content = node_input
    
    # Log the raw text content for debugging
    logger.info(f"Raw orchestrator output: {text_content}")

    # Initialize defaults
    category = "Routine"
    summary = ""
    draft = ""

    # Parse JSON from markdown code block
    json_match = re.search(r"```json\s*(.*?)\s*```", text_content, re.DOTALL | re.IGNORECASE)
    if json_match:
        try:
            data = json.loads(json_match.group(1).strip())
            category = data.get("category", "Routine")
            summary = data.get("summary", "")
            draft = data.get("draft", "")
        except Exception as e:
            logger.error(f"Failed to parse JSON block: {e}")

    # Fallback to loose regex parsing if JSON parsing fails or isn't found
    if not summary or not draft:
        category_match = re.search(r"category:\s*(\w+)", text_content, re.IGNORECASE)
        summary_match = re.search(r"summary:\s*(.*?)(?=\n\s*(?:draft|category|summary):|$)", text_content, re.DOTALL | re.IGNORECASE)
        draft_match = re.search(r"draft:\s*(.*)", text_content, re.DOTALL | re.IGNORECASE)

        if category_match:
            category = category_match.group(1).strip()
        if summary_match:
            summary = summary_match.group(1).strip()
        if draft_match:
            draft = draft_match.group(1).strip()

    # Clean up formatting
    category = category.replace("*", "").strip()
    summary = summary.replace("*", "").strip()

    # Write results to state
    state_updates = {
        "category": category,
        "draft": draft,
        "summary": summary
    }

    # Route decision
    if category.lower() == "sensitive":
        return Event(output=draft, route="needs_approval", state=state_updates)
    else:
        return Event(output=draft, route="auto_approve", state=state_updates)


async def human_approval(ctx: Context, node_input: str) -> AsyncGenerator[Event, None]:
    """Human-in-the-loop approval step for sensitive emails."""
    # If the user has not yet replied to this approval step
    if not ctx.resume_inputs or "approval_reply" not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="approval_reply",
            message=f"✋ SENSITIVE EMAIL DETECTED.\nSummary: {ctx.state.get('summary')}\nProposed Draft Reply:\n\n\"{node_input}\"\n\nPlease reply with 'approve' to send, or write your edited draft response."
        )
        return

    # User has responded! Read the response
    user_reply = ctx.resume_inputs["approval_reply"]
    if user_reply.strip().lower() == "approve":
        final_draft = node_input
        status = "approved"
    else:
        # User provided an edited draft
        final_draft = user_reply.strip()
        status = f"edited_and_approved"

    yield Event(
        output=final_draft,
        state={"final_draft": final_draft, "approval_status": status}
    )


def send_reply(ctx: Context, node_input: str) -> Event:
    """Final node that 'sends' the reply and prints the execution summary."""
    category = ctx.state.get("category", "Unknown")
    summary = ctx.state.get("summary", "No summary")
    status = ctx.state.get("approval_status", "auto_approved")

    result_text = f"""✉️ Email Processing Completed!
- Category: {category}
- Summary: {summary}
- Dispatch Status: {status.upper()}
- Final Sent Reply:
----------------------------------------
{node_input}
----------------------------------------
"""
    content = types.Content(role="model", parts=[types.Part.from_text(text=result_text)])
    return Event(output=result_text, content=content)


# --- Create Workflow Agent ---

root_agent = Workflow(
    name="inbox_filter",
    description="Inbox Filter Workflow Agent",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {
            "security_event": security_event,
            "clean": inbox_orchestrator
        }),
        (inbox_orchestrator, check_approval_requirement),
        (check_approval_requirement, {
            "needs_approval": human_approval,
            "auto_approve": send_reply
        }),
        (human_approval, send_reply),  # Unconditional transition after approval
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)
)
