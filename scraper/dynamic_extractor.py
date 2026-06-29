import os
import json
import re
import logging
import requests
from typing import Dict, Any, Optional, List
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class DynamicExtractor:
    """
    Dynamic semantic extraction engine using Gemini API to parse structured data
    from unstructured webpage HTML/Markdown without relying on rigid CSS templates.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    def html_to_clean_text(self, html_content: str, max_chars: int = 15000) -> str:
        """Strips tags, scripts, and styling to produce concise text for the LLM."""
        if not html_content:
            return ""
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Remove script and style elements
        for element in soup(["script", "style", "header", "footer", "nav", "svg", "noscript"]):
            element.decompose()
            
        text = soup.get_text(separator="\n")
        # Collapse multiple blank lines
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        clean_text = "\n".join(chunk for chunk in chunks if chunk)
        
        return clean_text[:max_chars]

    def extract_structured_data(
        self, 
        html_or_text: str, 
        extraction_prompt: str, 
        schema_description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Extracts structured data from webpage content using Gemini API.
        """
        if not self.api_key:
            logger.error("GEMINI_API_KEY is missing. Dynamic extraction unavailable.")
            return {"error": "Missing API Key", "data": []}

        cleaned_text = self.html_to_clean_text(html_or_text)
        if not cleaned_text:
            return {"data": []}

        system_instruction = (
            "You are an expert data extraction assistant. Your task is to extract exact structured data "
            "from raw web page content according to the requested instructions. "
            "Respond ONLY with valid JSON."
        )

        prompt = f"{extraction_prompt}\n\n"
        if schema_description:
            prompt += f"Required JSON Format/Schema:\n{schema_description}\n\n"
        prompt += f"Webpage Content:\n{cleaned_text}"

        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1
            }
        }

        try:
            url = f"{self.api_url}?key={self.api_key}"
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code == 200:
                result_json = response.json()
                candidates = result_json.get("candidates", [])
                if candidates:
                    text_response = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                    return json.loads(text_response)

            logger.warning(f"Gemini API returned {response.status_code}. Using fallback regex pattern extractor.")
            return self._regex_fallback_extract(cleaned_text)

        except Exception as e:
            logger.warning(f"Failed dynamic extraction via API ({e}). Using regex pattern fallback.")
            return self._regex_fallback_extract(cleaned_text)

    def _regex_fallback_extract(self, text: str) -> Dict[str, Any]:
        """Fallback regex extraction when LLM API is unavailable."""
        raw_score = None
        review_count = None
        ceo_approval = None
        recommend_to_friend = None

        rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', text, re.IGNORECASE)
        if rating_match:
            raw_score = float(rating_match.group(1))

        review_match = re.search(r'([\d,]+)\s*reviews?', text, re.IGNORECASE)
        if review_match:
            review_count = int(review_match.group(1).replace(',', ''))

        ceo_match = re.search(r'(\d+)%\s*(?:CEO|approve)', text, re.IGNORECASE)
        if ceo_match:
            ceo_approval = int(ceo_match.group(1))

        recommend_match = re.search(r'(\d+)%\s*(?:recommend|friend)', text, re.IGNORECASE)
        if recommend_match:
            recommend_to_friend = int(recommend_match.group(1))

        return {
            "raw_score": raw_score,
            "review_count": review_count,
            "ceo_approval": ceo_approval,
            "recommend_to_friend": recommend_to_friend
        }
