import unittest
from unittest.mock import MagicMock, patch
import asyncio

# Assuming lightweight_scraper is in the same directory
from .lightweight_scraper import UnifiedScraperSession

class TestUnifiedScraperSession(unittest.TestCase):

    @patch('psychological.scrapers.lightweight_scraper.SB')
    def setUp(self, mock_sb_class):
        # Mock the SeleniumBase SB instance
        self.mock_sb = MagicMock()
        # Make the context manager __enter__ return the mock_sb itself
        self.mock_sb.__enter__.return_value = self.mock_sb
        mock_sb_class.return_value = self.mock_sb
        
        # Mock the driver property on SB
        self.mock_driver = MagicMock()
        self.mock_sb.driver = self.mock_driver
        
        # Initialize UnifiedScraperSession, which will now use our mock SB
        self.session = UnifiedScraperSession()
        self.session.initialize()

    def tearDown(self):
        # Close session
        self.session.close()

    def test_basic_scrape_success(self):
        mock_url = "http://example.com/test"
        
        # Configure the mock SB behavior
        self.mock_sb.get.return_value = None
        
        # Run throttled_get using asyncio
        success = asyncio.run(self.session.throttled_get(mock_url))
        
        self.assertTrue(success)
        self.mock_sb.get.assert_called_once_with(mock_url)

