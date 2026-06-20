#!/usr/bin/env python3
"""
Football News Crawler & Metadata Generator
=========================================
This script grabs the first 40 articles from the Guardian Football API (with fallbacks to 
BBC Sport & Sky Sports RSS Feeds) and scrapes their full content.

Specific capabilities:
1. Gathers: title, published date, author (byline), main thumbnail, and URL.
2. Checks Firestore to see if the article has already been saved. Existing articles are skipped.
3. If no new articles are found, the crawler immediately STOPS operation before scraping.
4. Follows each new article URL to scrape the page's body text paragraphs, author, and categories.
5. Intelligently SKIPS the first image on the webpage (repeating header logo/banner logo) and fetches 
   subsequent inline content images.
6. Rotates through a pool of up to 8 Gemini API Keys. If one fails (e.g., due to billing limits or quotas),
   it retries 3 times and automatically rotates to the next available API key.
7. Compiles text content, category list/tags, and inline images into custom Firestore payloads.
"""

import os
import re
import sys
import time
from datetime import datetime
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup beautiful console logging with colors
class Col:
    GREEN = "\033[92m"
    BLUE = "\033[94m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

def log_success(msg):
    print(f"{Col.GREEN}{Col.BOLD}[+] SUCCESS:{Col.RESET} {msg}")

def log_info(msg):
    print(f"{Col.BLUE}{Col.BOLD}[!] INFO:{Col.RESET} {msg}")

def log_warn(msg):
    print(f"{Col.YELLOW}{Col.BOLD}[?] WARNING:{Col.RESET} {msg}")

def log_error(msg):
    print(f"{Col.RED}{Col.BOLD}[x] ERROR:{Col.RESET} {msg}")


# Gracefully import required packages with detailed setup guides if they are missing
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    log_error("Missing scraping dependencies. Please install them using:")
    print("    pip install requests beautifulsoup4")
    sys.exit(1)

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    log_warn("firebase-admin is not installed. To save articles directly to Firestore, please install it:")
    print("    pip install firebase-admin")
    log_info("We will run the crawler and print the compiled article metadata to the console.")
    firebase_admin = None


# -------------------------------------------------------------
# RETREIVE & ROTATE POOL OF 8 GEMINI API KEYS
# -------------------------------------------------------------
def load_rotating_gemini_keys():
    """
    Looks up to 8 GEMINI API keys from the environment or .env file.
    """
    keys = []
    
    # 1. Primary standard environment variables
    env_varnames = [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4",
        "GEMINI_API_KEY_5",
        "GEMINI_API_KEY_6",
        "GEMINI_API_KEY_7",
        "GEMINI_API_KEY_8",
    ]
    for varname in env_varnames:
        val = os.environ.get(varname)
        if val and val.strip() and val not in keys:
            keys.append(val.strip())
            
    # 2. Supplementary check for .env variables (reading manually)
    if os.path.exists(".env"):
        try:
            with open(".env", "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k in env_varnames:
                        if v and v not in keys:
                            keys.append(v)
        except Exception:
            pass

    # Ensure uniqueness and strip strings
    keys = [k for k in keys if k]
    return keys

# Preload available key pool
GEMINI_KEY_POOL = load_rotating_gemini_keys()
current_key_idx = 0
key_lock = threading.Lock()

if GEMINI_KEY_POOL:
    log_success(f"Configured rotation pool with {len(GEMINI_KEY_POOL)} Gemini API keys.")
else:
    log_warn("No Gemini API keys detected in your environment or .env file.")
    log_info("The script will compile articles using standard high-precision Markdown template fallbacks instead.")


# -------------------------------------------------------------
# 1. INIT FIRESTORE CONNECTION
# -------------------------------------------------------------
db = None
if firebase_admin:
    cred = None
    cred_sources = [
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        "service-account.json",
        "firebase-credentials.json"
    ]
    
    for src in cred_sources:
        if src and os.path.exists(src):
            try:
                cred = credentials.Certificate(src)
                log_success(f"Loaded Firebase credentials from: {src}")
                break
            except Exception as e:
                log_error(f"Failed to parse credentials from {src}: {e}")

    try:
        if cred:
            firebase_admin.initialize_app(cred)
            db = firestore.client()
        else:
            # Fallback to Application Default Credentials
            firebase_admin.initialize_app()
            db = firestore.client()
            log_success("Initialized Firestore using Application Default Credentials.")
    except Exception as e:
        log_warn("Firestore could not be initialized automatically.")
        print("    Note: To write to Firestore, place your Service Account credentials file ")
        print("    as 'service-account.json' in this directory or run with default env variables.")
        print(f"    Error detail: {e}")
        db = None


# -------------------------------------------------------------
# 2. SOURCE API AND RSS NEWS GATHERING
# -------------------------------------------------------------
GUARDIAN_API_KEY = "3c4a4b66-7db0-4bff-a86a-9d66187ed246"
DEFAULT_THUMBNAIL = "https://images.unsplash.com/photo-1508098682722-e99c43a406b2?w=600&auto=format&fit=crop&q=60"

def strip_html_tags(text):
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', '', text)
    return " ".join(clean.split()).strip()

def parse_rss_feed(feed_url, source_name):
    """
    Parses RSS feeds (e.g. BBC Sport, Sky Sports) manually for football articles.
    """
    articles = []
    try:
        response = requests.get(feed_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if response.status_code != 200:
            return []
        
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')
        
        for item in items:
            title = item.find('title')
            title_text = title.text.strip() if title else "Football News Update"
            
            desc = item.find('description')
            desc_text = strip_html_tags(desc.text) if desc else ""
            
            link = item.find('link')
            link_text = link.text.strip() if link else "#"
            
            pub_date = item.find('pubDate')
            date_text = pub_date.text.strip() if pub_date else datetime.utcnow().isoformat()
            
            # Author Parsing from RSS creator tags
            author_tag = item.find('dc:creator') or item.find('creator') or item.find('author')
            author_text = author_tag.text.strip() if author_tag else "Sports Desk"
            
            # Look for thumbnails/enclosures for main images
            img_url = DEFAULT_THUMBNAIL
            enclosure = item.find('enclosure')
            if enclosure and enclosure.get('url'):
                img_url = enclosure.get('url')
            else:
                media_content = item.find('media:content') or item.find('content')
                if media_content and media_content.get('url'):
                    img_url = media_content.get('url')
                else:
                    media_thumbnail = item.find('media:thumbnail') or item.find('thumbnail')
                    if media_thumbnail and media_thumbnail.get('url'):
                        img_url = media_thumbnail.get('url')

            articles.append({
                "title": title_text,
                "description": desc_text,
                "link": link_text,
                "date": date_text,
                "image": img_url,
                "author": author_text,
                "source": source_name,
                "api_categories": ["Football", source_name]
            })
    except Exception as e:
        log_warn(f"Failed to parse RSS feed from {source_name}: {e}")
    return articles

def fetch_top_stories_list():
    """
    Fetches the base listing of news up to 40 items.
    """
    log_info("Fetching the first 40 football news articles from live networks...")
    news_list = []
    
    # 1. Try The Guardian API
    try:
        url = f"https://content.guardianapis.com/search?q=football+OR+soccer&api-key={GUARDIAN_API_KEY}&show-fields=thumbnail,trailText,byline&page-size=40&order-by=newest"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            results = data.get("response", {}).get("results", [])
            for art in results:
                fields = art.get("fields", {})
                pub_date = art.get("webPublicationDate", datetime.utcnow().isoformat())
                
                # Fetching author/byline beautifully
                author = fields.get("byline", "The Guardian Sports Desk")
                
                # Gather categories
                categories = ["Football"]
                sec_name = art.get('sectionName')
                if sec_name and sec_name not in categories:
                    categories.append(sec_name)
                
                news_list.append({
                    "title": art.get("webTitle", "Football Update"),
                    "description": strip_html_tags(fields.get("trailText", "Click to read full coverage on this football update.")),
                    "link": art.get("webUrl", "#"),
                    "date": pub_date,
                    "image": fields.get("thumbnail", DEFAULT_THUMBNAIL),
                    "author": author,
                    "source": f"The Guardian ({sec_name})" if sec_name else "The Guardian",
                    "api_categories": categories
                })
            log_success(f"Retrieved {len(news_list)} articles from Guardian API.")
    except Exception as e:
        log_warn(f"Guardian API failed: {e}")
        
    # 2. Fallbacks if needed (if less than 40 or API was down)
    if len(news_list) < 40:
        log_info("Guardian result less than 40. Running BBC Football RSS scanner feeds...")
        bbc_news = parse_rss_feed("https://feeds.bbci.co.uk/sport/football/rss.xml", "BBC Sport")
        news_list.extend(bbc_news)
        
    if len(news_list) < 40:
        log_info("BBC Feed completed. Running Sky Sports RSS feeds to complete quota...")
        sky_news = parse_rss_feed("https://www.skysports.com/rss/12040", "Sky Sports")
        news_list.extend(sky_news)

    # Clean and parse publication dates to sort news chronologically descending (newest first)
    def parse_item_date(item):
        d_str = item.get("date", "")
        if not d_str:
            return datetime.min
        try:
            clean_str = d_str
            if clean_str.endswith('Z'):
                clean_str = clean_str[:-1] + '+00:00'
            dt = datetime.fromisoformat(clean_str)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        except Exception:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(d_str)
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                return dt
            except Exception:
                return datetime.min

    news_list.sort(key=parse_item_date, reverse=True)

    # 3. Trim to exactly 40 or generate custom mock fillups if sources are entirely offline
    if len(news_list) > 40:
        news_list = news_list[:40]
    elif len(news_list) < 40:
        missing_count = 40 - len(news_list)
        log_warn(f"Crawl feeds only returned {len(news_list)} items. Creating {missing_count} placeholder bulletins to meet the 40 limit.")
        for i in range(missing_count):
            news_list.append({
                "title": f"Global Football Round-Up Series #{i + 1}",
                "description": "Comprehensive league updates, expert commentary, technical analytics, and transfer insights from leagues around the globe.",
                "link": "#",
                "date": datetime.utcnow().isoformat(),
                "image": DEFAULT_THUMBNAIL,
                "author": "Sports Desk Editor",
                "source": "Football Intelligence Feed",
                "api_categories": ["Football", "Match Analysis"]
            })
            
    return news_list


# -------------------------------------------------------------
# 3. WEB PAGE SCRAPING & CONTENT EXTRACTION (WITH CATEGORIES)
# -------------------------------------------------------------
def scrape_article_body_and_media(url, default_categories=[]):
    """
    Crawls the dynamic article url, parses body paragraphs, 
    extracts contextual images while skipping the first banner image,
    and extracts category tags professionally.
    """
    scraped_paragraphs = []
    scraped_images = []
    scraped_categories = list(default_categories)
    
    if not url or url == "#" or not url.startswith("http"):
        return scraped_paragraphs, scraped_images, scraped_categories

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=6)
        if response.status_code != 200:
            return scraped_paragraphs, scraped_images, scraped_categories
            
        soup = BeautifulSoup(response.content, 'html.parser')

        # Clean non-editorial tags and elements 
        for tag in soup(["script", "style", "iframe", "nav", "footer", "header", "noscript", "form", "svg", "button", "fieldset", "aside"]):
            tag.decompose()

        # Remove common ads and newsletter widgets to stay focused purely on content
        for promo in soup.find_all(class_=re.compile(r"sidebar|ad-container|ads|comments|newsletter-signup|newsletter-promo|promo-newsletter")):
            promo.decompose()
        for promo in soup.find_all(id=re.compile(r"sidebar|ad-container|ads|comments|newsletter-signup|newsletter-promo|promo-newsletter")):
            promo.decompose()

        # Extract paragraphs (applying heuristics to avoid side promos, share blocks, tags, copyright notifications)
        potential_paras = soup.find_all('p')
        for p in potential_paras:
            text = " ".join(p.get_text().split()).strip()
            if len(text) > 30:
                is_boilerplate = len(text) < 130 and re.search(r"cookie|subscribe|terms of|privacy policy|copyright|rights reserved|newsletter|sign up|download the app|disable ad|ad blocker|javascript", text, re.I)
                if not is_boilerplate:
                    read_more_match = re.match(r"^\s*(read more|more on this story|related stories|recommended reads|you might also like|trending|latest news|associated press)\b", text, re.I)
                    if not read_more_match and text not in scraped_paragraphs:
                        scraped_paragraphs.append(text)

        # Handle image scanning, resolving relative links to absolute URLs
        all_imgs = soup.find_all('img')
        seen_valid_images_count = 0
        
        for img_el in all_imgs:
            src = img_el.get('src') or img_el.get('data-src') or img_el.get('data-srcset') or img_el.get('srcset') or img_el.get('data-original')
            if not src:
                continue

            if "," in src:
                parts = src.split(",")
                first_part = parts[0].strip().split()[0]
                if first_part:
                    src = first_part

            try:
                if src.startswith("//"):
                    src = f"https:{src}"
                elif src.startswith("/"):
                    parsed_base = requests.utils.urlparse(url)
                    src = f"{parsed_base.scheme}://{parsed_base.netloc}{src}"
                elif not src.startswith("http"):
                    src = requests.compat.urljoin(url, src)
            except Exception:
                pass

            if src and src.startswith("http"):
                lower_src = src.lower()
                is_noise = any(kw in lower_src for kw in ["avatar", "logo", "icon", "/ad/", "advert", "pixel", "tracker", "sprite", "button", "widget", "facebook", "twitter", "header", "footer"]) or lower_src.endswith(".gif") or lower_src.endswith(".svg")
                
                if not is_noise:
                    alt_text = img_el.get('alt') or img_el.get('title') or ""
                    clean_alt = " ".join(alt_text.split()).strip()
                    lower_alt = clean_alt.lower()
                    
                    invalid_alt = any(kw in lower_alt for kw in ["advertisement", "click here", "logo", "sidebar", "sign up", "icon"]) or len(lower_alt) > 200
                    
                    if not invalid_alt:
                        # Ensure uniqueness
                        if not any(img["url"] == src for img in scraped_images):
                            seen_valid_images_count += 1
                            if seen_valid_images_count == 1:
                                # SKIP the first valid image on the page (usually repeating header/banner image)
                                continue
                            scraped_images.append({
                                "url": src,
                                "alt": clean_alt or "Match Coverage Incident"
                            })

        # --- Professional Category & Tags Scraper ---
        # 1. Search meta tags for news segments or section names
        for meta in soup.find_all('meta'):
            name = meta.get('name', '').lower()
            prop = meta.get('property', '').lower()
            content = meta.get('content', '').strip()
            
            if not content:
                continue
                
            if name in ['category', 'keywords', 'news_keywords', 'section'] or prop in ['article:section', 'article:tag', 'og:section']:
                vals = [v.strip() for v in re.split(r'[;,|]', content)]
                for val in vals:
                    # Clean tags up
                    if val and len(val) > 2 and len(val) < 25:
                        title_val = val.title()
                        if title_val not in scraped_categories:
                            scraped_categories.append(title_val)

        # 2. Heuristics based on text & title corpus properties
        full_text_snapshot = " ".join(scraped_paragraphs[:5])
        corpus = f"{url} {full_text_snapshot}".lower()
        
        football_keyword_map = {
            "transfer": ["Transfers", "Transfer Rumors"],
            "premier league": ["Premier League", "EPL"],
            "champions league": ["Champions League", "UCL"],
            "europa league": ["Europa League", "UEL"],
            "arsenal": ["Arsenal"],
            "chelsea": ["Chelsea"],
            "liverpool": ["Liverpool"],
            "manchester united": ["Manchester United", "Man Utd"],
            "manchester city": ["Manchester City", "Man City"],
            "real madrid": ["Real Madrid", "La Liga"],
            "barcelona": ["Barcelona", "La Liga"],
            "serie a": ["Serie A", "Italian Football"],
            "la liga": ["La Liga", "Spanish Football"],
            "bundesliga": ["Bundesliga", "German Football"],
            "scotland": ["Scottish Premiership"],
            "world cup": ["World Cup", "International"],
            "euros": ["European Championships"],
            "injury": ["Injuries"],
            "tactics": ["Tactical Analysis"],
            "scouting": ["Scouting Reports"],
        }
        
        for keyword, tags in football_keyword_map.items():
            if keyword in corpus:
                for tag in tags:
                    if tag not in scraped_categories:
                        scraped_categories.append(tag)

        # Exclude blacklisted strings from categories
        blacklist = {"News", "Sports", "Sport", "Football", "Soccer", "Article", "Video", "Broadcast", "Web", "Feed", "Live", "Latest"}
        scraped_categories = [c for c in scraped_categories if c not in blacklist]
        
        # Bring back Football at the front
        scraped_categories.insert(0, "Football")
        
        # De-duplicate while maintaining ordering
        seen = set()
        scraped_categories = [x for x in scraped_categories if not (x in seen or seen.add(x))]
        
        # Trim to top 5 categories
        scraped_categories = scraped_categories[:5]

    except Exception as e:
         log_warn(f"Failed to scrape webpage body content at {url}: {e}")

    return scraped_paragraphs, scraped_images, scraped_categories


# -------------------------------------------------------------
# 4. STORY COMPILATION WITH ROTATING GEMINI API KEYS
# -------------------------------------------------------------
def compile_story_to_markdown(title, description, paragraphs, scraped_images):
    """
    Compiles article body slices + inline image placements beautifully into 
    clean Markdown layout. Uses the pool of up to 8 Gemini Keys with rotating safeguards.
    """
    global current_key_idx
    
    if GEMINI_KEY_POOL:
        raw_text = "\n\n".join(paragraphs[:35])  # Limit input text length
        limit_images = scraped_images[:5]
        
        images_prompt = ""
        if limit_images:
            images_prompt = f"\nHere is a list of actual parsed images from this news webpage. You MUST place them naturally between paragraphs in the Markdown article at appropriate spots (e.g. before/after paragraphs that correspond, or as natural visual transitions) using standard Markdown image tags:\n"
            for i, img in enumerate(limit_images):
                images_prompt += f'{i+1}. URL: "{img["url"]}" (Description: "{img["alt"]}")\n'
            images_prompt += "\nRule: Only use the exact image URLs provided. If an image is relevant to a specific team or event discussed, place that image directly next to it!"

        prompt = f"""You are a talented sports reporter. Your task is to clean up and format the following raw article content into a cohesive, naturally written sports news story.

Original Article Title: "{title}"
Original Article Description: "{description}"

Parsed source content:
\"\"\"
{raw_text}
\"\"\"
{images_prompt}

Instructions:
1. Write a natural, direct, and engaging sports news article. Keep the natural pace of live human sports reporting.
2. Keep ALL factual details strictly intact: Transfer numbers, scores, player names, and direct quotes must remain completely accurate.
3. Use Markdown formatting naturally:
   - Use headings (##) ONLY when there is a natural transition or topic shift.
   - Insert the provided image URLs as markdown tags at matching spots.

Respond ONLY with the clean markdown text representing the article. Do not wrap the output in a ```markdown code block or JSON."""

        # Try key rotation loop
        while True:
            with key_lock:
                if current_key_idx >= len(GEMINI_KEY_POOL):
                    break
                active_key = GEMINI_KEY_POOL[current_key_idx]
                key_num = current_key_idx + 1

            key_preview = f"...{active_key[-6:]}" if len(active_key) > 6 else "Key"
            log_info(f"Using Gemini API Key #{key_num} ({key_preview}) to format Markdown story.")
            
            # Setup dynamic credentials configuration
            try:
                from google import genai
                client = genai.Client(api_key=active_key)
            except ImportError:
                log_warn("google-genai client library missing. Skipping AI formatting.")
                break

            # Run 3 retries for this single key
            for attempt in range(1, 4):
                try:
                    response = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                    )
                    if response and response.text:
                        text_out = response.text.strip()
                        # Strip code fences cleanly
                        if text_out.startswith("```markdown"):
                            text_out = re.sub(r"^```markdown\n", "", text_out)
                            text_out = re.sub(r"\n```$", "", text_out)
                        elif text_out.startswith("```"):
                            text_out = re.sub(r"^```\n", "", text_out)
                            text_out = re.sub(r"\n```$", "", text_out)
                        return text_out
                except Exception as e:
                    log_warn(f"Retry {attempt}/3 failed using Gemini Key #{key_num}: {e}")
                    time.sleep(1.0)
            
            # Since 3 retries failed for this key, rotate immediately to the next API key
            with key_lock:
                if current_key_idx < key_num:
                    log_error(f"Gemini API Key #{key_num} exhausted or billing-blocked. Rotating keys...")
                    current_key_idx += 1

        log_warn("All available Gemini API keys exhausted. Running automatic local template compiler instead.")

    # High-quality fallback template formatter
    markdown = f"## {title}\n\n"
    if description:
        markdown += f"> *{description}*\n\n---\n\n"
    
    for idx, p in enumerate(paragraphs[:20]):
        if idx == 0:
            markdown += f"**{p}**\n\n"
        elif len(p) < 150 and ('"' in p or '“' in p):
            markdown += f"> *{p}*\n\n"
        else:
            markdown += f"{p}\n\n"
            
        # Staggered inline image placement
        img_idx = idx
        if img_idx < len(scraped_images) and img_idx < 4:
            markdown += f"![{scraped_images[img_idx]['alt']}]({scraped_images[img_idx]['url']})\n\n"

    markdown += "\n\n---\n*Disclaimer: Autocompiled from sport news live streams.*"
    return markdown


# -------------------------------------------------------------
# 5. CORE EXECUTOR Loop (WITH SKIP AND STOP CONDITIONS)
# -------------------------------------------------------------
def run_crawler():
    # Gather news items from feeds
    news_items = fetch_top_stories_list()
    
    if not news_items:
        log_warn("No candidate news stories could be retrieved. Shutdown.")
        return

    # Filter out duplicate articles that have already been saved in Firestore
    new_indices = []
    
    if db:
        log_info("Inspecting Firestore database for saving statuses (checks titles & duplicates)...")
        for index, item in enumerate(news_items):
            # Same title sanitization block used below for Document ID matching
            doc_safe_title = re.sub(r'[^a-zA-Z0-9-]', '_', item['title'].lower()[:60])
            try:
                doc_ref = db.collection("football_articles").document(doc_safe_title)
                if doc_ref.get().exists:
                    log_info(f"Skipping: [SAVED] '{item['title']}' matches existing firestore key.")
                else:
                    new_indices.append(index)
            except Exception as e:
                # If lookup fails on Firestore, keep in pipeline as safety default
                log_warn(f"Firestore presence check failed for '{doc_safe_title}': {e}. Keeping in list.")
                new_indices.append(index)
    else:
        log_warn("Firestore connection is offline. Processing first run locally for display preview.")
        new_indices = list(range(len(news_items)))

    # SPECIFIC STOP CONDITION: Stop operation immediately if no new ones are detected 
    if len(new_indices) == 0:
        log_success("All retrieved articles are already saved in your database! NO NEW ARTICLES detected.")
        log_info("Crawler operation has safely stopped. No network bandwidth or Gemini quota was spent.")
        return

    log_success(f"Deduplication complete. Found {len(new_indices)} brand-new articles out of the 40 items.")
    print("="*80)
    
    scraped_count = 0
    saved_count = 0
    total = len(new_indices)

    def process_article(idx, count):
        item = news_items[idx]
        ref_num = count + 1
        
        # Scrape full text text-paragraphs, inline webpage images (excluding banner), and professional category/tags
        paragraphs, body_images, categories = scrape_article_body_and_media(item['link'], item.get('api_categories', []))
        
        scraped_success = len(paragraphs) > 0
        if scraped_success:
            log_success(f"[{ref_num}/{total}] Scraped {len(paragraphs)} paragraphs, {len(body_images)} inline body images, and categories: {categories} for: {item['title']}")
        else:
            log_warn(f"[{ref_num}/{total}] Web crawl did not retrieve body text paragraphs for: {item['title']}. Setting up description summary as fallback.")
            paragraphs = [item['description']] if item['description'] else ["No text context fetched."]
        
        # Format whole content inside compiled markdown with Key Rotation active
        compiled_markdown = compile_story_to_markdown(
            title=item['title'],
            description=item['description'],
            paragraphs=paragraphs,
            scraped_images=body_images
        )
        
        # Structure beautiful, highly professional football article entry metadata payload
        article_metadata = {
            "title": item['title'],
            "description": item['description'],
            "url": item['link'],
            "publishedDate": item['date'],
            "author": item['author'],
            "source": item['source'],
            "mainImage": item['image'],
            "scrapedParagraphs": paragraphs,
            "images": body_images,
            "categories": categories,
            "compiledMarkdown": compiled_markdown,
            "scrapedAt": datetime.utcnow().isoformat(),
            "status": "processed" if scraped_success else "fallback"
        }
        
        # Save directly to database
        if db:
            try:
                doc_safe_title = re.sub(r'[^a-zA-Z0-9-]', '_', item['title'].lower()[:60])
                doc_ref = db.collection("football_articles").document(doc_safe_title)
                doc_ref.set(article_metadata)
                log_success(f"[{ref_num}/{total}] Article successfully saved to Firestore path: /football_articles/{doc_safe_title}")
                return True, scraped_success
            except Exception as e:
                log_error(f"Failed to submit parsed article payload to Firestore for '{item['title']}': {e}")
                return False, scraped_success
        else:
            # Print elegant CLI preview if database credential is not configured
            preview_subset = {
                "title": article_metadata["title"],
                "author": article_metadata["author"],
                "mainImage": article_metadata["mainImage"],
                "categories": article_metadata["categories"],
                "paragraphs_count": len(article_metadata["scrapedParagraphs"]),
                "scraped_images_count": len(article_metadata["images"]),
            }
            log_info(f"Firestore offline. Preview for {item['title']}:\n{json.dumps(preview_subset, indent=2)}")
            return True, scraped_success

    # Run the worker threads concurrently up to 8 threads
    with ThreadPoolExecutor(max_workers=min(8, total)) as executor:
        futures = {executor.submit(process_article, idx, count): idx for count, idx in enumerate(new_indices)}
        for future in as_completed(futures):
            try:
                is_saved, scraped_success = future.result()
                if is_saved:
                    saved_count += 1
                if scraped_success:
                    scraped_count += 1
            except Exception as e:
                log_error(f"Failed to execute parallel article task for index {futures[future]}: {e}")

    print("\n" + "="*80)
    log_success("CRAWLER JOB SUMMARY INVOCATION COMPLETED SUCCESSFULLY!")
    print(f"Total News Pool Scanned: 40 articles matching key phrases.")
    print(f"Successfully processed & formatted: {scraped_count} brand-new articles.")
    if db:
        log_success(f"Committed to Firestore production database path: {saved_count} articles.")
    print("="*80)

if __name__ == "__main__":
    run_crawler()
