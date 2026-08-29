import sys
import urllib.parse

import requests

name = sys.argv[1] if len(sys.argv) > 1 else "Snowflake"
q = urllib.parse.quote('"%s" sourcelang:english' % name)
url = (
    "https://api.gdeltproject.org/api/v2/doc/doc?query=%s&mode=artlist"
    "&format=json&maxrecords=10&timespan=1d" % q
)
r = requests.get(url, timeout=30)
print(r.status_code, len(r.content))
print(r.text[:400])
