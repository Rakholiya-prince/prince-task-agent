import os
import sys
import time
import json
import socket
import traceback
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib3.util.connection as urllib_connection
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

# Force IPv4 for all outbound connections (fixes Render -> Google/Telegram latency)
urllib_connection.allowed_gai_family = lambda: socket.AF_INET

# Environment configuration
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
PORT = int(os.getenv("PORT", "10000"))

START_TIME = time.time()

# ----------------- AGENT TOOLS ----------------- #
def tool_web_search(query: str) -> str:
    """Searches the live web using DuckDuckGo."""
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append(f"Title: {r.get('title')}\nURL: {r.get('href')}\nSnippet: {r.get('body')}\n")
        if not results:
            return "No search results found."
        return "\n---\n".join(results)
    except Exception as e:
        return f"Error during web search: {str(e)}"

def tool_fetch_webpage(url: str) -> str:
    """Fetches text content from a given URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Connection": "close"}
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = ' '.join(soup.stripped_strings)
        return text[:4000]
    except Exception as e:
        return f"Error fetching webpage: {str(e)}"

def tool_run_python(code: str) -> str:
    """Executes Python code in an isolated subprocess."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=25
        )
        output = proc.stdout
        if proc.stderr:
            output += "\nSTDERR:\n" + proc.stderr
        return output.strip() if output.strip() else "(Code ran successfully with no output)"
    except subprocess.TimeoutExpired:
        return "Error: Code execution timed out after 25 seconds."
    except Exception as e:
        return f"Execution error: {str(e)}"

TOOLS_DECLARATIONS = [
    {
        "name": "web_search",
        "description": "Search the live web for real-time information, news, and answers.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "fetch_webpage",
        "description": "Fetch text from a specific webpage URL.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url": {"type": "STRING", "description": "The URL to fetch"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "run_python",
        "description": "Run Python code to compute, analyze data, scrape, or solve logic.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "code": {"type": "STRING", "description": "The Python code to execute"}
            },
            "required": ["code"]
        }
    }
]

# ----------------- TELEGRAM MESSAGING ----------------- #
def telegram_send_message(chat_id: int, text: str, parse_mode: str = "Markdown") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, headers={"Connection": "close"}, timeout=12)
        if not r.ok:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, headers={"Connection": "close"}, timeout=12)
    except Exception as e:
        print(f"[!] Telegram sendMessage error: {e}")

# ----------------- GEMINI AGENT EXECUTION LOOP ----------------- #
def ask_gemini_agent(user_prompt: str, chat_id: int) -> str:
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent"
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
        "Connection": "close"
    }
    
    system_instruction = (
        "You are Prince's 24/7 Autonomous AI Agent. "
        "You are hosted permanently on cloud infrastructure with tools: web_search, fetch_webpage, run_python. "
        "Use tools autonomously to solve user requests, perform research, run code, or summarize information. "
        "Always be helpful, precise, and address the user as Sir."
    )

    contents = [{"role": "user", "parts": [{"text": user_prompt}]}]
    
    for step in range(4):
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": contents,
            "tools": [{"function_declarations": TOOLS_DECLARATIONS}]
        }
        
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=25)
            data = resp.json()
            
            if "error" in data:
                return f"⚠️ Gemini API Error: {data['error'].get('message')}"
            
            candidate = data.get("candidates", [{}])[0]
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            
            if not parts:
                return "Task finished without output."
            
            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]
            
            if not function_calls:
                text_parts = [p["text"] for p in parts if "text" in p]
                return "\n".join(text_parts)
            
            contents.append(content)
            
            tool_response_parts = []
            for fc in function_calls:
                fn_name = fc["name"]
                fn_args = fc.get("args", {})
                
                telegram_send_message(chat_id, f"⚙️ *Executing tool:* `{fn_name}`...")
                
                if fn_name == "web_search":
                    result = tool_web_search(fn_args.get("query", ""))
                elif fn_name == "fetch_webpage":
                    result = tool_fetch_webpage(fn_args.get("url", ""))
                elif fn_name == "run_python":
                    result = tool_run_python(fn_args.get("code", ""))
                else:
                    result = f"Unknown tool: {fn_name}"
                
                tool_response_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"result": result}
                    }
                })
            
            contents.append({
                "role": "user",
                "parts": tool_response_parts
            })
            
        except requests.exceptions.Timeout:
            # Quick lightweight fallback
            try:
                simple_payload = {"contents": [{"role": "user", "parts": [{"text": user_prompt}]}]}
                resp = requests.post(endpoint, json=simple_payload, headers=headers, timeout=25)
                data = resp.json()
                text_parts = [p.get("text", "") for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", []) if "text" in p]
                if text_parts:
                    return "".join(text_parts)
            except Exception:
                pass
            return "⚠️ Request timed out. Please try asking again."
        except Exception as e:
            return f"⚠️ Agent error: {str(e)}"
            
    return "Task reached maximum tool iterations."

# ----------------- MESSAGE PROCESSOR ----------------- #
def process_message(message: dict):
    try:
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")
        user_first_name = message.get("from", {}).get("first_name", "Sir")
        
        if not text or not chat_id:
            return
            
        print(f"[+] Received message from {user_first_name} ({chat_id}): {text}")
        
        if text == "/start":
            greeting = (
                f"👋 Hello {user_first_name} Sir!\n\n"
                "I am your **24/7 Autonomous AI Agent**, hosted on the cloud!\n\n"
                "⚡ **What I can do for you:**\n"
                "• 🌐 **Live Web Search & Scraping**: Give me any topic to research.\n"
                "• 🐍 **Python Code Execution**: I can calculate, write, and run Python code.\n"
                "• 📊 **Analysis & Data Tasks**: I process tasks autonomously.\n"
                "• ⏰ **24/7 Uptime**: Send me any job, close your phone, and I'll work on it!\n\n"
                "Commands:\n"
                "/status - Check server uptime and health\n"
                "Or simply send me your task instructions!"
            )
            telegram_send_message(chat_id, greeting)
            return
            
        elif text == "/status":
            uptime_sec = int(time.time() - START_TIME)
            hours = uptime_sec // 3600
            minutes = (uptime_sec % 3600) // 60
            seconds = uptime_sec % 60
            status_msg = (
                f"🟢 **Agent Status:** Online & Active (Webhook Mode)\n"
                f"⏳ **Uptime:** {hours}h {minutes}m {seconds}s\n"
                f"☁️ **Host:** 24/7 Render Cloud Server\n"
                f"🧠 **Model:** Gemini Flash (Agentic Loop Enabled)\n"
                f"🛠️ **Tools:** Web Search, Web Scraper, Python Runner"
            )
            telegram_send_message(chat_id, status_msg)
            return
            
        elif text.lower().strip() in ["hi", "hello", "hey", "kem cho", "namaste"]:
            telegram_send_message(chat_id, f"👋 Hello {user_first_name} Sir!\n\nI am online 24/7 on the cloud and ready for your work. What task would you like me to do?")
            return
            
        # Normal task execution
        telegram_send_message(chat_id, "⏳ *Working on your task, Sir... Please wait.*")
        agent_reply = ask_gemini_agent(text, chat_id)
        final_msg = f"{agent_reply}\n\n━━━━━━━━━━━━━━━━━━━━\n✅ *Task completed, Sir! You can check now.*"
        telegram_send_message(chat_id, final_msg)
        
    except Exception as e:
        print(f"[!] Error in process_message: {e}")

# ----------------- HTTP SERVER & WEBHOOK HANDLER ----------------- #
class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        uptime_sec = int(time.time() - START_TIME)
        hours = uptime_sec // 3600
        minutes = (uptime_sec % 3600) // 60
        seconds = uptime_sec % 60
        
        response_data = {
            "status": "online",
            "mode": "webhook",
            "service": "Prince Task Agent 24/7",
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "telegram_token_present": bool(TELEGRAM_BOT_TOKEN),
            "gemini_key_present": bool(GEMINI_API_KEY),
            "health": "ok"
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(json.dumps(response_data, indent=2).encode("utf-8"))

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len)
        
        # 1. Antigravity Delegation endpoint
        if self.path in ["/delegate", "/api/task"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "task_received_and_processing"}')
            try:
                data = json.loads(post_body.decode("utf-8"))
                task_text = data.get("task", "")
                chat_id = data.get("chat_id", 2117099158)
                if task_text:
                    fake_message = {
                        "text": task_text,
                        "chat": {"id": chat_id},
                        "from": {"first_name": "Prince (via Antigravity)"}
                    }
                    threading.Thread(target=process_message, args=(fake_message,), daemon=True).start()
            except Exception as e:
                print(f"[!] Error handling delegation: {e}")
            return
            
        # 2. Telegram Webhook endpoint
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"OK")
        
        try:
            update = json.loads(post_body.decode("utf-8"))
            if "message" in update:
                threading.Thread(target=process_message, args=(update["message"],), daemon=True).start()
        except Exception as e:
            print(f"[!] Webhook parsing error: {e}")

    def log_message(self, format, *args):
        # Silence standard HTTP access logging to keep terminal clean
        return

def setup_telegram_webhook():
    """Configures Telegram to deliver updates to this server via webhook."""
    webhook_url = "https://prince-task-agent.onrender.com/webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook?url={webhook_url}"
    try:
        r = requests.get(url, headers={"Connection": "close"}, timeout=10)
        print(f"[*] Telegram Webhook set: {r.text}")
    except Exception as e:
        print(f"[!] Webhook setup error: {e}")

# ----------------- MAIN ENTRYPOINT ----------------- #
if __name__ == "__main__":
    print("=" * 50)
    print("🤖 Launching Prince's 24/7 Autonomous AI Agent (Webhook Mode)")
    print("=" * 50)
    
    # Register webhook with Telegram
    if TELEGRAM_BOT_TOKEN:
        setup_telegram_webhook()
        
    # Start HTTP & Webhook Server
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, WebhookHandler)
    print(f"[*] Agent Server listening on port {PORT}...")
    httpd.serve_forever()
