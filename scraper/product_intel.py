import requests
from bs4 import BeautifulSoup
import json
import time
import random
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ProductIntelScraper:
    """
    A collection of scrapers for gathering product intelligence from various platforms
    like G2, Capterra, and App Store.
    """

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/91.0.864.59 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
    ]

    def _get_random_user_agent(self):
        return random.choice(self.USER_AGENTS)

    def _fetch_page(self, url, params=None, headers=None):
        """Helper to fetch a page with retries and random user agents."""
        if headers is None:
            headers = {}
        headers['User-Agent'] = self._get_random_user_agent()

        for attempt in range(3): # Retry up to 3 times
            try:
                response = requests.get(url, params=params, headers=headers, timeout=10)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                return response
            except requests.exceptions.RequestException as e:
                logging.warning(f"Attempt {attempt+1} failed for {url}: {e}")
                time.sleep(2 ** attempt) # Exponential backoff
        logging.error(f"Failed to fetch {url} after multiple attempts.")
        return None

    def scrape_g2_reviews(self, product_name: str, max_pages: int = 5) -> list:
        """
        Scrapes G2.com for product reviews.
        Note: G2 uses heavy JavaScript, direct scraping might be limited without a headless browser.
              This implementation attempts to find review links from search results.
        """
        logging.info(f"Scraping G2 for product: {product_name}")
        base_url = "https://www.g2.com"
        search_url = f"{base_url}/search?query={requests.utils.quote(product_name)}"
        reviews_data = []

        response = self._fetch_page(search_url)
        if not response:
            return reviews_data

        soup = BeautifulSoup(response.text, 'html.parser')
        # Try to find the product page link from search results
        # IMPORTANT: The selectors below are examples and may need to be updated
        # based on the current live HTML structure of G2.com.
        product_link_tag = soup.find('a', class_='product-card__product-name') # Example selector
        if product_link_tag and product_link_tag.has_attr('href'):
            product_page_url = base_url + product_link_tag['href']
            logging.info(f"Found G2 product page: {product_page_url}")

            # Now navigate to the product page and look for reviews
            # G2 reviews are often loaded dynamically or paginated.
            # This is a placeholder for a more complex review extraction logic.
            # For a full solution, one might need to inspect network requests for review APIs.
            reviews_url = f"{product_page_url}/reviews" # Common pattern
            for page in range(1, max_pages + 1):
                page_url = f"{reviews_url}?page={page}"
                review_response = self._fetch_page(page_url)
                if not review_response:
                    break
                review_soup = BeautifulSoup(review_response.text, 'html.parser')
                # Example: Find review containers (selectors would need to be precise)
                # IMPORTANT: The selectors below are examples and may need to be updated
                # based on the current live HTML structure of G2.com.
                review_elements = review_soup.find_all('div', class_='review-card') # Placeholder class
                if not review_elements:
                    logging.info(f"No more reviews found on G2 page {page} for {product_name}.")
                    break

                for review_el in review_elements:
                    # Extract review details (e.g., title, rating, text, author)
                    title = review_el.find('h3', class_='review-title').text.strip() if review_el.find('h3', class_='review-title') else 'N/A'
                    rating = review_el.find('div', class_='rating-value').text.strip() if review_el.find('div', class_='rating-value') else 'N/A'
                    text = review_el.find('div', class_='review-text').text.strip() if review_el.find('div', class_='review-text') else 'N/A'
                    reviews_data.append({
                        'platform': 'G2',
                        'product_name': product_name,
                        'title': title,
                        'rating': rating,
                        'text': text,
                        'url': page_url
                    })
                time.sleep(random.uniform(1, 3)) # Be polite

        if not reviews_data:
            logging.warning(f"Could not find or scrape reviews for {product_name} on G2. Consider refining selectors or using a headless browser.")
        return reviews_data

    def scrape_capterra_reviews(self, product_name: str, max_pages: int = 5) -> list:
        """
        Scrapes Capterra.com for product reviews.
        Similar to G2, Capterra also uses dynamic content.
        """
        logging.info(f"Scraping Capterra for product: {product_name}")
        base_url = "https://www.capterra.com"
        search_url = f"{base_url}/search?q={requests.utils.quote(product_name)}"
        reviews_data = []

        response = self._fetch_page(search_url)
        if not response:
            return reviews_data

        soup = BeautifulSoup(response.text, 'html.parser')
        # Try to find the product page link
        # IMPORTANT: The selectors below are examples and may need to be updated
        # based on the current live HTML structure of Capterra.com.
        product_link_tag = soup.find('a', class_='ProductCard_productName__1_X_Y') # Example selector
        if product_link_tag and product_link_tag.has_attr('href'):
            product_page_url = base_url + product_link_tag['href']
            logging.info(f"Found Capterra product page: {product_page_url}")

            # Capterra reviews are often on a dedicated reviews tab or paginated.
            # This is a placeholder.
            reviews_url = f"{product_page_url}/reviews" # Common pattern
            for page in range(1, max_pages + 1):
                page_url = f"{reviews_url}?page={page}"
                review_response = self._fetch_page(page_url)
                if not review_response:
                    break
                review_soup = BeautifulSoup(review_response.text, 'html.parser')
                # IMPORTANT: The selectors below are examples and may need to be updated
                # based on the current live HTML structure of Capterra.com.
                review_elements = review_soup.find_all('div', class_='ReviewCard_reviewCard__2_Z_X') # Placeholder class
                if not review_elements:
                    logging.info(f"No more reviews found on Capterra page {page} for {product_name}.")
                    break

                for review_el in review_elements:
                    title = review_el.find('h3', class_='ReviewCard_title__3_A_B').text.strip() if review_el.find('h3', class_='ReviewCard_title__3_A_B') else 'N/A'
                    rating = review_el.find('span', class_='Rating_ratingValue__4_C_D').text.strip() if review_el.find('span', class_='Rating_ratingValue__4_C_D') else 'N/A'
                    text = review_el.find('div', class_='ReviewCard_text__5_E_F').text.strip() if review_el.find('div', class_='ReviewCard_text__5_E_F') else 'N/A'
                    reviews_data.append({
                        'platform': 'Capterra',
                        'product_name': product_name,
                        'title': title,
                        'rating': rating,
                        'text': text,
                        'url': page_url
                    })
                time.sleep(random.uniform(1, 3)) # Be polite

        if not reviews_data:
            logging.warning(f"Could not find or scrape reviews for {product_name} on Capterra. Consider refining selectors or using a headless browser.")
        return reviews_data

    def scrape_app_store_reviews(self, app_id: str, country: str = 'us', max_reviews: int = 100) -> list:
        """
        Scrapes Apple App Store reviews for a given app ID.
        Uses the iTunes RSS feed for reviews, which is more stable than HTML scraping.
        App ID can be found in the app's URL (e.g., idXXXXXXXXX).
        """
        logging.info(f"Scraping App Store for app ID: {app_id} in country: {country}")
        # iTunes RSS feed for reviews: https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostrecent/json
        # Or for a specific page: https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/page={page_number}/sortBy=mostrecent/json
        # Note: The RSS feed provides a limited number of reviews (usually 50-100 per feed).
        # For more extensive scraping, a dedicated App Store API or a more complex web scraping
        # approach (e.g., using Selenium to interact with the web page) would be needed.

        reviews_data = []
        # The RSS feed typically returns a fixed number of reviews, not paginated in the traditional sense.
        # We'll fetch the main feed.
        feed_url = f"https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostrecent/json"

        response = self._fetch_page(feed_url)
        if not response:
            return reviews_data

        try:
            data = response.json()
            entries = data.get('feed', {}).get('entry', [])
            if not entries:
                logging.info(f"No reviews found in App Store RSS feed for app ID {app_id}.")
                return reviews_data

            # The first entry is usually app metadata, actual reviews start from the second.
            # Check if entries is a list and has more than one element.
            if isinstance(entries, list) and len(entries) > 1:
                # Skip the first entry which is often the app itself, not a review
                # Sometimes the first entry is also a review, so we need to check its structure.
                # A more robust check would be to look for specific keys like 'im:rating'
                for entry in entries:
                    if 'im:rating' in entry and 'title' in entry and 'content' in entry:
                        rating = entry['im:rating']['label']
                        title = entry['title']['label']
                        text = entry['content']['label']
                        author = entry['author']['name']['label'] if 'author' in entry and 'name' in entry['author'] else 'Anonymous'
                        reviews_data.append({
                            'platform': 'App Store',
                            'app_id': app_id,
                            'country': country,
                            'title': title,
                            'rating': rating,
                            'text': text,
                            'author': author,
                            'url': feed_url # The feed URL itself
                        })
                        if len(reviews_data) >= max_reviews:
                            break
            else:
                logging.warning(f"App Store RSS feed for {app_id} returned unexpected structure or no review entries.")

        except json.JSONDecodeError as e:
            logging.error(f"Failed to decode JSON from App Store RSS feed for {app_id}: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred while scraping App Store for {app_id}: {e}")

        if not reviews_data:
            logging.warning(f"Could not scrape reviews for app ID {app_id} on App Store. Check app ID or country.")
        return reviews_data

if __name__ == "__main__":
    scraper = ProductIntelScraper()

    print("--- G2 Scraper Test ---")
    g2_product = "Slack" # Example product
    g2_reviews = scraper.scrape_g2_reviews(g2_product, max_pages=1)
    if g2_reviews:
        print(f"Found {len(g2_reviews)} G2 reviews for {g2_product}. Sample:")
        for review in g2_reviews[:2]:
            print(f"  Title: {review['title']}, Rating: {review['rating']}, Text: {review['text'][:100]}...")
    else:
        print(f"No G2 reviews found for {g2_product}.")

    print("\n--- Capterra Scraper Test ---")
    capterra_product = "Zoom" # Example product
    capterra_reviews = scraper.scrape_capterra_reviews(capterra_product, max_pages=1)
    if capterra_reviews:
        print(f"Found {len(capterra_reviews)} Capterra reviews for {capterra_product}. Sample:")
        for review in capterra_reviews[:2]:
            print(f"  Title: {review['title']}, Rating: {review['rating']}, Text: {review['text'][:100]}...")
    else:
        print(f"No Capterra reviews found for {capterra_product}.")

    print("\n--- App Store Scraper Test ---")
    # Example App ID for Slack (US App Store) - you'd need to find actual IDs
    # A quick search for "Slack app store id" suggests 803453959 for iOS
    app_store_id = "803453959"
    app_store_reviews = scraper.scrape_app_store_reviews(app_store_id, country='us', max_reviews=5)
    if app_store_reviews:
        print(f"Found {len(app_store_reviews)} App Store reviews for app ID {app_store_id}. Sample:")
        for review in app_store_reviews[:2]:
            print(f"  Title: {review['title']}, Rating: {review['rating']}, Author: {review['author']}, Text: {review['text'][:100]}...")
    else:
        print(f"No App Store reviews found for app ID {app_store_id}.")
