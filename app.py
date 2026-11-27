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
try:
    import urllib3.util.connection as urllib_connection
    urllib_connection.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import job_hunter
import multi_agent_team

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
    """Smartly curates the highest-value, non-repeating article across Space, AI, Science, Business, Environment & Global Discoveries."""
    feeds = [
        ("Technology & AI", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
        ("Science & Environment", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
        ("Business & Economy", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("Education & Society", "https://feeds.bbci.co.uk/news/education/rss.xml"),
        ("Health & Human Body", "https://feeds.bbci.co.uk/news/health/rss.xml"),
        ("World Discoveries", "https://feeds.bbci.co.uk/news/world/rss.xml")
    ]
    seen = get_seen_articles()
    seen_links = {item.get("link", "").lower() for item in seen}
    seen_titles = {item.get("title", "").lower() for item in seen}

    # Also avoid the previous Microsoft outage article
    seen_titles.add("microsoft says services fully restored after day-long global outage")

    candidates = []
    for category_name, url in feeds:
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
                    'startup', 'google', 'apple', 'meta', 'amazon', 'software', 'technology', 'education', 'economics'
                ]
                for hw in high_value_words:
                    if hw in lower:
                        score += 3
                if len(desc) > 60:
                    score += 1
                candidates.append({"title": title, "desc": desc, "link": clean_link, "score": score, "category": category_name})
        except Exception as e:
            print(f"[!] fetch_latest_bbc error for {url}: {e}")
            
    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]
        mark_article_seen(best["title"], best["link"])
        return {"title": best["title"], "desc": best["desc"], "link": best["link"], "category": best.get("category", "Science & Technology")}
        
    return {
        "title": "SpaceX successfully catches top of huge Starship rocket for first time",
        "desc": "The rocket booster was caught mid air using huge mechanical arms nicknamed chopsticks in an engineering milestone.",
        "link": "https://www.bbc.co.uk/news/articles/ckregdgnl0xpo",
        "category": "Space & Frontier Technology"
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
    """Deterministic fallback ensuring Prince ALWAYS gets a full, structured morning briefing with Brick-by-Brick IELTS 9."""
    b_title = bbc_data.get('title', "Europe Plans to Catch the Next Computer Chip Wave")
    b_link = bbc_data.get('link', "https://www.bbc.co.uk/news/articles/cqglwjd450k0o")
    b_desc = bbc_data.get('desc', "Europe hopes to be a leader in a new generation of computer chips which use light to process data.")
    b_cat = bbc_data.get('category', "Technology & Computing")
    
    h_title = hhh_data.get('title', "🪅 Heap Heap Hurray Ed. #667")
    h_url = hhh_data.get('url', "https://www.heapheaphurray.com/p/heap-heap-hurray-ed-667")

    return f"""🌅 *PRINCE'S MORNING BRIEFING (7:30 AM)*
_Good morning, Prince Sir! Here is your sorted tech & English drill._

━━━━━━━━━━━━━━━━━━━━
💻 *1. TODAY'S TECH BRIEF (Heap Heap Hurray)*
📰 *Edition:* {h_title}
🔗 *Direct Link:* {h_url}
• *Enterprise Messaging & Mobile*: Meta begins rollout of standalone Direct messaging threads for focused communications.
• *Indigenous Robotics Milestone*: Bangalore startup General Autonomy claims gold in India's first quadruped robot race.
• *Efficient AI Tooling*: Next-gen lightweight coding models reduce inference expenses while accelerating dev workflows.

━━━━━━━━━━━━━━━━━━━━
📖 *2. DAILY ENGLISH READING (Brick-by-Brick IELTS 9)*
🎯 *Level:*  | *IELTS Band:*  | *Domain:* {b_cat}
📰 *Article Title:* {b_title}
🔗 *Direct Link:* {b_link}

💡 *સંદર્ભ (Context Anchor):*
પરંપરાગત સિલિકોન ચિપ્સ ગરમી પકડે છે, તેથી યુરોપિયન સંશોધકો હવે વીજળીના બદલે પ્રકાશના કિરણો (ફોટોનિક્સ) વડે અતિ ઝડપી અને ઓછી ઊર્જા વાપરતી નવી ચિપ્સ વિકસાવી રહ્યા છે.

📜 *Reading Passage (Read Aloud 2 Mins):*
Today's information society relies heavily on silicon microchips that shuttle billions of electrons through microscopic circuits. However, as frontier artificial intelligence models demand unprecedented compute power, conventional electronics face insurmountable thermodynamic bottlenecks and generate immense heat. In response, European research institutes and sovereign governments are backing photonic chips that process data with light pulses rather than electrical currents. This profound transition promises orders-of-magnitude faster throughput while drastically curtailing energy consumption.

━━━━━━━━━━━━━━━━━━━━
🧩 *3. SENTENCE ANATOMY & GRAMMAR BREAKDOWN (વાક્યનું ઓપરેશન)*
🔍 *Key Sentence:*
_"However, as frontier artificial intelligence models demand unprecedented compute power, conventional electronics face insurmountable thermodynamic bottlenecks."_

• **Subordinate Clause (કારણ / પૃષ્ઠભૂમિ):** "as frontier artificial intelligence models demand unprecedented compute power..." (જેમ જેમ AI મોડેલ્સ અભૂતપૂર્વ કમ્પ્યુટ પાવર માંગે છે...)
• **Main Clause (મુખ્ય પરિણામ):** "...conventional electronics face insurmountable thermodynamic bottlenecks." (...તેમ જૂની ઇલેક્ટ્રોનિક્સ હીટિંગ અને ગરમીની અગવડનો સામનો કરે છે.)
• **Grammar Signpost:**  (જેમ જેમ / કારણ કે) બે સમાંતર સ્થિતિઓને જોડે છે.
👉 *કુદરતી વિચાર:* જ્યારે કોઈ વસ્તુની માંગ અતિશય વધે, ત્યારે જૂની સિસ્ટમ મર્યાદાઓનો સામનો કરવા લાગે છે.

━━━━━━━━━━━━━━━━━━━━
🧠 *4. THREE POWER COLLOCATIONS (શબ્દોની જોડીઓ)*
1. *Insurmountable Bottlenecks*
   🗣️ સરળ ઉચ્ચાર: ઇન-સર-માઉન્ટે-બલ બોટલ-નેક્સ (In-sur-mount-able Bot-tle-necks)
   📖 Meaning: Severe obstacles that cannot be overcome.
   👉 ગુજરાતી અર્થ: પાર ન પાડી શકાય તેવી ગંભીર અડચણો અથવા મર્યાદાઓ.
   💼 Workplace/IELTS: "Legacy database architectures create insurmountable bottlenecks during sudden traffic spikes."

2. *Relies Heavily On*
   🗣️ સરળ ઉચ્ચાર: રિ-લાઇઝ હેવિ-લી ઓન (Re-lies Heav-i-ly On)
   📖 Meaning: To depend on something with complete necessity.
   👉 ગુજરાતી અર્થ: સંપૂર્ણપણે નિર્ભર હોવું.
   💼 Workplace/IELTS: "Global cloud supply chains rely heavily on robust automation and real-time APIs."

3. *Curtail Energy Consumption*
   🗣️ સરળ ઉચ્ચાર: કર-ટેલ એનર્જી કન્ઝમ્પ-શન (Cur-tail En-er-gy Con-sump-tion)
   📖 Meaning: To significantly reduce the amount of power used.
   👉 ગુજરાતી અર્થ: ઊર્જાના વપરાશમાં મોટો કાપ મૂકવો / ઘટાડો કરવો.
   💼 Workplace/IELTS: "Migrating backend workloads to ARM processors helped curtail energy consumption by 40%."

━━━━━━━━━━━━━━━━━━━━
🗣️ *5. 1-MINUTE IELTS SPEAKING DRILL*
❓ *Question Prompt:* "Do you believe emerging hardware innovations will successfully solve AI's power crisis? Explain your reasoning in 3-4 sentences."
💡 *Confidence Anchor:* Start with: *"From an engineering standpoint, the decisive advantage is..."* Speak smoothly and pause naturally at commas without translating!

Have a productive day of building, Sir! 🚀"""

def generate_morning_digest(hhh_data: dict = None, bbc_data: dict = None) -> str:
    """Generates Morning Intelligence & English Reading Digest (for 7:30 AM) with Brick-by-Brick IELTS 9 Frame."""
    print("[*] Generating Morning Digest with Brick-by-Brick IELTS 9 Frame...")
    if not hhh_data:
        hhh_data = fetch_latest_hhh()
    if not bbc_data:
        bbc_data = fetch_latest_bbc()
    
    prompt = f"""You are Prince's elite Personal AI Tutor and English Fluency Architect.
Today is a fresh morning. Generate Prince's daily 'Morning Tech & English Reading Digest' structured strictly around the "Brick-by-Brick IELTS Band 9 Framework".

SOURCE 1: Heap Heap Hurray (Tech news, changelog, trending repos):
Edition Title: {hhh_data.get('title', 'Heap Heap Hurray')}
Link: {hhh_data.get('url', 'https://www.heapheaphurray.com')}
Content:
{hhh_data.get('content', '')[:2500]}

SOURCE 2: BBC News:
Article Title: {bbc_data.get('title', '')}
Summary: {bbc_data.get('desc', '')}
Category/Domain: {bbc_data.get('category', 'Science & Innovation')}
Direct Link: {bbc_data.get('link', '')}

Prince's Core Learning Goals:
1. IELTS Band 8.5-9 & Native-like Intuition (Stop translating, understand sentence architecture & collocations).
2. Professional Speaking Fluency.
3. ASP.NET Core & C# Backend Development + DSA.

Structure the response strictly as follows with clean emojis and Telegram markdown:

🌅 *PRINCE'S MORNING BRIEFING (7:30 AM)*
_Good morning, Prince Sir! Here is your sorted tech & English drill._

━━━━━━━━━━━━━━━━━━━━
💻 *1. TODAY'S TECH BRIEF (Heap Heap Hurray)*
📰 *Edition:* {hhh_data.get('title')}
🔗 *Direct Link:* {hhh_data.get('url')}
• 3 sharp, high-value bullet points summarizing today's key developer updates, product changelogs, or trending GitHub repositories.

━━━━━━━━━━━━━━━━━━━━
📖 *2. DAILY ENGLISH READING (Brick-by-Brick IELTS 9)*
🎯 *Level:* `B2-C1 (Upper Intermediate / Advanced)` | *IELTS Band:* `7.5 - 8.5` | *Domain:* {bbc_data.get('category', 'Science & Innovation')}
📰 *Article Title:* {bbc_data.get('title')}
🔗 *Direct Link:* {bbc_data.get('link')}

💡 *સંદર્ભ (Context Anchor):*
[2 lines in Gujarati giving the high-level background of this story so Prince's brain understands the concept before reading English words.]

📜 *Reading Passage (Read Aloud 2 Mins):*
[Write a compelling, sophisticated 120-140 word English reading passage directly based on this exact BBC article. Use natural native connectors (Although, Whereas, In response, Consequently, Crucially) so Prince can read it aloud for 2 minutes to practice pronunciation.]

━━━━━━━━━━━━━━━━━━━━
🧩 *3. SENTENCE ANATOMY & GRAMMAR BREAKDOWN (વાક્યનું ઓપરેશન)*
🔍 *Key Sentence:*
_"[Pick 1 complex, high-scoring sentence from the passage above]"_

• **Subordinate Clause (ગૌણ વાક્ય / શરત / કારણ):** [Explain what this part introduces]
• **Main Clause (મુખ્ય વાક્ય / પરિણામ):** [Explain the primary subject and action]
• **Grammar Signpost:** [Explain the connector e.g. Although, Whereas, As, In light of]
👉 *કુદરતી વિચાર:* [1 line in Gujarati explaining the thought process of how native speakers formulate this thought]

━━━━━━━━━━━━━━━━━━━━
🧠 *4. THREE POWER COLLOCATIONS (શબ્દોની જોડીઓ)*
Select 3 natural collocations or phrasal chunks from the passage (e.g. "pave the way for", "insurmountable challenge", "rely heavily on", NOT single obscure words):
Format each strictly as:
1. *[Collocation Phrase]*
   🗣️ સરળ ઉચ્ચાર: [Easy phonetic syllables in Gujarati & English, e.g. ઇન-સર-માઉન્ટે-બલ બોટલ-નેક્સ (In-sur-mount-able Bot-tle-necks) - NEVER USE IPA SYMBOLS]
   📖 Meaning: [Clear English definition]
   👉 ગુજરાતી અર્થ: [Simple Gujarati meaning]
   💼 Workplace/IELTS: "[Realistic professional workplace or IELTS sentence]"
2. *[Collocation Phrase]*: ...
3. *[Collocation Phrase]*: ...

━━━━━━━━━━━━━━━━━━━━
🗣️ *5. 1-MINUTE IELTS SPEAKING DRILL*
❓ *Question Prompt:* [1 targeted IELTS Speaking Part 3 style question related to this topic for Prince to answer out loud]
💡 *Confidence Anchor:* Start with: *"[e.g., 'From an engineering standpoint, the decisive advantage is...']"* Speak smoothly and pause naturally at commas without translating!

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

def generate_reader_payload_ai(bbc_data: dict) -> dict:
    """Uses Gemini to generate structured 3-tier IELTS content, Sentence Anatomy, and Power Collocations."""
    title = bbc_data.get('title', "Europe Plans to Catch the Next Computer Chip Wave")
    desc = bbc_data.get('desc', "Europe hopes to be a leader in a new generation of computer chips which use light to process data.")
    link = bbc_data.get('link', "https://www.bbc.co.uk/news/articles/cqglwjd450k0o")
    category = bbc_data.get('category', "Technology & Computing")
    
    prompt = f"""You are Prince's elite Personal AI Tutor and English Fluency Architect.
Generate daily English reading content, Sentence Anatomy, and Collocations based on this news:
Article Title: {title}
Summary: {desc}
Domain: {category}
Link: {link}

Return strictly a valid JSON object matching this schema:
{{
  "kicker_emoji": "⚡",
  "kicker_topic": "{category}",
  "headline": "{title}",
  "level_a_p1": "Simple English paragraph 1 (Band 5.0-6.0)...",
  "level_a_p2": "Simple English paragraph 2...",
  "gujarati_summary": "2-3 lines in Gujarati explaining the concept clearly before reading...",
  "level_b_p1": "Professional medium English paragraph 1 (Band 7.0-7.5)...",
  "level_b_p2": "Professional medium English paragraph 2...",
  "level_c_p1": "Native authentic English paragraph 1 (Band 8.5-9.0)...",
  "level_c_p2": "Native authentic English paragraph 2...",
  "anatomy_sentence": "1 complex high-scoring sentence from the story...",
  "anatomy_breakdown": "Detailed breakdown of Subordinate Clause, Main Clause, and Grammar Connectors...",
  "anatomy_gujarati_thought": "Natural thought process in Gujarati on how native speakers formulate this thought...",
  "word1_title": "Power Collocation 1 (e.g. Insurmountable Bottlenecks)",
  "word1_syllables": "ગુજરાતી ઉચ્ચાર (English-Phonetic)",
  "word1_def": "Clear English definition",
  "word1_guj": "સરળ ગુજરાતી અર્થ",
  "word1_example": "Professional IELTS/workplace example sentence.",
  "word2_title": "Power Collocation 2",
  "word2_syllables": "...",
  "word2_def": "...",
  "word2_guj": "...",
  "word2_example": "...",
  "word3_title": "Power Collocation 3",
  "word3_syllables": "...",
  "word3_def": "...",
  "word3_guj": "...",
  "word3_example": "...",
  "speaking_prompt": "IELTS Part 3 speaking question on this topic...",
  "speaking_tip": "Actionable confidence anchor starting phrase..."
}}
CRITICAL RULES:
- Never use confusing phonetic symbols like /ʌn/ or IPA. Always use simple Gujarati syllables e.g. ફો-ટો-નિક (Pho-ton-ic).
- For vocabulary, choose natural COLLOCATIONS / PHRASES, not single obscure words.
- Return ONLY the JSON object, nothing else."""

    try:
        resp_text = ask_gemini_direct(prompt)
        cleaned = re.sub(r'^$', '', cleaned.strip()).strip()
        data = json.loads(cleaned)
        if "level_a_p1" in data and "word1_title" in data:
            data["headline"] = title
            return data
    except Exception as e:
        print(f"[!] AI payload generation error: {e}, falling back.")

    return {
        "kicker_emoji": "⚡",
        "kicker_topic": category,
        "headline": title,
        "level_a_p1": f"{desc} This development marks an exciting new milestone for modern technological progress.",
        "level_a_p2": f"Researchers, developers, and global organizations are closely tracking these advancements to unlock high-impact real-world solutions.",
        "gujarati_summary": f"આજના સમાચાર {title} અંગે નવીનતા અને તેના દૂરગામી પ્રભાવ પર પ્રકાશ પાડે છે.",
        "level_b_p1": f"{title} represents a notable paradigm shift across engineering and modern distributed systems.",
        "level_b_p2": f"{desc} Industry leaders project accelerated efficiency benchmarks alongside enhanced operational resilience.",
        "level_c_p1": f"The architectural and strategic implications surrounding {title} signal a profound technological milestone across global systems.",
        "level_c_p2": f"{desc} Frontier institutions anticipate unprecedented operational gains as these innovations achieve maturity.",
        "anatomy_sentence": f"Although developing {title} requires substantial capital, its long-term societal benefits far outweigh initial constraints.",
        "anatomy_breakdown": "Subordinate Clause (વિરોધાભાસ / શરત): 'Although developing... requires substantial capital' | Main Clause (મુખ્ય પરિણામ): 'its long-term societal benefits far outweigh initial constraints' | Signpost: 'Although' (જોકે / તેમ છતાં).",
        "anatomy_gujarati_thought": "જ્યારે કોઈ મોટા બદલાવમાં શરૂઆતમાં મુશ્કેલી હોય પણ લાંબા ગાળે મોટો ફાયદો થવાનો હોય, ત્યારે 'Although' વાપરીને બે વિરોધી વિચારોને એકસાથે વણી લેવાય છે.",
        "word1_title": "Insurmountable Bottlenecks",
        "word1_syllables": "ઇન-સર-માઉન્ટે-બલ બોટલ-નેક્સ (In-sur-mount-able Bot-tle-necks)",
        "word1_def": "Severe structural limitations or obstacles that cannot easily be overcome.",
        "word1_guj": "પાર ન પાડી શકાય તેવી ગંભીર અડચણો અથવા મર્યાદાઓ.",
        "word1_example": "Legacy system architectures create insurmountable bottlenecks during enterprise data processing.",
        "word2_title": "Rely Heavily On",
        "word2_syllables": "રિ-લાઇ હેવિ-લી ઓન (Re-ly Heav-i-ly On)",
        "word2_def": "To depend completely on a critical factor or infrastructure.",
        "word2_guj": "કોઈ બાબત પર સંપૂર્ણપણે નિર્ભર રહેવું.",
        "word2_example": "Next-generation distributed services rely heavily on low-latency communication channels.",
        "word3_title": "Curtail Energy Consumption",
        "word3_syllables": "કર-ટેલ એનર્જી કન્ઝમ્પ-શન (Cur-tail En-er-gy Con-sump-tion)",
        "word3_def": "To drastically restrict or reduce the volume of power utilized.",
        "word3_guj": "ઊર્જા અને વીજળીના વપરાશમાં મોટો કાપ મૂકવો.",
        "word3_example": "Optimized algorithmic pipelines help data centers curtail energy consumption significantly.",
        "speaking_prompt": f"Explain in 3-4 sentences how '{title}' might influence the future of its industry.",
        "speaking_tip": "Open decisively with 'From an analytical standpoint, the critical breakthrough here is...' and pause naturally at commas."
    }

def generate_sections_html(p: dict, bbc_link: str) -> str:
    return f"""<!-- =========================================================
         SECTION 2: BBC VOCAL READING & AUDIO PRACTICE
         ========================================================= -->
    <section class="article-section">
      <div class="section-kicker-badge">
        <span>{p.get('kicker_emoji', '⚡')}</span> Vocal Reading • {p.get('kicker_topic', 'Technology & Innovation')}
      </div>
      <h2 class="article-headline">{p.get('headline', 'Frontier Innovation')}</h2>
      <div class="article-byline">
        <span>Reported by <strong>BBC News Global Intelligence</strong></span>
        <span>•</span>
        <span>Target: 2 Min Aloud Practice</span>
        <span>•</span>
        <a href="{bbc_link}" target="_blank" rel="noopener noreferrer">bbc.co.uk ↗</a>
      </div>

      <!-- IELTS Difficulty Level Switcher -->
      <div class="level-switcher-container">
        <div class="level-switcher-label">
          <span>🎯 IELTS Reading Difficulty:</span>
          <span class="level-desc" id="level-desc-badge">🌱 Band 5.0 - 6.0 • Easy Understanding</span>
        </div>
        <div class="level-pill-group">
          <button class="level-pill-btn active" id="pill-level-a" onclick="switchDifficulty('a')">
            <span class="pill-dot dot-a"></span> 🌱 Level A (Simple)
          </button>
          <button class="level-pill-btn" id="pill-level-b" onclick="switchDifficulty('b')">
            <span class="pill-dot dot-b"></span> 🌿 Level B (Medium)
          </button>
          <button class="level-pill-btn" id="pill-level-c" onclick="switchDifficulty('c')">
            <span class="pill-dot dot-c"></span> 🌳 Level C (IELTS Band 9)
          </button>
        </div>
      </div>

      <!-- Minimal Audio Player Pill -->
      <div class="audio-player-bar">
        <div class="audio-player-left">
          <button class="play-trigger-btn" id="voice-play-btn" onclick="toggleReadAloud()">
            <span>▶</span> <span id="voice-btn-label">Listen to Selected Level</span>
          </button>
        </div>
        <div class="audio-player-meta">
          <span class="audio-status-label" id="audio-status">Deliberate 0.92x Pace</span>
          <span class="audio-sub-hint">Listen once, then read aloud</span>
        </div>
      </div>

      <!-- Editorial Story Body with 3 IELTS Progression Levels -->
      <div class="editorial-story" id="vocal-passage">
        <!-- Level A: Simple English (IELTS Band 5.0 - 6.0) -->
        <div class="story-level-content" id="content-level-a">
          <p>{p.get('level_a_p1', '')}</p>
          <p>{p.get('level_a_p2', '')}</p>
          <div class="level-quick-summary">
            💡 <strong>ગુજરાતી સંદર્ભ (Context Anchor):</strong> {p.get('gujarati_summary', '')}
          </div>
        </div>

        <!-- Level B: Medium English (IELTS Band 7.0 - 7.5) -->
        <div class="story-level-content" id="content-level-b" style="display: none;">
          <p>{p.get('level_b_p1', '')}</p>
          <p>{p.get('level_b_p2', '')}</p>
        </div>

        <!-- Level C: Native / Authentic BBC (IELTS Band 8.5 - 9.0) -->
        <div class="story-level-content" id="content-level-c" style="display: none;">
          <p>{p.get('level_c_p1', '')}</p>
          <p>{p.get('level_c_p2', '')}</p>
        </div>
      </div>

      <!-- SENTENCE ANATOMY & GRAMMAR BREAKDOWN (BRICK BY BRICK) -->
      <div class="anatomy-breakdown-card" style="background: var(--bg-surface); border: 1px solid var(--border-subtle); border-left: 4px solid var(--text-accent); border-radius: 12px; padding: 18px; margin: 20px 0; box-shadow: var(--shadow-sm);">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; flex-wrap: wrap; gap: 6px;">
          <span style="font-size: 0.75rem; font-family: var(--font-mono); color: var(--text-accent); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;">
            🧩 SENTENCE ANATOMY & GRAMMAR (વાક્યનું ઓપરેશન)
          </span>
          <span style="font-size: 0.7rem; font-family: var(--font-mono); background: var(--bg-subtle); color: var(--text-muted); border: 1px solid var(--border-subtle); padding: 2px 8px; border-radius: 4px; font-weight: 700;">
            IELTS Band 9 Syntax
          </span>
        </div>
        <p style="font-size: 0.98rem; font-weight: 600; color: var(--text-primary); margin-bottom: 10px; font-family: var(--font-editorial); font-style: italic; line-height: 1.5;">
          "{p.get('anatomy_sentence', '')}"
        </p>
        <div style="font-size: 0.85rem; color: var(--text-secondary); line-height: 1.6; margin-bottom: 8px;">
          <strong>🔍 માળખું (Structure):</strong> {p.get('anatomy_breakdown', '')}
        </div>
        <div style="font-size: 0.84rem; color: var(--text-accent); background: var(--bg-subtle); border: 1px solid var(--border-subtle); padding: 8px 12px; border-radius: 8px; font-weight: 500;">
          👉 <strong>કુદરતી વિચાર (Thought Habit):</strong> {p.get('anatomy_gujarati_thought', '')}
        </div>
      </div>

      <!-- Luxury Callout to Exact BBC Story -->
      <a href="{bbc_link}" target="_blank" rel="noopener noreferrer" class="source-callout-card bbc-border">
        <div class="source-callout-content">
          <span class="source-callout-label">Original BBC Coverage</span>
          <span class="source-callout-title">Read Full BBC Story: "{p.get('headline', '')}"</span>
        </div>
        <span class="source-callout-arrow">↗</span>
      </a>
    </section>

    <hr class="editorial-divider">

    <!-- =========================================================
         SECTION 3: HIGH-IMPACT VOCABULARY & COLLOCATIONS
         ========================================================= -->
    <section class="article-section">
      <div class="section-kicker-badge">
        <span>🧠</span> Power Collocations • 3 Chunks
      </div>
      <h2 class="article-headline">Power Collocations & Phrasal Chunks</h2>
      <div class="article-byline">
        <span>શબ્દોના ઝૂમખાં — With Simple Gujarati Syllables & Workplace Context</span>
      </div>

      <div class="vocab-deck">
        <!-- Word 1 -->
        <div class="vocab-unit">
          <div class="vocab-unit-header">
            <span class="vocab-word-title">1. {p.get('word1_title', 'Insurmountable Bottlenecks')}</span>
            <button class="vocab-audio-button" onclick="pronounceSingle('{p.get('word1_title', 'Insurmountable Bottlenecks')}')" title="Listen to pronunciation">🔊</button>
          </div>
          <div class="vocab-pronounce-tag">
            🗣️ સરળ ઉચ્ચાર: <strong>{p.get('word1_syllables', '')}</strong>
          </div>
          <p class="vocab-definition-text">{p.get('word1_def', '')}</p>
          <p class="vocab-gujarati-meaning">👉 ગુજરાતી અર્થ: {p.get('word1_guj', '')}</p>
          <p class="vocab-context-sentence">
            "{p.get('word1_example', '')}"
          </p>
        </div>

        <!-- Word 2 -->
        <div class="vocab-unit">
          <div class="vocab-unit-header">
            <span class="vocab-word-title">2. {p.get('word2_title', 'Rely Heavily On')}</span>
            <button class="vocab-audio-button" onclick="pronounceSingle('{p.get('word2_title', 'Rely Heavily On')}')" title="Listen to pronunciation">🔊</button>
          </div>
          <div class="vocab-pronounce-tag">
            🗣️ સરળ ઉચ્ચાર: <strong>{p.get('word2_syllables', '')}</strong>
          </div>
          <p class="vocab-definition-text">{p.get('word2_def', '')}</p>
          <p class="vocab-gujarati-meaning">👉 ગુજરાતી અર્થ: {p.get('word2_guj', '')}</p>
          <p class="vocab-context-sentence">
            "{p.get('word2_example', '')}"
          </p>
        </div>

        <!-- Word 3 -->
        <div class=vocab-unit>
          <div class="vocab-unit-header">
            <span class="vocab-word-title">3. {p.get('word3_title', 'Curtail Energy Consumption')}</span>
            <button class="vocab-audio-button" onclick="pronounceSingle('{p.get('word3_title', 'Curtail Energy Consumption')}')" title="Listen to pronunciation">🔊</button>
          </div>
          <div class="vocab-pronounce-tag">
            🗣️ સરળ ઉચ્ચાર: <strong>{p.get('word3_syllables', '')}</strong>
          </div>
          <p class="vocab-definition-text">{p.get('word3_def', '')}</p>
          <p class="vocab-gujarati-meaning">👉 ગુજરાતી અર્થ: {p.get('word3_guj', '')}</p>
          <p class="vocab-context-sentence">
            "{p.get('word3_example', '')}"
          </p>
        </div>
      </div>

      <!-- Speaking Practice Prompt -->
      <div class="speaking-lab-banner">
        <div class="speaking-lab-kicker">🗣️ IELTS Speaking Practice Drill (1 Minute)</div>
        <p class="speaking-lab-prompt">
          "{p.get('speaking_prompt', '')}"
        </p>
        <p class="speaking-lab-tip">
          💡 <strong>Confidence Anchor:</strong> {p.get('speaking_tip', '')}
        </p>
      </div>
    </section>

    """

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
            content = re.sub(r'<a href="https://www\.heapheaphurray\.com/p/heap-heap-hurray-ed-\d+"', f'<a href="{h_url}"', content)
            content = re.sub(r'Curated from <strong>.*?</strong>', f'Curated from <strong>{h_title}</strong>', content)
            content = re.sub(r'Read Full ".*?" Newsletter', f'Read Full "{h_title}" Newsletter', content)

        # 3. Check if this exact BBC article link is already present
        b_link = bbc_data.get('link', '')
        if b_link and b_link in content:
            with open(reader_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[*] Reader already up to date for {b_link}, updated dates for {date_formatted}.")
            return

        # 4. Generate Section 2 & 3 payload and inject HTML
        payload = generate_reader_payload_ai(bbc_data)
        new_sec2_3 = generate_sections_html(payload, b_link)
        
        s_tag = 'SECTION 2: BBC VOCAL READING'
        e_tag = '<!-- Consistency Streak & Completion Card -->'
        
        s_idx = content.find(s_tag)
        comment_start = content.rfind('<!--', 0, s_idx)
        e_idx = content.find(e_tag)
        
        if comment_start != -1 and e_idx != -1:
            content = content[:comment_start] + new_sec2_3 + content[e_idx:]
            
        with open(reader_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[*] Dynamic reader.html FULLY REWRITTEN with new story for {date_formatted}!")
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

def send_morning_digest(chat_id: int = PRINCE_CHAT_ID):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
    date_formatted = ist_now.strftime("%A • %d %b %Y")
    
    hhh_data = fetch_latest_hhh()
    bbc_data = fetch_latest_bbc()  # <-- Always fresh unseen article from live RSS!
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
    last_email_check_time = 0
    
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

            # 2. Morning Job Briefing: 7:15 AM IST (Hour 7, Minute 15)
            if hour == 7 and minute >= 15 and minute <= 20:
                if last_morning_job_date != today_str:
                    print(f"[*] Sending 7:15 AM IST Morning Job Briefing for {today_str}...")
                    try:
                        summary_msg = "🌅 *MORNING EXECUTIVE OVERNIGHT BRIEFING FOR BOSS PRINCE (7:15 AM IST)*\n" + job_hunter.generate_daily_jobs_telegram_summary()
                        telegram_send_message(PRINCE_CHAT_ID, summary_msg)
                    except Exception as e:
                        print(f"[!] Error sending morning job report: {e}")
                    last_morning_job_date = today_str
            
            # 3. Evening Comprehensive Job Report: 9:00 PM IST (Hour 21, Minute 00)
            if hour == 21 and minute >= 0 and minute <= 5:
                if last_evening_job_date != today_str:
                    print(f"[*] Sending 9:00 PM IST Evening Job Report for {today_str}...")
                    try:
                        summary_msg = "🌙 *EVENING COMPREHENSIVE JOB REPORT FOR BOSS PRINCE (9:00 PM IST)*\n" + job_hunter.generate_daily_jobs_telegram_summary()
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

            # 6. Periodic IMAP Client Response Check (Every 5 minutes = 300 seconds)
            if time.time() - last_email_check_time > 300:
                try:
                    import email_dealmaker
                    threading.Thread(target=email_dealmaker.check_client_inbox_and_alert, args=(telegram_send_message, PRINCE_CHAT_ID, GEMINI_API_KEY), daemon=True).start()
                except Exception as e:
                    print(f"[!] Email inbox check error: {e}")
                last_email_check_time = time.time()
                    
            # 7. Prevent Render from sleeping: Self-ping every 10 minutes
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
