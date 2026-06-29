import pytest
from scraper.dynamic_extractor import DynamicExtractor

def test_html_to_clean_text():
    raw_html = """
    <html>
        <head><style>body { color: red; }</style></head>
        <body>
            <header>Header Content</header>
            <div class="review-card">
                <h3>Great product!</h3>
                <p>Loved using this tool, 5 stars.</p>
            </div>
            <script>console.log("ignore me");</script>
        </body>
    </html>
    """
    extractor = DynamicExtractor()
    clean_text = extractor.html_to_clean_text(raw_html)
    
    assert "Great product!" in clean_text
    assert "Loved using this tool, 5 stars." in clean_text
    assert "console.log" not in clean_text
    assert "color: red" not in clean_text

def test_dynamic_extractor_initialization():
    extractor = DynamicExtractor(api_key="test_key", model="gemini-2.5-flash")
    assert extractor.api_key == "test_key"
    assert extractor.model == "gemini-2.5-flash"
