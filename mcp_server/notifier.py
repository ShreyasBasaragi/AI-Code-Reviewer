import re
import sys
import subprocess
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def parse_review_verdict(review_text: str) -> Dict[str, Any]:
    """
    Parse structured sections from Critique LLM output.
    Extracts whether an issue was found, issue type, and severity.
    """
    verdict = {
        "issue_found": False,
        "issue_type": "None",
        "severity": "None",
        "summary": ""
    }

    if not review_text:
        return verdict

    # Check "Issue Found"
    issue_match = re.search(r"Issue Found:\s*(Yes|No|True|False)", review_text, re.IGNORECASE)
    if issue_match:
        verdict["issue_found"] = issue_match.group(1).lower() in ["yes", "true"]
    else:
        # Fallback check
        lower = review_text.lower()
        if "critical" in lower or "security" in lower or "bug" in lower or "vulnerability" in lower:
            verdict["issue_found"] = True

    # Check "Issue Type"
    type_match = re.search(r"Issue Type:\s*([^\n\r]+)", review_text, re.IGNORECASE)
    if type_match:
        verdict["issue_type"] = type_match.group(1).strip()

    # Check "Severity"
    sev_match = re.search(r"Severity:\s*([^\n\r]+)", review_text, re.IGNORECASE)
    if sev_match:
        verdict["severity"] = sev_match.group(1).strip()

    # Check "Explanation" or summary snippet
    exp_match = re.search(r"Explanation:\s*([^\n\r]+)", review_text, re.IGNORECASE)
    if exp_match:
        verdict["summary"] = exp_match.group(1).strip()[:120]

    return verdict


def send_windows_toast(title: str, message: str) -> bool:
    """
    Send a native Windows Desktop Toast Notification using PowerShell.
    Non-blocking, requires no external dependencies.
    """
    safe_title = title.replace('"', '`"').replace("'", "''")
    safe_message = message.replace('"', '`"').replace("'", "''")

    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
    $textNodes = $xml.GetElementsByTagName("text")
    $textNodes.Item(0).AppendChild($xml.CreateTextNode("{safe_title}")) > $null
    $textNodes.Item(1).AppendChild($xml.CreateTextNode("{safe_message}")) > $null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Critique AI Reviewer")
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $notifier.Show($toast)
    """

    try:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW  # Do not open console popup

        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to display Windows toast notification: {e}")
        return False


def notify_user_if_issue(review_text: str, file_path: Optional[str] = None) -> bool:
    """
    Evaluate review output and send a desktop alert if a code issue is detected.

    Args:
        review_text: Markdown or text output from Critique LLM.
        file_path: Optional file path or identifier being reviewed.

    Returns:
        True if notification was triggered, False otherwise.
    """
    verdict = parse_review_verdict(review_text)

    if verdict["issue_found"]:
        target = f" in {file_path}" if file_path else ""
        title = f"⚠️ Critique: {verdict['issue_type']} Issue Detected"
        msg = f"Severity: {verdict['severity']}{target}\n{verdict['summary'] or 'Check review log for recommended fix.'}"

        logger.info(f"Triggering desktop notification: {title} - {msg}")
        send_windows_toast(title, msg)
        return True
    else:
        logger.info("No issues detected in code. Desktop alert suppressed.")
        return False
