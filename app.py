import os
import sys
import time
import json
import traceback
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

import socket
import urllib3.util.connection as urllib_connection
urllib_connection.allowed_gai_family = lambda: socket.AF_INET

# Environment configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

START_TIME = time.time()

# ----------------- HTTP HEALTHCHECK SERVER FOR RENDER ----------------- #
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        uptime_sec = int(time.time() - START_TIME)
        hours = uptime_sec // 3600
        minutes = (uptime_sec % 3600) // 60
        seconds = uptime_sec % 60
        
        response_data = {
            "status": "online",
            "service": "Prince Task Agent 24/7",
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "telegram_token_present": bool(TELEGRAM_BOT_TOKEN),
            "gemini_key_present": bool(GEMINI_API_KEY),
            "health": "ok"
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response_data, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        # Silence standard HTTP access logging to keep terminal clean
        return

def run_http_server():
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print(f"[*] Healthcheck HTTP Server listening on port {PORT}...")
    httpd.serve_forever()

# ----------------- AGENT TOOLS ----------------- #
def tool_web_search(query: str) -> str:
    """Searches the web using DuckDuckGo and returns summaries of top results."""
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
    """Fetches and extracts clean text content from a given URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = ' '.join(soup.stripped_strings)
        return text[:4000] # return first 4000 chars
    except Exception as e:
        return f"Error fetching webpage: {str(e)}"

def tool_run_python(code: str) -> str:
    """Executes Python code in an isolated subprocess and returns stdout and stderr."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30
        )
        output = proc.stdout
        if proc.stderr:
            output += "\nSTDERR:\n" + proc.stderr
        return output.strip() if output.strip() else "(Execution completed with no output)"
    except subprocess.TimeoutExpired:
        return "Error: Code execution timed out after 30 seconds."
    except Exception as e:
        return f"Execution error: {str(e)}"

TOOLS_DECLARATIONS = [
    {
        "name": "web_search",
        "description": "Search the live web for real-time information, news, articles, and answers.",
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
        "description": "Run Python code to calculate, process data, scrape, or solve complex logic.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "code": {"type": "STRING", "description": "The Python code to execute"}
            },
            "required": ["code"]
        }
    }
]

# ----------------- TELEGRAM HELPER FUNCTIONS ----------------- #
def telegram_send_message(chat_id: int, text: str, parse_mode: str = "Markdown") -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=10)
        # If markdown parsing fails, fallback to plain text
        if not r.ok:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[!] Telegram sendMessage error: {e}")

def telegram_send_document(chat_id: int, filename: str, content: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        files = {"document": (filename, content.encode("utf-8"))}
        requests.post(url, data={"chat_id": chat_id}, files=files, timeout=15)
    except Exception as e:
        print(f"[!] Telegram sendDocument error: {e}")

# ----------------- GEMINI AGENT EXECUTION LOOP ----------------- #
def ask_gemini_agent(user_prompt: str, chat_id: int) -> str:
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    
    system_instruction = (
        "You are Prince's elite 24/7 Autonomous AI Agent. "
        "You are deployed on a 24/7 cloud server and have full tools: web_search, fetch_webpage, and run_python. "
        "When the user asks you to do something, use your tools autonomously to gather information, "
        "execute scripts, crunch numbers, and deliver thorough, professional results. "
        "Always communicate respectfully, address the user as Sir, and summarize your findings clearly."
    )

    contents = [{"role": "user", "parts": [{"text": user_prompt}]}]
    
    # Run Agent Loop up to 5 steps
    for step in range(5):
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": contents,
            "tools": [{"function_declarations": TOOLS_DECLARATIONS}]
        }
        
        headers = {"Content-Type": "application/json", "Connection": "close"}
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
            data = resp.json()
            
            if "error" in data:
                return f"⚠️ Gemini API Error: {data['error'].get('message')}"
            
            candidate = data.get("candidates", [{}])[0]
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            
            if not parts:
                return "Task finished without output."
            
            # Check for function calls
            function_calls = [p["functionCall"] for p in parts if "functionCall" in p]
            
            if not function_calls:
                # Text answer produced!
                text_parts = [p["text"] for p in parts if "text" in p]
                return "\n".join(text_parts)
            
            # Add assistant's response with function call to conversation history
            contents.append(content)
            
            # Execute tool calls
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
            
            # Append tool responses as user role (standard Gemini format)
            contents.append({
                "role": "user",
                "parts": tool_response_parts
            })
            
        except requests.exceptions.Timeout:
            # Quick fallback without complex tools
            try:
                simple_payload = {"contents": [{"role": "user", "parts": [{"text": user_prompt}]}]}
                resp = requests.post(endpoint, json=simple_payload, headers=headers, timeout=25)
                data = resp.json()
                text_parts = [p.get("text", "") for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", []) if "text" in p]
                if text_parts:
                    return "".join(text_parts)
            except Exception:
                pass
            return "⚠️ Gemini request took too long. Please try again."
        except Exception as e:
            return f"⚠️ Agent error: {str(e)}"
            
    return "Task reached maximum tool iterations."

# ----------------- TELEGRAM POLLING WORKER ----------------- #
def run_telegram_bot():
    print("[*] Starting 24/7 Telegram Poller...")
    offset = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 25, "offset": offset}
            resp = requests.get(url, params=params, timeout=30)
            
            if not resp.ok:
                time.sleep(3)
                continue
                
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                text = message.get("text", "").strip()
                chat_id = message.get("chat", {}).get("id")
                user_first_name = message.get("from", {}).get("first_name", "Sir")
                
                if not text or not chat_id:
                    continue
                
                print(f"[+] Message received from {user_first_name} ({chat_id}): {text}")
                
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
                    continue
                    
                elif text == "/status":
                    uptime_sec = int(time.time() - START_TIME)
                    hours = uptime_sec // 3600
                    minutes = (uptime_sec % 3600) // 60
                    seconds = uptime_sec % 60
                    status_msg = (
                        f"🟢 **Agent Status:** Online & Active\n"
                        f"⏳ **Uptime:** {hours}h {minutes}m {seconds}s\n"
                        f"☁️ **Host:** 24/7 Cloud Server\n"
                        f"🧠 **Model:** Gemini Flash (Agentic Loop Enabled)\n"
                        f"🛠️ **Tools:** Web Search, Web Scraper, Python Runner"
                    )
                    telegram_send_message(chat_id, status_msg)
                    continue
                
                # Normal user task prompt
                telegram_send_message(chat_id, "⏳ *Working on your task, Sir... Please wait.*")
                
                # Execute agent
                agent_reply = ask_gemini_agent(text, chat_id)
                
                # Final response
                final_msg = f"{agent_reply}\n\n━━━━━━━━━━━━━━━━━━━━\n✅ *Task completed, Sir! You can check now.*"
                telegram_send_message(chat_id, final_msg)
                
        except Exception as e:
            print(f"[!] Polling loop error: {e}")
            time.sleep(3)

# ----------------- MAIN ENTRYPOINT ----------------- #
if __name__ == "__main__":
    print("=" * 50)
    print("🤖 Launching Prince's 24/7 Autonomous AI Agent")
    print("=" * 50)
    
    # 1. Start HTTP Server thread for Render
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    # 2. Start Telegram Bot in main thread
    run_telegram_bot()
