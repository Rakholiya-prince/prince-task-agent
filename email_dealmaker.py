import os
import json
import time
import imaplib
import email
from email.header import decode_header
import urllib.request
from datetime import datetime, timezone

DB_DIR = os.path.dirname(os.path.abspath(__file__))
REPLIES_PATH = os.path.join(DB_DIR, "client_replies.json")
JOBS_PATH = os.path.join(DB_DIR, "jobs_database.json")

def get_env_credentials():
    """Reads credentials from environment variables or .env file."""
    user = os.getenv("OUTREACH_EMAIL")
    pwd = os.getenv("OUTREACH_EMAIL_PASS")
    if not user or not pwd:
        env_file = os.path.join(DB_DIR, ".env")
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        if k.strip() == "OUTREACH_EMAIL":
                            user = v.strip()
                        elif k.strip() == "OUTREACH_EMAIL_PASS":
                            pwd = v.strip()
    return user, pwd

def load_replies():
    if os.path.exists(REPLIES_PATH):
        try:
            with open(REPLIES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error loading client replies: {e}")
    return []

def save_replies(replies):
    try:
        with open(REPLIES_PATH, "w", encoding="utf-8") as f:
            json.dump(replies, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Error saving client replies: {e}")

def decode_mime_words(s):
    if not s:
        return ""
    decoded_fragments = decode_header(s)
    res = []
    for frag, enc in decoded_fragments:
        if isinstance(frag, bytes):
            res.append(frag.decode(enc or "utf-8", errors="ignore"))
        else:
            res.append(str(frag))
    return "".join(res)

def draft_ai_negotiation_reply(sender, subject, body, gemini_api_key):
    """Generates an intelligent context-aware reply to seal the deal."""
    if not gemini_api_key:
        return "Hello,\n\nThank you for reaching out! I would love to discuss this deliverable and look forward to coordinating with your team.\n\nBest regards,\nPrince"
        
    prompt = f"""
A potential client just replied to Prince's application.
Sender: {sender}
Subject: {subject}
Message:
{body[:2000]}

Prince is a reliable asynchronous developer who delivers tickets, QA testing, and automation smoothly.
Draft a professional, clear, and confident response addressing their question, confirming availability, and proposing the next easy step (e.g. deliverable scope, kickoff ticket, or brief asynchronous chat).
Keep it concise, friendly, and professional.
"""
    try:
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}]
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-goog-api-key": gemini_api_key}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[!] AI reply drafting error: {e}")
        return "Thank you for getting back to me. I am ready to begin immediately and can deliver the first milestone asynchronously. Looking forward to your instructions."

def check_client_inbox_and_alert(telegram_sender_fn, chat_id, gemini_api_key):
    """
    Connects to IMAP 993, checks for UNSEEN incoming emails.
    If an actual client response is found:
    1. Saves reply to client_replies.json.
    2. Drafts an AI response.
    3. Sends HIGH-PRIORITY Telegram notification to Prince.
    """
    user, pwd = get_env_credentials()
    if not user or not pwd:
        return
        
    replies = load_replies()
    seen_ids = {r.get("msg_id") for r in replies}
    
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(user, pwd)
        mail.select("inbox")
        
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages[0]:
            mail.logout()
            return
            
        msg_nums = messages[0].split()
        for num in msg_nums:
            res, data = mail.fetch(num, "(RFC822)")
            if res != "OK":
                continue
                
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            sender = decode_mime_words(msg.get("From"))
            subject = decode_mime_words(msg.get("Subject"))
            msg_id = msg.get("Message-ID") or f"{sender}_{subject}_{time.time()}"
            
            # Skip automated notifications from Google/system
            lower_sender = sender.lower()
            if any(s in lower_sender for s in ["google.com", "mailer-daemon", "no-reply", "noreply", "notifications"]):
                continue
                
            if msg_id in seen_ids:
                continue
                
            # Extract plain text body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if content_type == "text/plain" and "attachment" not in content_disposition:
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                
            ai_reply = draft_ai_negotiation_reply(sender, subject, body, gemini_api_key)
            
            reply_record = {
                "msg_id": msg_id,
                "sender": sender,
                "subject": subject,
                "body": body[:2000],
                "received_at": datetime.now(timezone.utc).isoformat(),
                "suggested_reply": ai_reply
            }
            replies.insert(0, reply_record)
            seen_ids.add(msg_id)
            save_replies(replies)
            
            # Send ONLY THIS High Priority Telegram Notification to Prince
            alert_msg = (
                f"🎉 *CLIENT RESPONSE RECEIVED FOR BOSS PRINCE!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *From:* `{sender}`\n"
                f"📌 *Subject:* *{subject}*\n\n"
                f"💬 *Client Message:*\n_{body[:400]}..._\n\n"
                f"🤖 *AI Suggested Response:*\n_{ai_reply[:500]}..._\n\n"
                f"🔗 [View Live Mission Control](https://prince-task-agent.onrender.com/jobs)"
            )
            try:
                telegram_sender_fn(chat_id, alert_msg)
            except Exception as e:
                print(f"[!] Telegram alert error for reply: {e}")
                
        mail.logout()
    except Exception as e:
        print(f"[!] IMAP check error: {e}")
