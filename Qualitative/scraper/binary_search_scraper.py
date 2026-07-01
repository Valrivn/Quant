#!/usr/bin/env python3
"""
Binary search approach to identify and fix anti-bot bypass issues.
"""
import asyncio
import time
from pathlib import Path

WORKSPACE = Path('/Users/hayden/Desktop/quant-py')

async def test_proxy_sources():
    """Test proxy extraction from different sources"""
    from curl_cffi import AsyncSession
    from bs4 import BeautifulSoup
    from psychological.scrapers.proxy_manager import JA4_HEADERS
    
    proxy_sources = [
        "https://free-proxy-list.net/",
        "https://www.sslproxies.org/",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    ]
    
    for source in proxy_sources:
        print(f"\nTesting source: {source}")
        try:
            async with AsyncSession(impersonate="chrome120", timeout=10) as session:
                resp = await session.get(source, headers=JA4_HEADERS, timeout=10)
                print(f"  HTTP status: {resp.status}")
                if resp.status == 200:
                    text = resp.text
                    if '<table' in text or '<tr' in text or '<td' in text or '<th' in text:
                        soup = BeautifulSoup(text, "html.parser")
                        proxies = []
                        for row in soup.select("table.table tbody tr, table#proxylisttable tbody tr, table tbody tr"):
                            cells = row.find_all("td")
                            if len(cells) >= 2:
                                ip = cells[0].get_text(strip=True)
                                port = cells[1].get_text(strip=True)
                                if ip and port and ip.replace('.', '').isdigit() and 1 <= int(port) <= 65535:
                                    proxies.append(f"{ip}:{port}")
                        print(f"  Extracted {len(proxies)} proxies")
                        for p in proxies[:3]:
                            print(f"    {p}")
        except Exception as e:
            print(f"  Failed: {e}")

async def test_bing_search():
    """Test direct Bing search approach for Glassdoor data"""
    print("\n" + "="*60)
    print("TESTING BING SEARCH APPROACH")
    print("="*60)
    
    import asyncio
    from curl_cffi import AsyncSession
    from bs4 import BeautifulSoup
    from psychological.scrapers.proxy_manager import JA4_HEADERS, build_ja4_headers
    
    async def find_glassdoor_data():
        try:
            query = "site:glassdoor.com \"NVIDIA\" reviews rating CEO approval 2025"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
            
            headers = JA4_HEADERS
            headers.update({
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            
            timeout = aiohttp.ClientTimeout(total=45)
            async with AsyncSession(impersonate="chrome120", timeout=timeout) as session:
                resp = await session.get(url, headers=headers)
                
                if resp.status != 200:
                    print(f"  Bing search failed with status: {resp.status}")
                    return None
                
                soup = BeautifulSoup(resp.text, "html.parser")
                results = []
                
                seen = set()
                for result_elem in soup.select("li.b_algo, div.b_caption"):
                    raw_text = result_elem.get_text()
                    text_key = raw_text.strip()[:100]
                    if text_key in seen:
                        continue
                    seen.add(text_key)
                    
                    import re
                    rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', raw_text, re.IGNORECASE)
                    review_match = re.search(r'([\d,]+)\s*(?:review|Reviews)', raw_text, re.IGNORECASE)
                    ceo_match = re.search(r'(\d+)%\s*(?:approve|CEO)', raw_text, re.IGNORECASE)
                    recommend_match = re.search(r'(\d+)%\s*(?:recommend|friend)', raw_text, re.IGNORECASE)
                    
                    if any([rating_match, review_match, ceo_match, recommend_match]):
                        results.append({
                            'raw_score': float(rating_match.group(1)) if rating_match else None,
                            'review_count': int(review_match.group(1).replace(',', '')) if review_match else None,
                            'ceo_approval': int(ceo_match.group(1)) if ceo_match else None,
                            'recommend_to_friend': int(recommend_match.group(1)) if recommend_match else None,
                            'text_preview': raw_text[:200]
                        })
                
                return results
                
        except Exception as e:
            print(f"  Bing search error: {e}")
            return None
    
    try:
        import aiohttp
        results = await find_glassdoor_data()
        
        if results:
            print(f"  Found {len(results)} potential Glassdoor results:")
            for i, result in enumerate(results[:3]):
                print(f"  Result {i+1}:")
                print(f"    Raw score: {result['raw_score']}")
                print(f"    CEO approval: {result['ceo_approval']}%")
                print(f"    Reviews: {result['review_count']}")
                print(f"    Recommend to friend: {result['recommend_to_friend']}%")
                print(f"    Text: {result['text_preview']}")
        else:
            print("  No Bing search results found")
            
    except Exception as e:
        print(f"  Bing search test failed: {e}")

async def test_curl_cffi_with_ja4():
    """Test curl_cffi directly with JA4 headers"""
    print("\n" + "="*60)
    print("TESTING curl_cffi WITH JA4 HEADERS")
    print("="*60)
    
    from curl_cffi import AsyncSession
    from psychological.scrapers.proxy_manager import build_ja4_headers
    
    try:
        async with AsyncSession(impersonate="chrome124", timeout=30) as session:
            headers = build_ja4_headers({"Referer": "https://www.glassdoor.com/index.htm"})
            
            # Test direct Glassdoor
            test_urls = [
                "https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E11505.htm",
                "https://www.glassdoor.com/Reviews/Broadcom-Reviews-E41843.htm"
            ]
            
            for url in test_urls:
                print(f"\nTesting: {url}")
                try:
                    resp = await asyncio.wait_for(session.get(url, headers=headers), timeout=45.0)
                    print(f"  Status: {resp.status}")
                    print(f"  Content length: {len(resp.text)}")
                    
                    # Extract some basic info
                    if resp.text and len(resp.text) > 500:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(resp.text, "html.parser")
                        
                        # Try to find rating
                        rating_selectors = [
                            "div[data-test='overallRating']",
                            "span[data-test='overall-rating']",
                            ".ratingNumber",
                            "[class*='rating'] [class*='number']",
                            ".bigRating strong",
                            ".ratingNum"
                        ]
                        
                        rating_elem = None
                        for selector in rating_selectors:
                            rating_elem = soup.select_one(selector)
                            if rating_elem:
                                break
                        
                        if rating_elem:
                            rating_text = rating_elem.get_text(strip=True)
                            print(f"  Found potential rating: {rating_text}")
                        else:
                            # Fallback to regex search
                            import re
                            match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', resp.text, re.IGNORECASE)
                            if match:
                                print(f"  Found rating via regex: {match.group(1)}")
                        
                        # Find review count
                        import re
                        review_match = re.search(r'([\d,]+)\s*(?:review|Reviews)', resp.text, re.IGNORECASE)
                        if review_match:
                            print(f"  Found reviews via regex: {review_match.group(1)}")
                        
                        # Stop after first successful test
                        break
                    else:
                        print(f"  Response too short: {len(resp.text)} bytes")
                        
                except asyncio.TimeoutError:
                    print(f"  Timeout for {url}")
                except Exception as e:
                    print(f"  Error for {url}: {e}")
                    
    except Exception as e:
        print(f"curl_cffi test failed: {e}")

async def main():
    try:
        import aiohttp
        print("🔍 Starting binary search for anti-bot solutions...")
        
        await test_proxy_sources()
        await test_bing_search()
        await test_curl_cffi_with_ja4()
        
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(WORKSPACE))
    asyncio.run(main())
