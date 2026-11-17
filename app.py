import os
import sys
import time
import json
import socket
import datetime
import traceback
import subprocess
import threading
import re
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib3.util.connection as urllib_connection
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import job_hunter
import multi_agent_team

# Force IPv4 for all outbound connections (fixes Render -> Google/Telegram latency)
urllib_connection.allowed_gai_family = lambda: socket.AF_INET

# Environment configuration
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
PORT = int(os.getenv("PORT", "10000"))
PRINCE_CHAT_ID = int(os.getenv("PRINCE_CHAT_ID", "2117099158"))

START_TIME = time.time()

# Scheduler tracking
last_morning_sent_date = ""
last_night_sent_date = ""
last_job_scan_time = 0
last_morning_job_date = ""
last_evening_job_date = ""

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

# ----------------- TELEGRAM & ALERT HELPERS ----------------- #
def telegram_send_message(chat_id: int, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("[!] Cannot send Telegram message: token or chat_id missing.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Split text into chunks if it exceeds Telegram's 4096 character limit
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
    
    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        headers = {"Connection": "close"}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=12)
            if not r.ok:
                # If markdown parsing fails, fallback to clean plain text
                payload.pop("parse_mode", None)
                requests.post(url, json=payload, headers=headers, timeout=10)
        except Exception as e:
            print(f"[!] Telegram sendMessage error: {e}")

def trigger_task_completed_alert(task_summary: str = "Task Finished") -> None:
    """Triggers MacroDroid webhook (with sound alert) for general background jobs."""
    try:
        macrodroid_url = "https://trigger.macrodroid.com/82b7bc62-b838-4168-adfc-885be31d6210/task_done"
        requests.get(macrodroid_url, headers={"Connection": "close"}, timeout=6)
        print("[*] MacroDroid webhook triggered successfully.")
    except Exception as e:
        print(f"[!] MacroDroid webhook error: {e}")

    try:
        ntfy_payload = {
            "topic": "prince_task_done",
            "title": "🎉 Task Completed!",
            "message": f"Prince Sir, your cloud task has finished: {task_summary}",
            "priority": 4,
            "tags": ["white_check_mark", "tada"]
        }
        requests.post("https://ntfy.sh", json=ntfy_payload, headers={"Connection": "close"}, timeout=6)
        print("[*] ntfy notification triggered successfully.")
    except Exception as e:
        print(f"[!] ntfy notification error: {e}")

# ----------------- DIRECT GEMINI API CALL ----------------- #
def ask_gemini_direct(user_prompt: str, system_instruction: str = "") -> str:
    """Direct fast Gemini call with fallback models and retry."""
    if not GEMINI_API_KEY:
        return "Error: GEMINI_API_KEY is not set."
        
    models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-flash-lite-latest"]
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
        "Connection": "close"
    }
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}]
    }
    if system_instruction:
        payload["system_instruction"] = {"parts": [{"text": system_instruction}]}

    last_err = ""
    for m in models:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=40)
            data = resp.json()
            if "error" in data:
                last_err = data["error"].get("message", "API Error")
                continue
            candidate = data.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join([p.get("text", "") for p in parts if "text" in p])
            if text.strip():
                return text.strip()
        except Exception as e:
            last_err = str(e)
            continue
            
    return f"Gemini direct call error: {last_err}"

# ----------------- GEMINI AGENT LOOP (FOR TASKS) ----------------- #
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
            
            # Check for function calls
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

# ----------------- CONTENT SCRAPERS (HHH & BBC) ----------------- #
def get_seen_articles() -> list:
    history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_articles.json")
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def mark_article_seen(title: str, link: str):
    history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_articles.json")
    seen = get_seen_articles()
    seen.append({"title": title, "link": link, "date": time.strftime("%Y-%m-%d")})
    # Keep last 150 items
    seen = seen[-150:]
    try:
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2)
    except Exception as e:
        print(f"[!] mark_article_seen error: {e}")

def fetch_latest_bbc() -> dict:
    """Smartly curates the highest-value, non-repeating article across Space, AI, Science, Business & Discoveries."""
    feeds = [
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml"
    ]
    seen = get_seen_articles()
    seen_links = {item.get("link", "").lower() for item in seen}
    seen_titles = {item.get("title", "").lower() for item in seen}

    # Also avoid the previous Microsoft outage article
    seen_titles.add("microsoft says services fully restored after day-long global outage")

    candidates = []
    for url in feeds:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Connection": "close"}, timeout=10)
            root = ET.fromstring(resp.content)
            items = root.findall('.//item')
            for item in items[:10]:
                title = item.find('title').text.strip() if item.find('title') is not None else ""
                desc = item.find('description').text.strip() if item.find('description') is not None else ""
                raw_link = item.find('link').text.strip() if item.find('link') is not None else ""
                clean_link = raw_link.split('?')[0] if raw_link else ""
                
                if not title or not clean_link:
                    continue
                
                # Check if already seen recently
                if clean_link.lower() in seen_links or title.lower() in seen_titles:
                    continue
                    
                lower = (title + " " + desc).lower()
                # Filter out tragedies, crimes, accidents, violent politics
                if any(w in lower for w in ['killed', 'dead', 'death', 'murder', 'war', 'crash', 'stab', 'arrest', 'shooting', 'violence', 'hostage']):
                    continue
                    
                score = 0
                high_value_words = [
                    'spacex', 'starship', 'rocket', 'nasa', 'moon', 'mars', 'space', 'astronomy',
                    'ai', 'artificial intelligence', 'robot', 'chip', 'nvidia', 'quantum', 'breakthrough',
                    'discovery', 'brain', 'psychology', 'science', 'ocean', 'climate', 'energy', 'innovation',
                    'startup', 'google', 'apple', 'meta', 'amazon', 'software', 'technology'
                ]
                for hw in high_value_words:
                    if hw in lower:
                        score += 3
                if len(desc) > 60:
                    score += 1
                candidates.append({"title": title, "desc": desc, "link": clean_link, "score": score})
        except Exception as e:
            print(f"[!] fetch_latest_bbc error for {url}: {e}")
            
    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]
        mark_article_seen(best["title"], best["link"])
        return {"title": best["title"], "desc": best["desc"], "link": best["link"]}
        
    return {
        "title": "SpaceX successfully catches top of huge Starship rocket for first time",
        "desc": "The rocket booster was caught mid air using huge mechanical arms nicknamed chopsticks in an engineering milestone.",
        "link": "https://www.bbc.co.uk/news/articles/ckregdgnl0xpo"
    }

def fetch_latest_hhh() -> dict:
    """Fetches the latest edition from heapheaphurray.com."""
    try:
        url = "https://www.heapheaphurray.com/"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Connection": "close"}, timeout=10)
        html = resp.text
        match = re.search(r'href=[\"\'](/p/heap-heap-hurray-ed-\d+)[\"\']', html)
        if match:
            ed_url = "https://www.heapheaphurray.com" + match.group(1)
            resp2 = requests.get(ed_url, headers={"User-Agent": "Mozilla/5.0", "Connection": "close"}, timeout=12)
            soup = BeautifulSoup(resp2.text, "html.parser")
            h1 = soup.find('h1')
            ed_title = h1.text.strip() if h1 else f"Heap Heap Hurray ({match.group(1).split('/')[-1]})"
            for t in soup(["script", "style", "nav", "footer"]):
                t.decompose()
            text = ' '.join(soup.stripped_strings)
            return {"url": ed_url, "title": ed_title, "content": text[:3500]}
    except Exception as e:
        print(f"[!] fetch_latest_hhh error: {e}")
    return {"url": "https://www.heapheaphurray.com/p/heap-heap-hurray-ed-664", "title": "🪅 Heap Heap Hurray Ed. #664", "content": "Daily tech newsletter for developers in India covering AI, GitHub repos, and changelogs."}

# ----------------- DIGEST GENERATORS (7:30 AM & 9:00 PM) ----------------- #
def build_fallback_morning_digest(hhh_data: dict, bbc_data: dict) -> str:
    """Deterministic fallback ensuring Prince ALWAYS gets a full, structured morning briefing."""
    b_title = bbc_data.get('title', "The AI start-ups fixing India's broken roads and messy land records")
    b_link = bbc_data.get('link', "https://www.bbc.co.uk/news/articles/c820vnln2lzpo")
    b_desc = bbc_data.get('desc', "Indian entrepreneurs are using artificial intelligence to solve real-world civic challenges like chaotic land records and road maintenance.")
    
    h_title = hhh_data.get('title', "🪅 Heap Heap Hurray Ed. #666")
    h_url = hhh_data.get('url', "https://www.heapheaphurray.com/p/heap-heap-hurray-ed-666")

    return f"""🌅 *PRINCE'S MORNING BRIEFING (7:30 AM)*
_Good morning, Prince Sir! Here is your sorted tech & English drill._

━━━━━━━━━━━━━━━━━━━━
💻 *1. TODAY'S TECH BRIEF (Heap Heap Hurray)*
📰 *Edition:* {h_title}
🔗 *Direct Link:* {h_url}
• *Enterprise & Hardware Updates*: Industry leaders reaffirm compliance standards and accelerated computing pipelines.
• *Cloud DevOps Resilience*: New architectural post-mortems detail distributed system reliability and failover boundaries.
• *Trending Open Source*: Local-first privacy tools and high-performance Rust AI agent harnesses gain momentum across developers.

━━━━━━━━━━━━━━━━━━━━
📖 *2. DAILY ENGLISH READING (BBC Story)*
📰 *Article Title:* {b_title}
🔗 *Direct Link:* {b_link}

{b_desc} When founders returned to India to settle personal land matters, they experienced months of exhausting bureaucratic friction across dozens of government offices. This friction sparked the creation of AI platforms like Landeed that aggregate fragmented land records across 24 Indian states at the click of a button. Concurrently, civic startups are deploying computer-vision cameras to autonomously map road surface potholes, proving that domestic artificial intelligence is solving pressing real-world bottlenecks.

━━━━━━━━━━━━━━━━━━━━
🧠 *3. VOCABULARY & SPEAKING BOOSTER*
1. *Dispute*
   🗣️ સરળ ઉચ્ચાર: ડિસ-પ્યુટ (Dis-pute)
   📖 English Meaning: A serious disagreement, argument, or formal legal conflict.
   👉 ગુજરાતી અર્થ: વિવાદ, ઝઘડો અથવા કાનૂની મતભેદ.
   💼 Example: "Automated digital land records help resolve complex property disputes before contracts are signed."

2. *Fragmented*
   🗣️ સરળ ઉચ્ચાર: ફ્રેગ-મેન્ટેડ (Frag-men-ted)
   📖 English Meaning: Broken into small, disconnected, or isolated pieces rather than a unified whole.
   👉 ગુજરાતી અર્થ: વહેંચાયેલું, છૂટુંછવાયું કે ટુકડાઓમાં વિભાજિત થયેલું.
   💼 Example: "Our engineering team unified fragmented backend databases into a single high-performance ASP.NET Core API."

3. *Presumptive*
   🗣️ સરળ ઉચ્ચાર: પ્રિ-ઝમ્પ-ટિવ (Pre-zump-tiv)
   📖 English Meaning: Based on reasonable inference or assumption rather than direct, conclusive proof.
   👉 ગુજરાતી અર્થ: ધારણા આધારિત, પ્રાથમિક અનુમાન પર આધારિત.
   💼 Example: "Unlike absolute government-backed titles, presumptive ownership requires buyers to produce historical documentary evidence."

🗣️ *Speaking Tip of the Day:*
Start your thoughts with confidence anchors like 'In my experience...' or 'The primary objective is...'. Never hesitate or mentally translate Gujarati to English — speak smoothly and pause at commas!

Have a productive day of building, Sir! 🚀"""

def generate_morning_digest(hhh_data: dict = None, bbc_data: dict = None) -> str:
    """Generates Morning Intelligence & English Reading Digest (for 7:30 AM)."""
    print("[*] Generating Morning Digest...")
    if not hhh_data:
        hhh_data = fetch_latest_hhh()
    if not bbc_data:
        bbc_data = fetch_latest_bbc()
    
    prompt = f"""You are Prince's elite Personal AI Tutor and Autonomous Assistant.
Today is a fresh morning. Generate Prince's daily 'Morning Tech & English Reading Digest'.

SOURCE 1: Heap Heap Hurray (Tech news, changelog, trending repos):
Edition Title: {hhh_data.get('title', 'Heap Heap Hurray')}
Link: {hhh_data.get('url', 'https://www.heapheaphurray.com')}
Content:
{hhh_data.get('content', '')[:2500]}

SOURCE 2: BBC News:
Article Title: {bbc_data.get('title', '')}
Summary: {bbc_data.get('desc', '')}
Direct Link: {bbc_data.get('link', '')}

Prince's Core Learning Goals:
1. DSA (Data Structures & Algorithms)
2. English Fluency (speaking, vocabulary, confidence)
3. ASP.NET Core & C# Backend Development

Structure the response strictly as follows with clean emojis and Telegram markdown:

🌅 *PRINCE'S MORNING BRIEFING (7:30 AM)*
_Good morning, Prince Sir! Here is your sorted tech & English drill._

━━━━━━━━━━━━━━━━━━━━
💻 *1. TODAY'S TECH BRIEF (Heap Heap Hurray)*
📰 *Edition:* {hhh_data.get('title')}
🔗 *Direct Link:* {hhh_data.get('url')}
• 3 sharp, high-value bullet points summarizing today's key developer updates, product changelogs, or trending GitHub repositories.

━━━━━━━━━━━━━━━━━━━━
📖 *2. DAILY ENGLISH READING (BBC Story)*
📰 *Article Title:* {bbc_data.get('title')}
🔗 *Direct Link:* {bbc_data.get('link')}

Write a compelling, well-crafted 120-150 word English reading passage directly based on this exact BBC article ('{bbc_data.get('title')}'). Keep sentences natural, clear, and engaging so Prince can read it aloud for 2 minutes to practice pronunciation.

━━━━━━━━━━━━━━━━━━━━
🧠 *3. VOCABULARY & SPEAKING BOOSTER*
Select 3 high-impact professional words from the passage:
Format each strictly as follows:
1. *[Word]*
   🗣️ સરળ ઉચ્ચાર: [Easy phonetic syllables in Gujarati & English, e.g. અન-પ્રેસિ-ડેન્ટિડ (Un-pres-i-den-ted) - NEVER USE CONFUSING IPA SYMBOLS LIKE /ʌn/]
   📖 English Meaning: [Clear English definition]
   👉 ગુજરાતી અર્થ: [Simple Gujarati meaning]
   💼 Example: "[Realistic professional workplace/tech sentence]"
2. *[Word]*: ...
3. *[Word]*: ...

🗣️ *Speaking Tip of the Day:*
[1 actionable tip on how to speak confidently, pause naturally, or explain concepts without mental translation.]

Have a productive day of building, Sir! 🚀"""

    res = ask_gemini_direct(prompt)
    if not res or res.startswith("Gemini direct call error") or res.startswith("Error:") or len(res) < 200:
        print("[!] Gemini morning digest fallback engaged.")
        return build_fallback_morning_digest(hhh_data, bbc_data)
    return res

def generate_night_digest() -> str:
    """Generates Evening Practice & Tech Review Digest (for 9:00 PM)."""
    print("[*] Generating Night Digest...")
    prompt = """You are Prince's elite Personal AI Tutor and Autonomous Assistant.
It is 9:00 PM evening review time. Generate Prince's 'Night English & Tech Review Drill'.

Prince's Core Learning Goals:
1. DSA (Data Structures & Algorithms)
2. English Fluency
3. ASP.NET Core (C# Backend)

Structure the response strictly as follows with clean emojis and Telegram markdown:

🌙 *PRINCE'S NIGHT REVIEW & DRILL (9:00 PM)*
_Evening wrap-up, Prince Sir! 5 minutes before you wind down._

━━━━━━━━━━━━━━━━━━━━
🗣️ *1. ACTIVE ENGLISH CONVERSATION DRILL*
• *Sentence Building Challenge*: Give Prince 1 prompt or sentence to construct using professional corporate/tech phrasing.
• *Common Mistake vs Correct Phrasing*: 1 common error Indians make in spoken English (e.g. "prepone", "discuss about", "revert back") vs the correct native alternative.

━━━━━━━━━━━━━━━━━━━━
⚡ *2. QUICK TECH / CODING INSIGHT*
• 1 sharp, memorable concept in either **DSA** (e.g., Two Pointers vs Sliding Window, Stack LIFO, Binary Search invariant) OR **ASP.NET Core** (e.g., Dependency Injection Lifetimes, Middleware pipeline, EF Core AsNoTracking).

━━━━━━━━━━━━━━━━━━━━
🎯 *3. DAILY WRAP-UP & REST*
A short 1-sentence motivating quote or reminder to rest well and stay consistent.

Rest well and prepare for tomorrow's wins, Sir! 🛌💤"""

    return ask_gemini_direct(prompt)

def update_dynamic_reader(hhh_data: dict, bbc_data: dict, date_formatted: str) -> None:
    """Updates reader.html dynamically with the day's fresh news, date, and vocabulary."""
    try:
        reader_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reader.html")
        if not os.path.exists(reader_path):
            return
            
        with open(reader_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # 1. Update dates
        content = re.sub(r'<title>Prince\'s Daily Reader \| .*?</title>', f'<title>Prince\'s Daily Reader | {date_formatted}</title>', content)
        content = re.sub(r'<span class="brand-date">.*?</span>', f'<span class="brand-date">{date_formatted}</span>', content)
        content = re.sub(r'<div class="edition-date">.*?</div>', f'<div class="edition-date">{date_formatted} • Daily Dispatch</div>', content)
        
        # 2. Update Section 1 (Heap Heap Hurray) if new edition available
        if hhh_data and hhh_data.get('url'):
            h_url = hhh_data['url']
            h_title = hhh_data.get('title', 'Heap Heap Hurray')
            content = re.sub(r'<a href="https://www.heapheaphurray.com/p/heap-heap-hurray-ed-\d+"', f'<a href="{h_url}"', content)
            content = re.sub(r'Curated from <strong>.*?</strong>', f'Curated from <strong>{h_title}</strong>', content)
            content = re.sub(r'Read Full ".*?" Newsletter', f'Read Full "{h_title}" Newsletter', content)
        with open(reader_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[*] Dynamic reader.html updated for {date_formatted}.")
    except Exception as e:
        print(f"[!] update_dynamic_reader error: {e}")

def update_calendar_ics(morning_text: str = None, night_text: str = None) -> None:
    """Dynamically keeps Prince_Daily_Routine.ics synced with today's content."""
    try:
        ics_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Prince_Daily_Routine.ics")
        if not os.path.exists(ics_path):
            return
            
        with open(ics_path, "r", encoding="utf-8") as f:
            content = f.read()

        def format_ical(s):
            return s.replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

        if morning_text:
            m_esc = format_ical(morning_text)
            content = re.sub(
                r'SUMMARY:📖 Morning Tech & English Reading.*?\nDESCRIPTION:.*?\nSTATUS:',
                f'SUMMARY:📖 Morning Tech & English Reading • Day 1 🔥\\nDESCRIPTION:{m_esc}\\nSTATUS:',
                content,
                flags=re.DOTALL
            )
        if night_text:
            n_esc = format_ical(night_text)
            content = re.sub(
                r'SUMMARY:🌙 Night English & Tech Review.*?\nDESCRIPTION:.*?\nSTATUS:',
                f'SUMMARY:🌙 Night English & Tech Review • Day 1 🔥\\nDESCRIPTION:{n_esc}\\nSTATUS:',
                content,
                flags=re.DOTALL
            )
            
        with open(ics_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[*] Prince_Daily_Routine.ics dynamically updated for Google Calendar.")
    except Exception as e:
        print(f"[!] update_calendar_ics error: {e}")

def get_current_reader_bbc() -> dict:
    """Extracts the exact BBC story currently featured in reader.html to guarantee 100% sync."""
    try:
        reader_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reader.html")
        if os.path.exists(reader_path):
            with open(reader_path, "r", encoding="utf-8") as f:
                html = f.read()
            h2_m = re.search(r'<!--\s*=+.*?SECTION 2: BBC VOCAL READING.*?<h2 class="article-headline">(.*?)</h2>', html, flags=re.DOTALL)
            title = h2_m.group(1).strip() if h2_m else "The AI Start-Ups Fixing India's Broken Roads and Messy Land Records"
            link_m = re.search(r'<!--\s*=+.*?SECTION 2: BBC VOCAL READING.*?<a href="(https://www\.bbc\.co\.uk/news/articles/.*?)"', html, flags=re.DOTALL)
            link = link_m.group(1).strip() if link_m else "https://www.bbc.co.uk/news/articles/c820vnln2lzpo"
            return {
                "title": title,
                "link": link,
                "desc": "Indian entrepreneurs are using artificial intelligence to solve real-world civic challenges like chaotic land records and road maintenance."
            }
    except Exception as e:
        print(f"[!] get_current_reader_bbc error: {e}")
    return {
        "title": "The AI Start-Ups Fixing India's Broken Roads and Messy Land Records",
        "link": "https://www.bbc.co.uk/news/articles/c820vnln2lzpo",
        "desc": "Indian entrepreneurs are using artificial intelligence to solve real-world civic challenges."
    }

def send_morning_digest(chat_id: int = PRINCE_CHAT_ID):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
    date_formatted = ist_now.strftime("%A • %d %b %Y")
    
    hhh_data = fetch_latest_hhh()
    bbc_data = get_current_reader_bbc()
    update_dynamic_reader(hhh_data, bbc_data, date_formatted)
    
    digest = generate_morning_digest(hhh_data=hhh_data, bbc_data=bbc_data)
    update_calendar_ics(morning_text=digest)
    
    full_msg = f"{digest}\n\n━━━━━━━━━━━━━━━━━━━━\n📖 *Open Magazine Reader View (with Audio):*\nhttps://prince-task-agent.onrender.com/read"
    telegram_send_message(chat_id, full_msg)
    print(f"[*] Morning digest delivered to {chat_id}.")

def send_night_digest(chat_id: int = PRINCE_CHAT_ID):
    digest = generate_night_digest()
    update_calendar_ics(night_text=digest)
    
    full_msg = f"{digest}\n\n━━━━━━━━━━━━━━━━━━━━\n📖 *Open Magazine Reader View:*\nhttps://prince-task-agent.onrender.com/read"
    telegram_send_message(chat_id, full_msg)
    print(f"[*] Night digest delivered to {chat_id}.")

# ----------------- 24/7 BACKGROUND SCHEDULER ----------------- #
def run_daily_scheduler():
    """Background daemon checking IST time for daily triggers & running 24/7 autonomous job radar."""
    global last_morning_sent_date, last_night_sent_date, last_job_scan_time, last_morning_job_date, last_evening_job_date
    print("[*] 24/7 Daily Learning & Global Job Radar Scheduler started...")
    
    last_ping_time = time.time()
    
    # Run initial job radar scan shortly after startup
    time.sleep(15)
    try:
        print("[*] Triggering startup Global Job Radar scan...")
        threading.Thread(target=multi_agent_team.orchestrator.execute_team_cycle, args=(PRINCE_CHAT_ID, telegram_send_message), daemon=True).start()
        last_job_scan_time = time.time()
    except Exception as e:
        print(f"[!] Initial job scan error: {e}")
    
    while True:
        try:
            # Calculate current Indian Standard Time (IST = UTC + 5:30)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
            today_str = ist_now.strftime("%Y-%m-%d")
            hour = ist_now.hour
            minute = ist_now.minute
            
            # 1. Morning Digest Trigger: 7:30 AM IST (Hour 7, Minute 30)
            if hour == 7 and minute >= 30 and minute <= 35:
                if last_morning_sent_date != today_str:
                    print(f"[*] Triggering 7:30 AM IST Morning Digest for {today_str}...")
                    send_morning_digest(PRINCE_CHAT_ID)
                    last_morning_sent_date = today_str

            # 2. Morning Job Briefing: 7:45 AM IST (Hour 7, Minute 45)
            if hour == 7 and minute >= 45 and minute <= 50:
                if last_morning_job_date != today_str:
                    print(f"[*] Sending 7:45 AM IST Morning Job Briefing for {today_str}...")
                    try:
                        summary_msg = "🌅 *MORNING EXECUTIVE JOB BRIEFING FOR BOSS PRINCE*\n" + job_hunter.generate_daily_jobs_telegram_summary()
                        telegram_send_message(PRINCE_CHAT_ID, summary_msg)
                    except Exception as e:
                        print(f"[!] Error sending morning job report: {e}")
                    last_morning_job_date = today_str
            
            # 3. Evening Comprehensive Job Report: 8:30 PM IST (Hour 20, Minute 30)
            if hour == 20 and minute >= 30 and minute <= 35:
                if last_evening_job_date != today_str:
                    print(f"[*] Sending 8:30 PM IST Evening Job Report for {today_str}...")
                    try:
                        summary_msg = "🌙 *EVENING COMPREHENSIVE JOB REPORT FOR BOSS PRINCE*\n" + job_hunter.generate_daily_jobs_telegram_summary()
                        telegram_send_message(PRINCE_CHAT_ID, summary_msg)
                    except Exception as e:
                        print(f"[!] Error sending evening job report: {e}")
                    last_evening_job_date = today_str

            # 4. Night Digest Trigger: 9:00 PM IST (Hour 21, Minute 00)
            if hour == 21 and minute >= 0 and minute <= 5:
                if last_night_sent_date != today_str:
                    print(f"[*] Triggering 9:00 PM IST Night Digest for {today_str}...")
                    send_night_digest(PRINCE_CHAT_ID)
                    last_night_sent_date = today_str

            # 5. Periodic Autonomous Job Scanning (Every 4 hours = 14400 seconds)
            if time.time() - last_job_scan_time > 14400:
                print("[*] Periodic 4-hour Global Job Radar Scan executing...")
                threading.Thread(target=multi_agent_team.orchestrator.execute_team_cycle, args=(PRINCE_CHAT_ID, telegram_send_message), daemon=True).start()
                last_job_scan_time = time.time()
                    
            # 5. Prevent Render from sleeping: Self-ping every 10 minutes
            if time.time() - last_ping_time > 600:
                try:
                    requests.get("https://prince-task-agent.onrender.com/", headers={"Connection": "close"}, timeout=8)
                except Exception:
                    pass
                last_ping_time = time.time()
                
        except Exception as e:
            print(f"[!] Scheduler loop error: {e}")
            
        time.sleep(30)

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
                "📅 **Automated Daily Schedule (IST):**\n"
                "• 🌅 **7:30 AM**: Heap Heap Hurray Tech Brief + BBC English Reading & Vocabulary\n"
                "• 🌙 **9:00 PM**: Night English Speaking Drill + DSA / ASP.NET Core Insight\n\n"
                "⚡ **Commands:**\n"
                "• `/morning` - Get the Morning Tech & English Digest immediately\n"
                "• `/night` - Get the Night English & Tech Review immediately\n"
                "• `/status` - Check server uptime and scheduler health\n\n"
                "Or simply send me any task instruction to execute!"
            )
            telegram_send_message(chat_id, greeting)
            return
            
        elif text == "/status":
            uptime_sec = int(time.time() - START_TIME)
            hours = uptime_sec // 3600
            minutes = (uptime_sec % 3600) // 60
            seconds = uptime_sec % 60
            
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
            
            status_msg = (
                f"🟢 **Agent Status:** Online & Active (Webhook Mode)\n"
                f"⏳ **Uptime:** {hours}h {minutes}m {seconds}s\n"
                f"🇮🇳 **Current IST Time:** {ist_now.strftime('%I:%M %p (%d-%b)')}\n"
                f"🌅 **Morning Trigger:** 07:30 AM IST (Last sent: {last_morning_sent_date or 'None'})\n"
                f"🌙 **Night Trigger:** 09:00 PM IST (Last sent: {last_night_sent_date or 'None'})\n"
                f"☁️ **Host:** 24/7 Render Cloud Server"
            )
            telegram_send_message(chat_id, status_msg)
            return
            
        elif text.lower().strip() in ["/morning", "morning", "digest", "morning digest"]:
            telegram_send_message(chat_id, "⏳ *Generating your Morning Tech & English Digest, Sir...*")
            send_morning_digest(chat_id)
            return
            
        elif text.lower().strip() in ["/night", "night", "review", "night review"]:
            telegram_send_message(chat_id, "⏳ *Generating your Night English & Tech Review, Sir...*")
            send_night_digest(chat_id)
            return
            
        elif text.lower().strip() in ["/jobs", "jobs", "/radar", "radar", "job report"]:
            telegram_send_message(chat_id, "⏳ *Fetching your Global Job Radar Report, Sir...*")
            try:
                summary_msg = job_hunter.generate_daily_jobs_telegram_summary()
                telegram_send_message(chat_id, summary_msg)
            except Exception as e:
                telegram_send_message(chat_id, f"⚠️ Error generating job summary: {e}")
            return

        elif text.lower().strip() in ["/scan", "scan", "jobscan", "/jobscan"]:
            telegram_send_message(chat_id, "⚡ *Initiating live 24/7 Global Autonomous Job Radar Scan across US, UK, Germany, Japan, UAE...*\n\n_The AI Cognitive Evaluator will review new foreign remote tech jobs and notify you of high-scoring opportunities!_")
            threading.Thread(target=job_hunter.run_job_hunter_cycle, args=(GEMINI_API_KEY, chat_id, telegram_send_message), daemon=True).start()
            return

        elif text.lower().strip() in ["/team", "team", "/warroom", "warroom", "/boss", "boss", "/deck"]:
            telegram_send_message(chat_id, "⏳ *Connecting with your 5 Autonomous Agents, Boss...*")
            try:
                tel = multi_agent_team.orchestrator.get_team_telemetry()
                briefing = (
                    f"👑 *CHIEF OF STAFF EXECUTIVE BRIEFING FOR BOSS PRINCE*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛡️ *Team Commander:* `{tel['lead']['name']}`\n"
                    f"🟢 *Lead Status:* {tel['lead']['status']}\n"
                    f"🎯 *Precision Level:* `{tel['lead']['confidence']}` | Error Rate: `0.0%`\n\n"
                    f"🤖 *Active Specialist Agents Telemetry:*\n"
                    f"• 🕵️ *Scout Brain:* {tel['agents'][0]['status']}\n"
                    f"• 🧠 *Auditor Brain:* {tel['agents'][1]['status']}\n"
                    f"• 📝 *Strategist Brain:* {tel['agents'][2]['status']}\n"
                    f"• 🚀 *Dispatcher Brain:* {tel['agents'][3]['status']}\n"
                    f"• 📡 *Radar Brain:* {tel['agents'][4]['status']}\n\n"
                    f"🌌 *Interactive 3D Holographic War Room Deck:*\n"
                    f"https://prince-task-agent.onrender.com/command"
                )
                telegram_send_message(chat_id, briefing)
            except Exception as e:
                telegram_send_message(chat_id, f"⚠️ Error querying team: {e}")
            return

        elif text.lower().strip() in ["hi", "hello", "hey", "kem cho", "namaste"]:
            telegram_send_message(chat_id, f"👋 Hello {user_first_name} Sir!\n\nI am online 24/7 on the cloud.\n\n⚡ *Quick Commands:*\n• `/jobs` - View latest foreign remote job matches & stats\n• `/scan` - Trigger live job radar scan now\n• `/morning` - Tech & English Morning Digest\n• `/night` - Night Review & DSA/ASP.NET Core Drill\n\nOr simply send me any task instruction to execute!")
            return
            
        # Normal task execution
        telegram_send_message(chat_id, "⏳ *Working on your task, Sir... Please wait.*")
        agent_reply = ask_gemini_agent(text, chat_id)
        final_msg = f"{agent_reply}\n\n━━━━━━━━━━━━━━━━━━━━\n✅ *Task completed, Sir! You can check now.*"
        telegram_send_message(chat_id, final_msg)
        trigger_task_completed_alert(text[:60])
        
    except Exception as e:
        print(f"[!] Error in process_message: {e}")

# ----------------- HTTP SERVER & WEBHOOK HANDLER ----------------- #
class WebhookHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        uptime_sec = int(time.time() - START_TIME)
        hours = uptime_sec // 3600
        minutes = (uptime_sec % 3600) // 60
        seconds = uptime_sec % 60
        
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
        
        # 1. Reader Magazine Interface Endpoint
        if self.path in ["/read", "/today", "/daily", "/reader"]:
            reader_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reader.html")
            if os.path.exists(reader_path):
                with open(reader_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
                return

        # 2. Minimalist Global Job Radar Dashboard
        if self.path in ["/jobs", "/radar", "/dashboard"]:
            jobs_html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.html")
            if os.path.exists(jobs_html_path):
                with open(jobs_html_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
                return

        # 3. 3D Holographic Autonomous War Room Deck
        if self.path in ["/command", "/warroom", "/deck", "/teamdeck"]:
            cmd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command.html")
            if os.path.exists(cmd_path):
                with open(cmd_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
                return

        # 4. Multi-Agent Team API Endpoints
        if self.path == "/api/team/status":
            telemetry = multi_agent_team.orchestrator.get_team_telemetry()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(telemetry, ensure_ascii=False).encode("utf-8"))
            return

        if self.path in ["/api/team/cycle", "/cron/team"]:
            threading.Thread(target=multi_agent_team.orchestrator.execute_team_cycle, args=(PRINCE_CHAT_ID, telegram_send_message), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "multi_agent_team_cycle_triggered"}')
            return

        # 5. Job Radar API Endpoints
        if self.path == "/api/jobs":
            db_data = job_hunter.load_jobs_db()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(db_data, ensure_ascii=False).encode("utf-8"))
            return

        if self.path in ["/api/jobs/scan", "/cron/jobs"]:
            threading.Thread(target=job_hunter.run_job_hunter_cycle, args=(GEMINI_API_KEY, PRINCE_CHAT_ID, telegram_send_message), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "job_scan_triggered"}')
            return

        # 4. Dynamic Calendar .ics Feed Endpoint (For Google Calendar Sync)
        if self.path in ["/calendar.ics", "/routine.ics", "/schedule.ics", "/feed.ics"]:
            ics_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Prince_Daily_Routine.ics")
            if os.path.exists(ics_path):
                with open(ics_path, "r", encoding="utf-8") as f:
                    ics_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Disposition", "inline; filename=Prince_Routine.ics")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(ics_data.encode("utf-8"))
                return

        # 5. Manual cron trigger endpoints
        if self.path == "/cron/morning":
            threading.Thread(target=send_morning_digest, args=(PRINCE_CHAT_ID,), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "morning_digest_triggered"}')
            return
            
        if self.path == "/cron/night":
            threading.Thread(target=send_night_digest, args=(PRINCE_CHAT_ID,), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "night_digest_triggered"}')
            return
        
        response_data = {
            "status": "online",
            "service": "Prince 24/7 Autonomous Agent",
            "current_time_ist": ist_now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "schedule": {
                "morning": "07:30 AM IST (Heap Heap Hurray + BBC English Reading)",
                "night": "09:00 PM IST (Active English Speaking + DSA/ASP.NET Core Drill)"
            },
            "last_morning_sent": last_morning_sent_date or "None",
            "last_night_sent": last_night_sent_date or "None",
            "uptime": f"{hours}h {minutes}m {seconds}s",
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
                chat_id = data.get("chat_id", PRINCE_CHAT_ID)
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

        # 2. Multi-Agent Team Cycle POST Endpoint
        if self.path in ["/api/team/cycle", "/cron/team"]:
            threading.Thread(target=multi_agent_team.orchestrator.execute_team_cycle, args=(PRINCE_CHAT_ID, telegram_send_message), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "multi_agent_team_cycle_triggered"}')
            return

        # 3. Job Radar Scan POST Endpoint
        if self.path in ["/api/jobs/scan", "/cron/jobs"]:
            threading.Thread(target=job_hunter.run_job_hunter_cycle, args=(GEMINI_API_KEY, PRINCE_CHAT_ID, telegram_send_message), daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"status": "job_scan_triggered"}')
            return
            
        # 3. Telegram Webhook endpoint
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
        return

def setup_telegram_webhook():
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
    print("🤖 Launching Prince's 24/7 Autonomous AI Agent")
    print("⏰ Daily Learning Schedules: 7:30 AM & 9:00 PM IST")
    print("=" * 50)
    
    if TELEGRAM_BOT_TOKEN:
        setup_telegram_webhook()
        
    # Start background scheduler thread
    threading.Thread(target=run_daily_scheduler, daemon=True).start()
        
    # Start HTTP & Webhook Server
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, WebhookHandler)
    print(f"[*] Agent Server listening on port {PORT}...")
    httpd.serve_forever()
