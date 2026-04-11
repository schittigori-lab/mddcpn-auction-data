# coding: utf-8
"""
MD/DC Public Notices - Foreclosure Scraper
==========================================
Scrapes trustee sale notices for Somerset, Wicomico, Dorchester, Worcester counties
from https://www.mddcpublicnotices.com

Searches "substitute trustees" to target actual auction notices (not court filings
or tax lien actions).  Property address is parsed from the search-result snippet
(which begins with the notice title: "SUBSTITUTE TRUSTEES' SALE OF... [address]").
Detail pages are fetched in a new tab (same browser context = shared cookies).

OUTPUT: mddcpn_auctions.json
RUN:    py mddcpn_scraper.py
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, date

# ── Auto-install dependencies ──────────────────────────────────────────────────
def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

try:
    from playwright.async_api import async_playwright
except ImportError:
    install("playwright")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    from playwright.async_api import async_playwright

try:
    from bs4 import BeautifulSoup
except ImportError:
    install("beautifulsoup4")
    from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
except ImportError:
    install("python-dotenv")
    from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

SEARCH_URL      = 'https://www.mddcpublicnotices.com/Search.aspx'
SEARCH_KEYWORD  = 'substitute trustees'
TARGET_COUNTIES = ['Dorchester', 'Somerset', 'Wicomico', 'Worcester']
# Require "County" after the name to avoid false positives like "Dorchester Road"
COUNTY_PAT      = re.compile(
    r'\b(' + '|'.join(TARGET_COUNTIES) + r')\s+County\b', re.IGNORECASE
)

GITHUB_TOKEN    = os.getenv('GITHUB_TOKEN')
OUTPUT_FILE     = 'mddcpn_auctions.json'

# ── Text parsers ───────────────────────────────────────────────────────────────

MONTH_MAP = {m.lower(): i+1 for i, m in enumerate([
    'January','February','March','April','May','June',
    'July','August','September','October','November','December'])}

def parse_auction_date(text):
    patterns = [
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
        r'\bthe\s+(\d{1,2})(?:st|nd|rd|th)\s+day\s+of\s+(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(\d{4})',
        r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m: continue
        try:
            g = m.groups()
            if '/' in m.group(0):
                month, day, year = int(g[0]), int(g[1]), int(g[2])
            elif g[0].isdigit():
                day = int(g[0]); month = MONTH_MAP.get(g[1].lower(), 0); year = int(g[2])
            else:
                month = MONTH_MAP.get(g[0].lower(), 0); day = int(g[1]); year = int(g[2])
            if month and 1 <= day <= 31 and year >= 2024:
                return f'{year}-{month:02d}-{day:02d}'
        except Exception:
            continue
    return ''

def parse_auction_time(text):
    m = re.search(r'\b(\d{1,2}):(\d{2})\s*(AM|PM|a\.m\.|p\.m\.)', text, re.IGNORECASE)
    if m:
        return f'{m.group(1)}:{m.group(2)} {m.group(3).upper().replace(".","")}'
    m = re.search(r'\b(\d{1,2})\s*o\'clock\s*(AM|PM|a\.m\.|p\.m\.)?', text, re.IGNORECASE)
    if m:
        ampm = (m.group(2) or 'AM').upper().replace('.', '')
        return f'{m.group(1)}:00 {ampm}'
    return ''

def parse_auction_location(text):
    m = re.search(
        r'(?:at|in front of|held at|sold at)\s+(.{10,100}?(?:courthouse|court house|steps|entrance|front door))',
        text, re.IGNORECASE)
    if m: return m.group(0).strip()[:120]
    m = re.search(r'(Circuit Court(?:\s+(?:for|of)\s+[A-Za-z\s]+County)?)', text, re.IGNORECASE)
    if m: return m.group(0).strip()
    return ''

STREET_SUFFIX = re.compile(
    r'\b(Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Court|Ct|'
    r'Boulevard|Blvd|Circle|Cir|Highway|Hwy|Pike|Terrace|Ter|Place|Pl|'
    r'Run|Row|Path|Trail|Trl|Square|Sq)\b', re.IGNORECASE
)

def _is_office_addr(addr):
    """Return True for law firm / office addresses."""
    return bool(re.search(r'\bSuite\b|\bFloor\b|\b#\s*\d|\bSte\.?\b', addr, re.IGNORECASE))

def parse_property_address(text):
    """
    Extract property address from MD trustee-sale / court-confirmation notice snippets.

    Strategy (in priority order):
    1. Defendant's street+city/state block (complete address visible in caption)
    2. "property in this case, [address]" phrase (court confirmation notices)
    3. "KNOWN AS / property at / situated at" patterns
    4. Trustee-sale title: "TRUSTEES' SALE OF ... [address]"
    """
    clean = re.sub(r'[ \t]+', ' ', text)

    # ── 1. Defendant address: street line immediately before "City, MD XXXXX" ──
    # Iterate all matches — first match is often the law firm address (has Suite/Floor)
    for m in re.finditer(
        r'\n([0-9][^\n]{5,60})\n([A-Za-z][^\n]{2,40},\s*MD\s+\d{5})',
        clean, re.IGNORECASE
    ):
        street = m.group(1).strip()
        city_state = m.group(2).strip()
        addr = f'{street}, {city_state}'
        if STREET_SUFFIX.search(street) and not _is_office_addr(addr):
            return re.sub(r'\s+', ' ', addr)[:120]

    # ── 2. "sale of the property in this case, [address]" ──
    # Address may be truncated (no street suffix); include anyway — partial address
    # is better than nothing for the listing (won't geocode but shows in list).
    m = re.search(
        r'property in this case,\s*([0-9][^\n.]{5,100})',
        clean, re.IGNORECASE
    )
    if m:
        addr = re.sub(r'\s+', ' ', m.group(1).strip()).rstrip('., ')
        addr = re.sub(r'\s+\.\.\.\s*(?:click.*)?$', '', addr).strip()
        if not _is_office_addr(addr) and len(addr) > 5:
            return addr[:120]

    # ── 3. Known-as / property-at / situated-at ──
    patterns_3 = [
        r'known as[\s\n]+([0-9][^\n;]{5,100})',
        r"TRUSTEES['\u2019]?\s+SALE\s+OF[^\n]*[\n\s]+([0-9][^\n;]{5,80})",
        r'propert\w*\s+(?:located\s+)?at\s+([0-9][^\n;]{5,80})',
        r'premises\s+(?:known\s+as\s+)?([0-9][^\n;]{5,80})',
        r'situate[d]?\s+(?:at|on|in)\s+([0-9][^\n;]{5,80})',
    ]
    for pat in patterns_3:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            addr = re.sub(r'\s+', ' ', m.group(1).strip())[:120]
            if STREET_SUFFIX.search(addr) and not _is_office_addr(addr) and len(addr) > 10:
                return addr

    return ''

def detect_county(text):
    m = COUNTY_PAT.search(text)
    return f'{m.group(1).title()} County' if m else ''

# ── Page parser ────────────────────────────────────────────────────────────────

def parse_notices_from_page(html):
    """
    Extract (notice_id, short_text, detail_url) tuples from a results page.
    Grabs extra sibling rows to capture more notice text.
    """
    soup    = BeautifulSoup(html, 'html.parser')
    notices = []
    seen    = set()

    for hidden in soup.find_all('input', {'type': 'hidden', 'name': re.compile(r'hdnPKValue')}):
        nid = hidden.get('value', '').strip()
        if not nid or nid in seen:
            continue
        seen.add(nid)

        # Grab text from the row and up to 3 sibling rows (more notice text)
        row  = hidden.find_parent('tr')
        text = ''
        if row:
            text = row.get_text(' ', strip=True)
            sib = row
            for _ in range(3):
                sib = sib.find_next_sibling('tr')
                if sib:
                    text += ' ' + sib.get_text(' ', strip=True)
                else:
                    break

        # Extract stable detail URL (strip session ID — it expires)
        detail_url = ''
        if row:
            btn = row.find('input', onclick=re.compile(r'Details\.aspx'))
            if btn:
                m = re.search(r'[?&]ID=(\d+)', btn.get('onclick', ''))
                if m:
                    detail_url = f'Details.aspx?ID={m.group(1)}'

        notices.append((nid, text, detail_url))

    return notices

def get_total_pages(html):
    soup = BeautifulSoup(html, 'html.parser')
    lbl  = soup.find(id=re.compile(r'lblTotalPages'))
    if lbl:
        m = re.search(r'of\s+(\d+)', lbl.get_text())
        if m:
            return int(m.group(1))
    return 1

# ── Main scraper ───────────────────────────────────────────────────────────────

async def scrape():
    auctions = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        print('Loading search page...')
        await page.goto(SEARCH_URL, wait_until='networkidle', timeout=30000)

        print(f'Setting search criteria (keyword="{SEARCH_KEYWORD}", last 60 days)...')
        await page.evaluate(f'''() => {{
            document.querySelector('input[value="rbLastNumDays"]').checked = true;
            document.getElementById('ctl00_ContentPlaceHolder1_as1_txtLastNumDays').value = '60';
            document.getElementById('ctl00_ContentPlaceHolder1_as1_txtSearch').value = {json.dumps(SEARCH_KEYWORD)};
        }}''')

        print('Submitting...')
        await page.evaluate(
            'document.querySelector(\'input[name="ctl00$ContentPlaceHolder1$as1$btnGo"]\').click()'
        )
        await page.wait_for_load_state('networkidle', timeout=30000)
        await page.wait_for_timeout(2000)

        print('Setting 50 results per page...')
        await page.select_option(
            '#ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1_ctl01_ddlPerPage', '50'
        )
        await page.wait_for_load_state('networkidle', timeout=20000)
        await page.wait_for_timeout(1500)

        # Iterate pages, collect matching notices
        all_notices = {}   # nid -> (short_text, detail_url)
        page_num    = 1
        total_pages = get_total_pages(await page.content())
        base_url    = SEARCH_URL.rsplit('/', 1)[0]
        print(f'Total pages: {total_pages}')

        while True:
            html    = await page.content()
            notices = parse_notices_from_page(html)
            matched = 0
            for nid, text, detail_url in notices:
                if COUNTY_PAT.search(text):
                    all_notices[nid] = (text, detail_url)
                    matched += 1
            print(f'Page {page_num}/{total_pages}: {len(notices)} notices, {matched} county matches')

            if page_num >= total_pages:
                break

            next_btn = page.locator(
                '#ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1_ctl01_btnNext'
            )
            if await next_btn.count() == 0 or not await next_btn.is_enabled():
                print('Next button not available — stopping')
                break

            await next_btn.click()
            await page.wait_for_load_state('networkidle', timeout=20000)
            await page.wait_for_timeout(1000)
            page_num += 1

        print(f'\nTotal county matches: {len(all_notices)}')

        # Deduplicate by court case number — same case published in multiple newspapers
        # MD case format: C-09-CV-25-000340 (letter, dash, county, dash, type, dash, year, dash, seq)
        def parse_case_number(text):
            m = re.search(
                r'(?:Case|Civil)\s+No\.?\s*:?\s*([A-Z]\d*-\d{2}-[A-Z]{2}-\d{2}-\d{6})',
                text, re.IGNORECASE
            )
            return m.group(1).upper() if m else None

        # Pick one representative notice per unique case number (prefer complete address)
        unique_cases = {}  # case_num -> (nid, short_text, detail_path)
        no_case_num  = {}  # nid -> (short_text, detail_path) for notices without case number
        for nid, (short_text, detail_path) in all_notices.items():
            case_num = parse_case_number(short_text)
            if case_num:
                existing = unique_cases.get(case_num)
                if existing is None:
                    unique_cases[case_num] = (nid, short_text, detail_path)
                # else: keep first occurrence (all publications have same text)
            else:
                no_case_num[nid] = (short_text, detail_path)

        deduped = {nid: (txt, dp) for nid, txt, dp in unique_cases.values()}
        deduped.update(no_case_num)
        print(f'After dedup: {len(deduped)} unique notices ({len(all_notices) - len(deduped)} duplicates removed)')

        # Process each deduplicated notice (detail pages blocked by reCAPTCHA — use snippet only)
        for i, (nid, (short_text, detail_path)) in enumerate(deduped.items(), 1):
            if detail_path:
                detail_url = f'{base_url}/{detail_path}' if not detail_path.startswith('http') else detail_path
            else:
                detail_url = f'{base_url}/Details.aspx?ID={nid}'

            county   = detect_county(short_text)
            address  = parse_property_address(short_text)
            auc_date = parse_auction_date(short_text)
            auc_time = parse_auction_time(short_text)
            auc_loc  = re.sub(r'\s+', ' ', parse_auction_location(short_text)).strip()

            if not address:
                print(f'  [{i}/{len(deduped)}] SKIP ID={nid} — no address found')
                continue

            # If address is truncated (no city/state), append county for geocoding context
            if county and not re.search(r',\s*[A-Z]{2}(?:\s+\d{5})?\s*$', address):
                address = f'{address}, {county}, MD'

            case_num  = parse_case_number(short_text)
            record_id = f'mddcpn-{case_num.replace("-", "").lower()}' if case_num else f'mddcpn-{nid}'

            auctions.append({
                'id':               record_id,
                'property_address': address,
                'auction_date':     auc_date,
                'auction_time':     auc_time,
                'auction_location': auc_loc,
                'bid_deposit':      '',
                'opening_bid':      '',
                'county':           county,
                'state':            'MD',
                'source':           'MDDCPN',
                'status':           'active',
                'detail_url':       detail_url,
            })
            print(f'  [{i}/{len(deduped)}] OK: {address[:60]} | {county} | {auc_date}')

        await browser.close()

    return auctions

# ── GitHub push ────────────────────────────────────────────────────────────────

def push_to_github(auctions):
    today   = date.today().isoformat()
    payload = {'last_updated': datetime.now().isoformat() + 'Z', 'auctions': auctions}
    content = json.dumps(payload, indent=2, ensure_ascii=False)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'\nWrote {OUTPUT_FILE} ({len(auctions)} listings)')

    archive_path = f'archive/{today}.json'
    os.makedirs('archive', exist_ok=True)
    with open(archive_path, 'w', encoding='utf-8') as f:
        f.write(content)

    if not GITHUB_TOKEN:
        print('No GITHUB_TOKEN — skipping push')
        return

    try:
        subprocess.check_call(['git', 'pull', '--rebase', 'origin', 'main'])
        subprocess.check_call(['git', 'add', OUTPUT_FILE, archive_path])
        subprocess.check_call(['git', 'commit', '-m', f'Auction update {today}'])
        subprocess.check_call(['git', 'push'])
        print('Pushed to GitHub')
    except subprocess.CalledProcessError as e:
        print(f'Git error: {e}')

# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    auctions = asyncio.run(scrape())
    push_to_github(auctions)
    print('Done.')
