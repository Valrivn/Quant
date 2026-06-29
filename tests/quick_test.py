#!/usr/bin/env python3
"""
Quick test to verify core infrastructure and scraper imports.
"""
import asyncio
import sys

async def main():
    # Create event loop if needed
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    print("Testing core imports...")
    
    # Test basic imports
    try:
        from psychological.scrapers.corp_audit import GlassdoorScraper, G2EmployerScraper
        print("✓ corp_audit imports successful")
    except Exception as e:
        print(f"✗ corp_audit import failed: {e}")
        return
    
    # Test with minimal config
    min_config = {
        'glassdoor': {
            'company_slugs': {
                'NVDA': 'nvidia',
                'AVGO': 'broadcom', 
                'INTC': 'intel',
                'AMD': 'advanced-micro-devices'
            }
        },
        'proxy': {
            'sources': ['https://free-proxy-list.net/'],
            'min_proxies': 1,
            'refresh_interval': 600,
        }
    }
    
    try:
        print("\nTesting GlassdoorScraper initialization...")
        gd = GlassdoorScraper(config_dict=min_config)
        await gd.initialize()
        print("✓ GlassdoorScraper initialized")
        
        # Test with timeout
        try:
            result = await asyncio.wait_for(gd.scrape_company('NVDA'), timeout=120.0)
            print(f"✓ Glassdoor scrape result: {result.raw_score}/5.0 (CEO: {result.ceo_approval}%)")
        except asyncio.TimeoutError:
            print("✗ Glassdoor scrape timed out (this is expected in this test environment)")
        except Exception as e:
            print(f"✗ Glassdoor scrape failed: {e}")
        
        await gd.close()
    except Exception as e:
        print(f"✗ Glassdoor setup failed: {e}")
    
    print("\nTesting antigravity daemon scan...")
    try:
        from opencode_scripts.antigravity_daemon import scan_for_hardcoding
        result = scan_for_hardcoding("")
        print(f"✓ antigravity daemon scan: {len(result)} hardcoded patterns found")
    except Exception as e:
        print(f"✗ antigravity daemon scan failed: {e}")

if __name__ == "__main__":
    sys.path.insert(0, '/Users/hayden/Desktop/quant-py')
    asyncio.run(main())
